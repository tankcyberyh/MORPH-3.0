import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional, Tuple, Union


class LocalDatabase:
    """Thread-safe key/value storage backed by SQLite."""

    def __init__(self, database_path: Union[Path, str]):
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path.as_posix(), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def reference(self, *segments: str) -> "LocalReference":
        parts: list[str] = []
        for segment in segments:
            if segment is None:
                continue
            cleaned = segment.strip("/")
            if not cleaned:
                continue
            parts.extend(filter(None, cleaned.split("/")))
        if not parts:
            raise ValueError("Reference path must contain at least one segment")
        return LocalReference(self, parts)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            cursor = self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
            row = cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        timestamp = int(time.time())
        with self._lock:
            self._conn.execute(
                "REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
                (key, payload, timestamp),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class LocalReference:
    """Firebase-like reference built on top of LocalDatabase."""

    def __init__(self, db: LocalDatabase, segments: Iterable[str]):
        self._db = db
        normalized: list[str] = []
        for segment in segments:
            if segment is None:
                continue
            cleaned = segment.strip("/")
            if not cleaned:
                continue
            normalized.extend(filter(None, cleaned.split("/")))
        self._segments: Tuple[str, ...] = tuple(normalized)
        if not self._segments:
            raise ValueError("Reference path cannot be empty")

    def child(self, segment: str) -> "LocalReference":
        if not segment or not segment.strip("/"):
            raise ValueError("Child segment cannot be empty")
        return LocalReference(self._db, self._segments + (segment,))

    def get(self, default: Any = None) -> Any:
        root_key = self._segments[0]
        data = self._db.get(root_key)
        if data is None:
            return default

        node = data
        for segment in self._segments[1:]:
            if not isinstance(node, dict) or segment not in node:
                return default
            node = node[segment]
        return node

    def set(self, value: Any) -> None:
        root_key = self._segments[0]
        if len(self._segments) == 1:
            self._db.set(root_key, value)
            return

        data = self._db.get(root_key)
        if not isinstance(data, dict):
            data = {}

        node = data
        for segment in self._segments[1:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = {}
            node[segment] = child
            node = child

        node[self._segments[-1]] = value
        self._db.set(root_key, data)

    def update(self, value: Dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise ValueError("Value for update must be a dictionary")
        current = self.get({})
        if not isinstance(current, dict):
            current = {}
        current.update(value)
        self.set(current)

    def delete(self) -> None:
        root_key = self._segments[0]
        if len(self._segments) == 1:
            self._db.delete(root_key)
            return

        data = self._db.get(root_key)
        if not isinstance(data, dict):
            return

        node_stack = [data]
        node = data
        for segment in self._segments[1:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                return
            node = child
            node_stack.append(node)

        if self._segments[-1] in node:
            del node[self._segments[-1]]
            self._db.set(root_key, node_stack[0])


_DEFAULT_DB_PATH = Path(__file__).with_name("storage.sqlite3")
_db_lock = threading.RLock()
_db_instance: Optional[LocalDatabase] = None


def initialize(database_path: Optional[Union[Path, str]] = None) -> LocalDatabase:
    global _db_instance
    with _db_lock:
        path = Path(database_path) if database_path else _DEFAULT_DB_PATH
        _db_instance = LocalDatabase(path)
        return _db_instance


def get_database() -> LocalDatabase:
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = LocalDatabase(_DEFAULT_DB_PATH)
        return _db_instance


def reference(*segments: str) -> LocalReference:
    return get_database().reference(*segments)


class _Facade(SimpleNamespace):
    def reference(self, *segments: str) -> LocalReference:
        return reference(*segments)


db = _Facade()
