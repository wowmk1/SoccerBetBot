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

# ==== GENERATE COMBINED MATCH IMAGE ====
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

    # Filter strictly for matches within the next 24h
    return [
        m for m in matches
        if now <= datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) <= next_24h
    ]

# ==== POST MATCH ====
async def post_match(match):
    match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    if match_time < datetime.now(timezone.utc):
        return

    # Format kickoff as Discord timestamp
    kickoff_ts = int(match_time.timestamp())

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    match_id = str(match["id"])
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]

    embed_desc = f"Kickoff: <t:{kickoff_ts}:f>"
    if voter_names:
        embed_desc += "\n\n**Voted:** " + ", ".join(voter_names)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=embed_desc,
        color=discord.Color.blue()
    )

    # Generate combined image
    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    file = None
    if home_crest or away_crest:
        image_buffer = await generate_match_image(home_crest, away_crest)
        file = discord.File(fp=image_buffer, filename="match.png")
        embed.set_image(url="attachment://match.png")

    msg = await channel.send(embed=embed, file=file)

    # Store kickoff time
    if not hasattr(bot, "match_times"):
        bot.match_times = {}
    bot.match_times[str(msg.id)] = match_time

    # Add custom emoji reactions
    guild = channel.guild
    for emoji_name in VOTE_EMOJIS.keys():
        emoji = discord.utils.get(guild.emojis, name=emoji_name)
        if emoji:
            await msg.add_reaction(emoji)

# ==== REACTION HANDLER ====
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    message = await channel.fetch_message(payload.message_id)
    match_id = str(message.id)

    emoji_name = payload.emoji.name
    if emoji_name not in VOTE_EMOJIS:
        async for react in message.reactions:
            if getattr(react.emoji, "name", None) == emoji_name:
                async for u in react.users():
                    if u.id == payload.user_id:
                        await react.remove(u)
        return

    match_time = bot.match_times.get(match_id)
    if not match_time or match_time < datetime.now(timezone.utc):
        return

    user_id = str(payload.user_id)
    user = await bot.fetch_user(payload.user_id)
    if user_id not in leaderboard:
        leaderboard[user_id] = {"name": user.name, "points": 0, "predictions": {}}

    # Enforce one vote per user
    for react in message.reactions:
        if getattr(react.emoji, "name", None) != emoji_name:
            async for u in react.users():
                if u.id == payload.user_id:
                    await react.remove(u)

    leaderboard[user_id]["predictions"][match_id] = VOTE_EMOJIS[emoji_name]
    save_leaderboard()

    # Update embed description only (image stays)
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]
    embed = message.embeds[0]
    kickoff_line = embed.description.split("Kickoff:")[1].splitlines()[0].strip()

    new_desc = f"Kickoff: {kickoff_line}"
    if voter_names:
        new_desc += "\n\n**Voted:** " + ", ".join(voter_names)

    new_embed = discord.Embed(
        title=embed.title,
        description=new_desc,
        color=embed.color
    )

    if embed.image.url:
        new_embed.set_image(url=embed.image.url)

    await message.edit(embed=new_embed)

# ==== CHECK FINISHED MATCHES & AWARD POINTS ====
@tasks.loop(minutes=5)
async def update_match_results():
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
                    save_leaderboard()

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
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry['points']} pts"
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
