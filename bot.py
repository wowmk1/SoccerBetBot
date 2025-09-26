import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
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
    def __init__(self, match):
        super().__init__(timeout=None)
        self.match = match

    async def handle_vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        now = datetime.now(timezone.utc)
        match_time = datetime.fromisoformat(self.match["utcDate"].replace("Z","+00:00"))

        if match_time <= now:
            await interaction.response.send_message("⚠️ This match has already started. Voting is closed.", ephemeral=True)
            return

        if user_id not in leaderboard:
            leaderboard[user_id] = {
                "name": interaction.user.name,
                "avatar": str(interaction.user.display_avatar.url),
                "points": 0,
                "predictions": {}
            }

        if str(self.match["id"]) in leaderboard[user_id]["predictions"]:
            await interaction.response.send_message("✅ You already voted for this match.", ephemeral=True)
            return

        # Save prediction
        leaderboard[user_id]["predictions"][str(self.match["id"])] = prediction
        leaderboard[user_id]["avatar"] = str(interaction.user.display_avatar.url)
        save_leaderboard()

        # Disable buttons for this user only
        for item in self.children:
            if hasattr(item, "label"):
                item.disabled = True
                item.label = "Voted!" if item.label == prediction else item.label

        await interaction.response.send_message(f"✅ Voted: **{prediction}**", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "AWAY_TEAM")

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
        await interaction.response.send_message(
            "⚠️ Are you sure you want to reset the leaderboard?", 
            view=LeaderboardResetConfirm(), ephemeral=True
        )

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
                        # Skip past or finished games
                        match_time = datetime.fromisoformat(m["utcDate"].replace("Z","+00:00"))
                        if match_time >= now:
                            matches.append(m)
    return matches

# ==== CREATE MATCH IMAGE ====
async def create_match_image(match):
    width, height = 400, 200
    home_url = match["homeTeam"].get("crest", "")
    away_url = match["awayTeam"].get("crest", "")
    home_img, away_img = None, None

    async with aiohttp.ClientSession() as session:
        if home_url:
            try:
                async with session.get(home_url) as r:
                    data = await r.read()
                    home_img = Image.open(BytesIO(data)).convert("RGBA").resize((120, 120))
            except: pass
        if away_url:
            try:
                async with session.get(away_url) as r:
                    data = await r.read()
                    away_img = Image.open(BytesIO(data)).convert("RGBA").resize((120, 120))
            except: pass

    canvas = Image.new("RGBA", (width, height), (255,255,255,0))
    if home_img: canvas.paste(home_img, (20, 40), home_img)
    if away_img: canvas.paste(away_img, (260, 40), away_img)
    bio = BytesIO()
    canvas.save(bio, "PNG")
    bio.seek(0)
    return bio

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

    # Attach image
    img_bytes = await create_match_image(match)
    file = discord.File(img_bytes, filename="match.png")
    embed.set_image(url="attachment://match.png")

    await channel.send(embed=embed, file=file, view=MatchView(match))

# ==== AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return

    # Group by league
    leagues = {}
    for match in matches:
        comp = match["competition"]["name"]
        if comp not in leagues:
            leagues[comp] = []
        leagues[comp].append(match)

    for league_name, league_matches in leagues.items():
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if channel:
            await channel.send(f"🏆 **{league_name} Matches**")
            for m in league_matches:
                await post_match(m)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    # Group by league
    leagues = {}
    for match in matches:
        comp = match["competition"]["name"]
        if comp not in leagues:
            leagues[comp] = []
        leagues[comp].append(match)

    for league_name, league_matches in leagues.items():
        await interaction.channel.send(f"🏆 **{league_name} Matches**")
        for m in league_matches[:5]:
            await post_match(m)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    # Sort: points descending, if tie sort alphabetically
    sorted_lb = sorted(
        leaderboard.values(),
        key=lambda x: (-x["points"], x["name"])
    )
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts"
                      for i, entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
