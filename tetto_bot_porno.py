import asyncio
import html
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# =============================================================================
# НАСТРОЙКИ
# =============================================================================
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "8904950437:AAHUQcrqpC7hvMOhFWuu6he8mvLSfBbCTn8"
if TOKEN == "8904950437:AAHUQcrqpC7hvMOhFWuu6he8mvLSfBbCTn8":
    raise RuntimeError("Вставь токен бота в строку TOKEN = ...")

CHANNEL_ID = -1002130614493
ADMIN_ID = 6936783270
VEXX_ID = 8021778299
ALL_ADMINS = {ADMIN_ID, VEXX_ID}

# Тихие часы считаются по САРАТОВУ
TZ_CHANNEL = ZoneInfo("Europe/Saratov")
TZ_MSK = ZoneInfo("Europe/Moscow")
TZ_SARATOV = ZoneInfo("Europe/Saratov")
TZ_EKB = ZoneInfo("Asia/Yekaterinburg")

QUIET_FROM = 0
QUIET_TO = 7

LOW_QUEUE_THRESHOLD = 2
LOW_QUEUE_EVERY = 1800

CLEANUP_REJECTED_DAYS = 30
CLEANUP_LOGS_DAYS = 30
CLEANUP_HISTORY_DAYS = 90
CLEANUP_EVERY = 86400

SWEEP_EVERY = 600
NOTIFY_PUBLISH = False

DB_PLAN = "plan.db"
DB_SUGG = "proposed.db"
DB_MAIN = "main.db"
STATS_FILE = "stats.json"
BLOCK_FILE = "block.json"

REASONS = [
    ("format", "🚫 Не формат"),
    ("quality", "📉 Плохое качество"),
    ("duplicate", "👯 Дубликат"),
    ("forbidden", "⛔ Запрещённый контент"),
    ("other", "📄 Другое"),
    ("none", "🤷 Без причины"),
]
REASON_MAP = dict(REASONS)

