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
# ВСТАВЬ СВОЙ ТОКЕН СЮДА
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "8904950437:AAHUQcrqpC7hvMOhFWuu6he8mvLSfBbCTn8"

if TOKEN == "1234567890:FAKE_REPLACE_ME":
    raise RuntimeError("Замени токен в коде: строка TOKEN = ...")

CHANNEL_ID = -1002130614493
ADMIN_ID = 6936783270
VEXX_ID = 8021778299
ALL_ADMINS = {ADMIN_ID, VEXX_ID}

TZ_MSK = ZoneInfo("Europe/Moscow")
TZ_SARATOV = ZoneInfo("Europe/Saratov")
TZ_EKB = ZoneInfo("Asia/Yekaterinburg")

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("TETO_BOT")


# =============================================================================
# УТИЛИТЫ
# =============================================================================
def esc(text):
    return html.escape(text or "", quote=False)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default or {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def _ensure_columns(con, table, required_columns):
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in required_columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            logger.info("Миграция БД: добавлено поле %s.%s", table, name)


def init_db():
    with sqlite3.connect(DB_PLAN) as con:
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
        _ensure_columns(
            con,
            "posts",
            {
                "slot": "INTEGER",
                "caption": "TEXT",
                "source": "TEXT",
                "author_id": "INTEGER",
                "chat_id": "INTEGER",
                "msg_ids": "TEXT",
                "created_at": "INTEGER",
            },
        )
        con.commit()

    with sqlite3.connect(DB_SUGG) as con:
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
        _ensure_columns(
            con,
            "batches",
            {
                "user_id": "INTEGER",
                "username": "TEXT",
                "is_anon": "INTEGER",
                "status": "TEXT",
                "created_at": "INTEGER",
                "last_sent": "INTEGER",
            },
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
                "batch_id": "INTEGER",
                "caption": "TEXT",
                "status": "TEXT",
                "pos": "INTEGER",
                "chat_id": "INTEGER",
                "msg_ids": "TEXT",
                "reject_reason": "TEXT",
                "reviewed_by": "INTEGER",
                "reviewed_at": "INTEGER",
                "created_at": "INTEGER",
            },
        )
        con.commit()

    with sqlite3.connect(DB_MAIN) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                reason TEXT,
                banned_by INTEGER,
                banned_at INTEGER
            )"""
        )
        con.commit()

    if not os.path.exists(STATS_FILE):
        save_json(
            STATS_FILE,
            {"sent": {"posts": 0, "photo": 0, "video": 0, "animation": 0}, "paused": False},
        )

    if not os.path.exists(BLOCK_FILE):
        save_json(BLOCK_FILE, {})

    old_block = load_json(BLOCK_FILE, {})
    if old_block:
        with sqlite3.connect(DB_MAIN) as con:
            for uid_str, uname in old_block.items():
                try:
                    uid = int(uid_str)
                except Exception:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO blacklist (user_id, username, reason, banned_by, banned_at) VALUES (?,?,?,?,?)",
                    (uid, uname, "Импорт из старого списка", 0, int(time.time())),
                )
            con.commit()


def get_stat(key):
    data = load_json(STATS_FILE)
    if key == "paused":
        return data.get(key, False)
    return data.get(key, 0)


def set_stat(key, val):
    data = load_json(STATS_FILE)
    data[key] = val
    save_json(STATS_FILE, data)


def get_tz(uid):
    if uid == VEXX_ID:
        return TZ_EKB
    if uid == ADMIN_ID:
        return TZ_SARATOV
    return TZ_MSK


def fmt_time(ts, uid):
    tz = get_tz(uid)
    try:
        return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M %d.%m.%Y")
    except Exception:
        return str(ts)


def get_day_range(filter_name, uid):
    tz = get_tz(uid)
    now = datetime.now(tz)

    if filter_name == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif filter_name == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif filter_name == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    else:
        return None

    return int(start.timestamp()), int(end.timestamp())


def get_next_slot():
    now = datetime.now(TZ_MSK).replace(minute=0, second=0, microsecond=0)
    next_hour = int((now + timedelta(hours=1)).timestamp())

    with sqlite3.connect(DB_PLAN) as con:
        used = {row[0] for row in con.execute("SELECT slot FROM posts")}

    slot = next_hour
    while slot in used:
        slot += 3600
    return slot


def is_blocked(user_id):
    with sqlite3.connect(DB_MAIN) as con:
        row = con.execute("SELECT 1 FROM blacklist WHERE user_id=?", (user_id,)).fetchone()
        return row is not None


def block_user(user_id, username, reason, banned_by):
    with sqlite3.connect(DB_MAIN) as con:
        con.execute(
            "INSERT OR REPLACE INTO blacklist (user_id, username, reason, banned_by, banned_at) VALUES (?,?,?,?,?)",
            (user_id, username, reason, banned_by, int(time.time())),
        )
        con.commit()


def unblock_user(user_id):
    with sqlite3.connect(DB_MAIN) as con:
        con.execute("DELETE FROM blacklist WHERE user_id=?", (user_id,))
        con.commit()


def history_status(s_status, b_status):
    if b_status == "banned":
        return "🚫", "Забанен"
    if s_status == "accepted":
        return "✅", "Принято"
    if s_status == "rejected":
        return "❌", "Отклонено"
    return "⏳", "Ожидает"


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

    if len(FLOOD_TRACKER[uid]) >= 5:
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
    kb = [
        [
            InlineKeyboardButton(text="🗂 Очередь", callback_data="planf:all:0"),
            InlineKeyboardButton(text="📜 История", callback_data="hist:0"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="m:stats"),
            InlineKeyboardButton(text="🛡 ЧС", callback_data="m:blacklist"),
        ],
        [
            InlineKeyboardButton(text=("▶️ Возобновить" if paused else "⏸ Пауза"), callback_data="m:pause"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="m:main"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Помощь", callback_data="m:help"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_preview_kb():
    kb = [
        [
            InlineKeyboardButton(text="✅ Отложить", callback_data="a:confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="a:cancel"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_user_anon_kb():
    kb = [
        [InlineKeyboardButton(text="👻 Анонимно", callback_data="u:anon")],
        [InlineKeyboardButton(text="👤 Не анонимно", callback_data="u:nonanon")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="u:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_review_kb(bid, spid):
    kb = [
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"r:acc:{spid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"r:rej:{spid}"),
        ],
        [
            InlineKeyboardButton(text="✅ Принять все", callback_data=f"r:accall:{bid}"),
            InlineKeyboardButton(text="❌ Отклонить все", callback_data=f"r:rejall:{bid}"),
        ],
        [
            InlineKeyboardButton(text="🚫 Забанить", callback_data=f"r:ban:{bid}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_back_kb(cb="m:main"):
    kb = [[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def build_main_text(uid):
    paused = get_stat("paused")

    with sqlite3.connect(DB_PLAN) as con:
        queue_count = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    st = load_json(STATS_FILE).get("sent", {})
    status = "⏸ Пауза" if paused else "🟢 Работает"
    tz_name = "Екатеринбург" if uid == VEXX_ID else ("Саратов" if uid == ADMIN_ID else "Москва")

    return (
        "🏠 <b>TETO POSTIT</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Очередь:</b> {queue_count}\n"
        f"✅ <b>Опубликовано:</b> {st.get('posts', 0)}\n"
        f"⚙️ <b>Статус:</b> {status}\n"
        f"🌍 <b>Часовой пояс:</b> {tz_name}\n"
        "━━━━━━━━━━━━━━━━\n"
        "Выбери действие ниже 👇"
    )


def build_stats_text():
    with sqlite3.connect(DB_PLAN) as con:
        queue_count = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    st = load_json(STATS_FILE).get("sent", {})

    return (
        "📊 <b>Статистика</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"✅ Опубликовано: <b>{st.get('posts', 0)}</b>\n"
        f"📦 В очереди: <b>{queue_count}</b>\n\n"
        "<i>Разбивка по типам медиа пока не ведётся достоверно.</i>"
    )


def get_blacklist_view():
    with sqlite3.connect(DB_MAIN) as con:
        rows = con.execute(
            "SELECT user_id, username, reason, banned_by, banned_at FROM blacklist ORDER BY banned_at DESC"
        ).fetchall()

    text = (
        "🛡 <b>Чёрный список</b>\n"
        "━━━━━━━━━━━━━━━━\n"
    )
    kb = []

    if not rows:
        text += "✨ Пусто."
        kb.append([InlineKeyboardButton(text="✨ Пусто", callback_data="noop")])
    else:
        text += f"Всего: <b>{len(rows)}</b>\n\n"
        lines = []

        for user_id, username, reason, banned_by, banned_at in rows[:10]:
            try:
                banned_dt = datetime.fromtimestamp(banned_at, TZ_MSK).strftime("%d.%m.%Y %H:%M") if banned_at else "—"
            except Exception:
                banned_dt = "—"

            uname = username or str(user_id)
            reason_short = (reason or "Не указана")[:50]

            lines.append(
                f"👤 <b>{esc(uname)}</b>\n"
                f"   ID: <code>{user_id}</code>\n"
                f"   Причина: {esc(reason_short)}\n"
                f"   Дата: {banned_dt}"
            )

            kb.append(
                [
                    InlineKeyboardButton(
                        text=f"🔓 Разбанить {uname[:25]}",
                        callback_data=f"unblock:{user_id}",
                    )
                ]
            )

        if len(rows) > 10:
            lines.append(f"<i>И ещё {len(rows) - 10}...</i>")

        text += "\n\n".join(lines)

    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


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


admin_albums = {}
user_albums = {}
admin_batches = {}
admin_preview_tasks = {}


# =============================================================================
# УВЕДОМЛЕНИЯ
# =============================================================================
async def notify_user_accept(user_id, slot=None):
    if not user_id:
        return

    try:
        if slot:
            await bot.send_message(
                user_id,
                "✅ <b>Принято!</b>\n"
                f"🕒 Твоё предложение запланировано на <b>{fmt_time(slot, user_id)}</b>.",
            )
        else:
            await bot.send_message(
                user_id,
                "✅ <b>Принято!</b>\nТвоё предложение появится в канале.",
            )
    except Exception:
        logger.exception("Не удалось отправить уведомление пользователю %s", user_id)


async def notify_user_reject(user_id, reason):
    if not user_id:
        return

    try:
        await bot.send_message(
            user_id,
            "❌ <b>Отклонено</b>\n"
            f"Причина: {esc(reason)}",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление пользователю %s", user_id)


async def notify_user_accepted_all(user_id, count):
    if not user_id:
        return

    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Принято!</b>\nТвои предложения приняты. Количество: <b>{count}</b>.",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление пользователю %s", user_id)


async def notify_user_rejected_all(user_id, reason):
    if not user_id:
        return

    try:
        await bot.send_message(
            user_id,
            "❌ <b>Отклонено</b>\n"
            f"Причина: {esc(reason)}",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление пользователю %s", user_id)


def accepted_caption_raw(con, post_id):
    row = con.execute(
        """
        SELECT s.caption, b.username, b.user_id, b.is_anon
        FROM sugg_posts s
        JOIN batches b ON b.id = s.batch_id
        WHERE s.id=?
        """,
        (post_id,),
    ).fetchone()

    if not row:
        return "Предложил - неизвестный пользователь"

    caption, username, user_id, is_anon = row

    if is_anon:
        attribution = "Предложил - Анон."
    else:
        attribution = f"Предложил - @{username}" if username else f"Предложил - {user_id}"

    return attribution + ("\n\n" + caption if caption else "")


# =============================================================================
# СТАРТ
# =============================================================================
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    if uid in ALL_ADMINS:
        paused = get_stat("paused")
        await message.answer(build_main_text(uid), reply_markup=get_reply_kb(paused))
        await message.answer("⬇️ Меню действий:", reply_markup=get_main_inline(paused))
        return

    if is_blocked(uid):
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
        "• Отправь фото/видео/гифку — поставлю в очередь.\n"
        "• /start или /menu — главное меню.\n"
        "• В разделе «Очередь» можно переносить и удалять посты."
    )


# =============================================================================
# ПРИЁМ МЕДИА
# =============================================================================
async def send_preview_after_delay(uid, state):
    try:
        await asyncio.sleep(3.0)
        batch = admin_batches.get(uid, [])
        if not batch:
            return

        total_posts = len(batch)
        base_slot = get_next_slot()
        last_slot = base_slot + ((total_posts - 1) * 3600)
        paused = get_stat("paused")

        text = (
            "📦 <b>Предпросмотр пачки</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Постов: <b>{total_posts}</b>\n"
            f"Последний слот: <b>{fmt_time(last_slot, uid)}</b>\n\n"
            "Отложить?"
        )

        if paused:
            text += "\n\n⏰ <b>Постинг сейчас на паузе.</b>"

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

    if uid in ALL_ADMINS:
        if message.media_group_id:
            gid = message.media_group_id
            admin_albums.setdefault(gid, []).append(message)
            await asyncio.sleep(1.5)

            if gid not in admin_albums:
                return

            msgs = sorted(admin_albums.pop(gid), key=lambda m: m.message_id)
            msg_ids = [m.message_id for m in msgs]
            cap = msgs[0].caption or ""

            admin_batches.setdefault(uid, []).append(
                {"chat_id": uid, "msg_ids": msg_ids, "cap": cap}
            )
        else:
            admin_batches.setdefault(uid, []).append(
                {"chat_id": uid, "msg_ids": [message.message_id], "cap": message.caption or ""}
            )

        if uid in admin_preview_tasks:
            admin_preview_tasks[uid].cancel()

        admin_preview_tasks[uid] = asyncio.create_task(send_preview_after_delay(uid, state))
        return

    if is_blocked(uid):
        return

    if message.media_group_id:
        gid = "u_" + message.media_group_id
        user_albums.setdefault(gid, []).append(message)
        await asyncio.sleep(1.5)

        if gid not in user_albums:
            return

        msgs = sorted(user_albums.pop(gid), key=lambda m: m.message_id)
        msg_ids = [m.message_id for m in msgs]
        cap = msgs[0].caption or ""
    else:
        msg_ids = [message.message_id]
        cap = message.caption or ""

    await state.update_data(user_chat_id=uid, user_msg_ids=msg_ids, user_cap=cap)
    await message.answer(
        "📨 <b>Как предложить?</b>\n\n"
        "👻 Анонимно — без имени\n"
        "👤 Не анонимно — с твоим именем",
        reply_markup=get_user_anon_kb(),
    )
    await state.set_state(UserStates.choosing_anon)


# =============================================================================
# ПОДТВЕРЖДЕНИЕ АДМИНА
# =============================================================================
@router.callback_query(F.data.startswith("a:"))
async def admin_preview_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    if query.data == "a:cancel":
        admin_batches.pop(uid, None)
        await state.clear()
        await query.answer("Отменено")
        try:
            await query.message.edit_text("❌ Отменено.")
        except TelegramBadRequest:
            pass
        return

    if query.data != "a:confirm":
        return await query.answer("Неизвестное действие", show_alert=True)

    batch = admin_batches.pop(uid, None)
    if not batch:
        return await query.answer("Пусто!", show_alert=True)

    base_slot = get_next_slot()
    now = int(time.time())

    with sqlite3.connect(DB_PLAN) as con:
        for index, item in enumerate(batch):
            current_slot = base_slot + (index * 3600)
            con.execute(
                """INSERT INTO posts (slot, caption, source, author_id, chat_id, msg_ids, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    current_slot,
                    item["cap"],
                    "admin",
                    uid,
                    item["chat_id"],
                    json.dumps(item["msg_ids"]),
                    now,
                ),
            )
        con.commit()

    await state.clear()
    last_slot = base_slot + ((len(batch) - 1) * 3600)

    await query.answer("Готово!")
    try:
        await query.message.edit_text(
            "✅ <b>Готово!</b>\n"
            f"📦 Отложено: <b>{len(batch)}</b>\n"
            f"🕒 Последний: <b>{fmt_time(last_slot, uid)}</b>"
        )
    except TelegramBadRequest:
        pass


