import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timezone

# ------------------ CONFIG ------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([TOKEN, API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError(
        "Missing environment variables: DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID"
    )

LEAGUES = ["WC", "CL", "BL1", "DED", "PD", "FL1", "ELC", "PPL", "EC", "SA", "PL"]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
votes = {}  # match_id -> {"home": [], "away": [], "draw": []}

# ------------------ HELPERS ------------------
async def fetch_matches():
    matches = []
    now = datetime.now(timezone.utc).date()
    async with aiohttp.ClientSession() as session:
        for league in LEAGUES:
            url = f"https://api.football-data.org/v4/competitions/{league}/matches"
            headers = {"X-Auth-Token": API_KEY}
            params = {
                "dateFrom": str(now),
                "dateTo": str(now)
            }
            async with session.get(url, headers=headers, params=params) as r:
                data = await r.json()
                matches.extend(data.get("matches", []))
    return matches

def format_voters(user_ids):
    if not user_ids:
        return "No votes yet"
    return "\n".join(f"<@{uid}>" for uid in user_ids)

async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    match_id = match["id"]
    votes.setdefault(match_id, {"home": [], "away": [], "draw": []})

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=match['homeTeam'].get("crest"))
    embed.set_image(url=match['awayTeam'].get("crest"))
    embed.add_field(name="Home votes", value=format_voters(votes[match_id]["home"]), inline=True)
    embed.add_field(name="Away votes", value=format_voters(votes[match_id]["away"]), inline=True)
    embed.add_field(name="Draw votes", value=format_voters(votes[match_id]["draw"]), inline=True)

    class VoteButtons(discord.ui.View):
        def __init__(self, match_id):
            super().__init__(timeout=None)
            self.match_id = match_id

        @discord.ui.button(label="Home", style=discord.ButtonStyle.primary)
        async def vote_home(self, interaction: discord.Interaction, button: discord.ui.Button):
            user_id = interaction.user.id
            for key in ["home", "away", "draw"]:
                if user_id in votes[self.match_id][key]:
                    votes[self.match_id][key].remove(user_id)
            votes[self.match_id]["home"].append(user_id)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="Away", style=discord.ButtonStyle.danger)
        async def vote_away(self, interaction: discord.Interaction, button: discord.ui.Button):
            user_id = interaction.user.id
            for key in ["home", "away", "draw"]:
                if user_id in votes[self.match_id][key]:
                    votes[self.match_id][key].remove(user_id)
            votes[self.match_id]["away"].append(user_id)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="Draw", style=discord.ButtonStyle.secondary)
        async def vote_draw(self, interaction: discord.Interaction, button: discord.ui.Button):
            user_id = interaction.user.id
            for key in ["home", "away", "draw"]:
                if user_id in votes[self.match_id][key]:
                    votes[self.match_id][key].remove(user_id)
            votes[self.match_id]["draw"].append(user_id)
            await interaction.response.edit_message(embed=embed, view=self)

    await channel.send(embed=embed, view=VoteButtons(match_id))

# ------------------ TASKS ------------------
@tasks.loop(minutes=1)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

# ------------------ COMMANDS ------------------
@bot.tree.command(name="matches", description="Show today's matches")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    msg = "\n".join(f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}" for m in matches)
    await interaction.response.send_message(msg or "No upcoming matches.")

@bot.tree.command(name="refresh_leaderboard", description="Refresh leaderboard manually (admin only)")
async def refresh_leaderboard(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You are not allowed to do this.", ephemeral=True)
        return
    # Placeholder for leaderboard logic
    await interaction.response.send_message("Leaderboard refreshed!", ephemeral=True)

# ------------------ BOT EVENTS ------------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    auto_post_matches.start()

# ------------------ RUN ------------------
bot.run(TOKEN)
