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
                            v["points"] = v.get("points",0)+1
                            leaderboard_changed = True
                    save_leaderboard()

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
    desc = "\n".join([f"**{i+1}. {entry['name']}** — {entry.get('points',0)} pts" for i,entry in enumerate(sorted_lb[:10])])
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ==== STARTUP ====
@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_post_matches.start()
    update_match_results.start()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