# =============================================================================
# ГЛАВНОЕ МЕНЮ
# =============================================================================
@router.callback_query(F.data.startswith("m:"))
async def admin_menu_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    action = query.data.split(":")[1]
    await query.answer()

    if action == "main":
        paused = get_stat("paused")
        try:
            await query.message.edit_text(build_main_text(uid), reply_markup=get_main_inline(paused))
        except TelegramBadRequest:
            pass

    elif action == "pause":
        new = not get_stat("paused")
        set_stat("paused", new)
        try:
            await query.message.edit_text(build_main_text(uid), reply_markup=get_main_inline(new))
        except TelegramBadRequest:
            pass

    elif action == "stats":
        try:
            await query.message.edit_text(build_stats_text(), reply_markup=get_back_kb())
        except TelegramBadRequest:
            pass

    elif action == "help":
        text = (
            "ℹ️ <b>Помощь</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            "• Отправь фото/видео/гифку — поставлю в очередь.\n"
            "• /start или /menu — главное меню.\n"
            "• В разделе «Очередь» можно переносить и удалять посты."
        )
        try:
            await query.message.edit_text(text, reply_markup=get_back_kb())
        except TelegramBadRequest:
            pass

    elif action == "blacklist":
        text, kb = get_blacklist_view()
        try:
            await query.message.edit_text(text, reply_markup=kb)
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
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    filter_name = parts[1] if len(parts) > 1 else "all"

    if filter_name not in {"today", "tomorrow", "week", "all"}:
        filter_name = "all"

    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0

    if page < 0:
        page = 0

    per_page = 5
    offset = page * per_page
    date_range = get_day_range(filter_name, uid) if filter_name in {"today", "tomorrow", "week"} else None

    with sqlite3.connect(DB_PLAN) as con:
        if date_range:
            total = con.execute(
                "SELECT COUNT(*) FROM posts WHERE slot >= ? AND slot < ?",
                date_range,
            ).fetchone()[0]

            posts = con.execute(
                """SELECT id, slot, caption FROM posts
                   WHERE slot >= ? AND slot < ?
                   ORDER BY slot
                   LIMIT ? OFFSET ?""",
                (*date_range, per_page, offset),
            ).fetchall()
        else:
            total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            posts = con.execute(
                "SELECT id, slot, caption FROM posts ORDER BY slot LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)

    mode_names = {
        "today": "📅 Сегодня",
        "tomorrow": "🌇 Завтра",
        "week": "🗓 Неделя",
        "all": "♾ Все",
    }

    text = (
        "🗂 <b>Контент-план</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Режим: {mode_names.get(filter_name, '♾ Все')}\n"
        f"Всего: <b>{total}</b>\n"
    )

    rows = []

    if not posts:
        text += "\n✨ Очередь пуста."
        rows.append([InlineKeyboardButton(text="✨ Очередь пуста", callback_data="noop")])
    else:
        for pid, slot, cap in posts:
            cap_short = esc((cap or "Без подписи"))[:25]
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🕒 {fmt_time(slot, uid)} • {cap_short}",
                        callback_data=f"view:{pid}:{page}:{filter_name}",
                    )
                ]
            )

    def filter_btn(code, label):
        active = "✅ " if filter_name == code else ""
        return InlineKeyboardButton(text=f"{active}{label}", callback_data=f"planf:{code}:0")

    rows.append([filter_btn("today", "📅 Сегодня"), filter_btn("tomorrow", "🌇 Завтра")])
    rows.append([filter_btn("week", "🗓 Неделя"), filter_btn("all", "♾ Все")])

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
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    try:
        page = int(query.data.split(":")[1])
    except Exception:
        page = 0

    query.data = f"planf:all:{page}"
    await content_plan_filter_cb(query)


