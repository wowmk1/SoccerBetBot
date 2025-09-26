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
intents.members = True  # for user avatars
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
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

# ==== SAVE LEADERBOARD ====
def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def handle_vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}

        if str(self.match_id) in leaderboard[user_id]["predictions"]:
            await interaction.response.send_message("⚠️ You already voted for this match.", ephemeral=True)
            return

        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()
        await interaction.response.send_message(f"✅ Voted for **{prediction}**!", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "AWAY_TEAM")

# ==== LEADERBOARD RESET VIEW ====
class LeaderboardResetConfirm(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="✅ Confirm Reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)
            return

        global leaderboard
        leaderboard = {}
        save_leaderboard()
        await interaction.response.send_message("✅ Leaderboard has been reset!", ephemeral=True)

        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel:
            await channel.send("🔄 The leaderboard has been reset by an admin.")

        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Reset cancelled.", ephemeral=True)
        self.stop()

class LeaderboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reset Leaderboard", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("🚫 You don’t have permission.", ephemeral=True)
            return
        await interaction.response.send_message("⚠️ Are you sure you want to reset the leaderboard?", 
                                                view=LeaderboardResetConfirm(), ephemeral=True)

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
                        match["competition_code"] = comp
                        matches.append(match)
    return matches

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    # Fetch home/away crest images
    async with aiohttp.ClientSession() as session:
        home_crest_url = match['homeTeam'].get('crest') or ""
        away_crest_url = match['awayTeam'].get('crest') or ""
        home_image, away_image = None, None
        if home_crest_url:
            async with session.get(home_crest_url) as r:
                if r.status == 200:
                    home_image = Image.open(BytesIO(await r.read())).resize((64,64))
        if away_crest_url:
            async with session.get(away_crest_url) as r:
                if r.status == 200:
                    away_image = Image.open(BytesIO(await r.read())).resize((64,64))

        # Combine images side by side
        if home_image or away_image:
            combined = Image.new("RGBA", (128,64), (255,255,255,0))
            if home_image:
                combined.paste(home_image, (0,0))
            if away_image:
                combined.paste(away_image, (64,0))
            buffer = BytesIO()
            combined.save(buffer, format="PNG")
            buffer.seek(0)
            file = discord.File(fp=buffer, filename="match.png")
        else:
            file = None

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    await channel.send(embed=embed, file=file, view=MatchView(match["id"]))

# ==== BACKGROUND AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return

    # Group by league
    league_matches = {}
    for m in matches:
        league_matches.setdefault(m["competition_code"], []).append(m)

    for league, matches_list in league_matches.items():
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if channel:
            await channel.send(f"🏆 **{league} Matches**")
            for match in matches_list:
                await post_match(match)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    league_matches = {}
    for m in matches[:10]:
        league_matches.setdefault(m["competition_code"], []).append(m)

    for league, matches_list in league_matches.items():
        await interaction.channel.send(f"🏆 **{league} Matches**")
        for match in matches_list:
            await post_match(match)

    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        # If empty, show users who voted
        sorted_lb = sorted([(uid, data) for uid,data in leaderboard.items()], key=lambda x: x[1]["name"])
    else:
        # Sort by points, then alphabetically
        sorted_lb = sorted(leaderboard.items(), key=lambda x: (-x[1]["points"], x[1]["name"]))

    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts"
                      for i, (uid, entry) in enumerate(sorted_lb[:10])])

    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
