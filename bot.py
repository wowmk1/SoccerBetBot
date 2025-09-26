import os
import discord
from discord.ext import tasks
from discord import app_commands
import requests

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Environment variables
TOKEN = os.environ.get("TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
API_KEY = os.environ.get("API_KEY")

# Simple points leaderboard
points = {}

# Active matches (for testing)
active_matches = {}

# Debug function to fetch matches
def fetch_matches():
    url = "https://api.football-data.org/v4/matches?status=SCHEDULED"
    headers = {"X-Auth-Token": API_KEY}
    try:
        resp = requests.get(url, headers=headers)
        print("API status:", resp.status_code)
        print(resp.text[:500])  # first 500 chars
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("matches", [])
    except Exception as e:
        print("Fetch error:", e)
        return None

# Slash commands
@tree.command(name="matches", description="Show upcoming matches")
async def matches_cmd(interaction: discord.Interaction):
    match_list = fetch_matches()
    if not match_list:
        await interaction.response.send_message("❌ Error fetching matches. Please try again later.")
        return
    msg = ""
    for m in match_list[:5]:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        msg += f"{home} vs {away}\n"
    await interaction.response.send_message(msg)

@tree.command(name="leaderboard", description="Show top scores")
async def leaderboard(interaction: discord.Interaction):
    if not points:
        await interaction.response.send_message("No scores yet.")
        return
    sorted_points = sorted(points.items(), key=lambda x: x[1], reverse=True)
    msg = "\n".join([f"<@{user}>: {pts}" for user, pts in sorted_points[:10]])
    await interaction.response.send_message(f"🏆 Leaderboard 🏆\n{msg}")

# On ready
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")

# Run the bot
bot.run(TOKEN)
