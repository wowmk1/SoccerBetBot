import os
import json
import aiohttp
from datetime import datetime, timezone, timedelta
from io import BytesIO
from PIL import Image
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

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

# ==== TRACK VOTES ====
vote_data = {}  # match_id: {"home": set(), "draw": set(), "away": set(), "votes_msg_id": int, "locked_users": set(), "buttons_disabled": bool}
last_leaderboard_msg_id = None

# ==== VOTES EMBED CREATION ====
def create_votes_embed(match_id, match_result=None):
    votes_dict = vote_data[match_id]
    embed = discord.Embed(title="Current Votes", color=discord.Color.green())
    for option in ["home", "draw", "away"]:
        voters = sorted(votes_dict[option])
        field_value = "\n".join(voters) if voters else "No votes yet"
        if match_result == option:
            field_value += "\n✅ Correct!"
        embed.add_field(name=option.capitalize(), value=field_value, inline=False)
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
        img.paste(home, (0,0), home)
    if away_img_bytes:
        away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0]+padding, 0), away)
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
    return [m for m in matches if now <= datetime.fromisoformat(m['utcDate'].replace("Z","+00:00")) <= next_24h]

# ==== VOTE BUTTON ====
class VoteButton(Button):
    def __init__(self, label, category, match_id, kickoff_time):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.category = category
        self.match_id = match_id
        self.kickoff_time = kickoff_time

    async def callback(self, interaction: discord.Interaction):
        now = datetime.now(timezone.utc)
        if now >= self.kickoff_time:
            await interaction.response.send_message("⏰ Voting for this match has ended!", ephemeral=True)
            return
        user = interaction.user
        match_id = self.match_id
        if match_id not in vote_data:
            vote_data[match_id] = {"home": set(), "draw": set(), "away": set(),
                                   "votes_msg_id": None, "locked_users": set(), "buttons_disabled": False}
        if user.id in vote_data[match_id]["locked_users"]:
            await interaction.response.send_message("✅ You have already voted!", ephemeral=True)
            return

        # Record vote
        vote_data[match_id][self.category].add(user.name)
        vote_data[match_id]["locked_users"].add(user.id)

        # Update votes embed
        votes_msg_id = vote_data[match_id]["votes_msg_id"]
        embed = create_votes_embed(match_id)
        if votes_msg_id:
            votes_message = await interaction.channel.fetch_message(votes_msg_id)
            await votes_message.edit(embed=embed)
        else:
            votes_message = await interaction.channel.send(embed=embed)
            vote_data[match_id]["votes_msg_id"] = votes_message.id

        # Update leaderboard predictions
        user_id = str(user.id)
        if user_id not in leaderboard:
            leaderboard[user_id] = {"name": user.name, "points": 0, "predictions": {}}
        leaderboard[user_id]["predictions"][match_id] = self.category
        save_leaderboard()

        await interaction.response.send_message(f"You voted for **{self.label}**!", ephemeral=True)

# ==== POST MATCH ====
async def post_match(match):
    match_id = str(match["id"])
    if match_id in posted_matches:
        return
    match_time = datetime.fromisoformat(match['utcDate'].replace("Z","+00:00"))
    if match_time < datetime.now(timezone.utc):
        return
    kickoff_ts = int(match_time.timestamp())
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
                          description=f"Kickoff: <t:{kickoff_ts}:f>", color=discord.Color.blue())
    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    file = None
    if home_crest or away_crest:
        image_buffer = await generate_match_image(home_crest, away_crest)
        file = discord.File(fp=image_buffer, filename="match.png")
        embed.set_image(url="attachment://match.png")

    # Add vote buttons
    view = View()
    view.add_item(VoteButton("Home", "home", match_id, kickoff_time=match_time))
    view.add_item(VoteButton("Draw", "draw", match_id, kickoff_time=match_time))
    view.add_item(VoteButton("Away", "away", match_id, kickoff_time=match_time))

    votes_message = await channel.send(embed=embed, file=file, view=view)
    vote_data[match_id] = {"home": set(), "draw": set(), "away": set(),
                           "votes_msg_id": votes_message.id, "locked_users": set(), "buttons_disabled": False}
    posted_matches.add(match_id)
    save_posted()

