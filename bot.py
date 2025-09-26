import os
import asyncio
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from discord.ext import tasks, commands
from discord import app_commands

# ------------------ CONFIG ------------------
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing one or more required environment variables.")

# ------------------ LEAGUES ------------------
LEAGUES = {
    "WC": "FIFA World Cup",
    "CL": "UEFA Champions League",
    "BL1": "Bundesliga",
    "DED": "Eredivisie",
    "PD": "Primera Division",
    "FL1": "Ligue 1",
    "ELC": "Championship",
    "PPL": "Primeira Liga",
    "EC": "European Championship",
    "SA": "Serie A",
    "PL": "Premier League"
}

# ------------------ BOT ------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Store posted matches to prevent double-posting
posted_matches = set()
# Store bets {user_id: {match_id: choice}}
bets = {}
# Store scores {user_id: points}
scores = {}

# ------------------ FETCH MATCHES ------------------
async def fetch_matches():
    url = "https://api.football-data.org/v4/matches"
    today = datetime.now(timezone.utc).date()
    two_days_later = today + timedelta(days=2)

    params = {
        "dateFrom": today.isoformat(),
        "dateTo": two_days_later.isoformat(),
        "competitions": ",".join(LEAGUES.keys())
    }

    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as r:
            data = await r.json()
            return data.get("matches", [])

# ------------------ POST MATCHES ------------------
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    kick_off = match["utcDate"]

    embed = discord.Embed(
        title=f"{home} vs {away}",
        description=f"Kick-off: {kick_off}",
        color=discord.Color.green()
    )
    embed.add_field(name="League", value=LEAGUES.get(match["competition"]["code"], "Unknown"))

    await channel.send(embed=embed)

# ------------------ AUTO POST TASK ------------------
@tasks.loop(minutes=10)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        print("No upcoming matches to post.")
        return

    now = datetime.now(timezone.utc)

    for match in matches:
        match_id = match["id"]
        match_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))

        if match_id in posted_matches:
            continue
        if match_time <= now:
            continue

        await post_match(match)
        posted_matches.add(match_id)

# ------------------ COMMANDS ------------------
@tree.command(name="matches", description="Show upcoming matches")
async def matches_command(interaction: discord.Interaction):
    upcoming = await fetch_matches()
    if not upcoming:
        await interaction.response.send_message("No upcoming matches.")
        return

    msg = ""
    now = datetime.now(timezone.utc)
    for match in upcoming:
        match_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        if match_time <= now:
            continue
        msg += f"{match['homeTeam']['name']} vs {match['awayTeam']['name']} ({LEAGUES.get(match['competition']['code'], 'Unknown')})\n"

    await interaction.response.send_message(msg or "No upcoming matches.")

@tree.command(name="leaderboard", description="Show leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    if not scores:
        await interaction.response.send_message("No scores yet.")
        return

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    msg = "\n".join(f"<@{user_id}>: {points}" for user_id, points in sorted_scores)
    await interaction.response.send_message(msg)

# ------------------ BET FUNCTION ------------------
async def place_bet(user_id, match_id, choice, match_time):
    now = datetime.now(timezone.utc)
    if now >= match_time:
        return False, "Cannot bet after match started."

    user_bets = bets.setdefault(user_id, {})
    if match_id in user_bets:
        return False, "You already bet on this match."

    user_bets[match_id] = choice
    return True, "Bet placed successfully."

# ------------------ EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    auto_post_matches.start()
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# ------------------ RUN ------------------
bot.run(DISCORD_BOT_TOKEN)
