"""LAB RELAYER — @labrelayer: приём NFT-подарков, вывод, рассылка, промокоды и управление заявками"""

import os, sys, json, asyncio, requests, urllib.parse, threading, tempfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from dotenv import load_dotenv
    load_dotenv()
    print('[RELAYER] .env загружен', flush=True)
except ImportError:
    print('[RELAYER] python-dotenv не установлен — работаем без .env', flush=True)

try:
    from pyrogram import Client, filters, enums
    from pyrogram.enums import MessageServiceType
    from pyrogram.errors import (
        UserIsBlocked, PeerIdInvalid, ChatWriteForbidden,
        FloodWait, MessageTooLong,
    )
    # LinkPreviewOptions отсутствует в pyrogram 2.0.106 — не используем
    # from pyrogram.types import LinkPreviewOptions
    print('[RELAYER] Pyrogram импортирован', flush=True)
except Exception as e:
    print(f'[RELAYER] ❌ Ошибка импорта Pyrogram: {e}', flush=True)
    sys.exit(1)


# ==================== КОНФИГ ====================

API_BASE = 'https://lab-nft-jude.mia0.amvera.tech'

TG_API_ID      = int(os.environ.get("TG_API_ID", "30067455"))
TG_API_HASH    = os.environ.get("TG_API_HASH", "1f7be014d3891c8824123256088a5d90")
TG_SESSION_STR = os.environ.get("TG_SESSION_STRING", "")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8756942487:AAFSt8_KlBMgbcsalOCnuz4KON1-HDrKnjo")

ADMIN_IDS = {1956581254, 8180548711}

BROADCAST_DELAY = 0.05
CAPTION_LIMIT   = 1024

print(f'[RELAYER] TG_API_ID={TG_API_ID}, session_len={len(TG_SESSION_STR)}', flush=True)
print(f'[RELAYER] BOT_TOKEN set: {bool(BOT_TOKEN)}', flush=True)

# ================================================================
# НАЗВАНИЕ ПОДАРКА → ЦЕНА В ЗВЁЗДАХ
# ================================================================
GIFT_NAME_TO_PRICE = {
    # --- Базовые ---
    "B-Day Candle": 633, "Berry Box": 859, "Big Year": 448,
    "Bow Tie": 600, "Chill Flame": 499, "Crystal Ball": 1215,
    "Desk Calendar": 575, "Diamond Ring": 2969, "Durov Cap": 42229,
    "Durov's Glasses": 8544,
    "Electric Skull": 2490, "Eternal Candle": 630, "Evil Eye": 759,
    "Fine Pen": 895, "Flying Broom": 1163, "Hanging Star": 974,
    "Hex Pot": 550, "Homemade Cake": 630, "Ion Gem": 7889,
    "Jack-In-The-Box": 500, "Kissed Frog": 3865, "Liberty Figure": 512,
    "Light Sword": 665, "Lol Pop": 490, "Love Candle": 963,
    "Low Rider": 5388, "Mad Pumpkin": 1259, "Mood Pak": 513,
    "Neko Helmet": 3775, "Party Sparkler": 545, "Plush Pepe": 553220,
    "Sakura Flower": 1025, "Scared Cat": 22489, "Signet Ring": 3239,
    "Skull Flower": 1125, "Snoop Cigar": 1430, "Snoop Dogg": 630,
    "Spy Agaric": 630, "StarNotepad": 550, "Top Hat": 1077,
    "Toy Bear": 3626, "Vintage Cigar": 3687, "Witch Hat": 629,

    # --- Новые подарки ---
    "Input Key":        681,     # ⚠️ поправь под актуальную цену
    "Happy Brownie":    540,     # ⚠️
    "Instant Ramen":    532,     # ⚠️
    "Jolly Chimp":      695,     # ⚠️
    "Khabib's Papakha": 2449,    # ⚠️
    "Swag Bag":         610,     # ⚠️
    "UFC Strike":       1564,     # ⚠️
}

