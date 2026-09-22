import asyncio
import datetime
import logging
import os
import sys
import traceback
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from services.roblox import get_top_games

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()


class PopularGamesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

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
            embed.set_footer(text="Roblox Popular Games • fetched just now")
            await interaction.followup.send(embed=embed)

    async def setup_hook(self):
        extensions = [
            "commands.clear",
            "commands.info",
            "commands.moderation",
            "commands.scan",
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
            "ping", "uptime", "botinfo", "help", "invite", "timestamp", "poll", "remind"
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


bot = PopularGamesBot()


@bot.event
async def on_ready():
    print(f"\nLogged in as {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Put it in .env")
    bot.run(TOKEN)
