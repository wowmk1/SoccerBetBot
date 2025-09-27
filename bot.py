import os
import json
import aiohttp
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
import discord
from discord.ext import commands, tasks

# ==== ENV VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID]):
    raise ValueError("Missing one or more environment variables.")

# ==== BOT SETUP ====
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== LEADERBOARD ====
LEADERBOARD_FILE = "leaderboard.json"
if os.path.exists(LEADERBOARD_FILE):
    with open(LEADERBOARD_FILE, "r") as f:
        leaderboard = json.load(f)
else:
    leaderboard = {}

def save_leaderboard():
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(leaderboard, f)

# ==== TRACK POSTED MATCHES ====
POSTED_FILE = "posted_matches.json"
if os.path.exists(POSTED_FILE):
    with open(POSTED_FILE, "r") as f:
        posted_matches = set(json.load(f))
else:
    posted_matches = set()

def save_posted():
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted_matches), f)

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "PD", "FL1", "SA", "EC", "WC"]

# ==== VOTE EMOJIS (CUSTOM SERVER EMOJIS) ====
VOTE_EMOJIS = {
    "home": "HOME_TEAM",
    "draw": "DRAW",
    "away": "AWAY_TEAM"
}

# ==== TRACK VOTES SEPARATELY ====
vote_data = {}  # {match_msg_id: {"home": set(), "draw": set(), "away": set(), "votes_msg_id": int}}

# ==== TRACK LAST LEADERBOARD MESSAGE ====
last_leaderboard_msg_id = None

def create_votes_embed(votes_dict):
    embed = discord.Embed(title="Current Votes", color=discord.Color.green())
    embed.add_field(
        name="Home",
        value=", ".join(votes_dict["home"]) if votes_dict["home"] else "No votes yet",
        inline=False
    )
    embed.add_field(
        name="Draw",
        value=", ".join(votes_dict["draw"]) if votes_dict["draw"] else "No votes yet",
        inline=False
    )
    embed.add_field(
        name="Away",
        value=", ".join(votes_dict["away"]) if votes_dict["away"] else "No votes yet",
        inline=False
    )
    return embed

# ==== GENERATE MATCH IMAGE ====
async def generate_match_image(home_url, away_url):
    async with aiohttp.ClientSession() as session:
        home_img_bytes, away_img_bytes = None, None
        try:
            if home_url:
                async with session.get(home_url) as r:
                    home_img_bytes = await r.read()
        except: pass
        try:
            if away_url:
                async with session.get(away_url) as r:
                    away_img_bytes = await r.read()
        except: pass

    size = (100, 100)
    padding = 40
    width = size[0]*2 + padding
    height = size[1]

    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))

    if home_img_bytes:
        home = Image.open(BytesIO(home_img_bytes)).convert("RGBA").resize(size)
        img.paste(home, (0, 0), home)

    if away_img_bytes:
        away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0] + padding, 0), away)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== FETCH MATCHES (NEXT 24H ONLY) ====
async def fetch_matches():
    now = datetime.now(timezone.utc)
    next_24h = now + timedelta(hours=24)
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={next_24h.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("matches", []):
                        m["competition"]["name"] = data.get("competition", {}).get("name", comp)
                        matches.append(m)

    return [
        m for m in matches
        if now <= datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) <= next_24h
    ]

# ==== POST MATCH ====
async def post_match(match):
    match_id = str(match["id"])
    if match_id in posted_matches:
        return  # Already posted

    match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    if match_time < datetime.now(timezone.utc):
        return

    kickoff_ts = int(match_time.timestamp())
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: <t:{kickoff_ts}:f>",
        color=discord.Color.blue()
    )

    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    file = None
    if home_crest or away_crest:
        image_buffer = await generate_match_image(home_crest, away_crest)
        file = discord.File(fp=image_buffer, filename="match.png")
        embed.set_image(url="attachment://match.png")

    msg = await channel.send(embed=embed, file=file)

    if not hasattr(bot, "match_times"):
        bot.match_times = {}
    bot.match_times[str(msg.id)] = match_time

    guild = channel.guild
    for emoji_name in VOTE_EMOJIS.keys():
        emoji = discord.utils.get(guild.emojis, name=emoji_name)
        if emoji:
            await msg.add_reaction(emoji)

    # Initialize vote tracking
    vote_data[msg.id] = {"home": set(), "draw": set(), "away": set(), "votes_msg_id": None}

    posted_matches.add(match_id)
    save_posted()