BAN_REASONS = [
    ("spam", "📨 Спам"),
    ("ads", "💰 Реклама"),
    ("shock", "⛔ Шок-контент"),
    ("dupes", "👯 Дубликаты"),
    ("rules", "🚫 Нарушение правил"),
    ("other", "📄 Другое"),
]
BAN_REASON_MAP = dict(BAN_REASONS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("TETO_BOT")


def esc(text):
    return html.escape(str(text) if text is not None else "", quote=False)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


# =============================================================================
# СЛОЙ БД (не блокирует event loop)
# =============================================================================
def _sync_run(path, fn):
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        result = fn(con)
        con.commit()
        return result
    finally:
        con.close()


async def db_run(path, fn):
    return await asyncio.to_thread(_sync_run, path, fn)


async def db_all(path, sql, params=()):
    return await db_run(path, lambda c: c.execute(sql, params).fetchall())


async def db_one(path, sql, params=()):
    return await db_run(path, lambda c: c.execute(sql, params).fetchone())


async def db_exec(path, sql, params=()):
    def fn(c):
        return c.execute(sql, params).lastrowid
    return await db_run(path, fn)


def _ensure_columns(con, table, required):
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in required.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            logger.info("Миграция: %s.%s", table, name)


SETTINGS_CACHE = {}


async def get_setting(key, default=None):
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    row = await db_one(DB_MAIN, "SELECT value FROM settings WHERE key=?", (key,))
    value = json.loads(row[0]) if row else default
    SETTINGS_CACHE[key] = value
    return value


async def set_setting(key, value):
    SETTINGS_CACHE[key] = value
    await db_exec(
        DB_MAIN,
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _init_plan(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot INTEGER,
            caption TEXT,
            source TEXT,
            author_id INTEGER,
            chat_id INTEGER,
            msg_ids TEXT,
            created_at INTEGER
        )"""
    )
    _ensure_columns(con, "posts", {"media_type": "TEXT", "author_id": "INTEGER"})
    con.execute(
        """CREATE TABLE IF NOT EXISTS published_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER,
            published_at INTEGER,
            source TEXT,
            author_id INTEGER,
            media_type TEXT,
            media_count INTEGER,
            caption_short TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_slot ON posts(slot)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_hist_time ON published_history(published_at)")


def _init_sugg(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            is_anon INTEGER,
            status TEXT,
            created_at INTEGER,
            last_sent INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS sugg_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            caption TEXT,
            status TEXT,
            pos INTEGER,
            chat_id INTEGER,
            msg_ids TEXT
        )"""
    )
    _ensure_columns(
        con,
        "sugg_posts",
        {
            "reject_reason": "TEXT",
            "reviewed_by": "INTEGER",
            "reviewed_at": "INTEGER",
            "created_at": "INTEGER",
            "media_type": "TEXT",
        },
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_sugg_status ON sugg_posts(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sugg_batch ON sugg_posts(batch_id)")


def _init_main(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            reason TEXT,
            banned_by INTEGER,
            banned_at INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            admin_id INTEGER,
            action TEXT,
            details TEXT
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")


async def init_db():
    await db_run(DB_PLAN, _init_plan)
    await db_run(DB_SUGG, _init_sugg)
    await db_run(DB_MAIN, _init_main)

    if not await get_setting("legacy_imported", False):
        old_stats = load_json(STATS_FILE, {})
        await set_setting("legacy_published", int(old_stats.get("sent", {}).get("posts", 0)))
        await set_setting("paused", bool(old_stats.get("paused", False)))

        old_block = load_json(BLOCK_FILE, {})
        for uid_str, uname in old_block.items():
            try:
                uid = int(uid_str)
            except Exception:
                continue
            await db_exec(
                DB_MAIN,
                "INSERT OR IGNORE INTO blacklist(user_id,username,reason,banned_by,banned_at) VALUES(?,?,?,?,?)",
                (uid, str(uname), "Импорт из старого списка", 0, int(time.time())),
            )
        await set_setting("legacy_imported", True)

    await get_setting("paused", False)
    await get_setting("last_low_notify", 0)
    await get_setting("last_cleanup", 0)
    await get_setting("last_sweep", 0)


# =============================================================================
# ВРЕМЯ / СЛОТЫ / ТИХИЕ ЧАСЫ
# =============================================================================
def get_tz(uid):
    if uid == VEXX_ID:
        return TZ_EKB
    if uid == ADMIN_ID:
        return TZ_SARATOV
    return TZ_MSK


def fmt_time(ts, uid=ADMIN_ID):
    try:
        return datetime.fromtimestamp(int(ts), get_tz(uid)).strftime("%H:%M %d.%m.%Y")
    except Exception:
        return str(ts)


def is_quiet_ts(ts):
    dt = datetime.fromtimestamp(int(ts), TZ_CHANNEL)
    return QUIET_FROM <= dt.hour < QUIET_TO


def shift_out_of_quiet(ts):
    dt = datetime.fromtimestamp(int(ts), TZ_CHANNEL)
    if QUIET_FROM <= dt.hour < QUIET_TO:
        dt = dt.replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def day_start(offset=0):
    base = datetime.now(TZ_CHANNEL).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=offset)


def get_day_range(filter_name, uid=None):
    if filter_name == "today":
        start = day_start(0)
    elif filter_name == "tomorrow":
        start = day_start(1)
    elif filter_name == "week":
        start = day_start(0)
        return int(start.timestamp()), int((start + timedelta(days=7)).timestamp())
    else:
        return None
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


async def allocate_slots(count=1):
    used = {r[0] for r in await db_all(DB_PLAN, "SELECT slot FROM posts")}
    now = datetime.now(TZ_CHANNEL).replace(minute=0, second=0, microsecond=0)
    cand = int((now + timedelta(hours=1)).timestamp())
    slots = []
    for _ in range(count):
        cand = shift_out_of_quiet(cand)
        while cand in used:
            cand = shift_out_of_quiet(cand + 3600)
        used.add(cand)
        slots.append(cand)
        cand += 3600
    return slots


async def get_next_slot():
    return (await allocate_slots(1))[0]


async def reschedule_quiet_posts():
    rows = await db_all(DB_PLAN, "SELECT id, slot FROM posts ORDER BY slot")
    if not rows:
        return []
    used = {r[1] for r in rows}
    moved = []
    for pid, slot in rows:
        if not is_quiet_ts(slot):
            continue
        new_slot = shift_out_of_quiet(slot)
        while new_slot in used:
            new_slot = shift_out_of_quiet(new_slot + 3600)
        await db_exec(DB_PLAN, "UPDATE posts SET slot=? WHERE id=?", (new_slot, pid))
        used.discard(slot)
        used.add(new_slot)
        moved.append((pid, slot, new_slot))
    return moved


# =============================================================================
# ЧС / ЛОГИ / СТАТИСТИКА
# =============================================================================
async def is_blocked(user_id):
    return await db_one(DB_MAIN, "SELECT 1 FROM blacklist WHERE user_id=?", (user_id,)) is not None


async def block_user(user_id, username, reason, banned_by):
    await db_exec(
        DB_MAIN,
        "INSERT OR REPLACE INTO blacklist(user_id,username,reason,banned_by,banned_at) VALUES(?,?,?,?,?)",
        (user_id, username, reason, banned_by, int(time.time())),
    )


async def unblock_user(user_id):
    await db_exec(DB_MAIN, "DELETE FROM blacklist WHERE user_id=?", (user_id,))


async def safe_send(chat_id, text, reply_markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except Exception:
        logger.warning("Не удалось отправить сообщение %s", chat_id)
        return False


async def audit(admin_id, action, details="", notify=True):
    ts = int(time.time())
    await db_exec(
        DB_MAIN,
        "INSERT INTO audit_log(ts,admin_id,action,details) VALUES(?,?,?,?)",
        (ts, admin_id, action, details),
    )
    if not notify:
        return
    who = "🤖 Система" if not admin_id else f"👤 <code>{admin_id}</code>"
    text = (
        "🧾 <b>ЛОГ ДЕЙСТВИЯ</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"<b>{esc(action)}</b>\n"
        f"{details}\n"
        f"{who}\n"
        f"🕒 {fmt_time(ts, ADMIN_ID)}"
    )
    await safe_send(ADMIN_ID, text)


async def queue_count():
    row = await db_one(DB_PLAN, "SELECT COUNT(*) FROM posts")
    return row[0] if row else 0


async def total_published():
    row = await db_one(DB_PLAN, "SELECT COUNT(*) FROM published_history")
    legacy = await get_setting("legacy_published", 0)
    return (row[0] if row else 0) + int(legacy or 0)


async def published_between(start_ts, end_ts=None):
    if end_ts is None:
        row = await db_one(DB_PLAN, "SELECT COUNT(*) FROM published_history WHERE published_at>=?", (start_ts,))
    else:
        row = await db_one(
            DB_PLAN,
            "SELECT COUNT(*) FROM published_history WHERE published_at>=? AND published_at<?",
            (start_ts, end_ts),
        )
    return row[0] if row else 0


async def sugg_stats():
    rows = await db_all(DB_SUGG, "SELECT status, COUNT(*) FROM sugg_posts GROUP BY status")
    data = {"pending": 0, "accepted": 0, "rejected": 0}
    for status, cnt in rows:
        data[status or "pending"] = cnt
    data["total"] = sum(data.values())
    week = int(day_start(-6).timestamp())
    row = await db_one(DB_SUGG, "SELECT COUNT(*) FROM sugg_posts WHERE COALESCE(created_at,0)>=?", (week,))
    data["week_new"] = row[0] if row else 0
    row = await db_one(
        DB_SUGG,
        "SELECT COUNT(*) FROM sugg_posts WHERE status='accepted' AND COALESCE(reviewed_at,0)>=?",
        (week,),
    )
    data["week_acc"] = row[0] if row else 0
    row = await db_one(
        DB_SUGG,
        "SELECT COUNT(*) FROM sugg_posts WHERE status='rejected' AND COALESCE(reviewed_at,0)>=?",
        (week,),
    )
    data["week_rej"] = row[0] if row else 0
    row = await db_one(DB_MAIN, "SELECT COUNT(*) FROM blacklist")
    data["banned"] = row[0] if row else 0
    decided = data["accepted"] + data["rejected"]
    data["rate"] = round(data["accepted"] / decided * 100, 1) if decided else 0.0
    return data


def detect_media_type(messages):
    if len(messages) > 1:
        return "album"
    m = messages[0]
    if m.photo:
        return "photo"
    if m.video:
        return "video"
    if m.animation:
        return "animation"
    return "other"


MEDIA_LABEL = {
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "animation": "🌀 Гифка",
    "album": "🗂 Альбом",
    "other": "📦 Другое",
}


# =============================================================================
# АНТИФЛУД
# =============================================================================
FLOOD_TRACKER = {}
MUTED_USERS = {}


def check_flood(uid):
    if uid not in ALL_ADMINS:
        return False
    now = time.time()
    if uid in MUTED_USERS:
        if now < MUTED_USERS[uid]:
            return True
        del MUTED_USERS[uid]
    FLOOD_TRACKER.setdefault(uid, []).append(now)
    FLOOD_TRACKER[uid] = [t for t in FLOOD_TRACKER[uid] if now - t < 2]
    if len(FLOOD_TRACKER[uid]) >= 8:
        MUTED_USERS[uid] = now + 5
        FLOOD_TRACKER[uid].clear()
        return True
    return False


# =============================================================================
# КЛАВИАТУРЫ
# =============================================================================
def get_reply_kb(paused):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Гл. Меню"), KeyboardButton(text="🗂 План")],
            [KeyboardButton(text=("Возобновить постинг ▶️" if paused else "Пауза постинга ⏸"))],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="🚫 Черный список 🚫")],
            [KeyboardButton(text="🔽 Скрыть клавиатуру 🔽")],
        ],
        resize_keyboard=True,
    )


def get_main_inline(paused):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗂 Очередь", callback_data="planf:all:0"),
                InlineKeyboardButton(text="📜 История", callback_data="hist:0"),
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="m:stats"),
                InlineKeyboardButton(text="🧾 Логи", callback_data="log:0"),
            ],
            [
                InlineKeyboardButton(text="🛡 ЧС", callback_data="m:blacklist"),
                InlineKeyboardButton(text="🧹 Очистка", callback_data="m:cleanup"),
            ],
            [
                InlineKeyboardButton(text=("▶️ Возобновить" if paused else "⏸ Пауза"), callback_data="m:pause"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="m:main"),
            ],
            [
                InlineKeyboardButton(text="📄 HTML-отчёт", callback_data="st:report"),
                InlineKeyboardButton(text="ℹ️ Помощь", callback_data="m:help"),
            ],
        ]
    )


def get_preview_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отложить", callback_data="a:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="a:cancel"),
            ]
        ]
    )


def get_user_anon_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👻 Анонимно", callback_data="u:anon")],
            [InlineKeyboardButton(text="👤 Не анонимно", callback_data="u:nonanon")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="u:cancel")],
        ]
    )


def get_review_kb(bid, spid):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"r:acc:{spid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"r:rej:{spid}"),
            ],
            [
                InlineKeyboardButton(text="✅ Принять все", callback_data=f"r:accall:{bid}"),
                InlineKeyboardButton(text="❌ Отклонить все", callback_data=f"r:rejall:{bid}"),
            ],
            [InlineKeyboardButton(text="🚫 Забанить", callback_data=f"r:ban:{bid}")],
        ]
    )


def get_back_kb(cb="m:main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]])


def get_stats_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 По дням", callback_data="st:days"),
                InlineKeyboardButton(text="📥 Предложка", callback_data="st:sugg"),
            ],
            [InlineKeyboardButton(text="📄 HTML-отчёт", callback_data="st:report")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")],
        ]
    )


async def build_main_text(uid):
    paused = await get_setting("paused", False)
    q = await queue_count()
    pub = await total_published()
    today = await published_between(int(day_start(0).timestamp()))
    nxt = await db_one(DB_PLAN, "SELECT MIN(slot) FROM posts")
    next_txt = fmt_time(nxt[0], uid) if nxt and nxt[0] else "—"
    status = "⏸ Пауза" if paused else "🟢 Работает"
    tz_name = "Екатеринбург" if uid == VEXX_ID else ("Саратов" if uid == ADMIN_ID else "Москва")
    return (
        "🏠 <b>TETO POSTIT</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📦 <b>В очереди:</b> {q}\n"
        f"📅 <b>Сегодня вышло:</b> {today}\n"
        f"✅ <b>Всего опубликовано:</b> {pub}\n"
        f"⚙️ <b>Статус:</b> {status}\n"
        f"🕒 <b>Ближайший пост:</b> {next_txt}\n"
        f"🌍 <b>Твой пояс:</b> {tz_name}\n"
        f"🌙 <b>Тихие часы:</b> 00:00–07:00 (Саратов)\n"
        "━━━━━━━━━━━━━━━━\n"
        "Выбери действие ниже 👇"
    )


async def build_stats_text():
    q = await queue_count()
    pub = await total_published()
    today = await published_between(int(day_start(0).timestamp()))
    yest = await published_between(int(day_start(-1).timestamp()), int(day_start(0).timestamp()))
    week = await published_between(int(day_start(-6).timestamp()))
    month = await published_between(int(day_start(-29).timestamp()))
    return (
        "📊 <b>Статистика</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📦 В очереди: <b>{q}</b>\n"
        f"✅ Всего опубликовано: <b>{pub}</b>\n\n"
        f"📅 Сегодня: <b>{today}</b>\n"
        f"📅 Вчера: <b>{yest}</b>\n"
        f"🗓 За 7 дней: <b>{week}</b>\n"
        f"🗓 За 30 дней: <b>{month}</b>\n\n"
        "<i>Счётчик «всего» учитывает и старые публикации.</i>"
    )


async def get_blacklist_view():
    rows = await db_all(
        DB_MAIN,
        "SELECT user_id, username, reason, banned_by, banned_at FROM blacklist ORDER BY banned_at DESC",
    )
    text = "🛡 <b>Чёрный список</b>\n━━━━━━━━━━━━━━━━\n"
    kb = []
    if not rows:
        text += "✨ Пусто."
        kb.append([InlineKeyboardButton(text="✨ Пусто", callback_data="noop")])
    else:
        text += f"Всего: <b>{len(rows)}</b>\n\n"
        lines = []
        for user_id, username, reason, banned_by, banned_at in rows[:10]:
            uname = username or str(user_id)
            who = "система" if not banned_by else ("ты" if banned_by == ADMIN_ID else str(banned_by))
            lines.append(
                f"👤 <b>{esc(uname)}</b>\n"
                f"   ID: <code>{user_id}</code>\n"
                f"   Причина: {esc((reason or 'Не указана')[:60])}\n"
                f"   Забанил: {esc(who)}\n"
                f"   Дата: {fmt_time(banned_at, ADMIN_ID) if banned_at else '—'}"
            )
            kb.append([InlineKeyboardButton(text=f"🔓 Разбанить {uname[:22]}", callback_data=f"unblock:{user_id}")])
        if len(rows) > 10:
            lines.append(f"<i>И ещё {len(rows) - 10}...</i>")
        text += "\n\n".join(lines)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


def history_status(s_status, b_status):
    if b_status == "banned":
        return "🚫", "Забанен"
    if s_status == "accepted":
        return "✅", "Принято"
    if s_status == "rejected":
        return "❌", "Отклонено"
    return "⏳", "Ожидает"


# =============================================================================
# HTML ОТЧЁТ (НЕОН)
# =============================================================================
REPORT_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:#05060f;
  color:#e8f6ff;
  font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  min-height:100vh;padding:36px 18px;overflow-x:hidden;
  background-image:
    radial-gradient(circle at 12% 8%, rgba(0,240,255,.20), transparent 45%),
    radial-gradient(circle at 88% 12%, rgba(255,0,229,.18), transparent 45%),
    radial-gradient(circle at 50% 100%, rgba(120,0,255,.22), transparent 55%);
}
body:before{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(rgba(0,240,255,.05) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(255,0,229,.05) 1px,transparent 1px);
  background-size:44px 44px;
  mask-image:radial-gradient(circle at 50% 30%,#000 30%,transparent 85%);
}
.wrap{position:relative;z-index:1;max-width:1000px;margin:0 auto}
.head{text-align:center;margin-bottom:34px}
.logo{
  font-size:44px;font-weight:900;letter-spacing:6px;
  color:#fff;
  text-shadow:0 0 8px #00f0ff,0 0 22px #00f0ff,0 0 46px #0090ff;
  animation:pulse 3.2s ease-in-out infinite;
}
.logo span{color:#ff3df2;text-shadow:0 0 8px #ff00e5,0 0 26px #ff00e5,0 0 52px #ff00e5}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.82}}
.sub{margin-top:12px;color:#7fe9ff;letter-spacing:3px;font-size:13px;text-transform:uppercase}
.period{margin-top:10px;color:#9fb3c8;font-size:14px}
.line{height:2px;margin:26px 0;background:linear-gradient(90deg,transparent,#00f0ff,#ff00e5,transparent);box-shadow:0 0 14px #00f0ff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin-bottom:26px}
.card{
  background:rgba(10,16,32,.72);border:1px solid rgba(0,240,255,.35);border-radius:16px;
  padding:20px;box-shadow:0 0 18px rgba(0,240,255,.18),inset 0 0 22px rgba(0,240,255,.06);
  backdrop-filter:blur(6px);
}
.card.pink{border-color:rgba(255,0,229,.38);box-shadow:0 0 18px rgba(255,0,229,.18),inset 0 0 22px rgba(255,0,229,.06)}
.card.lime{border-color:rgba(182,255,0,.35);box-shadow:0 0 18px rgba(182,255,0,.15),inset 0 0 22px rgba(182,255,0,.05)}
.card.violet{border-color:rgba(150,90,255,.4);box-shadow:0 0 18px rgba(150,90,255,.2),inset 0 0 22px rgba(150,90,255,.06)}
.klabel{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#8fb6cc}
.kval{font-size:40px;font-weight:800;margin-top:8px;color:#fff;text-shadow:0 0 12px #00f0ff,0 0 30px #0080ff}
.card.pink .kval{text-shadow:0 0 12px #ff00e5,0 0 30px #ff00e5}
.card.lime .kval{text-shadow:0 0 12px #b6ff00,0 0 30px #7fff00}
.card.violet .kval{text-shadow:0 0 12px #965aff,0 0 30px #965aff}
.kfoot{margin-top:6px;font-size:12px;color:#7d94a8}
h2{
  font-size:18px;letter-spacing:4px;text-transform:uppercase;margin:30px 0 16px;color:#fff;
  text-shadow:0 0 10px #00f0ff,0 0 26px #0090ff;
}
h2.pink{text-shadow:0 0 10px #ff00e5,0 0 26px #ff00e5}
.panel{
  background:rgba(8,12,26,.75);border:1px solid rgba(120,160,255,.18);border-radius:18px;padding:22px;
  box-shadow:0 0 24px rgba(0,0,0,.6),inset 0 0 30px rgba(0,120,255,.05);
}
.chart{display:flex;align-items:flex-end;gap:14px;height:230px;padding:10px 4px 0}
.col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
.bar{
  width:100%;max-width:64px;border-radius:10px 10px 4px 4px;position:relative;
  background:linear-gradient(180deg,#00f0ff,#0066ff 55%,#7a00ff);
  box-shadow:0 0 16px rgba(0,240,255,.65),0 0 34px rgba(0,120,255,.35);
  min-height:6px;
}
.bar.hot{background:linear-gradient(180deg,#ff6bf3,#ff00e5 55%,#8a00ff);box-shadow:0 0 16px rgba(255,0,229,.7),0 0 34px rgba(255,0,229,.35)}
.bar b{position:absolute;top:-24px;left:0;right:0;text-align:center;font-size:13px;color:#fff;text-shadow:0 0 10px #00f0ff}
.day{margin-top:10px;font-size:12px;color:#8fb6cc;letter-spacing:1px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:11px 10px;text-align:center;border-bottom:1px solid rgba(120,160,255,.14)}
th{color:#7fe9ff;text-transform:uppercase;font-size:11px;letter-spacing:2px}
td:first-child,th:first-child{text-align:left}
tr:hover td{background:rgba(0,240,255,.05)}
.badge{display:inline-block;padding:4px 12px;border-radius:999px;font-size:12px;border:1px solid rgba(0,240,255,.45);color:#7fe9ff;box-shadow:0 0 12px rgba(0,240,255,.25)}
.badge.pink{border-color:rgba(255,0,229,.5);color:#ff9df5;box-shadow:0 0 12px rgba(255,0,229,.25)}
.badge.lime{border-color:rgba(182,255,0,.5);color:#dcff8a;box-shadow:0 0 12px rgba(182,255,0,.2)}
.progress{height:14px;border-radius:999px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:12px;border:1px solid rgba(0,240,255,.25)}
.progress i{display:block;height:100%;background:linear-gradient(90deg,#00f0ff,#b6ff00);box-shadow:0 0 18px rgba(0,240,255,.7)}
.rows{display:flex;flex-direction:column;gap:10px}
.row{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid rgba(120,160,255,.12)}
.row span{color:#a9c3d6}
.row b{color:#fff;text-shadow:0 0 10px rgba(0,240,255,.6)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}
.foot{margin-top:34px;text-align:center;color:#63798c;font-size:12px;letter-spacing:2px}
.foot b{color:#00f0ff;text-shadow:0 0 12px #00f0ff}
"""


async def collect_report_data(days=7):
    start = day_start(-(days - 1))
    start_ts = int(start.timestamp())
    rows = await db_all(
        DB_PLAN,
        "SELECT published_at, media_type, source FROM published_history WHERE published_at>=?",
        (start_ts,),
    )
    per_day = {}
    for i in range(days):
        key = (start + timedelta(days=i)).date().isoformat()
        per_day[key] = {
            "total": 0, "photo": 0, "video": 0, "animation": 0, "album": 0, "other": 0,
            "admin": 0, "sugg": 0,
        }
    for ts, mt, src in rows:
        key = datetime.fromtimestamp(ts, TZ_CHANNEL).date().isoformat()
        if key not in per_day:
            continue
        per_day[key]["total"] += 1
        mt = mt if mt in ("photo", "video", "animation", "album") else "other"
        per_day[key][mt] += 1
        if src in ("admin", "sugg"):
            per_day[key][src] += 1

    top = await db_all(
        DB_SUGG,
        """SELECT b.user_id, b.username, COUNT(*) AS cnt
           FROM sugg_posts s JOIN batches b ON b.id=s.batch_id
           WHERE s.status='accepted' AND COALESCE(s.reviewed_at,0)>=? AND COALESCE(b.is_anon,0)=0
           GROUP BY b.user_id ORDER BY cnt DESC LIMIT 5""",
        (start_ts,),
    )
    return {
        "days": days,
        "start": start,
        "per_day": per_day,
        "week_total": sum(d["total"] for d in per_day.values()),
        "queue": await queue_count(),
        "total": await total_published(),
        "sugg": await sugg_stats(),
        "top": top,
        "blacklist": (await db_one(DB_MAIN, "SELECT COUNT(*) FROM blacklist"))[0],
        "next_slot": (await db_one(DB_PLAN, "SELECT MIN(slot) FROM posts"))[0],
    }


def build_html_report(d):
    per_day = d["per_day"]
    max_val = max([v["total"] for v in per_day.values()] + [1])
    today_key = datetime.now(TZ_CHANNEL).date().isoformat()
    wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    bars = []
    trows = []
    for key, v in per_day.items():
        dt = datetime.fromisoformat(key)
        label = f"{wd[dt.weekday()]} {dt.strftime('%d.%m')}"
        pct = max(4, int(v["total"] / max_val * 100))
        hot = " hot" if key == today_key else ""
        bars.append(
            f'<div class="col"><div class="bar{hot}" style="height:{pct}%"><b>{v["total"]}</b></div>'
            f'<div class="day">{label}</div></div>'
        )
        trows.append(
            f"<tr><td>{label}</td><td><b>{v['total']}</b></td><td>{v['photo']}</td><td>{v['video']}</td>"
            f"<td>{v['animation']}</td><td>{v['album']}</td><td>{v['admin']}</td><td>{v['sugg']}</td></tr>"
        )

    s = d["sugg"]
    rate = s["rate"]
    period_from = d["start"].strftime("%d.%m.%Y")
    period_to = datetime.now(TZ_CHANNEL).strftime("%d.%m.%Y")
    generated = datetime.now(TZ_CHANNEL).strftime("%d.%m.%Y %H:%M")
    next_txt = fmt_time(d["next_slot"], ADMIN_ID) if d["next_slot"] else "—"

    top_rows = []
    for i, (uid, uname, cnt) in enumerate(d["top"], start=1):
        medal = ["🥇", "🥈", "🥉", "▫️", "▫️"][i - 1]
        name = f"@{uname}" if uname else f"ID {uid}"
        top_rows.append(f'<div class="row"><span>{medal} {esc(name)}</span><b>{cnt}</b></div>')
    if not top_rows:
        top_rows.append('<div class="row"><span>Пока нет данных</span><b>0</b></div>')

    body = (
        '<div class="wrap">'
        '<div class="head">'
        '<div class="logo">TETO<span>POSTIT</span></div>'
        '<div class="sub">neon analytics report</div>'
        f'<div class="period">Период: {period_from} — {period_to} · часовой пояс: Саратов</div>'
        '</div>'
        '<div class="line"></div>'
        '<div class="grid">'
        f'<div class="card"><div class="klabel">Постов за {d["days"]} дней</div><div class="kval">{d["week_total"]}</div><div class="kfoot">опубликовано в канал</div></div>'
        f'<div class="card pink"><div class="klabel">Всего опубликовано</div><div class="kval">{d["total"]}</div><div class="kfoot">за всё время</div></div>'
        f'<div class="card lime"><div class="klabel">В очереди</div><div class="kval">{d["queue"]}</div><div class="kfoot">следующий: {next_txt}</div></div>'
        f'<div class="card violet"><div class="klabel">Предложка ждёт</div><div class="kval">{s["pending"]}</div><div class="kfoot">новых за неделю: {s["week_new"]}</div></div>'
        '</div>'
        '<h2>Публикации по дням</h2>'
        '<div class="panel"><div class="chart">' + "".join(bars) + '</div></div>'
        '<h2 class="pink">Детализация</h2>'
        '<div class="panel"><table>'
        '<tr><th>День</th><th>Всего</th><th>Фото</th><th>Видео</th><th>Гиф</th><th>Альбом</th><th>Админ</th><th>Предложка</th></tr>'
        + "".join(trows) +
        '</table></div>'
        '<h2>Предложка</h2>'
        '<div class="two">'
        '<div class="panel"><div class="rows">'
        f'<div class="row"><span>✅ Принято всего</span><b>{s["accepted"]}</b></div>'
        f'<div class="row"><span>❌ Отклонено всего</span><b>{s["rejected"]}</b></div>'
        f'<div class="row"><span>⏳ На рассмотрении</span><b>{s["pending"]}</b></div>'
        f'<div class="row"><span>📥 Новых за неделю</span><b>{s["week_new"]}</b></div>'
        f'<div class="row"><span>🚫 В чёрном списке</span><b>{d["blacklist"]}</b></div>'
        '</div></div>'
        '<div class="panel">'
        f'<div class="klabel">Процент одобрения</div><div class="kval" style="font-size:34px">{rate}%</div>'
        f'<div class="progress"><i style="width:{rate}%"></i></div>'
        f'<div style="margin-top:16px"><span class="badge">принято за неделю: {s["week_acc"]}</span> '
        f'<span class="badge pink">отклонено: {s["week_rej"]}</span> '
        f'<span class="badge lime">всего заявок: {s["total"]}</span></div>'
        '</div>'
        '</div>'
        '<h2 class="pink">Топ авторов недели</h2>'
        '<div class="panel"><div class="rows">' + "".join(top_rows) + '</div></div>'
        '<h2>Режим работы</h2>'
        '<div class="panel"><div class="rows">'
        '<div class="row"><span>🌙 Тихие часы</span><b>00:00 — 07:00 (Саратов)</b></div>'
        '<div class="row"><span>↪️ Ночные посты</span><b>автоперенос на 07:00</b></div>'
        f'<div class="row"><span>🔔 Напоминание о队列</span><b>каждые 30 мин при ≤ {LOW_QUEUE_THRESHOLD}</b></div>'
        '</div></div>'
        f'<div class="foot">Сгенерировано <b>{generated}</b> · TETO POSTIT · neon build</div>'
        '</div>'
    )
    body = body.replace("о队列", "об очереди")
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>TETO POSTIT · Neon Report</title><style>"
        + REPORT_CSS
        + "</style></head><body>"
        + body
        + "</body></html>"
    )


async def send_html_report(chat_id):
    data = await collect_report_data(7)
    html_text = build_html_report(data)
    filename = f"teto_report_{datetime.now(TZ_CHANNEL).strftime('%Y-%m-%d_%H%M')}.html"
    file = BufferedInputFile(html_text.encode("utf-8"), filename=filename)
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=file,
            caption=(
                "📄 <b>Неоновый отчёт готов</b>\n"
                f"🗓 За 7 дней: <b>{data['week_total']}</b> постов\n"
                f"📦 В очереди: <b>{data['queue']}</b>\n"
                "Открой файл в браузере ✨"
            ),
        )
        return True
    except Exception:
        logger.exception("Не удалось отправить отчёт")
        return False