@router.callback_query(F.data.startswith("view:"))
async def view_post_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])

    try:
        page = int(parts[2])
    except Exception:
        page = 0

    filter_name = parts[3] if len(parts) > 3 else "all"
    if filter_name not in {"today", "tomorrow", "week", "all"}:
        filter_name = "all"

    with sqlite3.connect(DB_PLAN) as con:
        row = con.execute("SELECT slot, caption, chat_id, msg_ids FROM posts WHERE id=?", (pid,)).fetchone()

    if not row:
        await query.answer("Пост удалён!", show_alert=True)
        query.data = f"planf:{filter_name}:{page}"
        return await content_plan_filter_cb(query)

    slot, cap, chat_id, msg_ids_json = row

    text = (
        "📦 <b>Пост в очереди</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🕒 Время: <b>{fmt_time(slot, uid)}</b>\n"
        f"🆔 ID: <code>{pid}</code>\n"
        f"📝 Подпись: {esc(cap or '—')[:300]}"
    )

    kb = [
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_del:{pid}:{page}:{filter_name}")],
        [
            InlineKeyboardButton(text="🕒 Следующий слот", callback_data=f"move_next:{pid}:{page}:{filter_name}"),
            InlineKeyboardButton(text="📅 Указать дату", callback_data=f"move_date:{pid}:{page}:{filter_name}"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"planf:{filter_name}:{page}")],
    ]

    await query.answer()

    msg_ids = json.loads(msg_ids_json)

    try:
        if len(msg_ids) > 1:
            await bot.copy_messages(chat_id=uid, from_chat_id=chat_id, message_ids=msg_ids)
            await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_ids[0])
            await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except Exception as e:
        try:
            await query.message.edit_text(
                f"{text}\n\n⚠️ Медиа не найдено: {esc(str(e))}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            )
        except TelegramBadRequest:
            await bot.send_message(
                uid,
                f"{text}\n\n⚠️ Медиа не найдено: {esc(str(e))}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            )


# =============================================================================
# УДАЛЕНИЕ
# =============================================================================
@router.callback_query(F.data.startswith("confirm_del:"))
async def confirm_del_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    page = int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    with sqlite3.connect(DB_PLAN) as con:
        row = con.execute("SELECT slot FROM posts WHERE id=?", (pid,)).fetchone()

    if not row:
        await query.answer("Пост удалён!", show_alert=True)
        query.data = f"planf:{filter_name}:{page}"
        return await content_plan_filter_cb(query)

    slot = row[0]

    text = (
        "🗑 <b>Удаление поста</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Удалить пост на <b>{fmt_time(slot, uid)}</b>?\n"
        "Это действие нельзя отменить."
    )

    kb = [
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_yes:{pid}:{page}:{filter_name}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"del_no:{pid}:{page}:{filter_name}"),
        ]
    ]

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    except TelegramBadRequest:
        await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("del_yes:"))
