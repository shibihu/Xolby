"""Moderation commands cog for Xolby Discord Bot.

Provides commands:
/purge, /slowmode, /lock, /unlock, /kick, /ban, /unban, /timeout, /warn, /warnings
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.db import db

log = logging.getLogger(__name__)


def parse_duration(duration_str: str) -> Optional[datetime.timedelta]:
    """Parse strings like 30s, 10m, 1h, 1d into timedelta."""
    match = re.match(r"^(\d+)\s*([sSmMhHdD])$", duration_str.strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        return None
    if unit == "s":
        return datetime.timedelta(seconds=amount)
    if unit == "m":
        return datetime.timedelta(minutes=amount)
    if unit == "h":
        return datetime.timedelta(hours=amount)
    if unit == "d":
        return datetime.timedelta(days=amount)
    return None


class ModerationActionView(discord.ui.View):
    """Interactive confirmation buttons for Kick / Ban."""

    def __init__(
        self,
        author_id: int,
        action_name: str,
        target_str: str,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.action_name = action_name
        self.target_str = target_str
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the user who started this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content=f"⏳ Processing {self.action_name} for {self.target_str}...",
            view=self,
            embed=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content=f"❌ {self.action_name} cancelled.", view=self, embed=None
        )


class ModerationCog(commands.Cog):
    """Moderation command suite."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _check_hierarchy(
        self,
        guild: discord.Guild,
        invoker: discord.Member,
        target: discord.Member,
    ) -> tuple[bool, str]:
        if target.id == guild.owner_id:
            return False, "❌ Cannot moderate the server owner."
        if invoker.id != guild.owner_id and target.top_role >= invoker.top_role:
            return False, "❌ You cannot moderate a member with a higher or equal top role."
        me = guild.me or guild.get_member(self.bot.user.id)
        if me and target.top_role >= me.top_role:
            return False, "❌ I cannot moderate a member with a higher or equal top role than my highest role."
        return True, ""

    # ---------------------------------------------------------------------------
    # /purge
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="purge", description="Delete recent messages in the current channel."
    )
    @app_commands.describe(amount="Number of recent messages to delete (1-100).")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int) -> None:
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "❌ Amount must be between 1 and 100.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(
            channel,
            (discord.TextChannel, discord.VoiceChannel, discord.Thread, discord.StageChannel),
        ):
            await interaction.response.send_message(
                "❌ Cannot purge messages in this channel type.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild and hasattr(channel, "permissions_for"):
            me = guild.me or guild.get_member(self.bot.user.id)
            if me:
                perms = channel.permissions_for(me)
                if not (perms.manage_messages and perms.read_message_history):
                    await interaction.response.send_message(
                        "❌ I don't have permission to manage messages in this channel.",
                        ephemeral=True,
                    )
                    return

        await interaction.response.defer(ephemeral=True)

        now = datetime.datetime.now(datetime.timezone.utc)
        two_weeks_ago = now - datetime.timedelta(days=14)

        try:
            deleted_bulk = await channel.purge(
                limit=amount, after=two_weeks_ago, bulk=True
            )
            deleted_count = len(deleted_bulk)
            failed_count = amount - deleted_count if deleted_count < amount else 0

            if failed_count > 0:
                await interaction.followup.send(
                    f"⚠️ Deleted {deleted_count} message(s). {failed_count} message(s) could not be deleted (e.g. older than 14 days).",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"🧹 Successfully purged {deleted_count} message(s).",
                    ephemeral=True,
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to delete messages in this channel.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.error("Error during /purge: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to purge messages: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /slowmode
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="slowmode", description="Set the slowmode delay for the current channel."
    )
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable, max 21600).")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: int) -> None:
        if seconds < 0 or seconds > 21600:
            await interaction.response.send_message(
                "❌ Slowmode seconds must be between 0 and 21600 (6 hours).",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Slowmode cannot be set on this channel type.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild and hasattr(channel, "permissions_for"):
            me = guild.me or guild.get_member(self.bot.user.id)
            if me and not channel.permissions_for(me).manage_channels:
                await interaction.response.send_message(
                    "❌ I don't have permission to manage this channel.",
                    ephemeral=True,
                )
                return

        try:
            await channel.edit(slowmode_delay=seconds)
            if seconds == 0:
                await interaction.response.send_message("🐢 Slowmode disabled.")
            else:
                await interaction.response.send_message(
                    f"🐢 Slowmode enabled: {seconds} seconds."
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage this channel.", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to set slowmode: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /lock & /unlock
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="lock", description="Lock the current channel so normal members cannot send messages."
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            await interaction.response.send_message(
                "❌ Locking is not supported on this channel type.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild:
            me = guild.me or guild.get_member(self.bot.user.id)
            if me and not channel.permissions_for(me).manage_roles:
                await interaction.response.send_message(
                    "❌ I don't have permission to manage permissions in this channel.",
                    ephemeral=True,
                )
                return

        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            await interaction.response.send_message("🔒 Channel locked.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage permissions in this channel.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to lock channel: `{exc}`", ephemeral=True
            )

    @app_commands.command(
        name="unlock", description="Unlock the current channel so normal members can send messages."
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            await interaction.response.send_message(
                "❌ Unlocking is not supported on this channel type.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild:
            me = guild.me or guild.get_member(self.bot.user.id)
            if me and not channel.permissions_for(me).manage_roles:
                await interaction.response.send_message(
                    "❌ I don't have permission to manage permissions in this channel.",
                    ephemeral=True,
                )
                return

        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            await interaction.response.send_message("🔓 Channel unlocked.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage permissions in this channel.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to unlock channel: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /kick
    # ---------------------------------------------------------------------------
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="The member to kick.", reason="Reason for kicking.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        ok, err_msg = self._check_hierarchy(guild, interaction.user, user)
        if not ok:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ Confirm Kick",
            description=f"Are you sure you want to kick **{user.mention}** (`{user.id}`)?\n**Reason:** {reason or 'No reason provided'}",
            color=0xED4245,
        )

        view = ModerationActionView(
            author_id=interaction.user.id,
            action_name="Kick",
            target_str=f"{user}",
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

        timed_out = await view.wait()
        if timed_out and view.confirmed is None:
            await interaction.edit_original_response(
                content="⏱️ Kick confirmation expired.", embed=None, view=None
            )
            return

        if not view.confirmed:
            return

        try:
            await user.kick(reason=f"{reason or 'No reason'} (by {interaction.user})")
            await interaction.followup.send(
                f"👢 **{user}** was kicked. | Reason: {reason or 'No reason provided'}",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to kick this member.", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to kick member: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /ban
    # ---------------------------------------------------------------------------
    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="The member to ban.", reason="Reason for banning.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        ok, err_msg = self._check_hierarchy(guild, interaction.user, user)
        if not ok:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ Confirm Ban",
            description=f"Are you sure you want to ban **{user.mention}** (`{user.id}`)?\n**Reason:** {reason or 'No reason provided'}",
            color=0xED4245,
        )

        view = ModerationActionView(
            author_id=interaction.user.id,
            action_name="Ban",
            target_str=f"{user}",
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

        timed_out = await view.wait()
        if timed_out and view.confirmed is None:
            await interaction.edit_original_response(
                content="⏱️ Ban confirmation expired.", embed=None, view=None
            )
            return

        if not view.confirmed:
            return

        try:
            await user.ban(
                reason=f"{reason or 'No reason'} (by {interaction.user})",
                delete_message_days=0,
            )
            await interaction.followup.send(
                f"🔨 **{user}** was banned. | Reason: {reason or 'No reason provided'}",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to ban this member.", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to ban member: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /unban
    # ---------------------------------------------------------------------------
    @app_commands.command(name="unban", description="Unban a user by their User ID.")
    @app_commands.describe(user_id="The numeric ID of the user to unban.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str) -> None:
        if not user_id.isdigit():
            await interaction.response.send_message(
                "❌ Please enter a valid numeric User ID.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        target_id = int(user_id)
        try:
            ban_entry = await guild.fetch_ban(discord.Object(id=target_id))
            await guild.unban(
                ban_entry.user, reason=f"Unbanned by {interaction.user}"
            )
            await interaction.response.send_message(
                f"🔓 **{ban_entry.user}** (`{target_id}`) was unbanned."
            )
        except discord.NotFound:
            await interaction.response.send_message(
                "❌ User is not banned or invalid ID.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to unban members.", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to unban user: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /timeout
    # ---------------------------------------------------------------------------
    @app_commands.command(name="timeout", description="Timeout/mute a member.")
    @app_commands.describe(
        user="The member to timeout.",
        duration="Duration (e.g. 30s, 10m, 1h, 1d).",
        reason="Reason for timeout.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        delta = parse_duration(duration)
        if not delta:
            await interaction.response.send_message(
                "❌ Invalid duration format. Use e.g. `30s`, `10m`, `1h`, `1d`.",
                ephemeral=True,
            )
            return

        if delta > datetime.timedelta(days=28):
            await interaction.response.send_message(
                "❌ Timeout duration cannot exceed 28 days.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        ok, err_msg = self._check_hierarchy(guild, interaction.user, user)
        if not ok:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        try:
            await user.timeout(delta, reason=f"{reason or 'No reason'} (by {interaction.user})")
            await interaction.response.send_message(
                f"🔇 **{user.mention}** has been timed out for **{duration}**.\nReason: {reason or 'No reason provided'}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to timeout this member.", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"❌ Failed to timeout member: `{exc}`", ephemeral=True
            )

    # ---------------------------------------------------------------------------
    # /warn & /warnings
    # ---------------------------------------------------------------------------
    @app_commands.command(name="warn", description="Issue a warning to a member.")
    @app_commands.describe(user="The member to warn.", reason="Reason for warning.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str,
    ) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        record = db.add_warning(
            guild_id=guild.id,
            user_id=user.id,
            moderator_id=interaction.user.id,
            reason=reason,
        )

        embed = discord.Embed(
            title="⚠️ Member Warned",
            description=f"**User:** {user.mention} (`{user.id}`)\n**Reason:** {reason}\n**Warning ID:** #{record.id}",
            color=0xFEE75C,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="warnings", description="View warning history for a member."
    )
    @app_commands.describe(user="The member whose warnings to inspect.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return

        records = db.get_warnings(guild_id=guild.id, user_id=user.id)
        if not records:
            await interaction.response.send_message(
                f"🟢 **{user}** has no recorded warnings in this server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📋 Warnings for {user} ({len(records)} total)",
            color=0xFEE75C,
        )

        lines = []
        for r in records[:15]:
            lines.append(
                f"**#{r.id}** • <t:{int(datetime.datetime.fromisoformat(r.timestamp).timestamp())}:R>\n"
                f"Reason: {r.reason}\nBy: <@{r.moderator_id}>"
            )

        embed.description = "\n\n".join(lines)
        if len(records) > 15:
            embed.set_footer(text=f"Showing 15 of {len(records)} warnings")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You don't have the required permissions to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "❌ I don't have the required permissions to execute this command."
        else:
            log.error("Unhandled error in ModerationCog", exc_info=error)
            msg = f"❌ Unexpected error: `{type(error).__name__}: {error}`"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
    log.info("Loaded Moderation commands cog")
