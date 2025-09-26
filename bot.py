import os
import discord
from discord.ext import tasks
from discord import app_commands
import requests
from datetime import datetime, timezone, timedelta

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
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))  # default channel for auto-posting
API_KEY = os.environ.get("API_KEY")

# ----------------------
# Data storage
# ----------------------
server_points = {}  # guild_id -> {user_id -> points}
user_bets = {}      # match_id -> {user_id: bet_choice}
posted_matches = set()  # match IDs already posted
match_lookup = {}   # match_id -> match info

# ----------------------
# Fetch upcoming matches (today + next 3 days)
# ----------------------
def fetch_upcoming_matches(days_ahead=3):
    """
    Fetch matches from today up to `days_ahead` days in the future.
    """
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": API_KEY}
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print("API response:", resp.text[:500])
            return None
        data = resp.json()
        matches = data.get("matches", [])

        upcoming = []
        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=days_ahead)

        for m in matches:
            status = m.get("status")
            match_date_str = m.get("utcDate")
            match_date = datetime.fromisoformat(match_date_str.replace("Z", "+00:00"))

            if status == "SCHEDULED" and now <= match_date <= end_date:
                upcoming.append(m)

        return upcoming
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
        match = match_lookup.get(self.match_id)
        if not match:
            await interaction.response.send_message("❌ Match not found!", ephemeral=True)
            return

        status = match.get("status")
        match_time_str = match.get("utcDate")
        match_time = datetime.fromisoformat(match_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)

        if status != "SCHEDULED" or match_time <= now:
            await interaction.response.send_message("❌ Betting is closed for this match!", ephemeral=True)
            return

        if self.match_id not in user_bets:
            user_bets[self.match_id] = {}
        if interaction.user.id in user_bets[self.match_id]:
            await interaction.response.send_message("❌ You already placed a bet on this match!", ephemeral=True)
            return

        user_bets[self.match_id][interaction.user.id] = choice
        await interaction.response.send_message(f"✅ You bet on {choice.capitalize()}!", ephemeral=True)

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
    match_list = fetch_upcoming_matches()
    if not match_list:
        await interaction.followup.send("❌ No upcoming matches found.")
        return

    for m in match_list[:5]:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        home_logo = m["homeTeam"].get("crest", "")
        away_logo = m["awayTeam"].get("crest", "")
        match_lookup[m["id"]] = m

        embed = discord.Embed(
            title=f"{home} vs {away}",
            description="Place your bet!",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=home_logo or away_logo)
        await interaction.followup.send(embed=embed, view=BetView(m["id"]))

@tree.command(name="leaderboard", description="Show top scores in this server")
async def leaderboard(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    points_dict = server_points.get(guild_id, {})

    if not points_dict:
        await interaction.response.send_message("No scores yet in this server. Be the first to bet! ⚽")
        return

    sorted_points = sorted(points_dict.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (user_id, pts) in enumerate(sorted_points):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <@{user_id}> — **{pts} point{'s' if pts != 1 else ''}**")

    embed = discord.Embed(
        title="🏆 Server Leaderboard 🏆",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Keep betting to climb the ranks!")
    await interaction.response.send_message(embed=embed)

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

    matches = fetch_upcoming_matches(days_ahead=3)
    if not matches:
        print("No upcoming matches to post.")
        return

    for m in matches:
        if m["id"] in posted_matches:
            continue
        posted_matches.add(m["id"])
        match_lookup[m["id"]] = m

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
        print(f"Posted match: {home} vs {away}")

# ----------------------
# Score finished matches (per server)
# ----------------------
@tasks.loop(minutes=5)
async def score_matches():
    matches = fetch_upcoming_matches(days_ahead=3)
    if not matches:
        return

    now = datetime.now(timezone.utc)

    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        match_id = m["id"]
        result = get_match_result(m)
        if not result or match_id not in user_bets:
            continue

        for guild_id in server_points.keys():
            for user_id, bet in user_bets[match_id].items():
                if bet == result:
                    if guild_id not in server_points:
                        server_points[guild_id] = {}
                    server_points[guild_id][user_id] = server_points[guild_id].get(user_id, 0) + 1
        del user_bets[match_id]

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
