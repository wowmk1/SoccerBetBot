import os
import json
import aiohttp
import asyncio
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
from io import BytesIO, StringIO
from PIL import Image
from contextlib import contextmanager
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
import logging
import threading

# ==== LOGGING SETUP ====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('football_bot')

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

# ==== CACHE FOR MATCH RESULTS ====
match_results_cache = {}
cache_timestamp = None

# ==== DATABASE CONNECTION POOL ====
db_pool = None
pool_lock = threading.Lock()

def init_db_pool():
    """Initialize database connection pool"""
    global db_pool
    with pool_lock:
        if db_pool is None:
            db_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                dsn=DATABASE_URL,
                cursor_factory=RealDictCursor
            )
    logger.info("Database connection pool initialized")

@contextmanager
def db_connection():
    """Context manager using connection pool"""
    conn = None
    try:
        conn = db_pool.getconn()
        yield conn
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

# ==== DATABASE FUNCTIONS ====
def init_db():
    """Initialize database tables"""
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            
            # Create base tables
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
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_matches (
                    match_id TEXT PRIMARY KEY,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Add new columns to existing tables (safe if they already exist)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_streak INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE vote_data ADD COLUMN IF NOT EXISTS live_predictions_msg_id BIGINT")
            cur.execute("ALTER TABLE posted_matches ADD COLUMN IF NOT EXISTS competition TEXT")
            cur.execute("ALTER TABLE posted_matches ADD COLUMN IF NOT EXISTS home_score INTEGER")
            cur.execute("ALTER TABLE posted_matches ADD COLUMN IF NOT EXISTS away_score INTEGER")
            cur.execute("ALTER TABLE posted_matches ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'SCHEDULED'")
            cur.execute("ALTER TABLE posted_matches ADD COLUMN IF NOT EXISTS notification_sent BOOLEAN DEFAULT FALSE")
            
            # Create weekly_stats table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS weekly_stats (
                    user_id TEXT NOT NULL,
                    week_start DATE NOT NULL,
                    correct INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, week_start)
                )
            """)
            
            # Create indexes for better performance
            cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_match_id ON predictions(match_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_user_id ON predictions(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_posted_matches_time ON posted_matches(match_time)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_posted_matches_status ON posted_matches(status)")
            
            conn.commit()
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

def get_leaderboard():
    """Get all users sorted by points"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, points FROM users ORDER BY points DESC, username ASC")
        return cur.fetchall()

def get_user(user_id):
    """Get user data"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone()

def upsert_user(user_id, username):
    """Create or update user"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, username, points)
            VALUES (%s, %s, 0)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
        """, (user_id, username))
        conn.commit()

def add_prediction(user_id, match_id, prediction):
    """Add a prediction"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO predictions (user_id, match_id, prediction)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, match_id) DO NOTHING
        """, (user_id, match_id, prediction))
        conn.commit()

def update_prediction(user_id, match_id, new_prediction):
    """Update existing prediction"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE predictions
            SET prediction = %s
            WHERE user_id = %s AND match_id = %s
        """, (new_prediction, user_id, match_id))
        conn.commit()
        return cur.rowcount > 0

def get_user_prediction(user_id, match_id):
    """Get user's prediction for a match"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT prediction FROM predictions WHERE user_id = %s AND match_id = %s", 
                   (user_id, match_id))
        result = cur.fetchone()
        return result['prediction'] if result else None

def delete_prediction(user_id, match_id):
    """Delete a prediction"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM predictions WHERE user_id = %s AND match_id = %s", (user_id, match_id))
        conn.commit()
        return cur.rowcount > 0

def update_user_streak(user_id, is_correct):
    """Update user's streak"""
    with db_connection() as conn:
        cur = conn.cursor()
        
        if is_correct:
            cur.execute("""
                UPDATE users 
                SET current_streak = current_streak + 1,
                    best_streak = GREATEST(best_streak, current_streak + 1)
                WHERE user_id = %s
            """, (user_id,))
        else:
            cur.execute("UPDATE users SET current_streak = 0 WHERE user_id = %s", (user_id,))
        
        conn.commit()

def get_user_streaks(user_id):
    """Get user streak info"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_streak, best_streak FROM users WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        return result if result else {"current_streak": 0, "best_streak": 0}

def record_weekly_stat(user_id, is_correct):
    """Record weekly statistics"""
    with db_connection() as conn:
        cur = conn.cursor()
        today = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=today.weekday())  # Monday
        
        if is_correct:
            cur.execute("""
                INSERT INTO weekly_stats (user_id, week_start, correct, total)
                VALUES (%s, %s, 1, 1)
                ON CONFLICT (user_id, week_start) 
                DO UPDATE SET correct = weekly_stats.correct + 1, total = weekly_stats.total + 1
            """, (user_id, week_start))
        else:
            cur.execute("""
                INSERT INTO weekly_stats (user_id, week_start, correct, total)
                VALUES (%s, %s, 0, 1)
                ON CONFLICT (user_id, week_start) 
                DO UPDATE SET total = weekly_stats.total + 1
            """, (user_id, week_start))
        
        conn.commit()

def get_weekly_stats(user_id, week_start):
    """Get stats for a specific week"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT correct, total FROM weekly_stats 
            WHERE user_id = %s AND week_start = %s
        """, (user_id, week_start))
        result = cur.fetchone()
        return result if result else {"correct": 0, "total": 0}

def get_last_week_stats():
    """Get all users' stats from last week"""
    with db_connection() as conn:
        cur = conn.cursor()
        today = datetime.now(timezone.utc).date()
        last_week_start = today - timedelta(days=today.weekday() + 7)
        
        cur.execute("""
            SELECT u.user_id, u.username, ws.correct, ws.total
            FROM weekly_stats ws
            JOIN users u ON ws.user_id = u.user_id
            WHERE ws.week_start = %s AND ws.total > 0
            ORDER BY ws.correct DESC, ws.total ASC
        """, (last_week_start,))
        return cur.fetchall()

def mark_notification_sent(match_id):
    """Mark that notification was sent for this match"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posted_matches SET notification_sent = TRUE WHERE match_id = %s", (match_id,))
        conn.commit()

def get_upcoming_matches_for_notification():
    """Get matches starting in 10-15 minutes that haven't been notified"""
    with db_connection() as conn:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        start_window = now + timedelta(minutes=10)
        end_window = now + timedelta(minutes=15)
        
        cur.execute("""
            SELECT match_id, home_team, away_team, match_time
            FROM posted_matches
            WHERE match_time BETWEEN %s AND %s
            AND notification_sent = FALSE
            AND status = 'SCHEDULED'
        """, (start_window, end_window))
        return cur.fetchall()

def get_predictions_for_match(match_id):
    """Get all predictions for a match grouped by prediction type"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.prediction, u.username
            FROM predictions p
            JOIN users u ON p.user_id = u.user_id
            WHERE p.match_id = %s
            ORDER BY u.username
        """, (match_id,))
        results = cur.fetchall()
    
    votes = {"home": set(), "draw": set(), "away": set()}
    for row in results:
        votes[row['prediction']].add(row['username'])
    return votes

def get_user_stats(user_id):
    """Get user prediction stats"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM predictions WHERE user_id = %s", (user_id,))
        total = cur.fetchone()['total']
        
        cur.execute("SELECT points FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        correct = user['points'] if user else 0
    
    accuracy = (correct / total * 100) if total > 0 else 0
    return {"total": total, "correct": correct, "accuracy": accuracy}

def user_has_prediction(user_id, match_id):
    """Check if user already voted"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM predictions WHERE user_id = %s AND match_id = %s", (user_id, match_id))
        return cur.fetchone() is not None

def add_points(user_id, points_to_add):
    """Add points to user"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET points = points + %s WHERE user_id = %s", (points_to_add, user_id))
        conn.commit()

def set_user_points(user_id, points):
    """Set user points to specific value"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET points = %s WHERE user_id = %s", (points, user_id))
        conn.commit()

def is_match_posted(match_id):
    """Check if match already posted"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM posted_matches WHERE match_id = %s", (match_id,))
        return cur.fetchone() is not None

def mark_match_posted(match_id, home_team, away_team, match_time, competition):
    """Mark match as posted"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO posted_matches (match_id, home_team, away_team, match_time, competition)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (match_id, home_team, away_team, match_time, competition))
        conn.commit()

