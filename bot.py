"""
Telegram-бот для LAB NFT UPGRADER.
- /start → приветствие с картинкой welcome.png и кнопкой «ОТКРЫТЬ ИГРУ»
- Кнопка ведёт на Mini App
"""

import os
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ==================== КОНФИГ ====================
BOT_TOKEN    = os.getenv('BOT_TOKEN', '8756942487:AAFSt8_KlBMgbcsalOCnuz4KON1-HDrKnjo')
WEBAPP_URL   = os.getenv('WEBAPP_URL', 'https://judepip.github.io/NFT-LAB/')
WELCOME_IMG  = os.getenv('WELCOME_IMG', 'welcome.png')   # путь к файлу рядом с bot.py

# ==================== ЛОГИ ====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def make_open_keyboard():
    """Кнопка открытия Mini App."""
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎮 ОТКРЫТЬ ИГРУ",
        web_app=WebAppInfo(url=WEBAPP_URL)
    )
    return kb.as_markup()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Приветствие с картинкой и кнопкой запуска приложения."""

    caption = (
        f"👋 <b>Привет, {message.from_user.first_name or 'друг'}!</b>\n\n"
        "🎮 Добро пожаловать в <b>LAB NFT UPGRADER</b> — "
        "игру, где ты прокачиваешь свои NFT-подарки!\n\n"
        "👇 Нажми кнопку ниже, чтобы начать!"
    )

    try:
        photo = FSInputFile(WELCOME_IMG)
        await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=make_open_keyboard(),
            parse_mode='HTML'
        )
    except Exception as e:
        log.warning('Не удалось отправить фото (%s), отправляю только текст', e)
        await message.answer(
            caption,
            reply_markup=make_open_keyboard(),
            parse_mode='HTML'
        )


@dp.message(F.photo)
async def on_photo(message: types.Message):
    """Если пользователь прислал фото — отвечаем вежливо."""
    await message.answer(
        "📸 Картинка получена! Но чтобы играть — нажми кнопку ниже 👇",
        reply_markup=make_open_keyboard()
    )


@dp.message()
async def any_message(message: types.Message):
    """Ответ на любые другие сообщения."""
    await message.answer(
        "🎮 Открой приложение, чтобы начать играть:",
        reply_markup=make_open_keyboard()
    )


async def main():
    log.info('Бот запущен. WEBAPP_URL=%s', WEBAPP_URL)
    await dp.start_polling(bot)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())