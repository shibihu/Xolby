"""TikTok Analytics caching, historical graph rendering, and background live tracker manager."""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import os
import time
from typing import Any, Optional
import discord
from discord.ext import commands
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from services.db import TikTokSnapshotRecord, db
from services.tiktok import (
    UNAVAILABLE_STUDIO_METRICS,
    TikTokAPIClient,
    TikTokTokenExpiredError,
    calculate_engagement_metrics,
    get_valid_access_token,
)

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 180.0  # 3 minutes


# ---------------------------------------------------------------------------
# Centralized Analytics Cache
# ---------------------------------------------------------------------------
class TikTokAnalyticsCache:
    """Centralized async cache to prevent redundant TikTok API calls."""

    def __init__(self) -> None:
        self._cache: dict[int, dict[str, Any]] = {}  # user_id -> {timestamp, videos, account, status}
        self._lock = asyncio.Lock()

    async def get_user_analytics(
        self,
        discord_user_id: int,
        api_client: Optional[TikTokAPIClient] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            now = time.time()
            cached = self._cache.get(discord_user_id)
            if not force_refresh and cached and (now - cached["timestamp"] < CACHE_TTL_SECONDS):
                return cached

            client = api_client or TikTokAPIClient()
            access_token, account = await get_valid_access_token(discord_user_id, client)

            if not account:
                res = {"status": "not_connected", "videos": [], "account": None, "timestamp": now}
                self._cache[discord_user_id] = res
                return res

            if not access_token:
                res = {"status": "reconnect_required", "videos": [], "account": account, "timestamp": now}
                self._cache[discord_user_id] = res
                return res

            try:
                videos = await client.get_user_videos(access_token, max_count=10)
                if videos:
                    # Save snapshot for latest video
                    latest = videos[0]
                    v_id = str(latest.get("id", ""))
                    v_title = latest.get("title") or latest.get("video_description") or "Untitled Video"
                    db.add_tiktok_snapshot(
                        discord_user_id=discord_user_id,
                        tiktok_open_id=account.tiktok_open_id,
                        video_id=v_id,
                        video_title=v_title,
                        view_count=int(latest.get("view_count", 0)),
                        like_count=int(latest.get("like_count", 0)),
                        comment_count=int(latest.get("comment_count", 0)),
                        share_count=int(latest.get("share_count", 0)),
                        favorite_count=int(latest.get("favorite_count", 0)),
                    )

                res = {"status": "ok", "videos": videos, "account": account, "timestamp": now}
                self._cache[discord_user_id] = res
                return res

            except TikTokTokenExpiredError:
                res = {"status": "reconnect_required", "videos": [], "account": account, "timestamp": now}
                self._cache[discord_user_id] = res
                return res
            except Exception as exc:
                log.warning("Failed to refresh TikTok analytics for user %s: %s", discord_user_id, type(exc).__name__)
                if cached:
                    cached["status"] = "offline_cached"
                    return cached
                res = {"status": "error", "error": str(exc), "videos": [], "account": account, "timestamp": now}
                self._cache[discord_user_id] = res
                return res


tiktok_cache = TikTokAnalyticsCache()


# ---------------------------------------------------------------------------
# Chart Generation
# ---------------------------------------------------------------------------
def generate_history_chart(snapshots: list[TikTokSnapshotRecord]) -> Optional[io.BytesIO]:
    """Generate a mobile-friendly matplotlib chart PNG from snapshot records."""
    if not snapshots or len(snapshots) < 2:
        return None

    # Sort chronologically (oldest first)
    sorted_snaps = sorted(snapshots, key=lambda s: s.timestamp)

    timestamps = []
    views = []
    likes = []
    comments = []
    shares = []

    for s in sorted_snaps:
        try:
            dt = datetime.datetime.fromisoformat(s.timestamp)
        except Exception:
            continue
        timestamps.append(dt)
        views.append(s.view_count)
        likes.append(s.like_count)
        comments.append(s.comment_count)
        shares.append(s.share_count)

    if len(timestamps) < 2:
        return None

    plt.style.use("dark_background")
    fig, ax1 = plt.subplots(figsize=(8, 4.5), dpi=120)
    fig.patch.set_facecolor("#1e1e2e")
    ax1.set_facecolor("#181825")

    # Plot Views on primary Y-axis
    line1 = ax1.plot(timestamps, views, color="#38bdf8", linewidth=2.5, marker="o", markersize=4, label="Views")
    ax1.set_ylabel("Views", color="#38bdf8", fontsize=11, fontweight="bold")
    ax1.tick_params(axis="y", labelcolor="#38bdf8")
    ax1.grid(True, linestyle="--", alpha=0.2, color="#6c7086")

    # Plot Likes, Comments, Shares on secondary Y-axis for better visibility scale
    ax2 = ax1.twinx()
    line2 = ax2.plot(timestamps, likes, color="#f43f5e", linewidth=2, linestyle="-", marker="s", markersize=3, label="Likes")
    line3 = ax2.plot(timestamps, comments, color="#34d399", linewidth=2, linestyle="--", marker="^", markersize=3, label="Comments")
    line4 = ax2.plot(timestamps, shares, color="#fbbf24", linewidth=2, linestyle=":", marker="d", markersize=3, label="Shares")
    ax2.set_ylabel("Interactions", color="#cdd6f4", fontsize=11)
    ax2.tick_params(axis="y", labelcolor="#cdd6f4")

    # Format X axis dates
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    fig.autofmt_xdate(rotation=25)

    # Combine legends
    lines = line1 + line2 + line3 + line4
    labels = [l.get_label() for l in lines]
    ax1.legend(lines, labels, loc="upper left", facecolor="#11111b", edgecolor="#45475a", fontsize=9)

    plt.title("TikTok Analytics History", fontsize=13, fontweight="bold", color="#cdd6f4", pad=12)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), edgecolor="none", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Embed Building Helpers
