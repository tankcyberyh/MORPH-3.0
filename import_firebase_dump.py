"""Utility script to migrate a Firebase Realtime Database export into the local SQLite store.

Usage
-----
python import_firebase_dump.py path/to/export.json [--database path/to/storage.sqlite3]

The export JSON should correspond to the Firebase structure that was previously used by the bot.
The script writes the data into the same local_db facade used by the bot, ensuring that
all existing helper functions consume the data with the expected shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

from local_db import initialize as init_local_db, db

# Firebase root nodes that the bot expects to find.
KNOWN_ROOT_KEYS: tuple[str, ...] = (
    "users_data",
    "ban_list",
    "promocodes",
    "roulette_bets",
    "chat_treasury",
    "cities_data",
    "stocks_data",
    "stock_prices",
    "game_history",
    "marriages",
    "user_avatars",
    "daily_leaderboard",
    "chat_moderators",
    "chat_mutes",
    "chat_rules",
    "chat_bans",
    "vip_subscriptions",
    "user_inventory",
    "user_collection",
    "fast_promocodes",
)


def normalize_structure(value: Any) -> Any:
    """Recursively ensure dictionary keys are strings for JSON compatibility."""
    if isinstance(value, dict):
        return {str(key): normalize_structure(sub_value) for key, sub_value in value.items()}
    if isinstance(value, list):
        return [normalize_structure(item) for item in value]
    return value


def import_nodes(data: Dict[str, Any], root_keys: Iterable[str]) -> Dict[str, str]:
    """Import provided root nodes into the local database.

    Returns a mapping of node name to a short status string for reporting.
    """
    status: Dict[str, str] = {}

    for node in root_keys:
        if node not in data:
            continue

        ref = db.reference(node)
        node_value = data[node]

        if node_value is None:
            ref.delete()
            status[node] = "deleted"
            continue

        ref.set(normalize_structure(node_value))
        status[node] = "imported"

    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Firebase export JSON into local SQLite storage")
    parser.add_argument(
        "export_file",
        type=Path,
        help="Path to Firebase export JSON (use the Export JSON feature in the Firebase console)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("storage.sqlite3"),
        help="Path to the SQLite file managed by local_db (default: storage.sqlite3 next to this script)",
    )
    parser.add_argument(
        "--allow-unknown",
        action="store_true",
        help="Import any additional top-level nodes present in the JSON, not only the known ones",
    )

    args = parser.parse_args()

    if not args.export_file.exists():
        parser.error(f"Export file not found: {args.export_file}")

    try:
        payload = json.loads(args.export_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        parser.error(f"Failed to decode JSON from {args.export_file}: {exc}")

    if not isinstance(payload, dict):
        parser.error("Export root must be a JSON object with the Firebase nodes as keys")

    # Initialize the local database (creates the file if it does not yet exist).
    init_local_db(args.database)

    root_keys: set[str] = set(KNOWN_ROOT_KEYS)
    if args.allow_unknown:
        root_keys.update(payload.keys())

    status = import_nodes(payload, sorted(root_keys))

    if status:
        longest = max(len(name) for name in status)
        print("Imported nodes:")
        for name in sorted(status):
            print(f"  {name.ljust(longest)} -> {status[name]}")
    else:
        print("No matching nodes were imported from the provided export")

    skipped = sorted(set(payload.keys()) - set(status.keys()))
    if skipped:
        print("\nSkipped nodes (not in known list; use --allow-unknown to import them):")
        for name in skipped:
            print(f"  {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
