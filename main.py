"""LAB NFT UPGRADER — Flask + TON watcher (точка входа для Amvera)"""

import os, time, json, base64, sqlite3, threading, secrets
from datetime import datetime, timedelta
import requests
from flask import Flask, request, jsonify

# ==================== КОНФИГ ====================

DATA_DIR = '/data' if os.path.isdir('/data') else os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'lab_nft.db')

RECIPIENT_UQ  = 'UQBhNenZ50ac9WskDqQGeajDC62-RoRwqO961LGRdu3Dml3i'
RECIPIENT_RAW = '0:6135e9d9e7469cf56b240ea40679a8c30badbe468470a8ef7ad4b19176edc39a'

TON_API          = f'https://tonapi.io/v2/accounts/{RECIPIENT_UQ}/events'
WATCH_INTERVAL   = 30
STARS_TO_TON     = 0.011
MIN_WITHDRAW     = 500
MAX_WITHDRAW_DAY = 10000

ADMIN_IDS = {1956581254, 8180548711}
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8756942487:AAFSt8_KlBMgbcsalOCnuz4KON1-HDrKnjo')
CHANNEL_ID = '@labupgrader'

BROADCAST_KEY = os.environ.get('BROADCAST_KEY', '')

BASE_IMG = 'https://judepip.github.io/NFT-LAB/images/'

REFERRAL_REWARD = 1

# Сколько часов подарок "остывает" после получения (нельзя выводить)
GIFT_COOLDOWN_HOURS = 168   # 7 дней

# ================================================================
# СПРАВОЧНИК ПОДАРКОВ (название → цена в звёздах)
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
    "Input Key":        681,
    "Happy Brownie":    540,
    "Instant Ramen":    532,
    "Jolly Chimp":      695,
    "Khabib's Papakha": 2449,
    "Swag Bag":         610,
    "UFC Strike":       1564,
}


# ==================== БД ====================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _table_columns(conn, table):
    try:
        rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                balance INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                type TEXT NOT NULL, amount_ton REAL DEFAULT 0, amount_stars INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success', tx_hash TEXT, meta TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS ton_processed (
                tx_hash TEXT PRIMARY KEY, user_id INTEGER,
                processed_at TEXT DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, inventory_id INTEGER,
                gift_name TEXT, gift_price INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_wd_user ON withdrawals(user_id, status);

            CREATE TABLE IF NOT EXISTS user_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, gift_name TEXT NOT NULL,
                gift_price INTEGER NOT NULL, gift_image TEXT,
                gift_unique_id TEXT, gift_slug TEXT,
                source TEXT DEFAULT 'promo',
                status TEXT DEFAULT 'in_stock',
                available_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_inv_user ON user_inventory(user_id, status);

            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, type TEXT NOT NULL, value INTEGER DEFAULT 0,
                gift_name TEXT, gift_image TEXT, gift_price INTEGER DEFAULT 0,
                bonus_percent INTEGER DEFAULT 0, boost_type TEXT, boost_hours INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
                expires_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS promo_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, user_id));

            CREATE TABLE IF NOT EXISTS user_boosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                bonus_percent INTEGER NOT NULL, boost_type TEXT NOT NULL,
                uses_left INTEGER DEFAULT 1, expires_at TEXT, code TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_boost_user ON user_boosts(user_id, expires_at);

            CREATE TABLE IF NOT EXISTS partners (
                user_id INTEGER PRIMARY KEY,
                note TEXT,
                added_by INTEGER,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                reward_stars INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);
        ''')

        # ==================== МИГРАЦИИ ====================
        user_cols = _table_columns(conn, 'users')
        for col, ddl in [
            ('referral_code', 'TEXT'),
            ('referred_by', 'INTEGER'),
            ('username', 'TEXT'),
            ('first_name', 'TEXT'),
            ('balance', 'INTEGER DEFAULT 0'),
        ]:
            if col not in user_cols:
                try:
                    conn.execute(f'ALTER TABLE users ADD COLUMN {col} {ddl}')
                    print(f'[DB] migration: added users.{col}', flush=True)
                except Exception as e:
                    print(f'[DB] migration {col} error: {e}', flush=True)

        inv_cols = _table_columns(conn, 'user_inventory')
        if 'available_at' not in inv_cols:
            try:
                conn.execute('ALTER TABLE user_inventory ADD COLUMN available_at TEXT')
                print('[DB] migration: added user_inventory.available_at', flush=True)
            except Exception as e:
                print(f'[DB] migration available_at error: {e}', flush=True)

        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_refcode ON users(referral_code)')
        except Exception as e:
            print(f'[DB] index idx_users_refcode error: {e}', flush=True)

        try:
            rows = conn.execute(
                "SELECT user_id FROM users WHERE referral_code IS NULL OR referral_code = ''"
            ).fetchall()
            for r in rows:
                new_code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]
                conn.execute('UPDATE users SET referral_code=? WHERE user_id=?', (new_code, r['user_id']))
            if rows:
                print(f'[DB] backfilled referral_code for {len(rows)} users', flush=True)
        except Exception as e:
            print(f'[DB] backfill referral_code error: {e}', flush=True)

        conn.commit()
    print(f'[DB] Таблицы готовы, путь: {DB_PATH}', flush=True)


def ensure_user(conn, user_id):
    row = conn.execute('SELECT user_id FROM users WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]
        try:
            conn.execute('INSERT INTO users (user_id, referral_code) VALUES (?, ?)', (user_id, code))
        except sqlite3.OperationalError as e:
            print(f'[DB] ensure_user insert error: {e}', flush=True)
            conn.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))

    row = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
    if row is None:
        return None
    keys = row.keys()
    if 'referral_code' in keys:
        try:
            if not row['referral_code']:
                code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]
                conn.execute('UPDATE users SET referral_code=? WHERE user_id=?', (code, user_id))
                row = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
        except Exception:
            pass
    return row


def log_tx(conn, user_id, tx_type, amount_stars=0, amount_ton=0.0,
           status='success', tx_hash=None, meta=None):
    conn.execute('''INSERT INTO transactions (user_id, type, amount_ton, amount_stars, status, tx_hash, meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (user_id, tx_type, amount_ton, amount_stars, status, tx_hash,
                  json.dumps(meta or {}, ensure_ascii=False)))


