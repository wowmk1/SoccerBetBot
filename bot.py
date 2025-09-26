import os
import json
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

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
        self.user_votes = {}  # user_id -> prediction

    async def handle_vote(self, interaction: discord.Interaction, prediction: str):
        user_id = str(interaction.user.id)
        # record vote
        if user_id not in self.user_votes:
            self.user_votes[user_id] = prediction

        await record_prediction(interaction, self.match_id, prediction)

        # update button colors for this user only
        for button in self.children:
            label_key = button.label.replace(" ", "_").upper()
            if label_key in ["HOME_WIN", "HOME_TEAM"]:
                button.style = discord.ButtonStyle.success if self.user_votes[user_id] == "HOME_TEAM" else discord.ButtonStyle.primary
            elif label_key == "DRAW":
                button.style = discord.ButtonStyle.secondary if self.user_votes[user_id] == "DRAW" else discord.ButtonStyle.secondary
            elif label_key in ["AWAY_WIN", "AWAY_TEAM"]:
                button.style = discord.ButtonStyle.danger if self.user_votes[user_id] == "AWAY_TEAM" else discord.ButtonStyle.primary
            # keep enabled for others

        # acknowledge vote
        await interaction.response.send_message(f"✅ You voted: **{prediction}**", ephemeral=True)
        # edit original message to show updated buttons
        await interaction.message.edit(view=self)

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
        await interaction.response.send_message(
            "⚠️ Are you sure you want to reset the leaderboard?",
            view=LeaderboardResetConfirm(), ephemeral=True
        )

# ==== RECORD PREDICTIONS ====
async def record_prediction(interaction, match_id, prediction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": interaction.user.name, "points": 0, "predictions": {}}
    leaderboard[user_id]["predictions"][str(match_id)] = prediction
    save_leaderboard()

# ==== FETCH MATCHES ====
async def fetch_matches():
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={tomorrow.date()}"
            async with session.get(url, headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    matches.extend(data.get("matches", []))
    # Only future matches
    matches = [m for m in matches if datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")) > now]
    return matches

# ==== POST MATCH WITH CRESTS ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    # Compose image with home/away crests side by side
    home_crest_url = match["homeTeam"].get("crest")
    away_crest_url = match["awayTeam"].get("crest")
    try:
        async with aiohttp.ClientSession() as session:
            home_img = away_img = None
            if home_crest_url:
                async with session.get(home_crest_url) as r:
                    home_img = Image.open(BytesIO(await r.read())).convert("RGBA").resize((80, 80))
            if away_crest_url:
                async with session.get(away_crest_url) as r:
                    away_img = Image.open(BytesIO(await r.read())).convert("RGBA").resize((80, 80))
            combined = Image.new("RGBA", (200, 80), (255,255,255,0))
            if home_img: combined.paste(home_img, (0,0), home_img)
            if away_img: combined.paste(away_img, (120,0), away_img)
            buf = BytesIO()
            combined.save(buf, format="PNG")
            buf.seek(0)
            file = discord.File(buf, filename="match.png")
    except:
        file = None

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    if file:
        embed.set_image(url="attachment://match.png")
        await channel.send(embed=embed, file=file, view=MatchView(match["id"]))
    else:
        await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== BACKGROUND AUTO POST ====
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
    for league, league_matches in leagues.items():
        embed = discord.Embed(title=f"🏆 {league} Matches", color=discord.Color.dark_blue())
        await channel.send(embed=embed)
        for match in league_matches:
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
    await interaction.response.send_message(embed=embed, view=LeaderboardView())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