# ---------------------------------------------------------------------------
def build_tiktok_stats_embed(
    video: dict,
    account_name: str = "",
    status_tag: str = "",
    prev_snapshot: Optional[TikTokSnapshotRecord] = None,
) -> discord.Embed:
    """Build standardized Discord embed for TikTok stats."""
    v_title = video.get("title") or video.get("video_description") or "Untitled Video"
    views = int(video.get("view_count", 0))
    likes = int(video.get("like_count", 0))
    comments = int(video.get("comment_count", 0))
    shares = int(video.get("share_count", 0))
    favorites = int(video.get("favorite_count", 0))
    share_url = video.get("share_url") or ""
    cover_url = video.get("cover_image_url") or ""
    create_time = video.get("create_time", 0)

    eng = calculate_engagement_metrics(views, likes, comments, shares, favorites)

    embed = discord.Embed(
        title="📊 TikTok Analytics",
        description=f"**Latest Video**\n[{discord.utils.escape_markdown(v_title)}]({share_url})" if share_url else f"**Latest Video**\n{discord.utils.escape_markdown(v_title)}",
        color=0xFE2C55,  # TikTok brand color hex
    )

    if cover_url:
        embed.set_thumbnail(url=cover_url)

    embed.add_field(name="👁️ Views", value=f"`{views:,}`", inline=True)
    embed.add_field(name="❤️ Likes", value=f"`{likes:,}`", inline=True)
    embed.add_field(name="💬 Comments", value=f"`{comments:,}`", inline=True)
    embed.add_field(name="🔁 Shares", value=f"`{shares:,}`", inline=True)
    embed.add_field(name="⭐ Favorites", value=f"`{favorites:,}`", inline=True)
    embed.add_field(name="📈 Total Engagement", value=f"`{eng['total_engagement_rate']}%` *(Calculated)*", inline=True)

    # Detailed engagement rates field
    eng_details = (
        f"• Like Rate: `{eng['like_rate']}%` *(Calculated)*\n"
        f"• Comment Rate: `{eng['comment_rate']}%` *(Calculated)*\n"
        f"• Share Rate: `{eng['share_rate']}%` *(Calculated)*"
    )
    embed.add_field(name="📊 Engagement Breakdown", value=eng_details, inline=False)

    # Growth since previous snapshot if available
    if prev_snapshot and prev_snapshot.video_id == str(video.get("id", "")):
        d_views = views - prev_snapshot.view_count
        d_likes = likes - prev_snapshot.like_count
        d_comments = comments - prev_snapshot.comment_count
        d_shares = shares - prev_snapshot.share_count

        growth_str = (
            f"👁️ `{d_views:+}` views | ❤️ `{d_likes:+}` likes | "
            f"💬 `{d_comments:+}` comments | 🔁 `{d_shares:+}` shares"
        )
        embed.add_field(name="🚀 Growth Since Previous Snapshot", value=growth_str, inline=False)

    if create_time:
        posted_ts = int(create_time)
        embed.add_field(name="🕐 Posted", value=f"<t:{posted_ts}:R>", inline=True)

    if share_url:
        embed.add_field(name="🔗 Watch", value=f"[Watch on TikTok]({share_url})", inline=True)

    studio_note = "\n\n*Note: Advanced metrics (Watch time, Retention) are TikTok Studio-only and not available via official API.*"
    status_suffix = f" • {status_tag}" if status_tag else ""
    account_suffix = f" • @{account_name}" if account_name else ""

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC")
    embed.set_footer(
        text=f"Last updated: {now_utc}{account_suffix}{status_suffix}{studio_note}"
    )

    return embed