# =============================================================================
# ОЧИСТКА
# =============================================================================
async def cleanup_old_data():
    now = int(time.time())
    rej_cut = now - CLEANUP_REJECTED_DAYS * 86400
    log_cut = now - CLEANUP_LOGS_DAYS * 86400
    hist_cut = now - CLEANUP_HISTORY_DAYS * 86400

    def sugg_fn(c):
        cur = c.execute(
            "DELETE FROM sugg_posts WHERE status='rejected' "
            "AND COALESCE(reviewed_at, created_at, 0) > 0 "
            "AND COALESCE(reviewed_at, created_at, 0) < ?",
            (rej_cut,),
        )
        deleted = cur.rowcount or 0
        c.execute(
            "DELETE FROM batches WHERE id NOT IN (SELECT DISTINCT batch_id FROM sugg_posts WHERE batch_id IS NOT NULL) "
            "AND COALESCE(created_at,0) < ?",
            (rej_cut,),
        )
        return deleted

    deleted_sugg = await db_run(DB_SUGG, sugg_fn)
    deleted_logs = await db_run(DB_MAIN, lambda c: c.execute("DELETE FROM audit_log WHERE ts<?", (log_cut,)).rowcount or 0)
    deleted_hist = await db_run(DB_PLAN, lambda c: c.execute("DELETE FROM published_history WHERE published_at<?", (hist_cut,)).rowcount or 0)
    return {"sugg": deleted_sugg, "logs": deleted_logs, "hist": deleted_hist}