# ================================================================
# СЛОВАРЬ gift_id (Telegram) → название подарка
# ================================================================
# Заполни, чтобы релеер мог отправлять подарки по заявкам.
# Способ 1: подарить @labrelayer подарок — в логах будет ID
#           ([GIFT] 🔑 KEY INFO: gift_id=NNNN → ...)
# Способ 2: --list-gifts (только на kurigram)
# ================================================================
GIFT_ID_TO_NAME = {
    # --- Базовые ---
    # 55555: "Big Year",
    # 55556: "B-Day Candle",
    # 55557: "Plush Pepe",

    # --- Новые (заполнить после получения ID) ---
    # NNNNN: "Input Key",
    # NNNNN: "Happy Brownie",
    # NNNNN: "Instant Ramen",
    # NNNNN: "Jolly Chimp",
    # NNNNN: "Khabib's Papakha",
    # NNNNN: "Swag Bag",
    # NNNNN: "UFC Strike",
}

BASE_IMG = 'https://judepip.github.io/NFT-LAB/images/'

def gift_image_url(name):
    return BASE_IMG + urllib.parse.quote(name) + '.gif'


# ==================== HEALTHCHECK ====================

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args, **kwargs):
        pass

def _run_health_server():
    port = int(os.environ.get('PORT', 80))
    try:
        server = HTTPServer(('0.0.0.0', port), _HealthHandler)
        print(f'[RELAYER] Healthcheck на :{port}', flush=True)
        server.serve_forever()
    except Exception as e:
        print(f'[RELAYER] ⚠️ Healthcheck не запустился: {e}', flush=True)

threading.Thread(target=_run_health_server, daemon=True).start()
print('[RELAYER] Healthcheck-поток запущен', flush=True)


# ==================== ПРОВЕРКА ====================

if not TG_SESSION_STR:
    print("❌ TG_SESSION_STRING не задан.", flush=True)
    sys.exit(1)

print('[RELAYER] Все проверки пройдены — создаём клиент', flush=True)

app = Client(
    ":memory:",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    session_string=TG_SESSION_STR,
)


# ==================== БЭКЕНД ====================

def backend_gift_received(user_id, gift_name, gift_price, gift_image, unique_id, gift_slug=None):
    try:
        r = requests.post(
            f'{API_BASE}/api/relayer/gift_received',
            json={
                'user_id': user_id,
                'gift_name': gift_name,
                'gift_price': gift_price,
                'gift_image': gift_image,
                'unique_id': str(unique_id),
                'gift_slug': gift_slug or '',
            },
            timeout=15
        )
        return r.json()
    except Exception as e:
        print(f'[RELAYER] ❌ backend error: {e}', flush=True)
        return {'ok': False}


def backend_get_pending_withdrawal(user_id):
    try:
        r = requests.get(
            f'{API_BASE}/api/relayer/pending_withdrawal',
            params={'user_id': user_id},
            timeout=15
        )
        d = r.json()
        return d.get('withdrawal') if d.get('ok') else None
    except Exception as e:
        print(f'[RELAYER] ❌ backend error: {e}', flush=True)
        return None


def backend_complete_withdrawal(withdrawal_id):
    try:
        r = requests.post(
            f'{API_BASE}/api/relayer/complete_withdrawal',
            json={'withdrawal_id': withdrawal_id},
            timeout=15
        )
        return r.json()
    except Exception as e:
        print(f'[RELAYER] ❌ backend error: {e}', flush=True)
        return {'ok': False}


def backend_rollback_withdrawal(withdrawal_id, reason='rollback'):
    """Откат заявки: подарок вернётся в инвентарь, 25⭐ вернутся."""
    try:
        r = requests.post(
            f'{API_BASE}/api/relayer/rollback_withdrawal',
            json={'withdrawal_id': withdrawal_id, 'reason': reason},
            timeout=15
        )
        return r.json()
    except Exception as e:
        print(f'[RELAYER] ❌ rollback error: {e}', flush=True)
        return {'ok': False}


