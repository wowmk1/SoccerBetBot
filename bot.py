import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ==== ENVIRONMENT VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

# ==== BOT SETUP ====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "PD", "SA"]
MATCH_CACHE = {}

# ==== CREST COMBINER ====
async def generate_match_image(home_url, away_url):
    async with aiohttp.ClientSession() as session:
        home_bytes = await (await session.get(home_url)).read() if home_url else None
        away_bytes = await (await session.get(away_url)).read() if away_url else None

    size = (100, 100)
    img = Image.new("RGBA", (size[0]*2 + 40, size[1]), (255, 255, 255, 0))

    if home_bytes:
        home = Image.open(BytesIO(home_bytes)).convert("RGBA").resize(size)
        img.paste(home, (0, 0), home)
    if away_bytes:
        away = Image.open(BytesIO(away_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0]+40, 0), away)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def record_vote(self, interaction: discord.Interaction, prediction):
        user_id = str(interaction.user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}

        if str(self.match_id) in leaderboard[user_id]["predictions"]:
            await interaction.response.send_message("🔒 You already voted on this match.", ephemeral=True)
            return

        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()

        view = discord.ui.View()
        for child in self.children:
            label = child.label
            key = label.replace(" ", "_").upper()
            style = discord.ButtonStyle.primary
            if key == prediction:
                style = {
                    "HOME_TEAM": discord.ButtonStyle.success,
                    "DRAW": discord.ButtonStyle.secondary,
                    "AWAY_TEAM": discord.ButtonStyle.danger
                }.get(key, discord.ButtonStyle.primary)
            view.add_item(discord.ui.Button(label=label, style=style, disabled=True))

        embed = discord.Embed(description=f"✅ You voted: **{prediction}**")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=view)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "AWAY_TEAM")

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    image_buffer = await generate_match_image(home_crest, away_crest)
    file = discord.File(fp=image_buffer, filename="match.png")

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_image(url="attachment://match.png")
    await channel.send(embed=embed, view=MatchView(match["id"]), file=file)

# ==== FETCH MATCHES ====
async def fetch_matches():
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={tomorrow.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("matches", []):
                        m["competition"]["name"] = data.get("competition", {}).get("name", comp)
                        MATCH_CACHE[str(m["id"])] = m
                        matches.append(m)
    return matches

# ==== AUTO POST MATCHES ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return

    leagues = {}
    for m in matches:
        league_name = m["competition"]["name"]
        leagues.setdefault(league_name, []).append(m)

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    for league, league_matches in leagues.items():
        embed = discord.Embed(title=f"🏆 {league} Matches", color=discord.Color.purple())
        embed.description = "\n\n".join([f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}\nKickoff: {m['utcDate']}" for m in league_matches])
        await channel.send(embed=embed)
        for m in league_matches:
            await post_match(m)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    leagues = {}
    for m in matches[:10]:
        league_name = m["competition"]["name"]
        leagues.setdefault(league_name, []).append(m)

    for league, league_matches in leagues.items():
        embed = discord.Embed(title=f"🏆 {league} Matches", color=discord.Color.green())
        embed.description = "\n\n".join([f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}\nKickoff: {m['utcDate']}" for m in league_matches])
        await interaction.channel.send(embed=embed)
        for m in league_matches:
            await post_match(m)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    users = [v for v in leaderboard.values() if v.get("predictions")]
    if not users:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts" for i, entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"✅ Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
