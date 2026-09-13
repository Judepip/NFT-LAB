"""LAB NFT UPGRADER — backend (с автоначислением через memo и поддержкой всех форматов адреса)"""

import os
import time
import json
import base64
import sqlite3
import threading
from datetime import datetime

import requests
from flask import Flask, request, jsonify

# ==================== КОНФИГ ====================

DATA_DIR = '/data' if os.path.isdir('/data') else os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'lab_nft.db')

# Адрес получателя в UQ-формате (user-friendly)
RECIPIENT_UQ  = 'UQBhNenZ50ac9WskDqQGeajDC62-RoRwqO961LGRdu3Dml3i'
# Тот же адрес в raw-формате (TonAPI возвращает именно его)
RECIPIENT_RAW = '0:6135e9d9e7469cf56b240ea40679a8c30badbe468470a8ef7ad4b19176edc39a'

TON_API          = f'https://tonapi.io/v2/accounts/{RECIPIENT_UQ}/events'
WATCH_INTERVAL   = 30
STARS_TO_TON     = 0.011
MIN_WITHDRAW     = 500
MAX_WITHDRAW_DAY = 10000


# ==================== БД ====================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                balance INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                amount_stars INTEGER NOT NULL, amount_ton REAL NOT NULL,
                address TEXT NOT NULL, status TEXT DEFAULT 'pending', tx_hash TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        conn.commit()
    print(f'[DB] Таблицы готовы, путь: {DB_PATH}')


