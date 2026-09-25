"""TikTok Analytics caching, historical graph rendering using pure Python standard library,
and background live tracker manager.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import math
import os
import struct
import time
import zlib
from typing import Any, Optional
import discord
from discord.ext import commands

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
# Pure Python 5x7 ASCII Bitmap Font for Label Rendering
# ---------------------------------------------------------------------------
FONT_5X7: dict[str, list[int]] = {
    '0': [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
    '1': [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
    '2': [0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F],
    '3': [0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E],
    '4': [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
    '5': [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
    '6': [0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E],
    '7': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    '8': [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
    '9': [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C],
    ':': [0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00],
    '/': [0x01, 0x02, 0x04, 0x08, 0x10, 0x00, 0x00],
    '.': [0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C, 0x00],
    '-': [0x00, 0x00, 0x1F, 0x00, 0x00, 0x00, 0x00],
    '+': [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
    ' ': [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    'A': [0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'B': [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
    'C': [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    'D': [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
    'E': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    'F': [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
    'G': [0x0E, 0x11, 0x10, 0x13, 0x11, 0x11, 0x0F],
    'H': [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    'I': [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    'J': [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
    'K': [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
    'L': [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    'M': [0x11, 0x1B, 0x15, 0x11, 0x11, 0x11, 0x11],
    'N': [0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11],
    'O': [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'P': [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    'Q': [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
    'R': [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    'S': [0x0E, 0x11, 0x10, 0x0E, 0x01, 0x11, 0x0E],
    'T': [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    'U': [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    'V': [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
    'W': [0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11],
    'X': [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
    'Y': [0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04],
    'Z': [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
    'a': [0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F],
    'b': [0x10, 0x10, 0x16, 0x19, 0x11, 0x11, 0x1E],
    'c': [0x00, 0x00, 0x0E, 0x10, 0x10, 0x11, 0x0E],
    'd': [0x01, 0x01, 0x0D, 0x13, 0x11, 0x11, 0x0F],
    'e': [0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E],
    'i': [0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E],
    'k': [0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12],
    'l': [0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    'm': [0x00, 0x00, 0x1A, 0x15, 0x15, 0x11, 0x11],
    'n': [0x00, 0x00, 0x16, 0x19, 0x11, 0x11, 0x11],
    'o': [0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E],
    'p': [0x00, 0x00, 0x1E, 0x11, 0x1E, 0x10, 0x10],
    'r': [0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10],
    's': [0x00, 0x00, 0x0E, 0x10, 0x0E, 0x01, 0x1E],
    't': [0x08, 0x08, 0x1C, 0x08, 0x08, 0x09, 0x06],
    'u': [0x00, 0x00, 0x11, 0x11, 0x11, 0x13, 0x0D],
    'v': [0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04],
    'w': [0x00, 0x00, 0x11, 0x11, 0x15, 0x15, 0x0A],
    'y': [0x00, 0x00, 0x11, 0x11, 0x0F, 0x01, 0x0E],
    '%': [0x19, 0x1A, 0x04, 0x08, 0x13, 0x13, 0x00],
}


class PureCanvas:
    """Pure Python RGBA Canvas and PNG Encoder using standard zlib and struct."""

    def __init__(self, width: int = 640, height: int = 360, bg_color=(0x1E, 0x1E, 0x2E, 0xFF)):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 4)
        self.clear(bg_color)

    def clear(self, color: tuple[int, int, int, int]):
        r, g, b, a = color
        px = bytes([r, g, b, a]) * (self.width * self.height)
        self.pixels[:] = px

    def set_pixel(self, x: int, y: int, color: tuple[int, int, int, int]):
        if 0 <= x < self.width and 0 <= y < self.height:
            idx = (y * self.width + x) * 4
            self.pixels[idx:idx+4] = bytes(color)

    def draw_filled_rect(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int]):
        min_x, max_x = max(0, min(x0, x1)), min(self.width - 1, max(x0, x1))
        min_y, max_y = max(0, min(y0, y1)), min(self.height - 1, max(y0, y1))
        px = bytes(color)
        for y in range(min_y, max_y + 1):
            row_idx = y * self.width * 4
            for x in range(min_x, max_x + 1):
                idx = row_idx + x * 4
                self.pixels[idx:idx+4] = px

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int, int], thickness: int = 1):
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        x, y = x0, y0
        half_t = thickness // 2
        while True:
            for tx in range(-half_t, half_t + 1):
                for ty in range(-half_t, half_t + 1):
                    self.set_pixel(x + tx, y + ty, color)

            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy

    def draw_text(self, x: int, y: int, text: str, color: tuple[int, int, int, int], scale: int = 1):
        curr_x = x
        for ch in text:
            bitmap = FONT_5X7.get(ch, FONT_5X7.get(' '))
            for row_idx, row_byte in enumerate(bitmap):
                for col_idx in range(5):
                    if (row_byte >> (4 - col_idx)) & 1:
                        px_x = curr_x + col_idx * scale
                        px_y = y + row_idx * scale
                        if scale == 1:
                            self.set_pixel(px_x, px_y, color)
                        else:
                            self.draw_filled_rect(px_x, px_y, px_x + scale - 1, px_y + scale - 1, color)
            curr_x += (5 * scale) + (1 * scale)

    def to_png_bytes(self) -> bytes:
        raw_data = bytearray()
        stride = self.width * 4
        for y in range(self.height):
            raw_data.append(0)  # Filter type 0
            idx = y * stride
            raw_data.extend(self.pixels[idx:idx+stride])

        compressed = zlib.compress(bytes(raw_data), level=9)

        png = bytearray(b"\x89PNG\r\n\x1a\n")
        ihdr_data = struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0)
        png.extend(self._make_chunk(b"IHDR", ihdr_data))
        png.extend(self._make_chunk(b"IDAT", compressed))
        png.extend(self._make_chunk(b"IEND", b""))
        return bytes(png)

    def _make_chunk(self, chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return length + chunk_type + data + crc


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
# Pure Python Chart Generation (No Native/C Dependencies)
# ---------------------------------------------------------------------------
def _format_num(n: float) -> str:
    """Format large numbers with K / M suffixes for chart labels."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))


