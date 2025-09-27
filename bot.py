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
vote_data = {}  # match_id: {"home": set(), "draw": set(), "away": set(), "votes_msg_id": int, "locked_users": set(), "buttons_disabled": bool, "match_msg_id": int, "kickoff_time": datetime, "home_team": str, "away_team": str}
last_leaderboard_msg_id = None
active_views = {}  # Store active views by match_id

# ==== VOTES EMBED CREATION ====
def create_votes_embed(match_id, match_result=None):
    if match_id not in vote_data:
        return discord.Embed(title="Votes", description="No vote data found", color=discord.Color.red())
        
    votes_dict = vote_data[match_id]
    home_team = votes_dict.get("home_team", "Home")
    away_team = votes_dict.get("away_team", "Away")
    
    embed = discord.Embed(title="Current Votes", color=discord.Color.green())
    
    # Home votes
    home_voters = sorted(votes_dict["home"]) if votes_dict["home"] else []
    home_value = "\n".join(home_voters) if home_voters else "No votes yet"
    if match_result == "home":
        home_value += "\n✅ **WINNER!**"
    embed.add_field(name=f"🏠 {home_team}", value=home_value, inline=True)
    
    # Draw votes  
    draw_voters = sorted(votes_dict["draw"]) if votes_dict["draw"] else []
    draw_value = "\n".join(draw_voters) if draw_voters else "No votes yet"
    if match_result == "draw":
        draw_value += "\n✅ **WINNER!**"
    embed.add_field(name="🤝 Draw", value=draw_value, inline=True)
    
    # Away votes
    away_voters = sorted(votes_dict["away"]) if votes_dict["away"] else []
    away_value = "\n".join(away_voters) if away_voters else "No votes yet"
    if match_result == "away":
        away_value += "\n✅ **WINNER!**"
    embed.add_field(name=f"✈️ {away_team}", value=away_value, inline=True)
    
    return embed

