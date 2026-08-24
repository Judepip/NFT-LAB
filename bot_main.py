import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import json

load_dotenv()

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- ИНИЦИАЛИЗАЦИЯ AIOGRAM ----------
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Отправляет кнопку для запуска Mini App."""
    web_app_url = os.getenv("MINI_APP_URL", "https://your-domain.com/miniapp")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть Mini App",
                    web_app=WebAppInfo(url=web_app_url)
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="stats"
                )
            ]
        ]
    )
    await message.answer(
        "Нажмите кнопку ниже, чтобы запустить встроенное приложение:",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data == "stats")
async def stats_callback(callback: types.CallbackQuery):
    """Пример обработки callback (не Mini App)."""
    await callback.answer("Статистика пока пуста", show_alert=True)

# ---------- ОБРАБОТКА ДАННЫХ ИЗ MINI APP (Web App Data) ----------
@dp.message(types.ContentType.WEB_APP_DATA)
async def web_app_data_handler(message: types.Message):
    """
    Получает JSON от Mini App через поле web_app_data.
    Данные приходят в message.web_app_data.data (строка).
    """
    try:
        raw_data = message.web_app_data.data
        logger.info(f"Получены данные из Mini App: {raw_data}")
        
        # Парсим JSON (ожидаем структуру { "action": "...", "payload": {...} })
        data = json.loads(raw_data)
        action = data.get("action", "unknown")
        payload = data.get("payload", {})
        
        # Логика обработки разных действий
        if action == "user_info":
            username = payload.get("username", "аноним")
            await message.answer(f"Привет, {username}! Данные получены.")
        elif action == "feedback":
            rating = payload.get("rating", 0)
            comment = payload.get("comment", "")
            await message.answer(f"Спасибо за оценку {rating}/5. Комментарий: '{comment}'")
        else:
            await message.answer(f"Действие '{action}' выполнено. Данные сохранены.")
            
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON: {e}")
        await message.answer("Ошибка формата данных. Отправьте корректный JSON.")
    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer("Внутренняя ошибка сервера.")

# ---------- FASTAPI ДЛЯ WEBHOOK И СТАТИКИ ----------
app = FastAPI(title="Telegram Mini App Backend")

# Монтируем статику (папка static) для HTML/JS/CSS
app.mount("/static", StaticFiles(directory="static"), name="static")

# Эндпоинт для самого Mini App (главная страница)
@app.get("/miniapp")
async def serve_miniapp():
    """Отдает HTML-страницу Mini App."""
    return FileResponse("static/miniapp.html")

# Эндпоинт для приема данных от Mini App напрямую (через fetch)
@app.post("/api/data")
async def receive_data(request: Request):
    """
    Альтернативный способ отправки данных из Mini App на сервер,
    минуя Telegram Bot API. Используется, когда нужно больше гибкости.
    """
    try:
        body = await request.json()
        logger.info(f"API данные: {body}")
        # Здесь можно записать в БД, отправить уведомление и т.д.
        return JSONResponse(content={"status": "ok", "received": body})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Эндпоинт для установки вебхука (вызвать один раз)
@app.get("/set_webhook")
async def set_webhook():
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        return JSONResponse({"error": "WEBHOOK_URL not set"}, status_code=400)
    
    # Удаляем старый вебхук и устанавливаем новый
    await bot.delete_webhook()
    result = await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    return JSONResponse({"webhook_set": result})

# ---------- ЗАПУСК (если файл выполняется напрямую) ----------
if __name__ == "__main__":
    # Запускаем сервер Uvicorn с FastAPI + встроенным ботом (вебхук)
    # Важно: dp и bot будут доступны через глобальные переменные
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))