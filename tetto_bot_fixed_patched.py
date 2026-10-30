# ==============================================================================
# БЛЯДЬ, ТОТАЛЬНЫЙ РЕФАКТОРИНГ! ТЕПЕРЬ С copy_message И БЕЗ СКАЧИВАНИЯ В КЭШ!
# ==============================================================================
import asyncio
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
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- БЛЯДСКИЕ НАСТРОЙКИ (ТВОИ ДАННЫЕ) ---
BOT_TOKEN = 
CHANNEL_ID = -1002130614493
ADMIN_ID = 6936783270
VEXX_ID = 8021778299 
ALL_ADMINS = {ADMIN_ID, VEXX_ID}

# --- ЧАСОВЫЕ ПОЯСА ---
TZ_MSK = ZoneInfo("Europe/Moscow")
TZ_SARATOV = ZoneInfo("Europe/Saratov")
TZ_EKB = ZoneInfo("Asia/Yekaterinburg")

# --- ФАЙЛЫ ---
DB_PLAN = "plan.db"
DB_SUGG = "proposed.db"
STATS_FILE = "stats.json"
BLOCK_FILE = "block.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("TETO_BOT")

def _ensure_columns(con, table, required_columns):
    """Добавляет отсутствующие поля в существующую БД без удаления данных."""
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in required_columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
            logger.info("Миграция БД: добавлено поле %s.%s", table, name)


def init_db():
    # Планировщик. Если БД осталась от старой версии, добавляем новые поля,
    # не удаляя и не пересоздавая существующие таблицы.
    with sqlite3.connect(DB_PLAN) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot INTEGER,
            caption TEXT,
            source TEXT,
            author_id INTEGER,
            chat_id INTEGER,
            msg_ids TEXT,
            created_at INTEGER)""")
        _ensure_columns(con, "posts", {
            "slot": "INTEGER", "caption": "TEXT", "source": "TEXT",
            "author_id": "INTEGER", "chat_id": "INTEGER",
            "msg_ids": "TEXT", "created_at": "INTEGER"
        })
        con.commit()

    # Предложка. Главное исправление: миграция старой sugg_posts,
    # где chat_id и msg_ids могли отсутствовать.
    with sqlite3.connect(DB_SUGG) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            is_anon INTEGER,
            status TEXT,
            created_at INTEGER,
            last_sent INTEGER)""")
        _ensure_columns(con, "batches", {
            "user_id": "INTEGER", "username": "TEXT", "is_anon": "INTEGER",
            "status": "TEXT", "created_at": "INTEGER", "last_sent": "INTEGER"
        })
        con.execute("""CREATE TABLE IF NOT EXISTS sugg_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            caption TEXT,
            status TEXT,
            pos INTEGER,
            chat_id INTEGER,
            msg_ids TEXT)""")
        _ensure_columns(con, "sugg_posts", {
            "batch_id": "INTEGER", "caption": "TEXT", "status": "TEXT",
            "pos": "INTEGER", "chat_id": "INTEGER", "msg_ids": "TEXT"
        })
        con.commit()

    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({"sent": {"posts": 0, "photo": 0, "video": 0, "animation": 0}, "paused": False}, f)
    if not os.path.exists(BLOCK_FILE):
        with open(BLOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)

def load_json(path, default=None):
    if not os.path.exists(path): return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default or {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=4, ensure_ascii=False)

def get_stat(key):
    data = load_json(STATS_FILE)
    return data.get(key, False if key == "paused" else 0)

def set_stat(key, val):
    data = load_json(STATS_FILE)
    data[key] = val
    save_json(STATS_FILE, data)

def get_next_slot():
    now = datetime.now(TZ_MSK).replace(minute=0, second=0, microsecond=0)
    next_hour = int((now + timedelta(hours=1)).timestamp())
    with sqlite3.connect(DB_PLAN) as con:
        used = {row[0] for row in con.execute("SELECT slot FROM posts")}
    slot = next_hour
    while slot in used:
        slot += 3600
    return slot

