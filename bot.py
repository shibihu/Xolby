import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import logging
import os
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

    async def setup_hook(self):
        # Read-only scanner feature (/scan). Loaded defensively so a problem
        # with the scanner can never break the existing /populargames command.
        try:
            await self.load_extension("commands.scan")
        except Exception:
            log.exception("Failed to load commands.scan; /populargames is unaffected")
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
    except Exception as e:
        await interaction.followup.send(
            f"❌ Could not fetch Roblox game data.\n`{type(e).__name__}: {e}`"
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
