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

# Only include desired leagues
COMPETITIONS = ["PL", "CL", "BL1", "PD", "FL1", "SA", "EC", "WC"]  # Excluded DED, ELC, PPL

# ==== SAVE LEADERBOARD ====
def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== GENERATE MATCH IMAGE WITH CRESTS AND VOTERS ====
async def generate_match_image(home_url, away_url, match_id):
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
    padding = 10
    avatar_size = 30

    # Base image for match crests
    img_width = size[0]*2 + 40
    img_height = size[1] + avatar_size + padding*2
    img = Image.new("RGBA", (img_width, img_height), (255, 255, 255, 0))

    # Draw home and away crests
    if home_img_bytes:
        home = Image.open(BytesIO(home_img_bytes)).convert("RGBA").resize(size)
        img.paste(home, (0, 0), home)
    if away_img_bytes:
        away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0]+40, 0), away)

    # Draw voter avatars below
    voter_urls = [v["avatar"] for uid, v in leaderboard.items() if str(match_id) in v.get("predictions", {})]
    x = 0
    y = size[1] + padding
    for i, url in enumerate(voter_urls):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as r:
                    avatar_bytes = await r.read()
            avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((avatar_size, avatar_size))
            img.paste(avatar, (x, y), avatar)
            x += avatar_size + 5
        except: continue

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id, message: discord.Message, home_crest, away_crest):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.message = message
        self.home_crest = home_crest
        self.away_crest = away_crest

    async def record_vote(self, interaction: discord.Interaction, prediction):
        user_id = str(interaction.user.id)

        # One vote per user per match
        if user_id in leaderboard and str(self.match_id) in leaderboard[user_id].get("predictions", {}):
            await interaction.response.send_message("❌ You already voted for this match.", ephemeral=True)
            return

        # Save vote
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}, "avatar": interaction.user.display_avatar.url}
        else:
            leaderboard[user_id]["avatar"] = interaction.user.display_avatar.url
        leaderboard[user_id]["predictions"][str(self.match_id)] = prediction
        save_leaderboard()

        # Regenerate image with all voter avatars
        image_buffer = await generate_match_image(self.home_crest, self.away_crest, self.match_id)
        file = discord.File(fp=image_buffer, filename="match.png")
        embed = self.message.embeds[0]
        embed.set_image(url="attachment://match.png")
        await self.message.edit(embed=embed)  # <-- Fixed: remove file here

        await interaction.response.send_message(f"✅ Prediction saved: **{prediction}**", ephemeral=True)

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "HOME_TEAM")

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "DRAW")

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_vote(interaction, "AWAY_TEAM")

# ==== POST MATCH ====
async def post_match(match):
    # Skip finished/past games
    match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    if match_time < datetime.now(timezone.utc):
        return

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )

    image_buffer = await generate_match_image(home_crest, away_crest, match["id"])
    file = discord.File(fp=image_buffer, filename="match.png")
    embed.set_image(url="attachment://match.png")

    # Send message with file and view
    msg = await channel.send(embed=embed, file=file)
    view = MatchView(match["id"], msg, home_crest, away_crest)
    await msg.edit(view=view)  # <-- Fixed: only edit view here

# ==== AUTO POST MATCHES ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return
    for m in matches:
        await post_match(m)

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
    # Filter only upcoming matches
    upcoming = [m for m in matches if datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) > datetime.now(timezone.utc)]
    return upcoming

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return
    for m in matches[:10]:
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
    await interaction.response.send_message(embed=embed)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