def fmt_time(ts, uid):
    tz = TZ_EKB if uid == VEXX_ID else TZ_SARATOV if uid == ADMIN_ID else TZ_MSK
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M %d.%m.%Y")

# --- АНТИФЛУД ---
FLOOD_TRACKER, MUTED_USERS = {}, {}
def check_flood(uid):
    if uid not in ALL_ADMINS: return False
    now = time.time()
    if uid in MUTED_USERS:
        if now < MUTED_USERS[uid]: return True
        del MUTED_USERS[uid]
    FLOOD_TRACKER.setdefault(uid, []).append(now)
    FLOOD_TRACKER[uid] = [t for t in FLOOD_TRACKER[uid] if now - t < 2]
    if len(FLOOD_TRACKER[uid]) >= 5:
        MUTED_USERS[uid] = now + 5
        FLOOD_TRACKER[uid].clear()
        return True
    return False

# --- КЛАВИАТУРЫ ---
def get_reply_kb(paused):
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🏠 Гл. Меню"), KeyboardButton(text="🗂 План")],
        [KeyboardButton(text=("Возобновить постинг ▶️" if paused else "Пауза постинга ⏸"))],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="ℹ️ Помощь")],
        [KeyboardButton(text="🚫 Черный список 🚫")],
        [KeyboardButton(text="🔽 Скрыть клавиатуру 🔽")]
    ], resize_keyboard=True)

def get_main_inline(paused):
    """Компактное меню: основные действия сгруппированы по две кнопки."""
    b = InlineKeyboardBuilder()
    b.button(text="🗂 Очередь публикаций", callback_data="plan:0")
    b.button(text="📊 Статистика", callback_data="m:stats")
    b.button(text=("▶️ Возобновить" if paused else "⏸ Пауза постинга"), callback_data="m:pause")
    b.button(text="🔄 Обновить", callback_data="m:main")
    b.button(text="🛡 Чёрный список", callback_data="m:blacklist")
    b.button(text="❔ Помощь", callback_data="m:help")
    b.adjust(2, 2, 2)
    return b.as_markup()

def get_preview_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Отложить ✅", callback_data="a:confirm")
    b.button(text="Отмена ✖️", callback_data="a:cancel")
    b.adjust(2)
    return b.as_markup()

def get_user_anon_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Анонимно 👻", callback_data="u:anon")
    b.button(text="Не анонимно", callback_data="u:nonanon")
    b.button(text="Отмена.", callback_data="u:cancel")
    b.adjust(1)
    return b.as_markup()

def get_review_kb(bid, spid):
    b = InlineKeyboardBuilder()
    b.button(text="Принять ✅", callback_data=f"r:acc:{spid}")
    b.button(text="Отклонить ❌", callback_data=f"r:rej:{spid}")
    b.button(text="Принять Все", callback_data=f"r:accall:{bid}")
    b.button(text="Отклонить все ❌", callback_data=f"r:rejall:{bid}")
    b.button(text="Заблокировать 🚫", callback_data=f"r:ban:{bid}")
    b.adjust(2, 2, 1)
    return b.as_markup()

def get_back_kb(cb="m:main"):
    b = InlineKeyboardBuilder()
    b.button(text="Назад 📤", callback_data=cb)
    return b.as_markup()

# --- БОТ И РОУТЕРЫ ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class UserStates(StatesGroup):
    waiting_media = State()
    choosing_anon = State()
    confirming = State()

admin_albums, user_albums = {}, {}
admin_batches = {}       
admin_preview_tasks = {} 