# ==== UPDATE MATCH RESULTS & LEADERBOARD ====
@tasks.loop(minutes=5)
async def update_match_results():
    global last_leaderboard_msg_id
    leaderboard_changed = False
    previous_points = {uid: v.get("points",0) for uid,v in leaderboard.items()}

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                for m in data.get("matches",[]):
                    match_id = str(m["id"])
                    status = m.get("status")
                    if status != "FINISHED":
                        continue
                    result = m.get("score",{}).get("winner")
                    if not result:
                        continue

                    # Update leaderboard points
                    for uid, v in leaderboard.items():
                        if v.get("predictions",{}).get(match_id) == result:
                            v["points"] = v.get("points",0)+1
                            leaderboard_changed = True
                    save_leaderboard()

                    # Update vote embed & disable buttons
                    if match_id in vote_data:
                        try:
                            msg_id = vote_data[match_id]["votes_msg_id"]
                            if msg_id:
                                channel = bot.get_channel(MATCH_CHANNEL_ID)
                                votes_message = await channel.fetch_message(msg_id)
                                embed = create_votes_embed(match_id, match_result=result)
                                new_view = View()
                                for item in votes_message.children if hasattr(votes_message,"children") else votes_message.components[0].children:
                                    item.disabled = True
                                    new_view.add_item(item)
                                await votes_message.edit(embed=embed, view=new_view)
                                vote_data[match_id]["buttons_disabled"] = True
                        except Exception as e:
                            print(f"Failed to update votes for finished match: {e}")

    # Update leaderboard message
    if leaderboard_changed:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            return
        users = [v for v in leaderboard.values() if v.get("predictions")]
        if not users:
            return
        sorted_lb = sorted(users, key=lambda x:(-x.get("points",0), x["name"].lower()))
        desc_lines = []
        for i, entry in enumerate(sorted_lb[:10]):
            uid = next(uid for uid,v in leaderboard.items() if v["name"]==entry["name"])
            diff = entry.get("points",0)-previous_points.get(uid,0)
            suffix = f" (+{diff})" if diff>0 else ""
            desc_lines.append(f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts{suffix}")
        desc = "\n".join(desc_lines)
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
        league_name = m["competition"].get("name","Unknown League")
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
    sorted_lb = sorted(users, key=lambda x:(-x.get("points",0), x["name"].lower()))
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts" for i, entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ==== TEST MATCHES COMMAND ====
@bot.tree.command(name="test_matches", description="Create test matches and leaderboard for simulation.")
async def test_matches_command(interaction: discord.Interaction):
    await interaction.response.send_message("📝 Creating test matches...", ephemeral=True)
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    now = datetime.now(timezone.utc)

    # Clear old test matches
    vote_data.clear()
    posted_matches.clear()

    test_matches = [
        {"id": 9991, "homeTeam": {"name": "Team A", "crest": None}, "awayTeam": {"name": "Team B", "crest": None},
         "competition": {"name": "Test League"}, "utcDate": (now+timedelta(minutes=1)).isoformat()},
        {"id": 9992, "homeTeam": {"name": "Team C", "crest": None}, "awayTeam": {"name": "Team D", "crest": None},
         "competition": {"name": "Test League"}, "utcDate": (now+timedelta(minutes=2)).isoformat()}
    ]

    for m in test_matches:
        await post_match(m)

    # Simulate voting and finishing matches after 1 minute
    async def simulate_votes_and_finish():
        global last_leaderboard_msg_id
        await asyncio.sleep(60)
        # Randomly mark results
        results = { "9991": "home", "9992": "draw" }
        for match_id, result in results.items():
            for uid, v in leaderboard.items():
                if v.get("predictions",{}).get(match_id) == result:
                    v["points"] += 1
            save_leaderboard()
            # Update votes message
            if match_id in vote_data:
                try:
                    msg_id = vote_data[match_id]["votes_msg_id"]
                    if msg_id:
                        votes_message = await channel.fetch_message(msg_id)
                        embed = create_votes_embed(match_id, match_result=result)
                        new_view = View()
                        for item in votes_message.components[0].children:
                            item.disabled = True
                            new_view.add_item(item)
                        await votes_message.edit(embed=embed, view=new_view)
                except: pass
        # Update leaderboard in channel
        users = [v for v in leaderboard.values() if v.get("predictions")]
        if users:
            sorted_lb = sorted(users, key=lambda x:(-x.get("points",0), x["name"].lower()))
            desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts" for i, entry in enumerate(sorted_lb[:10])])
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

    bot.loop.create_task(simulate_votes_and_finish())

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    update_match_results.start()
    scheduler.start()
    print(f"Logged in as {bot.user}")

# ==== DAILY MATCH POST SCHEDULER ====
async def daily_fetch_matches():
    matches = await fetch_matches()
    for m in matches:
        await post_match(m)

scheduler = AsyncIOScheduler()
scheduler.add_job(lambda: bot.loop.create_task(daily_fetch_matches()), "cron", hour=6, minute=0)

bot.run(DISCORD_BOT_TOKEN)