def update_match_score(match_id, home_score, away_score, status):
    """Update match score and status"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE posted_matches
            SET home_score = %s, away_score = %s, status = %s
            WHERE match_id = %s
        """, (home_score, away_score, status, match_id))
        conn.commit()

def get_match_info(match_id):
    """Get match information including scores"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT home_team, away_team, home_score, away_score, status, competition, match_time
            FROM posted_matches WHERE match_id = %s
        """, (match_id,))
        return cur.fetchone()

def save_vote_message(match_id, msg_id):
    """Save vote message ID"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vote_data (match_id, votes_msg_id)
            VALUES (%s, %s)
            ON CONFLICT (match_id) DO UPDATE SET votes_msg_id = EXCLUDED.votes_msg_id
        """, (match_id, msg_id))
        conn.commit()

def save_live_predictions_message(match_id, msg_id):
    """Save live predictions message ID"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE vote_data
            SET live_predictions_msg_id = %s
            WHERE match_id = %s
        """, (msg_id, match_id))
        conn.commit()

def get_live_predictions_message_id(match_id):
    """Get live predictions message ID"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT live_predictions_msg_id FROM vote_data WHERE match_id = %s", (match_id,))
        result = cur.fetchone()
        return result['live_predictions_msg_id'] if result else None

def get_vote_message_id(match_id):
    """Get vote message ID"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT votes_msg_id, buttons_disabled FROM vote_data WHERE match_id = %s", (match_id,))
        return cur.fetchone()

def disable_vote_buttons(match_id):
    """Mark vote buttons as disabled"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE vote_data SET buttons_disabled = TRUE WHERE match_id = %s", (match_id,))
        conn.commit()

def is_match_processed(match_id):
    """Check if match results were already processed"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_matches WHERE match_id = %s", (match_id,))
        return cur.fetchone() is not None

def mark_match_processed(match_id):
    """Mark match as processed"""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO processed_matches (match_id) VALUES (%s) ON CONFLICT DO NOTHING", (match_id,))
        conn.commit()

# ==== COMPETITION INFO ====
COMPETITION_INFO = {
    "PL": {"name": "Premier League", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "country": "England"},
    "CL": {"name": "Champions League", "flag": "🇪🇺", "country": "Europe"},
    "BL1": {"name": "Bundesliga", "flag": "🇩🇪", "country": "Germany"},
    "PD": {"name": "La Liga", "flag": "🇪🇸", "country": "Spain"},
    "FL1": {"name": "Ligue 1", "flag": "🇫🇷", "country": "France"},
    "SA": {"name": "Serie A", "flag": "🇮🇹", "country": "Italy"}
}

# ==== FOOTBALL API ====
BASE_URL = "https://api.football-data.org/v4/competitions/"
HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
COMPETITIONS = ["PL", "CL", "BL1", "PD", "FL1", "SA"]

last_leaderboard_msg_id = None

# ==== HEALTH CHECK SERVER ====
async def health_check(request):
    """Health check endpoint for Railway"""
    return web.Response(text="Bot is running")

