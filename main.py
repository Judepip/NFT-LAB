import json
import sqlite3
import os
import logging
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
GROUP_ID = int(os.getenv("GROUP_ID", 0))  # ← ТЕПЕРЬ GROUP_ID ВМЕСТО ADMIN_ID
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://judepip.github.io/NFT-LAB")
BOT_USERNAME = os.getenv("BOT_USERNAME", "lab_game_bot")
PROXY_URL = os.getenv("PROXY_URL")
YOUR_TON_ADDRESS = "UQBhNenZ50ac9WskDqQGeajDC62-RoRwqO961LGRdu3Dml3i"

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
            can_withdraw INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referrer_id) REFERENCES users(user_id),
            FOREIGN KEY (referred_id) REFERENCES users(user_id)
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ton_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            transaction_hash TEXT UNIQUE,
            amount_ton REAL,
            stars INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        INSERT INTO users (user_id, inventory, balance, spins, upgrades, referrer_id, first_spin_done, can_withdraw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, json.dumps([]), 1000.0, 0, json.dumps([{"id":"up1","level":0},{"id":"up2","level":0},{"id":"up3","level":0}]), referrer_id, 0, 0))
    conn.commit()
    conn.close()
    return get_user(user_id)

def mark_first_spin_done(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET first_spin_done = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def grant_withdraw(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET can_withdraw = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"✅ Пользователю {user_id} выдано право на вывод")

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

def can_withdraw(user_id: int) -> bool:
    row = get_user(user_id)
    if row:
        return row["can_withdraw"] == 1
    return False

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

# ---------- TON API ----------
def verify_ton_transaction(transaction_hash, expected_amount_ton):
    try:
        url = f"https://tonapi.io/v2/transactions/{transaction_hash}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return False
        
        tx_data = response.json()
        if tx_data.get('destination') != YOUR_TON_ADDRESS:
            return False
        
        amount_nano = int(float(expected_amount_ton) * 1e9)
        if int(tx_data.get('value', 0)) < amount_nano:
            return False
        
        return True
    except Exception as e:
        logger.error(f"Ошибка проверки транзакции: {e}")
        return False

def add_stars_to_user(user_id: int, stars: int):
    row = get_user(user_id)
    if not row:
        create_user(user_id)
        row = get_user(user_id)
    
    inventory = json.loads(row["inventory"])
    balance = row["balance"] + stars
    spins = row["spins"]
    upgrades = json.loads(row["upgrades"])
    
    save_user(user_id, inventory, balance, spins, upgrades)
    return balance

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
        await message.answer(
            f"🧪 Добро пожаловать в LAB NFT!\n\n"
            f"Нажми кнопку ниже, чтобы открыть Mini App и начать игру!",
            reply_markup=keyboard
        )

@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    # Проверяем, что сообщение из группы с ID = GROUP_ID
    if message.chat.id != GROUP_ID:
        await message.answer("⛔ Эта команда доступна только в специальной группе.")
        return
    
    pending = get_pending_withdraws()
    if not pending:
        await message.answer("📭 Нет ожидающих заявок на вывод.")
        return
    
    text = "📋 **Ожидающие заявки на вывод:**\n\n"
    buttons = []
    for req in pending:
        text += f"🆔 #{req['id']} | @{req['username']} | {req['amount']} ⭐\n"
        buttons.append([
            InlineKeyboardButton(text=f"✅ #{req['id']}", callback_data=f"approve_{req['id']}"),
            InlineKeyboardButton(text=f"❌ #{req['id']}", callback_data=f"reject_{req['id']}")
        ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data.startswith("approve_") or c.data.startswith("reject_"))
async def process_withdraw_callback(callback: types.CallbackQuery):
    # Проверяем, что callback из правильной группы
    if callback.message.chat.id != GROUP_ID:
        await callback.answer("⛔ Доступно только в специальной группе.", show_alert=True)
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
            is_free = payload.get("is_free", False)
            
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
            
            if not is_free and user and user["can_withdraw"] == 0:
                grant_withdraw(user_id)
            
            if is_free:
                update_free_spin(user_id)
            
        elif action == "withdraw":
            if not can_withdraw(user_id):
                await message.answer(f"❌ Вывод доступен только после платного спина или 7 рефералов.\nВаш прогресс: {get_referral_count(user_id)}/7")
                return
            
            username = payload.get("username", "unknown")
            amount = payload.get("amount", 25)
            
            save_withdraw_request(user_id, username, amount)
            
            # ---- ОТПРАВКА В ГРУППУ (ВМЕСТО АДМИНУ) ----
            if GROUP_ID:
                admin_text = (
                    f"💸 **НОВАЯ ЗАЯВКА НА ВЫВОД**\n\n"
                    f"👤 Пользователь: @{username}\n"
                    f"🆔 ID: {user_id}\n"
                    f"⭐ Сумма: {amount} звёзд\n"
                    f"📅 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"Используйте /admin в этой группе для управления заявками."
                )
                try:
                    await bot.send_message(GROUP_ID, admin_text, parse_mode="Markdown")
                    logger.info(f"✅ Заявка отправлена в группу {GROUP_ID}")
                except Exception as e:
                    logger.error(f"Ошибка отправки в группу: {e}")
                    await message.answer("⚠️ Ошибка отправки заявки. Попробуйте позже.")
                
    except Exception as e:
        logger.error(f"Ошибка: {e}")

# ---------- FASTAPI ----------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        "can_withdraw": row["can_withdraw"] == 1,
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
    is_free = data.get("is_free", False)
    
    if row["first_spin_done"] == 0 and row["referrer_id"] > 0:
        process_referral(row["referrer_id"], user_id)
        mark_first_spin_done(user_id)
    
    if not is_free:
        if balance < 150:
            raise HTTPException(400, "Недостаточно звёзд")
        if row["can_withdraw"] == 0:
            grant_withdraw(user_id)
            row = get_user(user_id)
        balance -= 150
    else:
        if not can_use_free_spin(user_id):
            raise HTTPException(400, "Бесплатная рулетка ещё не доступна")
        update_free_spin(user_id)
    
    spins += 1
    
    new_gift = {
        "id": f"gift_{datetime.now().timestamp()}",
        "name": data.get("prize"),
        "image": data.get("image", ""),
        "price": 0,
        "is_free": is_free
    }
    inventory.append(new_gift)
    save_user(user_id, inventory, balance, spins, upgrades)
    
    row = get_user(user_id)
    
    return {
        "success": True,
        "prize": new_gift,
        "new_balance": balance,
        "inventory": inventory,
        "can_free_spin": can_use_free_spin(user_id),
        "first_spin_done": 1,
        "can_withdraw": row["can_withdraw"] == 1
    }

@app.post("/api/user/{user_id}/upgrade")
async def upgrade_level(user_id: int, request: Request):
    data = await request.json()
    upgrade_id = data.get("upgrade_id")
    
    if not upgrade_id:
        raise HTTPException(400, "Не указан ID апгрейда")
    
    row = get_user(user_id)
    if not row:
        create_user(user_id)
        row = get_user(user_id)
    
    inventory = json.loads(row["inventory"])
    balance = row["balance"]
    spins = row["spins"]
    upgrades = json.loads(row["upgrades"])
    
    upgrade = next((u for u in upgrades if u["id"] == upgrade_id), None)
    if not upgrade:
        raise HTTPException(404, "Апгрейд не найден")
    
    cost_map = {"up1": 150, "up2": 200, "up3": 100}
    max_lvl_map = {"up1": 5, "up2": 4, "up3": 6}
    
    cost = cost_map.get(upgrade_id, 150) * (upgrade["level"] + 1)
    max_lvl = max_lvl_map.get(upgrade_id, 5)
    
    if upgrade["level"] >= max_lvl:
        raise HTTPException(400, "Максимальный уровень достигнут")
    
    if balance < cost:
        raise HTTPException(400, "Недостаточно звёзд")
    
    balance -= cost
    upgrade["level"] += 1
    
    save_user(user_id, inventory, balance, spins, upgrades)
    
    return {
        "success": True,
        "upgrade": upgrade,
        "new_balance": balance
    }

@app.post("/api/ton/payment")
async def ton_payment(request: Request):
    try:
        data = await request.json()
        user_id = data.get("user_id")
        amount_ton = data.get("amount_ton")
        stars = data.get("stars")
        transaction_hash = data.get("transaction_hash", "pending")
        
        if not user_id or not amount_ton or not stars:
            raise HTTPException(400, "Недостаточно данных")
        
        if transaction_hash and transaction_hash != "pending":
            is_valid = verify_ton_transaction(transaction_hash, amount_ton)
            if not is_valid:
                return {"success": False, "message": "Транзакция не найдена или недействительна"}
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ton_payments WHERE transaction_hash = ?", (transaction_hash,))
            if cursor.fetchone():
                conn.close()
                return {"success": False, "message": "Транзакция уже обработана"}
            
            cursor.execute('''
                INSERT INTO ton_payments (user_id, transaction_hash, amount_ton, stars, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, transaction_hash, amount_ton, stars, 'completed'))
            conn.commit()
            conn.close()
            
            new_balance = add_stars_to_user(user_id, stars)
            logger.info(f"✅ Зачислено {stars} звёзд пользователю {user_id} за {amount_ton} TON")
            
            return {
                "success": True,
                "message": f"Зачислено {stars} звёзд",
                "new_balance": new_balance
            }
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ton_payments (user_id, amount_ton, stars, status)
                VALUES (?, ?, ?, ?)
            ''', (user_id, amount_ton, stars, 'pending'))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "message": "Платёж создан, ожидает подтверждения",
                "status": "pending"
            }
            
    except Exception as e:
        logger.error(f"Ошибка в ton_payment: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/withdraw/request")
async def create_withdraw_request(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    username = data.get("username")
    amount = data.get("amount", 25)
    
    if not user_id or not username:
        raise HTTPException(400, "Не указаны user_id или username")
    
    if not can_withdraw(user_id):
        raise HTTPException(403, "Вывод заблокирован. Сделайте платный спин или пригласите 7 друзей.")
    
    save_withdraw_request(user_id, username, amount)
    
    return {
        "success": True,
        "message": f"Заявка на вывод {amount} ⭐ для @{username} создана"
    }

# ---------- ЗАПУСК ----------
def run_bot():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    if GROUP_ID == 0:
        logger.warning("⚠️ GROUP_ID не установлен. Укажите его в .env файле.")
    
    threading.Thread(target=run_bot, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)