def already_processed(tx_hash):
    with get_db() as conn:
        return conn.execute('SELECT 1 FROM ton_processed WHERE tx_hash=?', (tx_hash,)).fetchone() is not None


def mark_processed(tx_hash, user_id):
    with get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO ton_processed (tx_hash, user_id) VALUES (?, ?)',
                     (tx_hash, user_id))
        conn.commit()


def gift_cooldown_until():
    return (datetime.utcnow() + timedelta(hours=GIFT_COOLDOWN_HOURS)).strftime('%Y-%m-%d %H:%M:%S')


# ==================== АДРЕС ====================

def is_our_address(addr_str):
    if not addr_str:
        return False
    if addr_str == RECIPIENT_RAW or addr_str == RECIPIENT_UQ:
        return True
    try:
        from tonsdk.utils import Address
        a = Address(addr_str)
        uq = a.to_string(is_user_friendly=True, is_bounceable=False)
        eq = a.to_string(is_user_friendly=True, is_bounceable=True)
        raw = a.to_string(is_user_friendly=False)
        return (uq == RECIPIENT_UQ or eq == RECIPIENT_UQ or
                raw == RECIPIENT_RAW or raw == RECIPIENT_UQ)
    except Exception:
        return False


def decode_ton_comment(b64_or_text):
    if not b64_or_text:
        return None
    if b64_or_text.startswith('u:'):
        return b64_or_text
    try:
        from tonsdk.boc import Cell
        from tonsdk.utils import b64str_to_bytes
        cell = Cell.one_from_boc(b64str_to_bytes(b64_or_text))
        cs = cell.begin_parse()
        cs.load_uint(32)
        text = cs.load_string_tail()
        if text:
            return text
    except Exception:
        pass
    try:
        raw = base64.b64decode(b64_or_text)
        idx = raw.find(b'u:')
        if idx == -1:
            return None
        end = idx
        while end < len(raw) and 32 <= raw[end] <= 126:
            end += 1
        return raw[idx:end].decode('ascii')
    except Exception:
        return None


# ==================== БУСТЫ ====================

def get_active_boost(conn, user_id):
    now = datetime.utcnow()
    conn.execute('''
        DELETE FROM user_boosts
        WHERE boost_type='time' AND expires_at IS NOT NULL AND expires_at < ?
    ''', (now.strftime('%Y-%m-%d %H:%M:%S'),))

    once = conn.execute('''
        SELECT * FROM user_boosts
        WHERE user_id=? AND boost_type='once' AND uses_left > 0
        ORDER BY id ASC LIMIT 1
    ''', (user_id,)).fetchone()
    if once:
        return once

    time_boost = conn.execute('''
        SELECT * FROM user_boosts
        WHERE user_id=? AND boost_type='time'
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY id ASC LIMIT 1
    ''', (user_id, now.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    return time_boost


def apply_boost(conn, user_id, stars):
    boost = get_active_boost(conn, user_id)
    if not boost:
        return stars, 0, None
    percent = boost['bonus_percent']
    extra = int(stars * percent / 100)
    total = stars + extra
    if boost['boost_type'] == 'once':
        conn.execute('UPDATE user_boosts SET uses_left = uses_left - 1 WHERE id=?', (boost['id'],))
    return total, percent, boost


# ==================== TON WATCHER ====================

def check_transactions():
    try:
        r = requests.get(TON_API, params={'limit': 30}, timeout=15)
        if r.status_code != 200:
            print(f'[TON] API status {r.status_code}', flush=True)
            return
        events = r.json().get('events', [])
        for event in events:
            for action in event.get('actions', []):
                if action.get('type') != 'TonTransfer':
                    continue
                tr = action.get('TonTransfer', {})
                recipient_addr = tr.get('recipient', {}).get('address', '')
                if not is_our_address(recipient_addr):
                    continue
                raw_comment = (tr.get('comment') or '').strip()
                comment = decode_ton_comment(raw_comment)
                if not comment or not comment.startswith('u:'):
                    continue
                parts = comment.split(':')
                if len(parts) != 4 or parts[0] != 'u' or parts[2] != 's':
                    continue
                try:
                    user_id = int(parts[1]); stars = int(parts[3])
                except ValueError:
                    continue
                tx_hash = event.get('event_id') or str(event.get('lt', ''))
                if not tx_hash or already_processed(tx_hash):
                    continue
                amount_ton = tr.get('amount', 0) / 1e9
                with get_db() as conn:
                    ensure_user(conn, user_id)
                    final_stars, bonus_percent, boost = apply_boost(conn, user_id, stars)
                    conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (final_stars, user_id))
                    log_tx(conn, user_id, 'topup', amount_stars=final_stars, amount_ton=amount_ton,
                           tx_hash=tx_hash,
                           meta={'comment': comment, 'base_stars': stars,
                                 'bonus_percent': bonus_percent,
                                 'bonus_stars': final_stars - stars})
                    conn.commit()
                mark_processed(tx_hash, user_id)
                print(f'[TON] ✅ +{final_stars}⭐ → user {user_id}', flush=True)
    except Exception as e:
        print(f'[TON] error: {e}', flush=True)


def watcher_loop():
    print('[TON] watcher запущен', flush=True)
    while True:
        try:
            check_transactions()
        except Exception as e:
            print(f'[TON] loop error: {e}', flush=True)
        time.sleep(WATCH_INTERVAL)


# ==================== FLASK ====================

app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Admin-Key'
    return resp


@app.route('/')
def root():
    return jsonify({'ok': True, 'service': 'LAB NFT UPGRADER', 'version': '3.0'})


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'time': datetime.utcnow().isoformat(),
        'db': DB_PATH,
        'tg_client': False,
    })


