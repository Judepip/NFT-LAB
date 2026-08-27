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
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# ---------- 1. НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- 2. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://judepip.github.io/NFT-LAB")
BOT_USERNAME = os.getenv("BOT_USERNAME", "lab_game_bot")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env файле")

# ---------- 3. ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- 4. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ (SQLite) ----------
DB_PATH = "lab_nft.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Таблица пользователей
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
    
    # Таблица реферальных связей
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
    
    # Таблица заявок на вывод
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

# ---------- 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
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
    ''', (
        user_id,
        json.dumps([]),
        1000.0,
        0,
        json.dumps([{"id": "up1", "level": 0}, {"id": "up2", "level": 0}, {"id": "up3", "level": 0}]),
        referrer_id,
        0
    ))
    conn.commit()
    conn.close()
    return get_user(user_id)

def mark_first_spin_done(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET first_spin_done = 1
        WHERE user_id = ?
    ''', (user_id,))
    conn.commit()
    conn.close()

def process_referral(referrer_id: int, referred_id: int):
    referrer = get_user(referrer_id)
    if not referrer:
        return False
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM referrals WHERE referred_id = ?
    ''', (referred_id,))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return False
    
    if referrer_id == referred_id:
        conn.close()
        return False
    
    cursor.execute('''
        INSERT INTO referrals (referrer_id, referred_id)
        VALUES (?, ?)
    ''', (referrer_id, referred_id))
    conn.commit()
    
    cursor.execute('''
        UPDATE users 
        SET referral_count = referral_count + 1 
        WHERE user_id = ?
    ''', (referrer_id,))
    conn.commit()
    conn.close()
    return True

def save_user(user_id: int, inventory: list, balance: float, spins: int, upgrades: list):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET inventory = ?, balance = ?, spins = ?, upgrades = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (json.dumps(inventory), balance, spins, json.dumps(upgrades), user_id))
    conn.commit()
    conn.close()

def update_free_spin(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET free_spin_used_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    ''', (user_id,))
    conn.commit()
    conn.close()

def can_use_free_spin(user_id: int) -> bool:
    row = get_user(user_id)
    if row is None:
        return True
    used_at = row["free_spin_used_at"]
    if used_at is None:
        return True
    used_time = datetime.fromisoformat(used_at.replace('Z', '+00:00'))
    return datetime.now() > used_time + timedelta(hours=24)

def get_referral_count(user_id: int) -> int:
    row = get_user(user_id)
    if row is None:
        return 0
    return row["referral_count"] or 0

