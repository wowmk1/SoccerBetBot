import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import requests
from datetime import datetime, timedelta
import json
import os

# ------------------ CONFIG ------------------
# Use environment variables for secrets
TOKEN = os.environ["TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])  # must be integer
API_KEY = os.environ["API_KEY"]

# Leagues to track (PL=EPL, PD=La Liga, SA=Serie A, CL=Champions League, EL=Europa League, WC=World Cup)
LEAGUES = "PL,PD,SA,CL,EL,WC"

# ------------------ BOT SETUP ------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

points = {}  # user_id -> score
active_matches = {}  # match_id -> {"team1": str, "team2": str, "bets": {user_id: choice}}

headers = {"X-Auth-Token": API_KEY}

# ------------------ HELPER FUNCTIONS ------------------
def save_points():
    with open("points.json", "w") as f:
        json.dump(points, f)

def load_points():
    global points
    try:
        with open("points.json", "r") as f:
            points = json.load(f)
    except FileNotFoundError:
        points = {}

def fetch_upcoming_matches():
    url = f"https://api.football-data.org/v4/matches?competitions={LEAGUES}"
    resp = requests.get(url, headers=headers).json()
    matches = []
    for match in resp.get("matches", []):
        if match["status"] == "SCHEDULED":
            match_time = datetime.fromisoformat(match["utcDate"].replace("Z","+00:00"))
            # include matches starting within 1 hour
            if datetime.utcnow() <= match_time <= datetime.utcnow() + timedelta(hours=1):
                matches.append(match)
    return matches

# ------------------ BOT EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    load_points()
    check_matches.start()

# ------------------ SCHEDULER ------------------
@tasks.loop(minutes=5)
async def check_matches():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("Channel not found!")
        return

    matches = fetch_upcoming_matches()
    for match in matches:
        match_id = str(match["id"])
        if match_id in active_matches:
            continue  # already posted

        team1 = match["homeTeam"]["name"]
        team2 = match["awayTeam"]["name"]
        active_matches[match_id] = {"team1": team1, "team2": team2, "bets": {}}

        # Create buttons
        button1 = Button(label=team1, style=discord.ButtonStyle.primary)
        button_draw = Button(label="Draw", style=discord.ButtonStyle.secondary)
        button2 = Button(label=team2, style=discord.ButtonStyle.primary)

        async def bet_callback(interaction, choice):
            user_id = str(interaction.user.id)
            if user_id in active_matches[match_id]["bets"]:
                await interaction.response.send_message("You already placed a bet!", ephemeral=True)
                return
            active_matches[match_id]["bets"][user_id] = choice
            await interaction.response.send_message(f"You picked **{choice}**!", ephemeral=True)

        button1.callback = lambda i: bet_callback(i, team1)
        button_draw.callback = lambda i: bet_callback(i, "Draw")
        button2.callback = lambda i: bet_callback(i, team2)

        view = View()
        view.add_item(button1)
        view.add_item(button_draw)
        view.add_item(button2)

        embed = discord.Embed(
            title="⚽ Upcoming Match",
            description=f"{team1} vs {team2}\nKickoff: {match['utcDate']}",
            color=discord.Color.blue()
        )
        await channel.send(embed=embed, view=view)

    # Check finished matches
    url = f"https://api.football-data.org/v4/matches?competitions={LEAGUES}"
    resp = requests.get(url, headers=headers).json()
    for match in resp.get("matches", []):
        match_id = str(match["id"])
        if match["status"] == "FINISHED" and match_id in active_matches:
            winner = None
            if match["score"]["winner"] == "HOME_TEAM":
                winner = active_matches[match_id]["team1"]
            elif match["score"]["winner"] == "AWAY_TEAM":
                winner = active_matches[match_id]["team2"]
            else:
                winner = "Draw"

            results = []
            for user, choice in active_matches[match_id]["bets"].items():
                if choice == winner:
                    points[user] = points.get(user, 0) + 1
                    results.append(f"<@{user}> ✅ guessed right! (+1)")
                else:
                    results.append(f"<@{user}> ❌ guessed wrong.")
            save_points()
            await channel.send(
                f"Results for **{active_matches[match_id]['team1']} vs {active_matches[match_id]['team2']}**:\n" +
                "\n".join(results)
            )
            del active_matches[match_id]

# ------------------ COMMANDS ------------------
@bot.command()
async def leaderboard(ctx):
    sorted_points = sorted(points.items(), key=lambda x: x[1], reverse=True)
    lb = "\n".join([f"<@{user}>: {pts}" for user, pts in sorted_points[:10]])
    await ctx.send("🏆 Leaderboard 🏆\n" + (lb if lb else "No scores yet."))

# ------------------ RUN BOT ------------------
bot.run(TOKEN)