# ==================== СПИСОК ПОДАРКОВ ====================

@app.route('/api/gifts/list')
def api_gifts_list():
    """
    Справочник подарков: имя, цена, URL картинки.
    Полезно для динамического обновления клиента и валидации.
    """
    import urllib.parse as _up
    items = []
    for name, price in GIFT_NAME_TO_PRICE.items():
        items.append({
            'name': name,
            'price': price,
            'image': BASE_IMG + _up.quote(name) + '.gif',
        })
    return jsonify({'ok': True, 'items': items, 'count': len(items)})


# ==================== ПОДПИСКА ====================

@app.route('/api/check_subscription')
def api_check_subscription():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    if not BOT_TOKEN:
        return jsonify({'ok': True, 'subscribed': True})
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{BOT_TOKEN}/getChatMember',
            params={'chat_id': CHANNEL_ID, 'user_id': user_id},
            timeout=10
        )
        data = r.json()
        if not data.get('ok'):
            error = data.get('description', 'unknown')
            if 'CHAT_ADMIN_REQUIRED' in error:
                return jsonify({'ok': True, 'subscribed': True})
            if 'USER_NOT_PARTICIPANT' in error or 'user not found' in error.lower():
                return jsonify({'ok': True, 'subscribed': False})
            return jsonify({'ok': True, 'subscribed': True})
        status = data.get('result', {}).get('status', '')
        subscribed = status in ('member', 'administrator', 'creator')
        return jsonify({'ok': True, 'subscribed': subscribed, 'status': status})
    except Exception as e:
        print(f'[SUB] exception: {e}', flush=True)
        return jsonify({'ok': True, 'subscribed': True})


# ==================== БАЛАНС ====================

@app.route('/api/balance')
def api_balance():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        ensure_user(conn, user_id); conn.commit()
        r = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
    return jsonify({'ok': True, 'balance': r['balance'] if r and 'balance' in r.keys() else 0})


# ==================== ИНВЕНТАРЬ ====================

