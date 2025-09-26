import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
import discord
from discord.ext import commands, tasks
from discord import app_commands, File

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

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "SA", "EC", "WC"]

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match, user_id=None):
        super().__init__(timeout=None)
        self.match = match
        self.user_id = user_id

        # Disable buttons if this user already voted
        if user_id and str(match["id"]) in leaderboard.get(str(user_id), {}).get("predictions", {}):
            for item in self.children:
                item.disabled = True
                item.label = "Voted!"

    async def handle_vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        now = datetime.now(timezone.utc)
        match_time = datetime.fromisoformat(self.match["utcDate"].replace("Z", "+00:00"))

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
                    # Filter only upcoming matches
                    for m in data.get("matches", []):
                        match_time = datetime.fromisoformat(m["utcDate"].replace("Z","+00:00"))
                        if match_time > now:
                            matches.append(m)
    return matches

# ==== POST MATCH WITH CRESTS ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    async with aiohttp.ClientSession() as session:
        # Fetch club crests
        home_img_data = await fetch_image(session, match["homeTeam"].get("crest"))
        away_img_data = await fetch_image(session, match["awayTeam"].get("crest"))

    # Combine images side by side
    combined_file = combine_images(home_img_data, away_img_data)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )

    await channel.send(embed=embed, file=combined_file, view=MatchView(match))

async def fetch_image(session, url):
    if not url:
        return None
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return Image.open(BytesIO(await r.read())).convert("RGBA")
    except:
        return None
    return None

def combine_images(home_img, away_img):
    # Default size
    size = (128, 128)
    new_home = home_img.resize(size) if home_img else Image.new("RGBA", size, (200,200,200,255))
    new_away = away_img.resize(size) if away_img else Image.new("RGBA", size, (200,200,200,255))

    combined = Image.new("RGBA", (size[0]*2+20, size[1]), (255,255,255,0))
    combined.paste(new_home, (0,0), new_home)
    combined.paste(new_away, (size[0]+20,0), new_away)

    buffer = BytesIO()
    combined.save(buffer, format="PNG")
    buffer.seek(0)
    return File(fp=buffer, filename="match.png")

# ==== BACKGROUND AUTO POST ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
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

    # Sort by points, then alphabetically if tied
    sorted_lb = sorted(leaderboard.values(), key=lambda x: (-x["points"], x["name"]))
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
