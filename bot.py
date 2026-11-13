import discord
from discord.ext import commands, tasks
import aiohttp
import sqlite3
import os
import time
import math
import sys
import random
import traceback
from datetime import datetime, timezone
from asyncio import create_task, run as async_run
from aiohttp import web

# Token & Settings
BOT_TOKEN = os.getenv("BOT_TOKEN")
CLAN_TAG = os.getenv("CLAN_TAG", "RGW")
CHANNEL_ID = os.getenv("CHANNEL_ID")                     # Kanaal voor Rebirths (xx:00)
POWERFULCOINS_CHANNEL_ID = os.getenv("POWERFULCOINS_CHANNEL_ID")   # Kanaal voor PowerfulCoins (xx:30)
API_BASE = "https://public-api.powerfulstudio.xyz"

DB_PATH = "/var/data/rebirths.db" if os.path.exists("/var/data") else "rebirths.db"

SUFFIXES = [
    'K','M','B','T','Qd','Qn','Sx','Sp','O','N','de','Ud','DD','tdD','qdD','QnD',
    'sxD','SpD','OcD','NvD','Vgn','UVg','DVg','TVg','qtV','QnV','SeV','SPG','OVG',
    'NVG','TGN','UTG','DTG','tsTG','qtTG','QnTG','ssTG','SpTG','OcTG','NoAG','UnAG',
    'DuAG','TeAG','QdAG','QnAG','SxAG','SpAG','OcAG','NvAG','CT'
]

USER_CACHE = {}

