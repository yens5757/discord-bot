import asyncio
import hashlib
import io
import itertools
import json
import os
import random
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image

import champions
import riot
import wheel
from reel import SECONDS as REEL_SECONDS
from reel import render_reel_gif, render_reel_preview
from stat_card import STYLES, render_card_image, render_shop_preview, render_stat_card
from wheel import PRIZES, STYLE_REFUND, render_spin_gif, roll

load_dotenv()  # reads DISCORD_TOKEN from the .env file next to this script

# ---------- Settings ----------

TRIGGERS = ["冠", "魔"]
REPLY = "收網"

MIX_SIZE = 10            # 10mix: 10 players split into 2 teams (set to 2 to test)
START_ELO = 1000         # everyone's starting ELO
ELO_CHANGE = 20          # Flex: win +20, loss -20 (both modes share the same ELO)
START_CREDITS = 100      # credits every player starts with
CREDIT_WIN = 20          # credits for a win (10mix and Flex)
CREDIT_LOSS = 5          # credits for a loss
SPIN_COST = 25           # credits per paid spin (one free spin a day)
HKT = timezone(timedelta(hours=8))  # free spin resets at 00:00 Hong Kong time
MIX_K = 40               # 10mix: even teams = ±20; the stronger team gains less / loses more
DB_PATH = Path(__file__).with_name("elo.db")

# ---------- Database ----------

db = sqlite3.connect(DB_PATH)
db.execute("""
CREATE TABLE IF NOT EXISTS players (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    elo      INTEGER NOT NULL,
    wins     INTEGER NOT NULL DEFAULT 0,
    losses   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
)""")
db.execute("""
CREATE TABLE IF NOT EXISTS matches (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    team_a   TEXT NOT NULL,
    team_b   TEXT NOT NULL,                  -- empty for flex matches
    status   TEXT NOT NULL DEFAULT 'open',   -- open / done / cancelled
    winner   TEXT,
    mode     TEXT NOT NULL DEFAULT 'mix'     -- mix / flex
)""")
# Upgrade databases created by the older version of this bot
if "mode" not in [row[1] for row in db.execute("PRAGMA table_info(matches)")]:
    db.execute("ALTER TABLE matches ADD COLUMN mode TEXT NOT NULL DEFAULT 'mix'")
player_cols = [row[1] for row in db.execute("PRAGMA table_info(players)")]
if "credits" not in player_cols:
    db.execute("ALTER TABLE players ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")
    # give credits for games already played
    db.execute("UPDATE players SET credits = wins * ? + losses * ?", (CREDIT_WIN, CREDIT_LOSS))
if "style" not in player_cols:
    db.execute("ALTER TABLE players ADD COLUMN style TEXT NOT NULL DEFAULT 'dark'")
if "last_free_spin" not in player_cols:
    db.execute("ALTER TABLE players ADD COLUMN last_free_spin TEXT")         # HKT date, e.g. 2026-10-18
if "bonus_spins" not in player_cols:
    db.execute("ALTER TABLE players ADD COLUMN bonus_spins INTEGER NOT NULL DEFAULT 0")  # from 再轉一次
db.execute("""
CREATE TABLE IF NOT EXISTS owned_styles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    style    TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, style)
)""")
if "champ_spins" not in player_cols:
    # 英雄卡轉盤 spins done; while it's 0 the next one is free. Anyone with champion cards already spun.
    db.execute("ALTER TABLE players ADD COLUMN champ_spins INTEGER NOT NULL DEFAULT 0")
    db.execute("""UPDATE players SET champ_spins = (SELECT COUNT(*) FROM owned_styles o
        WHERE o.guild_id = players.guild_id AND o.user_id = players.user_id AND o.style LIKE 'champ:%')""")
RIOT_TABLES = {  # a member can link up to MAX_RIOT_ACCOUNTS Riot accounts
    "riot_accounts": """(
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    puuid     TEXT NOT NULL,
    riot_id   TEXT NOT NULL,                 -- 名字#TAG
    server    TEXT NOT NULL,                 -- e.g. tw2
    linked_at INTEGER NOT NULL,              -- unix time; only Flex games after this count
    PRIMARY KEY (guild_id, user_id, puuid)
)""",
    "riot_pending": """(                      -- linked before the Riot API key was set
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    riot_id    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, riot_id)
)""",
}
for table, columns in RIOT_TABLES.items():
    db.execute(f"CREATE TABLE IF NOT EXISTS {table} {columns}")
    key = [r[1] for r in sorted(db.execute(f"PRAGMA table_info({table})"), key=lambda r: r[5]) if r[5]]
    if key == ["guild_id", "user_id"]:  # older version allowed one account per member: widen the key
        db.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        db.execute(f"CREATE TABLE {table} {columns}")
        db.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
        db.execute(f"DROP TABLE {table}_old")
