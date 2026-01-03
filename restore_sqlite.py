import sqlite3
import pathlib

DB_PATH = pathlib.Path("storage.sqlite3")
DUMP_PATH = pathlib.Path("dump.sql")

if not DUMP_PATH.exists():
    raise SystemExit("dump.sql not found")

if DB_PATH.exists():
    DB_PATH.unlink()

connection = sqlite3.connect(DB_PATH)
with DUMP_PATH.open("r", encoding="utf-8") as dump_file:
    connection.executescript(dump_file.read())
connection.close()

print("storage.sqlite3 recreated from dump.sql")