# =============================================================================
# BOT
# =============================================================================
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class UserStates(StatesGroup):
    waiting_media = State()
    choosing_anon = State()
    confirming = State()


class AdminStates(StatesGroup):
    waiting_move = State()


ALBUMS = {}
admin_batches = {}
admin_preview_tasks = {}


async def notify_user_accept(user_id, slot=None):
    if not user_id:
        return
    if slot:
        await safe_send(user_id, f"✅ <b>Принято!</b>\n🕒 Публикация: <b>{fmt_time(slot, user_id)}</b>.")
    else:
        await safe_send(user_id, "✅ <b>Принято!</b>\nТвоё предложение появится в канале.")


async def notify_user_reject(user_id, reason):
    if not user_id:
        return
    await safe_send(user_id, f"❌ <b>Отклонено</b>\nПричина: {esc(reason)}")


def accepted_caption_raw(con, post_id):
    row = con.execute(
        """SELECT s.caption, b.username, b.user_id, b.is_anon
           FROM sugg_posts s JOIN batches b ON b.id=s.batch_id WHERE s.id=?""",
        (post_id,),
    ).fetchone()
    if not row:
        return "Предложил - неизвестный пользователь", None, None
    caption, username, user_id, is_anon = row
    if is_anon:
        attribution = "Предложил - Анон."
    else:
        attribution = f"Предложил - @{username}" if username else f"Предложил - {user_id}"
    return attribution + ("\n\n" + caption if caption else ""), user_id, None


# =============================================================================
# СТАРТ
# =============================================================================
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid in ALL_ADMINS:
        paused = await get_setting("paused", False)
        await message.answer(await build_main_text(uid), reply_markup=get_reply_kb(paused))
        await message.answer("⬇️ Меню действий:", reply_markup=get_main_inline(paused))
        return
    if await is_blocked(uid):
        return
    await message.answer(
        "👋 <b>Привет!</b>\n"
        "Я принимаю арты, видео и гифки в предложку.\n"
        "Просто отправь медиа — я передам его админам."
    )
    await state.set_state(UserStates.waiting_media)


@router.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await message.answer(
        "ℹ️ <b>Помощь</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "• Кидай фото/видео/гифку — встанет в очередь.\n"
        "• 🌙 С 00:00 до 07:00 (Саратов) посты не выходят — переносятся на 07:00.\n"
        "• 👀 Предпросмотр поста — кнопка в карточке поста.\n"
        "• 🔔 Если в очереди ≤ 2 постов — напомню каждые 30 минут.\n"
        "• /report — неоновый HTML-отчёт, /cleanup — очистка, /stats — статистика.",
        reply_markup=get_back_kb(),
    )


@router.message(Command("report"))
async def cmd_report(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await message.answer("⏳ Собираю отчёт...")
    await send_html_report(message.from_user.id)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await message.answer(await build_stats_text(), reply_markup=get_stats_kb())


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Очистить", callback_data="cl:yes")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")],
        ]
    )
    await message.answer(
        "🧹 <b>Очистка базы</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"• Отклонённые предложки старше {CLEANUP_REJECTED_DAYS} дней\n"
        f"• Логи старше {CLEANUP_LOGS_DAYS} дней\n"
        f"• История публикаций старше {CLEANUP_HISTORY_DAYS} дней\n\n"
        "Чёрный список и очередь не трогаются.",
        reply_markup=kb,
    )


# =============================================================================
# ПРИЁМ МЕДИА (дебаунс альбомов)
# =============================================================================
async def flush_album(key, state):
    try:
        await asyncio.sleep(1.6)
        entry = ALBUMS.pop(key, None)
        if not entry:
            return
        uid, is_admin = key[0], key[2]
        msgs = sorted(entry["msgs"], key=lambda m: m.message_id)
        item = {
            "chat_id": uid,
            "msg_ids": [m.message_id for m in msgs],
            "cap": msgs[0].caption or "",
            "media_type": detect_media_type(msgs),
        }
        if is_admin:
            admin_batches.setdefault(uid, []).append(item)
            if uid in admin_preview_tasks:
                admin_preview_tasks[uid].cancel()
            admin_preview_tasks[uid] = asyncio.create_task(send_preview_after_delay(uid, state))
        else:
            await state.update_data(
                user_chat_id=uid,
                user_msg_ids=item["msg_ids"],
                user_cap=item["cap"],
                user_media_type=item["media_type"],
            )
            await bot.send_message(
                uid,
                "📨 <b>Как предложить?</b>\n\n👻 Анонимно — без имени\n👤 Не анонимно — с твоим именем",
                reply_markup=get_user_anon_kb(),
            )
            await state.set_state(UserStates.choosing_anon)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Ошибка сборки альбома")


async def send_preview_after_delay(uid, state):
    try:
        await asyncio.sleep(3.0)
        batch = admin_batches.get(uid, [])
        if not batch:
            return
        slots = await allocate_slots(len(batch))
        text = (
            "📦 <b>Предпросмотр пачки</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Постов: <b>{len(batch)}</b>\n"
            f"Первый слот: <b>{fmt_time(slots[0], uid)}</b>\n"
            f"Последний слот: <b>{fmt_time(slots[-1], uid)}</b>\n\n"
            "Отложить?"
        )
        if await get_setting("paused", False):
            text += "\n\n⏰ <b>Постинг сейчас на паузе.</b>"
        if any(is_quiet_ts(s) for s in slots):
            text += "\n🌙 Ночные слоты будут сдвинуты на 07:00."
        data = await state.get_data()
        old_msg_id = data.get("preview_msg_id")
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=uid, message_id=old_msg_id)
            except Exception:
                pass
        msg = await bot.send_message(uid, text, reply_markup=get_preview_kb())
        await state.update_data(preview_msg_id=msg.message_id)
    except asyncio.CancelledError:
        pass


