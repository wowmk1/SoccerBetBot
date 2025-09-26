import os
import discord
from discord.ext import tasks
from discord import app_commands
import requests

# ----------------------
# Bot setup
# ----------------------
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ----------------------
# Environment variables
# ----------------------
TOKEN = os.environ.get("TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
API_KEY = os.environ.get("API_KEY")

# ----------------------
# Data storage
# ----------------------
points = {}  # user_id -> points
user_bets = {}  # match_id -> {user_id: bet_choice}
posted_matches = set()  # match IDs already posted

# ----------------------
# Fetch upcoming matches
# ----------------------
def fetch_matches():
    url = "https://api.football-data.org/v4/matches?status=SCHEDULED,FINISHED"
    headers = {"X-Auth-Token": API_KEY}
    try:
        resp = requests.get(url, headers=headers)
        print("API status:", resp.status_code)
        if resp.status_code != 200:
            print("API response:", resp.text[:500])
            return None
        data = resp.json()
        return data.get("matches", [])
    except Exception as e:
        print("Fetch error:", e)
        return None

# ----------------------
# Determine match result
# ----------------------
def get_match_result(match):
    score = match.get("score", {}).get("fullTime", {})
    home_score = score.get("home")
    away_score = score.get("away")
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    elif home_score < away_score:
        return "away"
    else:
        return "draw"

# ----------------------
# Betting buttons
# ----------------------
class BetView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def record_bet(self, interaction: discord.Interaction, choice: str):
        if self.match_id not in user_bets:
            user_bets[self.match_id] = {}
        user_bets[self.match_id][interaction.user.id] = choice
        await interaction.response.send_message(f"You bet on {choice.capitalize()}!", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.green)
    async def home_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "home")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.gray)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "draw")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.red)
    async def away_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "away")

# ----------------------
# Slash commands
# ----------------------
@tree.command(name="matches", description="Show upcoming matches")
async def matches_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    match_list = fetch_matches()
    if not match_list:
        await interaction.followup.send("❌ Error fetching matches. Please try again later.")
        return

    for m in match_list[:5]:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        home_logo = m["homeTeam"].get("crest", "")
        away_logo = m["awayTeam"].get("crest", "")

        embed = discord.Embed(
            title=f"{home} vs {away}",
            description="Place your bet!",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=home_logo or away_logo)
        await interaction.followup.send(embed=embed, view=BetView(m["id"]))

@tree.command(name="leaderboard", description="Show top scores")
async def leaderboard(interaction: discord.Interaction):
    if not points:
        await interaction.response.send_message("No scores yet.")
        return
    sorted_points = sorted(points.items(), key=lambda x: x[1], reverse=True)
    msg = "\n".join([f"<@{user}>: {pts}" for user, pts in sorted_points[:10]])
    await interaction.response.send_message(f"🏆 Leaderboard 🏆\n{msg}")

# ----------------------
# Auto-post upcoming matches
# ----------------------
@tasks.loop(minutes=5)
async def auto_post_matches():
    if CHANNEL_ID == 0:
        print("No CHANNEL_ID set for auto-posting.")
        return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found.")
        return

    matches = fetch_matches()
    if not matches:
        print("No matches fetched for auto-post.")
        return

    for m in matches:
        if m["id"] in posted_matches:
            continue
        if m.get("status") != "SCHEDULED":
            continue
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        home_logo = m["homeTeam"].get("crest", "")
        away_logo = m["awayTeam"].get("crest", "")

        embed = discord.Embed(
            title=f"{home} vs {away}",
            description="Place your bets!",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=home_logo or away_logo)

        await channel.send(embed=embed, view=BetView(m["id"]))
        posted_matches.add(m["id"])
        print(f"Posted match: {home} vs {away}")

# ----------------------
# Score finished matches
# ----------------------
@tasks.loop(minutes=5)
async def score_matches():
    matches = fetch_matches()
    if not matches:
        return

    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        match_id = m["id"]
        result = get_match_result(m)
        if match_id in user_bets:
            for user_id, bet in user_bets[match_id].items():
                if bet == result:
                    points[user_id] = points.get(user_id, 0) + 1
            del user_bets[match_id]
            print(f"Scored match {match_id}, updated points.")

# ----------------------
# Bot events
# ----------------------
@bot.event
async def on_ready():
    await tree.sync()
    auto_post_matches.start()
    score_matches.start()
    print(f"Logged in as {bot.user}")

# ----------------------
# Run bot
# ----------------------
bot.run(TOKEN)
