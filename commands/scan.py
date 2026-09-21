"""``/scan`` — read-only project monitoring command.

The command wires the pieces together:

    config check -> scanner.scan_project (in a worker thread)
    -> gemini.GeminiAnalyzer -> reporter.send_report -> status message

It only reads, sanitizes, analyzes and reports. It never modifies the scanned
project, never executes scanned code, and never installs anything.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from services import reporter
from services.gemini import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_MAX_BYTES_PER_CHUNK,
    DEFAULT_MAX_CHUNKS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    GeminiAnalyzer,
    GeminiError,
)
from services.reporter import ReporterError
from services.scanner import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    ScanError,
    scan_project,
)

log = logging.getLogger(__name__)

# Guard against accidental Gemini API abuse: one scan per guild per 5 minutes.
COOLDOWN_SECONDS = 300


class ConfigError(Exception):
    """Raised when .env is missing or has an unusable value."""


@dataclass(frozen=True)
class ScanConfig:
    directory: str
    report_channel_id: int
    api_key: str
    model: str
    fallback_model: str
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_bytes_per_chunk: int
    max_chunks: int
    timeout_seconds: float


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using default %d", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%d is below the minimum %d; using default", name, value, minimum)
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%s is below the minimum %s; using default", name, value, minimum)
        return default
    return value


def load_config() -> ScanConfig:
    """Read and validate the scanner configuration from the environment."""
    directory = (os.getenv("SCAN_DIRECTORY") or "").strip()
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    channel_raw = (os.getenv("REPORT_CHANNEL_ID") or "").strip()

    missing = [
        name
        for name, value in (
            ("SCAN_DIRECTORY", directory),
            ("GEMINI_API_KEY", api_key),
            ("REPORT_CHANNEL_ID", channel_raw),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(f"`{name}`" for name in missing)
            + ". Add them to `.env` and restart the bot."
        )

    if not channel_raw.isdigit():
        raise ConfigError(
            f"`REPORT_CHANNEL_ID` must be a numeric Discord channel ID, got `{channel_raw}`."
        )

    return ScanConfig(
        directory=directory,
        report_channel_id=int(channel_raw),
        api_key=api_key,
        model=(os.getenv("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL,
        fallback_model=(os.getenv("GEMINI_FALLBACK_MODEL") or "").strip() or DEFAULT_FALLBACK_MODEL,
        max_files=_env_int("SCAN_MAX_FILES", DEFAULT_MAX_FILES),
        max_file_bytes=_env_int("SCAN_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
        max_total_bytes=_env_int("SCAN_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES),
        max_bytes_per_chunk=_env_int(
            "SCAN_MAX_BYTES_PER_CHUNK", DEFAULT_MAX_BYTES_PER_CHUNK
        ),
        max_chunks=_env_int("SCAN_MAX_CHUNKS", DEFAULT_MAX_CHUNKS),
        timeout_seconds=_env_float("GEMINI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    )


class ScanCog(commands.Cog):
    """Read-only project and code monitoring."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="scan",
        description="Scan the configured project directory for bugs and issues.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS)
    async def scan(self, interaction: discord.Interaction) -> None:
        try:
            config = load_config()
        except ConfigError as exc:
            await interaction.response.send_message(f"⚙️ {exc}", ephemeral=True)
            return

        # Scanning + Gemini can take a while, so acknowledge immediately.
        await interaction.response.defer(thinking=True)
        log.info("Starting system scan")

        # 1) Read-only scan on a worker thread (never blocks the event loop).
        try:
            scan_result = await asyncio.to_thread(
                scan_project,
                config.directory,
                max_files=config.max_files,
                max_file_bytes=config.max_file_bytes,
                max_total_bytes=config.max_total_bytes,
            )
        except ScanError as exc:
            await interaction.followup.send(f"📂 {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - never crash the bot
            log.exception("Unexpected failure while scanning %s", config.directory)
            await interaction.followup.send(
                f"❌ Unexpected error while scanning: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        if not scan_result.files:
            await interaction.followup.send(
                "📂 No supported files were found in "
                f"`{scan_result.root}`. Check `SCAN_DIRECTORY` "
                "(on Termux, run `termux-setup-storage` first).",
                ephemeral=True,
            )
            return

        # 2) Analysis (async HTTP, so no thread needed here).
        analyzer: GeminiAnalyzer | None = None
        try:
            analyzer = GeminiAnalyzer(
                config.api_key,
                config.model,
                fallback_model=config.fallback_model,
                timeout_seconds=config.timeout_seconds,
                max_chunks=config.max_chunks,
                max_bytes_per_chunk=config.max_bytes_per_chunk,
            )
            analysis = await analyzer.analyze(scan_result.files)
        except GeminiError as exc:
            await interaction.followup.send(
                f"🤖 Gemini analysis failed: {exc}", ephemeral=True
            )
            return
        except Exception as exc:  # noqa: BLE001 - never crash the bot
            log.exception("Unexpected failure while analyzing the project")
            await interaction.followup.send(
                f"❌ Unexpected error during analysis: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return
        finally:
            if analyzer is not None:
                await analyzer.aclose()

        # 3) Deliver the report to the configured channel.
        try:
            channel = await self._resolve_channel(config.report_channel_id)
            messages = await reporter.send_report(
                channel, scan_result, analysis, model=config.model
            )
        except ReporterError as exc:
            await interaction.followup.send(f"📮 {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - never crash the bot
            log.exception("Unexpected failure while sending the report")
            await interaction.followup.send(
                f"❌ Unexpected error while reporting: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        log.info(
            "Scan complete: %d file(s), %d finding(s), %d message(s)",
            scan_result.file_count,
            len(analysis.findings),
            messages,
        )
        await interaction.followup.send(
            "✅ Scan complete. Report sent to "
            f"<#{config.report_channel_id}> as {messages} message(s).\n"
            f"**Files scanned:** {scan_result.file_count}\n"
            f"**Findings:** {reporter.format_counts(analysis)}",
            ephemeral=True,
        )

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable:
        """Find the report channel, with clear errors for common misconfig."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound as exc:
                raise ReporterError(
                    f"`REPORT_CHANNEL_ID` ({channel_id}) does not exist, or this bot "
                    "cannot see it. Invite the bot to that server first."
                ) from exc
            except discord.Forbidden as exc:
                raise ReporterError(
                    "The bot is not allowed to look up that channel. "
                    "Check the bot's View Channel permission."
                ) from exc
            except discord.HTTPException as exc:
                raise ReporterError(f"Could not fetch the report channel: {exc}") from exc

        if not isinstance(channel, discord.abc.Messageable):
            raise ReporterError(
                f"`REPORT_CHANNEL_ID` ({channel_id}) is not a text channel the bot "
                "can post in."
            )
        return channel

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Turn permission/cooldown failures into friendly ephemeral messages."""
        if isinstance(error, app_commands.MissingPermissions):
            message = "🔒 You need the **Manage Server** permission to run `/scan`."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = (
                f"⏳ `/scan` is on cooldown. Try again in {error.retry_after:.0f}s."
            )
        elif isinstance(error, app_commands.CheckFailure):
            message = "🔒 You are not allowed to run `/scan`."
        else:
            log.error("Unhandled error in /scan", exc_info=error)
            message = f"❌ Unexpected error: `{type(error).__name__}: {error}`"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver the /scan error message")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScanCog(bot))
    log.info("Loaded /scan command")