async def start_health_server():
    """Start health check web server"""
    try:
        app = web.Application()
        app.router.add_get('/', health_check)
        app.router.add_get('/health', health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get('PORT', 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"Health check server started on port {port}")
    except Exception as e:
        logger.error(f"Failed to start health check server: {e}")

# ==== VOTES EMBED CREATION ====
def create_live_predictions_embed(match_id, home_team, away_team, match_info=None):
    """Create live predictions embed showing vote breakdown"""
    votes = get_predictions_for_match(match_id)
    total_votes = len(votes['home']) + len(votes['draw']) + len(votes['away'])
    
    if total_votes == 0:
        home_pct = draw_pct = away_pct = 0
    else:
        home_pct = (len(votes['home']) / total_votes) * 100
        draw_pct = (len(votes['draw']) / total_votes) * 100
        away_pct = (len(votes['away']) / total_votes) * 100
    
    if match_info and match_info['status'] == 'FINISHED' and match_info['home_score'] is not None:
        title = "🏆 Final Result"
        description = f"**{home_team} {match_info['home_score']} - {match_info['away_score']} {away_team}**"
        color = discord.Color.gold()
    else:
        title = "📊 Live Predictions"
        description = f"**{home_team}** vs **{away_team}**"
        color = discord.Color.green()
    
    embed = discord.Embed(title=title, description=description, color=color)
    
    embed.add_field(
        name="🔮 Prediction Summary",
        value=f"**{total_votes}** prediction{'s' if total_votes != 1 else ''} made",
        inline=False
    )
    
    home_bar = "█" * int(home_pct / 5) if home_pct > 0 else "░"
    home_users = ", ".join(sorted(votes['home'])) if votes['home'] else "_No predictions yet_"
    embed.add_field(
        name=f"🏠 {home_team} Win",
        value=f"`{home_bar}` **{home_pct:.0f}%** ({len(votes['home'])} votes)\n{home_users}",
        inline=False
    )
    
    draw_bar = "█" * int(draw_pct / 5) if draw_pct > 0 else "░"
    draw_users = ", ".join(sorted(votes['draw'])) if votes['draw'] else "_No predictions yet_"
    embed.add_field(
        name=f"🤝 Draw",
        value=f"`{draw_bar}` **{draw_pct:.0f}%** ({len(votes['draw'])} votes)\n{draw_users}",
        inline=False
    )
    
    away_bar = "█" * int(away_pct / 5) if away_pct > 0 else "░"
    away_users = ", ".join(sorted(votes['away'])) if votes['away'] else "_No predictions yet_"
    embed.add_field(
        name=f"✈️ {away_team} Win",
        value=f"`{away_bar}` **{away_pct:.0f}%** ({len(votes['away'])} votes)\n{away_users}",
        inline=False
    )
    
    if match_info and match_info['status'] == 'FINISHED':
        embed.set_footer(text="Match finished • Points awarded to correct predictions")
    else:
        embed.set_footer(text="Live tracking • Predictions update in real-time")
    
    return embed

# ==== GENERATE MATCH IMAGE ====
async def generate_match_image(home_url, away_url):
    async with aiohttp.ClientSession() as session:
        home_img_bytes, away_img_bytes = None, None
        try:
            if home_url:
                async with session.get(home_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    home_img_bytes = await r.read()
        except Exception as e:
            logger.warning(f"Failed to fetch home crest: {e}")
        try:
            if away_url:
                async with session.get(away_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    away_img_bytes = await r.read()
        except Exception as e:
            logger.warning(f"Failed to fetch away crest: {e}")

    size = (100, 100)
    padding = 40
    width = size[0]*2 + padding
    height = size[1]
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    if home_img_bytes:
        try:
            home = Image.open(BytesIO(home_img_bytes)).convert("RGBA").resize(size)
            img.paste(home, (0, 0), home)
        except Exception as e:
            logger.warning(f"Failed to process home crest image: {e}")
    if away_img_bytes:
        try:
            away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
            img.paste(away, (size[0]+padding, 0), away)
        except Exception as e:
            logger.warning(f"Failed to process away crest image: {e}")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== FETCH MATCHES ====
async def fetch_matches(hours=24):
    """Fetch matches within specified hours window"""
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=hours)
    matches = []
    
    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={future.date()}"
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        comp_name = data.get("competition", {}).get("name", comp)
                        for m in data.get("matches", []):
                            m["competition"]["name"] = comp_name
                            matches.append(m)
                    else:
                        logger.warning(f"Failed to fetch {comp}: {resp.status}")
            except Exception as e:
                logger.error(f"Error fetching {comp}: {e}")
    
    return [m for m in matches if now <= datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00")) <= future]

async def fetch_all_match_results():
    """Fetch all match results and cache them"""
    global match_results_cache, cache_timestamp
    
    if cache_timestamp and (datetime.now(timezone.utc) - cache_timestamp).seconds < 300:
        logger.info("Using cached match results")
        return match_results_cache
    
    results = {}
    failed_comps = []
    
    async with aiohttp.ClientSession() as session:
        for i, comp in enumerate(COMPETITIONS):
            url = f"{BASE_URL}{comp}/matches"
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for m in data.get("matches", []):
                            if m.get("status") == "FINISHED":
                                match_id = str(m["id"])
                                winner = m.get("score", {}).get("winner")
                                home_score = m.get("score", {}).get("fullTime", {}).get("home")
                                away_score = m.get("score", {}).get("fullTime", {}).get("away")
                                
                                if winner:
                                    result_map = {"HOME_TEAM": "home", "AWAY_TEAM": "away", "DRAW": "draw"}
                                    results[match_id] = {
                                        "result": result_map.get(winner, winner.lower()),
                                        "home_score": home_score,
                                        "away_score": away_score
                                    }
                    elif resp.status == 429:
                        logger.warning(f"Rate limited on {comp}! Waiting 60 seconds...")
                        await asyncio.sleep(60)
                        failed_comps.append(comp)
                        continue
                    else:
                        logger.warning(f"Failed to fetch results for {comp}: {resp.status}")
                        failed_comps.append(comp)
            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching {comp}")
                failed_comps.append(comp)
            except Exception as e:
                logger.error(f"Error fetching results for {comp}: {e}")
                failed_comps.append(comp)
            
            if i < len(COMPETITIONS) - 1:
                await asyncio.sleep(2)
    
    if failed_comps:
        logger.warning(f"Failed to fetch competitions: {', '.join(failed_comps)}")
    
    match_results_cache = results
    cache_timestamp = datetime.now(timezone.utc)
    logger.info(f"Fetched {len(results)} match results")
    return results

# ==== VOTE BUTTON ====
class VoteButton(Button):
    def __init__(self, label, category, match_id):
        super().__init__(
            label=label, 
            style=discord.ButtonStyle.primary,
            custom_id=f"vote_{match_id}_{category}"
        )
        self.category = category
        self.match_id = match_id

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to defer interaction: {e}")
            return
        
        try:
            match_info = get_match_info(self.match_id)
            if not match_info:
                await interaction.followup.send("Match not found!", ephemeral=True)
                return
            
            match_time = match_info['match_time']
            if match_time.tzinfo is None:
                match_time = match_time.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            if now >= match_time:
                await interaction.followup.send("Voting for this match has ended!", ephemeral=True)
                return
            
            user = interaction.user
            user_id = str(user.id)
            match_id = self.match_id
            
            existing_prediction = get_user_prediction(user_id, match_id)
            
            if existing_prediction:
                if existing_prediction == self.category:
                    await interaction.followup.send(f"You already voted for **{self.label}**!", ephemeral=True)
                    return
                else:
                    upsert_user(user_id, user.name)
                    update_prediction(user_id, match_id, self.category)
                    
                    if match_info:
                        live_msg_id = get_live_predictions_message_id(match_id)
                        if live_msg_id:
                            try:
                                live_message = await interaction.channel.fetch_message(live_msg_id)
                                embed = create_live_predictions_embed(match_id, match_info['home_team'], match_info['away_team'])
                                await live_message.edit(embed=embed)
                            except Exception as e:
                                logger.error(f"Failed to update live predictions: {e}")
                    
                    await interaction.followup.send(f"Changed your vote to **{self.label}**!", ephemeral=True)
                    return
            
            upsert_user(user_id, user.name)
            add_prediction(user_id, match_id, self.category)
            
            if match_info:
                live_msg_id = get_live_predictions_message_id(match_id)
                if live_msg_id:
                    try:
                        live_message = await interaction.channel.fetch_message(live_msg_id)
                        embed = create_live_predictions_embed(match_id, match_info['home_team'], match_info['away_team'])
                        await live_message.edit(embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to update live predictions: {e}")
            
            await interaction.followup.send(f"You voted for **{self.label}**!", ephemeral=True)
        
        except Exception as e:
            logger.error(f"Error in vote callback: {e}", exc_info=True)
            try:
                await interaction.followup.send("An error occurred. Please try again.", ephemeral=True)
            except:
                pass

# ==== PERSISTENT VOTE VIEW ====
class PersistentVoteView(View):
    def __init__(self, match_id=None):
        super().__init__(timeout=None)
        if match_id:
            self.add_item(VoteButton("🏠 Home", "home", match_id))
            self.add_item(VoteButton("🤝 Draw", "draw", match_id))
            self.add_item(VoteButton("✈️ Away", "away", match_id))

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
        logger.error(f"Channel {MATCH_CHANNEL_ID} not found")
        return
    
    home_team = match['homeTeam']['name']
    away_team = match['awayTeam']['name']
    competition = match['competition'].get('name', 'Unknown')
    comp_code = match['competition'].get('code', '')
    
    comp_info = COMPETITION_INFO.get(comp_code, {"flag": "🌍", "country": "International"})
    
    now = datetime.now(timezone.utc)
    time_until = match_time - now
    days = time_until.days
    hours = time_until.seconds // 3600
    
    if days > 0:
        countdown = f"⏰ in {days} day{'s' if days != 1 else ''}"
    elif hours > 0:
        countdown = f"⏰ in ~{hours + (days * 24)} hours"
    else:
        mins = time_until.seconds // 60
        countdown = f"⏰ in {mins} minutes"
    
    embed = discord.Embed(
        title=f"⚽ {home_team} vs {away_team}",
        description=f"{comp_info['flag']} **{competition}**\n"
                    f"🕐 Kickoff: <t:{kickoff_ts}:f>\n"
                    f"{countdown}",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="📊 Status", value="🟢 Upcoming", inline=True)
    embed.add_field(name="🎯 Points", value="+1 for correct prediction", inline=True)
    
    voting_closes = match_time - timedelta(minutes=10)
    voting_closes_ts = int(voting_closes.timestamp())
    embed.add_field(name="🗳️ Voting", value=f"Closes <t:{voting_closes_ts}:R>", inline=True)
    
    comp_emblem = match['competition'].get('emblem')
    if comp_emblem:
        embed.set_thumbnail(url=comp_emblem)
    
    time_to_vote = voting_closes - now
    hours_to_vote = int(time_to_vote.total_seconds() // 3600)
    embed.set_footer(text=f"⏳ Voting closes 10 minutes before kickoff • You have ~{hours_to_vote} hours to vote!")
    
    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest")
    file = None
    if home_crest or away_crest:
        try:
            image_buffer = await generate_match_image(home_crest, away_crest)
            file = discord.File(fp=image_buffer, filename="match.png")
            embed.set_image(url="attachment://match.png")
        except Exception as e:
            logger.error(f"Failed to generate match image: {e}")
    
    view = PersistentVoteView(match_id)
    
    try:
        match_message = await channel.send(embed=embed, file=file, view=view)
        save_vote_message(match_id, match_message.id)
        
        live_embed = create_live_predictions_embed(match_id, home_team, away_team)
        live_message = await channel.send(embed=live_embed)
        save_live_predictions_message(match_id, live_message.id)
        
        separator_line = discord.Embed(description="───────────────────────────────", color=discord.Color.dark_gray())
        await channel.send(embed=separator_line)
        
        mark_match_posted(match_id, home_team, away_team, match_time, competition)
        logger.info(f"Posted match: {home_team} vs {away_team}")
    except Exception as e:
        logger.error(f"Failed to post match {match_id}: {e}")

# ==== UPDATE MATCH RESULTS ====
@tasks.loop(minutes=15)
async def update_match_results():
    try:
        import time
        start_time = time.time()
        
        global last_leaderboard_msg_id
        leaderboard_changed = False
        
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id, points FROM users")
            previous_points = {row['user_id']: row['points'] for row in cur.fetchall()}
        
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as count FROM posted_matches pm
                WHERE pm.status != 'FINISHED'
                AND pm.match_time < NOW()
                AND NOT EXISTS (
                    SELECT 1 FROM processed_matches proc WHERE proc.match_id = pm.match_id
                )
            """)
            unprocessed_count = cur.fetchone()['count']
        
        if unprocessed_count == 0:
            logger.debug("No pending matches to check")
            return
        
        results = await fetch_all_match_results()
        
        for match_id, result_data in results.items():
            if is_match_processed(match_id):
                continue
            
            result = result_data['result']
            home_score = result_data.get('home_score')
            away_score = result_data.get('away_score')
            
            if home_score is not None and away_score is not None:
                update_match_score(match_id, home_score, away_score, 'FINISHED')
            
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT user_id FROM predictions
                    WHERE match_id = %s AND prediction = %s
                """, (match_id, result))
                winners = cur.fetchall()
            
            for winner in winners:
                add_points(winner['user_id'], 1)
                update_user_streak(winner['user_id'], is_correct=True)
                record_weekly_stat(winner['user_id'], is_correct=True)
                leaderboard_changed = True
            
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT DISTINCT user_id FROM predictions
                    WHERE match_id = %s AND prediction != %s
                """, (match_id, result))
                losers = cur.fetchall()
            
            for loser in losers:
                update_user_streak(loser['user_id'], is_correct=False)
                record_weekly_stat(loser['user_id'], is_correct=False)
            
            mark_match_processed(match_id)
            
            vote_msg = get_vote_message_id(match_id)
            if vote_msg and not vote_msg['buttons_disabled']:
                try:
                    channel = bot.get_channel(MATCH_CHANNEL_ID)
                    votes_message = await channel.fetch_message(vote_msg['votes_msg_id'])
                    
                    disabled_view = View(timeout=None)
                    home_btn = Button(label="🏠 Home", style=discord.ButtonStyle.secondary, disabled=True)
                    draw_btn = Button(label="🤝 Draw", style=discord.ButtonStyle.secondary, disabled=True)
                    away_btn = Button(label="✈️ Away", style=discord.ButtonStyle.secondary, disabled=True)
                    
                    disabled_view.add_item(home_btn)
                    disabled_view.add_item(draw_btn)
                    disabled_view.add_item(away_btn)
                    
                    await votes_message.edit(view=disabled_view)
                    disable_vote_buttons(match_id)
                except discord.errors.NotFound:
                    disable_vote_buttons(match_id)
                except Exception as e:
                    logger.error(f"Failed to update vote buttons for {match_id}: {e}")
            
            match_info = get_match_info(match_id)
            if match_info:
                live_msg_id = get_live_predictions_message_id(match_id)
                if live_msg_id:
                    try:
                        channel = bot.get_channel(MATCH_CHANNEL_ID)
                        live_message = await channel.fetch_message(live_msg_id)
                        embed = create_live_predictions_embed(match_id, match_info['home_team'], 
                                                             match_info['away_team'], match_info)
                        await live_message.edit(embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to update final score for {match_id}: {e}")
            
            if winners:
                await check_streak_milestones(winners)
        
        if leaderboard_changed:
            await update_leaderboard(previous_points)
        
        elapsed = time.time() - start_time
        logger.info(f"update_match_results completed in {elapsed:.2f}s")
        if elapsed > 60:
            logger.warning(f"update_match_results took too long: {elapsed:.2f}s")
    
    except Exception as e:
        logger.error(f"Error in update_match_results: {e}", exc_info=True)

@update_match_results.before_loop
async def before_update_match_results():
    await bot.wait_until_ready()

async def update_leaderboard(previous_points):
    """Update the leaderboard message"""
    global last_leaderboard_msg_id
    
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return
    
    leaderboard = get_leaderboard()
    
    embed = discord.Embed(
        title="🏆 Prediction Leaderboard",
        description="Live rankings updated after each match",
        color=discord.Color.gold()
    )
    
    if len(leaderboard) >= 1:
        medals = ["🥇", "🥈", "🥉"]
        top_3_lines = []
        
        for i in range(min(3, len(leaderboard))):
            entry = leaderboard[i]
            diff = entry['points'] - previous_points.get(entry['user_id'], 0)
            
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) as total FROM predictions WHERE user_id = %s", (entry['user_id'],))
                total_preds = cur.fetchone()['total']
            
            accuracy = (entry['points'] / total_preds * 100) if total_preds > 0 else 0
            gain_text = f" `+{diff}`" if diff > 0 else ""
            streaks = get_user_streaks(entry['user_id'])
            streak_text = f" 🔥 {streaks['current_streak']}" if streaks['current_streak'] >= 3 else ""
            
            top_3_lines.append(
                f"{medals[i]} **{entry['username']}**{gain_text}{streak_text}\n"
                f"└ {entry['points']} pts • {accuracy:.0f}% accuracy"
            )
        
        embed.add_field(name="👑 Top 3", value="\n\n".join(top_3_lines), inline=False)
    
    if len(leaderboard) > 3:
        rest_lines = []
        for i in range(3, min(10, len(leaderboard))):
            entry = leaderboard[i]
            diff = entry['points'] - previous_points.get(entry['user_id'], 0)
            gain_text = f" `+{diff}`" if diff > 0 else ""
            rest_lines.append(f"`{i+1}.` **{entry['username']}** • {entry['points']} pts{gain_text}")
        
        if rest_lines:
            embed.add_field(name="📊 Rankings", value="\n".join(rest_lines), inline=False)
    
    total_players = len(leaderboard)
    total_points_awarded = sum(e['points'] for e in leaderboard)
    
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM predictions")
        total_predictions = cur.fetchone()['total']
    
    embed.set_footer(
        text=f"👥 {total_players} players • 🎯 {total_predictions} predictions • 🏅 {total_points_awarded} points awarded"
    )
    embed.timestamp = datetime.now(timezone.utc)
    
    try:
        if last_leaderboard_msg_id:
            msg = await channel.fetch_message(last_leaderboard_msg_id)
            await msg.edit(embed=embed)
        else:
            msg = await channel.send(embed=embed)
            last_leaderboard_msg_id = msg.id
    except Exception as e:
        logger.error(f"Failed to update leaderboard: {e}")
        msg = await channel.send(embed=embed)
        last_leaderboard_msg_id = msg.id

async def check_streak_milestones(winners):
    """Check if any winners hit streak milestones and notify"""
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
    
    for winner in winners:
        streaks = get_user_streaks(winner['user_id'])
        current = streaks['current_streak']
        
        if current in [3, 5, 10, 15, 20, 25, 30]:
            user_data = get_user(winner['user_id'])
            if user_data:
                try:
                    embed = discord.Embed(
                        title=f"🔥 Streak Alert!",
                        description=f"**{user_data['username']}** is on fire with a **{current}-game win streak!**",
                        color=discord.Color.orange()
                    )
                    await channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Failed to send streak notification: {e}")

# ==== MATCH NOTIFICATIONS ====
@tasks.loop(minutes=5)
async def send_match_notifications():
    try:
        matches = get_upcoming_matches_for_notification()
        
        if not matches:
            return
        
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            return
        
        for match in matches:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT u.user_id, u.username
                    FROM users u
                    WHERE NOT EXISTS (
                        SELECT 1 FROM predictions p 
                        WHERE p.user_id = u.user_id AND p.match_id = %s
                    )
                """, (match['match_id'],))
                non_voters = cur.fetchall()
            
            if non_voters and len(non_voters) > 0:
                mentions = " ".join([f"<@{user['user_id']}>" for user in non_voters[:10]])
                
                embed = discord.Embed(
                    title="⏰ Match Starting Soon!",
                    description=f"**{match['home_team']} vs {match['away_team']}**\nKickoff in ~10 minutes!",
                    color=discord.Color.red()
                )
                embed.add_field(
                    name="🔮 Haven't Voted Yet",
                    value=f"{len(non_voters)} player(s) haven't made predictions!",
                    inline=False
                )
                
                try:
                    await channel.send(content=mentions if len(non_voters) <= 10 else None, embed=embed)
                except Exception as e:
                    logger.error(f"Failed to send notification: {e}")
            
            mark_notification_sent(match['match_id'])
    except Exception as e:
        logger.error(f"Error in send_match_notifications: {e}", exc_info=True)

@send_match_notifications.before_loop
async def before_send_match_notifications():
    await bot.wait_until_ready()

# ==== DISABLE BUTTONS AT KICKOFF ====
@tasks.loop(minutes=5)
async def disable_buttons_at_kickoff():
    try:
        now = datetime.now(timezone.utc)
        
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT vd.match_id, vd.votes_msg_id, pm.home_team, pm.away_team
                FROM vote_data vd
                JOIN posted_matches pm ON vd.match_id = pm.match_id
                WHERE pm.match_time <= %s
                AND pm.match_time > %s
                AND vd.buttons_disabled = FALSE
                AND pm.status != 'FINISHED'
            """, (now, now - timedelta(minutes=10)))
            matches = cur.fetchall()
        
        if not matches:
            return
        
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            return
        
        for match in matches:
            try:
                votes_message = await channel.fetch_message(match['votes_msg_id'])
                
                disabled_view = View(timeout=None)
                home_btn = Button(label="🏠 Home", style=discord.ButtonStyle.secondary, disabled=True)
                draw_btn = Button(label="🤝 Draw", style=discord.ButtonStyle.secondary, disabled=True)
                away_btn = Button(label="✈️ Away", style=discord.ButtonStyle.secondary, disabled=True)
                
                disabled_view.add_item(home_btn)
                disabled_view.add_item(draw_btn)
                disabled_view.add_item(away_btn)
                
                await votes_message.edit(view=disabled_view)
                disable_vote_buttons(match['match_id'])
                logger.info(f"Disabled buttons for started match: {match['home_team']} vs {match['away_team']}")
            except discord.errors.NotFound:
                disable_vote_buttons(match['match_id'])
            except Exception as e:
                logger.error(f"Failed to disable buttons for {match['match_id']}: {e}")
    except Exception as e:
        logger.error(f"Error in disable_buttons_at_kickoff: {e}", exc_info=True)

@disable_buttons_at_kickoff.before_loop
async def before_disable_buttons_at_kickoff():
    await bot.wait_until_ready()

# ==== WEEKLY RECAP ====
@tasks.loop(hours=24)
async def weekly_recap():
    try:
        now = datetime.now(timezone.utc)
        
        if now.weekday() != 0:
            return
        
        last_week_stats = get_last_week_stats()
        
        if not last_week_stats:
            return
        
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            return
        
        embed = discord.Embed(
            title="📊 Weekly Recap",
            description="Last week's prediction results are in!",
            color=discord.Color.purple()
        )
        
        top_5 = last_week_stats[:5]
        top_text = []
        for i, user in enumerate(top_5):
            accuracy = (user['correct'] / user['total'] * 100) if user['total'] > 0 else 0
            medals = ["🥇", "🥈", "🥉", "4.", "5."]
            medal = medals[i] if i < len(medals) else f"{i+1}."
            top_text.append(f"{medal} **{user['username']}** — {user['correct']}/{user['total']} ({accuracy:.0f}%)")
        
        embed.add_field(name="🏆 Top Predictors", value="\n".join(top_text), inline=False)
        
        total_predictions = sum(u['total'] for u in last_week_stats)
        total_correct = sum(u['correct'] for u in last_week_stats)
        overall_accuracy = (total_correct / total_predictions * 100) if total_predictions > 0 else 0
        
        embed.add_field(
            name="📈 Community Stats",
            value=f"**Total Predictions:** {total_predictions}\n"
                  f"**Correct:** {total_correct}\n"
                  f"**Overall Accuracy:** {overall_accuracy:.1f}%",
            inline=False
        )
        
        for user_stat in last_week_stats:
            if user_stat['total'] >= 3:
                try:
                    user = await bot.fetch_user(int(user_stat['user_id']))
                    accuracy = (user_stat['correct'] / user_stat['total'] * 100)
                    
                    dm_embed = discord.Embed(
                        title="📊 Your Week in Review",
                        description=f"Here's how you did last week!",
                        color=discord.Color.blue()
                    )
                    dm_embed.add_field(
                        name="🎯 Your Stats",
                        value=f"**Correct:** {user_stat['correct']}/{user_stat['total']}\n"
                              f"**Accuracy:** {accuracy:.1f}%",
                        inline=False
                    )
                    
                    rank = next((i+1 for i, u in enumerate(last_week_stats) if u['user_id'] == user_stat['user_id']), None)
                    if rank:
                        dm_embed.add_field(
                            name="🏅 Weekly Rank",
                            value=f"#{rank} out of {len(last_week_stats)} players",
                            inline=False
                        )
                    
                    await user.send(embed=dm_embed)
                except Exception as e:
                    logger.error(f"Failed to send DM to user {user_stat['user_id']}: {e}")
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send weekly recap: {e}")
    except Exception as e:
        logger.error(f"Error in weekly_recap: {e}", exc_info=True)

@weekly_recap.before_loop
async def before_weekly_recap():
    await bot.wait_until_ready()

# ==== ADMIN COMMANDS ====
@bot.tree.command(name="admin", description="Admin commands")
@app_commands.describe(
    action="Admin action to perform",
    user="Target user (for setpoints/addpoints)",
    points="Points to set/add (for setpoints/addpoints)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="📦 Backup Database", value="backup"),
    app_commands.Choice(name="🎯 Set Points", value="setpoints"),
    app_commands.Choice(name="➕ Add Points", value="addpoints"),
    app_commands.Choice(name="🔧 Fix Database", value="fixdb"),
    app_commands.Choice(name="⚽ Force Fetch Matches", value="forcefetch"),
    app_commands.Choice(name="📊 Backfill Scores", value="backfillscores"),
    app_commands.Choice(name="✅ Check Database", value="checkdb"),
    app_commands.Choice(name="🔄 Repost Matches", value="repostmatches"),
])
async def admin_command(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    user: discord.Member = None,
    points: int = None
):
    """Unified admin command with action parameter"""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only", ephemeral=True)
        return
    
    action_value = action.value
    
    # BACKUP
    if action_value == "backup":
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id, username, points FROM users")
            users = cur.fetchall()
            cur.execute("SELECT user_id, match_id, prediction FROM predictions")
            predictions = cur.fetchall()
        
        backup_data = {
            "users": [dict(u) for u in users],
            "predictions": [dict(p) for p in predictions],
            "backup_time": datetime.now(timezone.utc).isoformat()
        }
        
        file_content = json.dumps(backup_data, indent=2)
        file = discord.File(StringIO(file_content), filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        await interaction.response.send_message("📦 Database backup:", file=file, ephemeral=True)
    
    # SET POINTS
    elif action_value == "setpoints":
        if not user or points is None:
            await interaction.response.send_message("❌ Please provide both user and points parameters", ephemeral=True)
            return
        
        user_id = str(user.id)
        upsert_user(user_id, user.name)
        set_user_points(user_id, points)
        await interaction.response.send_message(f"✅ Set {user.name}'s points to {points}", ephemeral=True)
    
    # ADD POINTS
    elif action_value == "addpoints":
        if not user or points is None:
            await interaction.response.send_message("❌ Please provide both user and points parameters", ephemeral=True)
            return
        
        user_id = str(user.id)
        upsert_user(user_id, user.name)
        add_points(user_id, points)
        current_user = get_user(user_id)
        await interaction.response.send_message(f"✅ Added {points} points to {user.name}. New total: {current_user['points']}", ephemeral=True)
    
    # FIX DATABASE
    elif action_value == "fixdb":
        await interaction.response.defer(ephemeral=True)
        try:
            init_db()
            await interaction.followup.send("✅ Database schema updated successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
    
    # FORCE FETCH
    elif action_value == "forcefetch":
        await interaction.response.defer(ephemeral=True)
        upcoming = await fetch_matches(hours=48)
        
        if not upcoming:
            await interaction.followup.send(f"⚠️ No matches found in next 48 hours.", ephemeral=True)
            return
        
        posted_count = 0
        for match in upcoming:
            match_id = str(match["id"])
            if not is_match_posted(match_id):
                await post_match(match)
                posted_count += 1
                await asyncio.sleep(1)
        
        await interaction.followup.send(f"✅ Found {len(upcoming)} matches. Posted {posted_count} new matches.", ephemeral=True)
    
    # BACKFILL SCORES
    elif action_value == "backfillscores":
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("⏳ Fetching match results from API... This may take a minute.", ephemeral=True)
        
        results = await fetch_all_match_results()
        updated = 0
        
        for match_id, result_data in results.items():
            if result_data.get('home_score') is not None:
                update_match_score(match_id, result_data['home_score'], result_data['away_score'], 'FINISHED')
                updated += 1
        
        await interaction.followup.send(f"✅ Updated {updated} match scores from API.", ephemeral=True)
    
    # CHECK DATABASE
    elif action_value == "checkdb":
        with db_connection() as conn:
            cur = conn.cursor()
            
            cur.execute("SELECT COUNT(*) as count FROM posted_matches WHERE home_score IS NOT NULL")
            finished = cur.fetchone()['count']
            
            cur.execute("SELECT COUNT(*) as count FROM posted_matches")
            total = cur.fetchone()['count']
            
            cur.execute("SELECT COUNT(*) as count FROM processed_matches")
            processed = cur.fetchone()['count']
            
            cur.execute("SELECT COUNT(*) as count FROM predictions")
            total_preds = cur.fetchone()['count']
            
            await interaction.response.send_message(
                f"**📊 Database Status:**\n"
                f"Total matches posted: {total}\n"
                f"Matches with scores: {finished}\n"
                f"Processed matches: {processed}\n"
                f"Total predictions: {total_preds}",
                ephemeral=True
            )
    
    # REPOST MATCHES
    elif action_value == "repostmatches":
        await interaction.response.defer(ephemeral=True)
        
        now = datetime.now(timezone.utc)
        
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT match_id, home_team, away_team, match_time, competition
                FROM posted_matches
                WHERE match_time > %s AND status != 'FINISHED'
                ORDER BY match_time ASC
            """, (now,))
            matches = cur.fetchall()
        
        if not matches:
            await interaction.followup.send("⚠️ No upcoming matches found in database.", ephemeral=True)
            return
        
        await interaction.followup.send("⏳ Fetching match details from API...", ephemeral=True)
        
        api_matches = {}
        async with aiohttp.ClientSession() as session:
            for comp in COMPETITIONS:
                url = f"{BASE_URL}{comp}/matches"
                try:
                    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for m in data.get("matches", []):
                                api_matches[str(m["id"])] = m
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error fetching {comp}: {e}")
        
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("❌ Match channel not found!", ephemeral=True)
            return
        
        matches_by_comp = {}
        for match in matches:
            comp = match['competition'] or 'Unknown'
            if comp not in matches_by_comp:
                matches_by_comp[comp] = []
            matches_by_comp[comp].append(match)
        
        reposted = 0
        
        for competition, comp_matches in matches_by_comp.items():
            comp_info = {"flag": "🌍", "country": "International"}
            for code, info in COMPETITION_INFO.items():
                if info['name'] in competition:
                    comp_info = info
                    break
            
            separator_embed = discord.Embed(
                title=f"{comp_info['flag']} {competition}",
                description=f"**{len(comp_matches)}** upcoming match{'es' if len(comp_matches) != 1 else ''}",
                color=discord.Color.dark_grey()
            )
            separator_embed.set_footer(text="─" * 50)
            
            await channel.send(embed=separator_embed)
            await asyncio.sleep(0.5)
            
            for match in comp_matches:
                match_id = match['match_id']
                match_time = match['match_time']
                if match_time.tzinfo is None:
                    match_time = match_time.replace(tzinfo=timezone.utc)
                
                kickoff_ts = int(match_time.timestamp())
                home_team = match['home_team']
                away_team = match['away_team']
                competition = match['competition'] or 'Unknown'
                
                time_until = match_time - now
                days = time_until.days
                hours = time_until.seconds // 3600
                
                if days > 0:
                    countdown = f"⏰ in {days} day{'s' if days != 1 else ''}"
                elif hours > 0:
                    countdown = f"⏰ in ~{hours + (days * 24)} hours"
                else:
                    mins = time_until.seconds // 60
                    countdown = f"⏰ in {mins} minutes"
                
                comp_info = {"flag": "🌍", "country": "International"}
                for code, info in COMPETITION_INFO.items():
                    if info['name'] in competition:
                        comp_info = info
                        break
                
                embed = discord.Embed(
                    title=f"⚽ {home_team} vs {away_team}",
                    description=f"{comp_info['flag']} **{competition}**\n"
                                f"🕐 Kickoff: <t:{kickoff_ts}:f>\n"
                                f"{countdown}",
                    color=discord.Color.blue()
                )
                
                embed.add_field(name="📊 Status", value="🟢 Upcoming", inline=True)
                embed.add_field(name="🎯 Points", value="+1 for correct prediction", inline=True)
                
                voting_closes = match_time - timedelta(minutes=10)
                voting_closes_ts = int(voting_closes.timestamp())
                embed.add_field(name="🗳️ Voting", value=f"Closes <t:{voting_closes_ts}:R>", inline=True)
                
                time_to_vote = voting_closes - now
                hours_to_vote = int(time_to_vote.total_seconds() // 3600)
                embed.set_footer(text=f"⏳ Voting closes 10 minutes before kickoff • You have ~{hours_to_vote} hours to vote!")
                
                file = None
                api_match = api_matches.get(match_id)
                if api_match:
                    home_crest = api_match["homeTeam"].get("crest")
                    away_crest = api_match["awayTeam"].get("crest")
                    comp_emblem = api_match['competition'].get('emblem')
                    
                    if comp_emblem:
                        embed.set_thumbnail(url=comp_emblem)
                    
                    if home_crest or away_crest:
                        try:
                            image_buffer = await generate_match_image(home_crest, away_crest)
                            file = discord.File(fp=image_buffer, filename="match.png")
                            embed.set_image(url="attachment://match.png")
                        except Exception as e:
                            logger.error(f"Failed to generate match image for {match_id}: {e}")
                
                view = PersistentVoteView(match_id)
                
                try:
                    match_message = await channel.send(embed=embed, file=file, view=view)
                    save_vote_message(match_id, match_message.id)
                    
                    live_embed = create_live_predictions_embed(match_id, home_team, away_team)
                    live_message = await channel.send(embed=live_embed)
                    save_live_predictions_message(match_id, live_message.id)
                    
                    separator_line = discord.Embed(description="───────────────────────────────", color=discord.Color.dark_gray())
                    await channel.send(embed=separator_line)
                    
                    reposted += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Failed to repost match {match_id}: {e}")
        
        await interaction.followup.send(f"✅ Reposted {reposted} upcoming matches with crests.", ephemeral=True)

# ==== USER COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches")
async def matches_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    matches = await fetch_matches()
    if not matches:
        await interaction.followup.send("⚠️ No upcoming matches in the next 24 hours.", ephemeral=True)
        return
    
    league_dict = {}
    for m in matches:
        league_name = m["competition"].get("name", "Unknown League")
        league_dict.setdefault(league_name, []).append(m)
    
    for league_name, league_matches in league_dict.items():
        comp_code = league_matches[0]['competition'].get('code', '')
        comp_info = COMPETITION_INFO.get(comp_code, {"flag": "🌍", "country": "International"})
        
        separator_embed = discord.Embed(
            title=f"{comp_info['flag']} {league_name}",
            description=f"**{len(league_matches)}** upcoming match{'es' if len(league_matches) != 1 else ''}",
            color=discord.Color.dark_grey()
        )
        separator_embed.set_footer(text="─" * 50)
        
        await interaction.channel.send(embed=separator_embed)
        await asyncio.sleep(0.5)
        
        for m in league_matches:
            await post_match(m)
            await asyncio.sleep(0.5)
    
    await interaction.followup.send("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    leaderboard = get_leaderboard()
    if not leaderboard:
        await interaction.response.send_message("⚠️ Leaderboard is empty.", ephemeral=True)
        return
    
    prediction_counts = {}
    with db_connection() as conn:
        cur = conn.cursor()
        for entry in leaderboard:
            cur.execute("SELECT COUNT(*) as count FROM predictions WHERE user_id = %s", (entry['user_id'],))
            prediction_counts[entry['user_id']] = cur.fetchone()['count']
    
    medals = ["🥇", "🥈", "🥉"]
    
    embed = discord.Embed(
        title="🏆 Prediction Leaderboard",
        description="Top predictors of the season",
        color=discord.Color.gold()
    )
    
    top_3 = []
    for i, entry in enumerate(leaderboard[:3]):
        pred_count = prediction_counts.get(entry['user_id'], 0)
        accuracy = (entry['points'] / pred_count * 100) if pred_count > 0 else 0
        streaks = get_user_streaks(entry['user_id'])
        streak_text = f" 🔥{streaks['current_streak']}" if streaks['current_streak'] >= 3 else ""
        top_3.append(f"{medals[i]} **{entry['username']}**{streak_text}\n**{entry['points']} pts** • {accuracy:.0f}% accuracy • {pred_count} predictions")
    
    embed.add_field(name="👑 Top 3", value="\n\n".join(top_3), inline=False)
    
    if len(leaderboard) > 3:
        rest = []
        for i, entry in enumerate(leaderboard[3:10], start=4):
            pred_count = prediction_counts.get(entry['user_id'], 0)
            rest.append(f"`{i}.` **{entry['username']}** • {entry['points']} pts")
        
        if rest:
            embed.add_field(name="📊 Rankings", value="\n".join(rest), inline=False)
    
    total_players = len(leaderboard)
    total_predictions = sum(prediction_counts.values())
    embed.set_footer(text=f"{total_players} active players • {total_predictions} total predictions made")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ticket", description="Show your recent predictions summary")
async def ticket_command(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    target_user = user or interaction.user
    user_id = str(target_user.id)
    
    user_data = get_user(user_id)
    if not user_data:
        await interaction.followup.send(f"⚠️ {target_user.name} has no predictions yet.", ephemeral=True)
        return
    
    stats = get_user_stats(user_id)
    streaks = get_user_streaks(user_id)
    
    header_embed = discord.Embed(
        title=f"🎫 {target_user.name}'s Prediction Ticket",
        description="Quick summary of your predictions",
        color=discord.Color.blue()
    )
    header_embed.set_thumbnail(url=target_user.display_avatar.url)
    
    accuracy_bar = "█" * int(stats['accuracy'] / 5) if stats['accuracy'] > 0 else "░"
    streak_emoji = "🔥" if streaks['current_streak'] >= 3 else "📈"
    header_embed.add_field(
        name="📊 Performance",
        value=f"**Points:** {user_data['points']}\n"
              f"**Accuracy:** `{accuracy_bar}` {stats['accuracy']:.1f}%\n"
              f"{streak_emoji} **Streak:** {streaks['current_streak']}",
        inline=True
    )
    header_embed.add_field(
        name="🎯 Record",
        value=f"**Correct:** {stats['correct']}\n"
              f"**Total:** {stats['total']}\n"
              f"**Best Streak:** {streaks['best_streak']}",
        inline=True
    )
    
    header_embed.add_field(
        name="📋 View Details",
        value="Use `/upcoming` to see future matches\nUse `/history` to see past results",
        inline=False
    )
    
    await interaction.followup.send(embed=header_embed, ephemeral=True)

@bot.tree.command(name="upcoming", description="Show all your upcoming predictions")
async def upcoming_command(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    
    target_user = user or interaction.user
    user_id = str(target_user.id)
    
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.match_id, p.prediction, pm.home_team, pm.away_team, pm.match_time,
                   pm.competition, pm.status, pm.home_score, pm.away_score
            FROM predictions p
            LEFT JOIN posted_matches pm ON p.match_id = pm.match_id
            WHERE p.user_id = %s
            AND pm.status != 'FINISHED'
            ORDER BY pm.match_time ASC
        """, (user_id,))
        predictions = cur.fetchall()
    
    if not predictions:
        await interaction.followup.send("⚠️ No upcoming or ongoing predictions.", ephemeral=True)
        return
    
    now = datetime.now(timezone.utc)
    
    ongoing = []
    upcoming = []
    
    for pred in predictions:
        if pred['match_time']:
            match_time = pred['match_time']
            if match_time.tzinfo is None:
                match_time = match_time.replace(tzinfo=timezone.utc)
            
            if match_time <= now:
                ongoing.append(pred)
            else:
                upcoming.append(pred)
    
    embeds_to_send = []
    
    if ongoing:
        ongoing_embed = discord.Embed(
            title="⚽ Live Matches",
            description=f"{len(ongoing)} match{'es' if len(ongoing) != 1 else ''} in progress",
            color=discord.Color.red()
        )
        
        for pred in ongoing[:15]:
            pred_emoji = {"home": "🏠", "draw": "🤝", "away": "✈️"}.get(pred['prediction'], "🔮")
            comp_short = pred['competition'][:20] if pred['competition'] else "Unknown"
            
            if pred['home_score'] is not None and pred['away_score'] is not None:
                score_text = f"**{pred['home_score']}-{pred['away_score']}** (Live)"
            else:
                score_text = "In Progress"
            
            ongoing_embed.add_field(
                name=f"🔴 {pred['home_team']} vs {pred['away_team']}",
                value=f"{pred_emoji} Predicted: **{pred['prediction'].capitalize()}** • {comp_short}\n{score_text}",
                inline=False
            )
        
        embeds_to_send.append(ongoing_embed)
    
    if upcoming:
        for i in range(0, len(upcoming), 20):
            chunk = upcoming[i:i+20]
            upcoming_embed = discord.Embed(
                title=f"🔮 Upcoming Predictions ({i+1}-{min(i+20, len(upcoming))} of {len(upcoming)})",
                color=discord.Color.blue()
            )
            
            for pred in chunk:
                match_time = pred['match_time']
                if match_time.tzinfo is None:
                    match_time = match_time.replace(tzinfo=timezone.utc)
                
                time_until = match_time - now
                if time_until.total_seconds() > 0:
                    status = f"⏰ <t:{int(match_time.timestamp())}:R>"
                else:
                    status = "Starting soon"
                
                pred_emoji = {"home": "🏠", "draw": "🤝", "away": "✈️"}.get(pred['prediction'], "🔮")
                comp_short = pred['competition'][:20] if pred['competition'] else "Unknown"
                
                upcoming_embed.add_field(
                    name=f"{pred['home_team']} vs {pred['away_team']}",
                    value=f"{pred_emoji} **{pred['prediction'].capitalize()}** • {comp_short}\n{status}",
                    inline=False
                )
            
            embeds_to_send.append(upcoming_embed)
    
    for embed in embeds_to_send:
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="history", description="Show your recent match results")
async def history_command(interaction: discord.Interaction, user: discord.Member = None, days: int = 7):
    await interaction.response.defer(ephemeral=True)
    
    target_user = user or interaction.user
    user_id = str(target_user.id)
    
    lookback = datetime.now(timezone.utc) - timedelta(days=days)
    
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.match_id, p.prediction, pm.home_team, pm.away_team, pm.match_time,
                   pm.home_score, pm.away_score, pm.status, pm.competition
            FROM predictions p
            LEFT JOIN posted_matches pm ON p.match_id = pm.match_id
            WHERE p.user_id = %s
            AND pm.home_score IS NOT NULL
            AND pm.match_time >= %s
            ORDER BY pm.match_time DESC
        """, (user_id, lookback))
        predictions = cur.fetchall()
    
    if not predictions:
        await interaction.followup.send(f"⚠️ No finished matches in the last {days} days.", ephemeral=True)
        return
    
    total_correct = 0
    for i in range(0, len(predictions), 20):
        chunk = predictions[i:i+20]
        embed = discord.Embed(
            title=f"🏆 Match History ({i+1}-{min(i+20, len(predictions))} of {len(predictions)})",
            description=f"Results from last {days} days",
            color=discord.Color.gold()
        )
        
        chunk_correct = 0
        for pred in chunk:
            if pred['home_score'] > pred['away_score']:
                actual_result = 'home'
            elif pred['away_score'] > pred['home_score']:
                actual_result = 'away'
            else:
                actual_result = 'draw'
            
            is_correct = actual_result == pred['prediction']
            if is_correct:
                chunk_correct += 1
                total_correct += 1
            
            result_emoji = "✅" if is_correct else "❌"
            pred_emoji = {"home": "🏠", "draw": "🤝", "away": "✈️"}.get(pred['prediction'], "🔮")
            
            embed.add_field(
                name=f"{result_emoji} {pred['home_team']} {pred['home_score']}-{pred['away_score']} {pred['away_team']}",
                value=f"{pred_emoji} Predicted: **{pred['prediction'].capitalize()}**",
                inline=False
            )
        
        chunk_accuracy = (chunk_correct / len(chunk) * 100) if chunk else 0
        embed.set_footer(text=f"This page: {chunk_correct}/{len(chunk)} ({chunk_accuracy:.0f}%)")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    total_accuracy = (total_correct / len(predictions) * 100)
    summary = discord.Embed(
        title="📊 Summary",
        description=f"**Overall:** {total_correct}/{len(predictions)} correct ({total_accuracy:.0f}%)",
        color=discord.Color.green()
    )
    await interaction.followup.send(embed=summary, ephemeral=True)

@bot.tree.command(name="mystats", description="Show your detailed statistics")
async def mystats_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_data = get_user(user_id)
    
    if not user_data:
        await interaction.response.send_message("⚠️ You haven't made any predictions yet!", ephemeral=True)
        return
    
    stats = get_user_stats(user_id)
    streaks = get_user_streaks(user_id)
    
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT pm.competition, COUNT(*) as total,
                   SUM(CASE WHEN proc.match_id IS NOT NULL THEN 1 ELSE 0 END) as finished
            FROM predictions p
            LEFT JOIN posted_matches pm ON p.match_id = pm.match_id
            LEFT JOIN processed_matches proc ON p.match_id = proc.match_id
            WHERE p.user_id = %s AND pm.competition IS NOT NULL
            GROUP BY pm.competition
            ORDER BY total DESC
        """, (user_id,))
        comp_breakdown = cur.fetchall()
    
    embed = discord.Embed(
        title=f"📊 {interaction.user.name}'s Statistics",
        description="Your prediction performance summary",
        color=discord.Color.blue()
    )
    
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    accuracy_bar = "█" * int(stats['accuracy'] / 5) if stats['accuracy'] > 0 else "░"
    embed.add_field(
        name="🎯 Overall Performance",
        value=f"**Points:** {user_data['points']}\n"
              f"**Predictions:** {stats['total']}\n"
              f"**Correct:** {stats['correct']}\n"
              f"**Accuracy:** `{accuracy_bar}` {stats['accuracy']:.1f}%",
        inline=False
    )
    
    streak_emoji = "🔥" if streaks['current_streak'] >= 3 else "📈"
    streak_display = f"**{streaks['current_streak']}**" if streaks['current_streak'] >= 3 else streaks['current_streak']
    embed.add_field(
        name=f"{streak_emoji} Streaks",
        value=f"**Current:** {streak_display}\n"
              f"**Best:** {streaks['best_streak']}",
        inline=True
    )
    
    leaderboard = get_leaderboard()
    position = next((i+1 for i, entry in enumerate(leaderboard) if entry['user_id'] == user_id), None)
    
    if position:
        rank_emoji = "👑" if position == 1 else "🏅" if position <= 3 else "📊"
        embed.add_field(
            name=f"{rank_emoji} Rank",
            value=f"**#{position}** of {len(leaderboard)}",
            inline=True
        )
    
    if comp_breakdown:
        comp_text = []
        for comp in comp_breakdown[:5]:
            comp_text.append(f"**{comp['competition']}:** {comp['total']} predictions")
        
        embed.add_field(
            name="🏆 By Competition",
            value="\n".join(comp_text),
            inline=False
        )
    
    embed.set_footer(text="Keep predicting to climb the leaderboard!")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="unpick", description="Delete your prediction for a match")
async def unpick_command(interaction: discord.Interaction, match_id: str):
    user_id = str(interaction.user.id)
    
    match_info = get_match_info(match_id)
    if not match_info:
        await interaction.response.send_message("❌ Match not found!", ephemeral=True)
        return
    
    match_time = match_info['match_time']
    if match_time.tzinfo is None:
        match_time = match_time.replace(tzinfo=timezone.utc)
    
    if datetime.now(timezone.utc) >= match_time:
        await interaction.response.send_message("❌ Can't delete prediction - match has already started!", ephemeral=True)
        return
    
    prediction = get_user_prediction(user_id, match_id)
    if not prediction:
        await interaction.response.send_message("❌ You haven't made a prediction for this match!", ephemeral=True)
        return
    
    if delete_prediction(user_id, match_id):
        live_msg_id = get_live_predictions_message_id(match_id)
        if live_msg_id:
            try:
                channel = bot.get_channel(MATCH_CHANNEL_ID)
                live_message = await channel.fetch_message(live_msg_id)
                embed = create_live_predictions_embed(match_id, match_info['home_team'], match_info['away_team'])
                await live_message.edit(embed=embed)
            except Exception as e:
                logger.error(f"Failed to update live predictions: {e}")
        
        await interaction.response.send_message(
            f"✅ Deleted your **{prediction.capitalize()}** prediction for {match_info['home_team']} vs {match_info['away_team']}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ Failed to delete prediction. Try again!", ephemeral=True)

@bot.tree.command(name="compare", description="Compare stats with another user")
async def compare_command(interaction: discord.Interaction, user: discord.Member):
    user1_id = str(interaction.user.id)
    user2_id = str(user.id)
    
    user1_data = get_user(user1_id)
    user2_data = get_user(user2_id)
    
    if not user1_data:
        await interaction.response.send_message("❌ You haven't made any predictions yet!", ephemeral=True)
        return
    
    if not user2_data:
        await interaction.response.send_message(f"❌ {user.name} hasn't made any predictions yet!", ephemeral=True)
        return
    
    stats1 = get_user_stats(user1_id)
    stats2 = get_user_stats(user2_id)
    
    embed = discord.Embed(
        title=f"⚔️ {interaction.user.name} vs {user.name}",
        color=discord.Color.purple()
    )
    
    points_diff = user1_data['points'] - user2_data['points']
    if points_diff > 0:
        points_text = f"**{interaction.user.name}** leads by {points_diff} pts"
    elif points_diff < 0:
        points_text = f"**{user.name}** leads by {abs(points_diff)} pts"
    else:
        points_text = "**Tied!**"
    
    embed.add_field(
        name="🏆 Points",
        value=f"{interaction.user.name}: {user1_data['points']}\n"
              f"{user.name}: {user2_data['points']}\n"
              f"{points_text}",
        inline=False
    )
    
    embed.add_field(
        name="🎯 Accuracy",
        value=f"{interaction.user.name}: {stats1['accuracy']:.1f}% ({stats1['correct']}/{stats1['total']})\n"
              f"{user.name}: {stats2['accuracy']:.1f}% ({stats2['correct']}/{stats2['total']})",
        inline=False
    )
    
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                p1.match_id,
                p1.prediction as user1_pred,
                p2.prediction as user2_pred,
                pm.home_team,
                pm.away_team,
                pm.home_score,
                pm.away_score,
                pm.status
            FROM predictions p1
            INNER JOIN predictions p2 ON p1.match_id = p2.match_id
            LEFT JOIN posted_matches pm ON p1.match_id = pm.match_id
            WHERE p1.user_id = %s AND p2.user_id = %s
            AND pm.status = 'FINISHED' AND pm.home_score IS NOT NULL
            ORDER BY pm.match_time DESC
            LIMIT 5
        """, (user1_id, user2_id))
        head_to_head = cur.fetchall()
    
    if head_to_head:
        h2h_text = []
        user1_wins = 0
        user2_wins = 0
        
        for match in head_to_head:
            if match['home_score'] > match['away_score']:
                actual = 'home'
            elif match['away_score'] > match['home_score']:
                actual = 'away'
            else:
                actual = 'draw'
            
            user1_correct = match['user1_pred'] == actual
            user2_correct = match['user2_pred'] == actual
            
            if user1_correct and not user2_correct:
                user1_wins += 1
                result = f"✅ {interaction.user.name}"
            elif user2_correct and not user1_correct:
                user2_wins += 1
                result = f"✅ {user.name}"
            elif user1_correct and user2_correct:
                result = "🤝 Both"
            else:
                result = "❌ Neither"
            
            h2h_text.append(f"{match['home_team']} {match['home_score']}-{match['away_score']} {match['away_team']}: {result}")
        
        embed.add_field(
            name=f"🥊 Head-to-Head (Last 5 Shared Matches)",
            value=f"**{interaction.user.name} wins:** {user1_wins}\n"
                  f"**{user.name} wins:** {user2_wins}\n\n"
                  + "\n".join(h2h_text[:3]),
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

# ==== SCHEDULER ====
scheduler = AsyncIOScheduler()

async def daily_fetch_matches():
    try:
        matches = await fetch_matches()
        for m in matches:
            await post_match(m)
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Error in daily_fetch_matches: {e}", exc_info=True)

scheduler.add_job(lambda: bot.loop.create_task(daily_fetch_matches()), "cron", hour=6, minute=0)

# ==== STARTUP ====
@bot.event
async def on_ready():
    try:
        # Initialize database pool FIRST
        init_db_pool()
        
        # Then initialize tables
        init_db()
        
        # Reconstruct persistent views for active matches
        try:
            with db_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT DISTINCT match_id FROM vote_data
                    WHERE buttons_disabled = FALSE
                """)
                active_matches = cur.fetchall()
            
            for match in active_matches:
                bot.add_view(PersistentVoteView(match['match_id']))
            
            logger.info(f"Reconstructed {len(active_matches)} active vote views")
        except Exception as e:
            logger.error(f"Error reconstructing views: {e}")
        
        await bot.tree.sync()
        
        # Start health check server for Railway
        bot.loop.create_task(start_health_server())
        
        # Start tasks
        if not update_match_results.is_running():
            update_match_results.start()
        if not send_match_notifications.is_running():
            send_match_notifications.start()
        if not weekly_recap.is_running():
            weekly_recap.start()
        if not disable_buttons_at_kickoff.is_running():
            disable_buttons_at_kickoff.start()
        
        scheduler.start()
        
        logger.info(f"✅ Bot logged in as {bot.user}")
        logger.info(f"✅ Serving {len(bot.guilds)} guild(s)")
    
    except Exception as e:
        logger.error(f"Error in on_ready: {e}", exc_info=True)

# ==== RUN BOT ====
if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
