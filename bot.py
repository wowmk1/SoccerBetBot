import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timezone, timedelta

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([TOKEN, API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing environment variables: DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID")

LEAGUES = ["PL", "BL1", "PD", "SA", "FL1", "DED", "PPL", "ELC", "CL", "WC", "EC"]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

posted_matches = set()
predictions = {}   # {match_id: {user_id: "HOME"/"DRAW"/"AWAY"}}
leaderboard = {}   # {user_id: points}

# --------------- FETCH MATCHES ---------------
async def fetch_matches():
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": API_KEY}
    now = datetime.now(timezone.utc)
    params = {
        "dateFrom": now.date().isoformat(),
        "dateTo": (now + timedelta(days=2)).date().isoformat(),
    }

    async with aiohttp.ClientSession() as session:
        all_matches = []
        for league in LEAGUES:
            league_params = dict(params)
            league_params["competitions"] = league
            async with session.get(url, headers=headers, params=league_params) as r:
                if r.status == 200:
                    data = await r.json()
                    all_matches.extend(data.get("matches", []))
        return all_matches

# --------------- BUTTONS ----------------
class PredictionView(discord.ui.View):
    def __init__(self, match_id, home_team, away_team, start_time):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.start_time = start_time
        self.add_item(discord.ui.Button(label=home_team, style=discord.ButtonStyle.primary, custom_id=f"{match_id}_HOME"))
        self.add_item(discord.ui.Button(label="Draw", style=discord.ButtonStyle.secondary, custom_id=f"{match_id}_DRAW"))
        self.add_item(discord.ui.Button(label=away_team, style=discord.ButtonStyle.danger, custom_id=f"{match_id}_AWAY"))

    async def interaction_check(self, interaction: discord.Interaction):
        now = datetime.now(timezone.utc)
        if now >= self.start_time:
            await interaction.response.send_message("⛔ Voting closed for this match.", ephemeral=True)
            return False

        choice = interaction.data["custom_id"].split("_")[1]
        predictions.setdefault(self.match_id, {})[interaction.user.id] = choice
        await interaction.response.send_message(f"✅ You predicted **{choice}**.", ephemeral=True)
        return True

# --------------- POST MATCH ---------------
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    match_id = match["id"]
    if match_id in posted_matches:
        return

    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    start_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))

    embed = discord.Embed(
        title=f"{home} vs {away}",
        description=f"Kick-off: {start_time.strftime('%Y-%m-%d %H:%M UTC')}",
        color=discord.Color.blue()
    )
    embed.set_footer(text=match["competition"]["name"])

    view = PredictionView(match_id, home, away, start_time)
    await channel.send(embed=embed, view=view)

    posted_matches.add(match_id)

# --------------- AUTO POST TASK ---------------
@tasks.loop(minutes=1)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

    await check_finished_matches(matches)

# --------------- CHECK FINISHED MATCHES + LEADERBOARD ---------------
async def check_finished_matches(matches):
    for match in matches:
        match_id = match["id"]
        status = match.get("status")

        if status == "FINISHED" and match_id in predictions:
            home_score = match["score"]["fullTime"]["home"]
            away_score = match["score"]["fullTime"]["away"]

            if home_score > away_score:
                result = "HOME"
            elif away_score > home_score:
                result = "AWAY"
            else:
                result = "DRAW"

            # Award points
            for user_id, pred in predictions[match_id].items():
                if pred == result:
                    leaderboard[user_id] = leaderboard.get(user_id, 0) + 3

            # Clear predictions for this match
            del predictions[match_id]

            # Post leaderboard update
            await post_leaderboard()

async def post_leaderboard():
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    if not leaderboard:
        await channel.send("Leaderboard is empty.")
        return

    sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for rank, (user_id, points) in enumerate(sorted_lb, start=1):
        user = await bot.fetch_user(user_id)
        lines.append(f"**{rank}. {user.name}** — {points} pts")

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await channel.send(embed=embed)

# --------------- COMMANDS ---------------
@bot.tree.command(name="matches", description="Show upcoming matches")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    now = datetime.now(timezone.utc)

    sent = False
    for match in matches:
        start_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        if start_time > now:
            await post_match(match)
            sent = True

    if not sent:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
    else:
        await interaction.response.send_message("Upcoming matches posted.", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the current leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.")
        return

    sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for rank, (user_id, points) in enumerate(sorted_lb, start=1):
        user = await bot.fetch_user(user_id)
        lines.append(f"**{rank}. {user.name}** — {points} pts")

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)

# --------------- STARTUP ---------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()
    auto_post_matches.start()

bot.run(TOKEN)