@app.route('/api/inventory')
def api_inventory():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT id, gift_name, gift_price, gift_image, source, status,
                      created_at, available_at
               FROM user_inventory
               WHERE user_id=? AND status='in_stock'
               ORDER BY created_at DESC''',
            (user_id,)
        ).fetchall()
    return jsonify({'ok': True, 'items': [dict(r) for r in rows]})


@app.route('/api/inventory/remove', methods=['POST'])
def api_inventory_remove():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    inventory_id = int(data.get('inventory_id', 0))
    if not user_id or not inventory_id:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    WITHDRAW_FEE = 25

    with get_db() as conn:
        partner = conn.execute('SELECT 1 FROM partners WHERE user_id=?', (user_id,)).fetchone()
        if partner:
            return jsonify({'ok': False, 'error': 'Вы являетесь партнёром, вывод недоступен'}), 403

        u = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not u or u['balance'] < WITHDRAW_FEE:
            return jsonify({'ok': False, 'error': f'Недостаточно звёзд для вывода (нужно {WITHDRAW_FEE}⭐)'}), 400

        row = conn.execute(
            "SELECT * FROM user_inventory WHERE id=? AND user_id=? AND status='in_stock'",
            (inventory_id, user_id)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'Предмет не найден или уже выводится'}), 404

        keys = row.keys()
        if 'available_at' in keys and row['available_at']:
            try:
                available = datetime.strptime(row['available_at'], '%Y-%m-%d %H:%M:%S')
                if datetime.utcnow() < available:
                    delta = available - datetime.utcnow()
                    hours = int(delta.total_seconds() // 3600)
                    mins = int((delta.total_seconds() % 3600) // 60)
                    return jsonify({
                        'ok': False,
                        'error': f'⏳ Подарок станет доступен через {hours}ч {mins}м',
                        'available_at': row['available_at'],
                        'code': 'NOT_AVAILABLE_YET',
                    }), 400
            except Exception:
                pass

        conn.execute('UPDATE users SET balance = balance - ? WHERE user_id=?', (WITHDRAW_FEE, user_id))
        log_tx(conn, user_id, 'withdraw_fee', amount_stars=-WITHDRAW_FEE,
               meta={'action': 'withdraw', 'gift': row['gift_name']})

        conn.execute("UPDATE user_inventory SET status='reserved' WHERE id=?", (inventory_id,))
        conn.execute(
            '''INSERT INTO withdrawals (user_id, inventory_id, gift_name, gift_price, status)
               VALUES (?, ?, ?, ?, 'pending')''',
            (user_id, inventory_id, row['gift_name'], row['gift_price'])
        )
        log_tx(conn, user_id, 'withdraw_request',
               meta={'gift': row['gift_name'], 'price': row['gift_price'], 'fee': WITHDRAW_FEE})
        conn.commit()

        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']

    return jsonify({'ok': True, 'balance': nb, 'fee': WITHDRAW_FEE})


@app.route('/api/sell', methods=['POST'])
def api_sell():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    inventory_id = int(data.get('inventory_id', 0))
    if not user_id or not inventory_id:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT * FROM user_inventory WHERE id=? AND user_id=? AND status='in_stock'",
            (inventory_id, user_id)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'Предмет не найден или уже продан'}), 404

        price = row['gift_price']
        conn.execute('DELETE FROM user_inventory WHERE id=?', (inventory_id,))
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (price, user_id))
        log_tx(conn, user_id, 'sell', amount_stars=price,
               meta={'name': row['gift_name'], 'price': price})
        conn.commit()
        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']

    return jsonify({'ok': True, 'balance': nb, 'price': price})


@app.route('/api/upgrade/remove', methods=['POST'])
def api_upgrade_remove():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    inventory_id = int(data.get('inventory_id', 0))
    if not user_id or not inventory_id:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_inventory WHERE id=? AND user_id=? AND status='in_stock'",
            (inventory_id, user_id)
        ).fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'Предмет не найден'}), 404
        conn.execute('DELETE FROM user_inventory WHERE id=?', (inventory_id,))
        conn.commit()

    return jsonify({'ok': True})


@app.route('/api/upgrade/reward', methods=['POST'])
def api_upgrade_reward():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    gift_name = data.get('gift_name', '')
    gift_price = int(data.get('gift_price', 0))
    gift_image = data.get('gift_image', '')

    if not user_id or not gift_name:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        ensure_user(conn, user_id)
        unique_id = f'upg:{user_id}:{int(datetime.utcnow().timestamp())}'
        conn.execute(
            '''INSERT INTO user_inventory
               (user_id, gift_name, gift_price, gift_image, gift_unique_id, source, status, available_at)
               VALUES (?, ?, ?, ?, ?, 'upgrade', 'in_stock', ?)''',
            (user_id, gift_name, gift_price, gift_image, unique_id, gift_cooldown_until())
        )
        log_tx(conn, user_id, 'upgrade_win', amount_stars=0,
               meta={'name': gift_name, 'price': gift_price})
        conn.commit()

    return jsonify({'ok': True})


# ==================== RELAYER API ====================

@app.route('/api/relayer/gift_received', methods=['POST'])
def api_relayer_gift_received():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    gift_name = data.get('gift_name', '')
    gift_price = int(data.get('gift_price', 0))
    gift_image = data.get('gift_image', '')
    unique_id = data.get('unique_id', '')
    gift_slug = data.get('gift_slug', '')

    if not user_id or not gift_name:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        ensure_user(conn, user_id)
        if unique_id:
            dup = conn.execute('SELECT 1 FROM user_inventory WHERE gift_unique_id=?', (str(unique_id),)).fetchone()
            if dup:
                return jsonify({'ok': True, 'duplicate': True})
        conn.execute(
            '''INSERT INTO user_inventory
               (user_id, gift_name, gift_price, gift_image, gift_unique_id, gift_slug, source, status, available_at)
               VALUES (?, ?, ?, ?, ?, ?, 'gift', 'in_stock', ?)''',
            (user_id, gift_name, gift_price, gift_image, str(unique_id), gift_slug, gift_cooldown_until())
        )
        log_tx(conn, user_id, 'topup_gift', amount_stars=gift_price,
               meta={'gift': gift_name, 'price': gift_price, 'unique_id': unique_id})
        conn.commit()

    return jsonify({'ok': True})


@app.route('/api/relayer/pending_withdrawal')
def api_relayer_pending_withdrawal():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400

    with get_db() as conn:
        rows = conn.execute(
            '''SELECT w.id, w.inventory_id, w.gift_name, w.gift_price,
                      i.gift_unique_id, i.gift_image, i.gift_slug
               FROM withdrawals w
               LEFT JOIN user_inventory i ON i.id = w.inventory_id
               WHERE w.user_id=? AND w.status='pending'
               ORDER BY w.created_at ASC''',
            (user_id,)
        ).fetchall()

    items = [dict(r) for r in rows]
    return jsonify({'ok': True, 'withdrawals': items, 'withdrawal': items[0] if items else None})


@app.route('/api/relayer/complete_withdrawal', methods=['POST'])
def api_relayer_complete_withdrawal():
    data = request.json or {}
    withdrawal_id = int(data.get('withdrawal_id', 0))
    if not withdrawal_id:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        w = conn.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
        if not w:
            return jsonify({'ok': False, 'error': 'Заявка не найдена'}), 404
        conn.execute("UPDATE withdrawals SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (withdrawal_id,))
        if w['inventory_id']:
            conn.execute('DELETE FROM user_inventory WHERE id=?', (w['inventory_id'],))
        log_tx(conn, w['user_id'], 'withdraw_complete', meta={'gift': w['gift_name'], 'price': w['gift_price']})
        conn.commit()

    return jsonify({'ok': True})


@app.route('/api/relayer/rollback_withdrawal', methods=['POST'])
def api_relayer_rollback_withdrawal():
    data = request.json or {}
    withdrawal_id = int(data.get('withdrawal_id', 0))
    reason = data.get('reason', 'rollback')

    if not withdrawal_id:
        return jsonify({'ok': False, 'error': 'withdrawal_id required'}), 400

    WITHDRAW_FEE = 25

    with get_db() as conn:
        w = conn.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
        if not w:
            return jsonify({'ok': False, 'error': 'Заявка не найдена'}), 404
        if w['status'] != 'pending':
            return jsonify({'ok': False, 'error': f'Заявка уже {w["status"]}'}), 400

        conn.execute("UPDATE withdrawals SET status='cancelled', completed_at=CURRENT_TIMESTAMP WHERE id=?", (withdrawal_id,))

        if w['inventory_id']:
            conn.execute("UPDATE user_inventory SET status='in_stock' WHERE id=? AND user_id=?",
                         (w['inventory_id'], w['user_id']))

        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (WITHDRAW_FEE, w['user_id']))
        log_tx(conn, w['user_id'], 'withdraw_refund', amount_stars=WITHDRAW_FEE,
               meta={'gift': w['gift_name'], 'reason': reason})
        log_tx(conn, w['user_id'], 'withdraw_cancelled',
               meta={'gift': w['gift_name'], 'price': w['gift_price'], 'reason': reason})
        conn.commit()

    print(f'[ROLLBACK] ✅ Заявка #{withdrawal_id} откатана ({reason})', flush=True)
    return jsonify({'ok': True, 'withdrawal_id': withdrawal_id, 'refund': WITHDRAW_FEE, 'reason': reason})


@app.route('/api/relayer/cancel_withdrawal', methods=['POST'])
def api_relayer_cancel_withdrawal():
    data = request.json or {}
    withdrawal_id = int(data.get('withdrawal_id', 0))
    reason = data.get('reason', 'cancelled')
    refund = bool(data.get('refund', True))

    if not withdrawal_id:
        return jsonify({'ok': False, 'error': 'withdrawal_id required'}), 400

    with get_db() as conn:
        w = conn.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
        if not w:
            return jsonify({'ok': False, 'error': 'Заявка не найдена'}), 404
        if w['status'] != 'pending':
            return jsonify({'ok': False, 'error': f'Заявка уже {w["status"]}'}), 400

        conn.execute("UPDATE withdrawals SET status='cancelled', completed_at=CURRENT_TIMESTAMP WHERE id=?", (withdrawal_id,))
        if w['inventory_id']:
            conn.execute("UPDATE user_inventory SET status='in_stock' WHERE id=? AND user_id=?", (w['inventory_id'], w['user_id']))
        if refund:
            WITHDRAW_FEE = 25
            conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (WITHDRAW_FEE, w['user_id']))
            log_tx(conn, w['user_id'], 'withdraw_refund', amount_stars=WITHDRAW_FEE,
                   meta={'gift': w['gift_name'], 'reason': reason})
        log_tx(conn, w['user_id'], 'withdraw_cancelled',
               meta={'gift': w['gift_name'], 'price': w['gift_price'], 'reason': reason})
        conn.commit()

    return jsonify({'ok': True, 'cancelled': withdrawal_id, 'reason': reason})


# ==================== ИСТОРИЯ ====================

@app.route('/api/history')
def api_history():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    limit = min(int(request.args.get('limit', 100)), 500)
    tx_type = request.args.get('type')
    q = 'SELECT * FROM transactions WHERE user_id=?'; params = [user_id]
    if tx_type and tx_type != 'all':
        q += ' AND type=?'; params.append(tx_type)
    q += ' ORDER BY created_at DESC LIMIT ?'; params.append(limit)
    with get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    return jsonify({'ok': True, 'items': [dict(r) for r in rows]})


@app.route('/api/spend', methods=['POST'])
def api_spend():
    data = request.json or {}
    user_id = int(data.get('user_id', 0)); stars = int(data.get('stars', 0))
    tx_type = data.get('type', 'upgrade'); meta = data.get('meta') or {}
    if not user_id or stars <= 0:
        return jsonify({'ok': False, 'error': 'bad params'}), 400
    with get_db() as conn:
        ensure_user(conn, user_id)
        u = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not u or u['balance'] < stars:
            return jsonify({'ok': False, 'error': 'not enough stars'}), 400
        conn.execute('UPDATE users SET balance = balance - ? WHERE user_id=?', (stars, user_id))
        log_tx(conn, user_id, tx_type, amount_stars=-stars, meta=meta); conn.commit()
        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']
    return jsonify({'ok': True, 'balance': nb})


@app.route('/api/buy', methods=['POST'])
def api_buy():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    gift_name = data.get('gift_name', '')
    gift_price = int(data.get('gift_price', 0))
    gift_image = data.get('gift_image', '')

    if not user_id or not gift_name or gift_price <= 0:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        ensure_user(conn, user_id)
        u = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
        if not u or u['balance'] < gift_price:
            return jsonify({'ok': False, 'error': 'Недостаточно звёзд'}), 400

        conn.execute('UPDATE users SET balance = balance - ? WHERE user_id=?', (gift_price, user_id))
        unique_id = f'buy:{user_id}:{int(datetime.utcnow().timestamp())}'
        conn.execute(
            '''INSERT INTO user_inventory
               (user_id, gift_name, gift_price, gift_image, gift_unique_id, source, status, available_at)
               VALUES (?, ?, ?, ?, ?, 'buy', 'in_stock', ?)''',
            (user_id, gift_name, gift_price, gift_image, unique_id, gift_cooldown_until())
        )
        log_tx(conn, user_id, 'buy', amount_stars=-gift_price, meta={'name': gift_name, 'price': gift_price})
        conn.commit()
        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']

    return jsonify({'ok': True, 'balance': nb})


@app.route('/api/earn', methods=['POST'])
def api_earn():
    data = request.json or {}
    user_id = int(data.get('user_id', 0)); stars = int(data.get('stars', 0))
    tx_type = data.get('type', 'sell'); meta = data.get('meta') or {}
    if not user_id or stars <= 0:
        return jsonify({'ok': False, 'error': 'bad params'}), 400
    with get_db() as conn:
        ensure_user(conn, user_id)
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (stars, user_id))
        log_tx(conn, user_id, tx_type, amount_stars=stars, meta=meta); conn.commit()
        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']
    return jsonify({'ok': True, 'balance': nb})


# ==================== ПРОМОКОДЫ ====================

@app.route('/api/admin/promo/create', methods=['POST'])
def api_admin_promo_create():
    data = request.json or {}
    admin_id = int(data.get('user_id', 0))

    if not is_admin(admin_id):
        return jsonify({'ok': False, 'error': 'Доступ запрещён'}), 403

    code  = (data.get('code') or '').strip().upper()
    ptype = (data.get('type') or '').strip().lower()

    if not code or not ptype:
        return jsonify({'ok': False, 'error': 'code и type обязательны'}), 400
    if ptype not in ('stars', 'gift', 'boost'):
        return jsonify({'ok': False, 'error': 'type должен быть stars | gift | boost'}), 400

    value         = int(data.get('value', 0))
    max_uses      = int(data.get('max_uses', 0))
    expires_at    = data.get('expires_at')
    gift_name     = data.get('gift_name')
    gift_image    = data.get('gift_image')
    gift_price    = int(data.get('gift_price', 0))
    bonus_percent = int(data.get('bonus_percent', 0))
    boost_type    = data.get('boost_type')
    boost_hours   = int(data.get('boost_hours', 0))

    with get_db() as conn:
        if conn.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone():
            return jsonify({'ok': False, 'error': 'Промокод с таким кодом уже существует'}), 400
        try:
            conn.execute('''
                INSERT INTO promo_codes
                    (code, type, value, gift_name, gift_image, gift_price,
                     bonus_percent, boost_type, boost_hours,
                     max_uses, uses, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ''', (code, ptype, value, gift_name, gift_image, gift_price,
                  bonus_percent, boost_type, boost_hours, max_uses, expires_at))
            conn.commit()
        except Exception as e:
            return jsonify({'ok': False, 'error': f'DB error: {e}'}), 500

    print(f'[PROMO] ✅ Создан промокод {code} ({ptype}) админом {admin_id}', flush=True)
    return jsonify({'ok': True, 'code': code, 'type': ptype,
                    'value': value, 'max_uses': max_uses, 'expires_at': expires_at})


@app.route('/api/admin/promo/list')
def api_admin_promo_list():
    admin_id = int(request.args.get('user_id', 0))
    if not is_admin(admin_id):
        return jsonify({'ok': False, 'error': 'Доступ запрещён'}), 403
    with get_db() as conn:
        rows = conn.execute('''
            SELECT code, type, value, gift_name, gift_price,
                   bonus_percent, boost_type, boost_hours,
                   max_uses, uses, expires_at, created_at
            FROM promo_codes ORDER BY created_at DESC LIMIT 500
        ''').fetchall()
    return jsonify({'ok': True, 'items': [dict(r) for r in rows], 'count': len(rows)})


@app.route('/api/admin/promo/delete', methods=['POST'])
def api_admin_promo_delete():
    data = request.json or {}
    admin_id = int(data.get('user_id', 0))
    code = (data.get('code') or '').strip().upper()

    if not is_admin(admin_id):
        return jsonify({'ok': False, 'error': 'Доступ запрещён'}), 403
    if not code:
        return jsonify({'ok': False, 'error': 'code обязателен'}), 400

    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM promo_codes WHERE code=?', (code,)).fetchone():
            return jsonify({'ok': False, 'error': 'Промокод не найден'}), 404
        conn.execute('DELETE FROM promo_codes WHERE code=?', (code,))
        conn.execute('DELETE FROM promo_activations WHERE code=?', (code,))
        conn.commit()

    return jsonify({'ok': True, 'deleted': code})


@app.route('/api/promo/redeem', methods=['POST'])
def api_promo_redeem():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    code = (data.get('code') or '').strip().upper()
    if not user_id or not code:
        return jsonify({'ok': False, 'error': 'user_id и code обязательны'}), 400

    with get_db() as conn:
        ensure_user(conn, user_id)
        promo = conn.execute('SELECT * FROM promo_codes WHERE code=?', (code,)).fetchone()
        if not promo:
            return jsonify({'ok': False, 'error': 'Промокод не найден'}), 404

        if promo['expires_at']:
            try:
                exp = datetime.strptime(promo['expires_at'], '%Y-%m-%d %H:%M:%S')
                if datetime.utcnow() > exp:
                    return jsonify({'ok': False, 'error': 'Промокод истёк'}), 400
            except Exception:
                pass

        if promo['max_uses'] > 0 and promo['uses'] >= promo['max_uses']:
            return jsonify({'ok': False, 'error': 'Лимит промокода исчерпан'}), 400

        already = conn.execute('SELECT 1 FROM promo_activations WHERE code=? AND user_id=?', (code, user_id)).fetchone()
        if already:
            return jsonify({'ok': False, 'error': 'Вы уже активировали этот промокод'}), 400

        result = {'ok': True, 'type': promo['type']}

        if promo['type'] == 'stars':
            stars = promo['value']
            conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (stars, user_id))
            log_tx(conn, user_id, 'promo', amount_stars=stars, meta={'code': code, 'type': 'stars', 'value': stars})
            result['stars'] = stars
            result['message'] = f'✅ Получено {stars} ⭐'

        elif promo['type'] == 'gift':
            gift_name = promo['gift_name']; gift_image = promo['gift_image']; gift_price = promo['gift_price']
            conn.execute(
                '''INSERT INTO user_inventory
                   (user_id, gift_name, gift_price, gift_image, source, status, available_at)
                   VALUES (?, ?, ?, ?, 'promo', 'in_stock', ?)''',
                (user_id, gift_name, gift_price, gift_image, gift_cooldown_until())
            )
            log_tx(conn, user_id, 'gift', amount_stars=0,
                   meta={'name': gift_name, 'image': gift_image, 'price': gift_price, 'code': code})
            result['gift'] = {'name': gift_name, 'image': gift_image, 'price': gift_price}
            result['message'] = f'🎁 Получен подарок: {gift_name}'

        elif promo['type'] == 'boost':
            percent = promo['bonus_percent']; btype = promo['boost_type'] or 'once'; hours = promo['boost_hours'] or 24
            expires_at = None
            if btype == 'time':
                expires_at = (datetime.utcnow() + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute('''
                INSERT INTO user_boosts (user_id, bonus_percent, boost_type, uses_left, expires_at, code)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, percent, btype, 1 if btype == 'once' else 999, expires_at, code))
            result['boost'] = {'percent': percent, 'type': btype}
            result['message'] = f'🚀 +{percent}% буст активирован'
        else:
            return jsonify({'ok': False, 'error': 'Неизвестный тип промокода'}), 400

        conn.execute('INSERT INTO promo_activations (code, user_id) VALUES (?, ?)', (code, user_id))
        conn.execute('UPDATE promo_codes SET uses = uses + 1 WHERE code=?', (code,))
        conn.commit()
        nb = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()['balance']
        result['balance'] = nb
        return jsonify(result)


