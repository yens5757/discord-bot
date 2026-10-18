import asyncio
import hashlib
import io
import itertools
import os
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

import champions
from reel import SECONDS as REEL_SECONDS
from reel import render_reel_gif
from stat_card import STYLES, render_shop_preview, render_stat_card
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
    await interaction.response.send_message(embed=embed)


client.tree.add_command(result_group)


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
@app_commands.default_permissions(administrator=True)  # hides it from non-admins in the menu
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
@app_commands.default_permissions(administrator=True)  # hides it from non-admins in the menu
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


@client.tree.command(name="leaderboard", description="ELO 排行榜")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, elo, wins, losses FROM players WHERE guild_id=? "
        "ORDER BY elo DESC LIMIT 10",
        (interaction.guild_id,),
    ).fetchall()
    if not rows:
        await interaction.response.send_message("還沒有人有 ELO 記錄。")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} <@{u}> — {rating}（{w} 勝 {l} 負）"
        for i, (u, rating, w, l) in enumerate(rows)
    ]
    embed = discord.Embed(title="🏅 ELO 排行榜", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


# ---------- Card style shop ----------

CHAMP_SPIN_COST = 300  # 英雄卡轉盤: always a champion card you don't own yet


async def prepare_shop():
    await asyncio.to_thread(champions.load_list)
    await asyncio.to_thread(render_shop_preview)  # draw it once now, so the buy page opens quickly
    await asyncio.to_thread(champions.download_all)


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


class ShopView(discord.ui.View):
    """/shop: a home page with 購買 and 更換卡面, the buy page (styles + 英雄卡轉盤), and the card switcher."""

    PER_PAGE = 25  # Discord's limit for one dropdown

    def __init__(self, interaction):
        super().__init__(timeout=840)  # interaction tokens expire after 15 minutes
        self.interaction = interaction
        self.user_id = interaction.user.id
        self.page = "home"
        self.switch_page = 0

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
        """Lay out the current page: returns its embed and sets the buttons."""
        credits, equipped, owned = get_wallet(gid, uid)
        collected = sum(champions.is_champion(k) for k in owned)
        total = len(champions.CHAMPIONS)
        self.clear_items()
        embed = discord.Embed(color=discord.Color.gold())

        if self.page == "home":
            embed.title = "🛒 戰績卡商店"
            embed.description = (f"你的金幣：**{credits:,}**\n目前卡面：**{card_label(equipped)}**\n"
                                 f"英雄卡：已收集 **{collected}/{total}**")
            self.button("🛒 購買", self.go("buy"), discord.ButtonStyle.primary)
            self.button("🎨 更換卡面", self.go("switch"), discord.ButtonStyle.success)

        elif self.page == "buy":
            lines = []
            for key, (label, price, _) in STYLES.items():
                status = "已擁有" if key in owned else "🎡 /spin 轉盤限定" if price is None else f"🪙 {price} 金幣"
                lines.append(f"**{label}**　{status}")
            lines.append(f"\n🎰 **英雄卡轉盤**　🪙 {CHAMP_SPIN_COST} 金幣\n必定抽到未擁有的英雄卡（已收集 {collected}/{total}）")
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
            self.button(f"🎰 英雄卡轉盤（{CHAMP_SPIN_COST}）", self.champion_spin, discord.ButtonStyle.success,
                        disabled=credits < CHAMP_SPIN_COST or collected >= total, row=2)
            self.button("⬅ 返回", self.go("home"), row=2)

        else:  # switch
            cards = owned_cards(owned)
            pages = max(1, -(-len(cards) // self.PER_PAGE))
            self.switch_page = min(self.switch_page, pages - 1)
            shown = cards[self.switch_page * self.PER_PAGE:(self.switch_page + 1) * self.PER_PAGE]
            embed.title = "🎨 更換卡面"
            embed.description = (f"目前卡面：**{card_label(equipped)}**\n已擁有 {len(cards)} 款"
                                 f"（英雄卡 {collected}/{total}）\n在下面選擇要使用的卡面")
            select = discord.ui.Select(
                placeholder=f"選擇卡面（第 {self.switch_page + 1}/{pages} 頁）",
                options=[discord.SelectOption(
                    label=card_label(k), value=k, default=k == equipped,
                    description=(champions.CHAMPIONS.get(champions.champion_id(k), {}).get("title")
                                 if champions.is_champion(k) else "基本款式") or None,
                ) for k in shown],
            )
            select.callback = self.equip
            self.add_item(select)
            if pages > 1:
                self.button("◀", self.turn(-1), disabled=self.switch_page == 0, row=1)
                self.button("▶", self.turn(1), disabled=self.switch_page >= pages - 1, row=1)
            self.button("⬅ 返回", self.go("home"), row=1)

        if notice:
            embed.description = f"{notice}\n\n{embed.description}"
        return embed

    async def show(self, interaction, page=None, notice=None):
        if page:
            self.page = page
        self.interaction = interaction
        if not interaction.response.is_done():
            await interaction.response.defer()
        embed = self.build(interaction.guild_id, interaction.user.id, notice)
        files = []
        if self.page == "buy":
            files = [discord.File(await asyncio.to_thread(render_shop_preview), filename="shop.png")]
        await interaction.edit_original_response(embed=embed, view=self, attachments=files)

    def turn(self, step):
        async def callback(interaction):
            self.switch_page += step
            await self.show(interaction)
        return callback

    async def equip(self, interaction):
        key = interaction.data["values"][0]
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

    async def champion_spin(self, interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        async with spin_lock:  # stops double-spending if someone spams the button
            credits, _, owned = get_wallet(gid, uid)
            unowned = [c for c in champions.CHAMPIONS if champions.style_key(c) not in owned]
            if not unowned or credits < CHAMP_SPIN_COST:
                notice = "你已經集齊所有英雄卡！" if not unowned else f"金幣不足：需要 {CHAMP_SPIN_COST}，你只有 {credits}。"
                await self.show(interaction, notice=notice)
                return
            cid = random.choice(unowned)
            db.execute("UPDATE players SET credits = credits - ? WHERE guild_id=? AND user_id=?",
                       (CHAMP_SPIN_COST, gid, uid))
            db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)",
                       (gid, uid, champions.style_key(cid)))
            db.commit()
        await self.show(interaction, notice=f"🎰 已扣 {CHAMP_SPIN_COST} 金幣，抽卡結果請看頻道！")

        async with spin_render_lock:
            gif = await asyncio.to_thread(render_reel_gif, cid, list(champions.CHAMPIONS))
        who = interaction.user.mention
        start = f"🎰 {who} 抽英雄卡（{CHAMP_SPIN_COST} 金幣）"
        msg = await interaction.followup.send(content=f"{start}……", file=discord.File(gif, filename="champion.gif"),
                                              wait=True)
        await asyncio.sleep(REEL_SECONDS)
        info = champions.CHAMPIONS[cid]
        collected = sum(champions.is_champion(k) for k in get_wallet(gid, uid)[2])
        await msg.edit(content=f"{start}\n✨ 獲得英雄卡「**{info['name']}**」{info['title']}！"
                               f"（已收集 {collected}/{len(champions.CHAMPIONS)}）\n用 /shop →「更換卡面」套用")

    async def on_timeout(self):
        try:
            await self.interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass


@client.tree.command(name="shop", description="戰績卡商店：購買卡面、英雄卡轉盤、更換卡面")
@app_commands.guild_only()
async def shop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # only the person using it sees the shop
    view = ShopView(interaction)
    embed = view.build(interaction.guild_id, interaction.user.id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


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


@client.tree.command(name="spin", description=f"幸運轉盤（每日 1 次免費，之後每次 {SPIN_COST} 金幣）")
@app_commands.guild_only()
async def spin(interaction: discord.Interaction):
    gid, uid = interaction.guild_id, interaction.user.id

    async with spin_lock:  # stops double-spending if someone spams the command
        credits, _, owned = get_wallet(gid, uid)
        last_free, bonus = db.execute(
            "SELECT last_free_spin, bonus_spins FROM players WHERE guild_id=? AND user_id=?", (gid, uid)
        ).fetchone()

        # pay for the spin: daily free → bonus spin → credits
        if last_free != hk_today():
            db.execute("UPDATE players SET last_free_spin=? WHERE guild_id=? AND user_id=?", (hk_today(), gid, uid))
            paid_with = "今日免費轉"
        elif bonus > 0:
            db.execute("UPDATE players SET bonus_spins = bonus_spins - 1 WHERE guild_id=? AND user_id=?", (gid, uid))
            paid_with = "免費加轉"
        elif credits >= SPIN_COST:
            db.execute("UPDATE players SET credits = credits - ? WHERE guild_id=? AND user_id=?", (SPIN_COST, gid, uid))
            paid_with = f"{SPIN_COST} 金幣"
        else:
            await interaction.response.send_message(
                f"金幣不足（需要 {SPIN_COST}，你有 {credits}）。下次免費轉：{time_until_reset()}後。",
                ephemeral=True,
            )
            return

        # draw the prize and apply it
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
            result = f"🔄 獲得**{times}免費加轉**！再用 /spin"
        else:  # style
            style_label = STYLES[value][0]
            if value in owned:
                db.execute(
                    "UPDATE players SET credits = credits + ? WHERE guild_id=? AND user_id=?", (STYLE_REFUND, gid, uid)
                )
                result = f"🌈 **超級 JACKPOT！** 抽中「{style_label}」，但你已經擁有，換成 **{STYLE_REFUND} 金幣**！"
            else:
                db.execute("INSERT INTO owned_styles (guild_id, user_id, style) VALUES (?, ?, ?)", (gid, uid, value))
                result = f"🌈 **超級 JACKPOT！！** 抽中限定卡面「**{style_label}**」！用 /shop →「更換卡面」套用"
        db.commit()
        balance = get_wallet(gid, uid)[0]

    await interaction.response.defer()
    variants = spin_variants(key)
    if variants:
        gif = await asyncio.to_thread(random.choice(variants).read_bytes)
        run_in_background(refresh_spin_gif(key))
    else:  # library still being drawn, e.g. right after wheel.py changed
        gif = await draw_spin_gif(key, 0)
    who = interaction.user.mention
    msg = await interaction.followup.send(
        content=f"🎡 {who} 轉動了轉盤（{paid_with}）……",
        file=discord.File(io.BytesIO(gif), filename="spin.gif"),
        wait=True,
    )
    await asyncio.sleep(2.6)  # let the wheel stop before revealing the result
    await msg.edit(content=f"🎡 {who} 轉動了轉盤（{paid_with}）\n{result}\n剩餘金幣：{balance:,}")


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
