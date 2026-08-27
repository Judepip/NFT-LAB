import sqlite3

conn = sqlite3.connect("lab_nft.db")
cursor = conn.cursor()

# Проверяем и добавляем недостающие столбцы
columns_to_add = {
    "free_spin_used_at": "TIMESTAMP",
    "referrer_id": "INTEGER DEFAULT 0",
    "referral_count": "INTEGER DEFAULT 0",
    "first_spin_done": "INTEGER DEFAULT 0",
    "can_withdraw": "INTEGER DEFAULT 0"
}

for col, col_type in columns_to_add.items():
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        print(f"✅ Добавлен столбец: {col}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print(f"ℹ️ Столбец {col} уже существует")
        else:
            print(f"⚠️ Ошибка: {e}")

conn.commit()
conn.close()
print("✅ Проверка и обновление базы данных завершены")