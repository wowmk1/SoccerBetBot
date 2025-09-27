import os
import json
import aiohttp
from datetime import datetime, timezone, timedelta
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

# ==== FETCH MATCHES ====
async def fetch_matches(days_ahead=7):
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=days_ahead)
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={future.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("matches", []):
                        m["competition"]["name"] = data.get("competition", {}).get("name", comp)
                        matches.append(m)
    return [m for m in matches if datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) > datetime.now(timezone.utc)]

# ==== POST MATCH ====
async def post_match(match):
    match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    if match_time < datetime.now(timezone.utc):
        return

    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return

    match_id = str(match["id"])
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]

    embed_desc = f"Kickoff: {match['utcDate']}"
    if voter_names:
        embed_desc += "\n\n**Voted:** " + ", ".join(voter_names)

    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=embed_desc,
        color=discord.Color.blue()
    )

    # Home crest as thumbnail, away crest as main image
    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    if home_crest:
        embed.set_thumbnail(url=home_crest)
    if away_crest:
        embed.set_image(url=away_crest)

    msg = await channel.send(embed=embed)

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

    # Check custom emoji
    emoji_name = payload.emoji.name
    if emoji_name not in VOTE_EMOJIS:
        # remove invalid reactions
        async for react in message.reactions:
            if getattr(react.emoji, "name", None) == emoji_name:
                async for u in react.users():
                    if u.id == payload.user_id:
                        await react.remove(u)
        return

    # Check match time
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

    # Update embed with voter names without duplicating images
    voter_names = [v["name"] for uid, v in leaderboard.items() if match_id in v.get("predictions", {})]
    embed = message.embeds[0]
    kickoff_line = embed.description.split("Kickoff:")[1].splitlines()[0]

    new_desc = f"Kickoff: {kickoff_line}"
    if voter_names:
        new_desc += "\n\n**Voted:** " + ", ".join(voter_names)

    # Create new embed preserving thumbnail and image URLs
    new_embed = discord.Embed(
        title=embed.title,
        description=new_desc,
        color=embed.color
    )
    if embed.thumbnail.url:
        new_embed.set_thumbnail(url=embed.thumbnail.url)
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
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches(days_ahead=7)
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return

    # Group matches by league
    league_dict = {}
    for m in matches[:10]:
        league_name = m["competition"].get("name", "Unknown League")
        league_dict.setdefault(league_name, []).append(m)

    for league_name, league_matches in league_dict.items():
        await interaction.channel.send(f"🏟 **{league_name}**")
        for m in league_matches:
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
    update_match_results.start()
    print(f"Logged in as {bot.user}")

# ==== AUTO POST MATCHES ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches(days_ahead=7)
    if not matches:
        return

    # Group matches by league
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
