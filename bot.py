import os
import json
import aiohttp
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
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Leaderboard persistence
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}

# Only include desired leagues
COMPETITIONS = ["PL", "CL", "BL1", "PD", "FL1", "SA", "EC", "WC"]

# ==== SAVE LEADERBOARD ====
def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== VOTE EMOJIS ====
VOTE_EMOJIS = {
    "🏠": "HOME_TEAM",
    "⚖️": "DRAW",
    "🛫": "AWAY_TEAM"
}

# ==== POST MATCH ====
async def post_match(match):
    # Skip past matches
    match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    if match_time < datetime.now(timezone.utc):
        return

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    match_id = str(match["id"])
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]

    embed_desc = f"Kickoff: {match['utcDate']}"
    if voter_names:
        embed_desc += "\n\n**Voted:** " + ", ".join(voter_names)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=embed_desc,
        color=discord.Color.blue()
    )

    msg = await channel.send(embed=embed)

    # Add reaction options
    for emoji in VOTE_EMOJIS:
        await msg.add_reaction(emoji)

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
                        matches.append(m)

    upcoming = [m for m in matches if datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) > datetime.now(timezone.utc)]
    return upcoming

# ==== AUTO POST MATCHES ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    for m in matches:
        await post_match(m)

# ==== REACTION HANDLING ====
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    if str(payload.emoji) not in VOTE_EMOJIS:
        return

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    match_id = str(message.id)  # Using message ID as match ID

    user_id = str(payload.user_id)
    user = await bot.fetch_user(payload.user_id)

    # Initialize user in leaderboard
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": user.name, "points": 0, "predictions": {}}

    # Remove other votes if user already voted
    for react in message.reactions:
        if str(react.emoji) != str(payload.emoji):
            async for u in react.users():
                if u.id == payload.user_id:
                    await react.remove(u)

    # Save vote
    leaderboard[user_id]["predictions"][match_id] = VOTE_EMOJIS[str(payload.emoji)]
    save_leaderboard()

    # Update embed with voter names
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]
    embed = message.embeds[0]
    kickoff_line = embed.description.split("Kickoff:")[1].splitlines()[0]
    embed.description = f"Kickoff: {kickoff_line}"
    if voter_names:
        embed.description += "\n\n**Voted:** " + ", ".join(voter_names)

    await message.edit(embed=embed)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return
    for m in matches[:10]:
        await post_match(m)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    users = [v for v in leaderboard.values() if v.get("predictions")]
    if not users:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
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
