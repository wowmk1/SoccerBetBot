import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ==== ENVIRONMENT VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing one or more environment variables.")

# ==== BOT SETUP ====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== LEADERBOARD PERSISTENCE ====
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

# ==== POSTED MATCHES TRACKING ====
POSTED_MATCHES_FILE = "posted_matches.json"
if os.path.exists(POSTED_MATCHES_FILE):
    with open(POSTED_MATCHES_FILE, "r") as f:
        posted_matches = set(json.load(f))
else:
    posted_matches = set()

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

def save_posted_matches():
    with open(POSTED_MATCHES_FILE, "w") as f:
        json.dump(list(posted_matches), f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

# ==== CLUB ICONS ====
CLUB_ICONS = {
    # Example: "Manchester United": "https://link_to_icon.png"
}

PLACEHOLDER_ICON = "https://via.placeholder.com/64.png?text=?"

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def record(self, interaction, prediction):
        user_id = str(interaction.user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
        leaderboard[user_id]["predictions"][str(self.match_id)] = {
            "choice": prediction,
            "avatar": str(interaction.user.display_avatar.url)
        }
        save_leaderboard()
        await interaction.response.send_message(f"✅ Prediction saved: **{prediction}**", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record(interaction, "AWAY_TEAM")

# ==== LEADERBOARD RESET ====
class LeaderboardResetConfirm(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="✅ Confirm Reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)
            return
        global leaderboard
        leaderboard = {}
        save_leaderboard()
        await interaction.response.send_message("✅ Leaderboard has been reset!", ephemeral=True)
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel:
            await channel.send("🔄 The leaderboard has been reset by an admin.")
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Reset cancelled.", ephemeral=True)
        self.stop()

class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reset Leaderboard", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)
            return
        await interaction.response.send_message("⚠️ Are you sure you want to reset the leaderboard?", 
                                                view=LeaderboardResetConfirm(), ephemeral=True)

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
    return matches

# ==== POST MATCH ====
async def post_match(match):
    if match["id"] in posted_matches:
        return  # skip already posted

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home_icon = CLUB_ICONS.get(match['homeTeam']['name'], PLACEHOLDER_ICON)
    away_icon = CLUB_ICONS.get(match['awayTeam']['name'], PLACEHOLDER_ICON)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=home_icon)
    embed.set_image(url=away_icon)

    await channel.send(embed=embed, view=MatchView(match["id"]))
    posted_matches.add(match["id"])
    save_posted_matches()

# ==== BACKGROUND AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return
    for match in matches:
        await post_match(match)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    for match in matches[:5]:
        home_icon = CLUB_ICONS.get(match['homeTeam']['name'], PLACEHOLDER_ICON)
        away_icon = CLUB_ICONS.get(match['awayTeam']['name'], PLACEHOLDER_ICON)

        embed = discord.Embed(
            title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            description=f"Kickoff: {match['utcDate']}",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=home_icon)
        embed.set_image(url=away_icon)
        await interaction.channel.send(embed=embed, view=MatchView(match["id"]))
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(leaderboard.values(), key=lambda x: x["points"], reverse=True)
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts"
                      for i, entry in enumerate(sorted_lb[:10])])

    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
