import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timezone

# ------------------ CONFIG ------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not TOKEN or not API_KEY or not MATCH_CHANNEL_ID or not LEADERBOARD_CHANNEL_ID:
    raise ValueError("One or more environment variables are missing: DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID")

# ------------------ BOT SETUP ------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ DATA STORAGE ------------------
# In-memory storage; replace with DB for persistence
users_bets = {}  # {user_id: {match_id: bet_info}}
leaderboard = {}  # {user_id: score}

LEAGUES = [
    "WC", "CL", "BL1", "DED", "PD", "FL1", "ELC",
    "PPL", "EC", "SA", "PL"
]

# ------------------ FOOTBALL DATA API ------------------
async def fetch_matches():
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": API_KEY}

    today = datetime.now(timezone.utc).date()
    params = {
        "dateFrom": str(today),
        "dateTo": str(today),
        "status": "SCHEDULED"
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("matches", [])

# ------------------ AUTO POST MATCHES ------------------
@tasks.loop(minutes=5)
async def auto_post_matches():
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        print("Match channel not found")
        return

    matches = await fetch_matches()
    if not matches:
        await channel.send("No upcoming matches to post.")
        return

    for match in matches:
        msg = f"**{match['homeTeam']['name']} vs {match['awayTeam']['name']}**\nLeague: {match['competition']['name']}\nMatch ID: {match['id']}"
        await channel.send(msg)

# ------------------ COMMANDS ------------------
@bot.command()
async def matches(ctx):
    matches = await fetch_matches()
    if not matches:
        await ctx.send("No upcoming matches.")
        return

    msg = "\n".join([f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} | League: {m['competition']['name']}" for m in matches])
    await ctx.send(msg)

@bot.command()
async def leaderboards(ctx):
    if not leaderboard:
        await ctx.send("No scores yet.")
        return
    sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    msg = "\n".join([f"<@{uid}>: {score}" for uid, score in sorted_lb])
    await ctx.send(f"**Leaderboard**\n{msg}")

@bot.command()
async def bet(ctx, match_id: int, prediction: str):
    now = datetime.now(timezone.utc)
    # Fetch match details
    matches = await fetch_matches()
    match = next((m for m in matches if m['id'] == match_id), None)
    if not match:
        await ctx.send("Match not found or already started/finished.")
        return

    user_bets = users_bets.get(ctx.author.id, {})
    if match_id in user_bets:
        await ctx.send("You already placed a bet on this match.")
        return

    user_bets[match_id] = {"prediction": prediction, "timestamp": now}
    users_bets[ctx.author.id] = user_bets
    await ctx.send(f"Bet placed on match {match_id}: {prediction}")

# ------------------ EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    auto_post_matches.start()

# ------------------ RUN BOT ------------------
bot.run(TOKEN)
