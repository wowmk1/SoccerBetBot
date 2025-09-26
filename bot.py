import os
import asyncio
import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiohttp
from datetime import datetime, timezone

# ------------------ CONFIG ------------------
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID", 0))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID", 0))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing environment variables.")

# ------------------ SETUP ------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

posted_matches = set()
votes = {}  # { match_id: { "home": set(), "draw": set(), "away": set() } }
leaderboard = {}  # { user_id: points }

# Example club icons, fill URLs as needed
DEFAULT_CLUB_ICON = "https://example.com/default.png"
CLUB_ICONS = {
    "Manchester United": "https://example.com/manu.png",
    "Liverpool": "https://example.com/liverpool.png",
    # Add other clubs
}

# ------------------ UTILITIES ------------------
def format_voter_grid(user_ids):
    return "\n".join(f"<@{uid}>" for uid in user_ids) or "No votes yet"

async def fetch_matches():
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    now = datetime.now(timezone.utc)
    params = {
        "dateFrom": now.date().isoformat(),
        "dateTo": now.date().isoformat(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as r:
            data = await r.json()
            return data.get("matches", [])

async def post_match(match):
    match_id = match["id"]
    if match_id in posted_matches:
        return

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    home_team = match["homeTeam"]["name"]
    away_team = match["awayTeam"]["name"]
    home_icon = CLUB_ICONS.get(home_team, DEFAULT_CLUB_ICON)
    away_icon = CLUB_ICONS.get(away_team, DEFAULT_CLUB_ICON)

    # Initialize votes for this match
    votes.setdefault(match_id, {"home": set(), "draw": set(), "away": set()})

    embed = discord.Embed(
        title=f"{home_team} vs {away_team}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=home_icon)
    embed.set_image(url=away_icon)

    embed.add_field(name="Home votes", value=format_voter_grid(votes[match_id]["home"]), inline=True)
    embed.add_field(name="Draw votes", value=format_voter_grid(votes[match_id]["draw"]), inline=True)
    embed.add_field(name="Away votes", value=format_voter_grid(votes[match_id]["away"]), inline=True)

    class VoteView(discord.ui.View):
        def __init__(self):
            super().__init__()
        
        async def handle_vote(self, interaction, option):
            user_id = interaction.user.id
            # Prevent voting after match started? (example)
            # votes[match_id][option].add(user_id)
            # Remove from other options
            for opt in ["home", "draw", "away"]:
                votes[match_id][opt].discard(user_id)
            votes[match_id][option].add(user_id)
            await interaction.response.send_message(f"You voted {option}!", ephemeral=True)

        @discord.ui.button(label="Home", style=discord.ButtonStyle.green)
        async def home_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.handle_vote(interaction, "home")

        @discord.ui.button(label="Draw", style=discord.ButtonStyle.gray)
        async def draw_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.handle_vote(interaction, "draw")

        @discord.ui.button(label="Away", style=discord.ButtonStyle.red)
        async def away_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self.handle_vote(interaction, "away")

    await channel.send(embed=embed, view=VoteView())
    posted_matches.add(match_id)

# ------------------ AUTO POST TASK ------------------
@tasks.loop(minutes=1)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

# ------------------ LEADERBOARD ------------------
async def update_leaderboard():
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title="Leaderboard", color=discord.Color.gold())
    if leaderboard:
        for uid, pts in sorted(leaderboard.items(), key=lambda x: x[1], reverse=True):
            embed.add_field(name=f"<@{uid}>", value=f"{pts} points", inline=False)
    else:
        embed.description = "No points yet."
    await channel.send(embed=embed)

# ------------------ ADMIN BUTTON ------------------
class LeaderboardRestartView(discord.ui.View):
    @discord.ui.button(label="Restart Leaderboard", style=discord.ButtonStyle.red)
    async def restart_leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only admins can restart.", ephemeral=True)
            return
        leaderboard.clear()
        posted_matches.clear()
        await interaction.response.send_message("Leaderboard reset!", ephemeral=True)
        await update_leaderboard()

# ------------------ BOT COMMANDS ------------------
@tree.command(name="matches", description="Show upcoming matches")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.")
        return
    msg = "\n".join(f"{m['homeTeam']['name']} vs {m['awayTeam']['name']} at {m['utcDate']}" for m in matches)
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="leaderboard", description="Show leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    await update_leaderboard()
    await interaction.response.send_message("Leaderboard posted!", ephemeral=True)

# ------------------ EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    auto_post_matches.start()
    await tree.sync()

# ------------------ RUN ------------------
bot.run(DISCORD_BOT_TOKEN)
