"""``/clear`` — clear/delete messages in the current Discord channel.

The command operates strictly on the channel where it is invoked, requiring
both user and bot permissions, and user confirmation via interactive UI buttons.
"""

from __future__ import annotations

import datetime
import logging
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class ConfirmationView(discord.ui.View):
    """Interactive confirmation buttons for /clear."""

    def __init__(self, author_id: int, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the user who started this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content="🧹 Clearing channel messages...", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content="❌ Clear cancelled.", view=self
        )


class ClearCog(commands.Cog):
    """Channel message clearing cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="clear",
        description="Clear/delete all messages in the current channel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel

        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread, discord.StageChannel)):
            await interaction.response.send_message(
                "❌ This command can only be used in text channels, threads, or voice text channels.",
                ephemeral=True,
            )
            return

        # Check bot permissions in current channel
        guild = interaction.guild
        if guild is not None and hasattr(channel, "permissions_for"):
            me = guild.me or guild.get_member(self.bot.user.id)
            if me is not None:
                perms = channel.permissions_for(me)
                if not (perms.manage_messages and perms.read_message_history and perms.view_channel):
                    await interaction.response.send_message(
                        "❌ I don't have permission to delete messages in this channel.",
                        ephemeral=True,
                    )
                    return

        embed = discord.Embed(
            title="⚠️ Clear Channel",
            description=(
                "This will delete messages from this channel.\n"
                "This action cannot be undone."
            ),
            color=0xED4245,
        )

        view = ConfirmationView(author_id=interaction.user.id, timeout=60.0)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

        timed_out = await view.wait()

        if timed_out and view.confirmed is None:
            for child in view.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            try:
                await interaction.edit_original_response(
                    content="⏱️ Clear confirmation expired.", embed=None, view=view
                )
            except discord.HTTPException:
                pass
            return

        if not view.confirmed:
            return

        # Perform deletion
        deleted_count = 0
        old_count = 0
        failed_count = 0

        now = datetime.datetime.now(datetime.timezone.utc)
        two_weeks_ago = now - datetime.timedelta(days=14)

        try:
            # 1. Bulk delete messages <= 14 days old
            deleted_bulk = await channel.purge(
                limit=None,
                after=two_weeks_ago,
                oldest_first=False,
                bulk=True,
            )
            deleted_count += len(deleted_bulk)

            # 2. Check for remaining older messages (> 14 days old)
            async for msg in channel.history(limit=None, before=two_weeks_ago):
                try:
                    await msg.delete()
                    deleted_count += 1
                except (discord.Forbidden, discord.HTTPException):
                    old_count += 1
                    failed_count += 1

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to delete messages in this channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("HTTP Exception during /clear purge: %s", exc)
            await interaction.followup.send(
                f"❌ Discord error during message deletion: `{exc}`",
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.exception("Unexpected error during /clear execution")
            await interaction.followup.send(
                "❌ An unexpected error occurred while clearing messages.",
                ephemeral=True,
            )
            return

        if failed_count > 0:
            msg = (
                f"⚠️ Channel partially cleared.\n\n"
                f"**Deleted:** {deleted_count}\n"
                f"**Could not delete:** {failed_count}\n\n"
                f"*Note: Messages older than 14 days could not be deleted due to Discord limits or missing permissions.*"
            )
        else:
            msg = f"🧹 Channel cleared successfully.\n\n**Deleted:** {deleted_count} messages"

        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle missing permissions and command errors."""
        if isinstance(error, app_commands.MissingPermissions):
            message = '❌ You need the "Manage Messages" permission to use this command.'
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "❌ I don't have permission to delete messages in this channel."
        elif isinstance(error, app_commands.CheckFailure):
            message = '❌ You need the "Manage Messages" permission to use this command.'
        else:
            log.error("Unhandled error in /clear", exc_info=error)
            message = f"❌ Unexpected error: `{type(error).__name__}: {error}`"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver the /clear error message")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClearCog(bot))
    log.info("Loaded /clear command")
