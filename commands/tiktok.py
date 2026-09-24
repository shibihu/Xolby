"""TikTok Analytics command cog for Xolby."""

from __future__ import annotations

import logging
from typing import Any, Optional
import discord
from discord import app_commands
from discord.ext import commands

from services.db import db
from services.tiktok import TikTokAPIClient, decrypt_token
from services.tiktok_analytics import (
    build_tiktok_stats_embed,
    generate_history_chart,
    tiktok_cache,
)
from services.tiktok_oauth import build_authorization_url, create_oauth_state

log = logging.getLogger(__name__)


class TikTokCog(commands.Cog):
    """Commands for TikTok account integration and video analytics."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------------------
    # /tiktokconnect
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="tiktokconnect",
        description="Connect your TikTok account securely using official OAuth.",
    )
    async def tiktokconnect(self, interaction: discord.Interaction) -> None:
        """Initiate official TikTok OAuth account connection flow."""
        existing_acc = db.get_tiktok_account(interaction.user.id)
        if existing_acc and existing_acc.display_name:
            prompt_msg = f" You are currently connected as **@{existing_acc.display_name}**. Authorizing again will update your connection."
        else:
            prompt_msg = ""

        state = create_oauth_state(interaction.user.id)
        auth_url = build_authorization_url(state)

        embed = discord.Embed(
            title="🔗 Connect Your TikTok Account",
            description=(
                f"Click the button below to connect your TikTok account via TikTok's official login page.{prompt_msg}\n\n"
                "🔒 **Security & Privacy Guarantee:**\n"
                "• Official TikTok OAuth flow (no password stored)\n"
                "• Read-only access to user profile and video list\n"
                "• Access tokens encrypted at rest\n"
                "• Authorization link expires in **10 minutes**"
            ),
            color=0xFE2C55,
        )

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Authorize with TikTok",
                url=auth_url,
                style=discord.ButtonStyle.link,
                emoji="🎵",
            )
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ---------------------------------------------------------------------------
    # /tiktokstats
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="tiktokstats",
        description="View analytics and calculated engagement for your latest TikTok video.",
    )
    async def tiktokstats(self, interaction: discord.Interaction) -> None:
        """Retrieve recent TikTok video metrics and render analytics embed."""
        await interaction.response.defer()

        analytics = await tiktok_cache.get_user_analytics(interaction.user.id)
        status = analytics.get("status")
        account = analytics.get("account")
        videos = analytics.get("videos", [])

        if status == "not_connected":
            await interaction.followup.send(
                "❌ You do not have a connected TikTok account.\n"
                "Run `/tiktokconnect` to link your TikTok account safely.",
                ephemeral=True,
            )
            return

        if status == "reconnect_required":
            await interaction.followup.send(
                "⚠️ Your TikTok connection needs to be reconnected.\n"
                "Please run `/tiktokconnect` to update your authorization.",
                ephemeral=True,
            )
            return

        if not videos:
            msg = "❌ No videos found on your connected TikTok account."
            if status == "offline_cached":
                msg = "🟡 Data temporarily unavailable. Please try again shortly."
            await interaction.followup.send(msg, ephemeral=True)
            return

        latest_video = videos[0]
        v_id = str(latest_video.get("id", ""))
        display_name = account.display_name if account else ""
        prev_snap = db.get_previous_tiktok_snapshot(interaction.user.id, v_id)

        status_tag = "🟡 Offline Cache" if status == "offline_cached" else ""
        embed = build_tiktok_stats_embed(
            video=latest_video,
            account_name=display_name,
            status_tag=status_tag,
            prev_snapshot=prev_snap,
        )

        await interaction.followup.send(embed=embed)

    # ---------------------------------------------------------------------------
    # /tiktoklive
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="tiktoklive",
        description="Create a persistent live-updating TikTok analytics tracker message in this channel.",
    )
    async def tiktoklive(self, interaction: discord.Interaction) -> None:
        """Start or update a live tracking message in current channel."""
        await interaction.response.defer()

        analytics = await tiktok_cache.get_user_analytics(interaction.user.id)
        status = analytics.get("status")
        account = analytics.get("account")
        videos = analytics.get("videos", [])

        if status == "not_connected":
            await interaction.followup.send(
                "❌ You do not have a connected TikTok account.\n"
                "Run `/tiktokconnect` to link your account first.",
                ephemeral=True,
            )
            return

        if status == "reconnect_required":
            await interaction.followup.send(
                "⚠️ Your TikTok connection needs to be reconnected.\n"
                "Please run `/tiktokconnect` to update your authorization.",
                ephemeral=True,
            )
            return

        if not videos:
            await interaction.followup.send(
                "❌ Could not retrieve TikTok video data to create live tracker.",
                ephemeral=True,
            )
            return

        latest_video = videos[0]
        display_name = account.display_name if account else ""
        v_id = str(latest_video.get("id", ""))
        prev_snap = db.get_previous_tiktok_snapshot(interaction.user.id, v_id)

        embed = build_tiktok_stats_embed(
            video=latest_video,
            account_name=display_name,
            status_tag="🟢 LIVE",
            prev_snapshot=prev_snap,
        )

        msg = await interaction.followup.send(embed=embed, wait=True)

        if msg and interaction.channel_id:
            guild_id = interaction.guild_id or 0
            db.add_or_update_tiktok_live_tracker(
                discord_user_id=interaction.user.id,
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                message_id=msg.id,
            )

    # ---------------------------------------------------------------------------
    # /tiktokhistory
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="tiktokhistory",
        description="View performance history and growth chart for your TikTok videos.",
    )
    async def tiktokhistory(self, interaction: discord.Interaction) -> None:
        """Display historical snapshots and attach performance chart."""
        await interaction.response.defer()

        account = db.get_tiktok_account(interaction.user.id)
        if not account:
            await interaction.followup.send(
                "❌ You do not have a connected TikTok account.\n"
                "Run `/tiktokconnect` to link your TikTok account.",
                ephemeral=True,
            )
            return

        snapshots = db.get_tiktok_snapshots(interaction.user.id, limit=30)
        if not snapshots:
            await interaction.followup.send(
                "ℹ️ No historical snapshots recorded yet.\n"
                "Run `/tiktokstats` or create a `/tiktoklive` tracker to start recording snapshot history.",
                ephemeral=True,
            )
            return

        display_name = account.display_name or "TikTok User"
        embed = discord.Embed(
            title=f"📊 TikTok History — @{display_name}",
            description=f"Recorded **{len(snapshots)}** performance snapshot(s) across recent videos.",
            color=0xFE2C55,
        )

        # Group latest snapshots by video_id
        video_latest: dict[str, Any] = {}
        for s in snapshots:
            if s.video_id not in video_latest:
                video_latest[s.video_id] = s

        lines = []
        for i, (v_id, snap) in enumerate(list(video_latest.items())[:5], 1):
            title = discord.utils.escape_markdown(snap.video_title or "Untitled Video")
            lines.append(
                f"**{i}. {title}**\n"
                f"👁️ `{snap.view_count:,}` views  •  ❤️ `{snap.like_count:,}`  •  💬 `{snap.comment_count:,}`  •  🔁 `{snap.share_count:,}`"
            )

        if lines:
            embed.add_field(
                name="🎬 Recent Tracked Videos",
                value="\n\n".join(lines),
                inline=False,
            )

        # Generate history chart PNG
        chart_buf = generate_history_chart(snapshots)
        if chart_buf:
            file = discord.File(chart_buf, filename="tiktok_history.png")
            embed.set_image(url="attachment://tiktok_history.png")
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

    # ---------------------------------------------------------------------------
    # /tiktokdisconnect
    # ---------------------------------------------------------------------------
    @app_commands.command(
        name="tiktokdisconnect",
        description="Disconnect your TikTok account and revoke stored OAuth tokens.",
    )
    async def tiktokdisconnect(self, interaction: discord.Interaction) -> None:
        """Disconnect TikTok account and purge user authorization tokens."""
        await interaction.response.defer(ephemeral=True)

        account = db.get_tiktok_account(interaction.user.id)
        if not account:
            await interaction.followup.send(
                "❌ You do not have a connected TikTok account.", ephemeral=True
            )
            return

        # Attempt to revoke token with official API
        decrypted_token = decrypt_token(account.access_token)
        client = TikTokAPIClient()
        try:
            await client.revoke_token(decrypted_token)
        except Exception as exc:
            log.warning("Revoke token API call error during disconnect: %s", type(exc).__name__)
        finally:
            await client.close()

        # Delete account and associated live trackers from database
        db.delete_tiktok_account(interaction.user.id)

        embed = discord.Embed(
            title="🔌 TikTok Account Disconnected",
            description=(
                "✅ Your TikTok account connection has been removed.\n"
                "All stored OAuth tokens have been permanently deleted and revoked."
            ),
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TikTokCog(bot))
    log.info("Loaded TikTok commands cog")
