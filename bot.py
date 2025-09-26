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
intents.message_content = True
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

# ==== MATCH BUTTONS ====
class MatchView(discord.ui.View):
    def __init__(self, match_id, user_votes=None):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.user_votes = user_votes or {}

        # Disable buttons for users who already voted
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.custom_id = f"{child.label}_{match_id}"

    async def disable_for_user(self, user_id):
        # Only disables for that user
        self.user_votes[str(user_id)] = True
        # Buttons themselves stay clickable for others

    @discord.ui.button(label="Home Win", style=discord.ButtonStyle.primary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "HOME_TEAM")
        await self.disable_for_user(interaction.user.id)
        await update_buttons(interaction, self)

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
    async def draw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "DRAW")
        await self.disable_for_user(interaction.user.id)
        await update_buttons(interaction, self)

    @discord.ui.button(label="Away Win", style=discord.ButtonStyle.danger)
    async def away(self, interaction: discord.Interaction, button: discord.ui.Button):
        await record_prediction(interaction, self.match_id, "AWAY_TEAM")
        await self.disable_for_user(interaction.user.id)
        await update_buttons(interaction, self)

async def update_buttons(interaction, view):
    # Disable buttons for this user visually (others can still click)
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            if str(interaction.user.id) in view.user_votes:
                child.disabled = True
                child.style = discord.ButtonStyle.gray
    await interaction.response.edit_message(view=view)
    # Send ephemeral message
    await interaction.followup.send(f"✅ You voted!", ephemeral=True)

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
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("matches", []):
                        if m['status'] == "SCHEDULED":
                            m["competition"] = comp
                            matches.append(m)
    return matches

# ==== CREATE COMBINED CREST IMAGE ====
async def combine_crests(home_url, away_url, size=(80, 80)):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(home_url) as r:
                home_bytes = await r.read()
            async with session.get(away_url) as r:
                away_bytes = await r.read()

            home_img = Image.open(BytesIO(home_bytes)).convert("RGBA").resize(size)
            away_img = Image.open(BytesIO(away_bytes)).convert("RGBA").resize(size)

            combined = Image.new("RGBA", (size[0]*2 + 20, size[1]), (255,255,255,0))
            combined.paste(home_img, (0,0), home_img)
            combined.paste(away_img, (size[0]+20,0), away_img)

            output = BytesIO()
            combined.save(output, format="PNG")
            output.seek(0)
            return output
        except:
            return None

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

    # Fetch combined crest image
    image = await combine_crests(match['homeTeam'].get("crest", ""), match['awayTeam'].get("crest", ""))
    if image:
        file = discord.File(fp=image, filename="match.png")
        embed.set_image(url="attachment://match.png")
        await channel.send(embed=embed, file=file, view=MatchView(match["id"]))
    else:
        await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== AUTO POST LOOP ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return
    leagues = {}
    for match in matches:
        comp = match['competition']
        if comp not in leagues:
            leagues[comp] = []
        leagues[comp].append(match)

    for comp, comp_matches in leagues.items():
        header = discord.Embed(title=f"🏆 {comp} Matches", color=discord.Color.green())
        await bot.get_channel(MATCH_CHANNEL_ID).send(embed=header)
        for match in comp_matches:
            await post_match(match)

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    leagues = {}
    for match in matches:
        comp = match['competition']
        if comp not in leagues:
            leagues[comp] = []
        leagues[comp].append(match)

    for comp, comp_matches in leagues.items():
        header = discord.Embed(title=f"🏆 {comp} Matches", color=discord.Color.green())
        await interaction.channel.send(embed=header)
        for match in comp_matches[:5]:
            await post_match(match)
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