async def build_main_text(uid):
    paused = get_stat("paused")
    with sqlite3.connect(DB_PLAN) as con:
        p_count = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    st = load_json(STATS_FILE).get("sent", {})
    status = "⏸ На паузе" if paused else "🟢 Работает"
    tz_name = "Екатеринбург" if uid == VEXX_ID else ("Саратов" if uid == ADMIN_ID else "Москва")
    return (
        "<b>✦ TETO POSTIT · ПАНЕЛЬ УПРАВЛЕНИЯ</b>\n"
        "<i>Отложенные публикации и предложка</i>\n\n"
        "<b>📦 Очередь</b>\n"
        f"  • Ожидают публикации: <b>{p_count}</b>\n"
        f"  • Опубликовано всего: <b>{st.get('posts', 0)}</b>\n\n"
        "<b>⚙️ Состояние</b>\n"
        f"  • Постинг: <b>{status}</b>\n"
        f"  • Часовой пояс: <b>{tz_name}</b>\n\n"
        "<i>Выбери действие в меню ниже.</i>"
    )

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid in ALL_ADMINS:
        paused = get_stat("paused")
        await message.answer(await build_main_text(uid), reply_markup=get_reply_kb(paused))
        await message.answer("⬇️ Меню действий:", reply_markup=get_main_inline(paused))
    else:
        if str(uid) in load_json(BLOCK_FILE): return
        await message.answer("Привет!\nТы можешь предложить арт, видео или гифку в канал.\n(Альбомы и подписи разрешены.)")
        await state.set_state(UserStates.waiting_media)

# --- ФУНКЦИЯ ОТЛОЖЕННОГО ПРЕВЬЮ ---
async def send_preview_after_delay(uid, state):
    try:
        await asyncio.sleep(3.0)
        batch = admin_batches.get(uid, [])
        if not batch: return
        
        total_posts = len(batch)
        base_slot = get_next_slot()
        last_slot = base_slot + ((total_posts - 1) * 3600)
        paused = get_stat("paused")
        
        text = f"Пост будет отложен, последний на <b>{fmt_time(last_slot, uid)}</b>.\nВсего постов в пачке: <b>{total_posts}</b>.\n\nОтложить?"
        if paused: text += "\n\n<b>⏰ Постинг на паузе.</b>"

        data = await state.get_data()
        old_msg_id = data.get("preview_msg_id")
        if old_msg_id:
            try: await bot.delete_message(chat_id=uid, message_id=old_msg_id)
            except: pass
            
        msg = await bot.send_message(uid, text, reply_markup=get_preview_kb())
        await state.update_data(preview_msg_id=msg.message_id)
    except asyncio.CancelledError:
        pass

@router.message(F.photo | F.video | F.animation, F.chat.type == "private")
async def handle_media(message: Message, state: FSMContext):
    uid = message.from_user.id

    # ======= АДМИН =======
    if uid in ALL_ADMINS:
        if message.media_group_id:
            gid = message.media_group_id
            admin_albums.setdefault(gid, []).append(message)
            await asyncio.sleep(1.5)
            if gid not in admin_albums: return
            msgs = sorted(admin_albums.pop(gid), key=lambda m: m.message_id)
            msg_ids = [m.message_id for m in msgs]
            cap = msgs[0].caption or ""
            admin_batches.setdefault(uid, []).append({"chat_id": uid, "msg_ids": msg_ids, "cap": cap})
        else:
            admin_batches.setdefault(uid, []).append({"chat_id": uid, "msg_ids": [message.message_id], "cap": message.caption or ""})

        if uid in admin_preview_tasks:
            admin_preview_tasks[uid].cancel()
        admin_preview_tasks[uid] = asyncio.create_task(send_preview_after_delay(uid, state))
        return

    # ======= ЮЗЕР =======
    if str(uid) in load_json(BLOCK_FILE): return
    
    if message.media_group_id:
        gid = "u_" + message.media_group_id
        user_albums.setdefault(gid, []).append(message)
        await asyncio.sleep(1.5)
        if gid not in user_albums: return
        msgs = sorted(user_albums.pop(gid), key=lambda m: m.message_id)
        msg_ids = [m.message_id for m in msgs]
        cap = msgs[0].caption or ""
    else:
        msg_ids = [message.message_id]
        cap = message.caption or ""
    
    await state.update_data(user_chat_id=uid, user_msg_ids=msg_ids, user_cap=cap)
    await message.answer("Как предложить?\nАнонимно — «Предложил - Анон.»\nНе анонимно — «Предложил - @юзер»", reply_markup=get_user_anon_kb())
    await state.set_state(UserStates.choosing_anon)

