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

    async def record_vote(self, interaction: discord.Interaction, prediction):
        user_id = str(interaction.user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()

        # Ephemeral confirmation with user's avatar under match
        embed = discord.Embed(
            title=f"You voted for {interaction.user.name}",
            description=f"✅ Prediction saved: **{prediction}**",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "AWAY_TEAM")

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
                        m["competition"]["name"] = data.get("competition", {}).get("name", comp)
                        matches.append(m)
    return matches

# ==== CREATE MATCH IMAGE WITH CRESTS ====
async def generate_match_image(home_url, away_url):
    async with aiohttp.ClientSession() as session:
        home_img_bytes, away_img_bytes = None, None
        try:
            async with session.get(home_url) as r:
                home_img_bytes = await r.read()
        except: pass
        try:
            async with session.get(away_url) as r:
                away_img_bytes = await r.read()
        except: pass

    size = (100, 100)
    img = Image.new("RGBA", (size[0]*2 + 40, size[1]), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)

    if home_img_bytes:
        home = Image.open(BytesIO(home_img_bytes)).convert("RGBA").resize(size)
        img.paste(home, (0, 0), home)
    if away_img_bytes:
        away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0]+40, 0), away)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")

    if home_crest or away_crest:
        image_buffer = await generate_match_image(home_crest, away_crest)
        file = discord.File(fp=image_buffer, filename="match.png")
        embed = discord.Embed(
            title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            description=f"Kickoff: {match['utcDate']}",
            color=discord.Color.blue()
        )
        embed.set_image(url="attachment://match.png")
        await channel.send(embed=embed, view=MatchView(match["id"]), file=file)
    else:
        embed = discord.Embed(
            title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            description=f"Kickoff: {match['utcDate']}",
            color=discord.Color.blue()
        )
        await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== AUTO POST MATCHES BY LEAGUE ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return

    # Group by league
    leagues = {}
    for m in matches:
        league_name = m["competition"]["name"]
        leagues.setdefault(league_name, []).append(m)

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    for league, league_matches in leagues.items():
        embed = discord.Embed(title=f"🏆 {league} Matches", color=discord.Color.purple())
        embed.description = "\n\n".join([f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}\nKickoff: {m['utcDate']}" for m in league_matches])
        await channel.send(embed=embed)
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
    for m in matches[:10]:
        league_name = m["competition"]["name"]
        leagues.setdefault(league_name, []).append(m)

    for league, league_matches in leagues.items():
        embed = discord.Embed(title=f"🏆 {league} Matches", color=discord.Color.green())
        embed.description = "\n\n".join([f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}\nKickoff: {m['utcDate']}" for m in league_matches])
        await interaction.channel.send(embed=embed)
        for m in league_matches:
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
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
