import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import requests
from datetime import datetime, timezone, timedelta

# ------------------ CONFIG ------------------
TOKEN = "YOUR_DISCORD_BOT_TOKEN"
API_KEY = "YOUR_FOOTBALL_DATA_API_KEY"
LEADERBOARD_CHANNEL_ID = YOUR_CHANNEL_ID  # Channel for leaderboard
POST_CHANNEL_ID = YOUR_CHANNEL_ID         # Channel to post matches
TRACKED_COMPETITIONS = ["PL", "PD", "SA", "CL", "EL", "WC"]  # League codes

# ------------------ GLOBALS ------------------
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
tree = bot.tree

posted_matches = set()
match_lookup = {}       # match_id -> match info
user_bets = {}          # match_id -> {user_id: choice}
server_points = {}      # guild_id -> {user_id: points}
leaderboard_message_id = None
match_messages = {}     # match_id -> message object for countdown

# ------------------ MATCH FETCHING ------------------
def fetch_upcoming_matches():
    url = "https://api.football-data.org/v4/matches"
    headers = {"X-Auth-Token": API_KEY}
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)
    params = {"dateFrom": str(today), "dateTo": str(tomorrow)}

    try:
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            print("API error:", resp.text[:500])
            return []
        data = resp.json()
        matches = [m for m in data.get("matches", []) if m["competition"]["code"] in TRACKED_COMPETITIONS]
        return matches
    except Exception as e:
        print("Error fetching matches:", e)
        return []

# ------------------ BETTING BUTTONS ------------------
class BetView(ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @ui.button(label="Home Win", style=discord.ButtonStyle.green)
    async def home_win(self, interaction: discord.Interaction, button: ui.Button):
        await handle_bet(interaction, self.match_id, "HOME")

    @ui.button(label="Draw", style=discord.ButtonStyle.gray)
    async def draw(self, interaction: discord.Interaction, button: ui.Button):
        await handle_bet(interaction, self.match_id, "DRAW")

    @ui.button(label="Away Win", style=discord.ButtonStyle.red)
    async def away_win(self, interaction: discord.Interaction, button: ui.Button):
        await handle_bet(interaction, self.match_id, "AWAY")

async def handle_bet(interaction, match_id, choice):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    match = match_lookup.get(match_id)
    if not match:
        await interaction.response.send_message("Match not found.", ephemeral=True)
        return

    match_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    if match_time <= now:
        await interaction.response.send_message("Cannot bet on started or finished match.", ephemeral=True)
        return

    if match_id not in user_bets:
        user_bets[match_id] = {}

    if interaction.user.id in user_bets[match_id]:
        await interaction.response.send_message("You already bet on this match.", ephemeral=True)
        return

    user_bets[match_id][interaction.user.id] = choice
    await interaction.response.send_message(f"Your bet ({choice}) has been placed!", ephemeral=True)

# ------------------ LEADERBOARD ------------------
async def get_leaderboard_embed(guild_id):
    points_dict = server_points.get(guild_id, {})
    if not points_dict:
        return discord.Embed(title="🏆 Server Leaderboard 🏆", description="No scores yet.", color=discord.Color.gold())

    sorted_points = sorted(points_dict.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (user_id, pts) in enumerate(sorted_points):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <@{user_id}> — **{pts} point{'s' if pts != 1 else ''}**")

    embed = discord.Embed(title="🏆 Server Leaderboard 🏆", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text="Keep betting to climb the ranks! ⚽")
    return embed

async def post_or_update_leaderboard(guild_id):
    global leaderboard_message_id
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    embed = await get_leaderboard_embed(guild_id)
    if leaderboard_message_id:
        try:
            msg = await channel.fetch_message(leaderboard_message_id)
            await msg.edit(embed=embed)
            return
        except:
            leaderboard_message_id = None

    msg = await channel.send(embed=embed)
    leaderboard_message_id = msg.id
    await msg.pin()

# ------------------ MATCH RESULTS ------------------
def get_match_result(match):
    score = match.get("score", {}).get("fullTime", {})
    home = score.get("home", 0)
    away = score.get("away", 0)
    if home > away:
        return "HOME"
    elif home < away:
        return "AWAY"
    return "DRAW"

async def update_match_results():
    headers = {"X-Auth-Token": API_KEY}
    try:
        resp = requests.get("https://api.football-data.org/v4/matches", headers=headers)
        data = resp.json()
        matches = data.get("matches", [])
        now = datetime.now(timezone.utc)

        for m in matches:
            match_id = m["id"]
            status = m.get("status")
            if status == "FINISHED" and match_id in user_bets:
                bets = user_bets.pop(match_id)
                result = get_match_result(m)

                for user_id, choice in bets.items():
                    for guild_id in server_points:
                        if user_id not in server_points[guild_id]:
                            server_points[guild_id][user_id] = 0
                        if choice == result:
                            server_points[guild_id][user_id] += 1
                # Update leaderboard for all guilds
                for guild_id in server_points:
                    await post_or_update_leaderboard(guild_id)

                posted_matches.discard(match_id)
                match_lookup.pop(match_id, None)
                match_messages.pop(match_id, None)

    except Exception as e:
        print("Error updating match results:", e)

# ------------------ AUTO POST MATCHES ------------------
@tasks.loop(minutes=5)
async def auto_post_matches():
    matches = fetch_upcoming_matches()
    channel = bot.get_channel(POST_CHANNEL_ID)
    if not channel:
        print("Channel not found.")
        return

    for m in matches:
        match_id = m["id"]
        if match_id in posted_matches:
            continue

        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        utc_date = m["utcDate"]
        logo_home = m["homeTeam"].get("crest", "")
        logo_away = m["awayTeam"].get("crest", "")

        embed = discord.Embed(title=f"{home} vs {away}", description=f"Kickoff: {utc_date}", color=discord.Color.blue())
        embed.set_thumbnail(url=logo_home)
        embed.set_image(url=logo_away)

        msg = await channel.send(embed=embed, view=BetView(match_id))
        posted_matches.add(match_id)
        match_lookup[match_id] = m
        match_messages[match_id] = msg

# ------------------ COUNTDOWN UPDATES ------------------
@tasks.loop(minutes=1)
async def update_match_countdowns():
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    for match_id, msg in list(match_messages.items()):
        match = match_lookup.get(match_id)
        if not match:
            continue

        match_time = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        delta = match_time - now
        if delta.total_seconds() <= 0:
            continue

        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]

        embed = discord.Embed(
            title=f"{home} vs {away}",
            description=f"Kickoff in {hours}h {minutes}m",
            color=discord.Color.blue()
        )
        logo_home = match["homeTeam"].get("crest", "")
        logo_away = match["awayTeam"].get("crest", "")
        embed.set_thumbnail(url=logo_home)
        embed.set_image(url=logo_away)

        try:
            await msg.edit(embed=embed)
        except:
            continue

# ------------------ BOT EVENTS ------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()
    auto_post_matches.start()
    auto_update_results.start()
    update_match_countdowns.start()

# ------------------ SLASH COMMANDS ------------------
@tree.command(name="leaderboard", description="Show server leaderboard")
async def leaderboard_cmd(interaction: discord.Interaction):
    embed = await get_leaderboard_embed(interaction.guild.id)
    await interaction.response.send_message(embed=embed)

# ------------------ RUN BOT ------------------
bot.run(TOKEN)