async def del_yes_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    page = int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    with sqlite3.connect(DB_PLAN) as con:
        con.execute("DELETE FROM posts WHERE id=?", (pid,))
        con.commit()

    await query.answer("Пост удалён!")
    query.data = f"planf:{filter_name}:{page}"
    await content_plan_filter_cb(query)


@router.callback_query(F.data.startswith("del_no:"))
async def del_no_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
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

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    page = int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    new_slot = get_next_slot()

    with sqlite3.connect(DB_PLAN) as con:
        con.execute("UPDATE posts SET slot=? WHERE id=?", (new_slot, pid))
        con.commit()

    await query.answer(f"Перенесён на {fmt_time(new_slot, uid)}")
    query.data = f"planf:{filter_name}:{page}"
    await content_plan_filter_cb(query)


@router.callback_query(F.data.startswith("move_date:"))
async def move_date_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    pid = int(parts[1])
    page = int(parts[2])
    filter_name = parts[3] if len(parts) > 3 else "all"

    await state.set_state(AdminStates.waiting_move)
    await state.update_data(move_pid=pid, move_page=page, move_filter=filter_name)

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="move_cancel")]]
    )

    text = (
        "📅 <b>Перенос поста</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "Отправь дату и время сообщением:\n"
        "<code>DD.MM.YYYY HH:MM</code>\n\n"
        "Пример: <code>25.11.2026 18:30</code>\n\n"
        "Если передумал — жми Отмена."
    )

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=cancel_kb)
    except TelegramBadRequest:
        await bot.send_message(uid, text, reply_markup=cancel_kb)


