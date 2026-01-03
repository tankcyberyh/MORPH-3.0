import sqlite3
from pathlib import Path

DB_PATH = Path("storage.sqlite3")
DUMP_PATH = Path("dump.sql")

if not DUMP_PATH.exists():
    raise SystemExit("dump.sql not found")

if DB_PATH.exists():
    DB_PATH.unlink()

with sqlite3.connect(DB_PATH) as connection, DUMP_PATH.open("r", encoding="utf-8") as dump_file:
    connection.executescript(dump_file.read())

print("storage.sqlite3 recreated from dump.sql")