def can_withdraw_or_sell_free_gift(user_id: int) -> bool:
    return get_referral_count(user_id) >= 7

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
    cursor.execute('''
        SELECT * FROM withdraw_requests 
        WHERE status = 'pending' 
        ORDER BY created_at ASC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_withdraw_status(request_id: int, status: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE withdraw_requests 
        SET status = ?, processed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (status, request_id))
    conn.commit()
    conn.close()

# ---------- 6. ОБРАБОТЧИКИ КОМАНД БОТА ----------
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
    
    # Формируем ссылку на Mini App с реферальным кодом
    mini_app_link = f"https://t.me/{BOT_USERNAME}/app?startapp={message.from_user.id}"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть LAB NFT",
                    web_app=WebAppInfo(url=MINI_APP_URL + f"?startapp={message.from_user.id}")
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Поделиться ссылкой",
                    url=mini_app_link
                )
            ]
        ]
    )
    
    await message.answer(
        f"🧪 Добро пожаловать в LAB NFT!\n\n"
        f"Нажми кнопку ниже, чтобы открыть Mini App.\n"
        f"Или поделись ссылкой с друзьями:\n`{mini_app_link}`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    pending = get_pending_withdraws()
    
    if not pending:
        await message.answer("📭 Нет ожидающих заявок на вывод.")
        return

    text = "📋 **Ожидающие заявки на вывод:**\n\n"
    buttons = []
    
    for req in pending:
        text += (
            f"🆔 Заявка #{req['id']}\n"
            f"👤 @{req['username']}\n"
            f"🆔 User ID: {req['user_id']}\n"
            f"⭐ Сумма: {req['amount']}\n"
            f"📅 Создана: {req['created_at']}\n\n"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"✅ Выполнено #{req['id']}",
                callback_data=f"approve_{req['id']}"
            ),
            InlineKeyboardButton(
                text=f"❌ Отклонить #{req['id']}",
                callback_data=f"reject_{req['id']}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data.startswith("approve_") or c.data.startswith("reject_"))
async def process_withdraw_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет доступа.", show_alert=True)
        return

    action, request_id_str = callback.data.split("_")
    request_id = int(request_id_str)
    
    if action == "approve":
        status = "approved"
        await callback.message.answer(f"✅ Заявка #{request_id} одобрена.")
    else:
        status = "rejected"
        await callback.message.answer(f"❌ Заявка #{request_id} отклонена.")
    
    update_withdraw_status(request_id, status)
    await callback.answer()

# ---------- 7. ОБРАБОТКА ДАННЫХ ИЗ MINI APP ----------
@dp.message(types.ContentType.WEB_APP_DATA)
async def web_app_data_handler(message: types.Message):
    try:
        raw_data = message.web_app_data.data
        logger.info(f"Получены данные от пользователя {message.from_user.id}: {raw_data}")
        
        data = json.loads(raw_data)
        action = data.get("action")
        payload = data.get("payload", {})
        user_id = message.from_user.id
        
        if action == "app_open":
            referrer_id = payload.get("referrer_id", 0)
            create_user(user_id, referrer_id)
            
        elif action == "roulette_spin":
            prize = payload.get("prize", "неизвестно")
            balance = payload.get("balance", 0)
            is_free = payload.get("is_free", False)
            
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
                                f"👤 Пользователь прокрутил свою первую рулетку!\n"
                                f"📊 Всего рефералов: {get_referral_count(referrer_id)}/7"
                            )
                        except:
                            pass
            
            if is_free:
                update_free_spin(user_id)
            
        elif action == "sell_nft":
            name = payload.get("name", "NFT")
            price = payload.get("price", 0)
            is_free_gift = payload.get("is_free_gift", False)
            
            if is_free_gift and not can_withdraw_or_sell_free_gift(user_id):
                await message.answer(
                    f"❌ Вы не можете продать этот подарок!\n"
                    f"Пригласите 7 друзей. Ваш прогресс: {get_referral_count(user_id)}/7"
                )
                return
            
        elif action == "withdraw":
            username = payload.get("username", "unknown")
            amount = payload.get("amount", 25)
            
            if not can_withdraw_or_sell_free_gift(user_id):
                await message.answer(
                    f"❌ Вывод заблокирован! Пригласите 7 друзей. Ваш прогресс: {get_referral_count(user_id)}/7"
                )
                return
            
            save_withdraw_request(user_id, username, amount)
            
            if ADMIN_ID:
                admin_text = (
                    f"💸 **НОВАЯ ЗАЯВКА НА ВЫВОД**\n\n"
                    f"👤 Пользователь: @{username}\n"
                    f"🆔 ID: {user_id}\n"
                    f"⭐ Сумма: {amount} звёзд\n"
                    f"📅 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                try:
                    await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления админу: {e}")
            
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON: {e}")
    except Exception as e:
        logger.error(f"Ошибка обработки данных: {e}")

# ---------- 8. FASTAPI ДЛЯ API ----------
app = FastAPI(title="LAB NFT - Backend")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
@app.get("/index.html")
async def serve_index():
    return FileResponse("static/index.html")

# ---------- 9. API ЭНДПОИНТЫ ----------
@app.get("/api/user/{user_id}")
async def get_user_data(user_id: int):
    row = get_user(user_id)
    if row is None:
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
    prize_name = data.get("prize")
    prize_image = data.get("image", "")
    is_free = data.get("is_free", False)
    
    if not prize_name:
        raise HTTPException(status_code=400, detail="Не указан выигрыш")
    
    row = get_user(user_id)
    if row is None:
        create_user(user_id)
        row = get_user(user_id)
    
    inventory = json.loads(row["inventory"])
    balance = row["balance"]
    spins = row["spins"]
    upgrades = json.loads(row["upgrades"])
    
    first_spin_done = row["first_spin_done"] or 0
    referrer_id = row["referrer_id"] or 0
    
    if first_spin_done == 0 and referrer_id > 0:
        success = process_referral(referrer_id, user_id)
        mark_first_spin_done(user_id)
        if success:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 У вас новый реферал!\n"
                    f"👤 Пользователь прокрутил свою первую рулетку!\n"
                    f"📊 Всего рефералов: {get_referral_count(referrer_id)}/7"
                )
            except:
                pass
    elif first_spin_done == 0:
        mark_first_spin_done(user_id)
    
    if is_free:
        if not can_use_free_spin(user_id):
            raise HTTPException(status_code=400, detail="Бесплатная рулетка ещё не доступна")
        update_free_spin(user_id)
    else:
        if balance < 150:
            raise HTTPException(status_code=400, detail="Недостаточно звёзд")
        balance -= 150
    
    spins += 1
    
    new_gift = {
        "id": f"gift_{datetime.now().timestamp()}",
        "name": prize_name,
        "image": prize_image,
        "price": 0,
        "is_free": is_free
    }
    inventory.append(new_gift)
    
    save_user(user_id, inventory, balance, spins, upgrades)
    
    return {
        "success": True,
        "prize": new_gift,
        "new_balance": balance,
        "inventory": inventory,
        "can_free_spin": can_use_free_spin(user_id),
        "first_spin_done": 1
    }

@app.post("/api/user/{user_id}/upgrade")
async def upgrade_level(user_id: int, request: Request):
    data = await request.json()
    upgrade_id = data.get("upgrade_id")
    
    if not upgrade_id:
        raise HTTPException(status_code=400, detail="Не указан ID апгрейда")
    
    row = get_user(user_id)
    if row is None:
        create_user(user_id)
        row = get_user(user_id)
    
    inventory = json.loads(row["inventory"])
    balance = row["balance"]
    spins = row["spins"]
    upgrades = json.loads(row["upgrades"])
    
    upgrade = next((u for u in upgrades if u["id"] == upgrade_id), None)
    if not upgrade:
        raise HTTPException(status_code=404, detail="Апгрейд не найден")
    
    cost_map = {"up1": 150, "up2": 200, "up3": 100}
    max_lvl_map = {"up1": 5, "up2": 4, "up3": 6}
    
    cost = cost_map.get(upgrade_id, 150) * (upgrade["level"] + 1)
    max_lvl = max_lvl_map.get(upgrade_id, 5)
    
    if upgrade["level"] >= max_lvl:
        raise HTTPException(status_code=400, detail="Максимальный уровень достигнут")
    
    if balance < cost:
        raise HTTPException(status_code=400, detail="Недостаточно звёзд")
    
    balance -= cost
    upgrade["level"] += 1
    
    save_user(user_id, inventory, balance, spins, upgrades)
    
    return {
        "success": True,
        "upgrade": upgrade,
        "new_balance": balance
    }

@app.post("/api/withdraw/request")
async def create_withdraw_request(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    username = data.get("username")
    amount = data.get("amount", 25)
    
    if not user_id or not username:
        raise HTTPException(status_code=400, detail="Не указаны user_id или username")
    
    if not can_withdraw_or_sell_free_gift(user_id):
        raise HTTPException(status_code=403, detail="Вывод заблокирован. Пригласите 7 друзей.")
    
    save_withdraw_request(user_id, username, amount)
    
    return {
        "success": True,
        "message": f"Заявка на вывод {amount} ⭐ для @{username} создана"
    }

# ---------- 10. ЗАПУСК БОТА В ФОНОВОМ ПОТОКЕ ----------
def run_bot():
    """Запускает бота в режиме long polling."""
    logger.info("Бот запущен в режиме long polling")
    asyncio.run(dp.start_polling(bot))

# ---------- 11. ЗАПУСК FASTAPI ----------
if __name__ == "__main__":
    if ADMIN_ID == 0:
        logger.warning("⚠️ ADMIN_ID не установлен. Укажите его в .env файле.")
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем FastAPI
    uvicorn.run(app, host="0.0.0.0", port=8000)