def backend_cancel_withdrawal(withdrawal_id, reason='cancelled', refund=True):
    """Отмена заявки с возвратом подарка и 25⭐."""
    try:
        r = requests.post(
            f'{API_BASE}/api/relayer/cancel_withdrawal',
            json={'withdrawal_id': withdrawal_id, 'reason': reason, 'refund': refund},
            timeout=15
        )
        return r.json()
    except Exception as e:
        print(f'[RELAYER] ❌ cancel error: {e}', flush=True)
        return {'ok': False}


def backend_get_all_user_ids():
    try:
        r = requests.get(f'{API_BASE}/api/admin/all_users', timeout=20)
        d = r.json()
        if d.get('ok'):
            return d.get('users', [])
    except Exception as e:
        print(f'[RELAYER] ❌ backend all_users error: {e}', flush=True)
    return []


def backend_list_pending():
    """Список всех pending-заявок."""
    try:
        r = requests.get(f'{API_BASE}/api/admin/all_pending_withdrawals', timeout=20)
        d = r.json()
        if d.get('ok'):
            return d.get('withdrawals', [])
    except Exception as e:
        print(f'[WD] ❌ list error: {e}', flush=True)
    return []


# ==================== ПРИЁМ ПОДАРКОВ ====================

@app.on_message(filters.service)
async def handle_gift(client, message):
    if message.service not in (MessageServiceType.STAR_GIFT,
                                MessageServiceType.STAR_GIFT_UNIQUE):
        return

    sender = message.from_user
    if not sender:
        print('[GIFT] ⚠️ Анонимный подарок — пропуск', flush=True)
        return

    sender_id = sender.id
    gift = getattr(message, 'gift', None) or getattr(message, 'star_gift', None)
    if not gift:
        print('[GIFT] ⚠️ Не удалось извлечь данные подарка', flush=True)
        return

    title = getattr(gift, 'title', None)
    gift_id_raw = getattr(gift, 'id', None)
    base_gift_id = getattr(gift, 'gift_id', None) or gift_id_raw
    slug = getattr(gift, 'slug', None)

    if title:
        gift_name = title
        unique_id = f'uniq:{gift_id_raw}:{message.id}'
        print(f'[GIFT] Коллекционный: "{gift_name}"', flush=True)
    else:
        gift_name = GIFT_ID_TO_NAME.get(base_gift_id, f'Подарок #{base_gift_id}')
        unique_id = f'msg:{message.id}'
        print(f'[GIFT] Обычный: ID={base_gift_id}, message_id={message.id}', flush=True)

    price = GIFT_NAME_TO_PRICE.get(gift_name, 0)
    if price == 0:
        print(f'[GIFT] ⚠️ Неизвестный подарок "{gift_name}" — нет цены', flush=True)
        print(f'[GIFT] 🔑 KEY INFO: gift_id={base_gift_id} → '
              f'добавь в GIFT_ID_TO_NAME: {base_gift_id}: "{gift_name}"', flush=True)
        print(f'[GIFT] DEBUG: title={title}, id={gift_id_raw}, base={base_gift_id}, slug={slug}', flush=True)
        return

    resp = backend_gift_received(
        user_id=sender_id,
        gift_name=gift_name,
        gift_price=price,
        gift_image=gift_image_url(gift_name),
        unique_id=unique_id,
        gift_slug=slug
    )
    if resp.get('ok'):
        print(f'[GIFT] ✅ "{gift_name}" ({price}⭐) → user {sender_id}', flush=True)
    else:
        print(f'[GIFT] ❌ backend отказал: {resp}', flush=True)


# ==================== ОТПРАВКА ПО ЗАЯВКАМ ====================

