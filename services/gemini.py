"""Gemini-backed static analysis for the ``/scan`` command.

Uses the modern ``google-genai`` SDK (``from google import genai``), not the
deprecated ``google-generativeai`` package.

The analyzer never executes or modifies the scanned code: it only sends
sanitized text to Gemini and parses structured JSON back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

from services.scanner import ScannedFile

try:  # Import defensively so a missing dependency never breaks bot startup.
    from google import genai
    from google.genai import types as genai_types

    _SDK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on install state
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    _SDK_IMPORT_ERROR = exc

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_CHUNKS = 8
DEFAULT_MAX_BYTES_PER_CHUNK = 180_000
MAX_FINDINGS_PER_CHUNK = 25
MAX_FINDINGS_TOTAL = 50

VALID_SEVERITIES: tuple[str, ...] = ("CRITICAL", "WARNING", "INFO")
SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}

# Keep this instruction exactly as specified: it defines the contract with the
# model and is what keeps the output trustworthy and parseable.
SYSTEM_INSTRUCTION = """You are a careful software diagnostics assistant.
Analyze ONLY the source code and files provided to you.
Find:
- likely bugs
- runtime failures
- incorrect logic
- security problems
- configuration problems
- obvious performance problems
- broken API usage
- missing error handling
- dangerous assumptions
Do not invent files, functions, APIs, line numbers, or behavior.
Only report issues supported by the provided source.
Every issue must have a severity:
CRITICAL
WARNING
INFO
CRITICAL: A likely serious security problem, data loss issue, application crash, or severe correctness problem.
WARNING: A credible bug, reliability problem, security risk, or potentially incorrect behavior.
INFO: A useful improvement, maintainability issue, minor optimization, or code-quality concern.
Return structured JSON."""

_FILE_BLOCK_OVERHEAD = 200  # "=== FILE: ... ===" delimiters, roughly


class GeminiError(Exception):
    """Raised when Gemini cannot be used or its answer cannot be trusted."""


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    description: str
    file: str | None = None
    line: int | None = None
    suggested_fix: str | None = None

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        if self.file:
            return self.file
        return "project"


@dataclass
class AnalysisResult:
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    chunks_total: int = 0
    chunks_analyzed: int = 0
    omitted_files: int = 0
    errors: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {severity: 0 for severity in VALID_SEVERITIES}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.chunks_total > 1:
            notes.append(
                f"Analyzed in {self.chunks_total} chunks "
                f"({self.chunks_analyzed} succeeded)."
            )
        if self.omitted_files:
            notes.append(
                f"{self.omitted_files} file(s) were not analyzed "
                "(chunk budget reached)."
            )
        return notes


# ---------------------------------------------------------------------------
# JSON parsing / validation
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json(text: str) -> object:
    """Parse JSON from a model answer, tolerating code fences and prose."""
    candidate = _FENCE_RE.sub("", text.strip())

    for attempt in (candidate, text):
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to the outermost JSON object or array inside the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise GeminiError("Gemini returned invalid JSON")


def _clean_str(value: object, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _clean_line(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            number = int(match.group())
            return number if number > 0 else None
    return None


def _clean_file(value: object) -> str | None:
    text = _clean_str(value, 300)
    if not text or text.lower() in {"unknown", "n/a", "none", "null"}:
        return None
    return text.lstrip("./").replace("\\", "/") or None


def parse_analysis_payload(payload: object) -> tuple[str, list[Finding]]:
    """Validate a decoded Gemini payload.

    Accepts ``{"summary": ..., "findings": [...]}`` and, defensively, a bare
    list of findings. Unknown severities are downgraded to ``INFO`` rather than
    discarding the finding, and malformed entries are dropped.
    """
    if isinstance(payload, list):
        raw_findings = payload
        summary: object = ""
    elif isinstance(payload, dict):
        raw_findings = payload.get("findings", [])
        summary = payload.get("summary") or payload.get("overview") or ""
    else:
        raise GeminiError("Gemini returned JSON in an unexpected shape")

    if isinstance(raw_findings, dict):
        raw_findings = [raw_findings]
    if not isinstance(raw_findings, list):
        raise GeminiError("Gemini returned no 'findings' list")

    findings: list[Finding] = []

    for entry in raw_findings[:MAX_FINDINGS_PER_CHUNK]:
        if not isinstance(entry, dict):
            continue

        title = _clean_str(
            entry.get("title") or entry.get("issue") or entry.get("name"), 200
        )
        description = _clean_str(
            entry.get("description") or entry.get("details") or entry.get("problem"),
            1800,
        )
        if not title and not description:
            continue

        severity = _clean_str(entry.get("severity") or entry.get("level"), 20).upper()
        if severity not in VALID_SEVERITIES:
            if severity:
                log.debug("Normalizing unknown severity %r to INFO", severity)
            severity = "INFO"

        findings.append(
            Finding(
                severity=severity,
                title=title or "Possible issue",
                description=description or "No description provided by the model.",
                file=_clean_file(entry.get("file") or entry.get("path")),
                line=_clean_line(entry.get("line") or entry.get("line_number")),
                suggested_fix=_clean_str(
                    entry.get("suggested_fix")
                    or entry.get("fix")
                    or entry.get("recommendation"),
                    1200,
                )
                or None,
            )
        )

    return _clean_str(summary, 2000), findings


def deduplicate_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Drop duplicate findings, keeping the most severe variant."""
    best: dict[tuple[str, str, int, str], Finding] = {}

    for finding in findings:
        key = (
            (finding.file or "").lower(),
            finding.line or 0,
            re.sub(r"\W+", " ", finding.title.lower()).strip(),
            finding.description.lower()[:160],
        )
        existing = best.get(key)
        if existing is None:
            best[key] = finding
        elif SEVERITY_ORDER[finding.severity] < SEVERITY_ORDER[existing.severity]:
            best[key] = finding

    ordered = sorted(
        best.values(),
        key=lambda f: (
            SEVERITY_ORDER[f.severity],
            (f.file or "").lower(),
            f.line or 0,
            f.title.lower(),
        ),
    )
    return ordered


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_files(
    files: Sequence[ScannedFile],
    *,
    max_bytes_per_chunk: int = DEFAULT_MAX_BYTES_PER_CHUNK,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> tuple[list[list[ScannedFile]], int]:
    """Greedily pack files into Gemini requests.

    Returns ``(chunks, omitted_file_count)``. Files are never split; when the
    chunk budget runs out the leftovers are simply not analyzed, so a large
    project degrades gracefully instead of failing.
    """
    chunks: list[list[ScannedFile]] = []
    current: list[ScannedFile] = []
    current_size = 0
    omitted = 0
    stopped = False

    for scanned in files:
        size = scanned.size_bytes + _FILE_BLOCK_OVERHEAD

        if stopped:
            omitted += 1
            continue

        if current and current_size + size > max_bytes_per_chunk:
            chunks.append(current)
            current = []
            current_size = 0
            if len(chunks) >= max_chunks:
                stopped = True
                omitted += 1
                continue

        current.append(scanned)
        current_size += size

    if current:
        if len(chunks) < max_chunks:
            chunks.append(current)
        else:  # pragma: no cover - defensive
            omitted += len(current)

    return chunks, omitted


def build_chunk_prompt(chunk: Sequence[ScannedFile], index: int, total: int) -> str:
    """Render one Gemini request body for a chunk of sanitized files."""
    parts = [
        f"Analyze part {index} of {total} of a project snapshot.",
        "Each file below is delimited by '=== FILE: <path> ===' and "
        "'=== END FILE: <path> ==='.",
        "Secrets were replaced with [REDACTED] before this request; do NOT "
        "report [REDACTED] values as exposed credentials and do not guess "
        "their contents.",
        f"Report at most {MAX_FINDINGS_PER_CHUNK} findings for this part, most "
        "severe first, and only findings you can support with the code shown.",
        "Use the exact relative file paths shown below. Never invent files, "
        "functions, APIs, or line numbers; omit \"line\" when unsure.",
        "Respond with JSON only, in this shape:",
        '{"summary": "short overall analysis", "findings": ['
        '{"severity": "CRITICAL|WARNING|INFO", "title": "Example issue", '
        '"description": "Explain the problem", "file": "src/example.py", '
        '"line": 42, "suggested_fix": "Explain how to fix it"}]}',
        "",
    ]

    for scanned in chunk:
        parts.append(f"=== FILE: {scanned.path} ===")
        parts.append(scanned.content)
        parts.append(f"=== END FILE: {scanned.path} ===")
        parts.append("")

    return "\n".join(parts)


def combine_summaries(summaries: Sequence[str]) -> str:
    """Merge per-chunk summaries into one report summary."""
    cleaned = [summary.strip() for summary in summaries if summary.strip()]
    if not cleaned:
        return "Gemini did not return a summary."
    if len(cleaned) == 1:
        return cleaned[0]

    joined = "\n\n".join(
        f"**Part {i}:** {summary}" for i, summary in enumerate(cleaned, 1)
    )
    if len(joined) > 1900:
        joined = joined[:1899].rstrip() + "…"
    return joined


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class GeminiAnalyzer:
    """Thin async wrapper around the google-genai client."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_bytes_per_chunk: int = DEFAULT_MAX_BYTES_PER_CHUNK,
    ) -> None:
        if not api_key or not api_key.strip():
            raise GeminiError(
                "GEMINI_API_KEY is missing. Add it to .env to use /scan."
            )
        if _SDK_IMPORT_ERROR is not None or genai is None:
            raise GeminiError(
                "google-genai is not installed or could not be imported "
                f"({_SDK_IMPORT_ERROR}). Run: pip install -r requirements.txt"
            )

        self.model = (model or DEFAULT_MODEL).strip()
        self.timeout_seconds = timeout_seconds
        self.max_chunks = max_chunks
        self.max_bytes_per_chunk = max_bytes_per_chunk

        try:
            self._client = genai.Client(api_key=api_key.strip())
        except Exception as exc:  # pragma: no cover - depends on SDK state
            raise GeminiError(f"Could not create the Gemini client: {exc}") from exc

    async def aclose(self) -> None:
        """Release the underlying HTTP resources if the SDK exposes them."""
        closer = getattr(self._client, "aio", None)
        closer = getattr(closer, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # pragma: no cover - best effort cleanup
            log.debug("Ignoring error while closing the Gemini client", exc_info=True)

    async def _generate_json(self, prompt: str) -> tuple[str, list[Finding]]:
        """Send one prompt to Gemini and validate the JSON answer."""
        config = genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json",
        )

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise GeminiError(
                f"Gemini timed out after {self.timeout_seconds:.0f}s"
            ) from exc
        except Exception as exc:
            raise GeminiError(f"Gemini request failed: {type(exc).__name__}: {exc}") from exc

        text = getattr(response, "text", None)
        if not text and getattr(response, "candidates", None):
            try:  # Defensive fallback for SDK shape changes.
                text = response.candidates[0].content.parts[0].text
            except Exception:
                text = None

        if not text or not text.strip():
            raise GeminiError("Gemini returned an empty response")

        return parse_analysis_payload(_extract_json(text))

    async def analyze(self, files: Sequence[ScannedFile]) -> AnalysisResult:
        """Analyze sanitized files, chunking large projects into parts.

        Chunks are analyzed sequentially to stay friendly to API rate limits,
        and a failing chunk never aborts the whole report.
        """
        result = AnalysisResult()

        if not files:
            raise GeminiError("Nothing to analyze: no supported files were found")

        chunks, omitted = chunk_files(
            files,
            max_bytes_per_chunk=self.max_bytes_per_chunk,
            max_chunks=self.max_chunks,
        )
        result.chunks_total = len(chunks)
        result.omitted_files = omitted

        log.info(
            "Sending project analysis to Gemini (%d chunk(s), model=%s)",
            len(chunks),
            self.model,
        )

        summaries: list[str] = []
        collected: list[Finding] = []

        for index, chunk in enumerate(chunks, 1):
            prompt = build_chunk_prompt(chunk, index, len(chunks))
            try:
                summary, findings = await self._generate_json(prompt)
            except GeminiError as exc:
                log.warning("Chunk %d/%d failed: %s", index, len(chunks), exc)
                result.errors.append(f"Part {index}: {exc}")
                continue

            result.chunks_analyzed += 1
            summaries.append(summary)
            collected.extend(findings)

        if result.chunks_analyzed == 0:
            detail = "; ".join(result.errors) or "unknown error"
            raise GeminiError(f"Gemini analysis failed for every part: {detail}")

        result.summary = combine_summaries(summaries)
        result.findings = deduplicate_findings(collected)[:MAX_FINDINGS_TOTAL]

        log.info(
            "Gemini returned %d finding(s) (%d after de-duplication)",
            len(collected),
            len(result.findings),
        )
        return result
