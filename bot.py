import os
import json
import aiohttp
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
from io import BytesIO, StringIO
from PIL import Image
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==== ENV VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
MATCH_CHANNEL_ID = int(os.environ.get("MATCH_CHANNEL_ID"))
LEADERBOARD_CHANNEL_ID = int(os.environ.get("LEADERBOARD_CHANNEL_ID"))
DATABASE_URL = os.environ.get("DATABASE_URL")

if not all([DISCORD_BOT_TOKEN, FOOTBALL_DATA_API_KEY, MATCH_CHANNEL_ID, LEADERBOARD_CHANNEL_ID, DATABASE_URL]):
    raise ValueError("Missing one or more environment variables.")

# ==== BOT SETUP ====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== DATABASE FUNCTIONS ====
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Initialize database tables"""
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            points INTEGER DEFAULT 0
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            prediction TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, match_id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted_matches (
            match_id TEXT PRIMARY KEY,
            home_team TEXT,
            away_team TEXT,
            match_time TIMESTAMP,
            posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vote_data (
            match_id TEXT PRIMARY KEY,
            votes_msg_id BIGINT,
            buttons_disabled BOOLEAN DEFAULT FALSE
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()

def get_leaderboard():
    """Get all users sorted by points"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, points FROM users ORDER BY points DESC, username ASC")
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results

def get_user(user_id):
    """Get user data"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result

def upsert_user(user_id, username):
    """Create or update user"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, points)
        VALUES (%s, %s, 0)
        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
    """, (user_id, username))
    conn.commit()
    cur.close()
    conn.close()

def add_prediction(user_id, match_id, prediction):
    """Add a prediction"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO predictions (user_id, match_id, prediction)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, match_id) DO NOTHING
    """, (user_id, match_id, prediction))
    conn.commit()
    cur.close()
    conn.close()

def get_predictions_for_match(match_id):
    """Get all predictions for a match grouped by prediction type"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.prediction, u.username
        FROM predictions p
        JOIN users u ON p.user_id = u.user_id
        WHERE p.match_id = %s
        ORDER BY u.username
    """, (match_id,))
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    votes = {"home": set(), "draw": set(), "away": set()}
    for row in results:
        votes[row['prediction']].add(row['username'])
    return votes

def get_user_stats(user_id):
    """Get user prediction stats"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM predictions WHERE user_id = %s", (user_id,))
    total = cur.fetchone()['total']
    
    cur.execute("SELECT points FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    correct = user['points'] if user else 0
    
    cur.close()
    conn.close()
    
    accuracy = (correct / total * 100) if total > 0 else 0
    return {"total": total, "correct": correct, "accuracy": accuracy}

def user_has_prediction(user_id, match_id):
    """Check if user already voted"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM predictions WHERE user_id = %s AND match_id = %s", (user_id, match_id))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None

def add_points(user_id, points_to_add):
    """Add points to user"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = points + %s WHERE user_id = %s", (points_to_add, user_id))
    conn.commit()
    cur.close()
    conn.close()

