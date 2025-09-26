import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO

import discord
from discord.ext import commands, tasks
from discord import app_commands

from PIL import Image

# ==== ENVIRONMENT VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing environment variables")

# ==== BOT SETUP ====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== LEADERBOARD ====
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

# ==== MATCH VIEW ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.user_votes = {}

    def get_button_style(self, user_id, prediction):
        if str(user_id) in self.user_votes and self.user_votes[str(user_id)] == prediction:
            # Change color for voted button
            if prediction == "HOME_TEAM":
                return discord.ButtonStyle.success
            elif prediction == "DRAW":
                return discord.ButtonStyle.secondary
            elif prediction == "AWAY_TEAM":
                return discord.ButtonStyle.danger
        return discord.ButtonStyle.primary

    async def update_view_for_user(self, interaction: discord.Interaction):
        for child in self.children:
            label_key = child.label.replace(" ", "_").upper()
            child.disabled = str(interaction.user.id) in self.user_votes
            child.style = self.get_button_style(interaction.user.id, label_key)
        await interaction.message.edit(view=self)

    async def vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        self.user_votes[user_id] = prediction
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()
        await self.update_view_for_user(interaction)
        if not interaction.response.is_done():
            await interaction.response.send_message(f"✅ You voted: **{prediction}**", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.primary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.primary)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "AWAY_TEAM")

# ==== IMAGE HANDLING ====
async def get_match_image(home_crest_url, away_crest_url):
    size = (128, 128)
    async with aiohttp.ClientSession() as session:
        home_bytes = away_bytes = None
        try:
            async with session.get(home_crest_url) as resp:
                if resp.status == 200:
                    home_bytes = await resp.read()
        except: pass
        try:
            async with session.get(away_crest_url) as resp:
                if resp.status == 200:
                    away_bytes = await resp.read()
        except: pass

    home_img = Image.open(BytesIO(home_bytes)).convert("RGBA") if home_bytes else Image.new("RGBA", size, (200, 200, 200))
    away_img = Image.open(BytesIO(away_bytes)).convert("RGBA") if away_bytes else Image.new("RGBA", size, (200, 200, 200))

    home_img = home_img.resize(size)
    away_img = away_img.resize(size)

    # Combine side by side
    combined = Image.new("RGBA", (size[0]*2 + 20, size[1]), (255, 255, 255, 0))
    combined.paste(home_img, (0, 0), home_img)
    combined.paste(away_img, (size[0] + 20, 0), away_img)

    output = BytesIO()
    combined.save(output, format="PNG")
    output.seek(0)
    return output

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
                    matches.extend(data.get("matches", []))
    # Only future matches
    matches = [m for m in matches if datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")) > now]
    return matches

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )

    # Get combined image
    home_crest = match['homeTeam'].get("crest")
    away_crest = match['awayTeam'].get("crest")
    if home_crest or away_crest:
        img_bytes = await get_match_image(home_crest, away_crest)
        file = discord.File(fp=img_bytes, filename="match.png")
        embed.set_image(url="attachment://match.png")
        await channel.send(file=file, embed=embed, view=MatchView(match["id"]))
    else:
        await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    for match in matches[:5]:
        await post_match(match)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return
    sorted_lb = sorted(
        leaderboard.values(),
        key=lambda x: (-x["points"], x["name"].lower())
    )
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts"
                      for i, entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