@router.message(F.photo | F.video | F.animation, F.chat.type == "private")
async def handle_media(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_admin = uid in ALL_ADMINS

    if not is_admin and await is_blocked(uid):
        return

    if is_admin and await state.get_state() == AdminStates.waiting_move.state:
        await state.clear()

    if message.media_group_id:
        key = (uid, message.media_group_id, is_admin)
        entry = ALBUMS.setdefault(key, {"msgs": [], "task": None})
        entry["msgs"].append(message)
        if entry["task"]:
            entry["task"].cancel()
        entry["task"] = asyncio.create_task(flush_album(key, state))
        return

    item = {
        "chat_id": uid,
        "msg_ids": [message.message_id],
        "cap": message.caption or "",
        "media_type": detect_media_type([message]),
    }

    if is_admin:
        admin_batches.setdefault(uid, []).append(item)
        if uid in admin_preview_tasks:
            admin_preview_tasks[uid].cancel()
        admin_preview_tasks[uid] = asyncio.create_task(send_preview_after_delay(uid, state))
        return

    await state.update_data(
        user_chat_id=uid,
        user_msg_ids=item["msg_ids"],
        user_cap=item["cap"],
        user_media_type=item["media_type"],
    )
    await message.answer(
        "📨 <b>Как предложить?</b>\n\n👻 Анонимно — без имени\n👤 Не анонимно — с твоим именем",
        reply_markup=get_user_anon_kb(),
    )
    await state.set_state(UserStates.choosing_anon)


# =============================================================================
# ПОДТВЕРЖДЕНИЕ ПАЧКИ АДМИНОМ
# =============================================================================
@router.callback_query(F.data.startswith("a:"))
async def admin_preview_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)
    if check_flood(uid):
        return await query.answer("Слишком часто, подожди 5 сек.", show_alert=True)

    if query.data == "a:cancel":
        admin_batches.pop(uid, None)
        await state.clear()
        await query.answer("Отменено")
        try:
            await query.message.edit_text("❌ Отменено.")
        except TelegramBadRequest:
            pass
        return

    batch = admin_batches.pop(uid, None)
    if not batch:
        return await query.answer("Пусто!", show_alert=True)

    slots = await allocate_slots(len(batch))
    now = int(time.time())

    def insert_all(c):
        for slot, item in zip(slots, batch):
            c.execute(
                """INSERT INTO posts(slot,caption,source,author_id,chat_id,msg_ids,created_at,media_type)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (slot, item["cap"], "admin", uid, item["chat_id"], json.dumps(item["msg_ids"]), now, item["media_type"]),
            )
    await db_run(DB_PLAN, insert_all)

    await state.clear()
    await query.answer("Готово!")
    try:
        await query.message.edit_text(
            "✅ <b>Отложено!</b>\n"
            f"📦 Постов: <b>{len(batch)}</b>\n"
            f"🕒 Первый: <b>{fmt_time(slots[0], uid)}</b>\n"
            f"🕒 Последний: <b>{fmt_time(slots[-1], uid)}</b>"
        )
    except TelegramBadRequest:
        pass

    await audit(uid, "Добавлены посты в очередь", f"Количество: <b>{len(batch)}</b>\nПервый слот: {fmt_time(slots[0], uid)}")


# =============================================================================
# ГЛАВНОЕ МЕНЮ
# =============================================================================
@router.callback_query(F.data.startswith("m:"))
async def admin_menu_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)
    if check_flood(uid):
        return await query.answer("Слишком часто, подожди 5 сек.", show_alert=True)

    action = query.data.split(":")[1]
    await query.answer()

    if action in ("main", "pause"):
        if action == "pause":
            new = not await get_setting("paused", False)
            await set_setting("paused", new)
            await audit(uid, "Постинг поставлен на паузу" if new else "Постинг возобновлён", "")
        paused = await get_setting("paused", False)
        try:
            await query.message.edit_text(await build_main_text(uid), reply_markup=get_main_inline(paused))
        except TelegramBadRequest:
            pass

    elif action == "stats":
        try:
            await query.message.edit_text(await build_stats_text(), reply_markup=get_stats_kb())
        except TelegramBadRequest:
            pass

    elif action == "help":
        try:
            await query.message.edit_text(
                "ℹ️ <b>Помощь</b>\n"
                "━━━━━━━━━━━━━━━━\n"
                "• Кидай медиа — встанет в очередь.\n"
                "• 🌙 00:00–07:00 (Саратов) — тишина, посты едут на 07:00.\n"
                "• 👀 Предпросмотр — в карточке поста.\n"
                "• 🔔 Напоминание при ≤ 2 постах каждые 30 минут.\n"
                "• /report · /cleanup · /stats",
                reply_markup=get_back_kb(),
            )
        except TelegramBadRequest:
            pass

    elif action == "blacklist":
        text, kb = await get_blacklist_view()
        try:
            await query.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass

    elif action == "cleanup":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Очистить", callback_data="cl:yes")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")],
            ]
        )
        try:
            await query.message.edit_text(
                "🧹 <b>Очистка базы</b>\n"
                "━━━━━━━━━━━━━━━━\n"
                f"• Отклонённые предложки старше {CLEANUP_REJECTED_DAYS} дней\n"
                f"• Логи старше {CLEANUP_LOGS_DAYS} дней\n"
                f"• История публикаций старше {CLEANUP_HISTORY_DAYS} дней\n\n"
                "Очередь и ЧС не трогаются.",
                reply_markup=kb,
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "cl:yes")
async def cleanup_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)
    await query.answer("Чищу...")
    res = await cleanup_old_data()
    await set_setting("last_cleanup", int(time.time()))
    try:
        await query.message.edit_text(
            "🧹 <b>Очистка завершена</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Отклонённые предложки: <b>{res['sugg']}</b>\n"
            f"Логи: <b>{res['logs']}</b>\n"
            f"История публикаций: <b>{res['hist']}</b>",
            reply_markup=get_back_kb(),
        )
    except TelegramBadRequest:
        pass
    await audit(uid, "Очистка базы", f"Предложка: {res['sugg']}\nЛоги: {res['logs']}\nИстория: {res['hist']}", notify=False)


# =============================================================================
# СТАТИСТИКА / ОТЧЁТ / ЛОГИ
# =============================================================================
@router.callback_query(F.data.startswith("st:"))
async def stats_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)
    action = query.data.split(":")[1]

    if action == "report":
        await query.answer("Генерирую отчёт...")
        ok = await send_html_report(uid)
        if not ok:
            await safe_send(uid, "⚠️ Не удалось сформировать отчёт.")
        return

    await query.answer()

    if action == "days":
        data = await collect_report_data(7)
        wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        lines = []
        for key, v in data["per_day"].items():
            dt = datetime.fromisoformat(key)
            bar = "▰" * min(v["total"], 12)
            lines.append(f"<b>{wd[dt.weekday()]} {dt.strftime('%d.%m')}</b> — {v['total']} {bar}")
        text = (
            "📈 <b>Публикации по дням</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
            + f"\n\nИтого за 7 дней: <b>{data['week_total']}</b>"
        )
        try:
            await query.message.edit_text(text, reply_markup=get_stats_kb())
        except TelegramBadRequest:
            pass

    elif action == "sugg":
        s = await sugg_stats()
        text = (
            "📥 <b>Статистика предложки</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Всего заявок: <b>{s['total']}</b>\n"
            f"⏳ Ожидает: <b>{s['pending']}</b>\n"
            f"✅ Принято: <b>{s['accepted']}</b>\n"
            f"❌ Отклонено: <b>{s['rejected']}</b>\n"
            f"🚫 В ЧС: <b>{s['banned']}</b>\n\n"
            f"🗓 За 7 дней: новых <b>{s['week_new']}</b>, "
            f"принято <b>{s['week_acc']}</b>, отклонено <b>{s['week_rej']}</b>\n"
            f"📊 Процент одобрения: <b>{s['rate']}%</b>"
        )
        try:
            await query.message.edit_text(text, reply_markup=get_stats_kb())
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("log:"))
async def logs_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)
    try:
        page = max(0, int(query.data.split(":")[1]))
    except Exception:
        page = 0
    per_page = 8
    total = (await db_one(DB_MAIN, "SELECT COUNT(*) FROM audit_log"))[0]
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)
    rows = await db_all(
        DB_MAIN,
        "SELECT ts, admin_id, action, details FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    text = "🧾 <b>Журнал действий</b>\n━━━━━━━━━━━━━━━━\n"
    if not rows:
        text += "✨ Пока пусто."
    else:
        lines = []
        for ts, admin_id, action, details in rows:
            who = "🤖" if not admin_id else ("👑" if admin_id == ADMIN_ID else "👤")
            det = (details or "").replace("\n", " · ")
            lines.append(f"{who} <b>{esc(action)}</b>\n   {det}\n   🕒 {fmt_time(ts, ADMIN_ID)}")
        text += "\n\n".join(lines)

    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"log:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"log:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton(text=f"📄 {page + 1}/{pages}", callback_data="noop")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        pass


# =============================================================================
# КОНТЕНТ-ПЛАН
# =============================================================================
@router.callback_query(F.data.startswith("planf:"))
async def content_plan_filter_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    if check_flood(uid):
        return await query.answer("Слишком часто, подожди 5 сек.", show_alert=True)

    parts = query.data.split(":")
    filter_name = parts[1] if len(parts) > 1 else "all"
    if filter_name not in {"today", "tomorrow", "week", "all"}:
        filter_name = "all"
    try:
        page = max(0, int(parts[2]))
    except Exception:
        page = 0

    per_page = 5
    rng = get_day_range(filter_name, uid)
    if rng:
        total = (await db_one(DB_PLAN, "SELECT COUNT(*) FROM posts WHERE slot>=? AND slot<?", rng))[0]
        posts = await db_all(
            DB_PLAN,
            "SELECT id, slot, caption FROM posts WHERE slot>=? AND slot<? ORDER BY slot LIMIT ? OFFSET ?",
            (*rng, per_page, page * per_page),
        )
    else:
        total = (await db_one(DB_PLAN, "SELECT COUNT(*) FROM posts"))[0]
        posts = await db_all(
            DB_PLAN,
            "SELECT id, slot, caption FROM posts ORDER BY slot LIMIT ? OFFSET ?",
            (per_page, page * per_page),
        )

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)
    modes = {"today": "📅 Сегодня", "tomorrow": "🌇 Завтра", "week": "🗓 Неделя", "all": "♾ Все"}
    text = (
        "🗂 <b>Контент-план</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Режим: {modes[filter_name]}\n"
        f"Всего: <b>{total}</b>\n"
        "🌙 Ночные посты переносятся на 07:00\n"
    )

    rows = []
    if not posts:
        text += "\n✨ Очередь пуста."
        rows.append([InlineKeyboardButton(text="✨ Очередь пуста", callback_data="noop")])
    else:
        for pid, slot, cap in posts:
            moon = "🌙 " if is_quiet_ts(slot) else ""
            cap_short = esc(cap or "Без подписи")[:22]
            rows.append([
                InlineKeyboardButton(
                    text=f"{moon}🕒 {fmt_time(slot, uid)} • {cap_short}",
                    callback_data=f"view:{pid}:{page}:{filter_name}",
                )
            ])

    def fbtn(code, label):
        active = "✅ " if filter_name == code else ""
        return InlineKeyboardButton(text=f"{active}{label}", callback_data=f"planf:{code}:0")

    rows.append([fbtn("today", "📅 Сегодня"), fbtn("tomorrow", "🌇 Завтра")])
    rows.append([fbtn("week", "🗓 Неделя"), fbtn("all", "♾ Все")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data=f"planf:{filter_name}:0"))
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"planf:{filter_name}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"planf:{filter_name}:{page + 1}"))
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"planf:{filter_name}:{pages - 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=f"📄 {page + 1}/{pages}", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("plan:"))
async def content_plan_cb(query: CallbackQuery):
    if query.from_user.id not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    try:
        page = int(query.data.split(":")[1])
    except Exception:
        page = 0
    query.data = f"planf:all:{page}"
    await content_plan_filter_cb(query)


def post_card_kb(pid, page, filter_name):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Предпросмотр", callback_data=f"prev:{pid}:{page}:{filter_name}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_del:{pid}:{page}:{filter_name}")],
            [
                InlineKeyboardButton(text="🕒 Следующий слот", callback_data=f"move_next:{pid}:{page}:{filter_name}"),
                InlineKeyboardButton(text="📅 Указать дату", callback_data=f"move_date:{pid}:{page}:{filter_name}"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"planf:{filter_name}:{page}")],
        ]
    )


@router.callback_query(F.data.startswith("view:"))
async def view_post_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    try:
        page = int(parts[2])
    except Exception:
        page = 0
    filter_name = parts[3] if len(parts) > 3 else "all"
    if filter_name not in {"today", "tomorrow", "week", "all"}:
        filter_name = "all"

    row = await db_one(DB_PLAN, "SELECT slot, caption, media_type FROM posts WHERE id=?", (pid,))
    if not row:
        await query.answer("Пост удалён!", show_alert=True)
        query.data = f"planf:{filter_name}:{page}"
        return await content_plan_filter_cb(query)

    slot, cap, media_type = row
    moon = "\n🌙 <i>Попадает в тихие часы — уедет на 07:00</i>" if is_quiet_ts(slot) else ""
    text = (
        "📦 <b>Пост в очереди</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🕒 Время: <b>{fmt_time(slot, uid)}</b>\n"
        f"🆔 ID: <code>{pid}</code>\n"
        f"🎞 Тип: {MEDIA_LABEL.get(media_type or 'other', '📦 Другое')}\n"
        f"📝 Подпись: {esc(cap or '—')[:300]}"
        f"{moon}"
    )
    await query.answer()
    await bot.send_message(uid, text, reply_markup=post_card_kb(pid, page, filter_name))


@router.callback_query(F.data.startswith("prev:"))
async def preview_post_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    filter_name = parts[3] if len(parts) > 3 else "all"

    row = await db_one(DB_PLAN, "SELECT caption, chat_id, msg_ids FROM posts WHERE id=?", (pid,))
    if not row:
        return await query.answer("Пост удалён!", show_alert=True)

    cap, chat_id, msg_ids_json = row
    msg_ids = json.loads(msg_ids_json)
    safe_cap = esc(cap) if cap else None
    await query.answer("Показываю...")

    await bot.send_message(uid, "👀 <b>Так пост будет выглядеть в канале:</b>")
    try:
        if len(msg_ids) > 1:
            await bot.copy_messages(chat_id=uid, from_chat_id=chat_id, message_ids=msg_ids)
            if safe_cap:
                await bot.send_message(uid, safe_cap)
        else:
            await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_ids[0], caption=safe_cap or None)
    except Exception as e:
        await bot.send_message(uid, f"⚠️ Не удалось показать медиа: {esc(str(e))}")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 К посту", callback_data=f"view:{pid}:{page}:{filter_name}")],
            [InlineKeyboardButton(text="🗂 К очереди", callback_data=f"planf:{filter_name}:{page}")],
        ]
    )
    await bot.send_message(uid, "👆 Это предпросмотр. В канал уйдёт именно так.", reply_markup=kb)


# =============================================================================
# УДАЛЕНИЕ
# =============================================================================
@router.callback_query(F.data.startswith("confirm_del:"))
async def confirm_del_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    pid, page = int(parts[1]), int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    row = await db_one(DB_PLAN, "SELECT slot FROM posts WHERE id=?", (pid,))
    if not row:
        await query.answer("Пост удалён!", show_alert=True)
        query.data = f"planf:{filter_name}:{page}"
        return await content_plan_filter_cb(query)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_yes:{pid}:{page}:{filter_name}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"del_no:{pid}:{page}:{filter_name}"),
            ]
        ]
    )
    await query.answer()
    try:
        await query.message.edit_text(
            "🗑 <b>Удаление поста</b>\n━━━━━━━━━━━━━━━━\n"
            f"Удалить пост на <b>{fmt_time(row[0], uid)}</b>?\nЭто действие нельзя отменить.",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("del_yes:"))
async def del_yes_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    pid, page = int(parts[1]), int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    row = await db_one(DB_PLAN, "SELECT slot FROM posts WHERE id=?", (pid,))
    await db_exec(DB_PLAN, "DELETE FROM posts WHERE id=?", (pid,))
    await query.answer("Пост удалён!")
    await audit(uid, "Удалён пост из очереди", f"ID: <code>{pid}</code>\nСлот: {fmt_time(row[0], uid) if row else '—'}")
    query.data = f"planf:{filter_name}:{page}"
    await content_plan_filter_cb(query)


@router.callback_query(F.data.startswith("del_no:"))
async def del_no_cb(query: CallbackQuery):
    if query.from_user.id not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    page = int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"
    await query.answer("Отменено")
    query.data = f"planf:{filter_name}:{page}"
    await content_plan_filter_cb(query)


# =============================================================================
# ПЕРЕНОС
# =============================================================================
@router.callback_query(F.data.startswith("move_next:"))
async def move_next_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    pid, page = int(parts[1]), int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    new_slot = await get_next_slot()
    await db_exec(DB_PLAN, "UPDATE posts SET slot=? WHERE id=?", (new_slot, pid))
    await query.answer(f"Перенесён на {fmt_time(new_slot, uid)}")
    await audit(uid, "Пост перенесён", f"ID: <code>{pid}</code>\nНовое время: {fmt_time(new_slot, uid)}")
    query.data = f"planf:{filter_name}:{page}"
    await content_plan_filter_cb(query)


@router.callback_query(F.data.startswith("move_date:"))
async def move_date_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    pid, page = int(parts[1]), int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    await state.set_state(AdminStates.waiting_move)
    await state.update_data(move_pid=pid, move_page=page, move_filter=filter_name)
    await query.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="move_cancel")]])
    try:
        await query.message.edit_text(
            "📅 <b>Перенос поста</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            "Отправь дату и время (по Саратову):\n"
            "<code>DD.MM.YYYY HH:MM</code>\n\n"
            "Пример: <code>25.11.2026 18:30</code>\n\n"
            "🌙 Если попадёшь в 00:00–07:00 — перенесу на 07:00.",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "move_cancel")
async def move_cancel_cb(query: CallbackQuery, state: FSMContext):
    if query.from_user.id not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    data = await state.get_data()
    await state.clear()
    await query.answer("Отменено")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="🗂 Открыть план",
            callback_data=f"planf:{data.get('move_filter', 'all')}:{data.get('move_page', 0)}",
        )]]
    )
    try:
        await query.message.edit_text("❌ Перенос отменён.", reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.message(AdminStates.waiting_move, F.text)
async def process_move_date(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in ALL_ADMINS:
        return
    if message.text.lower().strip() in {"отмена", "cancel"}:
        await state.clear()
        return await message.answer("❌ Отменено.")

    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ_CHANNEL)
        slot = int(dt.timestamp())
    except ValueError:
        return await message.answer(
            "⚠️ Неверный формат.\nНужно: <code>DD.MM.YYYY HH:MM</code>\nПример: <code>25.11.2026 18:30</code>"
        )

    if slot <= int(time.time()):
        return await message.answer("⚠️ Время уже прошло. Укажи будущее время.")

    shifted = shift_out_of_quiet(slot)
    was_shifted = shifted != slot
    slot = shifted

    data = await state.get_data()
    pid = data.get("move_pid")
    page = data.get("move_page", 0)
    filter_name = data.get("move_filter", "all")
    await state.clear()
    if not pid:
        return await message.answer("⚠️ Не нашёл пост для переноса.")

    await db_exec(DB_PLAN, "UPDATE posts SET slot=? WHERE id=?", (slot, pid))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть план", callback_data=f"planf:{filter_name}:{page}")]]
    )
    extra = "\n🌙 Время попало в тихие часы — сдвинул на 07:00." if was_shifted else ""
    await message.answer(
        f"✅ <b>Пост перенесён!</b>\n🕒 Новое время: <b>{fmt_time(slot, uid)}</b>{extra}",
        reply_markup=kb,
    )
    await audit(uid, "Пост перенесён вручную", f"ID: <code>{pid}</code>\nНовое время: {fmt_time(slot, uid)}{extra}")


# =============================================================================
# ИСТОРИЯ ПРЕДЛОЖКИ
# =============================================================================
@router.callback_query(F.data.startswith("hist:"))
async def history_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    try:
        page = max(0, int(query.data.split(":")[1]))
    except Exception:
        page = 0

    per_page = 5
    total = (await db_one(DB_SUGG, "SELECT COUNT(*) FROM sugg_posts"))[0]
    rows = await db_all(
        DB_SUGG,
        """SELECT s.id, s.status, s.caption, s.created_at, s.reject_reason, s.reviewed_at,
                  b.username, b.user_id, b.is_anon, b.status
           FROM sugg_posts s LEFT JOIN batches b ON b.id=s.batch_id
           ORDER BY s.id DESC LIMIT ? OFFSET ?""",
        (per_page, page * per_page),
    )
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)

    text = "📜 <b>История предложки</b>\n━━━━━━━━━━━━━━━━\n"
    if not rows:
        text += "✨ История пуста."
    else:
        lines = []
        for sid, s_status, caption, created_at, reject_reason, reviewed_at, username, user_id, is_anon, b_status in rows:
            emoji, status_text = history_status(s_status, b_status)
            ts = created_at or reviewed_at or 0
            author = "👻 Аноним" if is_anon else (f"👤 @{esc(username)}" if username else f"👤 ID {user_id}")
            line = (
                f"{emoji} <b>#{sid}</b> • {fmt_time(ts, uid) if ts else '—'}\n"
                f"Автор: {author}\n"
                f"Статус: {status_text}\n"
                f"Текст: {esc(caption or 'Без подписи')[:90]}"
            )
            if s_status == "rejected" and reject_reason:
                line += f"\nПричина: {esc(reject_reason)}"
            lines.append(line)
        text += "\n\n".join(lines)

    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data="hist:0"))
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"hist:{page + 1}"))
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"hist:{pages - 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton(text=f"📄 {page + 1}/{pages}", callback_data="noop")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        pass


# =============================================================================
# ЧС
# =============================================================================
@router.callback_query(F.data.startswith("unblock:"))
async def unblock_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        return await query.answer("Некорректный ID", show_alert=True)
    await unblock_user(user_id)
    await query.answer(f"Разблокирован {user_id}!")
    await audit(uid, "Разбан пользователя", f"ID: <code>{user_id}</code>")
    text, kb = await get_blacklist_view()
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "noop")
async def noop_cb(query: CallbackQuery):
    await query.answer()


# =============================================================================
# ПРЕДЛОЖКА ПОЛЬЗОВАТЕЛЯ
# =============================================================================
@router.callback_query(F.data.startswith("u:"))
async def user_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id
    if await is_blocked(uid):
        return await query.answer("Заблокировано", show_alert=True)

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await state.clear()
        await query.answer("Отменено")
        try:
            await query.message.edit_text("❌ Отменено.")
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    chat_id = data.get("user_chat_id")
    msg_ids = data.get("user_msg_ids", [])
    cap = data.get("user_cap", "")
    media_type = data.get("user_media_type", "other")

    if not msg_ids:
        return await query.answer("Пусто! Отправь медиа ещё раз.", show_alert=True)

    if action in {"anon", "nonanon"}:
        await state.update_data(user_anon=(action == "anon"))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтверждаю", callback_data="u:confirm")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="u:cancel")],
            ]
        )
        label = "анонимную" if action == "anon" else "не анонимную"
        await query.answer()
        try:
            await query.message.edit_text(
                f"📨 Подтверждаете <b>{label}</b> отправку?", reply_markup=kb
            )
        except TelegramBadRequest:
            pass
        await state.set_state(UserStates.confirming)
        return

    if action == "confirm":
        if await state.get_state() != UserStates.confirming.state:
            return await query.answer("Эта отправка уже обработана.", show_alert=True)
        is_anon = data.get("user_anon", True)
        now = int(time.time())

        def save(con):
            cur = con.execute(
                "INSERT INTO batches(user_id,username,is_anon,status,created_at,last_sent) VALUES(?,?,?,?,?,?)",
                (uid, query.from_user.username, int(is_anon), "pending", now, now),
            )
            bid = cur.lastrowid
            cur = con.execute(
                """INSERT INTO sugg_posts(batch_id,caption,status,pos,chat_id,msg_ids,created_at,media_type)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (bid, cap, "pending", 0, chat_id, json.dumps(msg_ids), now, media_type),
            )
            return bid, cur.lastrowid

        try:
            bid, spid = await db_run(DB_SUGG, save)
        except sqlite3.Error:
            logger.exception("Ошибка сохранения предложки %s", uid)
            return await query.answer("Не удалось сохранить. Сообщи админу.", show_alert=True)

        await query.answer("Отправлено!")
        await state.clear()
        try:
            await query.message.edit_text("✅ Отправлено админам на рассмотрение.\nСпасибо! 🎉")
        except TelegramBadRequest:
            pass

        author = "👻 Анонимно" if is_anon else (
            f"👤 @{esc(query.from_user.username)}" if query.from_user.username else f"👤 ID {uid}"
        )
        full_cap = (
            "📥 <b>Новая предложка</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Автор: {author}\n"
            f"Пачка: <code>{bid}</code>\n"
            f"Тип: {MEDIA_LABEL.get(media_type, '📦 Другое')}\n"
        )
        if cap:
            full_cap += f"\n💬 Комментарий:\n{esc(cap)}"

        for admin in ALL_ADMINS:
            try:
                if len(msg_ids) > 1:
                    await bot.copy_messages(chat_id=admin, from_chat_id=chat_id, message_ids=msg_ids)
                else:
                    await bot.copy_message(chat_id=admin, from_chat_id=chat_id, message_id=msg_ids[0])
                await bot.send_message(admin, full_cap, reply_markup=get_review_kb(bid, spid))
            except Exception as e:
                logger.error("Не смог отправить предложку админу %s: %s", admin, e)
        return

    await query.answer("Неизвестное действие", show_alert=True)


