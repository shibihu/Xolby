"""Utility commands cog for Xolby Discord Bot.

Provides commands:
/ping, /uptime, /botinfo, /help, /invite, /timestamp, /poll, /remind
"""

from __future__ import annotations

import datetime
import logging
import platform
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from commands.moderation import parse_duration
from services.db import db

log = logging.getLogger(__name__)


class PollView(discord.ui.View):
    """Simple button poll view."""

    def __init__(self, question: str, options: list[str]) -> None:
        super().__init__(timeout=None)
        self.question = question
        self.options = options
        self.votes: dict[int, int] = {}  # user_id -> option_idx

        for idx, option_text in enumerate(options):
            button = discord.ui.Button(
                label=f"{option_text} (0)",
                style=discord.ButtonStyle.primary,
                custom_id=f"poll_opt_{idx}",
            )
            button.callback = self._create_callback(idx)
            self.add_item(button)

    def _create_callback(self, idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            user_id = interaction.user.id
            self.votes[user_id] = idx

            # Recalculate counts
            counts = [0] * len(self.options)
            for v in self.votes.values():
                counts[v] += 1

            for child_idx, child in enumerate(self.children):
                if isinstance(child, discord.ui.Button):
                    child.label = f"{self.options[child_idx]} ({counts[child_idx]})"

            await interaction.response.edit_message(view=self)

        return callback


class UtilityCog(commands.Cog):
    """Utility command suite."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminder_task.start()

    async def cog_unload(self) -> None:
        self.reminder_task.cancel()

    @tasks.loop(seconds=15.0)
    async def reminder_task(self) -> None:
        """Background task to deliver due reminders."""
        try:
            due_reminders = db.get_due_reminders()
            for r in due_reminders:
                channel = self.bot.get_channel(r.channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(r.channel_id)
                    except Exception:
                        channel = None

                if channel and isinstance(channel, discord.abc.Messageable):
                    try:
                        await channel.send(
                            f"⏰ <@{r.user_id}> **Reminder:** {r.message}"
                        )
                        db.mark_reminder_completed(r.id)
                    except Exception as exc:
                        log.warning("Could not deliver reminder #%d: %s", r.id, exc)
                else:
                    log.warning("Could not find valid messageable channel %d for reminder #%d", r.channel_id, r.id)
        except Exception as exc:
            log.exception("Error in reminder background task: %s", exc)

    @reminder_task.before_loop
    async def before_reminder_task(self) -> None:
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------------------
    # /ping
    # ---------------------------------------------------------------------------
    @app_commands.command(name="ping", description="Check bot latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: **{latency}ms**")

    # ---------------------------------------------------------------------------
    # /uptime
    # ---------------------------------------------------------------------------
    @app_commands.command(name="uptime", description="Check bot uptime.")
    async def uptime(self, interaction: discord.Interaction) -> None:
        start_time = getattr(self.bot, "start_time", None)
        if not start_time:
            await interaction.response.send_message("⏱️ Uptime untracked.")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - start_time
        days, remainder = divmod(int(delta.total_seconds()), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")

        await interaction.response.send_message(f"⏱️ **Bot Uptime:** {', '.join(parts)}")

    # ---------------------------------------------------------------------------
    # /botinfo
    # ---------------------------------------------------------------------------
    @app_commands.command(name="botinfo", description="Display information about the bot.")
    async def botinfo(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🤖 Bot Information — Xolby",
            color=0x2ECC71,
        )
        embed.set_thumbnail(
            url=self.bot.user.display_avatar.url if self.bot.user else ""
        )

        embed.add_field(name="Bot Name", value=f"`{self.bot.user.name}`", inline=True)
        embed.add_field(name="Bot ID", value=f"`{self.bot.user.id}`", inline=True)
        embed.add_field(name="Python Version", value=f"`{platform.python_version()}`", inline=True)
        embed.add_field(name="Discord.py", value=f"`{discord.__version__}`", inline=True)
        embed.add_field(name="Servers", value=f"`{len(self.bot.guilds)}`", inline=True)

        start_time = getattr(self.bot, "start_time", None)
        if start_time:
            ts = int(start_time.timestamp())
            embed.add_field(name="Started On", value=f"<t:{ts}:R>", inline=True)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /help
    # ---------------------------------------------------------------------------
    @app_commands.command(name="help", description="Show all available slash commands.")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📖 Command Directory",
            description="Here are all registered slash commands in Xolby:",
            color=0x3498DB,
        )

        known_categories = {
            "🎮 Roblox": {"populargames"},
            "🤖 AI Scanner": {"scan"},
            "🎵 TikTok Analytics": {
                "tiktokconnect", "tiktokstats", "tiktoklive", "tiktokhistory", "tiktokdisconnect"
            },
            "🛡️ Moderation": {
                "clear", "purge", "slowmode", "lock", "unlock",
                "kick", "ban", "unban", "timeout", "warn", "warnings"
            },
            "📊 Server Info": {
                "serverinfo", "userinfo", "roleinfo", "channelinfo",
                "avatar", "roles", "channels", "membercount"
            },
            "🔧 Utility": {
                "ping", "uptime", "botinfo", "help",
                "invite", "timestamp", "poll", "remind"
            }
        }

        registered_commands = {cmd.name for cmd in self.bot.tree.get_commands()}
        categorized: dict[str, list[str]] = {}
        placed = set()

        for cat, name_set in known_categories.items():
            present = sorted([f"/{name}" for name in name_set if name in registered_commands])
            if present:
                categorized[cat] = present
                placed.update(name_set)

        other = sorted([f"/{name}" for name in registered_commands if name not in placed])
        if other:
            categorized["✨ Other"] = other

        for cat_title, cmds in categorized.items():
            embed.add_field(name=cat_title, value=" • ".join(cmds), inline=False)

        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /invite
    # ---------------------------------------------------------------------------
    @app_commands.command(name="invite", description="Get the bot invite link.")
    async def invite(self, interaction: discord.Interaction) -> None:
        if not self.bot.user:
            await interaction.response.send_message("❌ Bot user not initialized.", ephemeral=True)
            return

        permissions = discord.Permissions(
            manage_messages=True,
            manage_channels=True,
            kick_members=True,
            ban_members=True,
            moderate_members=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            view_channel=True,
        )
        invite_url = discord.utils.oauth_url(
            self.bot.user.id, permissions=permissions, scopes=("bot", "applications.commands")
        )

        embed = discord.Embed(
            title="🔗 Invite Xolby to Your Server",
            description="Click the button below to invite Xolby with required permissions.",
            color=0x3498DB,
        )
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Invite Bot", url=invite_url, style=discord.ButtonStyle.link
            )
        )

        await interaction.response.send_message(embed=embed, view=view)

    # ---------------------------------------------------------------------------
    # /timestamp
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="timestamp", description="Generate Discord dynamic timestamp markup."
    )
    @app_commands.describe(
        unix_time="Unix timestamp integer or leave empty for current time."
    )
    async def timestamp(
        self, interaction: discord.Interaction, unix_time: Optional[int] = None
    ) -> None:
        ts = unix_time if unix_time is not None else int(time.time())

        embed = discord.Embed(
            title="⏰ Discord Timestamp Helper",
            description=f"Generated timestamps for Unix time: `{ts}`",
            color=0x3498DB,
        )
        embed.add_field(name="Relative (<t:ts:R>)", value=f"`<t:{ts}:R>` → <t:{ts}:R>", inline=False)
        embed.add_field(name="Full Date/Time (<t:ts:F>)", value=f"`<t:{ts}:F>` → <t:{ts}:F>", inline=False)
        embed.add_field(name="Short Date/Time (<t:ts:f>)", value=f"`<t:{ts}:f>` → <t:{ts}:f>", inline=False)
        embed.add_field(name="Short Date (<t:ts:d>)", value=f"`<t:{ts}:d>` → <t:{ts}:d>", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # /poll
    # ---------------------------------------------------------------------------
    @app_commands.command(name="poll", description="Create an interactive poll.")
    @app_commands.describe(
        question="The poll question.",
        options="Comma-separated options (e.g. Yes, No, Maybe). Default: Yes, No",
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: str,
        options: Optional[str] = None,
    ) -> None:
        opt_list = (
            [o.strip() for o in options.split(",") if o.strip()]
            if options
            else ["Yes", "No"]
        )

        if len(opt_list) < 2 or len(opt_list) > 5:
            await interaction.response.send_message(
                "❌ Please provide between 2 and 5 poll options.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📊 Poll: {question}",
            description="Click a button below to vote!",
            color=0x3498DB,
        )
        embed.set_footer(text=f"Poll created by {interaction.user}")

        view = PollView(question=question, options=opt_list)
        await interaction.response.send_message(embed=embed, view=view)

    # ---------------------------------------------------------------------------
    # /remind
    # ---------------------------------------------------------------------------
    @app_commands.command(name="remind", description="Set a reminder.")
    @app_commands.describe(
        duration="Duration (e.g. 30s, 10m, 2h, 1d).", message="Reminder message."
    )
    async def remind(
        self, interaction: discord.Interaction, duration: str, message: str
    ) -> None:
        delta = parse_duration(duration)
        if not delta:
            await interaction.response.send_message(
                "❌ Invalid duration format. Use e.g. `30s`, `10m`, `2h`, `1d`.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        guild_id = guild.id if guild else 0
        remind_at = datetime.datetime.now(datetime.timezone.utc) + delta

        record = db.add_reminder(
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            message=message,
            remind_at=remind_at,
        )

        remind_ts = int(remind_at.timestamp())
        await interaction.response.send_message(
            f"⏰ Reminder set! I will remind you <t:{remind_ts}:R> about:\n> {message}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))
    log.info("Loaded Utility commands cog")