def ensure_user(conn, user_id):
    row = conn.execute('SELECT user_id FROM users WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        conn.execute('INSERT INTO users (user_id) VALUES (?)', (user_id,))


def log_tx(conn, user_id, tx_type, amount_stars=0, amount_ton=0.0,
           status='success', tx_hash=None, meta=None):
    conn.execute('''INSERT INTO transactions (user_id, type, amount_ton, amount_stars, status, tx_hash, meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (user_id, tx_type, amount_ton, amount_stars, status, tx_hash,
                  json.dumps(meta or {}, ensure_ascii=False)))


def add_stars(user_id, stars, tx_type='topup', amount_ton=0.0, tx_hash=None, meta=None):
    with get_db() as conn:
        ensure_user(conn, user_id)
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (stars, user_id))
        log_tx(conn, user_id, tx_type, amount_stars=stars, amount_ton=amount_ton,
               tx_hash=tx_hash, meta=meta)
        conn.commit()


def already_processed(tx_hash):
    with get_db() as conn:
        return conn.execute('SELECT 1 FROM ton_processed WHERE tx_hash=?', (tx_hash,)).fetchone() is not None


def mark_processed(tx_hash, user_id):
    with get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO ton_processed (tx_hash, user_id) VALUES (?, ?)',
                     (tx_hash, user_id))
        conn.commit()


# ==================== АДРЕС: ПОДДЕРЖКА UQ / EQ / RAW ====================

def is_our_address(addr_str):
    """Проверяет, что адрес — наш кошелёк, в любом формате (UQ, EQ, raw 0:...)."""
    if not addr_str:
        return False

    # Прямое сравнение
    if addr_str == RECIPIENT_RAW or addr_str == RECIPIENT_UQ:
        return True

    # Конвертация через tonsdk
    try:
        from tonsdk.utils import Address
        a = Address(addr_str)
        uq = a.to_string(is_user_friendly=True, is_bounceable=False)
        eq = a.to_string(is_user_friendly=True, is_bounceable=True)
        raw = a.to_string(is_user_friendly=False)
        return (uq == RECIPIENT_UQ or eq == RECIPIENT_UQ or
                raw == RECIPIENT_RAW or raw == RECIPIENT_UQ)
    except Exception as e:
        print(f'[TON] is_our_address error: {e}')
        return False


# ==================== ДЕКОДЕР MEMO ====================

def decode_ton_comment(b64_or_text):
    """Извлекает текст комментария (обычный текст или BOC-ячейка в base64)."""
    if not b64_or_text:
        return None

    # Уже текст
    if b64_or_text.startswith('u:'):
        return b64_or_text

    # Через tonsdk
    try:
        from tonsdk.boc import Cell
        from tonsdk.utils import b64str_to_bytes
        cell = Cell.one_from_boc(b64str_to_bytes(b64_or_text))
        cs = cell.begin_parse()
        cs.load_uint(32)
        text = cs.load_string_tail()
        if text:
            return text
    except Exception as e:
        print(f'[TON] tonsdk parse failed: {e}')

    # Резервный способ — поиск b'u:' в raw-байтах
    try:
        raw = base64.b64decode(b64_or_text)
        idx = raw.find(b'u:')
        if idx == -1:
            return None
        end = idx
        while end < len(raw) and 32 <= raw[end] <= 126:
            end += 1
        return raw[idx:end].decode('ascii')
    except Exception as e:
        print(f'[TON] raw parse failed: {e}')
        return None


# ==================== TON WATCHER ====================

def check_transactions():
    try:
        r = requests.get(TON_API, params={'limit': 30}, timeout=15)
        if r.status_code != 200:
            print(f'[TON] API status {r.status_code}')
            return

        events = r.json().get('events', [])
        print(f'[TON] получено событий: {len(events)}')

        for event in events:
            for action in event.get('actions', []):
                if action.get('type') != 'TonTransfer':
                    continue
                tr = action.get('TonTransfer', {})
                recipient_addr = tr.get('recipient', {}).get('address', '')

                if not is_our_address(recipient_addr):
                    print(f'[TON] пропуск: адрес {recipient_addr[:20]}… не наш')
                    continue

                raw_comment = (tr.get('comment') or '').strip()
                comment = decode_ton_comment(raw_comment)
                if not comment or not comment.startswith('u:'):
                    print(f'[TON] пропуск: комментарий {comment!r}')
                    continue

                parts = comment.split(':')
                if len(parts) != 4 or parts[0] != 'u' or parts[2] != 's':
                    print(f'[TON] пропуск: формат комментария {comment!r}')
                    continue

                try:
                    user_id = int(parts[1])
                    stars = int(parts[3])
                except ValueError:
                    continue

                tx_hash = event.get('event_id') or str(event.get('lt', ''))
                if not tx_hash or already_processed(tx_hash):
                    continue

                amount_ton = tr.get('amount', 0) / 1e9
                add_stars(user_id, stars, 'topup', amount_ton, tx_hash, {'comment': comment})
                mark_processed(tx_hash, user_id)
                print(f'[TON] ✅ +{stars}⭐ → user {user_id} (memo: {comment})')

    except Exception as e:
        import traceback
        print(f'[TON] error: {e}')
        traceback.print_exc()


def watcher_loop():
    print('[TON] watcher запущен')
    while True:
        try:
            check_transactions()
        except Exception as e:
            print(f'[TON] loop error: {e}')
        time.sleep(WATCH_INTERVAL)


# ==================== FLASK API ====================

app = Flask(__name__)


@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@app.route('/')
def root():
    return jsonify({'ok': True, 'service': 'LAB NFT UPGRADER', 'version': '1.0'})


@app.route('/api/health')
def health():
    return jsonify({'ok': True, 'time': datetime.utcnow().isoformat(), 'db': DB_PATH})


@app.route('/api/balance')
def api_balance():
    user_id = int(request.args.get('user_id', 0))
    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id required'}), 400
    with get_db() as conn:
        ensure_user(conn, user_id); conn.commit()
        r = conn.execute('SELECT balance FROM users WHERE user_id=?', (user_id,)).fetchone()
    return jsonify({'ok': True, 'balance': r['balance'] if r else 0})


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


# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    init_db()
    threading.Thread(target=watcher_loop, daemon=True).start()
    print(f'[API] Flask на http://0.0.0.0:5000 (DB: {DB_PATH})')
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)