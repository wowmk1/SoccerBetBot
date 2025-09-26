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
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r", encoding="utf-8") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(leaderboard, f, ensure_ascii=False, indent=2)

BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

MATCH_CACHE = {}
VOTE_TRACKER = {}

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.user_votes = {}

    def get_button_style(self, user_id, prediction):
        if str(user_id) in self.user_votes and self.user_votes[str(user_id)] == prediction:
            if prediction == "HOME_TEAM":
                return discord.ButtonStyle.success
            elif prediction == "DRAW":
                return discord.ButtonStyle.secondary
            elif prediction == "AWAY_TEAM":
                return discord.ButtonStyle.danger
        return discord.ButtonStyle.primary

    async def record_vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}

        if str(self.match_id) in leaderboard[user_id]["predictions"]:
            previous_vote = leaderboard[user_id]["predictions"][str(self.match_id)]
            disabled_view = discord.ui.View()
            for child in self.children:
                label_key = child.label.replace(" ", "_").upper()
                button = discord.ui.Button(
                    label=child.label,
                    style=self.get_button_style(user_id, label_key),
                    disabled=True
                )
                disabled_view.add_item(button)

            await interaction.response.send_message(
                f"🔒 You've already voted: **{previous_vote}**",
                ephemeral=True,
                view=disabled_view
            )
            return

        self.user_votes[user_id] = prediction
        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()

        match = MATCH_CACHE.get(str(self.match_id))
        if match:
            kickoff = match["utcDate"]
            if str(self.match_id) not in VOTE_TRACKER:
                VOTE_TRACKER[str(self.match_id)] = {
                    "kickoff": kickoff,
                    "voters": set()
                }
            VOTE_TRACKER[str(self.match_id)]["voters"].add(user_id)

        disabled_view = discord.ui.View()
        for child in self.children:
            label_key = child.label.replace(" ", "_").upper()
            button = discord.ui.Button(
                label=child.label,
                style=self.get_button_style(user_id, label_key),
                disabled=True
            )
            disabled_view.add_item(button)

        await interaction.response.send_message(
            f"✅ You voted: **{prediction}**",
            ephemeral=True,
            view=disabled_view
        )

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "AWAY_TEAM")

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
                    for match in data.get("matches", []):
                        MATCH_CACHE[str(match["id"])] = match
                        matches.append(match)
    matches = [m for m in matches if datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")) > now]
    return matches

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=match['homeTeam'].get('crest'))
    await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== SCORING ====
def get_result(match):
    home = match["score"]["fullTime"]["home"]
    away = match["score"]["fullTime"]["away"]
    if home > away:
        return "HOME_TEAM"
    elif away > home:
        return "AWAY_TEAM"
    else:
        return "DRAW"

def update_leaderboard(match_id, result):
    for user_id, data in leaderboard.items():
        prediction = data["predictions"].get(match_id)
        if prediction and prediction == result:
            data["points"] += 3
    save_leaderboard()

# ==== TASKS ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

@tasks.loop(hours=3)
async def update_scores():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={yesterday.date()}&dateTo={now.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for match in data.get("matches", []):
                        if match["status"] == "FINISHED":
                            match_id = str(match["id"])
                            result = get_result(match)
                            update_leaderboard(match_id, result)

@tasks.loop(minutes=5)
async def send_reminders():
    now = datetime.now(timezone.utc)
    for match_id, info in list(VOTE_TRACKER.items()):
        kickoff = datetime.fromisoformat(info["kickoff"].replace("Z", "+00:00"))
        if 25 < (kickoff - now).total_seconds() / 60 < 35:
            for user_id in info["voters"]:
                user = await bot.fetch_user(int(user_id))
                if user:
                    await user.send(f"⏰ Reminder: Your prediction for match `{match_id}` kicks off in 30 minutes!")
            del VOTE_TRACKER[match_id]

@tasks.loop(hours=1)
async def weekly_summary():
    now = datetime.now()
    if now.weekday() == 6 and now.hour == 20:
        for user_id, data in leaderboard.items():
            user = await bot.fetch_user(int(user_id))
            correct = data["points"] // 3
            total = len(data["predictions"])
            await user.send(
                f"📅 Weekly Summary:\n"
                f"• Matches voted: {total}\n"
                f"• Correct predictions: {correct}\n"
                f"• Total points: {data['points']}"
            )

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction:
