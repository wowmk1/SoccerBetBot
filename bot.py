import os
import json
import aiohttp
import asyncio
import requests
from io import BytesIO
from PIL import Image
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

# ==== CREST COMBINER ====
def combine_crests(home_url, away_url, output_path="combined.png", size=(128, 128)):
    try:
        home_img = Image.open(BytesIO(requests.get(home_url).content)).convert("RGBA")
        away_img = Image.open(BytesIO(requests.get(away_url).content)).convert("RGBA")
        home_img = home_img.resize(size, Image.LANCZOS)
        away_img = away_img.resize(size, Image.LANCZOS)
        combined = Image.new("RGBA", (size[0]*2, size[1]), (255, 255, 255, 0))
        combined.paste(home_img, (0, 0))
        combined.paste(away_img, (size[0], 0))
        combined.save(output_path)
        return output_path
    except Exception as e:
        print(f"Error combining crests: {e}")
        return None

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

        already_voted = str(self.match_id) in leaderboard[user_id]["predictions"]
        if already_voted:
            prediction = leaderboard[user_id]["predictions"][str(self.match_id)]

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
            f"{'🔒 Already voted:' if already_voted else '✅ You voted:'} **{prediction}**",
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

# ==== MATCH FETCHING ====
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

async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    crest_path = combine_crests(match['homeTeam'].get('crest'), match['awayTeam'].get('crest'))
    if crest_path:
        file = discord.File(crest_path, filename="crests.png")
        embed.set_image(url="attachment://crests.png")
        await channel.send(embed=embed, view=MatchView(match["id"]), file=file)
    else:
        embed.set_thumbnail(url=match['homeTeam'].get('crest'))
        embed.set_image(url=match['awayTeam'].get('crest'))
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
        if 25 < (kickoff - now).total
                if 25 < (kickoff - now).total_seconds() / 60 < 35:
            for user_id in info["voters"]:
                try:
                    user = await bot.fetch_user(int(user_id))
                    await user.send(f"⏰ Reminder: Your prediction for match `{match_id}` kicks off in 30 minutes!")
                except:
                    pass
            del VOTE_TRACKER[match_id]

@tasks.loop(hours=1)
async def weekly_summary():
    now = datetime.now()
    if now.weekday() == 6 and now.hour == 20:
        for user_id, data in leaderboard.items():
            try:
                user = await bot.fetch_user(int(user_id))
                correct = data["points"] // 3
                total = len(data["predictions"])
                await user.send(
                    f"📅 Weekly Summary:\n"
                    f"• Matches voted: {total}\n"
                    f"• Correct predictions: {correct}\n"
                    f"• Total points: {data['points']}"
                )
            except:
                pass

# ==== COMMANDS ====

@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    for match in matches[:5]:
        embed = discord.Embed(
            title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            description=f"Kickoff: {match['utcDate']}",
            color=discord.Color.green()
        )
        crest_path = combine_crests(match['homeTeam'].get('crest'), match['awayTeam'].get('crest'))
        if crest_path:
            file = discord.File(crest_path, filename="crests.png")
            embed.set_image(url="attachment://crests.png")
            await interaction.channel.send(embed=embed, view=MatchView(match["id"]), file=file)
        else:
            embed.set_thumbnail(url=match['homeTeam'].get('crest'))
            embed.set_image(url=match['awayTeam'].get('crest'))
            await interaction.channel.send(embed=embed, view=MatchView(match["id"]))

    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return

    sorted_lb = sorted(leaderboard.values(), key=lambda x: (-x["points"], x["name"].lower()))
    desc = "\n".join([
        f"**{i+1}. {entry['name']}** — {entry['points']} pts"
        for i, entry in enumerate(sorted_lb[:10])
    ])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="myvotes", description="See which matches you've voted on.")
async def myvotes_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard or not leaderboard[user_id]["predictions"]:
        await interaction.response.send_message("You haven't voted on any matches yet.", ephemeral=True)
        return

    predictions = leaderboard[user_id]["predictions"]
    embeds = []

    for match_id, prediction in list(predictions.items())[:5]:
        match = MATCH_CACHE.get(match_id)
        if match:
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            kickoff = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).strftime("%A, %d %b %Y %H:%M UTC")
            home_crest = match["homeTeam"].get("crest")
            away_crest = match["awayTeam"].get("crest")

            embed = discord.Embed(
                title=f"{home} vs {away}",
                description=f"🕒 Kickoff: {kickoff}\n🗳️ Your Prediction: **{prediction}**",
                color=discord.Color.purple()
            )
            if home_crest:
                embed.set_thumbnail(url=home_crest)
            if away_crest:
                embed.set_image(url=away_crest)
            embeds.append(embed)
        else:
            embed = discord.Embed(
                title=f"Match ID {match_id}",
                description=f"🗳️ Your Prediction: **{prediction}**",
                color=discord.Color.purple()
            )
            embeds.append(embed)

    for embed in embeds:
        await interaction.channel.send(embed=embed)
    await interaction.response.send_message("✅ Here's your recent votes!", ephemeral=True)

@bot.tree.command(name="stats", description="Show your prediction stats.")
async def stats_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard:
        await interaction.response.send_message("You haven't voted yet.", ephemeral=True)
        return

    data = leaderboard[user_id]
    total = len(data["predictions"])
    correct = data["points"] // 3
    embed = discord.Embed(
        title=f"📊 Stats for {data['name']}",
        description=f"• Total Votes: {total}\n• Correct Predictions: {correct}\n• Points: {data['points']}",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==== STARTUP ====

@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    update_scores.start()
    send_reminders.start()
    weekly_summary.start()
    print(f"✅ Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