# --- Database Setup ---
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                timestamp REAL,
                user_id TEXT,
                username TEXT,
                rebirths REAL,
                powerfulcoins REAL
            )
        ''')
        try:
            c.execute("ALTER TABLE snapshots ADD COLUMN powerfulcoins REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Migratie voor eventuele oude tabellen die 'pumpkins' heetten
        try:
            c.execute("ALTER TABLE snapshots RENAME COLUMN pumpkins TO powerfulcoins")
        except sqlite3.OperationalError:
            pass
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database init error: {e}")

def save_snapshot(user_id, username, rebirths, powerfulcoins):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)", (time.time(), str(user_id), username, rebirths, powerfulcoins))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Save snapshot error: {e}")

def get_stat_ago(user_id, hours_ago, stat_type):
    try:
        target_time = time.time() - (hours_ago * 3600)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(f'''
            SELECT {stat_type} FROM snapshots 
            WHERE user_id = ? AND timestamp <= ? 
            ORDER BY timestamp DESC LIMIT 1
        ''', (str(user_id), target_time))
        row = c.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 0.0
    except Exception:
        return 0.0

# --- Format Functie ---
def format_number(val, show_plus=False):
    if val is None or math.isnan(val) or val <= 0:
        return "+0" if show_plus else "0"

    if val < 1000:
        formatted = f"{val:.2f}"
        return f"+{formatted}" if show_plus else formatted

    i = int(math.floor(math.log(val, 1000))) - 1
    if i < 0:
        i = 0
    if i >= len(SUFFIXES):
        i = len(SUFFIXES) - 1

    scaled = val / (1000 ** (i + 1))
    formatted = f"{scaled:.2f} {SUFFIXES[i]}"
    return f"+{formatted}" if show_plus else formatted

async def resolve_username(session, member):
    for key in ["username", "userName", "displayName", "name"]:
        val = member.get(key)
        if val and isinstance(val, str) and not val.startswith("User_"):
            return val

    if isinstance(member.get("user"), dict):
        for key in ["username", "name"]:
            val = member["user"].get(key)
            if val and isinstance(val, str):
                return val

    raw_id = member.get("userId") or member.get("id") or member.get("robloxId")
    if raw_id:
        str_id = str(raw_id)
        if str_id in USER_CACHE:
            return USER_CACHE[str_id]

        if str_id.isdigit():
            try:
                async with session.get(f"https://users.roblox.com/v1/users/{str_id}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fetched_name = data.get("name") or data.get("displayName")
                        if fetched_name:
                            USER_CACHE[str_id] = fetched_name
                            return fetched_name
            except Exception:
                pass
        return f"User_{str_id}"

    return "Unknown Player"

# --- Web Server ---
async def handle_ping(request):
    return web.Response(text="Bot is online!")

async def start_dummy_webserver():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Webserver gestart op poort {port}")

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def fetch_clan_data(session, tag):
    try:
        async with session.get(f"{API_BASE}/rcu/v1/clans/tag/{tag}") as resp:
            if resp.status != 200:
                return None
            tag_data = await resp.json()
            clan_id = tag_data.get("id") or tag_data.get("clanId")

        async with session.get(f"{API_BASE}/rcu/v1/clans/{clan_id}") as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"API Error: {e}")
    return None

def extract_currency_map(clan_data, keyword="powerfulCoins"):
    currency_map = {}
    currencies = clan_data.get("currencies", {})
    
    target_key = None
    for key in currencies.keys():
        if keyword.lower() in key.lower():
            target_key = key
            break
            
    if target_key and isinstance(currencies[target_key], list):
        for entry in currencies[target_key]:
            u_id = str(entry.get("userId") or entry.get("id", ""))
            amount = float(entry.get("amount", 0))
            if u_id:
                currency_map[u_id] = amount
                
    return currency_map

async def build_entry(session, idx, m, members, stat_type="rebirths", coin_map=None):
    u_id = str(m.get("userId") or m.get("id") or m.get("username"))
    username = await resolve_username(session, m)
    safe_username = username.replace("_", "\\_")
    
    if stat_type == "rebirths":
        total = float(m.get("rebirths", 0))
    else:
        total = float(coin_map.get(u_id, 0) if coin_map else 0)

    t_3h = get_stat_ago(u_id, 3, stat_type)
    t_6h = get_stat_ago(u_id, 6, stat_type)
    t_12h = get_stat_ago(u_id, 12, stat_type)
    t_1h = get_stat_ago(u_id, 1, stat_type)

    gain_1h = (total - t_1h) if t_1h is not None else 0.0
    gain_3h = (total - t_3h) if t_3h is not None else 0.0
    gain_6h = (total - t_6h) if t_6h is not None else 0.0
    gain_12h = (total - t_12h) if t_12h is not None else 0.0

    profile_url = f"https://www.roblox.com/users/{u_id}/profile" if str(u_id).isdigit() else "https://www.roblox.com"
    blue_name = f"[{safe_username}]({profile_url})"

    rank_str = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"**{idx}.**"
    header_line = f"{rank_str} **{blue_name}**"

    if gain_1h > 0:
        status_dot = "🟢"
        start_1h = t_1h if t_1h and t_1h > 0 else (total - gain_1h)
        pct = (gain_1h / start_1h * 100) if start_1h > 0 else 0.0
        rate_str = f"**{format_number(gain_1h, show_plus=True)}**/h (+{pct:.2f}%)"
    else:
        status_dot = "🔴"
        rate_str = "+0/h"

    stat_line = f"{status_dot} {rate_str} · Total **{format_number(total)}**"
    period_line = f"├─ `3h` **{format_number(gain_3h, show_plus=True)}** · `6h` **{format_number(gain_6h, show_plus=True)}** · `12h` **{format_number(gain_12h, show_plus=True)}**"

    if idx == 1:
        if len(members) > 1:
            next_member = members[1]
            if stat_type == "rebirths":
                next_val = float(next_member.get("rebirths", 0))
            else:
                nm_id = str(next_member.get("userId") or next_member.get("id") or next_member.get("username"))
                next_val = float(coin_map.get(nm_id, 0) if coin_map else 0)
            diff = total - next_val
            gap_line = f"└ 👑 **leads #2 by {format_number(diff)}**"
        else:
            gap_line = ""
    else:
        prev_member = members[idx - 2]
        prev_u_id = str(prev_member.get("userId") or prev_member.get("id") or prev_member.get("username"))
        if stat_type == "rebirths":
            prev_total = float(prev_member.get("rebirths", 0))
        else:
            prev_total = float(coin_map.get(prev_u_id, 0) if coin_map else 0)

        prev_t1h = get_stat_ago(prev_u_id, 1, stat_type)
        prev_gain1h = (prev_total - prev_t1h) if prev_t1h is not None else 0.0

        diff = prev_total - total

        if gain_1h > prev_gain1h:
            speed_diff = gain_1h - prev_gain1h
            hours_to_overtake = diff / speed_diff if speed_diff > 0 else 0
            
            if hours_to_overtake < 24:
                overtake_str = f" · ~{hours_to_overtake:.1f}h"
            else:
                days = hours_to_overtake / 24
                overtake_str = f" · ~{days:.1f}d"

            gap_trend = f"↗️ closing gap ({overtake_str})"
        elif gain_1h < prev_gain1h:
            gap_trend = "↘️ gap widening"
        else:
            gap_trend = "➡️ gap stable"

        gap_line = f"└ {format_number(diff)} behind #{idx-1} · {gap_trend}"

    entry_lines = [header_line, stat_line, period_line]
    if gap_line:
        entry_lines.append(gap_line)

    return "\n".join(entry_lines)

async def build_leaderboard_embeds(stat_type="rebirths"):
    async with aiohttp.ClientSession() as session:
        clan_data = await fetch_clan_data(session, CLAN_TAG)
        if not clan_data or "members" not in clan_data:
            return None, None

        members = clan_data["members"]
        coin_map = {}

        if stat_type == "powerfulcoins":
            coin_map = extract_currency_map(clan_data, keyword="powerfulCoins")
            members.sort(key=lambda x: float(coin_map.get(str(x.get("userId") or x.get("id") or x.get("username")), 0)), reverse=True)
        else:
            members.sort(key=lambda x: float(x.get("rebirths", 0)), reverse=True)

        lines_part1, lines_part2 = [], []

        for idx, m in enumerate(members[:10], start=1):
            entry = await build_entry(session, idx, m, members, stat_type, coin_map)
            lines_part1.append(entry)

        for idx, m in enumerate(members[10:25], start=11):
            entry = await build_entry(session, idx, m, members, stat_type, coin_map)
            lines_part2.append(entry)

        title_prefix = "📊 Rebirth Stats" if stat_type == "rebirths" else "🪙 PowerfulCoins Stats"
        embed1 = discord.Embed(title=f"{title_prefix} — Clan {CLAN_TAG} (1-10)", color=0x2b2d31, description="\n\n".join(lines_part1))
        embed2 = discord.Embed(title=f"{title_prefix} — Clan {CLAN_TAG} (11-25)", color=0x2b2d31, description="\n\n".join(lines_part2) if lines_part2 and lines_part2[0] else None)

        return embed1, embed2

# Data opslaan elke 10 minuten
@tasks.loop(minutes=10)
async def track_data():
    async with aiohttp.ClientSession() as session:
        clan_data = await fetch_clan_data(session, CLAN_TAG)
        if clan_data and "members" in clan_data:
            coin_map = extract_currency_map(clan_data, keyword="powerfulCoins")
            for member in clan_data["members"]:
                u_id = str(member.get("userId") or member.get("id") or member.get("username"))
                username = await resolve_username(session, member)
                rebirths = float(member.get("rebirths", 0))
                powerfulcoins = float(coin_map.get(u_id, 0))
                save_snapshot(u_id, username, rebirths, powerfulcoins)

# Broadcasters (Rebirths om xx:00 en PowerfulCoins om xx:30)
@tasks.loop(minutes=1)
async def hourly_broadcaster():
    now = datetime.now(timezone.utc)
    
    if now.minute == 0 and CHANNEL_ID:
        try:
            channel = bot.get_channel(int(CHANNEL_ID))
            if channel:
                e1, e2 = await build_leaderboard_embeds("rebirths")
                if e1: await channel.send(embed=e1)
                if e2 and e2.description: await channel.send(embed=e2)
        except Exception as err:
            print(f"Error in rebirth broadcaster: {err}")

    if now.minute == 30 and POWERFULCOINS_CHANNEL_ID:
        try:
            p_channel = bot.get_channel(int(POWERFULCOINS_CHANNEL_ID))
            if p_channel:
                e1, e2 = await build_leaderboard_embeds("powerfulcoins")
                if e1: await p_channel.send(embed=e1)
                if e2 and e2.description: await p_channel.send(embed=e2)
        except Exception as err:
            print(f"Error in powerfulcoins broadcaster: {err}")

@hourly_broadcaster.before_loop
async def before_hourly_broadcaster():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    init_db()
    if not track_data.is_running(): track_data.start()
    if not hourly_broadcaster.is_running(): hourly_broadcaster.start()
    print(f"✅ Bot online als: {bot.user}")

# --- Bot Commando's ---
@bot.command(name="stats")
async def send_leaderboard(ctx):
    async with ctx.typing():
        e1, e2 = await build_leaderboard_embeds("rebirths")
        if e1:
            await ctx.send(embed=e1)
            if e2 and e2.description: await ctx.send(embed=e2)

@bot.command(name="powerfulcoins")
async def send_powerfulcoins_leaderboard(ctx):
    async with ctx.typing():
        e1, e2 = await build_leaderboard_embeds("powerfulcoins")
        if e1:
            await ctx.send(embed=e1)
            if e2 and e2.description: await ctx.send(embed=e2)

# Achtervang voor oude commando's
@bot.command(name="pumpkins")
async def send_old_pumpkins_redirect(ctx):
    async with ctx.typing():
        e1, e2 = await build_leaderboard_embeds("powerfulcoins")
        if e1:
            await ctx.send(embed=e1)
            if e2 and e2.description: await ctx.send(embed=e2)

@bot.command(name="rush")
async def rush_command(ctx):
    response = random.choice(["El Dictator", "Rushy Plushy"])
    await ctx.send(response)

@bot.command(name="peste")
async def peste_command(ctx):
    await ctx.send("Pasta pesto")

@bot.command(name="khelestial")
async def khelestial_command(ctx):
    await ctx.send("No mute for me , neither demote.,.")

@bot.command(name="allcommands")
async def show_all_api_fields(ctx):
    async with ctx.typing(), aiohttp.ClientSession() as session:
        clan_data = await fetch_clan_data(session, CLAN_TAG)
        if clan_data:
            currencies = clan_data.get("currencies", {})
            embed = discord.Embed(title=f"🔍 Beschikbare Currencies — Clan {CLAN_TAG}", color=0x2b2d31)
            embed.add_field(name="Gevonden Currency Sleutels:", value=str(list(currencies.keys())), inline=False)
            await ctx.send(embed=embed)
        else:
            await ctx.send("Kon geen clan-data ophalen.")

async def main():
    if not BOT_TOKEN:
        print("FOUT: Geen BOT_TOKEN ingesteld.")
        return
    
    await start_dummy_webserver()
    
    while True:
        try:
            print("🤖 Verbinden met Discord...")
            await bot.start(BOT_TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print("⚠️ Discord Rate Limit (429) geraakt. 60 seconden wachten...")
                await asyncio.sleep(60)
            else:
                traceback.print_exc()
                await asyncio.sleep(10)
        except Exception as e:
            print(f"Fout bij opstarten: {e}")
            traceback.print_exc()
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        async_run(main())
    except Exception:
        traceback.print_exc()
        time.sleep(10)
        sys.exit(1)
