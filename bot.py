import asyncio
import datetime
import logging
import os
import sys
import time
import traceback
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load .env BEFORE importing services that read environment variables.
# Some service modules create environment-dependent singletons at import time
# (e.g. services.web_backend.web_backend reads XOLBY_WEB_BASE_URL /
# XOLBY_WEB_API_KEY, and services.roblox reads ROBLOX_SESSION_ID).
load_dotenv()

from services.db import db
from services.roblox import roblox_cache, get_top_games
from services.tiktok_analytics import TikTokLiveManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()


def build_popular_games_embed(games: list[dict], live_status: str = "🟢 LIVE") -> discord.Embed:
    embed = discord.Embed(
        title="🔥 Roblox — Top 20 Popular Games",
        description="เรียงตามจำนวนผู้เล่นที่กำลังเล่นอยู่จากข้อมูลล่าสุดที่ Roblox API ส่งกลับมา",
        color=0xF2A900,
    )

    lines = []
    for i, game in enumerate(games[:20], 1):
        name = discord.utils.escape_markdown(game["name"])
        players = f'{game["playing"]:,}'
        link = f'https://www.roblox.com/games/{game["rootPlaceId"]}'
        lines.append(f"**{i}. [{name}]({link})**\n👥 `{players}` active players")

    embed.description += "\n\n" + "\n\n".join(lines)
    embed.set_footer(text=f"Roblox Popular Games • {live_status} • fetched just now")
    return embed


