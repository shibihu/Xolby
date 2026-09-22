"""Read-only project scanner used by the ``/scan`` command.

This module is deliberately side-effect free: it only ever *reads* files.
It never writes, renames, deletes, executes, or installs anything.

Pipeline implemented here:

    directory -> file discovery -> binary/limit filtering -> secret redaction
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (the /scan command overrides these from .env)
# ---------------------------------------------------------------------------
DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_MAX_FILES = 250
DEFAULT_MAX_TOTAL_BYTES = 800_000

# Files we can actually learn something from.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".kt",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".php",
        ".rb",
        ".swift",
        ".lua",
        ".gd",
        ".html",
        ".css",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".md",
        ".txt",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
    }
)

# Directories that never contain our own source, or that we must not touch.
SKIPPED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".idea",
        ".vscode",
        "dist",
        "build",
        # extra caches / vendored trees (same spirit as the list above)
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        ".gradle",
        "target",
        "vendor",
        "site-packages",
        "Pods",
    }
)

# Never read these, even though their extension may look supported.
SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.test",
        ".env.staging",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".htpasswd",
        ".git-credentials",
    }
)

SENSITIVE_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk"}
)

# Any path segment with one of these names is treated as a secret store.
SENSITIVE_PATH_SEGMENTS: frozenset[str] = frozenset(
    {".ssh", ".gnupg", "secrets", ".aws"}
)

REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class ScanError(Exception):
    """Raised when the scan target itself cannot be scanned."""


@dataclass(frozen=True)
class ScannedFile:
    """A single sanitized source file that is safe to send to Gemini."""

    path: str  # relative, POSIX-style, e.g. "services/api.py"
    content: str  # already sanitized
    size_bytes: int  # size on disk (before sanitization)
    redactions: int = 0


@dataclass
class ScanResult:
    root: str
    files: list[ScannedFile] = field(default_factory=list)
    skipped_sensitive: list[str] = field(default_factory=list)
    skipped_too_large: list[str] = field(default_factory=list)
    skipped_binary: list[str] = field(default_factory=list)
    skipped_unreadable: list[str] = field(default_factory=list)
    skipped_unsupported: int = 0
    hit_file_limit: bool = False
    hit_total_limit: bool = False
    total_bytes: int = 0
    redactions: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def skipped_count(self) -> int:
        return (
            len(self.skipped_sensitive)
            + len(self.skipped_too_large)
            + len(self.skipped_binary)
            + len(self.skipped_unreadable)
            + self.skipped_unsupported
        )

    def notes(self) -> list[str]:
        """Short human-readable notes about anything that was not analyzed."""
        notes: list[str] = []
        if self.hit_file_limit:
            notes.append(
                f"Stopped at the SCAN_MAX_FILES limit ({self.file_count} files scanned)."
            )
        if self.hit_total_limit:
            notes.append("Stopped at the total input size limit for one scan.")
        if self.skipped_unsupported:
            notes.append(f"{self.skipped_unsupported} unsupported file(s) ignored.")
        if self.skipped_sensitive:
            notes.append(
                f"{len(self.skipped_sensitive)} sensitive file(s) never read "
                "(.env / keys / credentials)."
            )
        if self.skipped_too_large:
            notes.append(
                f"{len(self.skipped_too_large)} file(s) skipped for exceeding "
                "SCAN_MAX_FILE_BYTES."
            )
        if self.skipped_binary:
            notes.append(f"{len(self.skipped_binary)} binary file(s) skipped.")
        if self.skipped_unreadable:
            notes.append(
                f"{len(self.skipped_unreadable)} file(s) could not be read "
                "(permissions or encoding)."
            )
        if self.redactions:
            notes.append(
                f"{self.redactions} likely secret value(s) redacted before analysis."
            )
        return notes


# ---------------------------------------------------------------------------
# Secret sanitization
# ---------------------------------------------------------------------------
# Names that almost always hold a credential when they appear as a key.
_SECRET_KEY_NAMES = (
    r"api[_-]?key|apikey|api[_-]?secret|secret|client[_-]?secret|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|id[_-]?token|token|"
    r"private[_-]?key|public[_-]?key|password|passwd|pwd|"
    r"db[_-]?password|database[_-]?url|db[_-]?url|redis[_-]?url|"
    r"webhook[_-]?url|session[_-]?secret|signing[_-]?key|encryption[_-]?key"
)

_KEY_VALUE_RE = re.compile(
    rf"""
    (?P<prefix>
        (?P<key>["']?(?:{_SECRET_KEY_NAMES})["']?)
        \s*[:=]\s*
    )
    (?P<value>"[^"\n]*"|'[^'\n]*'|\[[^\]\n]*\]|[^\s,;:#)}}\]\+]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Values that are almost certainly type annotations / placeholders, not secrets.
_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "dict",
        "list",
        "tuple",
        "set",
        "frozenset",
        "object",
        "none",
        "null",
        "any",
        "optional",
        "union",
        "callable",
        "mapping",
        "mutablemapping",
        "sequence",
        "iterable",
        "iterator",
        "text",
        "secretstr",
        "path",
        "datetime",
        "yourapikey",
        "your_api_key",
        "changeme",
    }
)