# ---------------------------------------------------------------------------
# Background Centralized Live Tracker Manager
# ---------------------------------------------------------------------------
class TikTokLiveManager:
    """Centralized async background task manager for live TikTok Discord trackers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.update_interval = float(os.getenv("TIKTOK_LIVE_INTERVAL_SECONDS", "300"))
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._update_loop())
            log.info("[TIKTOK LIVE] Manager started with interval %.1fs", self.update_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("[TIKTOK LIVE] Manager stopped.")

    async def _update_loop(self) -> None:
        await self.bot.wait_until_ready()
        while self._running:
            try:
                await self._update_all_trackers()
            except Exception as exc:
                log.exception("[TIKTOK LIVE] Error in live tracker update cycle")
            await asyncio.sleep(self.update_interval)

    async def _update_all_trackers(self) -> None:
        trackers = db.get_active_tiktok_live_trackers()
        if not trackers:
            return

        # Group trackers by user ID to deduplicate API calls
        user_trackers: dict[int, list] = {}
        for t in trackers:
            user_trackers.setdefault(t.discord_user_id, []).append(t)

        for user_id, user_t_list in user_trackers.items():
            analytics = await tiktok_cache.get_user_analytics(user_id)
            status = analytics.get("status")
            videos = analytics.get("videos", [])
            account = analytics.get("account")
            display_name = account.display_name if account else ""

            for tracker in user_t_list:
                channel = self.bot.get_channel(tracker.channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(tracker.channel_id)
                    except Exception:
                        log.warning("[TIKTOK LIVE] Could not fetch channel %s, deactivating", tracker.channel_id)
                        db.deactivate_tiktok_live_tracker(tracker.channel_id)
                        continue

                try:
                    message = await channel.fetch_message(tracker.message_id)
                except Exception:
                    log.warning("[TIKTOK LIVE] Message %s missing in channel %s, removing", tracker.message_id, tracker.channel_id)
                    db.remove_tiktok_live_tracker(tracker.channel_id)
                    continue

                if status == "reconnect_required":
                    embed = discord.Embed(
                        title="📊 TikTok Analytics — 🟢 LIVE",
                        description="⚠️ **Connection Required**\nYour TikTok connection needs to be reconnected. Run `/tiktokconnect` in Discord.",
                        color=0xE74C3C,
                    )
                    await message.edit(embed=embed)
                    continue

                if not videos:
                    if status == "offline_cached":
                        status_tag = "🟡 Data temporarily unavailable"
                    else:
                        status_tag = "⚠️ No videos found"
                    embed = discord.Embed(
                        title="📊 TikTok Analytics — 🟢 LIVE",
                        description=f"{status_tag}\nCould not retrieve latest video metrics.",
                        color=0xF1C40F,
                    )
                    await message.edit(embed=embed)
                    continue

                latest_video = videos[0]
                v_id = str(latest_video.get("id", ""))
                prev_snap = db.get_previous_tiktok_snapshot(user_id, v_id)

                status_tag = "🟢 LIVE"
                if status == "offline_cached":
                    status_tag = "🟡 Data temporarily unavailable"

                embed = build_tiktok_stats_embed(
                    video=latest_video,
                    account_name=display_name,
                    status_tag=status_tag,
                    prev_snapshot=prev_snap,
                )

                try:
                    await message.edit(embed=embed)
                except discord.HTTPException as e:
                    log.error("[TIKTOK LIVE] Failed to edit message %s: %s", tracker.message_id, e)
