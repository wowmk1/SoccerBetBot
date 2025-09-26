import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
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
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== LEADERBOARD PERSISTENCE ====
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]
LEAGUE_NAMES = {
    "PL": "Premier League",
    "CL": "UEFA Champions League",
    "BL1": "Bundesliga",
    "DED": "Eredivisie",
    "PD": "Primera Division",
    "FL1": "Ligue 1",
    "ELC": "Championship",
    "PPL": "Primeira Liga",
    "EC": "European Championship",
    "SA": "Serie A",
    "WC": "FIFA World Cup"
}

# ==== MATCH VIEW BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    async def disable_buttons(self, interaction):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "HOME_TEAM")
        await self.disable_buttons(interaction)

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "DRAW")
        await self.disable_buttons(interaction)

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "AWAY_TEAM")
        await self.disable_buttons(interaction)

# ==== LEADERBOARD RESET ====
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

# ==== RECORD PREDICTIONS ====
async def record_prediction(interaction, match_id, prediction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
    leaderboard[user_id]["predictions"][str(match_id)] = prediction
    save_leaderboard()
    await interaction.response.send_message("✅ You voted!", ephemeral=True)

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
                        m["league"] = LEAGUE_NAMES.get(comp, comp)
                        matches.append(m)
    return matches

# ==== CREATE MATCH IMAGE WITH CRESTS ====
async def create_match_image(home_crest, away_crest, user_icons=[]):
    # Load crests
    async with aiohttp.ClientSession() as session:
        async with session.get(home_crest) as r:
            home_img = Image.open(BytesIO(await r.read())).convert("RGBA").resize((100,100))
        async with session.get(away_crest) as r:
            away_img = Image.open(BytesIO(await r.read())).convert("RGBA").resize((100,100))

    # Create base image
    width = 300
    height = 100 + (20 if user_icons else 0)
    img = Image.new("RGBA", (width, height), (255,255,255,0))
    img.paste(home_img, (0,0), home_img)
    img.paste(away_img, (width-100,0), away_img)

    # Draw user avatars below if any
    if user_icons:
        x = 0
        for avatar in user_icons[:width//20]:
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar) as r:
                    a_img = Image.open(BytesIO(await r.read())).convert("RGBA").resize((20,20))
                    img.paste(a_img, (x, 100), a_img)
                    x += 25

    output = BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return discord.File(fp=output, filename="match.png")

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    file = None
    home_crest = match['homeTeam'].get('crest') or ""
    away_crest = match['awayTeam'].get('crest') or ""
    if home_crest and away_crest:
        file = await create_match_image(home_crest, away_crest)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )

    await channel.send(embed=embed, view=MatchView(match["id"]), file=file)

# ==== BACKGROUND AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return
    # Group by league
    leagues = {}
    for match in matches:
        leagues.setdefault(match["league"], []).append(match)

    for league, league_matches in leagues.items():
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        await channel.send(f"🏆 **{league} Matches**")
        for m in league_matches:
            await post_match(m)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    leagues = {}
    for match in matches[:10]:
        leagues.setdefault(match["league"], []).append(match)

    for league, league_matches in leagues.items():
        await interaction.channel.send(f"🏆 **{league} Matches**")
        for m in league_matches:
            await post_match(m)

    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        # Show users who voted even if 0 points
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(
        leaderboard.values(),
        key=lambda x: (-x["points"], x["name"])  # points desc, alphabetical
    )
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts" for i, entry in enumerate(sorted_lb[:10])])

    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