def generate_history_chart(snapshots: list[TikTokSnapshotRecord]) -> Optional[io.BytesIO]:
    """Generate a mobile-friendly PNG chart using pure Python standard library."""
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

    n_points = len(timestamps)
    if n_points < 2:
        return None

    # Canvas dimensions
    width, height = 640, 360
    canvas = PureCanvas(width=width, height=height, bg_color=(0x1E, 0x1E, 0x2E, 0xFF))

    # Colors
    c_card_bg = (0x18, 0x18, 0x25, 0xFF)
    c_grid = (0x31, 0x32, 0x44, 0xFF)
    c_border = (0x45, 0x47, 0x5A, 0xFF)
    c_text = (0xCD, 0xD6, 0xF4, 0xFF)
    c_subtext = (0xA6, 0xAD, 0xC8, 0xFF)

    c_views = (0x38, 0xBD, 0xF8, 0xFF)      # Sky Blue
    c_likes = (0xF4, 0x3F, 0x5E, 0xFF)      # Rose Red
    c_comments = (0x34, 0xD3, 0x99, 0xFF)   # Emerald Green
    c_shares = (0xFB, 0xBF, 0x24, 0xFF)     # Amber Yellow

    # Card area
    x0, y0 = 65, 50
    x1, y1 = 570, 290
    canvas.draw_filled_rect(x0, y0, x1, y1, c_card_bg)
    canvas.draw_filled_rect(x0, y0, x1, y0 + 1, c_border)
    canvas.draw_filled_rect(x0, y1, x1, y1 + 1, c_border)
    canvas.draw_filled_rect(x0, y0, x0 + 1, y1, c_border)
    canvas.draw_filled_rect(x1, y0, x1 + 1, y1, c_border)

    # Title
    canvas.draw_text(x=20, y=18, text="TikTok Analytics History", color=c_text, scale=2)

    # Legend at Top Right
    leg_x = 340
    leg_y = 22
    # Views legend key
    canvas.draw_filled_rect(leg_x, leg_y, leg_x + 8, leg_y + 8, c_views)
    canvas.draw_text(leg_x + 12, leg_y + 1, "Views", c_subtext)
    # Likes legend key
    canvas.draw_filled_rect(leg_x + 65, leg_y, leg_x + 73, leg_y + 8, c_likes)
    canvas.draw_text(leg_x + 77, leg_y + 1, "Likes", c_subtext)
    # Comments legend key
    canvas.draw_filled_rect(leg_x + 130, leg_y, leg_x + 138, leg_y + 8, c_comments)
    canvas.draw_text(leg_x + 142, leg_y + 1, "Comments", c_subtext)
    # Shares legend key
    canvas.draw_filled_rect(leg_x + 195, leg_y, leg_x + 203, leg_y + 8, c_shares)
    canvas.draw_text(leg_x + 207, leg_y + 1, "Shares", c_subtext)

    # Value ranges for scaling
    max_v = max(max(views), 1)
    min_v = min(views)
    range_v = max(max_v - min_v, 1)

    max_inter = max(max(likes + comments + shares), 1)
    min_inter = min(likes + comments + shares)
    range_inter = max(max_inter - min_inter, 1)

    # Gridlines and Y-axis labels
    n_grid = 4
    for i in range(n_grid + 1):
        grid_y = y1 - int(i * (y1 - y0) / n_grid)
        canvas.draw_line(x0, grid_y, x1, grid_y, c_grid, thickness=1)

        # Left Y label (Views)
        v_val = min_v + (i * range_v / n_grid)
        v_label = _format_num(v_val)
        canvas.draw_text(x0 - 5 - (len(v_label) * 6), grid_y - 3, v_label, c_views)

        # Right Y label (Interactions)
        inter_val = min_inter + (i * range_inter / n_grid)
        inter_label = _format_num(inter_val)
        canvas.draw_text(x1 + 6, grid_y - 3, inter_label, c_subtext)

    # X-axis time labels
    step_x = (x1 - x0) / (n_points - 1)
    pts_v = []
    pts_likes = []
    pts_comments = []
    pts_shares = []

    for idx, (dt, v, l, c, s) in enumerate(zip(timestamps, views, likes, comments, shares)):
        px = int(x0 + idx * step_x)
        # Views Y
        py_v = int(y1 - ((v - min_v) / range_v) * (y1 - y0 - 10))
        pts_v.append((px, py_v))

        # Likes Y
        py_l = int(y1 - ((l - min_inter) / range_inter) * (y1 - y0 - 10))
        pts_likes.append((px, py_l))

        # Comments Y
        py_c = int(y1 - ((c - min_inter) / range_inter) * (y1 - y0 - 10))
        pts_comments.append((px, py_c))

        # Shares Y
        py_s = int(y1 - ((s - min_inter) / range_inter) * (y1 - y0 - 10))
        pts_shares.append((px, py_s))

        # Time tick label at intervals
        if idx == 0 or idx == n_points - 1 or idx == n_points // 2:
            time_str = dt.strftime("%m/%d %H:%M")
            lbl_x = max(x0, min(px - 25, x1 - 50))
            canvas.draw_text(lbl_x, y1 + 8, time_str, c_subtext)

    # Plot trend lines and data markers
    def draw_series(pts, color, thickness=2, marker_type="square"):
        for i in range(len(pts) - 1):
            canvas.draw_line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], color, thickness=thickness)
        for px, py in pts:
            if marker_type == "square":
                canvas.draw_filled_rect(px - 2, py - 2, px + 2, py + 2, color)
            else:
                canvas.draw_filled_rect(px - 3, py - 3, px + 3, py + 3, color)

    draw_series(pts_v, c_views, thickness=3, marker_type="box")
    draw_series(pts_likes, c_likes, thickness=2, marker_type="square")
    draw_series(pts_comments, c_comments, thickness=2, marker_type="square")
    draw_series(pts_shares, c_shares, thickness=2, marker_type="square")

    buf = io.BytesIO(canvas.to_png_bytes())
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