class LiveTrackerManager:
    """Manages editing active Discord live tracker messages across servers.

    Consumes fresh data from RobloxLiveCache and updates Discord messages at ~1s interval,
    safely handling Discord rate limits, missing permissions, and deleted messages.
    """

    def __init__(self, bot: commands.Bot, update_interval: float = 1.0) -> None:
        self.bot = bot
        self.update_interval = update_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._active_trackers: dict[int, dict] = {}  # channel_id -> {'guild_id': int, 'message_id': int}

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._update_loop())
            log.info("[LIVE GAMES] LiveTrackerManager started.")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("[LIVE GAMES] LiveTrackerManager stopped.")

    def load_trackers_from_db(self) -> None:
        records = db.get_active_live_trackers()
        for r in records:
            self._active_trackers[r.channel_id] = {
                "guild_id": r.guild_id,
                "message_id": r.message_id,
            }
        log.info("[LIVE GAMES] Loaded %d active tracker(s) from database.", len(records))

    def register_tracker(self, guild_id: int, channel_id: int, message_id: int) -> None:
        db.add_or_update_live_tracker(guild_id, channel_id, message_id)
        self._active_trackers[channel_id] = {
            "guild_id": guild_id,
            "message_id": message_id,
        }
        log.info("[LIVE GAMES] Registered live tracker for channel %d (message %d)", channel_id, message_id)

    def unregister_tracker(self, channel_id: int) -> None:
        db.remove_live_tracker(channel_id)
        self._active_trackers.pop(channel_id, None)
        log.info("[LIVE GAMES] Unregistered live tracker for channel %d", channel_id)

    async def _update_loop(self) -> None:
        await self.bot.wait_until_ready()

        while self._running:
            start_time = time.monotonic()

            if self._active_trackers:
                games = roblox_cache.last_games
                if games:
                    embed = build_popular_games_embed(games)
                    channel_ids = list(self._active_trackers.keys())

                    for channel_id in channel_ids:
                        if not self._running:
                            break
                        tracker = self._active_trackers.get(channel_id)
                        if not tracker:
                            continue

                        message_id = tracker["message_id"]

                        # Resolve channel
                        channel = self.bot.get_channel(channel_id)
                        if channel is None:
                            try:
                                channel = await self.bot.fetch_channel(channel_id)
                            except (discord.NotFound, discord.Forbidden):
                                log.warning("[LIVE GAMES] Channel %d not found or inaccessible. Deactivating tracker.", channel_id)
                                self.unregister_tracker(channel_id)
                                continue
                            except Exception as exc:
                                log.warning("[LIVE GAMES] Could not fetch channel %d: %s", channel_id, exc)
                                continue

                        if not isinstance(channel, discord.abc.Messageable):
                            log.warning("[LIVE GAMES] Channel %d is not messageable. Deactivating tracker.", channel_id)
                            self.unregister_tracker(channel_id)
                            continue

                        # Fetch and edit message
                        try:
                            msg = await channel.fetch_message(message_id)
                            await msg.edit(embed=embed)
                        except discord.NotFound:
                            log.warning("[LIVE GAMES] Tracker message %d in channel %d deleted. Removing tracker.", message_id, channel_id)
                            self.unregister_tracker(channel_id)
                        except discord.Forbidden:
                            log.warning("[LIVE GAMES] Missing permission to edit message in channel %d. Removing tracker.", channel_id)
                            self.unregister_tracker(channel_id)
                        except discord.HTTPException as exc:
                            if exc.status == 429:
                                retry_after = getattr(exc, "retry_after", 5.0) or 5.0
                                log.warning("[LIVE GAMES] Discord rate limited (429). Sleeping %.1fs", retry_after)
                                await asyncio.sleep(retry_after)
                            else:
                                log.warning("[LIVE GAMES] Discord HTTP Exception %d editing message in channel %d: %s", exc.status, channel_id, exc)
                        except Exception as exc:
                            log.warning("[LIVE GAMES] Unexpected error updating tracker for channel %d: %s", channel_id, exc)

            elapsed = time.monotonic() - start_time
            sleep_time = max(0.1, self.update_interval - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break


class PopularGamesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        self.live_tracker_manager = LiveTrackerManager(self)
        # TikTok OAuth, state, token exchange and token storage are owned
        # entirely by the Render web backend. The Termux bot never runs a
        # local OAuth callback server and never handles TikTok secrets.
        self.tiktok_live_manager = TikTokLiveManager(self)

        # Register /populargames directly on self.tree in __init__
        @self.tree.command(name="populargames", description="Show the current Roblox Top 20 games by active players.")
        async def populargames(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            try:
                games = await get_top_games(limit=20)
            except asyncio.TimeoutError:
                log.error("Roblox API request timed out during /populargames execution.")
                await interaction.followup.send(
                    "❌ Could not fetch Roblox game data.\n"
                    "Reason: Roblox API request timed out.\n"
                    "The request was retried automatically."
                )
                return
            except aiohttp.ClientError as e:
                log.error("Roblox API HTTP error: %s: %s", type(e).__name__, e)
                await interaction.followup.send(
                    "❌ Could not fetch Roblox game data.\n"
                    "Reason: Roblox API connection error.\n"
                    "The request was retried automatically."
                )
                return
            except Exception as e:
                log.exception("Unexpected error in /populargames")
                await interaction.followup.send(
                    "❌ Could not fetch Roblox game data.\n"
                    f"Reason: Unexpected error ({type(e).__name__})."
                )
                return

            if not games:
                await interaction.followup.send("❌ Roblox did not return any games.")
                return

            embed = build_popular_games_embed(games)
            msg = await interaction.followup.send(embed=embed, wait=True)

            if msg:
                guild_id = interaction.guild_id or 0
                channel_id = interaction.channel_id or 0
                if channel_id:
                    self.live_tracker_manager.register_tracker(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        message_id=msg.id,
                    )

    async def setup_hook(self):
        # Start Roblox cache & live tracker manager
        roblox_cache.start()
        self.live_tracker_manager.start()
        self.live_tracker_manager.load_trackers_from_db()

        # Start TikTok live tracker manager (data is fetched from Render)
        self.tiktok_live_manager.start()

        extensions = [
            "commands.clear",
            "commands.info",
            "commands.moderation",
            "commands.scan",
            "commands.tiktok",
            "commands.utility",
        ]

        print("[EXTENSIONS] Loading extensions...")
        log.info("[EXTENSIONS] Loading extensions...")

        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"[EXTENSION] Loaded: {ext}")
                log.info("[EXTENSION] Loaded: %s", ext)
            except Exception as exc:
                print(f"[ERROR] Failed to load {ext}: {type(exc).__name__}: {exc}", file=sys.stderr)
                traceback.print_exc()
                log.exception("[ERROR] Failed to load %s", ext)
                raise RuntimeError(f"Failed to load extension {ext}: {type(exc).__name__}: {exc}") from exc

        # Inspect local command tree
        registered_cmds = sorted([cmd.name for cmd in self.tree.get_commands()])
        total_cmds = len(registered_cmds)

        print("\n[COMMAND DIAGNOSTIC]")
        print(f"Registered locally: {total_cmds}")
        print("Commands:")
        for cmd_name in registered_cmds:
            print(f"  /{cmd_name}")

        log.info("[COMMAND DIAGNOSTIC] Registered locally: %d commands", total_cmds)

        # Expected core commands check
        expected_commands = {
            "clear", "populargames", "scan",
            "purge", "slowmode", "lock", "unlock", "kick", "ban", "unban", "timeout", "warn", "warnings",
            "serverinfo", "userinfo", "roleinfo", "channelinfo", "avatar", "roles", "channels", "membercount",
            "ping", "uptime", "botinfo", "help", "invite", "timestamp", "poll", "remind",
            "tiktokconnect", "tiktokstats", "tiktoklive", "tiktokhistory", "tiktokdisconnect"
        }
        missing_commands = sorted(expected_commands - set(registered_cmds))
        if missing_commands:
            warn_msg = f"⚠️ Expected command(s) missing from tree: {', '.join('/' + c for c in missing_commands)}"
            print(f"\n[WARNING] {warn_msg}", file=sys.stderr)
            log.warning(warn_msg)

        dev_guild_id = (os.getenv("DEV_GUILD_ID") or "").strip()
        print("\n[SYNC]")
        if dev_guild_id and dev_guild_id.isdigit():
            guild_object = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild_object)
            synced = await self.tree.sync(guild=guild_object)
            print("Mode: DEV_GUILD")
            print(f"Target guild: {dev_guild_id}")
            print(f"Synced commands: {len(synced)}")
            print("Commands are being synchronized to DEV_GUILD_ID only.")
            log.info("[SYNC] Mode: DEV_GUILD | Target: %s | Synced: %d", dev_guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            print("Mode: GLOBAL")
            print(f"Synced commands: {len(synced)}")
            log.info("[SYNC] Mode: GLOBAL | Synced: %d", len(synced))

    async def close(self):
        await self.tiktok_live_manager.stop()
        await self.live_tracker_manager.stop()
        await roblox_cache.stop()
        await super().close()


bot = PopularGamesBot()


@bot.event
async def on_ready():
    print(f"\nLogged in as {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Put it in .env")
    bot.run(TOKEN)