@app.on_message(filters.private & filters.incoming & ~filters.service)
async def handle_withdraw(client, message):
    user = message.from_user
    if not user:
        return
    if message.outgoing or user.is_bot:
        return

    text = (message.text or message.caption or "").strip()
    if text.startswith('/'):
        return

    user_id = user.id
    print(f'[WITHDRAW] Сообщение от {user_id}: {text[:60]}', flush=True)

    w = backend_get_pending_withdrawal(user_id)
    if not w:
        return

    gift_name = w.get('gift_name')
    if not gift_name:
        backend_rollback_withdrawal(w['id'], reason='empty gift_name')
        await message.reply("❌ Ошибка: заявка без названия подарка.\nПодарок и звёзды возвращены.")
        return

    gift_id = None
    for gid, gname in GIFT_ID_TO_NAME.items():
        if gname == gift_name:
            gift_id = gid
            break

    if not gift_id:
        print(f'[WITHDRAW] ❌ Нет gift_id для "{gift_name}" — откат', flush=True)
        backend_rollback_withdrawal(w['id'], reason=f'gift not found: {gift_name}')
        await message.reply(
            f"❌ <b>Данный подарок не существует</b>: <code>{gift_name}</code>\n\n"
            f"🎁 Подарок возвращён в инвентарь\n"
            f"⭐ 25 звёзд за вывод возвращены",
            parse_mode=enums.ParseMode.HTML
        )
        return

    try:
        await client.send_gift(
            chat_id=user_id,
            gift_id=gift_id,
            text=f"🎁 Вывод #{w['id']}"
        )
        backend_complete_withdrawal(w['id'])
        await message.reply(f"✅ Отправлено: {gift_name}")
        print(f'[WITHDRAW] ✅ Вывод #{w["id"]} → {user_id} ({gift_name})', flush=True)
    except Exception as e:
        err = str(e)[:200]
        print(f'[WITHDRAW] ❌ Ошибка отправки: {err}', flush=True)
        backend_rollback_withdrawal(w['id'], reason=f'send failed: {err}')
        await message.reply(
            f"❌ Не удалось отправить подарок.\n\n"
            f"🎁 Подарок возвращён в инвентарь\n"
            f"⭐ 25 звёзд за вывод возвращены\n\n"
            f"Причина: <code>{err[:100]}</code>",
            parse_mode=enums.ParseMode.HTML
        )


# ==================== УПРАВЛЕНИЕ ЗАЯВКАМИ (АДМИН) ====================

WD_HELP = (
    "📋 <b>Управление заявками на вывод</b>\n\n"
    "<b>Список всех pending-заявок:</b>\n"
    "<code>/wd list</code>\n\n"
    "<b>Инфо по заявке:</b>\n"
    "<code>/wd info ID</code>\n\n"
    "<b>Отметить выполненной (без отправки):</b>\n"
    "<code>/wd complete ID</code>\n\n"
    "<b>Отменить (вернуть подарок и 25⭐):</b>\n"
    "<code>/wd cancel ID [причина]</code>\n\n"
    "<b>Откатить (то же, что cancel):</b>\n"
    "<code>/wd rollback ID</code>\n\n"
    "<i>При отмене/откате подарок возвращается в инвентарь юзера,\n"
    "а 25⭐ за вывод — на его баланс.</i>"
)


