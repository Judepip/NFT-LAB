import sqlite3

conn = sqlite3.connect("lab_nft.db")
cursor = conn.cursor()

# Добавляем недостающие столбцы (если их нет)
try:
    cursor.execute("ALTER TABLE users ADD COLUMN first_spin_done INTEGER DEFAULT 0")
    print("✅ Добавлен столбец first_spin_done")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("ℹ️ Столбец first_spin_done уже существует")
    else:
        print(f"⚠️ Ошибка: {e}")

try:
    cursor.execute("ALTER TABLE users ADD COLUMN free_spin_used_at TIMESTAMP")
    print("✅ Добавлен столбец free_spin_used_at")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("ℹ️ Столбец free_spin_used_at уже существует")
    else:
        print(f"⚠️ Ошибка: {e}")

conn.commit()
conn.close()
print("✅ База данных обновлена")