import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
from PIL import Image
from io import BytesIO

# ==== ENVIRONMENT VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing one or more environment variables.")

# ==== BOT SETUP ====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Leaderboard persistence
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

# Track posted match IDs to prevent reposts
posted_matches_file = "posted_matches.json"
if os.path.exists(posted_matches_file):
    with open(posted_matches_file, "r") as f:
        posted_matches = set(json.load(f))
else:
    posted_matches = set()

def save_posted_matches():
    with open(posted_matches_file, "w") as f:
        json.dump(list(posted_matches), f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

# ==== SAVE LEADERBOARD ====
def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== MATCH BUTTONS WITH PER-USER STATE ====
class MatchView(discord.ui.View):
    def __init__(self, match_id, user_votes):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.user_votes = user_votes  # dict of {user_id: prediction}

    def get_button_style(self, user_id, prediction):
        voted = self.user_votes.get(str(user_id))
        if voted is None:
            return discord.ButtonStyle.secondary
        elif voted == prediction:
            return discord.ButtonStyle.success
        else:
            return discord.ButtonStyle.gray

    def is_disabled(self, user_id):
        return str(user_id) in self.user_votes

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.secondary)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "AWAY_TEAM")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only disable buttons for the user who already voted
        for child in self.children:
            child.disabled = self.is_disabled(interaction.user.id)
            child.style = self.get_button_style(interaction.user.id, child.label.replace(" ", "_").upper())
        await interaction.response.edit_message(view=self)
        return True

# ==== RECORD PREDICTIONS ====
async def record_prediction(interaction, match_id, prediction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
    leaderboard[user_id]["predictions"][str(match_id)] = prediction
    save_leaderboard()
    # Update buttons for this user only
    await interaction.response.send_message(f"✅ You voted: **{prediction}**", ephemeral=True)

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
                        # Skip already started or finished matches
                        match_time = datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00"))
                        if match_time < now:
                            continue
                        # Skip if already posted
                        if m["id"] in posted_matches:
                            continue
                        m["competition"]["name"] = data.get("competition", {}).get("name", comp)
                        matches.append(m)
    return matches

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    # Compose embed
    embed = discord.Embed(
        title=f"{match['competition']['name']}",
        color=discord.Color.blue()
    )
    embed.add_field(name="Match", value=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}", inline=False)
    embed.add_field(name="Kickoff", value=f"{match['utcDate']}", inline=False)

    # Add crests as inline images (home left, away right)
    home_url = match['homeTeam'].get("crest")
    away_url = match['awayTeam'].get("crest")

    # Optionally resize images if needed or just show links
    if home_url or away_url:
        desc = ""
        if home_url:
            desc += f"[🏠]({home_url}) "
        if away_url:
            desc += f"[🏁]({away_url})"
        embed.set_image(url=home_url or away_url)

    # Track posted match
    posted_matches.add(match["id"])
    save_posted_matches()

    # Prepare per-user view
    user_votes = {}  # initially no votes
    await channel.send(embed=embed, view=MatchView(match["id"], user_votes))

# ==== BACKGROUND AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return
    for match in matches:
        await post_match(match)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return
    for match in matches[:5]:
        await post_match(match)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(
        leaderboard.values(),
        key=lambda x: (-x["points"], x["name"].lower())
    )
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