@app.on_message(filters.command("wd") & filters.private)
async def wd_handler(client, message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Команда только для админов.")
        return

    parts = (message.text or "").strip().split(maxsplit=3)
    if len(parts) < 2:
        await message.reply(WD_HELP, parse_mode=enums.ParseMode.HTML)
        return

    action = parts[1].lower()

    if action == 'help':
        await message.reply(WD_HELP, parse_mode=enums.ParseMode.HTML)
        return

    if action == 'list':
        status = await message.reply("📋 Загружаю список заявок...")
        items = backend_list_pending()
        if not items:
            await status.edit_text("📭 Нет активных заявок на вывод.")
            return
        lines = [f"📋 <b>Pending-заявки</b> ({len(items)}):\n"]
        for it in items[:50]:
            wid = it.get('id')
            uid = it.get('user_id')
            gname = it.get('gift_name', '?')
            gprice = it.get('gift_price', 0)
            created = (it.get('created_at') or '')[:16]
            lines.append(
                f"<code>#{wid}</code> · user <code>{uid}</code>\n"
                f"   🎁 {gname} (⭐{gprice}) · {created}"
            )
        text = "\n".join(lines)
        if len(items) > 50:
            text += f"\n\n…и ещё {len(items) - 50} шт."
        text += "\n\n<i>/wd info ID · /wd cancel ID · /wd complete ID</i>"
        await status.edit_text(text, parse_mode=enums.ParseMode.HTML)
        return

    if action == 'info':
        if len(parts) < 3:
            await message.reply("❌ /wd info ID")
            return
        try:
            wid = int(parts[2])
        except ValueError:
            await message.reply("❌ ID должен быть числом")
            return
        items = backend_list_pending()
        target = next((x for x in items if x.get('id') == wid), None)
        if not target:
            await message.reply(f"❌ Заявка #{wid} не найдена (или уже не pending)")
            return
        text = (
            f"📋 <b>Заявка #{wid}</b>\n\n"
            f"👤 user_id: <code>{target.get('user_id')}</code>\n"
            f"🎁 подарок: <b>{target.get('gift_name', '?')}</b>\n"
            f"💎 цена: ⭐{target.get('gift_price', 0)}\n"
            f"🕒 создана: {target.get('created_at', '?')}\n"
        )
        await message.reply(text, parse_mode=enums.ParseMode.HTML)
        return

    if action == 'complete':
        if len(parts) < 3:
            await message.reply("❌ /wd complete ID")
            return
        try:
            wid = int(parts[2])
        except ValueError:
            await message.reply("❌ ID должен быть числом")
            return
        d = backend_complete_withdrawal(wid)
        if d.get('ok'):
            await message.reply(f"✅ Заявка #{wid} отмечена выполненной.")
            print(f'[WD] ✅ complete #{wid}', flush=True)
        else:
            await message.reply(f"❌ Ошибка: {d.get('error', 'unknown')}")
        return

    if action == 'cancel':
        if len(parts) < 3:
            await message.reply("❌ /wd cancel ID [причина]")
            return
        try:
            wid = int(parts[2])
        except ValueError:
            await message.reply("❌ ID должен быть числом")
            return
        reason = parts[3] if len(parts) >= 4 else 'manual cancel by admin'
        d = backend_cancel_withdrawal(wid, reason=reason, refund=True)
        if d.get('ok'):
            await message.reply(
                f"↩️ Заявка #{wid} отменена.\n"
                f"🎁 Подарок возвращён в инвентарь.\n"
                f"⭐ 25 звёзд возвращены.\n"
                f"📝 Причина: {reason}"
            )
            print(f'[WD] ↩️ cancel #{wid} ({reason})', flush=True)
        else:
            await message.reply(f"❌ Ошибка: {d.get('error', 'unknown')}")
        return

    if action == 'rollback':
        if len(parts) < 3:
            await message.reply("❌ /wd rollback ID")
            return
        try:
            wid = int(parts[2])
        except ValueError:
            await message.reply("❌ ID должен быть числом")
            return
        d = backend_rollback_withdrawal(wid, reason='manual rollback by admin')
        if d.get('ok'):
            await message.reply(
                f"↩️ Заявка #{wid} откатана.\n"
                f"🎁 Подарок возвращён в инвентарь.\n"
                f"⭐ 25 звёзд возвращены."
            )
            print(f'[WD] ↩️ rollback #{wid}', flush=True)
        else:
            await message.reply(f"❌ Ошибка: {d.get('error', 'unknown')}")
        return

    await message.reply(WD_HELP, parse_mode=enums.ParseMode.HTML)


# ==================== РАССЫЛКА ====================

def tg_api(method: str, payload: dict, timeout: int = 15):
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',
            json=payload,
            timeout=timeout
        )
        return r.json()
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def upload_photo_to_bot(file_path: str, chat_id: int):
    try:
        with open(file_path, 'rb') as f:
            r = requests.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto',
                data={'chat_id': chat_id},
                files={'photo': f},
                timeout=60
            )
        d = r.json()
        if d.get('ok'):
            photos = d['result'].get('photo', [])
            if photos:
                return photos[-1]['file_id']
        print(f"[BROADCAST] ❌ upload_photo_to_bot: {d}", flush=True)
        return None
    except Exception as e:
        print(f"[BROADCAST] ❌ upload_photo_to_bot exception: {e}", flush=True)
        return None