@router.callback_query(F.data == "move_cancel")
async def move_cancel_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    data = await state.get_data()
    page = data.get("move_page", 0)
    filter_name = data.get("move_filter", "all")

    await state.clear()
    await query.answer("Отменено")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть план", callback_data=f"planf:{filter_name}:{page}")]]
    )

    try:
        await query.message.edit_text("❌ Перенос отменён.", reply_markup=kb)
    except TelegramBadRequest:
        await bot.send_message(uid, "❌ Перенос отменён.", reply_markup=kb)


@router.message(AdminStates.waiting_move, F.text)
async def process_move_date(message: Message, state: FSMContext):
    uid = message.from_user.id

    if uid not in ALL_ADMINS:
        return

    if message.text.lower().strip() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        dt = dt.replace(tzinfo=get_tz(uid))
        slot = int(dt.timestamp())
    except ValueError:
        await message.answer(
            "⚠️ Неверный формат.\n"
            "Нужно: <code>DD.MM.YYYY HH:MM</code>\n"
            "Пример: <code>25.11.2026 18:30</code>"
        )
        return

    if slot <= int(time.time()):
        await message.answer("⚠️ Время уже прошло. Укажи будущее время.")
        return

    data = await state.get_data()
    pid = data.get("move_pid")
    page = data.get("move_page", 0)
    filter_name = data.get("move_filter", "all")

    if not pid:
        await state.clear()
        await message.answer("⚠️ Не нашёл пост для переноса.")
        return

    with sqlite3.connect(DB_PLAN) as con:
        con.execute("UPDATE posts SET slot=? WHERE id=?", (slot, pid))
        con.commit()

    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗂 Открыть план", callback_data=f"planf:{filter_name}:{page}")]]
    )

    await message.answer(
        "✅ <b>Пост перенесён!</b>\n"
        f"🕒 Новое время: <b>{fmt_time(slot, uid)}</b>",
        reply_markup=kb,
    )


