"""Discord report rendering for the ``/scan`` command.

Everything here is presentation only: it builds embeds from an
:class:`~services.gemini.AnalysisResult` and sends them in Discord-safe
batches. Nothing is written back to the scanned project.
"""

from __future__ import annotations

import logging
from typing import Sequence

import discord

from services.gemini import AnalysisResult, Finding
from services.scanner import ScanResult

log = logging.getLogger(__name__)

# Discord hard limits (with a small safety margin on the message-wide total).
MAX_EMBEDS_PER_MESSAGE = 10
MAX_MESSAGE_CHARS = 5_800
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4_096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1_024
MAX_FOOTER = 2_048
MAX_ISSUE_EMBEDS = 40

SEVERITY_STYLES: dict[str, tuple[str, int]] = {
    "CRITICAL": ("🔴", 0xED4245),
    "WARNING": ("🟡", 0xFEE75C),
    "INFO": ("🟢", 0x5865F2),
}
SEVERITY_LABELS = {"CRITICAL": "Critical", "WARNING": "Warning", "INFO": "Info"}
SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


class ReporterError(Exception):
    """Raised when the report cannot be delivered to Discord."""


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` so Discord's embed limit is never exceeded."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _embed_size(embed: discord.Embed) -> int:
    """Rough character count, mirroring how Discord bills embed limits."""
    size = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        size += len(embed.footer.text)
    if embed.author and embed.author.name:
        size += len(embed.author.name)
    for field in embed.fields:
        size += len(field.name or "") + len(field.value or "")
    return size


def build_issue_embed(finding: Finding) -> discord.Embed:
    """One embed per finding (preserved for fallback/utility)."""
    emoji, color = SEVERITY_STYLES.get(finding.severity, SEVERITY_STYLES["INFO"])

    embed = discord.Embed(
        title=_clip(f"{emoji} {finding.severity} — {finding.title}", MAX_EMBED_TITLE),
        color=color,
    )
    embed.description = _clip(f"**File:** `{finding.location}`", MAX_EMBED_DESCRIPTION)
    embed.add_field(
        name="Description",
        value=_clip(finding.description, MAX_FIELD_VALUE),
        inline=False,
    )
    if finding.suggested_fix:
        embed.add_field(
            name="Suggested Fix",
            value=_clip(finding.suggested_fix, MAX_FIELD_VALUE),
            inline=False,
        )
    return embed


def build_summary_embed(
    scan: ScanResult,
    analysis: AnalysisResult,
    *,
    model: str | None = None,
) -> discord.Embed:
    """The main "System Scan Report" embed."""
    counts = analysis.counts()

    if counts.get("CRITICAL", 0) > 0:
        color = SEVERITY_STYLES["CRITICAL"][1]
    elif counts.get("WARNING", 0) > 0:
        color = SEVERITY_STYLES["WARNING"][1]
    else:
        color = 0x2ECC71

    embed = discord.Embed(
        title=_clip("🔍 System Scan Report", MAX_EMBED_TITLE),
        color=color,
    )

    # 1. Summary section
    raw_summary = (analysis.summary or "No summary returned.").strip()
    summary_text = _clip(" ".join(raw_summary.split()), 300)

    header_parts = [
        "📊 Summary",
        summary_text,
        "",
        f"🔴 CRITICAL: {counts.get('CRITICAL', 0)}",
        f"🟡 WARNING: {counts.get('WARNING', 0)}",
        f"🟢 INFO: {counts.get('INFO', 0)}",
        "",
    ]

    # 2. Notes section
    notes_parts = []
    notes = list(analysis.notes()) + list(scan.notes())
    if notes:
        notes_parts.append("📝 Notes")
        notes_parts.extend(f"• {_clip(note, 250)}" for note in notes)

    header_str = "\n".join(header_parts)
    notes_str = "\n".join(notes_parts) if notes_parts else ""

    # Calculate budget for issues (safe max description budget = 3800 chars)
    max_desc_budget = 3800
    used_chars = len(header_str) + (len(notes_str) + 2 if notes_str else 0)
    issues_budget = max_desc_budget - used_chars

    # 3. Group and sort findings by severity (CRITICAL -> WARNING -> INFO)
    sorted_findings = sorted(
        analysis.findings,
        key=lambda f: (
            SEVERITY_ORDER.get(f.severity, 2),
            (f.file or "").lower(),
            f.line or 0,
            f.title.lower(),
        ),
    )

    grouped: list[dict] = []
    seen: dict[tuple[int, str], dict] = {}
    for f in sorted_findings:
        key = (SEVERITY_ORDER.get(f.severity, 2), f.title.strip().lower())
        loc = f.location
        if key in seen:
            item = seen[key]
            if loc and loc not in item["locations"]:
                item["locations"].append(loc)
        else:
            item = {
                "severity": f.severity,
                "title": f.title,
                "locations": [loc] if loc else [],
                "description": f.description,
            }
            seen[key] = item
            grouped.append(item)

    issues_parts = ["⚠️ Issues", ""]
    if not grouped:
        issues_parts.append("No issues found.")
        issues_parts.append("")
    else:
        omitted = 0
        for idx, item in enumerate(grouped, 1):
            emoji = SEVERITY_STYLES.get(item["severity"], SEVERITY_STYLES["INFO"])[0]
            issue_lines = [f"{idx}. {emoji} {item['title']}"]
            for loc in item["locations"]:
                issue_lines.append(f"📁 {loc}")
            clean_desc = " ".join(item["description"].split())
            issue_lines.append(_clip(clean_desc, 180))
            issue_lines.append("")

            block = "\n".join(issue_lines)
            current_issues_str = "\n".join(issues_parts) + "\n" + block
            if len(current_issues_str) > issues_budget:
                omitted = len(grouped) - (idx - 1)
                break
            issues_parts.extend(issue_lines)

        if omitted > 0:
            issues_parts.append(f"… and {omitted} more finding(s) omitted.")
            issues_parts.append("")

    issues_str = "\n".join(issues_parts)

    description_parts = [header_str, issues_str]
    if notes_str:
        description_parts.append(notes_str)

    full_description = "\n".join(description_parts)
    embed.description = _clip(full_description, MAX_EMBED_DESCRIPTION)

    embed.set_footer(text=_clip(_footer_text(model), MAX_FOOTER))
    return embed


def _footer_text(model: str | None) -> str:
    if model:
        return f"Read-only scan • model: {model}"
    return "Read-only scan • no files were modified"


def build_report_embeds(
    scan: ScanResult,
    analysis: AnalysisResult,
    *,
    model: str | None = None,
    max_issue_embeds: int = MAX_ISSUE_EMBEDS,
) -> list[discord.Embed]:
    """Build the full report as a single summary embed."""
    return [build_summary_embed(scan, analysis, model=model)]


def batch_embeds(
    embeds: Sequence[discord.Embed],
    *,
    max_per_message: int = MAX_EMBEDS_PER_MESSAGE,
    max_message_chars: int = MAX_MESSAGE_CHARS,
) -> list[list[discord.Embed]]:
    """Split embeds into messages that stay inside Discord's limits."""
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_size = 0

    for embed in embeds:
        size = _embed_size(embed)
        too_many = len(current) >= max_per_message
        too_big = current and current_size + size > max_message_chars
        if too_many or too_big:
            batches.append(current)
            current = []
            current_size = 0
        current.append(embed)
        current_size += size

    if current:
        batches.append(current)

    return batches


async def send_report(
    channel: "discord.abc.Messageable",
    scan: ScanResult,
    analysis: AnalysisResult,
    *,
    model: str | None = None,
) -> int:
    """Send the report and return the number of messages posted.

    Raises :class:`ReporterError` if Discord refuses to accept the messages.
    """
    embeds = build_report_embeds(scan, analysis, model=model)
    batches = batch_embeds(embeds)

    log.info(
        "Sending report to Discord (%d embed(s) in %d message(s))",
        len(embeds),
        len(batches),
    )

    sent = 0
    try:
        for batch in batches:
            await channel.send(embeds=batch)
            sent += 1
    except discord.Forbidden as exc:
        raise ReporterError(
            "Discord denied sending messages/embeds to the report channel. "
            "Grant View Channel, Send Messages and Embed Links."
        ) from exc
    except discord.HTTPException as exc:
        raise ReporterError(f"Discord rejected the report: {exc}") from exc

    return sent


async def send_text(channel: "discord.abc.Messageable", content: str) -> None:
    """Small helper for error/short status messages."""
    try:
        await channel.send(content=_clip(content, 1_900))
    except discord.HTTPException as exc:
        raise ReporterError(f"Discord rejected the message: {exc}") from exc


def format_counts(analysis: AnalysisResult) -> str:
    """``Critical: 2 • Warning: 5 • Info: 8`` for status messages."""
    counts = analysis.counts()
    return " • ".join(
        f"{SEVERITY_LABELS[severity]}: {counts.get(severity, 0)}"
        for severity in ("CRITICAL", "WARNING", "INFO")
    )