@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client, message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Команда только для админов.")
        return

    raw = message.text or message.caption or ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "❌ <b>Как использовать:</b>\n\n"
            "• Отправь <b>картинку</b> с подписью:\n"
            "  <code>/broadcast Привет, тест!</code>\n\n"
            "• Или просто текст:\n"
            "  <code>/broadcast Привет, тест!</code>",
            parse_mode=enums.ParseMode.HTML
        )
        return

    broadcast_text = parts[1].strip()

    photo_msg = None
    if message.photo:
        photo_msg = message
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo_msg = message

    bot_file_id = None
    tmp_path = None

    if photo_msg:
        try:
            prep_msg = await message.reply("🖼 Готовлю картинку для @lab_game_bot...")
            tmp_dir = tempfile.gettempdir()
            tmp_path = await photo_msg.download(
                file_name=os.path.join(tmp_dir, f"bc_{message.id}")
            )
            bot_file_id = upload_photo_to_bot(tmp_path, message.from_user.id)

            if bot_file_id:
                print(f"[BROADCAST] ✅ Получен новый file_id: {bot_file_id[:40]}...", flush=True)
                try:
                    await prep_msg.edit_text("✅ Картинка готова, начинаю рассылку...")
                except Exception:
                    pass
            else:
                await prep_msg.edit_text(
                    "⚠️ Не удалось загрузить картинку.\nПроверь, что нажал /start у @lab_game_bot.\n"
                    "Рассылка пойдёт только текстом."
                )
        except Exception as e:
            print(f"[BROADCAST] ❌ download error: {e}", flush=True)
            await message.reply(f"⚠️ Ошибка подготовки картинки: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    user_ids = backend_get_all_user_ids()
    if not user_ids:
        await message.reply("⚠️ Нет пользователей для рассылки.")
        return

    has_photo = bool(bot_file_id)
    status_msg = await message.reply(
        f"📣 Начинаю рассылку на <b>{len(user_ids)}</b> юзеров...\n"
        f"Отправитель: <b>🤖 @lab_game_bot</b>\n"
        f"{'🖼 с картинкой' if has_photo else '📝 только текст'}",
        parse_mode=enums.ParseMode.HTML
    )

    ok, fail, blocked = 0, 0, 0
    fail_reasons = {}

    for i, uid in enumerate(user_ids, 1):
        try:
            if has_photo:
                if len(broadcast_text) <= CAPTION_LIMIT:
                    resp = tg_api('sendPhoto', {
                        'chat_id': uid,
                        'photo': bot_file_id,
                        'caption': broadcast_text,
                        'parse_mode': 'HTML',
                    })
                else:
                    tg_api('sendPhoto', {'chat_id': uid, 'photo': bot_file_id})
                    resp = tg_api('sendMessage', {
                        'chat_id': uid,
                        'text': broadcast_text,
                        'parse_mode': 'HTML',
                        'link_preview_options': {'is_disabled': True},
                    })
            else:
                resp = tg_api('sendMessage', {
                    'chat_id': uid,
                    'text': broadcast_text,
                    'parse_mode': 'HTML',
                    'link_preview_options': {'is_disabled': True},
                })

            if resp.get('ok'):
                ok += 1
            else:
                desc = resp.get('description', '') or str(resp.get('error', ''))
                if 'blocked' in desc.lower():
                    blocked += 1
                else:
                    fail += 1
                    key = desc.split(':')[-1].strip() if ':' in desc else desc
                    fail_reasons[key] = fail_reasons.get(key, 0) + 1
                    print(f"[BROADCAST] ❌ {uid}: {desc}", flush=True)

        except Exception as e:
            fail += 1
            key = type(e).__name__
            fail_reasons[key] = fail_reasons.get(key, 0) + 1
            print(f"[BROADCAST] ❌ {uid}: {type(e).__name__}: {e}", flush=True)

        if i % 25 == 0:
            try:
                await status_msg.edit_text(
                    f"📣 Рассылка... {i}/{len(user_ids)}\n"
                    f"✅ ok: {ok}  ❌ fail: {fail}  ⛔ blocked: {blocked}",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await asyncio.sleep(BROADCAST_DELAY)

    report = (
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"👥 Всего: <b>{len(user_ids)}</b>\n"
        f"✅ Доставлено: <b>{ok}</b>\n"
        f"❌ Ошибок: <b>{fail}</b>\n"
        f"⛔ Заблокировали: <b>{blocked}</b>"
    )
    if fail_reasons:
        report += "\n\n<b>Причины ошибок:</b>\n"
        for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1])[:5]:
            report += f"• {reason}: {count}\n"

    await status_msg.edit_text(report, parse_mode=enums.ParseMode.HTML)
    print(f"[BROADCAST] ✅ done ok={ok} fail={fail} blocked={blocked} reasons={fail_reasons}", flush=True)


