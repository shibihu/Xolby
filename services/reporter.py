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
    "CRITICAL": ("🚨", 0xED4245),
    "WARNING": ("⚠️", 0xFEE75C),
    "INFO": ("ℹ️", 0x5865F2),
}
SEVERITY_LABELS = {"CRITICAL": "Critical", "WARNING": "Warning", "INFO": "Info"}


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
    """One embed per finding."""
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

    embed = discord.Embed(
        title="🛡️ System Scan Report",
        color=0x2ECC71 if counts.get("CRITICAL", 0) == 0 else SEVERITY_STYLES["CRITICAL"][1],
    )

    # Keep a fixed budget for the summary so the notes below can never be
    # pushed out of the embed by a very long AI answer.
    summary_lines = ["**AI Summary**", _clip(analysis.summary or "No summary returned.", 2_500)]
    notes = list(analysis.notes()) + list(scan.notes())
    if notes:
        summary_lines.append("")
        summary_lines.append("**Notes**")
        summary_lines.extend(f"• {_clip(note, 250)}" for note in notes)
    embed.description = _clip("\n".join(summary_lines), MAX_EMBED_DESCRIPTION)

    embed.add_field(name="Directory", value=_clip(f"`{scan.root}`", MAX_FIELD_VALUE), inline=False)
    embed.add_field(name="Files scanned", value=str(scan.file_count), inline=True)
    embed.add_field(name="Critical", value=str(counts.get("CRITICAL", 0)), inline=True)
    embed.add_field(name="Warning", value=str(counts.get("WARNING", 0)), inline=True)
    embed.add_field(name="Info", value=str(counts.get("INFO", 0)), inline=True)
    embed.add_field(name="Files skipped", value=str(scan.skipped_count), inline=True)
    embed.add_field(
        name="Mode",
        value="Read-only (no files were modified)",
        inline=True,
    )
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
    """Build the full report: summary embed first, then one embed per issue."""
    embeds = [build_summary_embed(scan, analysis, model=model)]

    findings = analysis.findings[:max_issue_embeds]
    embeds.extend(build_issue_embed(finding) for finding in findings)

    omitted = len(analysis.findings) - len(findings)
    if omitted > 0:
        embeds.append(
            discord.Embed(
                title="➕ Report truncated",
                description=_clip(
                    f"{omitted} more finding(s) were not shown to keep the "
                    "report within Discord's limits.",
                    MAX_EMBED_DESCRIPTION,
                ),
                color=0x95A5A6,
            )
        )

    return embeds


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