@app.route('/api/boost/active')
def api_boost_active():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        boost = get_active_boost(conn, user_id)
        conn.commit()
    if not boost:
        return jsonify({'ok': True, 'boost': None})
    return jsonify({'ok': True, 'boost': {
        'percent': boost['bonus_percent'],
        'type': boost['boost_type'],
        'expires_at': boost['expires_at'],
        'uses_left': boost['uses_left'],
        'code': boost['code'],
    }})


# ==================== РЕФЕРАЛЫ ====================

@app.route('/api/referral/info')
def api_referral_info():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400

    with get_db() as conn:
        row = ensure_user(conn, user_id)
        conn.commit()

        if row is None:
            return jsonify({'ok': False, 'error': 'user not found'}), 500

        keys = row.keys() if hasattr(row, 'keys') else []
        code = row['referral_code'] if 'referral_code' in keys else None

        if not code:
            code = secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]
            try:
                conn.execute('UPDATE users SET referral_code=? WHERE user_id=?', (code, user_id))
                conn.commit()
            except Exception as e:
                print(f'[REF] update referral_code error: {e}', flush=True)

        invited = conn.execute(
            'SELECT COUNT(*) AS c, COALESCE(SUM(reward_stars), 0) AS s FROM referrals WHERE referrer_id=?',
            (user_id,)
        ).fetchone()

        referred_by = conn.execute(
            'SELECT referrer_id FROM referrals WHERE referred_id=?', (user_id,)
        ).fetchone()

    return jsonify({
        'ok': True,
        'code': code,
        'invited_count': invited['c'] if invited else 0,
        'earned_stars': invited['s'] if invited else 0,
        'has_referrer': bool(referred_by),
    })


