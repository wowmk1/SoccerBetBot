# ==== FETCH MATCHES ====
async def fetch_matches():
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={now.date()}&dateTo={tomorrow.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for match in data.get("matches", []):
                        MATCH_CACHE[str(match["id"])] = match
                        matches.append(match)
    matches = [m for m in matches if datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")) > now]
    return matches

# ==== POST MATCH ====
async def post_match(match):
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(
        title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
        description=f"Kickoff: {match['utcDate']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=match['homeTeam'].get('crest'))
    await channel.send(embed=embed, view=MatchView(match["id"]))

# ==== BACKGROUND TASKS ====
@tasks.loop(minutes=30)
async def auto_post_matches():
    matches = await fetch_matches()
    for match in matches:
        await post_match(match)

@tasks.loop(hours=3)
async def update_scores():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    async with aiohttp.ClientSession() as session:
        for comp in COMPETITIONS:
            url = f"{BASE_URL}{comp}/matches?dateFrom={yesterday.date()}&dateTo={now.date()}"
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for match in data.get("matches", []):
                        if match["status"] == "FINISHED":
                            match_id = str(match["id"])
                            result = get_result(match)
                            update_leaderboard(match_id, result)

def get_result(match):
    home = match["score"]["fullTime"]["home"]
    away = match["score"]["fullTime"]["away"]
    if home > away:
        return "HOME_TEAM"
    elif away > home:
        return "AWAY_TEAM"
    else:
        return "DRAW"

def update_leaderboard(match_id, result):
    for user_id, data in leaderboard.items():
        prediction = data["predictions"].get(match_id)
        if prediction and prediction == result:
            data["points"] += 3
    save_leaderboard()

@tasks.loop(minutes=5)
async def send_reminders():
    now = datetime.now(timezone.utc)
    for match_id, info in list(VOTE_TRACKER.items()):
        kickoff = datetime.fromisoformat(info["kickoff"].replace("Z", "+00:00"))
        if 25 < (kickoff - now).total_seconds() / 60 < 35:
            for user_id in info["voters"]:
                user = await bot.fetch_user(int(user_id))
                if user:
                    await user.send(f"⏰ Reminder: Your prediction for match `{match_id}` kicks off in 30 minutes!")
            del VOTE_TRACKER[match_id]

@tasks.loop(hours=1)
async def weekly_summary():
    now = datetime.now()
    if now.weekday() == 6 and now.hour == 20:
        for user_id, data in leaderboard.items():
            user = await bot.fetch_user(int(user_id))
            correct = data["points"] // 3
            total = len(data["predictions"])
            await user.send(
                f"📅 Weekly Summary:\n"
                f"• Matches voted: {total}\n"
                f"• Correct predictions: {correct}\n"
                f"• Total points: {data['points']}"
            )

# ==== COMMANDS ====
@bot.tree.command(name="matches", description="Show upcoming matches.")
async def matches_command(interaction: discord.Interaction):
    matches = await fetch_matches()
    if not matches:
        await interaction.response.send_message("No upcoming matches.", ephemeral=True)
        return
    for match in matches[:5]:
        embed = discord.Embed(
            title=f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            description=f"Kickoff: {match['utcDate']}",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=match['homeTeam'].get('crest'))
        await interaction.channel.send(embed=embed, view=MatchView(match["id"]))
    await interaction.response.send_message("✅ Posted upcoming matches!", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Show the leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    if not leaderboard:
        await interaction.response.send_message("Leaderboard is empty.", ephemeral=True)
        return
    sorted_lb = sorted(leaderboard.values(), key=lambda x: (-x["points"], x["name"].lower()))
    desc = "\n".join([
        f"**{i+1}. {entry['name']}** — {entry['points']} pts"
        for i, entry in enumerate(sorted_lb[:10])
    ])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="myvotes", description="See which matches you've voted on.")
async def myvotes_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard or not leaderboard[user_id]["predictions"]:
        await interaction.response.send_message("You haven't voted on any matches yet.", ephemeral=True)
        return
    predictions = leaderboard[user_id]["predictions"]
    embeds = []
    for match_id, prediction in list(predictions.items())[:5]:
        match = MATCH_CACHE.get(match_id)
        if match:
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            kickoff = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).strftime("%A, %d %b %Y %H:%M UTC")
            home_crest = match["homeTeam"].get("crest")
            away_crest = match["awayTeam"].get("crest")
            embed = discord.Embed(
                title=f"{home} vs {away}",
                description=f"🕒 Kickoff: {kickoff}\n🗳️ Your Prediction: **{prediction}**",
                color=discord.Color.purple()
            )
            if home_crest:
                embed.set_thumbnail(url=home_crest)
            if away_crest:
                embed.set_image(url=away_crest)
            embeds.append(embed)
        else:
            embed = discord.Embed(
                title=f"Match ID {match_id}",
                description=f"🗳️ Your Prediction: **{prediction}**",
                color=discord.Color.purple()
            )
            embeds.append(embed)
    for embed in embeds:
        await interaction.channel.send(embed=embed)
    await interaction.response.send_message("✅ Here's your recent votes!", ephemeral=True)

@bot.tree.command(name="stats", description="Show your prediction stats.")
async def stats_command(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id not in leaderboard:
        await interaction.response.send_message("You haven't voted yet.", ephemeral=True)
        return
    data = leaderboard[user_id]
    total = len(data["predictions"])
    correct = data["points"] // 3
    embed = discord.Embed(
        title=f"📊 Stats for {data['name']}",
        description=f"• Total Votes: {total}\n• Correct Predictions: {correct}\n• Points: {data['points']}",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    update_scores.start()
    send_reminders.start()
    weekly_summary.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