_EXTRA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private key blocks (multi-line).
    (
        re.compile(
            r"-----BEGIN[^-]{0,40}PRIVATE KEY-----.*?-----END[^-]{0,40}PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Discord bot tokens.
    (
        re.compile(r"\b[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27,}\b"),
        REDACTED,
    ),
    # Discord webhook URLs (the URL itself is the credential).
    (
        re.compile(
            r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+"
        ),
        "https://discord.com/api/webhooks/[REDACTED]",
    ),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), REDACTED),
    # AWS access key IDs.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # GitHub tokens.
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"), REDACTED),
    # Slack tokens.
    (re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"), REDACTED),
    # OpenAI-style keys.
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{16,}\b"), REDACTED),
    # Telegram bot tokens.
    (re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{30,}\b"), REDACTED),
    # JWTs.
    (
        re.compile(
            r"\beyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\b"
        ),
        REDACTED,
    ),
    # Authorization headers.
    (
        re.compile(r"(?<=[:=])\s*(?:Bearer|Basic|Token)\s+[^\s\"'\n]+", re.IGNORECASE),
        f" {REDACTED}",
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    (
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1[REDACTED]@",
    ),
)


def _looks_like_type_annotation(value: str) -> bool:
    """True for values such as ``password: str`` that are not secrets."""
    stripped = value.strip().strip("\"'").strip()
    return stripped.lower() in _TYPE_NAMES


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace likely credentials in ``text`` with ``[REDACTED]``.

    Returns ``(sanitized_text, redaction_count)``. This function is
    intentionally aggressive: a false positive only costs context, while a
    false negative would leak a credential into a third-party API call.
    """
    if not text:
        return text, 0

    count = 0
    sanitized = text

    # Token/key shapes first, so that e.g. a whole PEM block is removed as one
    # unit before the assignment rule sees (and truncates) it.
    for pattern, replacement in _EXTRA_PATTERNS:
        sanitized, hits = pattern.subn(replacement, sanitized)
        count += hits

    def _replace_assignment(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group("value")
        if _looks_like_type_annotation(value):
            return match.group(0)
        count += 1
        return f"{match.group('prefix')}{REDACTED}"

    sanitized = _KEY_VALUE_RE.sub(_replace_assignment, sanitized)

    return sanitized, count


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def _is_sensitive(relative: Path) -> bool:
    name = relative.name
    lower = name.lower()

    if lower in SENSITIVE_FILENAMES:
        return True
    if lower.startswith(".env"):
        return True
    if lower.startswith("id_rsa") or lower.startswith("id_ed25519"):
        return True
    if any(part.lower() in SENSITIVE_PATH_SEGMENTS for part in relative.parts[:-1]):
        return True
    return relative.suffix.lower() in SENSITIVE_SUFFIXES


def _is_supported(relative: Path) -> bool:
    return relative.suffix.lower() in SUPPORTED_EXTENSIONS


def _looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    # Control characters other than tab/newline/carriage return.
    control = sum(1 for byte in raw if byte < 9 or (13 < byte < 32))
    return bool(raw) and control / len(raw) > 0.10


def _iter_files(root: Path, errors: list[str]) -> Iterator[Path]:
    """Yield every candidate file below ``root``, skipping ignored dirs."""

    def onerror(exc: OSError) -> None:
        target = getattr(exc, "filename", None) or str(root)
        errors.append(f"{target}: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(
        root, onerror=onerror, followlinks=False
    ):
        # Prune in place so os.walk does not descend into them.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRECTORIES)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def _read_text(path: Path, max_bytes: int) -> tuple[str | None, int, str]:
    """Read at most ``max_bytes``; return (text, size, reason_if_skipped)."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, 0, f"stat failed: {exc.strerror or exc}"

    if size > max_bytes:
        return None, size, "too large"

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, size, f"read failed: {exc.strerror or exc}"

    if _looks_binary(raw):
        return None, size, "binary"

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return None, size, "undecodable"

    return text, size, ""


def scan_project(
    directory: str | os.PathLike[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ScanResult:
    """Scan ``directory`` and return a sanitized, read-only snapshot.

    Raises :class:`ScanError` when the target directory itself is unusable.
    Individual unreadable files are recorded in the result instead of raising.
    """
    root = Path(directory).expanduser()

    try:
        root = root.resolve()
    except OSError:
        pass

    if not root.exists():
        raise ScanError(f"SCAN_DIRECTORY does not exist: {root}")
    if not root.is_dir():
        raise ScanError(f"SCAN_DIRECTORY is not a directory: {root}")
    if not os.access(root, os.R_OK):
        raise ScanError(
            f"SCAN_DIRECTORY is not readable: {root} "
            "(check permissions; on Termux run `termux-setup-storage`)"
        )

    result = ScanResult(root=str(root))
    log.info("Scanning %s", root)

    for path in _iter_files(root, result.errors):
        try:
            relative = path.relative_to(root)
        except ValueError:  # pragma: no cover - defensive
            continue

        relative_posix = relative.as_posix()

        if _is_sensitive(relative):
            result.skipped_sensitive.append(relative_posix)
            continue

        if not _is_supported(relative):
            result.skipped_unsupported += 1
            continue

        if len(result.files) >= max_files:
            result.hit_file_limit = True
            break

        text, size, reason = _read_text(path, max_file_bytes)

        if text is None:
            if reason == "too large":
                result.skipped_too_large.append(relative_posix)
            elif reason == "binary":
                result.skipped_binary.append(relative_posix)
            else:
                result.skipped_unreadable.append(relative_posix)
                log.debug("Skipping %s (%s)", relative_posix, reason)
            continue

        if result.total_bytes + size > max_total_bytes:
            result.hit_total_limit = True
            break

        # Deterministic Python syntax validation on raw source before sanitization
        if relative.suffix.lower() == '.py':
            try:
                compile(text, relative_posix, 'exec')
            except SyntaxError as exc:
                log.debug('Deterministic syntax check on %s: line %s: %s', relative_posix, exc.lineno, exc.msg)

        sanitized, redactions = redact_secrets(text)

        result.files.append(
            ScannedFile(
                path=relative_posix,
                content=sanitized,
                size_bytes=size,
                redactions=redactions,
            )
        )
        result.total_bytes += size
        result.redactions += redactions

    hidden = len(result.skipped_sensitive)
    log.info(
        "Found %d supported file(s), %d byte(s); %d sensitive file(s) never read",
        result.file_count,
        result.total_bytes,
        hidden,
    )
    if result.hit_file_limit or result.hit_total_limit:
        log.info("Scan truncated by configured limits")

    return result