@app.route('/api/referral/register', methods=['POST'])
def api_referral_register():
    data = request.json or {}
    new_user_id = int(data.get('user_id', 0))
    code = (data.get('code') or '').strip().upper()

    if not new_user_id or not code:
        return jsonify({'ok': False, 'error': 'user_id и code обязательны'}), 400

    with get_db() as conn:
        new_user = ensure_user(conn, new_user_id)
        conn.commit()

        existing = conn.execute('SELECT 1 FROM referrals WHERE referred_id=?', (new_user_id,)).fetchone()
        if existing:
            return jsonify({'ok': True, 'already': True})

        referrer = conn.execute('SELECT * FROM users WHERE referral_code=?', (code,)).fetchone()
        if not referrer:
            return jsonify({'ok': False, 'error': 'Реферальный код не найден'}), 404

        referrer_id = referrer['user_id']
        if referrer_id == new_user_id:
            return jsonify({'ok': False, 'error': 'Нельзя пригласить самого себя'}), 400

        conn.execute(
            'INSERT INTO referrals (referrer_id, referred_id, reward_stars) VALUES (?, ?, ?)',
            (referrer_id, new_user_id, REFERRAL_REWARD)
        )
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (REFERRAL_REWARD, referrer_id))
        conn.execute('UPDATE users SET referred_by=? WHERE user_id=?', (referrer_id, new_user_id))
        log_tx(conn, referrer_id, 'referral_reward', amount_stars=REFERRAL_REWARD,
               meta={'referred_id': new_user_id, 'code': code})
        conn.commit()

    print(f'[REF] ✅ User {new_user_id} приглашён по коду {code} (referrer {referrer_id}, +{REFERRAL_REWARD}⭐)', flush=True)
    return jsonify({'ok': True, 'reward': REFERRAL_REWARD, 'referrer_id': referrer_id})