def set_user_points(user_id, points):
    """Set user points to specific value"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = %s WHERE user_id = %s", (points, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_match_posted(match_id):
    """Check if match already posted"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted_matches WHERE match_id = %s", (match_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result is not None

def mark_match_posted(match_id, home_team, away_team, match_time):
    """Mark match as posted"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO posted_matches (match_id, home_team, away_team, match_time)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (match_id, home_team, away_team, match_time))
    conn.commit()
    cur.close()
    conn.close()

def get_recent_matches():
    """Get matches from last 2 days and upcoming"""
    conn = get_db()
    cur = conn.cursor()
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    cur.execute("""
        SELECT match_id, home_team, away_team, match_time
        FROM posted_matches
        WHERE match_time >= %s
        ORDER BY match_time ASC
    """, (two_days_ago,))
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results

def save_vote_message(match_id, msg_id):
    """Save vote message ID"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO vote_data (match_id, votes_msg_id)
        VALUES (%s, %s)
        ON CONFLICT (match_id) DO UPDATE SET votes_msg_id = EXCLUDED.votes_msg_id
    """, (match_id, msg_id))
    conn.commit()
    cur.close()
    conn.close()

def get_vote_message_id(match_id):
    """Get vote message ID"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT votes_msg_id, buttons_disabled FROM vote_data WHERE match_id = %s", (match_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result

def disable_vote_buttons(match_id):
    """Mark buttons as disabled"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE vote_data SET buttons_disabled = TRUE WHERE match_id = %s", (match_id,))
    conn.commit()
    cur.close()
    conn.close()

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "PD", "FL1", "SA"]

last_leaderboard_msg_id = None

# ==== VOTES EMBED CREATION ====
def create_votes_embed(match_id, match_result=None):
    votes = get_predictions_for_match(match_id)
    embed = discord.Embed(title="Current Votes", color=discord.Color.green())
    for option in ["home", "draw", "away"]:
        voters = sorted(votes[option])
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
        img.paste(home, (0, 0), home)
    if away_img_bytes:
        away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
        img.paste(away, (size[0]+padding, 0), away)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== FETCH MATCHES ====
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
    return [m for m in matches if now <= datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) <= next_24h]

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
            await interaction.response.send_message("Voting for this match has ended!", ephemeral=True)
            return
        
        user = interaction.user
        user_id = str(user.id)
        match_id = self.match_id
        
        if user_has_prediction(user_id, match_id):
            await interaction.response.send_message("You have already voted!", ephemeral=True)
            return
        
        upsert_user(user_id, user.name)
        add_prediction(user_id, match_id, self.category)
        
        vote_msg = get_vote_message_id(match_id)
        embed = create_votes_embed(match_id)
        if vote_msg and vote_msg['votes_msg_id']:
            try:
                votes_message = await interaction.channel.fetch_message(vote_msg['votes_msg_id'])
                await votes_message.edit(embed=embed)
            except:
                votes_message = await interaction.channel.send(embed=embed)
                save_vote_message(match_id, votes_message.id)
        else:
            votes_message = await interaction.channel.send(embed=embed)
            save_vote_message(match_id, votes_message.id)
        
        await interaction.response.send_message(f"You voted for **{self.label}**!", ephemeral=True)

# ==== POST MATCH ====
async def post_match(match):
    match_id = str(match["id"])
    if is_match_posted(match_id):
        return
    
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
    
    view = View()
    view.add_item(VoteButton("Home", "home", match_id, kickoff_time=match_time))
    view.add_item(VoteButton("Draw", "draw", match_id, kickoff_time=match_time))
    view.add_item(VoteButton("Away", "away", match_id, kickoff_time=match_time))
    
    votes_message = await channel.send(embed=embed, file=file, view=view)
    save_vote_message(match_id, votes_message.id)
    mark_match_posted(match_id, match['homeTeam']['name'], match['awayTeam']['name'], match_time)

# ==== UPDATE MATCH RESULTS ====
@tasks.loop(minutes=5)
async def update_match_results():
    global last_leaderboard_msg_id
    leaderboard_changed = False
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, points FROM users")
    previous_points = {row['user_id']: row['points'] for row in cur.fetchall()}
    cur.close()
    conn.close()
    
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
                    
                    result_map = {"HOME_TEAM": "home", "AWAY_TEAM": "away", "DRAW": "draw"}
                    result = result_map.get(result, result.lower())
                    
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT user_id FROM predictions
                        WHERE match_id = %s AND prediction = %s
                    """, (match_id, result))
                    winners = cur.fetchall()
                    
                    for winner in winners:
                        add_points(winner['user_id'], 1)
                        leaderboard_changed = True
                    
                    cur.close()
                    conn.close()
                    
                    vote_msg = get_vote_message_id(match_id)
                    if vote_msg and not vote_msg['buttons_disabled']:
                        try:
                            channel = bot.get_channel(MATCH_CHANNEL_ID)
                            votes_message = await channel.fetch_message(vote_msg['votes_msg_id'])
                            embed = create_votes_embed(match_id, match_result=result)
                            new_view = View()
                            for item in votes_message.components[0].children:
                                item.disabled = True
                                new_view.add_item(item)
                            await votes_message.edit(embed=embed, view=new_view)
                            disable_vote_buttons(match_id)
                        except Exception as e:
                            print(f"Failed to update votes: {e}")
    
    if leaderboard_changed:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            return
        
        leaderboard = get_leaderboard()
        desc_lines = []
        for i, entry in enumerate(leaderboard[:10]):
            diff = entry['points'] - previous_points.get(entry['user_id'], 0)
            suffix = f" (+{diff})" if diff > 0 else ""
            desc_lines.append(f"**{i+1}. {entry['username']}** — {entry['points']} pts{suffix}")
        desc = "\n".join(desc_lines)
        embed = discord.Embed(title="Leaderboard", description=desc, color=discord.Color.gold())
        
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

# ==== ADMIN COMMANDS ====
@bot.tree.command(name="backup", description="[ADMIN] Backup all data to JSON")
async def backup_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only", ephemeral=True)
        return
    
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT user_id, username, points FROM users")
    users = cur.fetchall()
    
    cur.execute("SELECT user_id, match_id, prediction FROM predictions")
    predictions = cur.fetchall()
    
    cur.close()
    conn.close()
    
    backup_data = {
        "users": [dict(u) for u in users],
        "predictions": [dict(p) for p in predictions],
        "backup_time": datetime.now(timezone.utc).isoformat()
    }
    
    file_content = json.dumps(backup_data, indent=2)
    file = discord.File(StringIO(file_content), filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    
    await interaction.response.send_message("Database backup:", file=file, ephemeral=True)

@bot.tree.command(name="setpoints", description="[ADMIN] Set user points")
async def setpoints_command(interaction: discord.Interaction, user: discord.Member, points: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only", ephemeral=True)
        return
    
    user_id = str(user.id)
    upsert_user(user_id, user.name)
    set_user_points(user_id, points)
    
    await interaction.response.send_message(f"Set {user.name}'s points to {points}", ephemeral=True)

@bot.tree.command(name="addpoints", description="[ADMIN] Add points to user")
async def addpoints_command(interaction: discord.Interaction, user: discord.Member, points: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only", ephemeral=True)
        return
    
    user_id = str(user.id)
    upsert_user(user_id, user.name)
    add_points(user_id, points)
    
    current_user = get_user(user_id)
    await interaction.response.send_message(f"Added {points} points to {user.name}. New total: {current_user['points']}", ephemeral=True)

@bot.tree.command(name="restore", description="[ADMIN] Restore from backup JSON")
async def restore_command(interaction: discord.Interaction, backup_file: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        content = await backup_file.read()
        data = json.loads(content.decode('utf-8'))
        
        conn = get_db()
        cur = conn.cursor()
        
        for user_id, user_data in data.items():
            upsert_user(user_id, user_data['name'])
            set_user_points(user_id, user_data['points'])
            
            for match_id, prediction in user_data.get('predictions', {}).items():
                cur.execute("""
                    INSERT INTO predictions (user_id, match_id, prediction)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, match_id) DO NOTHING
                """, (user_id, match_id, prediction))
        
        conn.commit()
        cur.close()
        conn.close()
        
        await interaction.followup.send("Data restored successfully!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error restoring data: {str(e)}", ephemeral=True)

# ==== USER COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches")
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
        await interaction.channel.send(f"**{league_name}**")
        for m in league_matches:
            await post_match(m)
    
    try:
        await interaction.response.send_message("Posted upcoming matches!", ephemeral=True)
    except:
        pass

@bot.tree.command(name="leaderboard", description="Show the leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    leaderboard = get_leaderboard()
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return
    
    # Get prediction counts for each user
    conn = get_db()
    cur = conn.cursor()
    prediction_counts = {}
    for entry in leaderboard:
        cur.execute("SELECT COUNT(*) as count FROM predictions WHERE user_id = %s", (entry['user_id'],))
        prediction_counts[entry['user_id']] = cur.fetchone()['count']
    cur.close()
    conn.close()
    
    # Medal emojis
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    
    embed = discord.Embed(title="🏆 Prediction Leaderboard", color=discord.Color.gold())
    
    desc_lines = []
    for i, entry in enumerate(leaderboard[:5]):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        pred_count = prediction_counts.get(entry['user_id'], 0)
        desc_lines.append(f"{medal} **{entry['username']}** — **{entry['points']} pts** *({pred_count} predictions)*")
    
    embed.description = "\n".join(desc_lines)
    
    # Footer
    total_players = len(leaderboard)
    total_predictions = sum(prediction_counts.values())
    embed.set_footer(text=f"{total_players} players • 🎯 {total_predictions} total predictions")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ticket", description="Show your predictions")
async def ticket_command(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    user_id = str(target_user.id)
    
    user_data = get_user(user_id)
    if not user_data:
        await interaction.response.send_message(f"{target_user.name} has no predictions yet.", ephemeral=True)
        return
    
    stats = get_user_stats(user_id)
    
    # Get all user predictions
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.match_id, p.prediction, pm.home_team, pm.away_team, pm.match_time
        FROM predictions p
        LEFT JOIN posted_matches pm ON p.match_id = pm.match_id
        WHERE p.user_id = %s
        ORDER BY pm.match_time DESC NULLS LAST
    """, (user_id,))
    predictions = cur.fetchall()
    cur.close()
    conn.close()
    
    if not predictions:
        await interaction.response.send_message(f"{target_user.name} has no predictions yet.", ephemeral=True)
        return
    
    embeds = []
    current_embed = discord.Embed(
        title=f"{target_user.name}'s Predictions",
        description=f"Points: {user_data['points']} | Accuracy: {stats['accuracy']:.1f}% ({stats['correct']}/{stats['total']})",
        color=discord.Color.blue()
    )
    
    field_count = 0
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(days=2)
    
    for pred in predictions:
        # Only show recent matches (last 2 days + upcoming)
        if pred['match_time'] and pred['match_time'] < two_days_ago:
            continue
        
        # If no match data, show match ID only
        if not pred['home_team']:
            field_name = f"Match {pred['match_id']}"
            field_value = f"Prediction: {pred['prediction'].capitalize()}"
        else:
            match_time = pred['match_time']
            is_future = match_time > now if match_time else False
            status = "Upcoming" if is_future else "Played"
            field_name = f"{pred['home_team']} vs {pred['away_team']}"
            field_value = f"Prediction: {pred['prediction'].capitalize()}\nStatus: {status}"
        
        if field_count >= 20:
            embeds.append(current_embed)
            current_embed = discord.Embed(
                title=f"{target_user.name}'s Predictions (cont.)",
                color=discord.Color.blue()
            )
            field_count = 0
        
        current_embed.add_field(name=field_name, value=field_value, inline=False)
        field_count += 1
    
    if field_count == 0:
        await interaction.response.send_message(f"{target_user.name} has no recent predictions to display.", ephemeral=True)
        return
    
    embeds.append(current_embed)
    
    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=True)

# ==== STARTUP ====
@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    update_match_results.start()
    scheduler.start()
    print(f"Logged in as {bot.user}")

# ==== SCHEDULER ====
scheduler = AsyncIOScheduler()
async def daily_fetch_matches():
    matches = await fetch_matches()
    for m in matches:
        await post_match(m)
scheduler.add_job(lambda: bot.loop.create_task(daily_fetch_matches()), "cron", hour=6, minute=0)

bot.run(DISCORD_BOT_TOKEN)