# --- ПОДТВЕРЖДЕНИЕ АДМИНА ---
@router.callback_query(F.data.startswith("a:"))
async def admin_preview_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id
    if uid not in ALL_ADMINS: return await query.answer("Нет доступа", show_alert=True)
    if check_flood(uid): return await query.answer("FloodWait 5 second.", show_alert=True)
    
    if query.data == "a:cancel":
        admin_batches.pop(uid, None)
        await state.clear()
        return await query.message.edit_text("Отменено.")

    batch = admin_batches.pop(uid, None)
    if not batch: return await query.answer("Пусто!", show_alert=True)

    base_slot = get_next_slot()
    with sqlite3.connect(DB_PLAN) as con:
        for index, item in enumerate(batch):
            current_slot = base_slot + (index * 3600)
            # СОХРАНЯЕМ АЙДИШНИКИ СООБЩЕНИЙ В JSON
            cur = con.execute("INSERT INTO posts (slot, caption, source, author_id, chat_id, msg_ids, created_at) VALUES (?,?,?,?,?,?,?)", 
                              (current_slot, item["cap"], "admin", uid, item["chat_id"], json.dumps(item["msg_ids"]), int(time.time())))
        con.commit()

    await state.clear()
    last_slot = base_slot + ((len(batch) - 1) * 3600)
    await query.message.edit_text(f"✅ Отложено {len(batch)} постов. Последний — <b>{fmt_time(last_slot, uid)}</b>.")

# --- ИНЛАЙН МЕНЮ АДМИНА ---
@router.callback_query(F.data.startswith("m:"))
async def admin_menu_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS: return await query.answer("Нет доступа", show_alert=True)
    if check_flood(uid): return await query.answer("FloodWait 5 second.", show_alert=True)
    action = query.data.split(":")[1]
    await query.answer()

    if action == "main":
        paused = get_stat("paused")
        try: await query.message.edit_text(await build_main_text(uid), reply_markup=get_main_inline(paused))
        except TelegramBadRequest: pass
    elif action == "pause":
        new = not get_stat("paused")
        set_stat("paused", new)
        try: await query.message.edit_text(await build_main_text(uid), reply_markup=get_main_inline(new))
        except TelegramBadRequest: pass
    elif action == "stats":
        st = load_json(STATS_FILE).get("sent", {})
        text = f"<b>📊 Статистика публикаций</b>\n\nОпубликовано успешно: <b>{st.get('posts', 0)}</b>\n\n<i>Разбивка по типам медиа пока не ведётся достоверно.</i>"
        try: await query.message.edit_text(text, reply_markup=get_back_kb())
        except TelegramBadRequest: pass
    elif action == "help":
        try: await query.message.edit_text("Кидай фото/видео - отложу.\n/start - меню.", reply_markup=get_back_kb())
        except TelegramBadRequest: pass
    elif action == "blacklist":
        blocked = load_json(BLOCK_FILE)
        b = InlineKeyboardBuilder()
        if not blocked: b.button(text="Пусто", callback_data="noop")
        for u_id_str, u_name in blocked.items(): b.button(text=f"{u_name} • {u_id_str}", callback_data=f"unblock:{u_id_str}")
        b.adjust(1)
        b.row(InlineKeyboardButton(text="Назад 📤", callback_data="m:main"))
        try: await query.message.edit_text(f"<b>-= Заблокированные =-</b>\nВсего: {len(blocked)}", reply_markup=b.as_markup())
        except TelegramBadRequest: pass

# --- КОНТЕНТ ПЛАН ---
@router.callback_query(F.data.startswith("plan:"))
async def content_plan_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS: return await query.answer("Нет доступа!", show_alert=True)
    page = int(query.data.split(":")[1])
    per_page, offset = 5, page * 5

    with sqlite3.connect(DB_PLAN) as con:
        total = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        posts = con.execute("SELECT id, slot, caption FROM posts ORDER BY slot LIMIT ? OFFSET ?", (per_page, offset)).fetchall()

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages - 1)

    b = InlineKeyboardBuilder()
    if not posts: b.button(text="Очередь пуста", callback_data="noop")
    for pid, slot, cap in posts: b.button(text=f"Пост на {fmt_time(slot, uid)}", callback_data=f"view:{pid}:{page}")
    b.adjust(1)

    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"plan:{page-1}"))
    if page < pages - 1: nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"plan:{page+1}"))
    if nav: b.row(*nav)

    b.row(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    b.row(InlineKeyboardButton(text="Назад 📤", callback_data="m:main"))
    try: await query.message.edit_text(f"<b>-= Контент-План =-</b>\nЗапланированно: {total}", reply_markup=b.as_markup())
    except TelegramBadRequest: pass

@router.callback_query(F.data.startswith("view:"))
async def view_post_cb(query: CallbackQuery):
    uid = query.from_user.id
    if uid not in ALL_ADMINS: return await query.answer("Нет доступа!", show_alert=True)
    _, pid, page = query.data.split(":")
    pid = int(pid)
    with sqlite3.connect(DB_PLAN) as con:
        row = con.execute("SELECT slot, caption, chat_id, msg_ids FROM posts WHERE id=?", (pid,)).fetchone()
        if not row: return await query.answer("Пост удален!", show_alert=True)
        slot, cap, chat_id, msg_ids_json = row

    b = InlineKeyboardBuilder()
    b.button(text="🗑 Удалить", callback_data=f"del:{pid}:{page}")
    b.button(text="🛂 Перенести", callback_data=f"move:{pid}:{page}")
    b.button(text="Назад", callback_data=f"plan:{page}")
    b.adjust(2, 1)
    await query.answer()
    
    text = f"Пост на {fmt_time(slot, uid)}\nПодпись: {(cap or '—')[:200]}"
    
    # КОПИРУЕМ ЧТОБЫ ПОКАЗАТЬ АДМИНУ
    msg_ids = json.loads(msg_ids_json)
    try:
        if len(msg_ids) > 1:
            await bot.copy_messages(chat_id=uid, from_chat_id=chat_id, message_ids=msg_ids)
            await bot.send_message(uid, text, reply_markup=b.as_markup())
        else:
            await bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_ids[0])
            await bot.send_message(uid, text, reply_markup=b.as_markup())
    except Exception as e:
        await query.message.edit_text(f"{text}\n\n⚠️ Медиа не найдено: {e}", reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("del:"))
async def delete_post_cb(query: CallbackQuery):
    _, pid, page = query.data.split(":")
    with sqlite3.connect(DB_PLAN) as con:
        con.execute("DELETE FROM posts WHERE id=?", (pid,))
        con.commit()
    await query.answer("Пост удален нахуй!")
    query.data = f"plan:{page}"
    await content_plan_cb(query)

@router.callback_query(F.data.startswith("move:"))
async def move_post_cb(query: CallbackQuery):
    _, pid, page = query.data.split(":")
    new_slot = get_next_slot()
    with sqlite3.connect(DB_PLAN) as con:
        con.execute("UPDATE posts SET slot=? WHERE id=?", (new_slot, pid))
        con.commit()
    await query.answer(f"Перенесен на {fmt_time(new_slot, query.from_user.id)}")
    query.data = f"plan:{page}"
    await content_plan_cb(query)

# --- РАЗБАН И NOOP ---
@router.callback_query(F.data.startswith("unblock:"))
async def unblock_cb(query: CallbackQuery):
    u_id = query.data.split(":")[1]
    blocked = load_json(BLOCK_FILE)
    if u_id in blocked:
        del blocked[u_id]
        save_json(BLOCK_FILE, blocked)
        await query.answer(f"Разблокирован {u_id}!")
    query.data = "m:blacklist"
    await admin_menu_cb(query)

@router.callback_query(F.data == "noop")
async def noop_cb(query: CallbackQuery): await query.answer()

# --- ЮЗЕР (ПРЕДЛОЖКА) ---
@router.callback_query(F.data.startswith("u:"))
async def user_cb(query: CallbackQuery, state: FSMContext):
    uid = query.from_user.id
    action = query.data.split(":")[1]
    if action == "cancel":
        await state.clear()
        return await query.message.edit_text("Отменено.")
    data = await state.get_data()
    chat_id, msg_ids, cap = data.get("user_chat_id"), data.get("user_msg_ids", []), data.get("user_cap", "")
    if not msg_ids: return await query.answer("Пусто!", show_alert=True)

    if action in ["anon", "nonanon"]:
        await state.update_data(user_anon=(action == "anon"))
        b = InlineKeyboardBuilder()
        b.button(text="Подтверждаю", callback_data="u:confirm")
        b.button(text="Отмена", callback_data="u:cancel")
        label = "Анонимную" if action == "anon" else "Не анонимную"
        await query.message.edit_text(f"Подтверждаете {label} отправку?", reply_markup=b.as_markup())
        await state.set_state(UserStates.confirming)

    elif action == "confirm":
        if await state.get_state() != UserStates.confirming.state:
            return await query.answer("Эта отправка уже обработана или устарела.", show_alert=True)
        is_anon = data.get("user_anon", True)
        if not chat_id or not msg_ids:
            return await query.answer("Не нашёл исходное медиа. Отправь его ещё раз.", show_alert=True)
        try:
            with sqlite3.connect(DB_SUGG) as con:
                cur = con.execute("INSERT INTO batches (user_id, username, is_anon, status, created_at, last_sent) VALUES (?,?,?,?,?,?)",
                                  (uid, query.from_user.username, int(is_anon), "pending", int(time.time()), int(time.time())))
                bid = cur.lastrowid
                cur = con.execute("INSERT INTO sugg_posts (batch_id, caption, status, pos, chat_id, msg_ids) VALUES (?,?,?,?,?,?)",
                                  (bid, cap, "pending", 0, chat_id, json.dumps(msg_ids)))
                spid = cur.lastrowid
                con.commit()
        except sqlite3.Error:
            logger.exception("Ошибка сохранения предложки пользователя %s", uid)
            return await query.answer("Не удалось сохранить предложку. Сообщи администратору.", show_alert=True)

        await query.answer("Отправлено на рассмотрение!")
        await state.clear()
        try:
            await query.message.edit_text("Отправлено админам на рассмотрение.")
        except TelegramBadRequest:
            pass
        
        for admin in ALL_ADMINS:
            try:
                label = "Предложил - Анон." if is_anon else f"Предложил - @{query.from_user.username or uid}"
                full_cap = label + ("\n\nКомментарий:\n" + cap if cap else "")
                markup = get_review_kb(bid, spid)
                
                # КОПИРУЕМ АДМИНУ
                if len(msg_ids) > 1:
                    await bot.copy_messages(chat_id=admin, from_chat_id=chat_id, message_ids=msg_ids)
                    await bot.send_message(admin, full_cap, reply_markup=markup)
                else:
                    await bot.copy_message(chat_id=admin, from_chat_id=chat_id, message_id=msg_ids[0])
                    await bot.send_message(admin, full_cap, reply_markup=markup)
            except Exception as e:
                logger.error(f"Не смог отправить предложку: {e}")

# --- РЕВЬЮ АДМИНОМ ---
@router.callback_query(F.data.startswith("r:"))
async def review_cb(query: CallbackQuery):
    if query.from_user.id not in ALL_ADMINS: return await query.answer("Нет доступа!", show_alert=True)
    parts = query.data.split(":")
    action, target = parts[1], int(parts[2])

    if action in ["acc", "rej"]:
        with sqlite3.connect(DB_SUGG) as con:
            row = con.execute("SELECT batch_id, status FROM sugg_posts WHERE id=?", (target,)).fetchone()
            if not row or row[1] != "pending": return await query.answer("Уже рассмотрено!", show_alert=True)
            bid = row[0]
            con.execute("UPDATE sugg_posts SET status=? WHERE id=?", ("accepted" if action == "acc" else "rejected", target))
            con.commit()
        if action == "acc":
            with sqlite3.connect(DB_SUGG) as con:
                cap, chat_id, msg_ids_json = con.execute("SELECT caption, chat_id, msg_ids FROM sugg_posts WHERE id=?", (target,)).fetchone()
            slot = get_next_slot()
            with sqlite3.connect(DB_PLAN) as con:
                con.execute("INSERT INTO posts (slot, caption, source, chat_id, msg_ids, created_at) VALUES (?,?,?,?,?,?)", 
                            (slot, cap, "sugg", chat_id, msg_ids_json, int(time.time())))
                con.commit()
        await query.answer("Принято!" if action == "acc" else "Отклонено!")
        try: await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest: pass

    elif action == "ban":
        with sqlite3.connect(DB_SUGG) as con:
            user = con.execute("SELECT user_id, username FROM batches WHERE id=?", (target,)).fetchone()
            con.execute("UPDATE sugg_posts SET status='rejected' WHERE batch_id=?", (target,))
            con.execute("UPDATE batches SET status='banned' WHERE id=?", (target,))
            con.commit()
        if user:
            data = load_json(BLOCK_FILE)
            data[str(user[0])] = user[1] or str(user[0])
            save_json(BLOCK_FILE, data)
            with sqlite3.connect(DB_PLAN) as pcon:
                pcon.execute("DELETE FROM posts WHERE chat_id=? AND source='sugg'", (user[0],))
                pcon.commit()
            try: await bot.send_message(user[0], "Вы были заблокированы.")
            except: pass
        await query.answer("Забанен!")
        try: await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest: pass

    elif action == "accall":
        with sqlite3.connect(DB_SUGG) as con:
            posts = con.execute("SELECT id, caption, chat_id, msg_ids FROM sugg_posts WHERE batch_id=? AND status='pending'", (target,)).fetchall()
            for spid, cap, chat_id, msg_ids_json in posts:
                slot = get_next_slot()
                with sqlite3.connect(DB_PLAN) as pcon:
                    pcon.execute("INSERT INTO posts (slot, caption, source, chat_id, msg_ids, created_at) VALUES (?,?,?,?,?,?)", 
                                 (slot, cap, "sugg", chat_id, msg_ids_json, int(time.time())))
                    pcon.commit()
                con.execute("UPDATE sugg_posts SET status='accepted' WHERE id=?", (spid,))
            con.commit()
        await query.answer("Всё принято!")
        try: await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest: pass

    elif action == "rejall":
        with sqlite3.connect(DB_SUGG) as con:
            con.execute("UPDATE sugg_posts SET status='rejected' WHERE batch_id=? AND status='pending'", (target,))
            con.commit()
        await query.answer("Всё отклонено!")
        try: await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest: pass

# --- НИЖНЯЯ КЛАВИАТУРА (REPLY) ---
@router.message(F.text.in_({"🏠 Гл. Меню", "🏠 Главное меню", "🏡 Гл. Меню"}))
async def rk_main(message: Message, state: FSMContext):
    if message.from_user.id in ALL_ADMINS:
        await cmd_start(message, state)