# ==================== ПРОМОКОДЫ ====================

PROMO_HELP = (
    "🎟 <b>Управление промокодами</b>\n\n"
    "<b>Создать на звёзды:</b>\n"
    "<code>/promo create CODE stars СУММА ЛИМИТ</code>\n"
    "Пример: <code>/promo create NIKITAPUPS stars 50000 50</code>\n\n"
    "<b>Создать на буст:</b>\n"
    "<code>/promo create CODE boost ПРОЦЕНТ ЛИМИТ [once|time] [ЧАСЫ]</code>\n"
    "Пример: <code>/promo create BOOST25 boost 25 100 time 24</code>\n\n"
    "<b>Список всех:</b>\n"
    "<code>/promo list</code>\n\n"
    "<b>Удалить:</b>\n"
    "<code>/promo delete CODE</code>\n"
)


def promo_api_create(admin_id, payload):
    payload = dict(payload)
    payload['user_id'] = admin_id
    url = f'{API_BASE}/api/admin/promo/create'
    try:
        r = requests.post(url, json=payload, timeout=20)
        print(f'[PROMO] POST {url} -> {r.status_code}', flush=True)
        try:
            return r.json()
        except Exception:
            return {'ok': False, 'error': f'not json: {r.status_code} {r.text[:200]}'}
    except Exception as e:
        return {'ok': False, 'error': f'network: {e}'}


def promo_api_list(admin_id):
    try:
        r = requests.get(f'{API_BASE}/api/admin/promo/list', params={'user_id': admin_id}, timeout=20)
        try:
            return r.json()
        except Exception:
            return {'ok': False, 'error': f'not json: {r.status_code} {r.text[:200]}'}
    except Exception as e:
        return {'ok': False, 'error': f'network: {e}'}


def promo_api_delete(admin_id, code):
    try:
        r = requests.post(f'{API_BASE}/api/admin/promo/delete',
                          json={'user_id': admin_id, 'code': code}, timeout=20)
        try:
            return r.json()
        except Exception:
            return {'ok': False, 'error': f'not json: {r.status_code} {r.text[:200]}'}
    except Exception as e:
        return {'ok': False, 'error': f'network: {e}'}