# ==== GENERATE MATCH IMAGE ====
async def generate_match_image(home_url, away_url):
    async with aiohttp.ClientSession() as session:
        home_img_bytes, away_img_bytes = None, None
        try:
            if home_url:
                async with session.get(home_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        home_img_bytes = await r.read()
        except: 
            pass
        try:
            if away_url:
                async with session.get(away_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        away_img_bytes = await r.read()
        except: 
            pass

    size = (100, 100)
    padding = 40
    width = size[0]*2 + padding
    height = size[1]
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    
    if home_img_bytes:
        try:
            home = Image.open(BytesIO(home_img_bytes)).convert("RGBA").resize(size)
            img.paste(home, (0, 0), home)
        except:
            pass
            
    if away_img_bytes:
        try:
            away = Image.open(BytesIO(away_img_bytes)).convert("RGBA").resize(size)
            img.paste(away, (size[0]+padding, 0), away)
        except:
            pass
            
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

# ==== FETCH MATCHES (NEXT 24H ONLY) ====
async def fetch_matches():
    now = datetime.now(timezone.utc)
    next_24h = now + timedelta(hours=24)
    matches_by_competition = {}
    
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for comp in COMPETITIONS:
            try:
                url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={next_24h.date()}"
                async with session.get(url, headers=HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        competition_name = data.get("competition", {}).get("name", comp)
                        comp_matches = []
                        
                        for m in data.get("matches", []):
                            try:
                                match_time = datetime.fromisoformat(m['utcDate'].replace("Z", "+00:00"))
                                if now <= match_time <= next_24h:
                                    m["competition"]["name"] = competition_name
                                    comp_matches.append(m)
                            except:
                                continue
                                
                        if comp_matches:
                            matches_by_competition[competition_name] = comp_matches
                            
            except Exception as e:
                print(f"Error fetching {comp} matches: {e}")
                
    # If no matches found, add fallback
    if not matches_by_competition:
        matches_by_competition["Fallback League"] = [
            {
                "id": 88801,
                "utcDate": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "homeTeam": {"name": "Fallback Team A", "crest": None},
                "awayTeam": {"name": "Fallback Team B", "crest": None},
                "competition": {"name": "Fallback League"}
            }
        ]
    
    return matches_by_competition

# ==== PERSISTENT VIEW CLASS ====
class PersistentMatchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="🏠 Home", style=discord.ButtonStyle.primary, custom_id="vote_home")
    async def vote_home(self, interaction: discord.Interaction, button: Button):
        await self.handle_vote(interaction, "home", "Home")
        
    @discord.ui.button(label="🤝 Draw", style=discord.ButtonStyle.secondary, custom_id="vote_draw") 
    async def vote_draw(self, interaction: discord.Interaction, button: Button):
        await self.handle_vote(interaction, "draw", "Draw")
        
    @discord.ui.button(label="✈️ Away", style=discord.ButtonStyle.primary, custom_id="vote_away")
    async def vote_away(self, interaction: discord.Interaction, button: Button):
        await self.handle_vote(interaction, "away", "Away")
    
    async def handle_vote(self, interaction: discord.Interaction, vote_type: str, vote_label: str):
        try:
            # Find the match_id from the message
            match_id = None
            for mid, data in vote_data.items():
                if data.get("match_msg_id") == interaction.message.id:
                    match_id = mid
                    break
                    
            if not match_id or match_id not in vote_data:
                await interaction.response.send_message("❌ Match data not found!", ephemeral=True)
                return
                
            # Check if voting is still open (10 minutes before kickoff)
            now = datetime.now(timezone.utc) 
            kickoff_time = vote_data[match_id]["kickoff_time"]
            voting_cutoff = kickoff_time - timedelta(minutes=10)
            
            if now >= voting_cutoff:
                await interaction.response.send_message(
                    f"⏰ Voting has closed! Voting ends 10 minutes before kickoff.\n"
                    f"Kickoff: <t:{int(kickoff_time.timestamp())}:R>", 
                    ephemeral=True
                )
                return
                
            user = interaction.user
            user_id = user.id
            
            # Check if user already voted and show their current vote
            if user_id in vote_data[match_id]["locked_users"]:
                # Find their current vote
                current_vote = None
                user_id_str = str(user_id)
                if user_id_str in leaderboard and match_id in leaderboard[user_id_str].get("predictions", {}):
                    current_vote = leaderboard[user_id_str]["predictions"][match_id]
                
                vote_labels = {"home": f"🏠 {vote_data[match_id]['home_team']}", "draw": "🤝 Draw", "away": f"✈️ {vote_data[match_id]['away_team']}"}
                current_label = vote_labels.get(current_vote, "Unknown")
                
                await interaction.response.send_message(
                    f"✅ **You already voted for this match!**\n"
                    f"Your prediction: **{current_label}**\n\n"
                    f"Match: {vote_data[match_id]['home_team']} vs {vote_data[match_id]['away_team']}\n"
                    f"Kickoff: <t:{int(kickoff_time.timestamp())}:R>", 
                    ephemeral=True
                )
                return
                
            # Add the vote
            vote_data[match_id][vote_type].add(user.display_name)
            vote_data[match_id]["locked_users"].add(user_id)
            
            # Update leaderboard data
            user_id_str = str(user_id)
            if user_id_str not in leaderboard:
                leaderboard[user_id_str] = {"name": user.display_name, "points": 0, "predictions": {}}
            leaderboard[user_id_str]["predictions"][match_id] = vote_type
            leaderboard[user_id_str]["name"] = user.display_name  # Update name in case it changed
            save_leaderboard()
            
            # Create/update votes embed immediately after the match message
            embed = create_votes_embed(match_id)
            votes_msg_id = vote_data[match_id].get("votes_msg_id")
            match_msg_id = vote_data[match_id]["match_msg_id"]
            
            if votes_msg_id:
                try:
                    votes_message = await interaction.channel.fetch_message(votes_msg_id)
                    await votes_message.edit(embed=embed)
                except discord.NotFound:
                    # Message was deleted, create new one as reply to match message
                    try:
                        match_message = await interaction.channel.fetch_message(match_msg_id)
                        votes_message = await match_message.reply(embed=embed, mention_author=False)
                        vote_data[match_id]["votes_msg_id"] = votes_message.id
                    except Exception as e:
                        print(f"Error creating votes reply: {e}")
            else:
                # Create first votes message as reply to match message
                try:
                    match_message = await interaction.channel.fetch_message(match_msg_id)
                    votes_message = await match_message.reply(embed=embed, mention_author=False)
                    vote_data[match_id]["votes_msg_id"] = votes_message.id
                except Exception as e:
                    print(f"Error creating votes reply: {e}")
                    # Fallback: create at bottom if reply fails
                    votes_message = await interaction.channel.send(embed=embed)
                    vote_data[match_id]["votes_msg_id"] = votes_message.id
            
            # Enhanced success message
            home_team = vote_data[match_id]['home_team']
            away_team = vote_data[match_id]['away_team']
            vote_labels = {"home": f"🏠 {home_team}", "draw": "🤝 Draw", "away": f"✈️ {away_team}"}
            
            await interaction.response.send_message(
                f"🎯 **Vote Recorded!**\n"
                f"Match: **{home_team} vs {away_team}**\n"
                f"Your prediction: **{vote_labels[vote_type]}**\n"
                f"Kickoff: <t:{int(kickoff_time.timestamp())}:R>\n\n"
                f"✅ Good luck! You'll earn 1 point if you're correct.", 
                ephemeral=True
            )
            
        except discord.InteractionResponded:
            # Already responded somehow
            pass
        except Exception as e:
            print(f"Error in vote handling: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Something went wrong. Please try again.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            except:
                pass

# ==== POST MATCH ====
async def post_match(match):
    match_id = str(match["id"])
    if match_id in posted_matches:
        return
        
    try:
        match_time = datetime.fromisoformat(match['utcDate'].replace("Z", "+00:00"))
    except:
        print(f"Invalid date format for match {match_id}")
        return
        
    now = datetime.now(timezone.utc)
    if match_time < now:
        return
        
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        print("Match channel not found")
        return
        
    home_team = match['homeTeam']['name']
    away_team = match['awayTeam']['name']
    competition = match.get('competition', {}).get('name', 'Unknown')
    
    time_until_kickoff = match_time - now
    hours_until = int(time_until_kickoff.total_seconds() // 3600)
    
    embed = discord.Embed(
        title=f"{home_team} vs {away_team}",
        description=(
            f"🏆 **{competition}**\n"
            f"⏰ Kickoff: <t:{int(match_time.timestamp())}:F>\n"
            f"📅 <t:{int(match_time.timestamp())}:R>\n\n"
            f"🗳️ **Voting closes 10 minutes before kickoff**\n"
            f"⏳ You have ~{hours_until} hours to vote!"
        ),
        color=discord.Color.blue()
    )
    
    # Try to add team crests
    home_crest = match["homeTeam"].get("crest")
    away_crest = match["awayTeam"].get("crest") 
    file = None
    
    if home_crest or away_crest:
        try:
            image_buffer = await generate_match_image(home_crest, away_crest)
            file = discord.File(fp=image_buffer, filename="match.png")
            embed.set_image(url="attachment://match.png")
        except Exception as e:
            print(f"Error generating match image: {e}")
    
    # Create persistent view
    view = PersistentMatchView()
    
    try:
        match_message = await channel.send(embed=embed, file=file, view=view)
    except Exception as e:
        print(f"Error posting match: {e}")
        return
    
    # Store match data first
    vote_data[match_id] = {
        "home": set(), 
        "draw": set(), 
        "away": set(),
        "votes_msg_id": None, 
        "locked_users": set(), 
        "buttons_disabled": False, 
        "match_msg_id": match_message.id,
        "kickoff_time": match_time,
        "home_team": home_team,
        "away_team": away_team
    }
    
    # Create the votes embed immediately as a reply to the match
    try:
        votes_embed = create_votes_embed(match_id)
        votes_message = await match_message.reply(embed=votes_embed, mention_author=False)
        vote_data[match_id]["votes_msg_id"] = votes_message.id
    except Exception as e:
        print(f"Error creating initial votes embed: {e}")
    
    posted_matches.add(match_id)
    save_posted()
    
    print(f"Posted match: {home_team} vs {away_team} (ID: {match_id})")

# ==== CHECK AND DISABLE EXPIRED VOTES ====
@tasks.loop(minutes=2)
async def check_voting_status():
    """Check if any matches need their voting disabled"""
    if not vote_data:
        return
        
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
        
    now = datetime.now(timezone.utc)
    
    for match_id, data in list(vote_data.items()):
        if data.get("buttons_disabled"):
            continue
            
        kickoff_time = data.get("kickoff_time")
        if not kickoff_time:
            continue
            
        voting_cutoff = kickoff_time - timedelta(minutes=10)
        
        # If voting should be closed
        if now >= voting_cutoff:
            try:
                match_msg_id = data.get("match_msg_id")
                if match_msg_id:
                    match_message = await channel.fetch_message(match_msg_id)
                    
                    # Create disabled view
                    disabled_view = View(timeout=None)
                    disabled_view.add_item(Button(label="🏠 Home", style=discord.ButtonStyle.secondary, disabled=True))
                    disabled_view.add_item(Button(label="🤝 Draw", style=discord.ButtonStyle.secondary, disabled=True)) 
                    disabled_view.add_item(Button(label="✈️ Away", style=discord.ButtonStyle.secondary, disabled=True))
                    
                    await match_message.edit(view=disabled_view)
                    data["buttons_disabled"] = True
                    
                    print(f"Disabled voting for match {match_id}")
                    
            except Exception as e:
                print(f"Error disabling voting for match {match_id}: {e}")

# ==== UPDATE MATCH RESULTS & LEADERBOARD ====
@tasks.loop(minutes=5)
async def update_match_results():
    global last_leaderboard_msg_id
    leaderboard_changed = False
    previous_points = {uid: v.get("points", 0) for uid, v in leaderboard.items()}
    
    # Fetch finished matches and update points
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for comp in COMPETITIONS:
            try:
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
                            
                        score = m.get("score", {})
                        winner = score.get("winner")
                        
                        if not winner or match_id not in vote_data:
                            continue
                            
                        # Convert API result to our format
                        if winner == "HOME_TEAM":
                            result = "home"
                        elif winner == "AWAY_TEAM": 
                            result = "away"
                        elif winner == "DRAW":
                            result = "draw"
                        else:
                            continue
                            
                        # Award points to correct predictors
                        points_awarded = False
                        for uid, user_data in leaderboard.items():
                            prediction = user_data.get("predictions", {}).get(match_id)
                            if prediction == result:
                                user_data["points"] = user_data.get("points", 0) + 1
                                leaderboard_changed = True
                                points_awarded = True
                                
                        if points_awarded:
                            save_leaderboard()
                            
                        # Update votes display with result
                        try:
                            votes_msg_id = vote_data[match_id].get("votes_msg_id")
                            if votes_msg_id:
                                channel = bot.get_channel(MATCH_CHANNEL_ID)
                                votes_message = await channel.fetch_message(votes_msg_id)
                                result_embed = create_votes_embed(match_id, match_result=result)
                                await votes_message.edit(embed=result_embed)
                                
                            # Disable buttons if not already disabled
                            if not vote_data[match_id].get("buttons_disabled"):
                                match_msg_id = vote_data[match_id].get("match_msg_id")
                                if match_msg_id:
                                    match_message = await channel.fetch_message(match_msg_id)
                                    disabled_view = View(timeout=None)
                                    disabled_view.add_item(Button(label="🏠 Home", style=discord.ButtonStyle.secondary, disabled=True))
                                    disabled_view.add_item(Button(label="🤝 Draw", style=discord.ButtonStyle.secondary, disabled=True))
                                    disabled_view.add_item(Button(label="✈️ Away", style=discord.ButtonStyle.secondary, disabled=True))
                                    await match_message.edit(view=disabled_view)
                                    vote_data[match_id]["buttons_disabled"] = True
                                    
                        except Exception as e:
                            print(f"Error updating match result display for {match_id}: {e}")
                            
            except Exception as e:
                print(f"Error checking results for {comp}: {e}")
    
    # Update leaderboard message if there were changes
    if leaderboard_changed:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            return
            
        users = [v for v in leaderboard.values() if v.get("predictions")]
        if not users:
            return
            
        sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
        desc_lines = []
        
        for i, entry in enumerate(sorted_lb[:10]):
            uid = next(uid for uid, v in leaderboard.items() if v["name"] == entry["name"])
            current_points = entry.get("points", 0)
            previous = previous_points.get(uid, 0)
            diff = current_points - previous
            
            suffix = f" **(+{diff})**" if diff > 0 else ""
            desc_lines.append(f"**{i+1}.** {entry['name']} — {current_points} pts{suffix}")
            
        embed = discord.Embed(
            title="🏆 Leaderboard", 
            description="\n".join(desc_lines),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Last updated")
        
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
    await interaction.response.defer(ephemeral=True)
    try:
        matches_by_competition = await fetch_matches()
        if not matches_by_competition:
            await interaction.followup.send("No upcoming matches found.", ephemeral=True)
            return
        
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("❌ Match channel not found!", ephemeral=True)
            return
            
        total_posted = 0
        
        # Post matches organized by competition
        for competition_name, matches in matches_by_competition.items():
            if not matches:
                continue
                
            # Post competition header
            header_embed = discord.Embed(
                title=f"🏆 {competition_name}",
                description=f"📅 {len(matches)} match{'es' if len(matches) != 1 else ''} upcoming",
                color=discord.Color.gold()
            )
            await channel.send(embed=header_embed)
            
            # Post each match in this competition
            for match in matches:
                try:
                    await post_match(match)
                    total_posted += 1
                except Exception as e:
                    print(f"Error posting match: {e}")
                    
        await interaction.followup.send(f"✅ Posted {total_posted} matches across {len(matches_by_competition)} competitions!", ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching matches: {str(e)}", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the current leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    users = [v for v in leaderboard.values() if v.get("predictions")]
    if not users:
        await interaction.response.send_message("📊 Leaderboard is empty. Make some predictions first!", ephemeral=True)
        return
        
    sorted_lb = sorted(users, key=lambda x: (-x.get("points", 0), x["name"].lower()))
    desc = "\n".join([f"**{i+1}.** {entry['name']} — {entry.get('points',0)} pts" for i, entry in enumerate(sorted_lb[:10])])
    
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="my_votes", description="Check your current predictions.")
async def my_votes_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    
    if user_id not in leaderboard or not leaderboard[user_id].get("predictions"):
        await interaction.response.send_message("📝 You haven't made any predictions yet!", ephemeral=True)
        return
    
    user_data = leaderboard[user_id]
    predictions = user_data.get("predictions", {})
    points = user_data.get("points", 0)
    
    embed = discord.Embed(
        title=f"📊 {user_data['name']}'s Predictions",
        description=f"🏆 Current Points: **{points}**\n📝 Total Predictions: **{len(predictions)}**",
        color=discord.Color.blue()
    )
    
    # Show active predictions (matches that haven't started yet)
    active_predictions = []
    finished_predictions = []
    
    now = datetime.now(timezone.utc)
    
    for match_id, prediction in predictions.items():
        if match_id in vote_data:
            match_data = vote_data[match_id]
            home_team = match_data.get("home_team", "Unknown")
            away_team = match_data.get("away_team", "Unknown")
            kickoff_time = match_data.get("kickoff_time")
            
            vote_labels = {"home": f"🏠 {home_team}", "draw": "🤝 Draw", "away": f"✈️ {away_team}"}
            pred_label = vote_labels.get(prediction, prediction)
            
            if kickoff_time and now < kickoff_time:
                active_predictions.append(f"**{home_team} vs {away_team}**\n└ {pred_label} • <t:{int(kickoff_time.timestamp())}:R>")
            else:
                finished_predictions.append(f"**{home_team} vs {away_team}**\n└ {pred_label}")
    
    if active_predictions:
        embed.add_field(
            name="⏳ Upcoming Matches",
            value="\n\n".join(active_predictions[:5]) + ("\n\n*...and more*" if len(active_predictions) > 5 else ""),
            inline=False
        )
    
    if finished_predictions:
        embed.add_field(
            name="✅ Recent Predictions",
            value="\n\n".join(finished_predictions[-3:]),
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="match_status", description="Check voting status for active matches.")
async def match_status_command(interaction: discord.Interaction):
    if not vote_data:
        await interaction.response.send_message("📭 No active matches found.", ephemeral=True)
        return
    
    now = datetime.now(timezone.utc)
    active_matches = []
    
    for match_id, data in vote_data.items():
        kickoff_time = data.get("kickoff_time")
        if not kickoff_time:
            continue
            
        if now < kickoff_time:  # Only show upcoming matches
            home_team = data.get("home_team", "Unknown")
            away_team = data.get("away_team", "Unknown")
            
            total_votes = len(data.get("home", set())) + len(data.get("draw", set())) + len(data.get("away", set()))
            voting_cutoff = kickoff_time - timedelta(minutes=10)
            
            if now >= voting_cutoff:
                status = "🔒 Voting Closed"
            else:
                time_left = voting_cutoff - now
                hours_left = int(time_left.total_seconds() // 3600)
                minutes_left = int((time_left.total_seconds() % 3600) // 60)
                if hours_left > 0:
                    status = f"🗳️ {hours_left}h {minutes_left}m left"
                else:
                    status = f"🗳️ {minutes_left}m left"
            
            active_matches.append(
                f"**{home_team} vs {away_team}**\n"
                f"├ {status}\n"
                f"├ 👥 {total_votes} votes\n"
                f"└ ⚽ <t:{int(kickoff_time.timestamp())}:R>"
            )
    
    if not active_matches:
        await interaction.response.send_message("📭 No upcoming matches found.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="⚽ Active Matches",
        description="\n\n".join(active_matches[:8]) + ("\n\n*...and more*" if len(active_matches) > 8 else ""),
        color=discord.Color.green()
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="test_matches", description="Post test matches for voting (15min and 2hr from now).")
async def test_matches_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    test_matches = [
        {
            "id": 99901,
            "utcDate": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            "homeTeam": {"name": "Test Team A", "crest": None},
            "awayTeam": {"name": "Test Team B", "crest": None},
            "competition": {"name": "Test League"}
        },
        {
            "id": 99902, 
            "utcDate": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "homeTeam": {"name": "Test Team C", "crest": None},
            "awayTeam": {"name": "Test Team D", "crest": None},
            "competition": {"name": "Test League"}
        }
    ]
    
    for match in test_matches:
        await post_match(match)
        
    await interaction.followup.send("✅ Test matches posted!", ephemeral=True)

# ==== DAILY MATCH SCHEDULER ====
scheduler = AsyncIOScheduler()

async def daily_fetch_matches():
    try:
        matches_by_competition = await fetch_matches()
        channel = bot.get_channel(MATCH_CHANNEL_ID)
        if not channel:
            print("Match channel not found for daily fetch")
            return
            
        total_posted = 0
        
        # Post matches organized by competition
        for competition_name, matches in matches_by_competition.items():
            if not matches:
                continue
                
            # Post competition header
            header_embed = discord.Embed(
                title=f"🏆 {competition_name}",
                description=f"📅 {len(matches)} match{'es' if len(matches) != 1 else ''} today",
                color=discord.Color.gold()
            )
            await channel.send(embed=header_embed)
            
            # Post each match in this competition
            for match in matches:
                await post_match(match)
                total_posted += 1
                
        print(f"Daily fetch completed - posted {total_posted} matches across {len(matches_by_competition)} competitions")
    except Exception as e:
        print(f"Error in daily fetch: {e}")

scheduler.add_job(
    lambda: bot.loop.create_task(daily_fetch_matches()), 
    "cron", 
    hour=6, 
    minute=0,
    timezone="UTC"
)

# ==== STARTUP ====
@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    
    # Add persistent views
    bot.add_view(PersistentMatchView())
    
    # Sync commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # Start tasks
    if not update_match_results.is_running():
        update_match_results.start()
        print("Started match results task")
        
    if not check_voting_status.is_running():
        check_voting_status.start() 
        print("Started voting status task")
        
    if not scheduler.running:
        scheduler.start()
        print("Started scheduler")

# ==== ERROR HANDLING ====
@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Bot error in {event}: {args}")

bot.run(DISCORD_BOT_TOKEN)
