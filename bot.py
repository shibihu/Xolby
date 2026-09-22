import asyncio
import datetime
import logging
import os
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
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put it in .env")

intents = discord.Intents.default()


class PopularGamesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

    async def setup_hook(self):
        # Read-only scanner feature (/scan). Loaded defensively so a problem
        # with the scanner can never break the existing /populargames command.
        try:
            await self.load_extension("commands.scan")
        except Exception:
            log.exception("Failed to load commands.scan; /populargames is unaffected")

        # Channel clear command (/clear). Loaded defensively.
        try:
            await self.load_extension("commands.clear")
        except Exception:
            log.exception("Failed to load commands.clear; /populargames is unaffected")

        # Moderation commands (/purge, /slowmode, /lock, /unlock, /kick, /ban, /unban, /timeout, /warn, /warnings)
        try:
            await self.load_extension("commands.moderation")
        except Exception:
            log.exception("Failed to load commands.moderation; /populargames is unaffected")

        # Server Info commands (/serverinfo, /userinfo, /roleinfo, /channelinfo, /avatar, /roles, /channels, /membercount)
        try:
            await self.load_extension("commands.info")
        except Exception:
            log.exception("Failed to load commands.info; /populargames is unaffected")

        # Utility commands (/ping, /uptime, /botinfo, /help, /invite, /timestamp, /poll, /remind)
        try:
            await self.load_extension("commands.utility")
        except Exception:
            log.exception("Failed to load commands.utility; /populargames is unaffected")

        await self.tree.sync()


bot = PopularGamesBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="populargames", description="Show the current Roblox Top 20 games by active players.")
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


bot.run(TOKEN)