@app.route('/api/referral/list')
def api_referral_list():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400

    with get_db() as conn:
        rows = conn.execute('''
            SELECT r.referred_id, r.reward_stars, r.created_at,
                   u.first_name, u.username
            FROM referrals r
            LEFT JOIN users u ON u.user_id = r.referred_id
            WHERE r.referrer_id=?
            ORDER BY r.created_at DESC
            LIMIT 100
        ''', (user_id,)).fetchall()

    items = []
    for r in rows:
        keys = r.keys()
        items.append({
            'user_id': r['referred_id'],
            'name': (r['first_name'] if 'first_name' in keys else None) or 'Аноним',
            'username': (r['username'] if 'username' in keys else None),
            'reward': r['reward_stars'],
            'created_at': r['created_at'],
        })
    return jsonify({'ok': True, 'items': items, 'count': len(items)})


# ==================== АДМИН-ПАНЕЛЬ ====================

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


@app.route('/api/admin/check')
def api_admin_check():
    user_id = int(request.args.get('user_id', 0))
    return jsonify({'ok': True, 'is_admin': is_admin(user_id)})


@app.route('/api/admin/stats')
def api_admin_stats():
    user_id = int(request.args.get('user_id', 0))
    if not is_admin(user_id):
        return jsonify({'ok': False, 'error': 'Доступ запрещён'}), 403
    with get_db() as conn:
        users_count = conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
        promo_count = conn.execute('SELECT COUNT(*) AS c FROM promo_codes').fetchone()['c']
        activations = conn.execute('SELECT COUNT(*) FROM promo_activations').fetchone()['c']
        tx_count = conn.execute('SELECT COUNT(*) AS c FROM transactions').fetchone()['c']
        total_stars = conn.execute('SELECT COALESCE(SUM(balance), 0) AS s FROM users').fetchone()['s']
        partners_count = conn.execute('SELECT COUNT(*) AS c FROM partners').fetchone()['c']
        referrals_count = conn.execute('SELECT COUNT(*) AS c FROM referrals').fetchone()['c']
    return jsonify({
        'ok': True,
        'users': users_count,
        'promo_codes': promo_count,
        'promo_activations': activations,
        'transactions': tx_count,
        'total_stars': total_stars,
        'partners': partners_count,
        'referrals': referrals_count,
    })