# =============================================================================
# РЕВЬЮ
# =============================================================================
@router.callback_query(F.data.startswith("r:"))
async def review_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)
    if check_flood(uid):
        return await query.answer("Слишком часто, подожди 5 сек.", show_alert=True)

    parts = query.data.split(":")
    if len(parts) < 3:
        return await query.answer("Некорректные данные", show_alert=True)
    action = parts[1]
    now = int(time.time())

    if action == "acc":
        spid = int(parts[2])
        info = await db_one(
            DB_SUGG,
            """SELECT s.batch_id, s.status, s.chat_id, s.msg_ids, s.media_type, b.user_id
               FROM sugg_posts s JOIN batches b ON b.id=s.batch_id WHERE s.id=?""",
            (spid,),
        )
        if not info or info[1] != "pending":
            return await query.answer("Уже рассмотрено!", show_alert=True)
        bid, _, chat_id, msg_ids_json, media_type, user_id = info

        cap = await db_run(DB_SUGG, lambda c: accepted_caption_raw(c, spid)[0])
        slot = await get_next_slot()
        await db_exec(
            DB_PLAN,
            """INSERT INTO posts(slot,caption,source,author_id,chat_id,msg_ids,created_at,media_type)
               VALUES(?,?,?,?,?,?,?,?)""",
            (slot, cap, "sugg", user_id, chat_id, msg_ids_json, now, media_type),
        )
        await db_exec(
            DB_SUGG,
            "UPDATE sugg_posts SET status='accepted', reviewed_by=?, reviewed_at=? WHERE id=?",
            (uid, now, spid),
        )
        await notify_user_accept(user_id, slot)
        await query.answer("Принято!")
        try:
            await query.message.edit_text(f"✅ Принято!\n🕒 Слот: {fmt_time(slot, uid)}")
        except TelegramBadRequest:
            pass
        await audit(uid, "Принята предложка", f"Заявка #{spid}\nСлот: {fmt_time(slot, uid)}")

    elif action == "rej":
        spid = int(parts[2])
        row = await db_one(DB_SUGG, "SELECT batch_id, status FROM sugg_posts WHERE id=?", (spid,))
        if not row or row[1] != "pending":
            return await query.answer("Уже рассмотрено!", show_alert=True)
        rows = [[InlineKeyboardButton(text=t, callback_data=f"r:rejdo:{spid}:{c}")] for c, t in REASONS]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"r:back:{row[0]}:{spid}")])
        await query.answer()
        try:
            await query.message.edit_text(
                "❌ <b>Причина отклонения</b>\nВыбери, почему отклоняешь:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest:
            pass

    elif action == "rejdo":
        spid = int(parts[2])
        code = parts[3] if len(parts) > 3 else "none"
        reason = REASON_MAP.get(code, "Без причины")
        row = await db_one(
            DB_SUGG,
            """SELECT s.status, b.user_id FROM sugg_posts s JOIN batches b ON b.id=s.batch_id WHERE s.id=?""",
            (spid,),
        )
        if not row or row[0] != "pending":
            return await query.answer("Уже рассмотрено!", show_alert=True)
        await db_exec(
            DB_SUGG,
            "UPDATE sugg_posts SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (reason, uid, now, spid),
        )
        await notify_user_reject(row[1], reason)
        await query.answer("Отклонено!")
        try:
            await query.message.edit_text(f"❌ Отклонено.\nПричина: {esc(reason)}")
        except TelegramBadRequest:
            pass
        await audit(uid, "Отклонена предложка", f"Заявка #{spid}\nПричина: {esc(reason)}")

    elif action == "back":
        bid, spid = int(parts[2]), int(parts[3])
        await query.answer()
        try:
            await query.message.edit_text(
                "📥 <b>Предложка</b>\nВыбери действие:", reply_markup=get_review_kb(bid, spid)
            )
        except TelegramBadRequest:
            pass

    elif action == "accall":
        bid = int(parts[2])
        posts = await db_all(
            DB_SUGG,
            "SELECT id, chat_id, msg_ids, media_type FROM sugg_posts WHERE batch_id=? AND status='pending'",
            (bid,),
        )
        if not posts:
            return await query.answer("Нет постов для принятия!", show_alert=True)
        brow = await db_one(DB_SUGG, "SELECT user_id FROM batches WHERE id=?", (bid,))
        user_id = brow[0] if brow else None
        slots = await allocate_slots(len(posts))

        def insert_all(c):
            for slot, (spid, chat_id, msg_ids_json, media_type) in zip(slots, posts):
                c.execute(
                    """INSERT INTO posts(slot,caption,source,author_id,chat_id,msg_ids,created_at,media_type)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (slot, accepted_caption_raw(c, spid)[0], "sugg", user_id, chat_id, msg_ids_json, now, media_type),
                )
        await db_run(DB_PLAN, insert_all)

        def mark_all(c):
            for (spid, _, _, _) in posts:
                c.execute(
                    "UPDATE sugg_posts SET status='accepted', reviewed_by=?, reviewed_at=? WHERE id=?",
                    (uid, now, spid),
                )
        await db_run(DB_SUGG, mark_all)

        await safe_send(user_id, f"✅ <b>Принято!</b>\nТвои предложения приняты: <b>{len(posts)}</b>.")
        await query.answer("Всё принято!")
        try:
            await query.message.edit_text(
                f"✅ Принято постов: <b>{len(posts)}</b>\n🕒 Последний слот: {fmt_time(slots[-1], uid)}"
            )
        except TelegramBadRequest:
            pass
        await audit(uid, "Приняты все предложки пачки", f"Пачка #{bid}\nКоличество: {len(posts)}")

    elif action == "rejall":
        bid = int(parts[2])
        rows = [[InlineKeyboardButton(text=t, callback_data=f"r:rejall_do:{bid}:{c}")] for c, t in REASONS]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="noop")])
        await query.answer()
        try:
            await query.message.edit_text(
                "❌ <b>Отклонить все</b>\nВыбери причину:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest:
            pass

    elif action == "rejall_do":
        bid = int(parts[2])
        code = parts[3] if len(parts) > 3 else "none"
        reason = REASON_MAP.get(code, "Не прошло общий отбор")
        cnt = (await db_one(DB_SUGG, "SELECT COUNT(*) FROM sugg_posts WHERE batch_id=? AND status='pending'", (bid,)))[0]
        if not cnt:
            return await query.answer("Нет постов для отклонения!", show_alert=True)
        brow = await db_one(DB_SUGG, "SELECT user_id FROM batches WHERE id=?", (bid,))
        await db_exec(
            DB_SUGG,
            """UPDATE sugg_posts SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=?
               WHERE batch_id=? AND status='pending'""",
            (reason, uid, now, bid),
        )
        if brow:
            await safe_send(brow[0], f"❌ <b>Отклонено</b>\nПричина: {esc(reason)}")
        await query.answer("Всё отклонено!")
        try:
            await query.message.edit_text(f"❌ Всё отклонено.\nПричина: {esc(reason)}")
        except TelegramBadRequest:
            pass
        await audit(uid, "Отклонены все предложки пачки", f"Пачка #{bid}\nПричина: {esc(reason)}")

    elif action == "ban":
        bid = int(parts[2])
        rows = [[InlineKeyboardButton(text=t, callback_data=f"r:bando:{bid}:{c}")] for c, t in BAN_REASONS]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="noop")])
        await query.answer()
        try:
            await query.message.edit_text(
                "🚫 <b>Блокировка</b>\nВыбери причину бана:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest:
            pass

    elif action == "bando":
        bid = int(parts[2])
        code = parts[3] if len(parts) > 3 else "other"
        reason = BAN_REASON_MAP.get(code, "Нарушение правил")
        user = await db_one(DB_SUGG, "SELECT user_id, username FROM batches WHERE id=?", (bid,))
        if not user:
            return await query.answer("Пользователь не найден!", show_alert=True)
        user_id, username = user

        def ban_fn(c):
            c.execute(
                """UPDATE sugg_posts SET status='rejected', reject_reason='Пользователь заблокирован',
                   reviewed_by=?, reviewed_at=? WHERE batch_id=? AND status='pending'""",
                (uid, now, bid),
            )
            c.execute("UPDATE batches SET status='banned' WHERE id=?", (bid,))
        await db_run(DB_SUGG, ban_fn)
        await block_user(user_id, username or str(user_id), reason, uid)
        removed = await db_run(
            DB_PLAN,
            lambda c: c.execute(
                "DELETE FROM posts WHERE source='sugg' AND (chat_id=? OR author_id=?)", (user_id, user_id)
            ).rowcount or 0,
        )
        await safe_send(user_id, f"🚫 Вы были заблокированы.\nПричина: {esc(reason)}")
        await query.answer("Забанен!")
        try:
            await query.message.edit_text(f"🚫 Пользователь забанен.\nПричина: {esc(reason)}")
        except TelegramBadRequest:
            pass
        await audit(
            uid,
            "Бан пользователя",
            f"ID: <code>{user_id}</code>\nПричина: {esc(reason)}\nСнято из очереди: {removed}",
        )

    else:
        await query.answer("Неизвестное действие", show_alert=True)


# =============================================================================
# НИЖНЯЯ КЛАВИАТУРА
# =============================================================================
@router.message(F.text.in_({"🏠 Гл. Меню", "🏠 Главное меню", "🏡 Гл. Меню"}))
async def rk_main(message: Message, state: FSMContext):
    if message.from_user.id in ALL_ADMINS:
        await cmd_start(message, state)


@router.message(F.text == "🗂 План")
async def rk_plan(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть контент-план", callback_data="planf:all:0")]]
    )
    await message.answer("🗂 <b>Контент-план</b>\nСмотри, переноси, удаляй, предпросматривай.", reply_markup=kb)


@router.message(F.text == "📊 Статистика")
async def rk_stats(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await message.answer(await build_stats_text(), reply_markup=get_stats_kb())


@router.message(F.text == "ℹ️ Помощь")
async def rk_help(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await cmd_help(message)


@router.message(F.text == "🚫 Черный список 🚫")
async def rk_blacklist(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    text, kb = await get_blacklist_view()
    await message.answer(text, reply_markup=kb)


@router.message(F.text.in_({"Пауза постинга ⏸", "Возобновить постинг ▶️", "Возобновить постинг ↪️"}))
async def rk_pause(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    new = not await get_setting("paused", False)
    await set_setting("paused", new)
    await message.answer(
        f"Постинг {'на паузе ⏸' if new else 'возобновлён ▶️'}!", reply_markup=get_reply_kb(new)
    )
    await audit(message.from_user.id, "Постинг поставлен на паузу" if new else "Постинг возобновлён", "")


@router.message(F.text == "🔽 Скрыть клавиатуру 🔽")
async def rk_hide(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return
    await message.answer("Клавиатура скрыта. Вернуть — /start", reply_markup=ReplyKeyboardRemove())


# =============================================================================
# ПЛАНИРОВЩИК
# =============================================================================
async def publish_post(post):
    pid, cap, chat_id, msg_ids_json, source, author_id, media_type = post
    msg_ids = json.loads(msg_ids_json)
    safe_cap = esc(cap) if cap else None

    if len(msg_ids) > 1:
        await bot.copy_messages(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_ids=msg_ids)
        if safe_cap:
            await bot.send_message(chat_id=CHANNEL_ID, text=safe_cap)
    else:
        await bot.copy_message(
            chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg_ids[0], caption=safe_cap or None
        )

    now = int(time.time())

    def finish(c):
        c.execute("DELETE FROM posts WHERE id=?", (pid,))
        c.execute(
            """INSERT INTO published_history(post_id,published_at,source,author_id,media_type,media_count,caption_short)
               VALUES(?,?,?,?,?,?,?)""",
            (pid, now, source, author_id, media_type or "other", len(msg_ids), (cap or "")[:120]),
        )
    await db_run(DB_PLAN, finish)
    await audit(0, "Пост опубликован", f"ID: <code>{pid}</code>\nТип: {MEDIA_LABEL.get(media_type or 'other')}", notify=NOTIFY_PUBLISH)


async def publisher_loop():
    logger.info("Планировщик запущен")
    while True:
        try:
            if await get_setting("paused", False):
                await asyncio.sleep(10)
                continue

            now = int(time.time())
            if is_quiet_ts(now):
                await asyncio.sleep(60)
                continue

            post = await db_one(
                DB_PLAN,
                """SELECT id, caption, chat_id, msg_ids, source, author_id, media_type
                   FROM posts WHERE slot<=? ORDER BY slot LIMIT 1""",
                (now,),
            )
            if post:
                if is_quiet_ts(now):
                    await asyncio.sleep(60)
                    continue
                try:
                    await publish_post(post)
                except Exception as e:
                    logger.exception("Ошибка публикации поста %s", post[0])
                    retry = shift_out_of_quiet(int(time.time()) + 3600)
                    await db_exec(DB_PLAN, "UPDATE posts SET slot=? WHERE id=?", (retry, post[0]))
                    await audit(
                        0,
                        "⚠️ Ошибка публикации",
                        f"ID: <code>{post[0]}</code>\nПеренесён на {fmt_time(retry, ADMIN_ID)}\n{esc(str(e))[:200]}",
                    )
        except Exception as e:
            logger.error("Сбой планировщика: %s", e)
        await asyncio.sleep(10)


async def maintenance_loop():
    logger.info("Обслуживание запущено")
    while True:
        try:
            now = int(time.time())

            last_sweep = await get_setting("last_sweep", 0)
            if now - last_sweep >= SWEEP_EVERY:
                moved = await reschedule_quiet_posts()
                await set_setting("last_sweep", now)
                if moved:
                    lines = "\n".join(f"• #{pid} → {fmt_time(ns, ADMIN_ID)}" for pid, _, ns in moved[:10])
                    await audit(
                        0,
                        "🌙 Ночные посты перенесены",
                        f"Количество: <b>{len(moved)}</b>\n{lines}",
                    )

            q = await queue_count()
            last_notify = await get_setting("last_low_notify", 0)
            if q <= LOW_QUEUE_THRESHOLD and now - last_notify >= LOW_QUEUE_EVERY:
                await set_setting("last_low_notify", now)
                if q == 0:
                    txt = (
                        "🚨 <b>ОЧЕРЕДЬ ПУСТАЯ</b>\n"
                        "━━━━━━━━━━━━━━━━\n"
                        "Посты закончились, канал стоит.\n"
                        "Закинь новые посты 🙏"
                    )
                else:
                    txt = (
                        "⚠️ <b>ОЧЕРЕДЬ ЗАКАНЧИВАЕТСЯ</b>\n"
                        "━━━━━━━━━━━━━━━━\n"
                        f"Осталось постов: <b>{q}</b>\n"
                        "Пора добавить новые."
                    )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть очередь", callback_data="planf:all:0")]]
                )
                await safe_send(ADMIN_ID, txt, kb)

            last_cleanup = await get_setting("last_cleanup", 0)
            if now - last_cleanup >= CLEANUP_EVERY:
                res = await cleanup_old_data()
                await set_setting("last_cleanup", now)
                if any(res.values()):
                    await audit(
                        0,
                        "🧹 Автоочистка базы",
                        f"Предложка: {res['sugg']}\nЛоги: {res['logs']}\nИстория: {res['hist']}",
                    )

            stale = [k for k, v in ALBUMS.items() if not v.get("msgs")]
            for k in stale:
                ALBUMS.pop(k, None)
        except Exception as e:
            logger.error("Сбой обслуживания: %s", e)
        await asyncio.sleep(60)


# =============================================================================
# ЛОВУШКА СТАРЫХ КНОПОК
# =============================================================================
@router.callback_query()
async def unknown_callback(query: CallbackQuery):
    try:
        await query.answer("🔄 Кнопка устарела. Обнови меню: /start", show_alert=True)
    except Exception:
        pass


# =============================================================================
# ЗАПУСК
# =============================================================================
async def main():
    await init_db()
    moved = await reschedule_quiet_posts()
    if moved:
        logger.info("Ночных постов перенесено при старте: %s", len(moved))
    asyncio.create_task(publisher_loop())
    asyncio.create_task(maintenance_loop())
    logger.info("Бот запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass