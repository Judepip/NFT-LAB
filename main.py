import json
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import uvicorn
import threading
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://judepip.github.io/NFT-LAB")
BOT_USERNAME = os.getenv("BOT_USERNAME", "lab_game_bot")
PROXY_URL = os.getenv("PROXY_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен")

# ---------- СОЗДАНИЕ БОТА ----------
def create_bot():
    session = None
    if PROXY_URL:
        logger.info(f"Используется прокси: {PROXY_URL}")
        session = AiohttpSession(proxy=PROXY_URL)
    return Bot(token=BOT_TOKEN, session=session)

bot = create_bot()
dp = Dispatcher()

# ---------- БАЗА ДАННЫХ ----------
DB_PATH = "lab_nft.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            inventory TEXT DEFAULT '[]',
            balance REAL DEFAULT 1000.0,
            spins INTEGER DEFAULT 0,
            upgrades TEXT DEFAULT '[{"id":"up1","level":0},{"id":"up2","level":0},{"id":"up3","level":0}]',
            free_spin_used_at TIMESTAMP,
            referrer_id INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            first_spin_done INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("[DB] Таблицы инициализированы")

init_db()

# ---------- ФУНКЦИИ БД ----------
def get_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def create_user(user_id: int, referrer_id: int = 0):
    conn = get_db_connection()
    cursor = conn.cursor()
    existing = get_user(user_id)
    if existing:
        return existing
    
    cursor.execute('''
        INSERT INTO users (user_id, inventory, balance, spins, upgrades, referrer_id, first_spin_done)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, json.dumps([]), 1000.0, 0, json.dumps([{"id":"up1","level":0},{"id":"up2","level":0},{"id":"up3","level":0}]), referrer_id, 0))
    conn.commit()
    conn.close()
    return get_user(user_id)

def mark_first_spin_done(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET first_spin_done = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def process_referral(referrer_id: int, referred_id: int):
    if referrer_id == referred_id:
        return False
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM referrals WHERE referred_id = ?", (referred_id,))
    if cursor.fetchone():
        conn.close()
        return False
    cursor.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, referred_id))
    cursor.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (referrer_id,))
    conn.commit()
    conn.close()
    return True

def get_referral_count(user_id: int) -> int:
    row = get_user(user_id)
    return row["referral_count"] if row else 0

def can_withdraw_or_sell_free_gift(user_id: int) -> bool:
    return get_referral_count(user_id) >= 7

def save_user(user_id: int, inventory: list, balance: float, spins: int, upgrades: list):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET inventory = ?, balance = ?, spins = ?, upgrades = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (json.dumps(inventory), balance, spins, json.dumps(upgrades), user_id))
    conn.commit()
    conn.close()

def update_free_spin(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET free_spin_used_at = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def can_use_free_spin(user_id: int) -> bool:
    row = get_user(user_id)
    if not row or not row["free_spin_used_at"]:
        return True
    used = datetime.fromisoformat(row["free_spin_used_at"].replace('Z', '+00:00'))
    return datetime.now() > used + timedelta(hours=24)

def save_withdraw_request(user_id: int, username: str, amount: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO withdraw_requests (user_id, username, amount, status)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, amount, 'pending'))
    conn.commit()
    conn.close()

def get_pending_withdraws():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM withdraw_requests WHERE status = 'pending' ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_withdraw_status(request_id: int, status: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE withdraw_requests SET status = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()

# ---------- ОБРАБОТЧИКИ БОТА ----------
@dp.message(Command("start"))
async def start_command(message: types.Message):
    args = message.text.split()
    referrer_id = 0
    if len(args) > 1:
        try:
            referrer_id = int(args[1])
            if referrer_id == message.from_user.id:
                referrer_id = 0
        except:
            pass
    
    create_user(message.from_user.id, referrer_id)
    
    mini_app_url = MINI_APP_URL + f"?startapp={message.from_user.id}"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 ОТКРЫТЬ ИГРУ",
                    web_app=WebAppInfo(url=mini_app_url)
                )
            ]
        ]
    )
    
    # ---- ОТПРАВКА КАРТИНКИ welcome.png ----
    try:
        photo_path = "images/welcome.png"
        photo = FSInputFile(photo_path)
        
        await message.answer_photo(
            photo=photo,
            caption=f"🧪 Добро пожаловать в LAB NFT!\n\n"
                    f"Нажми кнопку ниже, чтобы открыть Mini App и начать игру!",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка отправки картинки: {e}")
        # Если картинка не загрузилась — отправляем просто текст
        await message.answer(
            f"🧪 Добро пожаловать в LAB NFT!\n\n"
            f"Нажми кнопку ниже, чтобы открыть Mini App и начать игру!",
            reply_markup=keyboard
        )

@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа.")
        return
    pending = get_pending_withdraws()
    if not pending:
        await message.answer("📭 Нет ожидающих заявок.")
        return
    text = "📋 **Ожидающие заявки:**\n\n"
    buttons = []
    for req in pending:
        text += f"🆔 #{req['id']} | @{req['username']} | {req['amount']} ⭐\n"
        buttons.append([
            InlineKeyboardButton(text=f"✅ #{req['id']}", callback_data=f"approve_{req['id']}"),
            InlineKeyboardButton(text=f"❌ #{req['id']}", callback_data=f"reject_{req['id']}")
        ])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data.startswith("approve_") or c.data.startswith("reject_"))
async def process_withdraw_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    action, req_id = callback.data.split("_")
    status = "approved" if action == "approve" else "rejected"
    update_withdraw_status(int(req_id), status)
    await callback.message.answer(f"✅ Заявка #{req_id} обработана.")
    await callback.answer()

@dp.message(types.ContentType.WEB_APP_DATA)
async def web_app_data_handler(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get("action")
        payload = data.get("payload", {})
        user_id = message.from_user.id
        
        if action == "app_open":
            referrer_id = payload.get("referrer_id", 0)
            create_user(user_id, referrer_id)
            
        elif action == "roulette_spin":
            user = get_user(user_id)
            if user and user["first_spin_done"] == 0:
                mark_first_spin_done(user_id)
                referrer_id = user["referrer_id"] or 0
                if referrer_id > 0:
                    success = process_referral(referrer_id, user_id)
                    if success:
                        try:
                            await bot.send_message(
                                referrer_id,
                                f"🎉 У вас новый реферал!\n"
                                f"👤 Пользователь прокрутил первую рулетку!\n"
                                f"📊 Всего: {get_referral_count(referrer_id)}/7"
                            )
                        except:
                            pass
            if payload.get("is_free", False):
                update_free_spin(user_id)
            
        elif action == "withdraw":
            if not can_withdraw_or_sell_free_gift(user_id):
                await message.answer(f"❌ Вывод заблокирован! Пригласите 7 друзей. Ваш прогресс: {get_referral_count(user_id)}/7")
                return
            save_withdraw_request(user_id, payload.get("username", "unknown"), payload.get("amount", 25))
            if ADMIN_ID:
                await bot.send_message(ADMIN_ID, f"💸 Заявка на вывод от @{payload.get('username')} ({user_id}) - {payload.get('amount')} ⭐")
                
    except Exception as e:
        logger.error(f"Ошибка: {e}")

# ---------- FASTAPI ----------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
@app.get("/index.html")
async def serve_index():
    return FileResponse("static/index.html")

@app.get("/api/user/{user_id}")
async def get_user_data(user_id: int):
    row = get_user(user_id)
    if not row:
        create_user(user_id)
        row = get_user(user_id)
    return {
        "user_id": row["user_id"],
        "inventory": json.loads(row["inventory"]),
        "balance": row["balance"],
        "spins": row["spins"],
        "upgrades": json.loads(row["upgrades"]),
        "referral_count": row["referral_count"] or 0,
        "can_withdraw": can_withdraw_or_sell_free_gift(user_id),
        "can_free_spin": can_use_free_spin(user_id),
        "free_spin_used_at": row["free_spin_used_at"],
        "first_spin_done": row["first_spin_done"] or 0
    }

@app.post("/api/user/{user_id}/spin")
async def spin_roulette(user_id: int, request: Request):
    data = await request.json()
    row = get_user(user_id)
    if not row:
        create_user(user_id)
        row = get_user(user_id)
    
    inventory = json.loads(row["inventory"])
    balance = row["balance"]
    spins = row["spins"]
    upgrades = json.loads(row["upgrades"])
    
    if row["first_spin_done"] == 0 and row["referrer_id"] > 0:
        process_referral(row["referrer_id"], user_id)
        mark_first_spin_done(user_id)
    
    if data.get("is_free", False):
        if not can_use_free_spin(user_id):
            raise HTTPException(400, "Бесплатная рулетка ещё не доступна")
        update_free_spin(user_id)
    else:
        if balance < 150:
            raise HTTPException(400, "Недостаточно звёзд")
        balance -= 150
    
    spins += 1
    new_gift = {
        "id": f"gift_{datetime.now().timestamp()}",
        "name": data.get("prize"),
        "image": data.get("image", ""),
        "price": 0,
        "is_free": data.get("is_free", False)
    }
    inventory.append(new_gift)
    save_user(user_id, inventory, balance, spins, upgrades)
    return {"success": True, "prize": new_gift, "new_balance": balance, "inventory": inventory}

# ---------- ЗАПУСК ----------
def run_bot():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)