@router.message(F.text == "🗂 План")
async def rk_plan(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    b = InlineKeyboardBuilder()
    b.button(text="Открыть контент-план 🗂", callback_data="plan:0")
    await message.answer("<b>🗂 Контент-план</b>\nОткрой очередь, чтобы посмотреть, перенести или удалить запланированный пост.", reply_markup=b.as_markup())

@router.message(F.text == "📊 Статистика")
async def rk_stats(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    st = load_json(STATS_FILE).get("sent", {})
    await message.answer(f"<b>📊 Статистика публикаций</b>\n\nОпубликовано успешно: <b>{st.get('posts', 0)}</b>\n\n<i>Разбивка по типам медиа пока не ведётся достоверно.</i>", reply_markup=get_back_kb())

@router.message(F.text == "ℹ️ Помощь")
async def rk_help(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    await message.answer("Кидай фото/видео - отложу.\n/start - меню.", reply_markup=get_back_kb())

@router.message(F.text == "🚫 Черный список 🚫")
async def rk_blacklist(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    blocked = load_json(BLOCK_FILE)
    b = InlineKeyboardBuilder()
    if not blocked: b.button(text="Пусто", callback_data="noop")
    for u_id_str, u_name in blocked.items(): b.button(text=f"{u_name} • {u_id_str}", callback_data=f"unblock:{u_id_str}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="Назад 📤", callback_data="m:main"))
    await message.answer(f"<b>-= Заблокированные =-</b>\nВсего: {len(blocked)}", reply_markup=b.as_markup())

@router.message(F.text.in_({"Пауза постинга ⏸", "Возобновить постинг ▶️", "Возобновить постинг ↪️"}))
async def rk_pause(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    new = not get_stat("paused")
    set_stat("paused", new)
    await message.answer(f"Постинг {'на паузе ⏸' if new else 'возобновлен ▶️'}!", reply_markup=get_reply_kb(new))

@router.message(F.text == "🔽 Скрыть клавиатуру 🔽")
async def rk_hide(message: Message):
    if message.from_user.id not in ALL_ADMINS: return
    await message.answer("Клавиатура скрыта. Чтобы вернуть, напиши /start", reply_markup=ReplyKeyboardRemove())

# --- ФОНОВЫЙ ПЛАНИРОВЩИК ---
async def publisher_loop():
    logger.info("Планировщик запущен!")
    while True:
        try:
            if get_stat("paused"): 
                await asyncio.sleep(10)
                continue
            now = int(time.time())
            with sqlite3.connect(DB_PLAN) as con:
                post = con.execute("SELECT id, caption, chat_id, msg_ids FROM posts WHERE slot <= ? ORDER BY slot LIMIT 1", (now,)).fetchone()
                if post:
                    pid, cap, chat_id, msg_ids_json = post
                    msg_ids = json.loads(msg_ids_json)
                    
                    try:
                        # БЛЯДЬ, ВОТ ОНА, МАГИЯ COPY_MESSAGE! НИКАКОГО СКАЧИВАНИЯ!
                        if len(msg_ids) > 1:
                            await bot.copy_messages(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_ids=msg_ids)
                        else:
                            await bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=msg_ids[0])
                            
                        with sqlite3.connect(DB_PLAN) as con2:
                            con2.execute("DELETE FROM posts WHERE id=?", (pid,))
                            con2.commit()
                            
                        st = load_json(STATS_FILE)
                        st["sent"]["posts"] += 1
                        # Тип медиа пока не хранится в старой схеме; не подделываем разбивку по типам.
                        save_json(STATS_FILE, st)
                        logger.info(f"Пост {pid} успешно скопирован в канал!")
                    except Exception as e:
                        logger.exception("Ошибка копирования поста %s; запись сохранена и перенесена на час", pid)
                        with sqlite3.connect(DB_PLAN) as con2:
                            retry_slot = int(time.time()) + 3600
                            con2.execute("UPDATE posts SET slot=? WHERE id=?", (retry_slot, pid))
                            con2.commit()
        except Exception as e:
            logger.error(f"Краш планировщика: {e}")
        await asyncio.sleep(10)

# --- ЗАПУСК ---
async def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь новый токен в переменные окружения хостинга.")
    init_db()
    asyncio.create_task(publisher_loop())
    logger.info("Бот запущен.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
