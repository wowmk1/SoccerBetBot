# bot.py
import os
import asyncio
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import tasks, commands
import aiohttp

# ------------------ CONFIG ------------------
TOKEN = os.environ.get("TOKEN")
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

# Supported leagues
LEAGUES = ["WC", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "EC", "SA", "PL"]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory storage for bets
bets = {}  # {match_id: {user_id: amount}}
match_cache = set()  # to prevent double posting
leaderboard = {}  # {user_id: points}


# ------------------ HELPER FUNCTIONS ------------------
async def fetch_matches():
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": API_KEY}
    params = {
        "dateFrom": datetime.utcnow().date(),
        "dateTo": (datetime.utcnow().date()),
        "status": "SCHEDULED",
        "competitions": ",".join(LEAGUES),
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("matches", [])
            return []


def format_match_embed(match):
    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Competition: {match['competition']['name']}\nDate: {match['utcDate']}",
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Match ID: {match['id']}")
    return embed


async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if match['id'] in match_cache:
        return
    embed = format_match_embed(match)
    msg = await channel.send(embed=embed)
    match_cache.add(match['id'])
    return msg


async def post_or_update_leaderboard():
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not leaderboard:
        await channel.send("No scores yet!")
        return

    sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    desc = "\n".join(f"<@{user_id}>: {points} pts" for user_id, points in sorted_lb)
    embed = discord.Embed(title="Leaderboard", description=desc, color=discord.Color.gold())
    await channel.send(embed=embed)


# ------------------ TASKS ------------------
@tasks.loop(minutes=5)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)


# ------------------ COMMANDS ------------------
@bot.command(name="matches")
async def matches_cmd(ctx):
    matches = await fetch_matches()
    if not matches:
        await ctx.send("No upcoming matches to post.")
        return

    for match in matches:
        await post_match(match)
    await ctx.send("Matches posted!")


@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx):
    await post_or_update_leaderboard()


@bot.command(name="bet")
async def bet_cmd(ctx, match_id: int, amount: int):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    matches = await fetch_matches()
    match = next((m for m in matches if m['id'] == match_id), None)
    if not match:
        await ctx.send("Match not found or already started/finished.")
        return

    user_bets = bets.setdefault(match_id, {})
    if ctx.author.id in user_bets:
        await ctx.send("You have already placed a bet on this match.")
        return

    user_bets[ctx.author.id] = amount
    await ctx.send(f"Bet of {amount} points placed on match {match['homeTeam']['name']} vs {match['awayTeam']['name']}.")


# ------------------ EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    auto_post_matches.start()


# ------------------ RUN BOT ------------------
bot.run(TOKEN)