@app.on_message(filters.command("promo") & filters.private)
async def promo_handler(client, message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("⛔ Команда только для админов.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.reply(PROMO_HELP, parse_mode=enums.ParseMode.HTML)
        return

    action = parts[1].lower()

    if action == 'help':
        await message.reply(PROMO_HELP, parse_mode=enums.ParseMode.HTML)
        return

    if action == 'list':
        status = await message.reply("📋 Загружаю список...")
        d = promo_api_list(message.from_user.id)
        if not d.get('ok'):
            await status.edit_text(f"❌ Ошибка: {d.get('error', 'unknown')}")
            return
        items = d.get('items', [])
        if not items:
            await status.edit_text("📭 Промокодов пока нет.")
            return
        lines = [f"🎟 <b>Промокоды</b> ({len(items)}):\n"]
        for it in items[:50]:
            code = it.get('code', '?')
            ptype = it.get('type', '?')
            uses = it.get('uses', 0)
            mx = it.get('max_uses', 0)
            exp = it.get('expires_at') or '—'
            if ptype == 'stars':
                detail = f"⭐ {it.get('value', 0)}"
            elif ptype == 'gift':
                detail = f"🎁 {it.get('gift_name') or '?'}"
            elif ptype == 'boost':
                detail = f"🚀 +{it.get('bonus_percent', 0)}% ({it.get('boost_type', '?')})"
            else:
                detail = ptype
            lim = f"{uses}/{mx}" if mx > 0 else f"{uses}/∞"
            lines.append(f"<code>{code}</code> · {detail} · {lim} · до {exp}")
        text = "\n".join(lines)
        if len(items) > 50:
            text += f"\n\n…и ещё {len(items) - 50} шт."
        await status.edit_text(text, parse_mode=enums.ParseMode.HTML)
        return

    if action == 'delete':
        if len(parts) < 3:
            await message.reply("❌ Использование: <code>/promo delete CODE</code>", parse_mode=enums.ParseMode.HTML)
            return
        code = parts[2].strip().upper()
        d = promo_api_delete(message.from_user.id, code)
        if d.get('ok'):
            await message.reply(f"✅ Промокод <code>{code}</code> удалён.", parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply(f"❌ Ошибка: {d.get('error', 'unknown')}")
        return

    if action == 'create':
        if len(parts) < 6:
            await message.reply(
                "❌ Использование:\n"
                "<code>/promo create CODE stars СУММА ЛИМИТ</code>\n"
                "<code>/promo create CODE boost ПРОЦЕНТ ЛИМИТ [once|time] [ЧАСЫ]</code>",
                parse_mode=enums.ParseMode.HTML
            )
            return

        code = parts[2].strip().upper()
        ptype = parts[3].strip().lower()

        try:
            value = int(parts[4])
            max_uses = int(parts[5])
        except ValueError:
            await message.reply("❌ VALUE и ЛИМИТ должны быть числами.")
            return

        payload = {'code': code, 'type': ptype, 'value': value, 'max_uses': max_uses}

        if ptype == 'gift':
            await message.reply("⚠️ gift-промокоды через Telegram не поддерживаются. Используй curl.")
            return

        if ptype == 'boost':
            payload['bonus_percent'] = value
            payload['boost_type'] = parts[6].strip().lower() if len(parts) >= 7 else 'once'
            if len(parts) >= 8:
                try:
                    payload['boost_hours'] = int(parts[7])
                except ValueError:
                    payload['boost_hours'] = 24

        d = promo_api_create(message.from_user.id, payload)
        if d.get('ok'):
            extra = ""
            if ptype == 'stars':
                extra = f"⭐ {value}"
            elif ptype == 'boost':
                extra = f"🚀 +{payload.get('bonus_percent', value)}% ({payload.get('boost_type', 'once')})"
            lim = f"{max_uses}" if max_uses > 0 else "∞"
            await message.reply(
                f"✅ Промокод <code>{code}</code> создан\nТип: {ptype} {extra}\nЛимит активаций: {lim}",
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await message.reply(f"❌ Ошибка: {d.get('error', 'unknown')}")
        return

    await message.reply(PROMO_HELP, parse_mode=enums.ParseMode.HTML)


# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    print('[RELAYER] Запуск @labrelayer...', flush=True)
    app.run()