# ==== REACTION HANDLER ====
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    emoji_name = getattr(payload.emoji, "name", str(payload.emoji))
    if emoji_name not in VOTE_EMOJIS.values():
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
    message = await channel.fetch_message(payload.message_id)

    # Initialize vote tracking for this match if not exists
    if payload.message_id not in vote_data:
        vote_data[payload.message_id] = {"home": set(), "draw": set(), "away": set(), "votes_msg_id": None}

    user = await bot.fetch_user(payload.user_id)

    # ---- REMOVE USER FROM ALL OTHER OPTIONS ----
    for category in ["home", "draw", "away"]:
        vote_data[payload.message_id][category].discard(user.name)
    
    # ---- ADD USER TO THE SELECTED OPTION ----
    for key, val in VOTE_EMOJIS.items():
        if emoji_name == val:
            vote_data[payload.message_id][key].add(user.name)

    # ---- REMOVE OTHER REACTIONS BY THE SAME USER ----
    for react in message.reactions:
        react_name = getattr(react.emoji, "name", str(react.emoji))
        if react_name != emoji_name:
            async for u in react.users():
                if u.id == payload.user_id:
                    await react.remove(u)

    # ---- UPDATE VOTES EMBED ----
    votes_msg_id = vote_data[payload.message_id]["votes_msg_id"]
    embed = create_votes_embed(vote_data[payload.message_id])

    if votes_msg_id:
        try:
            votes_message = await channel.fetch_message(votes_msg_id)
            await votes_message.edit(embed=embed)
        except:
            votes_msg_id = None

    if not votes_msg_id:
        votes_message = await channel.send(embed=embed)
        vote_data[payload.message_id]["votes_msg_id"] = votes_message.id

    # ---- UPDATE LEADERBOARD ----
    user_id = str(payload.user_id)
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": user.name, "points": 0, "predictions": {}}
    leaderboard[user_id]["predictions"][str(message.id)] = emoji_name
    save_leaderboard()

# ==== UPDATE MATCH RESULTS & LEADERBOARD ====
@tasks.loop(minutes=5)
async def update_match_results():
    global last_leaderboard_msg_id
    leaderboard_changed = False
    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                for m in data.get("matches", []):
                    match_id = str(m["id"])
                    status = m.get("status")
                    if status != "FINISHED":
                        continue
                    result = m.get("score", {}).get("winner")
                    if not result:
                        continue

                    for uid, v in leaderboard.items():
                        if v.get("predictions", {}).get(match_id) == result:
                            v["points"] = v.get("points", 0) + 1
                            leaderboard_changed = True
                    save_leaderboard()

    if leaderboard_changed:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            return

        users = [v for v in leaderboard.values() if v.get("predictions")]
        if not users:
            return

        sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
        desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts"
                          for i, entry in enumerate(sorted_lb[:10])])
        embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())

        try:
            if last_leaderboard_msg_id:
                msg = await channel.fetch_message(last_leaderboard_msg_id)
                await msg.edit(embed=embed)
            else:
                msg = await channel.send(embed=embed)
                last_leaderboard_msg_id = msg.id
        except:
            msg = await channel.send(embed=embed)
            last_leaderboard_msg_id = msg.id

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches in the next 24 hours.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches in the next 24 hours.", ephemeral=True)
        return

    league_dict = {}
    for m in matches:
        league_name = m["competition"].get("name", "Unknown League")
        league_dict.setdefault(league_name, []).append(m)

    for league_name, league_matches in league_dict.items():
        await interaction.channel.send(f"🏟 **{league_name}**")
        for m in league_matches:
            await post_match(m)

    await interaction.response.send_message("✅ Posted upcoming matches for the next 24 hours!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    users = [v for v in leaderboard.values() if v.get("predictions")]
    if not users:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return
    sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts"
                      for i, entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    update_match_results.start()
    print(f"Logged in as {bot.user}")

# ==== AUTO POST MATCHES ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    if not matches:
        return

    league_dict = {}
    for m in matches:
        league_name = m["competition"].get("name", "Unknown League")
        league_dict.setdefault(league_name, []).append(m)

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    for league_name, league_matches in league_dict.items():
        await channel.send(f"🏟 **{league_name}**")
        for m in league_matches:
            await post_match(m)

bot.run(DISCORD_BOT_TOKEN)