# =============================================================================
# ИСТОРИЯ
# =============================================================================
@router.callback_query(F.data.startswith("hist:"))
async def history_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    try:
        page = int(query.data.split(":")[1])
    except Exception:
        page = 0

    if page < 0:
        page = 0

    per_page = 5
    offset = page * per_page

    with sqlite3.connect(DB_SUGG) as con:
        total = con.execute("SELECT COUNT(*) FROM sugg_posts").fetchone()[0]
        rows = con.execute(
            """
            SELECT
                s.id,
                s.status,
                s.caption,
                s.created_at,
                s.reject_reason,
                s.reviewed_at,
                b.username,
                b.user_id,
                b.is_anon,
                b.status
            FROM sugg_posts s
            LEFT JOIN batches b ON b.id = s.batch_id
            ORDER BY s.id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)

    text = (
        "📜 <b>История предложки</b>\n"
        "━━━━━━━━━━━━━━━━\n"
    )

    if not rows:
        text += "✨ История пуста."
    else:
        lines = []

        for sid, s_status, caption, created_at, reject_reason, reviewed_at, username, user_id, is_anon, b_status in rows:
            emoji, status_text = history_status(s_status, b_status)
            ts = created_at or reviewed_at or 0

            try:
                date = datetime.fromtimestamp(ts, TZ_MSK).strftime("%H:%M %d.%m.%Y") if ts else "—"
            except Exception:
                date = "—"

            author = "👻 Аноним" if is_anon else (f"👤 @{esc(username)}" if username else f"👤 ID {user_id}")

            line = (
                f"{emoji} <b>#{sid}</b> • {date}\n"
                f"Автор: {author}\n"
                f"Статус: {status_text}\n"
                f"Текст: {esc(caption or 'Без подписи')[:100]}"
            )

            if s_status == "rejected" and reject_reason:
                line += f"\nПричина: {esc(reject_reason)}"

            lines.append(line)

        text += "\n\n".join(lines)

    rows_kb = []

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⏪", callback_data="hist:0"))
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"hist:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"hist:{page + 1}"))
        nav.append(InlineKeyboardButton(text="⏩", callback_data=f"hist:{pages - 1}"))

    if nav:
        rows_kb.append(nav)

    rows_kb.append([InlineKeyboardButton(text=f"📄 {page + 1}/{pages}", callback_data="noop")])
    rows_kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m:main")])

    await query.answer()
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))
    except TelegramBadRequest:
        pass


# =============================================================================
# ЧЁРНЫЙ СПИСОК
# =============================================================================
@router.callback_query(F.data.startswith("unblock:"))
async def unblock_cb(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in ALL_ADMINS:
        return await query.answer("Нет доступа!", show_alert=True)

    if check_flood(uid):
        return await query.answer("FloodWait 5 second.", show_alert=True)

    try:
        user_id = int(query.data.split(":")[1])
    except Exception:
        return await query.answer("Некорректный ID", show_alert=True)

    unblock_user(user_id)
    await query.answer(f"Разблокирован {user_id}!")

    query.data = "m:blacklist"
    await admin_menu_cb(query)


@router.callback_query(F.data == "noop")
async def noop_cb(query: CallbackQuery):
    await query.answer()


# =============================================================================
# ПРЕДЛОЖКА ПОЛЬЗОВАТЕЛЯ
# =============================================================================
@router.callback_query(F.data.startswith("u:"))
async def user_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id

    if is_blocked(uid):
        return await query.answer("Заблокировано", show_alert=True)

    parts = query.data.split(":")
    if len(parts) < 2:
        return

    action = parts[1]

    if action == "cancel":
        await state.clear()
        try:
            await query.message.edit_text("❌ Отменено.")
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    chat_id = data.get("user_chat_id")
    msg_ids = data.get("user_msg_ids", [])
    cap = data.get("user_cap", "")

    if not msg_ids:
        return await query.answer("Пусто!", show_alert=True)

    if action in {"anon", "nonanon"}:
        await state.update_data(user_anon=(action == "anon"))

        kb = [
            [InlineKeyboardButton(text="✅ Подтверждаю", callback_data="u:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="u:cancel")],
        ]

        label = "анонимную" if action == "anon" else "не анонимную"
        try:
            await query.message.edit_text(
                f"📨 Подтверждаете <b>{label}</b> отправку?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            )
        except TelegramBadRequest:
            pass

        await state.set_state(UserStates.confirming)
        return

    if action == "confirm":
        if await state.get_state() != UserStates.confirming.state:
            return await query.answer("Эта отправка уже обработана или устарела.", show_alert=True)

        is_anon = data.get("user_anon", True)

        if not chat_id or not msg_ids:
            return await query.answer("Не нашёл исходное медиа. Отправь его ещё раз.", show_alert=True)

        now = int(time.time())

        try:
            with sqlite3.connect(DB_SUGG) as con:
                cur = con.execute(
                    """INSERT INTO batches (user_id, username, is_anon, status, created_at, last_sent)
                       VALUES (?,?,?,?,?,?)""",
                    (uid, query.from_user.username, int(is_anon), "pending", now, now),
                )
                bid = cur.lastrowid

                cur = con.execute(
                    """INSERT INTO sugg_posts (batch_id, caption, status, pos, chat_id, msg_ids, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (bid, cap, "pending", 0, chat_id, json.dumps(msg_ids), now),
                )
                spid = cur.lastrowid
                con.commit()
        except sqlite3.Error:
            logger.exception("Ошибка сохранения предложки пользователя %s", uid)
            return await query.answer("Не удалось сохранить предложку. Сообщи администратору.", show_alert=True)

        await query.answer("Отправлено на рассмотрение!")
        await state.clear()

        try:
            await query.message.edit_text("✅ Отправлено админам на рассмотрение.\nСпасибо! 🎉")
        except TelegramBadRequest:
            pass

        for admin in ALL_ADMINS:
            try:
                if is_anon:
                    author = "👻 Анонимно"
                else:
                    author = f"👤 @{esc(query.from_user.username)}" if query.from_user.username else f"👤 ID {uid}"

                full_cap = (
                    "📥 <b>Новая предложка</b>\n"
                    "━━━━━━━━━━━━━━━━\n"
                    f"Автор: {author}\n"
                    f"Пачка: <code>{bid}</code>\n"
                )

                if cap:
                    full_cap += f"\n💬 Комментарий:\n{esc(cap)}"

                markup = get_review_kb(bid, spid)

                if len(msg_ids) > 1:
                    await bot.copy_messages(chat_id=admin, from_chat_id=chat_id, message_ids=msg_ids)
                    await bot.send_message(admin, full_cap, reply_markup=markup)
                else:
                    await bot.copy_message(chat_id=admin, from_chat_id=chat_id, message_id=msg_ids[0])
                    await bot.send_message(admin, full_cap, reply_markup=markup)
            except Exception as e:
                logger.error("Не смог отправить предложку: %s", e)

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
        return await query.answer("FloodWait 5 second.", show_alert=True)

    parts = query.data.split(":")
    if len(parts) < 3:
        return await query.answer("Некорректные данные", show_alert=True)

    action = parts[1]
    now = int(time.time())

    if action == "acc":
        spid = int(parts[2])

        with sqlite3.connect(DB_SUGG) as con:
            row = con.execute("SELECT batch_id, status FROM sugg_posts WHERE id=?", (spid,)).fetchone()
            if not row or row[1] != "pending":
                return await query.answer("Уже рассмотрено!", show_alert=True)

            bid = row[0]
            user_row = con.execute("SELECT user_id FROM batches WHERE id=?", (bid,)).fetchone()
            user_id = user_row[0] if user_row else None

            con.execute(
                "UPDATE sugg_posts SET status='accepted', reviewed_by=?, reviewed_at=? WHERE id=?",
                (uid, now, spid),
            )
            con.commit()

            cap = accepted_caption_raw(con, spid)
            media_row = con.execute("SELECT chat_id, msg_ids FROM sugg_posts WHERE id=?", (spid,)).fetchone()

        if not media_row:
            return await query.answer("Ошибка: медиа не найдено", show_alert=True)

        chat_id, msg_ids_json = media_row
        slot = get_next_slot()

        with sqlite3.connect(DB_PLAN) as pcon:
            pcon.execute(
                """INSERT INTO posts (slot, caption, source, author_id, chat_id, msg_ids, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (slot, cap, "sugg", user_id, chat_id, msg_ids_json, now),
            )
            pcon.commit()

        await notify_user_accept(user_id, slot)
        await query.answer("Принято!")

        try:
            await query.message.edit_text(f"✅ Принято!\n🕒 Слот: {fmt_time(slot, uid)}")
        except TelegramBadRequest:
            pass

    elif action == "rej":
        spid = int(parts[2])

        with sqlite3.connect(DB_SUGG) as con:
            row = con.execute("SELECT batch_id, status FROM sugg_posts WHERE id=?", (spid,)).fetchone()

        if not row or row[1] != "pending":
            return await query.answer("Уже рассмотрено!", show_alert=True)

        bid = row[0]

        rows = []
        for code, text in REASONS:
            rows.append([InlineKeyboardButton(text=text, callback_data=f"r:rejdo:{spid}:{code}")])

        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"r:back:{bid}:{spid}")])

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

        with sqlite3.connect(DB_SUGG) as con:
            row = con.execute(
                """
                SELECT s.batch_id, s.status, b.user_id
                FROM sugg_posts s
                JOIN batches b ON b.id = s.batch_id
                WHERE s.id=?
                """,
                (spid,),
            ).fetchone()

            if not row or row[1] != "pending":
                return await query.answer("Уже рассмотрено!", show_alert=True)

            user_id = row[2]

            con.execute(
                """UPDATE sugg_posts
                   SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=?
                   WHERE id=?""",
                (reason, uid, now, spid),
            )
            con.commit()

        await notify_user_reject(user_id, reason)
        await query.answer("Отклонено!")

        try:
            await query.message.edit_text(f"❌ Отклонено.\nПричина: {esc(reason)}")
        except TelegramBadRequest:
            pass

    elif action == "back":
        bid = int(parts[2])
        spid = int(parts[3])

        await query.answer()
        try:
            await query.message.edit_text(
                "📥 <b>Предложка</b>\nВыбери действие:",
                reply_markup=get_review_kb(bid, spid),
            )
        except TelegramBadRequest:
            pass

    elif action == "accall":
        bid = int(parts[2])

        with sqlite3.connect(DB_SUGG) as con:
            batch_row = con.execute("SELECT user_id FROM batches WHERE id=?", (bid,)).fetchone()
            user_id = batch_row[0] if batch_row else None

            posts = con.execute(
                "SELECT id FROM sugg_posts WHERE batch_id=? AND status='pending'",
                (bid,),
            ).fetchall()

            if not posts:
                return await query.answer("Нет постов для принятия!", show_alert=True)

            count = 0
            last_slot = None

            for (spid,) in posts:
                cap = accepted_caption_raw(con, spid)
                media_row = con.execute("SELECT chat_id, msg_ids FROM sugg_posts WHERE id=?", (spid,)).fetchone()
                if not media_row:
                    continue

                chat_id, msg_ids_json = media_row
                slot = get_next_slot()
                last_slot = slot

                with sqlite3.connect(DB_PLAN) as pcon:
                    pcon.execute(
                        """INSERT INTO posts (slot, caption, source, author_id, chat_id, msg_ids, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (slot, cap, "sugg", user_id, chat_id, msg_ids_json, now),
                    )
                    pcon.commit()

                con.execute(
                    "UPDATE sugg_posts SET status='accepted', reviewed_by=?, reviewed_at=? WHERE id=?",
                    (uid, now, spid),
                )
                count += 1

            con.commit()

        await notify_user_accepted_all(user_id, count)
        await query.answer("Всё принято!")

        try:
            text = f"✅ Принято постов: <b>{count}</b>."
            if last_slot:
                text += f"\n🕒 Последний слот: {fmt_time(last_slot, uid)}"
            await query.message.edit_text(text)
        except TelegramBadRequest:
            pass

    elif action == "rejall":
        bid = int(parts[2])

        with sqlite3.connect(DB_SUGG) as con:
            batch_row = con.execute("SELECT id FROM batches WHERE id=?", (bid,)).fetchone()

        if not batch_row:
            return await query.answer("Пачка не найдена!", show_alert=True)

        rows = []
        for code, text in REASONS:
            rows.append([InlineKeyboardButton(text=text, callback_data=f"r:rejall_do:{bid}:{code}")])

        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="noop")])

        await query.answer()
        try:
            await query.message.edit_text(
                "❌ <b>Отклонить все</b>\nВыбери причину для всех постов:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramBadRequest:
            pass

    elif action == "rejall_do":
        bid = int(parts[2])
        code = parts[3] if len(parts) > 3 else "none"
        reason = REASON_MAP.get(code, "Не прошло общий отбор")

        with sqlite3.connect(DB_SUGG) as con:
            batch_row = con.execute("SELECT user_id FROM batches WHERE id=?", (bid,)).fetchone()
            user_id = batch_row[0] if batch_row else None

            pending_count = con.execute(
                "SELECT COUNT(*) FROM sugg_posts WHERE batch_id=? AND status='pending'",
                (bid,),
            ).fetchone()[0]

            if pending_count == 0:
                return await query.answer("Нет постов для отклонения!", show_alert=True)

            con.execute(
                """UPDATE sugg_posts
                   SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=?
                   WHERE batch_id=? AND status='pending'""",
                (reason, uid, now, bid),
            )
            con.commit()

        await notify_user_rejected_all(user_id, reason)
        await query.answer("Всё отклонено!")

        try:
            await query.message.edit_text(f"❌ Всё отклонено.\nПричина: {esc(reason)}")
        except TelegramBadRequest:
            pass

    elif action == "ban":
        bid = int(parts[2])

        with sqlite3.connect(DB_SUGG) as con:
            user = con.execute("SELECT user_id, username FROM batches WHERE id=?", (bid,)).fetchone()
            if not user:
                return await query.answer("Пользователь не найден!", show_alert=True)

            user_id, username = user

            con.execute(
                """UPDATE sugg_posts
                   SET status='rejected', reject_reason='Пользователь заблокирован', reviewed_by=?, reviewed_at=?
                   WHERE batch_id=? AND status='pending'""",
                (uid, now, bid),
            )
            con.execute("UPDATE batches SET status='banned' WHERE id=?", (bid,))
            con.commit()

        block_user(user_id, username or str(user_id), "Нарушение правил/спам", uid)

        with sqlite3.connect(DB_PLAN) as pcon:
            pcon.execute(
                "DELETE FROM posts WHERE source='sugg' AND (chat_id=? OR author_id=?)",
                (user_id, user_id),
            )
            pcon.commit()

        try:
            await bot.send_message(user_id, "🚫 Вы были заблокированы.")
        except Exception:
            pass

        await query.answer("Забанен!")

        try:
            await query.message.edit_text("🚫 Пользователь забанен.")
        except TelegramBadRequest:
            pass

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

    kb = [[InlineKeyboardButton(text="🗂 Открыть контент-план", callback_data="planf:all:0")]]
    await message.answer(
        "🗂 <b>Контент-план</b>\n"
        "Открой очередь, чтобы посмотреть, перенести или удалить запланированный пост.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.message(F.text == "📊 Статистика")
async def rk_stats(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return

    await message.answer(build_stats_text(), reply_markup=get_back_kb())


@router.message(F.text == "ℹ️ Помощь")
async def rk_help(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return

    await message.answer(
        "ℹ️ <b>Помощь</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "• Отправь фото/видео/гифку — поставлю в очередь.\n"
        "• /start или /menu — главное меню.\n"
        "• В разделе «Очередь» можно переносить и удалять посты.",
        reply_markup=get_back_kb(),
    )


@router.message(F.text == "🚫 Черный список 🚫")
async def rk_blacklist(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return

    text, kb = get_blacklist_view()
    await message.answer(text, reply_markup=kb)


@router.message(F.text.in_({"Пауза постинга ⏸", "Возобновить постинг ▶️", "Возобновить постинг ↪️"}))
async def rk_pause(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return

    new = not get_stat("paused")
    set_stat("paused", new)

    await message.answer(
        f"Постинг {'на паузе ⏸' if new else 'возобновлен ▶️'}!",
        reply_markup=get_reply_kb(new),
    )


@router.message(F.text == "🔽 Скрыть клавиатуру 🔽")
async def rk_hide(message: Message):
    if message.from_user.id not in ALL_ADMINS:
        return

    await message.answer(
        "Клавиатура скрыта.\nЧтобы вернуть, напиши /start",
        reply_markup=ReplyKeyboardRemove(),
    )


# =============================================================================
# ПЛАНИРОВЩИК
# =============================================================================
async def publisher_loop():
    logger.info("Планировщик запущен!")

    while True:
        try:
            if get_stat("paused"):
                await asyncio.sleep(10)
                continue

            now = int(time.time())

            with sqlite3.connect(DB_PLAN) as con:
                post = con.execute(
                    "SELECT id, caption, chat_id, msg_ids FROM posts WHERE slot <= ? ORDER BY slot LIMIT 1",
                    (now,),
                ).fetchone()

            if post:
                pid, cap, chat_id, msg_ids_json = post
                msg_ids = json.loads(msg_ids_json)
                safe_cap = esc(cap) if cap else None

                try:
                    if len(msg_ids) > 1:
                        await bot.copy_messages(
                            chat_id=CHANNEL_ID,
                            from_chat_id=chat_id,
                            message_ids=msg_ids,
                        )
                        if safe_cap:
                            await bot.send_message(chat_id=CHANNEL_ID, text=safe_cap)
                    else:
                        await bot.copy_message(
                            chat_id=CHANNEL_ID,
                            from_chat_id=chat_id,
                            message_id=msg_ids[0],
                            caption=safe_cap or None,
                        )

                    with sqlite3.connect(DB_PLAN) as con2:
                        con2.execute("DELETE FROM posts WHERE id=?", (pid,))
                        con2.commit()

                    st = load_json(STATS_FILE)
                    st.setdefault("sent", {"posts": 0, "photo": 0, "video": 0, "animation": 0})
                    st["sent"]["posts"] = st["sent"].get("posts", 0) + 1
                    save_json(STATS_FILE, st)

                    logger.info("Пост %s успешно скопирован в канал!", pid)
                except Exception:
                    logger.exception("Ошибка копирования поста %s; запись сохранена и перенесена на час", pid)

                    with sqlite3.connect(DB_PLAN) as con2:
                        retry_slot = int(time.time()) + 3600
                        con2.execute("UPDATE posts SET slot=? WHERE id=?", (retry_slot, pid))
                        con2.commit()
        except Exception as e:
            logger.error("Краш планировщика: %s", e)

        await asyncio.sleep(10)


# =============================================================================
# ЛОВУШКА ДЛЯ СТАРЫХ КНОПОК
# =============================================================================
@router.callback_query()
async def unknown_callback(query: CallbackQuery):
    try:
        await query.answer("🔄 Кнопка устарела. Обнови меню через /start.", show_alert=True)
    except Exception:
        pass


# =============================================================================
# ЗАПУСК
# =============================================================================
async def main():
    init_db()
    asyncio.create_task(publisher_loop())
    logger.info("Бот запущен.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