@app.route('/api/admin/all_users')
def api_admin_all_users():
    with get_db() as conn:
        rows = conn.execute('SELECT user_id, username, first_name, balance FROM users ORDER BY created_at DESC').fetchall()
    return jsonify({'ok': True, 'users': [r['user_id'] for r in rows], 'count': len(rows), 'details': [dict(r) for r in rows]})


@app.route('/api/admin/broadcast_users')
def api_admin_broadcast_users():
    if BROADCAST_KEY:
        key = request.headers.get('X-Admin-Key', '')
        if key != BROADCAST_KEY:
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 403

    try:
        limit = min(int(request.args.get('limit', 10000)), 50000)
        offset = max(int(request.args.get('offset', 0)), 0)
    except ValueError:
        return jsonify({'ok': False, 'error': 'bad params'}), 400

    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
        rows = conn.execute(
            'SELECT user_id FROM users ORDER BY created_at ASC LIMIT ? OFFSET ?',
            (limit, offset)
        ).fetchall()

    user_ids = [r['user_id'] for r in rows]
    return jsonify({
        'ok': True,
        'users': user_ids,
        'count': len(user_ids),
        'total': total,
        'offset': offset,
        'limit': limit,
    })


@app.route('/api/admin/all_pending_withdrawals')
def api_admin_all_pending_withdrawals():
    with get_db() as conn:
        rows = conn.execute("""SELECT id, user_id, gift_name, gift_price, created_at FROM withdrawals WHERE status='pending' ORDER BY created_at ASC""").fetchall()
    return jsonify({'ok': True, 'withdrawals': [dict(r) for r in rows], 'count': len(rows)})


@app.route('/api/relayer/check_partner')
def api_relayer_check_partner():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        row = conn.execute('SELECT user_id, note FROM partners WHERE user_id=?', (user_id,)).fetchone()
    if row:
        return jsonify({'ok': True, 'is_partner': True, 'note': row['note']})
    return jsonify({'ok': True, 'is_partner': False})


@app.route('/api/admin/partners/add', methods=['POST'])
def api_admin_partners_add():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    note = data.get('note', '')
    added_by = int(data.get('added_by', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        if conn.execute('SELECT 1 FROM partners WHERE user_id=?', (user_id,)).fetchone():
            return jsonify({'ok': True, 'already': True})
        conn.execute('INSERT INTO partners (user_id, note, added_by) VALUES (?, ?, ?)', (user_id, note, added_by))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/partners/remove', methods=['POST'])
def api_admin_partners_remove():
    data = request.json or {}
    user_id = int(data.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        conn.execute('DELETE FROM partners WHERE user_id=?', (user_id,))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/partners/list')
def api_admin_partners_list():
    with get_db() as conn:
        rows = conn.execute('SELECT user_id, note, added_by, added_at FROM partners ORDER BY added_at DESC').fetchall()
    return jsonify({'ok': True, 'partners': [dict(r) for r in rows], 'count': len(rows)})


# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    print('[BOOT] Запуск приложения...', flush=True)
    init_db()
    threading.Thread(target=watcher_loop, daemon=True, name='ton_watcher').start()
    print('[BOOT] TON watcher запущен', flush=True)
    port = int(os.environ.get('PORT', 5000))
    print(f'[BOOT] Flask на http://0.0.0.0:{port}', flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)