db.execute("""
CREATE TABLE IF NOT EXISTS flex_reviews (     -- Flex games held until the owner decides about unlinked teammates
    match_id   TEXT NOT NULL,
    guild_id   INTEGER NOT NULL,
    team_id    INTEGER NOT NULL,
    game       TEXT NOT NULL,                -- JSON: result, length, start, server, the team's players
    decisions  TEXT NOT NULL DEFAULT '{}',   -- JSON: puuid -> "ignore"
    created_at INTEGER NOT NULL,
    PRIMARY KEY (match_id, guild_id, team_id)
)""")
db.execute("CREATE TABLE IF NOT EXISTS riot_matches (match_id TEXT PRIMARY KEY, checked_at INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
if db.execute("SELECT 1 FROM meta WHERE key='start_credits_given'").fetchone() is None:
    # one-time: existing players also get the starting credits
    db.execute("UPDATE players SET credits = credits + ?", (START_CREDITS,))
    db.execute("INSERT INTO meta (key, value) VALUES ('start_credits_given', '1')")
db.commit()


def get_player(guild_id, user_id):
    """Return (elo, wins, losses), creating the player on first use."""
    row = db.execute(
        "SELECT elo, wins, losses FROM players WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO players (guild_id, user_id, elo, credits) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, START_ELO, START_CREDITS),
        )
        db.commit()
        return START_ELO, 0, 0
    return row


def record(guild_id, user_ids, won, change=ELO_CHANGE):
    """Apply a win (+change ELO) or loss (-change ELO) and credits to each player."""
    change = change if won else -change
    credits = CREDIT_WIN if won else CREDIT_LOSS
    for u in user_ids:
        get_player(guild_id, u)  # make sure the row exists
        db.execute(
            "UPDATE players SET elo = elo + ?, wins = wins + ?, losses = losses + ?, "
            "credits = credits + ? WHERE guild_id=? AND user_id=?",
            (change, int(won), int(not won), credits, guild_id, u),
        )


def get_wallet(guild_id, user_id):
    """Return (credits, equipped style, set of owned styles)."""
    get_player(guild_id, user_id)
    credits, style = db.execute(
        "SELECT credits, style FROM players WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    owned = {"dark"} | {
        r[0] for r in db.execute(
            "SELECT style FROM owned_styles WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
    }
    return credits, style, owned


def ids_to_str(ids):
    return ",".join(str(i) for i in ids)


def str_to_ids(s):
    return [int(x) for x in s.split(",") if x]


def open_match_of(guild_id, user_id):
    """Return the id of an unreported match (any mode) this user is in, or None."""
    rows = db.execute(
        "SELECT id, team_a, team_b FROM matches WHERE guild_id=? AND status='open'",
        (guild_id,),
    )
    for match_id, a, b in rows:
        if user_id in str_to_ids(a) + str_to_ids(b):
            return match_id
    return None


def avg(team):
    return sum(elo for _, elo in team) / len(team)


def balance_teams(players):
    """players: list of (user_id, elo). Returns the split with the smallest ELO gap."""
    players = players[:]
    random.shuffle(players)  # so equal splits don't always come out the same
    total = sum(elo for _, elo in players)
    first, rest = players[0], players[1:]
    best, best_gap = None, None
    for combo in itertools.combinations(rest, len(players) // 2 - 1):
        team_a = [first, *combo]
        gap = abs(total - 2 * sum(elo for _, elo in team_a))
        if best_gap is None or gap < best_gap:
            best, best_gap = team_a, gap
    a_ids = {u for u, _ in best}
    team_b = [p for p in players if p[0] not in a_ids]
    return best, team_b


def mix_change(winner_avg, loser_avg):
    """ELO each 10mix winner gains (and each loser loses), based on the average gap."""
    expected = 1 / (1 + 10 ** ((loser_avg - winner_avg) / 400))
    return max(1, round(MIX_K * (1 - expected)))


def team_text(team):
    return "\n".join(f"<@{u}>（{elo}）" for u, elo in sorted(team, key=lambda p: -p[1]))


# ---------- Bot ----------

intents = discord.Intents.default()
intents.message_content = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # The bot's owner = whoever owns the app in the Developer Portal (you)
        info = await self.application_info()
        self.owner_id = info.owner.id
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash commands")
        run_in_background(fill_spin_library())
        run_in_background(prepare_shop())
        run_in_background(watch_flex())


client = Bot()
queues = {}               # guild_id -> list of user_ids in the 10mix queue
queue_lock = asyncio.Lock()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if any(t in message.content for t in TRIGGERS):
        await message.reply(REPLY)


# ---------- 10mix queue ----------

@client.tree.command(name="join", description="加入 10mix 排隊")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    gid, uid = interaction.guild_id, interaction.user.id

    async with queue_lock:
        q = queues.setdefault(gid, [])
        if uid in q:
            await interaction.response.send_message("你已經在排隊中。", ephemeral=True)
            return
        match_id = open_match_of(gid, uid)
        if match_id:
            await interaction.response.send_message(
                f"你還在第 {match_id} 場比賽中，請先用 /result 10mix 回報結果。", ephemeral=True
            )
            return

        q.append(uid)
        if len(q) < MIX_SIZE:
            await interaction.response.send_message(
                f"{interaction.user.mention} 加入了排隊（{len(q)}/{MIX_SIZE}）"
            )
            return

        # Queue is full: start a match
        player_ids = q[:MIX_SIZE]
        del q[:MIX_SIZE]

    players = [(u, get_player(gid, u)[0]) for u in player_ids]
    team_a, team_b = balance_teams(players)
    cur = db.execute(
        "INSERT INTO matches (guild_id, team_a, team_b, mode) VALUES (?, ?, ?, 'mix')",
        (gid, ids_to_str(u for u, _ in team_a), ids_to_str(u for u, _ in team_b)),
    )
    db.commit()
    match_id = cur.lastrowid

    a_win, b_win = mix_change(avg(team_a), avg(team_b)), mix_change(avg(team_b), avg(team_a))
    embed = discord.Embed(
        title=f"⚔️ 10mix 第 {match_id} 場開始！",
        description=f"A 隊贏：±{a_win}｜B 隊贏：±{b_win}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name=f"🔵 A 隊（平均 {avg(team_a):.0f}）", value=team_text(team_a))
    embed.add_field(name=f"🔴 B 隊（平均 {avg(team_b):.0f}）", value=team_text(team_b))
    embed.set_footer(text=f"比賽結束後用 /result 10mix {match_id} 回報勝方")
    await interaction.response.send_message(
        content=" ".join(f"<@{u}>" for u in player_ids), embed=embed
    )


@client.tree.command(name="leave", description="離開 10mix 排隊")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    async with queue_lock:
        q = queues.setdefault(interaction.guild_id, [])
        if interaction.user.id not in q:
            await interaction.response.send_message("你不在排隊中。", ephemeral=True)
            return
        q.remove(interaction.user.id)
        await interaction.response.send_message(
            f"{interaction.user.mention} 離開了排隊（{len(q)}/{MIX_SIZE}）"
        )


@client.tree.command(name="queue", description="查看 10mix 排隊名單")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    q = queues.get(interaction.guild_id, [])
    if not q:
        await interaction.response.send_message("目前沒有人排隊。")
        return
    names = "\n".join(f"{i}. <@{u}>" for i, u in enumerate(q, 1))
    embed = discord.Embed(title=f"10mix 排隊中（{len(q)}/{MIX_SIZE}）", description=names)
    await interaction.response.send_message(embed=embed)


# ---------- /result 10mix and /result flex ----------

result_group = app_commands.Group(name="result", description="回報比賽結果", guild_only=True)


@result_group.command(name="10mix", description="回報 10mix 比賽結果")
@app_commands.describe(match_id="比賽編號", winner="勝方")
@app_commands.choices(winner=[
    app_commands.Choice(name="A 隊", value="A"),
    app_commands.Choice(name="B 隊", value="B"),
])
async def result_mix(interaction: discord.Interaction, match_id: int, winner: app_commands.Choice[str]):
    gid = interaction.guild_id
    row = db.execute(
        "SELECT team_a, team_b, status FROM matches WHERE id=? AND guild_id=? AND mode='mix'",
        (match_id, gid),
    ).fetchone()
    if row is None:
        await interaction.response.send_message("找不到這場 10mix 比賽。", ephemeral=True)
        return
    a_str, b_str, status = row
    if status != "open":
        await interaction.response.send_message("這場比賽已經回報或取消了。", ephemeral=True)
        return
    a_ids, b_ids = str_to_ids(a_str), str_to_ids(b_str)
    is_admin = interaction.user.guild_permissions.manage_guild
    if interaction.user.id not in a_ids + b_ids and not is_admin:
        await interaction.response.send_message("只有這場比賽的玩家或管理員可以回報。", ephemeral=True)
        return

    winners, losers = (a_ids, b_ids) if winner.value == "A" else (b_ids, a_ids)
    win_avg = sum(get_player(gid, u)[0] for u in winners) / len(winners)
    lose_avg = sum(get_player(gid, u)[0] for u in losers) / len(losers)
    points = mix_change(win_avg, lose_avg)

    record(gid, winners, won=True, change=points)
    record(gid, losers, won=False, change=points)
    db.execute("UPDATE matches SET status='done', winner=? WHERE id=?", (winner.value, match_id))
    db.commit()

    embed = discord.Embed(title=f"🏆 10mix 第 {match_id} 場：{winner.name}獲勝！", color=discord.Color.gold())
    embed.add_field(name=f"勝方 +{points}", value="\n".join(f"<@{u}>" for u in winners))
    embed.add_field(name=f"敗方 -{points}", value="\n".join(f"<@{u}>" for u in losers))
    embed.set_footer(text=f"金幣：勝方 +{CREDIT_WIN}｜敗方 +{CREDIT_LOSS}")
    await interaction.response.send_message(embed=embed)


@result_group.command(name="flex", description="記錄 Flex 結果（5 人同隊）")
@app_commands.describe(p1="玩家 1", p2="玩家 2", p3="玩家 3", p4="玩家 4", p5="玩家 5", outcome="輸定贏")
@app_commands.choices(outcome=[
    app_commands.Choice(name="贏", value="win"),
    app_commands.Choice(name="輸", value="loss"),
])
async def result_flex(interaction: discord.Interaction,
               p1: discord.Member, p2: discord.Member, p3: discord.Member,
               p4: discord.Member, p5: discord.Member,
               outcome: app_commands.Choice[str]):
    team = [p1, p2, p3, p4, p5]
    ids = [p.id for p in team]
    if len(set(ids)) != len(ids):
        await interaction.response.send_message("同一個人被選了兩次。", ephemeral=True)
        return
    if any(p.bot for p in team):
        await interaction.response.send_message("不能把 bot 加入隊伍。", ephemeral=True)
        return

    gid = interaction.guild_id
    won = outcome.value == "win"
    record(gid, ids, won=won)
    cur = db.execute(
        "INSERT INTO matches (guild_id, team_a, team_b, status, winner, mode) "
        "VALUES (?, ?, '', 'done', ?, 'flex')",
        (gid, ids_to_str(ids), outcome.value),
    )
    db.commit()

    sign = f"+{ELO_CHANGE}" if won else f"-{ELO_CHANGE}"
    lines = [f"<@{u}> {sign} → **{get_player(gid, u)[0]}**" for u in ids]
    embed = discord.Embed(
        title=f"🏆 Flex 第 {cur.lastrowid} 場：贏了！" if won else f"💀 Flex 第 {cur.lastrowid} 場：輸了",
        description="\n".join(lines),
        color=discord.Color.gold() if won else discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"金幣：每人 +{CREDIT_WIN if won else CREDIT_LOSS}")
    await interaction.response.send_message(embed=embed, ephemeral=True)  # only the reporter sees it


client.tree.add_command(result_group)


# ---------- Riot: ranked Flex games recorded automatically ----------
# Every few minutes, check linked players' ranked Flex games. A game where 3+ linked members
# of the same server were on the same team is recorded like /result flex and announced.

FLEX_CHECK_SECONDS = 120  # ~12 accounts = ~12 requests per check; Riot allows 100 per 2 minutes
FLEX_LOOKBACK = 3 * 86400  # never look further back than this
MIN_FLEX_MEMBERS = 3      # linked members needed on one team for a game to count
MAX_RIOT_ACCOUNTS = 2     # Riot accounts one member can link


def flex_channel(guild_id):
    row = db.execute("SELECT value FROM meta WHERE key=?", (f"flex_channel:{guild_id}",)).fetchone()
    return int(row[0]) if row else None


def champion_name(riot_name):
    """Match data uses ids like 'Darius' (and 'FiddleSticks'); show the Chinese name."""
    for cid, info in champions.CHAMPIONS.items():
        if cid.lower() == riot_name.lower():
            return info["name"]
    return riot_name


def riot_links(gid, uid):
    """Riot IDs a member is linked to (or waiting to be linked to)."""
    return [r[0] for table in ("riot_accounts", "riot_pending")
            for r in db.execute(f"SELECT riot_id FROM {table} WHERE guild_id=? AND user_id=?", (gid, uid))]


def riot_id_owner(gid, riot_id):
    """Which member a Riot ID is already linked to, if any."""
    for table in ("riot_accounts", "riot_pending"):
        row = db.execute(f"SELECT user_id FROM {table} WHERE guild_id=? AND lower(riot_id)=lower(?)",
                         (gid, riot_id)).fetchone()
        if row:
            return row[0]
    return None


async def resolve_riot_id(session, riot_id):
    """'名字#TAG' -> (puuid, 'Name#TAG' as Riot spells it, server), or None if there's no such account."""
    acc = await riot.account(session, riot_id)
    if not acc:
        return None
    return acc["puuid"], f"{acc['gameName']}#{acc['tagLine']}", await riot.server(session, acc["puuid"])


@client.tree.command(name="link", description=f"連結 Riot 帳號（自動記錄 Flex 積分場，每人最多 {MAX_RIOT_ACCOUNTS} 個）")
@app_commands.guild_only()
@app_commands.describe(riot_id="Riot ID，例如 收網大師#TW2", user="要連結的玩家（預設自己；可以幫未連結的人連結）")
async def link(interaction: discord.Interaction, riot_id: str, user: discord.Member = None):
    user = user or interaction.user
    gid, me = interaction.guild_id, interaction.user.id
    riot_id = riot_id.strip()
    current = riot_links(gid, user.id)
    if current and user.id != me and me != client.owner_id:
        await interaction.response.send_message(
            f"{user.mention} 已經連結咗 **{'、'.join(current)}**，只有本人或 bot 擁有者可以再加。", ephemeral=True)
        return
    name, _, tag = riot_id.rpartition("#")
    if not name or not tag:
        await interaction.response.send_message("Riot ID 格式係 名字#TAG，例如 收網大師#TW2。", ephemeral=True)
        return
    taken = riot_id_owner(gid, riot_id)
    if taken:
        text = "已經連結咗呢個帳號。" if taken == user.id else f"**{riot_id}** 已經連結咗 <@{taken}>。"
        await interaction.response.send_message(text, ephemeral=True)
        return
    if len(current) >= MAX_RIOT_ACCOUNTS:
        await interaction.response.send_message(
            f"{user.mention} 已經連結咗 {MAX_RIOT_ACCOUNTS} 個帳號（{'、'.join(current)}），"
            f"請先用 /unlink 取消一個。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    get_player(gid, user.id)
    now = int(time.time())  # only Flex games from now on count
    if not riot.api_key():  # no key yet: keep it, and finish linking once the key is set
        db.execute("INSERT OR IGNORE INTO riot_pending VALUES (?, ?, ?, ?)", (gid, user.id, riot_id, now))
        db.commit()
        await interaction.followup.send(
            f"✅ {user.mention} → **{riot_id}** 已記錄，Riot API key 設定好之後會自動完成連結。"
            f"由而家開始打嘅 Flex 積分場都會計。", ephemeral=True)
        return
    try:
        async with aiohttp.ClientSession() as session:
            found = await resolve_riot_id(session, riot_id)
    except riot.RiotError as e:
        await interaction.followup.send(f"連接 Riot 失敗：{e}", ephemeral=True)
        return
    if not found:
        await interaction.followup.send(f"找不到 Riot ID「{riot_id}」，格式係 名字#TAG。", ephemeral=True)
        return
    puuid, proper, srv = found
    other = db.execute("SELECT user_id FROM riot_accounts WHERE guild_id=? AND puuid=?", (gid, puuid)).fetchone()
    if other:  # same account under an older name
        await interaction.followup.send(f"**{proper}** 已經連結咗 <@{other[0]}>。", ephemeral=True)
        return
    db.execute("INSERT INTO riot_accounts VALUES (?, ?, ?, ?, ?, ?)", (gid, user.id, puuid, proper, srv, now))
    db.commit()
    await interaction.followup.send(
        f"✅ {user.mention} 已連結 **{proper}**（{srv}）。由而家開始，同其他已連結成員（{MIN_FLEX_MEMBERS} 人或以上同隊）"
        f"打嘅 Flex 積分場會自動記錄。", ephemeral=True)


@client.tree.command(name="unlink", description="取消連結 Riot 帳號")
@app_commands.guild_only()
@app_commands.describe(riot_id="只取消呢個帳號（預設全部）", user="要取消連結的玩家（只限 bot 擁有者）")
async def unlink(interaction: discord.Interaction, riot_id: str = None, user: discord.Member = None):
    user = user or interaction.user
    if user.id != interaction.user.id and interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以幫其他人取消連結。", ephemeral=True)
        return
    gone = []
    for table in ("riot_accounts", "riot_pending"):
        query = f"FROM {table} WHERE guild_id=? AND user_id=?"
        args = [interaction.guild_id, user.id]
        if riot_id:
            query += " AND lower(riot_id)=lower(?)"
            args.append(riot_id.strip())
        gone += [r[0] for r in db.execute(f"SELECT riot_id {query}", args)]
        db.execute(f"DELETE {query}", args)
    db.commit()
    await interaction.response.send_message(
        f"已取消 {user.mention} 的連結：{'、'.join(gone)}" if gone else f"{user.mention} 沒有連結這個 Riot 帳號。",
        ephemeral=True)


@client.tree.command(name="flexchannel", description="設定自動 Flex 結果公佈的頻道（只限 bot 擁有者）")
@app_commands.guild_only()
async def flexchannel(interaction: discord.Interaction):
    if interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以使用這個指令。", ephemeral=True)
        return
    gid = interaction.guild_id
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"flex_channel:{gid}", str(interaction.channel_id)))
    db.commit()
    linked = [r[0] for r in db.execute("SELECT riot_id FROM riot_accounts WHERE guild_id=?", (gid,))]
    pending = [r[0] for r in db.execute("SELECT riot_id FROM riot_pending WHERE guild_id=?", (gid,))]
    text = f"✅ 自動 Flex 結果會公佈喺呢個頻道。\n已連結 {len(linked)} 個帳號：" + ("、".join(linked) or "（未有）")
    if pending:
        text += f"\n等待完成連結 {len(pending)} 個：" + "、".join(pending)
    await interaction.response.send_message(text, ephemeral=True)


async def finish_pending_links(session):
    """Links saved before the Riot API key was set: look the accounts up now."""
    for gid, uid, riot_id, created in db.execute("SELECT * FROM riot_pending").fetchall():
        found = await resolve_riot_id(session, riot_id)
        db.execute("DELETE FROM riot_pending WHERE guild_id=? AND user_id=? AND riot_id=?", (gid, uid, riot_id))
        if found:
            puuid, proper, srv = found
            db.execute("INSERT OR IGNORE INTO riot_accounts VALUES (?, ?, ?, ?, ?, ?)",
                       (gid, uid, puuid, proper, srv, created))
            print(f"Linked {uid} to {proper} ({srv})")
        else:
            print(f"Couldn't link {uid}: no Riot account called {riot_id}")
        db.commit()


TEAM_KEYS = ("puuid", "teamId", "win", "championName", "kills", "deaths", "assists",
             "riotIdGameName", "riotIdTagline", "summonerName")


def riot_name(p):
    return f"{p['riotIdGameName']}#{p['riotIdTagline']}" if p.get("riotIdGameName") else (p.get("summonerName") or "?")


def linked_puuids(gid):
    """puuid -> Discord user id, for everyone linked in this server."""
    return dict(db.execute("SELECT puuid, user_id FROM riot_accounts WHERE guild_id=?", (gid,)).fetchall())


def team_members(gid, game):
    """[(user_id, player)] for the linked members on the team (one entry per person)."""
    linked = linked_puuids(gid)
    members = {}
    for p in game["team"]:
        if p["puuid"] in linked:
            members.setdefault(linked[p["puuid"]], p)
    return list(members.items())


async def record_flex_game(match_id, data):
    """A finished ranked Flex game: for every server with 3+ linked members on one team, record it for
    them; teammates nobody has linked are kept for the owner to link or skip (/flexreview)."""
    info = data["info"]
    started = info["gameStartTimestamp"] // 1000
    if info.get("gameDuration", 0) < 300:  # remake
        return
    players = {p["puuid"]: p for p in info["participants"]}
    rows = db.execute("SELECT guild_id, user_id, puuid FROM riot_accounts WHERE linked_at <= ?", (started,))
    teams = {}  # (guild, teamId) -> {user_id: player}
    for gid, uid, puuid in rows:
        if puuid in players:
            teams.setdefault((gid, players[puuid]["teamId"]), {})[uid] = players[puuid]
    for (gid, team_id), found in teams.items():
        if len(found) < MIN_FLEX_MEMBERS:
            continue
        team = [{k: p.get(k) for k in TEAM_KEYS} for p in info["participants"] if p["teamId"] == team_id]
        game = {"won": team[0]["win"], "duration": info["gameDuration"], "start": started,
                "server": info.get("platformId", riot.DEFAULT_SERVER).lower(), "team": team}
        number = await finish_flex_game(gid, match_id, game)  # linked members count straight away
        linked = linked_puuids(gid)
        if any(p["puuid"] not in linked for p in team):  # unlinked teammates wait for the owner
            game["number"] = number
            held = db.execute("INSERT OR IGNORE INTO flex_reviews VALUES (?, ?, ?, ?, '{}', ?)",
                              (match_id, gid, team_id, json.dumps(game, ensure_ascii=False), int(time.time()))).rowcount
            db.commit()
            if held:  # tell the owner once per game
                await tell_owner_about_review(gid, match_id, game)


async def finish_flex_game(gid, match_id, game):
    """Record the game for the team's linked members and announce it. Returns its match number."""
    members = team_members(gid, game)
    ids = [uid for uid, _ in members]
    won = game["won"]
    record(gid, ids, won=won)
    cur = db.execute(
        "INSERT INTO matches (guild_id, team_a, team_b, status, winner, mode) VALUES (?, ?, '', 'done', ?, 'flex')",
        (gid, ids_to_str(ids), "win" if won else "loss"),
    )
    db.commit()
    await announce_flex(gid, cur.lastrowid, match_id, {"gameDuration": game["duration"]}, members, won)
    return cur.lastrowid


def review_embed(gid, match_id, game, decisions):
    linked = linked_puuids(gid)
    lines = []
    for p in game["team"]:
        stats = f"{champion_name(p['championName'])} {p['kills']}/{p['deaths']}/{p['assists']}"
        choice = decisions.get(p["puuid"])
        if p["puuid"] in linked:
            lines.append(f"✅ <@{linked[p['puuid']]}>（{riot_name(p)}）{stats}")
        elif choice == "ignore":
            lines.append(f"⏭️ ~~{riot_name(p)}~~ {stats}（略過）")
        else:
            lines.append(f"❓ **{riot_name(p)}** {stats}")
    started = datetime.fromtimestamp(game["start"], HKT).strftime("%m/%d %H:%M")
    return discord.Embed(
        title=f"📋 Flex 第 {game.get('number', '?')} 場：{'贏了' if game['won'] else '輸了'}（{started}，{game['duration'] // 60} 分鐘）",
        description="\n".join(lines) + "\n\n✅ 已連結成員已經計分　❓ 未連結的隊友：連結到成員（會補計呢場）或者略過",
        color=discord.Color.blurple(),
    ).set_footer(text=match_id)


async def tell_owner_about_review(gid, match_id, game):
    """Private message to the bot owner: this game has teammates nobody has linked."""
    try:
        owner = client.get_user(client.owner_id) or await client.fetch_user(client.owner_id)
        embed = review_embed(gid, match_id, game, {})
        await owner.send(content="有一場 Flex 有未連結的隊友（已連結成員已經計分），喺伺服器用 **/flexreview** 決定連結定略過。",
                         embed=embed)
    except discord.HTTPException as e:
        print(f"Couldn't DM the owner about {match_id}: {e!r}")


async def announce_flex(gid, number, match_id, info, members, won):
    channel_id = flex_channel(gid)
    channel = client.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    sign = f"+{ELO_CHANGE}" if won else f"-{ELO_CHANGE}"
    lines = []
    for uid, p in members:
        kda = f"{p['kills']}/{p['deaths']}/{p['assists']}"
        lines.append(f"<@{uid}> {champion_name(p['championName'])} {kda}　{sign} → **{get_player(gid, uid)[0]}**")
    embed = discord.Embed(
        title=f"🏆 Flex 第 {number} 場（自動記錄）：贏了！" if won else f"💀 Flex 第 {number} 場（自動記錄）：輸了",
        description="\n".join(lines),
        color=discord.Color.gold() if won else discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"金幣：每人 +{CREDIT_WIN if won else CREDIT_LOSS}｜{info['gameDuration'] // 60} 分鐘｜{match_id}")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"Couldn't announce Flex game {match_id}: {e!r}")


async def check_flex_games(session):
    accounts = db.execute("SELECT puuid, server, MIN(linked_at) FROM riot_accounts GROUP BY puuid, server").fetchall()
    found = {}
    for puuid, srv, linked_at in accounts:
        since = max(linked_at, int(time.time()) - FLEX_LOOKBACK)
        for match_id in await riot.flex_match_ids(session, puuid, srv, since):
            found[match_id] = srv
    for match_id, srv in sorted(found.items()):
        if db.execute("SELECT 1 FROM riot_matches WHERE match_id=?", (match_id,)).fetchone():
            continue
        data = await riot.match(session, srv, match_id)
        if not data or not data["info"].get("gameEndTimestamp"):
            continue  # not finished yet: look again next time
        # mark it checked first: if recording fails halfway, the game is skipped rather than counted twice
        db.execute("INSERT OR IGNORE INTO riot_matches VALUES (?, ?)", (match_id, int(time.time())))
        db.commit()
        await record_flex_game(match_id, data)


async def refresh_accounts_if_key_changed(session):
    """Riot encrypts account ids (puuid) differently for every API key, so after a key change
    look every linked account up again. Only a fingerprint of the key is stored."""
    fingerprint = hashlib.sha256(riot.api_key().encode()).hexdigest()[:16]
    row = db.execute("SELECT value FROM meta WHERE key='riot_key'").fetchone()
    if row and row[0] == fingerprint:
        return
    for gid, uid, old, riot_id in db.execute("SELECT guild_id, user_id, puuid, riot_id FROM riot_accounts").fetchall():
        found = await resolve_riot_id(session, riot_id)
        if found:
            db.execute("UPDATE riot_accounts SET puuid=?, riot_id=?, server=? WHERE guild_id=? AND user_id=? AND puuid=?",
                       (*found, gid, uid, old))
        else:
            print(f"Couldn't find {riot_id} with the new Riot key")
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('riot_key', ?)", (fingerprint,))
    db.commit()
    print("Riot key changed: linked accounts refreshed")


async def watch_flex():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            if riot.api_key():
                try:
                    await refresh_accounts_if_key_changed(session)
                    await finish_pending_links(session)
                    await check_flex_games(session)
                except Exception as e:  # e.g. an expired key: keep the bot running, try again later
                    print(f"Flex check failed: {e!r}")
            await asyncio.sleep(FLEX_CHECK_SECONDS)


# ---------- Admin / owner commands ----------

@client.tree.command(name="cancel", description="取消比賽（不計 ELO）")
@app_commands.guild_only()
@app_commands.describe(match_id="比賽編號")
async def cancel(interaction: discord.Interaction, match_id: int):
    cur = db.execute(
        "UPDATE matches SET status='cancelled' WHERE id=? AND guild_id=? AND status='open'",
        (match_id, interaction.guild_id),
    )
    db.commit()
    if cur.rowcount == 0:
        await interaction.response.send_message("找不到進行中的這場比賽。", ephemeral=True)
    else:
        await interaction.response.send_message(f"第 {match_id} 場比賽已取消，ELO 不變。")


@client.tree.command(name="setelo", description="設定玩家的 ELO（只限 bot 擁有者）")
@app_commands.guild_only()
@app_commands.describe(user="要修改的玩家", value="新的 ELO")
async def setelo(interaction: discord.Interaction, user: discord.Member, value: app_commands.Range[int, 0, 10000]):
    if interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以使用這個指令。", ephemeral=True)
        return
    old, _, _ = get_player(interaction.guild_id, user.id)
    db.execute(
        "UPDATE players SET elo=? WHERE guild_id=? AND user_id=?",
        (value, interaction.guild_id, user.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"已把 **{user.display_name}** 的 ELO 由 {old} 改為 {value}。",
        ephemeral=True,
    )


@client.tree.command(name="givecoins", description="送金幣給玩家（只限 bot 擁有者）")
@app_commands.guild_only()
@app_commands.describe(user="要送金幣的玩家", amount="金幣數量")
async def givecoins(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 100000]):
    if interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以使用這個指令。", ephemeral=True)
        return
    get_player(interaction.guild_id, user.id)
    db.execute(
        "UPDATE players SET credits = credits + ? WHERE guild_id=? AND user_id=?",
        (amount, interaction.guild_id, user.id),
    )
    db.commit()
    credits = get_wallet(interaction.guild_id, user.id)[0]
    await interaction.response.send_message(f"🎁 {user.mention} 獲得 **{amount} 金幣**！現有金幣：{credits}")


def all_cards():
    """Every card there is: the basic styles, then every champion."""
    return list(STYLES) + [champions.style_key(c) for c in champions.CHAMPIONS]


def card_subtitle(key):
    if champions.is_champion(key):
        return champions.CHAMPIONS.get(champions.champion_id(key), {}).get("title", "")
    return "基本款式"


@client.tree.command(name="givecard", description="送卡面給玩家（只限 bot 擁有者）")
@app_commands.guild_only()
@app_commands.describe(user="要送卡面的玩家", card="卡面（打字搜尋，例如 達瑞斯 / Darius / 黃金）")
async def givecard(interaction: discord.Interaction, user: discord.Member, card: str):
    if interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以使用這個指令。", ephemeral=True)
        return
    if card not in all_cards():  # typed something without picking from the list
        await interaction.response.send_message(f"找不到「{card}」，請喺選單揀一個卡面。", ephemeral=True)
        return
    gid = interaction.guild_id
    if card in get_wallet(gid, user.id)[2]:
        await interaction.response.send_message(f"{user.display_name} 已經擁有「{card_label(card)}」。", ephemeral=True)
        return
    db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)", (gid, user.id, card))
    db.commit()
    await interaction.response.defer()  # the card preview may need drawing first
    extra = ""
    if champions.is_champion(card):
        collected = sum(champions.is_champion(k) for k in get_wallet(gid, user.id)[2])
        extra = f"（英雄卡已收集 {collected}/{len(champions.CHAMPIONS)}）"
    await interaction.followup.send(
        content=f"🎁 {user.mention} 獲得卡面「**{card_label(card)}**」！{extra}\n用 /shop →「更換卡面」套用",
        file=discord.File(io.BytesIO(await card_preview(card)), filename="card.png"),
    )


@givecard.autocomplete("card")
async def givecard_search(interaction: discord.Interaction, current: str):
    query = current.strip().lower()
    choices = []
    for key in all_cards():
        name, sub = card_label(key), card_subtitle(key)
        if query in f"{name} {key} {sub}".lower():
            choices.append(app_commands.Choice(name=f"{name}・{sub}" if sub else name, value=key))
            if len(choices) == 25:  # Discord shows at most 25
                break
    return choices


# ---------- Stats commands ----------

def mode_stats(guild_id, user_id):
    """Return ((mix_games, mix_wins), (flex_games, flex_wins)) for one player."""
    res = {"mix": [0, 0], "flex": [0, 0]}
    for team, mode, won in team_results(guild_id):
        if user_id in team:
            res[mode][0] += 1
            res[mode][1] += int(won)
    return tuple(res["mix"]), tuple(res["flex"])


@client.tree.command(name="stat", description="查看個人戰績")
@app_commands.guild_only()
@app_commands.describe(user="要查看的玩家（預設自己）")
async def stat(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    await interaction.response.defer()  # drawing the image can take a moment

    gid = interaction.guild_id
    rating = get_player(gid, user.id)[0]
    mix, flex = mode_stats(gid, user.id)
    try:
        avatar = await user.display_avatar.with_static_format("png").with_size(256).read()
    except discord.HTTPException:
        avatar = None

    credits, style, _ = get_wallet(gid, user.id)
    png = await asyncio.to_thread(
        lambda: render_stat_card(user.display_name, avatar, rating, credits, mix, flex, card_theme(style))
    )
    await interaction.followup.send(file=discord.File(png, filename="stat.png"))


@client.tree.command(name="leaderboard", description="ELO 排行榜：最強 / 炸裂")
@app_commands.guild_only()
@app_commands.describe(kind="最強（ELO 最高）或炸裂（ELO 最低）")
@app_commands.choices(kind=[
    app_commands.Choice(name="最強（ELO 最高）", value="best"),
    app_commands.Choice(name="炸裂（ELO 最低）", value="worst"),
])
async def leaderboard(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    best = kind.value == "best"
    rows = db.execute(  # only people who have played (0 勝 0 負 left out)
        "SELECT user_id, elo, wins, losses FROM players WHERE guild_id=? AND wins + losses > 0 "
        + ("ORDER BY elo DESC, wins DESC LIMIT 10" if best else "ORDER BY elo ASC, losses DESC LIMIT 10"),
        (interaction.guild_id,),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("還沒有人打過比賽。")
        return
    medals = ["🥇", "🥈", "🥉"] if best else ["💥", "💥", "💥"]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} <@{u}> — {rating}（{w} 勝 {l} 負）"
        for i, (u, rating, w, l) in enumerate(rows)
    ]
    embed = discord.Embed(title="🏅 ELO 排行榜：最強" if best else "💥 ELO 排行榜：炸裂",
                          description="\n".join(lines),
                          color=discord.Color.gold() if best else discord.Color.dark_red())
    await interaction.response.send_message(embed=embed)


# ---------- Card style shop ----------

CHAMP_SPIN_COST = 300  # 英雄卡轉盤: always a champion card you don't own yet


async def prepare_shop():
    for old in CARD_PREVIEW_DIR.glob("*"):  # previews drawn by an older card design
        if old.name != CARD_PREVIEW_VERSION:
            shutil.rmtree(old, ignore_errors=True)
    await asyncio.to_thread(champions.load_list)
    await asyncio.to_thread(render_shop_preview)  # draw it once now, so the buy page opens quickly
    await asyncio.to_thread(champions.download_all)


# Generic previews of each card (placeholder name and stats), drawn once and kept on disk.
# The version is a fingerprint of the card-drawing code, so a redesign redraws them.
CARD_PREVIEW_DIR = Path(__file__).with_name("card_previews")
CARD_PREVIEW_VERSION = hashlib.sha1(b"".join(
    Path(__file__).with_name(f).read_bytes() for f in ("stat_card.py", "regions.py", "champions.py")
)).hexdigest()[:12]
card_render_lock = asyncio.Lock()  # one preview at a time, so scrolling through cards can't pile up work


def card_preview_path(style):
    return CARD_PREVIEW_DIR / CARD_PREVIEW_VERSION / f"{style.replace(':', '_')}.png"


def draw_card_preview(style):
    img = render_card_image("預覽", None, 1200, 520, (20, 12), (10, 6), card_theme(style))
    path = card_preview_path(style)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    img.convert("RGB").resize((900, 500), Image.LANCZOS).save(tmp, "PNG", optimize=True)
    tmp.replace(path)


async def card_preview(style):
    """PNG bytes of the card's generic preview; drawn the first time it's needed."""
    path = card_preview_path(style)
    if not path.exists():
        async with card_render_lock:
            if not path.exists():  # someone else may have drawn it while we waited
                await asyncio.to_thread(draw_card_preview, style)
    return path.read_bytes()


async def attach_card(msg, preview):
    """Add the won card's preview under the spin animation. `preview` = task drawing it."""
    try:
        png = await preview
        await msg.edit(attachments=[*msg.attachments, discord.File(io.BytesIO(png), filename="card.png")])
    except Exception as e:
        print(f"Couldn't attach card preview: {e!r}")


def card_label(style):
    if champions.is_champion(style):
        return champions.label(style)
    return STYLES.get(style, STYLES["dark"])[0]


def card_theme(style):
    """What render_stat_card needs: a STYLES key, or the colours of a champion card."""
    return champions.theme(style) if champions.is_champion(style) else style


def owned_cards(owned):
    """Basic styles first (in shop order), then champions by name."""
    basic = [k for k in STYLES if k in owned]
    champs = sorted((k for k in owned if champions.is_champion(k)), key=champions.label)
    return basic + champs


class MenuView(discord.ui.View):
    """A private menu (/shop, /spin) whose pages are switched with buttons, editing the same message."""

    def __init__(self, interaction):
        super().__init__(timeout=840)  # interaction tokens expire after 15 minutes
        self.interaction = interaction
        self.user_id = interaction.user.id
        self.page = "home"

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    def button(self, label, callback, style=discord.ButtonStyle.secondary, disabled=False, row=None):
        b = discord.ui.Button(label=label, style=style, disabled=disabled, row=row)
        b.callback = callback
        self.add_item(b)

    def go(self, page):
        async def callback(interaction):
            await self.show(interaction, page)
        return callback

    def build(self, gid, uid, notice=None):
        """Lay out the current page: return its embed and set the buttons."""
        raise NotImplementedError

    async def image(self):
        """Picture for the current page as a discord.File, or None."""
        return None

    async def open(self, interaction):
        await interaction.response.defer(ephemeral=True)  # only the person using it sees the menu
        embed = self.build(interaction.guild_id, interaction.user.id)
        file = await self.image()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True, **({"file": file} if file else {}))

    async def show(self, interaction, page=None, notice=None):
        if page:
            self.page = page
        self.interaction = interaction
        if not interaction.response.is_done():
            await interaction.response.defer()
        embed = self.build(interaction.guild_id, interaction.user.id, notice)
        file = await self.image()
        await interaction.edit_original_response(embed=embed, view=self, attachments=[file] if file else [])

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


class ShopView(MenuView):
    """/shop: a home page with 購買 and 更換卡面, the buy page, and the card switcher."""

    PER_PAGE = 25  # Discord's limit for one dropdown

    def __init__(self, interaction):
        super().__init__(interaction)
        self.switch_page = 0
        self.selected = None  # card picked in 更換卡面, previewed before applying

    def build(self, gid, uid, notice=None):
        credits, equipped, owned = get_wallet(gid, uid)
        collected = sum(champions.is_champion(k) for k in owned)
        total = len(champions.CHAMPIONS)
        self.clear_items()
        embed = discord.Embed(color=discord.Color.gold())

        if self.page == "home":
            embed.title = "🛒 戰績卡商店"
            embed.description = (f"你的金幣：**{credits:,}**\n目前卡面：**{card_label(equipped)}**\n"
                                 f"英雄卡：已收集 **{collected}/{total}**（用 /spin 英雄卡轉盤抽）")
            self.button("🛒 購買", self.go("buy"), discord.ButtonStyle.primary)
            self.button("🎨 更換卡面", self.go("switch"), discord.ButtonStyle.success)

        elif self.page == "buy":
            lines = []
            for key, (label, price, _) in STYLES.items():
                status = "已擁有" if key in owned else "🎡 /spin 幸運轉盤限定" if price is None else f"🪙 {price} 金幣"
                lines.append(f"**{label}**　{status}")
            lines.append(f"\n🎰 英雄卡：用 /spin →「英雄卡轉盤」抽（已收集 {collected}/{total}）")
            embed.title = "🛒 購買"
            embed.description = "\n".join(lines) + f"\n\n你的金幣：**{credits:,}**"
            embed.set_image(url="attachment://shop.png")
            for key, (label, price, _) in STYLES.items():
                if price is None or price == 0:
                    continue
                if key in owned:
                    self.button(f"✅ {label}", self.buy(key), disabled=True)
                else:
                    self.button(f"購買 {label}（{price}）", self.buy(key), disabled=credits < price)
            self.button("⬅ 返回", self.go("home"), row=2)

        else:  # switch
            cards = owned_cards(owned)
            pages = max(1, -(-len(cards) // self.PER_PAGE))
            self.switch_page = min(self.switch_page, pages - 1)
            shown = cards[self.switch_page * self.PER_PAGE:(self.switch_page + 1) * self.PER_PAGE]
            picked = self.selected if self.selected in owned else None
            embed.title = "🎨 更換卡面"
            embed.description = (f"目前卡面：**{card_label(equipped)}**\n已擁有 {len(cards)} 款"
                                 f"（英雄卡 {collected}/{total}）\n")
            if picked:
                embed.description += f"預覽：**{card_label(picked)}**"
                embed.set_image(url="attachment://card.png")
            else:
                embed.description += "在下面選擇卡面，可以先預覽再套用"
            select = discord.ui.Select(
                placeholder=f"選擇卡面（第 {self.switch_page + 1}/{pages} 頁）",
                options=[discord.SelectOption(
                    label=card_label(k), value=k, default=k == (picked or equipped),
                    description=(champions.CHAMPIONS.get(champions.champion_id(k), {}).get("title")
                                 if champions.is_champion(k) else "基本款式") or None,
                ) for k in shown],
            )
            select.callback = self.pick
            self.add_item(select)
            if picked:
                in_use = picked == equipped
                self.button("✅ 使用中" if in_use else f"✅ 套用「{card_label(picked)}」", self.equip,
                            discord.ButtonStyle.success, disabled=in_use, row=1)
            if pages > 1:
                self.button("◀", self.turn(-1), disabled=self.switch_page == 0, row=1)
                self.button("▶", self.turn(1), disabled=self.switch_page >= pages - 1, row=1)
            self.button("⬅ 返回", self.go("home"), row=1)

        if notice:
            embed.description = f"{notice}\n\n{embed.description}"
        return embed

    async def image(self):
        if self.page == "buy":
            return discord.File(await asyncio.to_thread(render_shop_preview), filename="shop.png")
        if self.page == "switch" and self.selected:
            return discord.File(io.BytesIO(await card_preview(self.selected)), filename="card.png")
        return None

    def turn(self, step):
        async def callback(interaction):
            self.switch_page += step
            await self.show(interaction)
        return callback

    async def pick(self, interaction):
        """Choosing a card in the dropdown shows its preview; 套用 then equips it."""
        self.selected = interaction.data["values"][0]
        await self.show(interaction)

    async def equip(self, interaction):
        key = self.selected
        gid, uid = interaction.guild_id, interaction.user.id
        if key in get_wallet(gid, uid)[2]:
            db.execute("UPDATE players SET style=? WHERE guild_id=? AND user_id=?", (key, gid, uid))
            db.commit()
            notice = f"✅ 已換成「{card_label(key)}」卡面，用 /stat 睇吓！"
        else:
            notice = "你還沒有這款卡面。"
        await self.show(interaction, notice=notice)

    def buy(self, key):
        async def callback(interaction):
            gid, uid = interaction.guild_id, interaction.user.id
            credits, _, owned = get_wallet(gid, uid)
            label, price, _ = STYLES[key]
            if key in owned:
                notice = f"你已經擁有「{label}」。"
            elif credits < price:
                notice = f"金幣不足：「{label}」需要 {price}，你只有 {credits}。"
            else:
                db.execute(
                    "UPDATE players SET credits = credits - ?, style = ? WHERE guild_id=? AND user_id=?",
                    (price, key, gid, uid),
                )
                db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)", (gid, uid, key))
                db.commit()
                notice = f"🎉 你購買了「{label}」並已套用！"
            await self.show(interaction, notice=notice)
        return callback


@client.tree.command(name="shop", description="戰績卡商店：購買及更換卡面")
@app_commands.guild_only()
async def shop(interaction: discord.Interaction):
    await ShopView(interaction).open(interaction)


# ---------- Lucky wheel ----------

spin_lock = asyncio.Lock()

# Library of ready-made spin GIFs on disk: spin_gifs/<version>/<prize>/<n>.gif
# /spin sends a random one for the rolled prize, then redraws one in the background
# so the library slowly changes. The version is a fingerprint of wheel.py: edit the
# wheel or its prizes and the old GIFs are thrown away and redrawn automatically.
SPIN_LIBRARY_SIZE = 80            # GIFs kept in total, shared out by each prize's chance
SPIN_DIR = Path(__file__).with_name("spin_gifs")
SPIN_VERSION = hashlib.sha1(Path(__file__).with_name("wheel.py").read_bytes()).hexdigest()[:12]
PRIZE_INDEX = {p[0]: i for i, p in enumerate(PRIZES)}
# common prizes are seen often, so they get more versions (repeats are less noticeable);
# rare ones still get 2
SPIN_VARIANTS = {
    p[0]: max(2, round(SPIN_LIBRARY_SIZE * p[2] / sum(q[2] for q in PRIZES))) for p in PRIZES
}
spin_render_lock = asyncio.Lock()  # draw one GIF at a time: the server's CPU is small
background_tasks = set()


def spin_variants(key):
    return list((SPIN_DIR / SPIN_VERSION / key).glob("*.gif"))


async def draw_spin_gif(key, slot):
    """Draw a fresh GIF for this prize, save it in the library and return its bytes."""
    async with spin_render_lock:
        gif = (await asyncio.to_thread(render_spin_gif, PRIZE_INDEX[key])).getvalue()
    path = SPIN_DIR / SPIN_VERSION / key / f"{slot}.gif"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gif)
    tmp.replace(path)  # swap in whole, so a spin never reads a half-written file
    return gif


async def fill_spin_library():
    for old in SPIN_DIR.glob("*"):  # GIFs drawn by an older wheel.py
        if old.name != SPIN_VERSION:
            shutil.rmtree(old, ignore_errors=True)
    for key, count in SPIN_VARIANTS.items():  # extra GIFs from when a prize was allowed more
        for path in spin_variants(key):
            if not path.stem.isdigit() or int(path.stem) >= count:
                path.unlink(missing_ok=True)
    drawn = 0
    for slot in range(max(SPIN_VARIANTS.values())):  # round by round, so every prize gets one early
        for key, count in SPIN_VARIANTS.items():
            if slot < count and not (SPIN_DIR / SPIN_VERSION / key / f"{slot}.gif").exists():
                try:
                    await draw_spin_gif(key, slot)
                    drawn += 1
                except Exception as e:
                    print(f"Couldn't draw spin GIF {key}/{slot}: {e!r}")
    print(f"Spin library ready ({drawn} new GIFs drawn)")


async def refresh_spin_gif(key):
    if spin_render_lock.locked():  # already drawing: skip rather than build a queue
        return
    try:
        await draw_spin_gif(key, random.randrange(SPIN_VARIANTS[key]))
    except Exception as e:
        print(f"Couldn't refresh spin GIF {key}: {e!r}")


def run_in_background(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(background_tasks.discard)


def hk_today():
    return datetime.now(HKT).date().isoformat()


def time_until_reset():
    now = datetime.now(HKT)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    hours, rem = divmod(int((midnight - now).total_seconds()), 3600)
    return f"{hours} 小時 {rem // 60} 分鐘"


def lucky_payment(gid, uid):
    """How the next 幸運轉盤 spin gets paid: (text, can pay?). Order: daily free → bonus spin → coins."""
    credits = get_wallet(gid, uid)[0]
    last_free, bonus = db.execute(
        "SELECT last_free_spin, bonus_spins FROM players WHERE guild_id=? AND user_id=?", (gid, uid)
    ).fetchone()
    if last_free != hk_today():
        return "今日免費轉", True
    if bonus > 0:
        return "免費加轉", True
    return f"{SPIN_COST} 金幣", credits >= SPIN_COST


def champion_spin_free(gid, uid):
    """The first 英雄卡轉盤 spin is free."""
    get_player(gid, uid)
    return db.execute("SELECT champ_spins FROM players WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0] == 0


@lru_cache(maxsize=1)
def lucky_wheel_png():
    buf = io.BytesIO()
    wheel.frame(0, phase=0, all_on=True).convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


_champion_reel_png = {}


def champion_reel_png():
    count = len(champions.CHAMPIONS)  # redrawn once the champion list has loaded
    if count not in _champion_reel_png:
        _champion_reel_png[count] = render_reel_preview(list(champions.CHAMPIONS))
    return _champion_reel_png[count]


class SpinView(MenuView):
    """/spin: pick a wheel, see it, spin it. The spin itself is shown to everyone in the channel."""

    def build(self, gid, uid, notice=None):
        credits, _, owned = get_wallet(gid, uid)
        collected = sum(champions.is_champion(k) for k in owned)
        total = len(champions.CHAMPIONS)
        free = champion_spin_free(gid, uid)
        champ_cost = "**首次免費！**" if free else f"{CHAMP_SPIN_COST} 金幣"
        self.clear_items()
        embed = discord.Embed(color=discord.Color.gold())

        if self.page == "home":
            payment, _ = lucky_payment(gid, uid)
            embed.title = "🎡 轉盤"
            embed.description = (
                f"🎡 **幸運轉盤**：每日 1 次免費，之後每次 {SPIN_COST} 金幣（下一轉：{payment}）\n"
                f"贏金幣、免費加轉，仲有機會抽中限定「彩虹」卡面\n\n"
                f"🎰 **英雄卡轉盤**：{champ_cost}，必定抽到未擁有的英雄卡（已收集 {collected}/{total}）\n\n"
                f"你的金幣：**{credits:,}**"
            )
            self.button("🎡 幸運轉盤", self.go("lucky"), discord.ButtonStyle.primary)
            self.button("🎰 英雄卡轉盤", self.go("champion"), discord.ButtonStyle.success)

        elif self.page == "lucky":
            payment, can_pay = lucky_payment(gid, uid)
            bonus = db.execute("SELECT bonus_spins FROM players WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0]
            weights = sum(p[2] for p in PRIZES)
            odds = "　".join(f"{p[1]} {p[2] / weights:.1%}".replace(".0%", "%") for p in sorted(PRIZES, key=lambda p: -p[2]))
            today = "可用 ✅" if payment == "今日免費轉" else f"已用（{time_until_reset()}後重置）"
            embed.title = "🎡 幸運轉盤"
            embed.description = (f"今日免費：{today}\n免費加轉：{bonus} 次\n之後每次 {SPIN_COST} 金幣\n"
                                 f"你的金幣：**{credits:,}**\n\n{odds}")
            embed.set_image(url="attachment://wheel.png")
            self.button(f"🎡 轉！（{payment}）", self.lucky_spin, discord.ButtonStyle.success, disabled=not can_pay)
            self.button("⬅ 返回", self.go("home"))

        else:  # champion
            embed.title = "🎰 英雄卡轉盤"
            embed.description = (f"必定抽到你**未擁有**的英雄卡\n費用：{champ_cost}\n"
                                 f"已收集：**{collected}/{total}**\n你的金幣：**{credits:,}**")
            embed.set_image(url="attachment://wheel.png")
            label = "🎰 抽！（首次免費）" if free else f"🎰 抽！（{CHAMP_SPIN_COST} 金幣）"
            self.button(label, self.champion_spin, discord.ButtonStyle.success,
                        disabled=total == 0 or collected >= total or (not free and credits < CHAMP_SPIN_COST))
            self.button("⬅ 返回", self.go("home"))

        if notice:
            embed.description = f"{notice}\n\n{embed.description}"
        return embed

    async def image(self):
        if self.page == "lucky":
            return discord.File(io.BytesIO(await asyncio.to_thread(lucky_wheel_png)), filename="wheel.png")
        if self.page == "champion":
            return discord.File(io.BytesIO(await asyncio.to_thread(champion_reel_png)), filename="wheel.png")
        return None

    async def lucky_spin(self, interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        async with spin_lock:  # stops double-spending if someone spams the button
            credits, _, owned = get_wallet(gid, uid)
            paid_with, can_pay = lucky_payment(gid, uid)
            if not can_pay:
                await self.show(interaction, notice=f"金幣不足（需要 {SPIN_COST}，你有 {credits}）。"
                                                    f"下次免費轉：{time_until_reset()}後。")
                return
            if paid_with == "今日免費轉":
                db.execute("UPDATE players SET last_free_spin=? WHERE guild_id=? AND user_id=?", (hk_today(), gid, uid))
            elif paid_with == "免費加轉":
                db.execute("UPDATE players SET bonus_spins = bonus_spins - 1 WHERE guild_id=? AND user_id=?", (gid, uid))
            else:
                db.execute("UPDATE players SET credits = credits - ? WHERE guild_id=? AND user_id=?",
                           (SPIN_COST, gid, uid))

            # draw the prize and apply it
            won_card = None
            index = roll()
            key, label, _, kind, value = PRIZES[index]
            if kind == "coins":
                db.execute("UPDATE players SET credits = credits + ? WHERE guild_id=? AND user_id=?", (value, gid, uid))
                result = "🕸️ 被收網了，甚麼都沒有！" if value == 0 else f"🪙 獲得 **{value} 金幣**！"
                if key == "jackpot":
                    result = f"💰 **JACKPOT！** 獲得 **{value} 金幣**！"
            elif kind == "again":
                db.execute(
                    "UPDATE players SET bonus_spins = bonus_spins + ? WHERE guild_id=? AND user_id=?", (value, gid, uid)
                )
                times = "一次" if value == 1 else f"{value} 次"
                result = f"🔄 獲得**{times}免費加轉**！"
            else:  # style
                style_label = STYLES[value][0]
                if value in owned:
                    db.execute(
                        "UPDATE players SET credits = credits + ? WHERE guild_id=? AND user_id=?", (STYLE_REFUND, gid, uid)
                    )
                    result = f"🌈 **超級 JACKPOT！** 抽中「{style_label}」，但你已經擁有，換成 **{STYLE_REFUND} 金幣**！"
                else:
                    db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)", (gid, uid, value))
                    won_card = value
                    result = f"🌈 **超級 JACKPOT！！** 抽中限定卡面「**{style_label}**」！用 /shop →「更換卡面」套用"
            db.commit()
            balance = get_wallet(gid, uid)[0]
        await self.show(interaction, notice="🎡 轉盤轉緊，結果請看頻道！")

        variants = spin_variants(key)
        if variants:
            gif = await asyncio.to_thread(random.choice(variants).read_bytes)
            run_in_background(refresh_spin_gif(key))
        else:  # library still being drawn, e.g. right after wheel.py changed
            gif = await draw_spin_gif(key, 0)
        who = interaction.user.mention
        msg = await interaction.followup.send(
            content=f"🎡 {who} 轉動了幸運轉盤（{paid_with}）……",
            file=discord.File(io.BytesIO(gif), filename="spin.gif"),
            wait=True,
        )
        preview = asyncio.create_task(card_preview(won_card)) if won_card else None  # drawn while the wheel spins
        await asyncio.sleep(2.6)  # let the wheel stop before revealing the result
        await msg.edit(content=f"🎡 {who} 轉動了幸運轉盤（{paid_with}）\n{result}\n剩餘金幣：{balance:,}")
        if preview:
            await attach_card(msg, preview)

    async def champion_spin(self, interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        async with spin_lock:  # stops double-spending if someone spams the button
            credits, _, owned = get_wallet(gid, uid)
            free = champion_spin_free(gid, uid)
            cost = 0 if free else CHAMP_SPIN_COST
            unowned = [c for c in champions.CHAMPIONS if champions.style_key(c) not in owned]
            if not unowned or credits < cost:
                notice = "你已經集齊所有英雄卡！" if not unowned else f"金幣不足：需要 {cost}，你只有 {credits}。"
                await self.show(interaction, notice=notice)
                return
            cid = random.choice(unowned)
            db.execute("UPDATE players SET credits = credits - ?, champ_spins = champ_spins + 1 "
                       "WHERE guild_id=? AND user_id=?", (cost, gid, uid))
            db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)",
                       (gid, uid, champions.style_key(cid)))
            db.commit()
        paid = "首次免費" if free else f"{CHAMP_SPIN_COST} 金幣"
        await self.show(interaction, notice=f"🎰 已抽卡（{paid}），結果請看頻道！")

        async with spin_render_lock:
            gif = await asyncio.to_thread(render_reel_gif, cid, list(champions.CHAMPIONS))
        who = interaction.user.mention
        start = f"🎰 {who} 抽英雄卡（{paid}）"
        msg = await interaction.followup.send(content=f"{start}……", file=discord.File(gif, filename="champion.gif"),
                                              wait=True)
        preview = asyncio.create_task(card_preview(champions.style_key(cid)))  # drawn while the reel plays
        await asyncio.sleep(REEL_SECONDS)
        info = champions.CHAMPIONS[cid]
        collected = sum(champions.is_champion(k) for k in get_wallet(gid, uid)[2])
        await msg.edit(content=f"{start}\n✨ 獲得英雄卡「**{info['name']}**」{info['title']}！"
                               f"（已收集 {collected}/{len(champions.CHAMPIONS)}）\n用 /shop →「更換卡面」套用")
        await attach_card(msg, preview)


@client.tree.command(name="spin", description="轉盤：幸運轉盤（每日免費）或英雄卡轉盤")
@app_commands.guild_only()
async def spin(interaction: discord.Interaction):
    await SpinView(interaction).open(interaction)


# ---------- Flex review (games with unlinked teammates) ----------

class FlexReviewView(MenuView):
    """/flexreview: recorded Flex games that had teammates nobody has linked. Link each one to a member
    (they get that game's result too) or skip them; once everyone is decided the game leaves the list."""

    def __init__(self, interaction):
        super().__init__(interaction)
        self.index = 0

    def load(self, gid):
        rows = db.execute("SELECT match_id, team_id, game, decisions FROM flex_reviews WHERE guild_id=? "
                          "ORDER BY created_at", (gid,)).fetchall()
        return [(m, t, json.loads(g), json.loads(d)) for m, t, g, d in rows]

    def build(self, gid, uid, notice=None):
        self.clear_items()
        reviews = self.load(gid)
        if not reviews:
            embed = discord.Embed(title="📋 Flex 待確認", description="冇待確認的 Flex 場。", color=discord.Color.blurple())
        else:
            self.index = min(self.index, len(reviews) - 1)
            match_id, team_id, game, decisions = reviews[self.index]
            self.current = (match_id, team_id, game, decisions)
            embed = review_embed(gid, match_id, game, decisions)
            embed.title += f"（{self.index + 1}/{len(reviews)}）"
            linked = linked_puuids(gid)
            open_ = [p for p in game["team"] if p["puuid"] not in linked and p["puuid"] not in decisions][:3]
            for row, p in enumerate(open_):
                select = discord.ui.UserSelect(placeholder=f"將 {riot_name(p)} 連結到…", row=row)
                select.callback = self.link_to(p, select)
                self.add_item(select)
                self.button(f"略過 {riot_name(p)}"[:80], self.skip(p), row=3)
            self.button("✅ 完成（略過其餘）", self.finish, discord.ButtonStyle.success, row=4)
            if len(reviews) > 1:
                self.button("◀", self.turn(-1), disabled=self.index == 0, row=4)
                self.button("▶", self.turn(1), disabled=self.index >= len(reviews) - 1, row=4)
        if notice:
            embed.description = f"{notice}\n\n{embed.description}"
        return embed

    def turn(self, step):
        async def callback(interaction):
            self.index += step
            await self.show(interaction)
        return callback

    def save(self, decisions):
        match_id, team_id, _, _ = self.current
        db.execute("UPDATE flex_reviews SET decisions=? WHERE match_id=? AND guild_id=? AND team_id=?",
                   (json.dumps(decisions), match_id, self.interaction.guild_id, team_id))
        db.commit()

    def close(self, gid):
        match_id, team_id, _, _ = self.current
        db.execute("DELETE FROM flex_reviews WHERE match_id=? AND guild_id=? AND team_id=?", (match_id, gid, team_id))
        db.commit()

    async def decided(self, interaction, notice):
        """After a decision: once every unlinked teammate is decided, the game leaves the list."""
        self.interaction = interaction
        gid = interaction.guild_id
        _, _, game, decisions = self.current
        linked = linked_puuids(gid)
        if all(p["puuid"] in linked or p["puuid"] in decisions for p in game["team"]):
            self.close(gid)
            notice += "\n✅ 呢場全部處理完。"
        await self.show(interaction, notice=notice)

    async def add_to_game(self, gid, member, p, game):
        """A teammate linked after the game was recorded gets its result too."""
        number = game.get("number")
        row = db.execute("SELECT team_a FROM matches WHERE id=? AND guild_id=?", (number, gid)).fetchone()
        if row is None:
            return "（搵唔到嗰場記錄，冇補計）"
        ids = str_to_ids(row[0])
        if member.id in ids:
            return "（佢已經喺呢場入面，冇重複計）"
        won = game["won"]
        record(gid, [member.id], won=won)
        db.execute("UPDATE matches SET team_a=? WHERE id=?", (ids_to_str(ids + [member.id]), number))
        db.commit()
        sign = f"+{ELO_CHANGE}" if won else f"-{ELO_CHANGE}"
        text = (f"{member.mention}（{riot_name(p)}）補計 Flex 第 {number} 場：{'贏了' if won else '輸了'}　"
                f"{sign} → **{get_player(gid, member.id)[0]}**")
        channel = client.get_channel(flex_channel(gid) or 0)
        if channel:
            try:
                await channel.send(f"🔗 {text}")
            except discord.HTTPException as e:
                print(f"Couldn't announce late link: {e!r}")
        return text

    def link_to(self, p, select):
        async def callback(interaction):
            gid = interaction.guild_id
            member = select.values[0]
            _, _, game, _ = self.current
            owner = db.execute("SELECT user_id FROM riot_accounts WHERE guild_id=? AND puuid=?",
                               (gid, p["puuid"])).fetchone()
            count = db.execute("SELECT count(*) FROM riot_accounts WHERE guild_id=? AND user_id=?",
                               (gid, member.id)).fetchone()[0]
            if member.bot:
                await self.show(interaction, notice="不能連結 bot。")
            elif owner:
                await self.show(interaction, notice=f"{riot_name(p)} 已經連結咗 <@{owner[0]}>。")
            elif count >= MAX_RIOT_ACCOUNTS:
                await self.show(interaction, notice=f"{member.mention} 已經連結咗 {MAX_RIOT_ACCOUNTS} 個帳號。")
            else:
                # every held game this account played in, not only the one on screen
                games = [g for _, _, g, _ in self.load(gid) if any(q["puuid"] == p["puuid"] for q in g["team"])]
                get_player(gid, member.id)
                db.execute("INSERT INTO riot_accounts VALUES (?, ?, ?, ?, ?, ?)",
                           (gid, member.id, p["puuid"], riot_name(p), game["server"],
                            min([g["start"] for g in games] + [game["start"]])))
                db.commit()
                added = []
                for g in games:
                    q = next(q for q in g["team"] if q["puuid"] == p["puuid"])
                    added.append(await self.add_to_game(gid, member, q, g))
                done = self.close_finished(gid)
                notice = (f"🔗 {riot_name(p)} 已連結到 {member.mention}（之後嘅 Flex 都會計）\n"
                          f"補計 {len(added)} 場：\n" + "\n".join(added))
                if done:
                    notice += f"\n✅ {done} 場已經全部處理完。"
                self.interaction = interaction
                await self.show(interaction, notice=notice)
        return callback

    def close_finished(self, gid):
        """Drop held games where every teammate is now linked or skipped. Returns how many."""
        linked = linked_puuids(gid)
        done = 0
        for match_id, team_id, g, decisions in self.load(gid):
            if all(q["puuid"] in linked or q["puuid"] in decisions for q in g["team"]):
                db.execute("DELETE FROM flex_reviews WHERE match_id=? AND guild_id=? AND team_id=?",
                           (match_id, gid, team_id))
                done += 1
        db.commit()
        return done

    def skip(self, p):
        async def callback(interaction):
            _, _, _, decisions = self.current
            decisions[p["puuid"]] = "ignore"
            self.save(decisions)
            await self.decided(interaction, f"⏭️ 已略過 {riot_name(p)}")
        return callback

    async def finish(self, interaction):
        self.interaction = interaction
        self.close(interaction.guild_id)
        await self.show(interaction, notice="⏭️ 已略過其餘隊友，呢場處理完。")


@client.tree.command(name="flexreview", description="處理有未連結隊友的 Flex 場（只限 bot 擁有者）")
@app_commands.guild_only()
async def flexreview(interaction: discord.Interaction):
    if interaction.user.id != client.owner_id:
        await interaction.response.send_message("只有 bot 擁有者可以使用這個指令。", ephemeral=True)
        return
    await FlexReviewView(interaction).open(interaction)


# ---------- Duo / line-up stats ----------

def team_results(guild_id):
    """Yield (team_ids, mode, won) for every team in every finished match."""
    rows = db.execute(
        "SELECT mode, team_a, team_b, winner FROM matches WHERE guild_id=? AND status='done'",
        (guild_id,),
    )
    for mode, a, b, winner in rows:
        if mode == "mix":
            yield str_to_ids(a), mode, winner == "A"
            yield str_to_ids(b), mode, winner == "B"
        else:
            yield str_to_ids(a), mode, winner == "win"


def add_result(stats, key, mode, won):
    """stats[key] = {"mix": [games, wins], "flex": [games, wins]}"""
    s = stats.setdefault(key, {"mix": [0, 0], "flex": [0, 0]})
    s[mode][0] += 1
    s[mode][1] += int(won)


def totals(rec):
    return rec["mix"][0] + rec["flex"][0], rec["mix"][1] + rec["flex"][1]


def ranked(stats, best, limit=5):
    """Top entries by overall win rate (ties: more games first)."""
    def key(item):
        games, wins = totals(item[1])
        return (-wins / games if best else wins / games, -games)
    return sorted(stats.items(), key=key)[:limit]


def record_text(rec):
    games, wins = totals(rec)
    return f"**{wins / games:.0%}**（{wins} 勝 {games - wins} 負）"


KIND_CHOICES = [
    app_commands.Choice(name="最強（最高勝率）", value="best"),
    app_commands.Choice(name="炸裂（最低勝率）", value="worst"),
]


@client.tree.command(name="duo", description="同邊勝率最高 / 最低的隊友")
@app_commands.guild_only()
@app_commands.describe(kind="最強或炸裂", user="要查看的玩家（預設自己）")
@app_commands.choices(kind=KIND_CHOICES)
async def duo(interaction: discord.Interaction, kind: app_commands.Choice[str],
              user: discord.Member = None):
    user = user or interaction.user
    stats = {}  # teammate_id -> [games together, wins together]
    for team, mode, won in team_results(interaction.guild_id):
        if user.id not in team:
            continue
        for mate in team:
            if mate != user.id:
                add_result(stats, mate, mode, won)

    if not stats:
        await interaction.response.send_message(
            f"**{user.display_name}** 還沒有比賽記錄。",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    top = ranked(stats, kind.value == "best")
    label = "💪 最強 duo" if kind.value == "best" else "💥 炸裂 duo"
    lines = [f"{i}. <@{mate}> — {record_text(rec)}" for i, (mate, rec) in enumerate(top, 1)]
    embed = discord.Embed(
        title=f"{label}：{user.display_name}",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="lineup", description="最強 / 炸裂 line up")
@app_commands.guild_only()
@app_commands.describe(kind="最強或炸裂")
@app_commands.choices(kind=KIND_CHOICES)
async def lineup(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    stats = {}
    for team, mode, won in team_results(interaction.guild_id):
        if len(team) == 5:  # a line up is a full team (auto Flex games can have just 2-4 members)
            add_result(stats, tuple(sorted(team)), mode, won)

    if not stats:
        await interaction.response.send_message("還沒有比賽記錄。")
        return
    top = ranked(stats, kind.value == "best")
    title = "💪 最強 line up" if kind.value == "best" else "💥 炸裂 line up"
    lines = [
        f"{i}. {' + '.join(f'<@{u}>' for u in team)}\n　　{record_text(rec)}"
        for i, (team, rec) in enumerate(top, 1)
    ]
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
