import asyncio
import logging
from datetime import datetime
from pathlib import Path
from aiogram import Bot, Dispatcher, types, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import random
import string
import json
import os
from typing import Dict, List, Optional
import time
from datetime import datetime, timedelta
import re
import inspect
import base64
from local_db import initialize as init_local_db, db

try:
    from import_firebase_dump import import_nodes as _import_nodes_from_dump, normalize_structure as _normalize_from_dump
except Exception:  # pragma: no cover - optional dependency during deployment
    _import_nodes_from_dump = None
    _normalize_from_dump = None

# Инициализация локальной базы данных (SQLite хранится рядом с ботом по умолчанию)
DEFAULT_DB_PATH = Path(__file__).with_name("storage.sqlite3")
ENV_DB_PATH = os.getenv("MORPH_DB_PATH")
DATASTORE_PATH = Path(ENV_DB_PATH).expanduser() if ENV_DB_PATH else DEFAULT_DB_PATH

db_already_exists = DATASTORE_PATH.exists()
init_local_db(DATASTORE_PATH)

if not db_already_exists and _import_nodes_from_dump and _normalize_from_dump:
    import_json_name = os.getenv("MORPH_DB_IMPORT_JSON", "firebase_dump.json")
    import_json_path = Path(import_json_name).expanduser()
    if not import_json_path.is_absolute():
        import_json_path = DATASTORE_PATH.parent / import_json_path

    if import_json_path.exists():
        try:
            payload = json.loads(import_json_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                normalized_payload = {key: _normalize_from_dump(value) for key, value in payload.items()}
                result = _import_nodes_from_dump(normalized_payload, normalized_payload.keys())
                logging.info(
                    "Seeded local database from %s (%d nodes)",
                    import_json_path,
                    len(result),
                )
            else:
                logging.error(
                    "Seed JSON must contain an object at the root, got %s",
                    type(payload).__name__,
                )
        except Exception:
            logging.exception("Failed to seed local database from %s", import_json_path)

users_ref = db.reference('users_data')
bans_ref = db.reference('ban_list')
promos_ref = db.reference('promocodes')
promo_broadcast_ref = db.reference('promo_broadcasts')
roulette_ref = db.reference('roulette_bets')
marriages_ref = db.reference('marriages')
avatars_ref = db.reference('user_avatars')
leaderboard_ref = db.reference('daily_leaderboard')
moderators_ref = db.reference('chat_moderators')  # {chat_id: {user_id: rank}}
mutes_ref = db.reference('chat_mutes')  # {chat_id: {user_id: end_timestamp}}
chat_rules_ref = db.reference('chat_rules')  # {chat_id: 'текст правил'}
chat_bans_ref = db.reference('chat_bans')  # {chat_id: [user_id]} - локальные баны в чатах
vip_subscriptions_ref = db.reference('vip_subscriptions')  # {user_id: end_timestamp}
user_inventory_ref = db.reference('user_inventory')  # {user_id: {'items': {item_id: count}, ...}}
user_collection_ref = db.reference('user_collection')  # {user_id: {'items': [item_id, ...], ...}}
bot_settings_ref = db.reference('bot_settings')
user_languages_ref = db.reference('user_languages')

def format_amount(amount):
    return f"{amount:,}".replace(",", ".")


PROMO_ALPHABET = string.ascii_uppercase + string.digits


def generate_random_promocode(prefix: str = "MORPH", length: int = 6) -> str:
    suffix = ''.join(random.choices(PROMO_ALPHABET, k=length))
    return f"{prefix}{suffix}"

# --- Вспомогательные функции для парсинга длительности ---
TIME_UNITS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800,
}
MAX_DURATION_SECONDS = 365 * 24 * 3600  # 1 год


def parse_duration(raw: str) -> Optional[int]:
    if raw is None:
        return None
    raw = raw.strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        minutes = int(raw)
        return minutes * 60
    if raw in {"perma", "perm", "forever"}:
        return MAX_DURATION_SECONDS

    matches = list(re.finditer(r"(\d+)([smhdw])", raw))
    if not matches:
        return None

    consumed = "".join(match.group(0) for match in matches)
    if consumed != raw:
        return None

    total = 0
    for match in matches:
        value = int(match.group(1))
        unit = match.group(2)
        total += value * TIME_UNITS[unit]

    return total


def format_duration(seconds: int) -> str:
    if seconds >= MAX_DURATION_SECONDS:
        return "навсегда"
    parts = []
    remaining = seconds
    for unit_seconds, label in ((86400, "д"), (3600, "ч"), (60, "м"), (1, "с")):
        if remaining >= unit_seconds:
            value = remaining // unit_seconds
            remaining %= unit_seconds
            parts.append(f"{value}{label}")
    return " ".join(parts) if parts else "0с"

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
TOKEN = "8137012238:AAGBcOG8UlEYZj5ciqAygUHVnGe5tg5rO6I"  # Замените на ваш токен
ADMIN_IDS = [5439940299,6570851164]  # Замените на свой Telegram user_id (например, 123456789)
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

_creator_ids_env = os.getenv("MORPH_CREATOR_IDS")
CREATOR_IDS: set[int] = set(ADMIN_IDS)
if _creator_ids_env:
    for raw_id in _creator_ids_env.split(","):
        raw_id = raw_id.strip()
        if raw_id.isdigit():
            CREATOR_IDS.add(int(raw_id))


# Функция для проверки мута и бана (будет использоваться в обработчике)
async def check_mute_ban_all_messages(message: types.Message) -> bool:
    """Проверяет мут и бан для всех сообщений в группах. Возвращает True если нужно заблокировать."""
    # Пропускаем, если не группа
    if message.chat.type not in ['group', 'supergroup']:
        return False
    
    # Пропускаем команды - они обрабатываются отдельными хендлерами
    if message.text and (message.text.startswith('/') or 
                         message.text.lower() in ['топ', 'top', 'топ банк', 'топ банки', 'top bank',
                                                  'правила', 'rules', 'модераторы', 'админы', 'моды',
                                                  'мут', 'бан', 'размут', 'разбан', 'mute', 'ban', 'unmute', 'unban',
                                                  'назначить модератора', 'убрать модератора', 'setmod', 'delmod',
                                                  '+правила']):
        return False
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Модераторы могут писать
    if get_moderator_rank(chat_id, user_id) > 0:
        return False
    
    # Проверяем локальный бан в чате - удаляем из группы
    if is_banned_in_chat(chat_id, user_id):
        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            print(f"Ошибка при удалении забаненного пользователя: {e}")
        return True  # Блокируем сообщение
    
    # Мут обрабатывается через ограничение прав Telegram, сообщения не удаляем
    # Если пользователь замучен, Telegram сам не даст ему отправить сообщение
    return False

# Глобальные переменные для хранения данных
users_data: Dict[int, Dict] = {}
banned_users: List[int] = []
promocodes: Dict[str, Dict] = {}
active_mines_games: Dict[int, Dict] = {}
active_tower_games: Dict[int, Dict] = {}
active_blackjack_games: Dict[int, Dict] = {}
active_knb_challenges: Dict[int, Dict] = {}
active_crypto_hacker_games: Dict[int, Dict] = {}
active_taxi_games: Dict[int, Dict] = {}
active_poker_games: Dict[int, Dict] = {}
active_reactor_games: Dict[int, Dict] = {}
active_fast_promos: Dict[str, Dict] = {}
FAST_PROMO_REF = db.reference('fast_promocodes')
marriages = {}  # {user_id: {'spouse_id': spouse_id, 'date': date}}
marriage_requests = {}  # {receiver_id: sender_id}
user_stocks: Dict[int, Dict] = {}
stock_prices: Dict[str, float] = {}
cities_ref = db.reference('cities_data')
user_cities: Dict[int, Dict] = {}
city_creation: Dict[int, Dict] = {}
city_names: set = set()
active_bunker_games = {}
active_hilo_games = {}
active_roulettes = {}  # {chat_id: {'bets': {}, 'spinning': False, 'end_time': 0}}
user_avatars = {}
active_treasure_games: Dict[int, Dict] = {}  # {user_id: {'finished': False, 'bet': bet}}
daily_leaderboard: Dict[int, int] = {}  # {user_id: выигранные_морфы_за_день}
leaderboard_date: str = datetime.now().strftime('%Y-%m-%d')  # Дата текущего лидерборда

TRANSFER_LIMITS = [
    {"limit": 50_000, "cost": None},
    {"limit": 250_000, "cost": 5_000_000},
    {"limit": 1_000_000, "cost": 20_000_000},
    {"limit": 5_000_000, "cost": 60_000_000},
    {"limit": 20_000_000, "cost": 150_000_000},
    {"limit": 50_000_000, "cost": 350_000_000},
    {"limit": 100_000_000, "cost": 750_000_000},
    {"limit": 250_000_000, "cost": 1_500_000_000},
    {"limit": 500_000_000, "cost": 3_000_000_000},
    {"limit": 1_000_000_000, "cost": 6_000_000_000},
    {"limit": 2_500_000_000, "cost": 12_000_000_000},
    {"limit": 5_000_000_000, "cost": 20_000_000_000},
    {"limit": 10_000_000_000, "cost": 35_000_000_000},
    {"limit": 25_000_000_000, "cost": 55_000_000_000},
    {"limit": 50_000_000_000, "cost": 85_000_000_000},
    {"limit": 100_000_000_000, "cost": 130_000_000_000},
    {"limit": 200_000_000_000, "cost": 200_000_000_000},
    {"limit": 400_000_000_000, "cost": 300_000_000_000},
    {"limit": 700_000_000_000, "cost": 450_000_000_000},
    {"limit": 1_000_000_000_000, "cost": 600_000_000_000},
    {"limit": None, "cost": 1_000_000_000_000},
]

TRANSFER_RESET_SECONDS = 24 * 60 * 60


def get_transfer_limit(level: int) -> Optional[int]:
    if level < 0:
        level = 0
    if level >= len(TRANSFER_LIMITS):
        level = len(TRANSFER_LIMITS) - 1
    return TRANSFER_LIMITS[level]["limit"]


def get_next_transfer_cost(level: int) -> Optional[int]:
    next_level = level + 1
    if next_level >= len(TRANSFER_LIMITS):
        return None
    return TRANSFER_LIMITS[next_level]["cost"]


def ensure_transfer_profile(user_id: int) -> None:
    init_user(user_id)
    data = users_data[user_id]
    if 'transfer_limit_level' not in data:
        data['transfer_limit_level'] = 0
    if 'transfer_daily_spent' not in data:
        data['transfer_daily_spent'] = 0
    if 'transfer_daily_reset' not in data:
        data['transfer_daily_reset'] = int(time.time())


def reset_transfer_counters_if_needed(user_id: int) -> bool:
    ensure_transfer_profile(user_id)
    data = users_data[user_id]
    last_reset = data.get('transfer_daily_reset', 0)
    now = int(time.time())
    if now - last_reset >= TRANSFER_RESET_SECONDS:
        data['transfer_daily_reset'] = now
        data['transfer_daily_spent'] = 0
        return True
    return False


def format_transfer_limit(limit: Optional[int]) -> str:
    return "безлимит" if limit is None else format_amount(limit)


def seconds_until_transfer_reset(user_id: int) -> int:
    data = users_data[user_id]
    last_reset = data.get('transfer_daily_reset', int(time.time()))
    elapsed = int(time.time()) - last_reset
    remaining = TRANSFER_RESET_SECONDS - elapsed
    return max(0, remaining)

disabled_games: set[str] = set()

GAME_DEFINITIONS = [
    {"code": "mines", "title": "💣 Мины", "aliases": ["мины"]},
    {"code": "tower", "title": "🏗️ Башенка", "aliases": ["башенка"]},
    {"code": "cube", "title": "🧊 Кубик", "aliases": ["кубик"]},
    {"code": "pirate", "title": "🏴‍☠️ Пират", "aliases": ["пират"]},
    {"code": "roulette", "title": "🎰 Рулетка", "aliases": ["рул", "рулетка"]},
    {"code": "hilo", "title": "🎯 Хило", "aliases": ["хило"]},
    {"code": "crypto_hacker", "title": "💻 Крипто-Хакер", "aliases": ["хакер"]},
    {"code": "wheel", "title": "🎡 Колесо удачи", "aliases": ["колесо"]},
    {"code": "taxi", "title": "🚕 Такси", "aliases": ["такси"]},
    {"code": "slots", "title": "🎰 Слоты", "aliases": ["слоты"]},
    {"code": "nvuti", "title": "❄️ НВУТИ", "aliases": ["нвути"]},
    {"code": "vilin", "title": "🎲 Вилин", "aliases": ["вилин"]},
    {"code": "labyrinth", "title": "🌀 Лабиринт", "aliases": ["лабиринт"]},
    {"code": "bunker", "title": "🏚️ Бункер", "aliases": ["бункер"]},
    {"code": "treasure", "title": "🎁 Сокровища", "aliases": ["сокровища"]},
    {"code": "blackjack", "title": "🃏 Блэкджек", "aliases": ["блэкджек", "бж"]},
    {"code": "basketball", "title": "🏀 Баскетбол", "aliases": ["баскетбол"]},
    {"code": "football", "title": "⚽ Футбол", "aliases": ["футбол"]},
    {"code": "bowling", "title": "🎳 Боулинг", "aliases": ["боулинг"]},
    {"code": "darts", "title": "🎯 Дартс", "aliases": ["дартс"]},
    {"code": "flip", "title": "🪙 Флип", "aliases": ["флип"]},
]

_ALIAS_MAP: list[tuple[str, str]] = []
for definition in GAME_DEFINITIONS:
    for alias in definition["aliases"]:
        _ALIAS_MAP.append((alias, definition["code"]))

def get_game_definition(code: str) -> Optional[dict]:
    for definition in GAME_DEFINITIONS:
        if definition["code"] == code:
            return definition
    return None

def is_game_disabled(game_code: str) -> bool:
    return game_code in disabled_games


def save_disabled_games() -> None:
    try:
        payload = {"disabled_games": sorted(disabled_games)}
        bot_settings_ref.update(payload)
    except Exception as exc:
        logging.error("Не удалось сохранить список отключенных игр: %s", exc, exc_info=True)


def build_games_control_view() -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🎮 <b>Управление играми</b>", "", "Нажмите на кнопку, чтобы включить или отключить игру:"]
    for definition in GAME_DEFINITIONS:
        status = "⛔ Отключена" if is_game_disabled(definition["code"]) else "✅ Включена"
        lines.append(f"{definition['title']} — {status}")

    builder = InlineKeyboardBuilder()
    current_row: list[InlineKeyboardButton] = []
    for definition in GAME_DEFINITIONS:
        status_icon = "⛔" if is_game_disabled(definition["code"]) else "✅"
        button = InlineKeyboardButton(
            text=f"{status_icon} {definition['title']}",
            callback_data=f"toggle_game_{definition['code']}"
        )
        current_row.append(button)
        if len(current_row) == 2:
            builder.row(*current_row)
            current_row = []
    if current_row:
        builder.row(*current_row)

    builder.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="games_control_refresh"))

    return "\n".join(lines), builder.as_markup()


def _matches_alias(text: str, alias: str) -> bool:
    if text == alias:
        return True
    if text.startswith(f"{alias} "):
        return True
    if text.startswith(f"{alias}\n"):
        return True
    return False


def enforce_game_enabled(game_code: str) -> None:
    if is_game_disabled(game_code):
        definition = get_game_definition(game_code)
        readable_name = definition["title"] if definition else game_code
        raise RuntimeError(f"Игра отключена: {readable_name}")


@router.message(lambda message: message.text and message.text.lower().startswith('игроконтроль'))
async def admin_games_control(message: types.Message):
    if is_banned(message.from_user.id):
        return

    if message.from_user.id not in CREATOR_IDS:
        await message.reply("⛔ Команда доступна только создателю бота.")
        return

    text, markup = build_games_control_view()
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(lambda c: c.data.startswith('toggle_game_') or c.data == 'games_control_refresh')
async def toggle_game_callback(callback: CallbackQuery):
    if callback.from_user.id not in CREATOR_IDS:
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return

    if callback.data == 'games_control_refresh':
        text, markup = build_games_control_view()
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer("🔄 Обновлено")
        return

    _, game_code = callback.data.split('toggle_game_', maxsplit=1)
    if not game_code:
        await callback.answer("❌ Ошибка данных!", show_alert=True)
        return

    if game_code in disabled_games:
        disabled_games.remove(game_code)
        await callback.answer("✅ Игра включена!", show_alert=True)
    else:
        disabled_games.add(game_code)
        await callback.answer("⛔ Игра отключена!", show_alert=True)

    save_disabled_games()
    text, markup = build_games_control_view()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

# Новые переменные для обновления
last_game_data: Dict[int, Dict] = {}  # {user_id: {'command': 'игра', 'bet': 1000, 'params': {}}}
user_quiet_mode: Dict[int, float] = {}  # {user_id: end_timestamp}
user_daily_actions: Dict[int, Dict] = {}  # {user_id: {'count': 0, 'date': '2025-12-05'}}
user_bonus_reminder_sent: Dict[int, str] = {}  # {user_id: '2025-12-05'}
user_bonus_reminder_enabled: Dict[int, bool] = {}  # {user_id: True/False}
game_feedback: Dict[int, Dict] = {}  # {user_id: {'game': 'mines', 'message_id': 123}}
user_game_history: Dict[int, List] = {}  # {user_id: [{'game': 'название', 'bet': 1000, 'result': 'win/lose', 'amount': 2000, 'time': '2025-12-05 12:00:00'}]}
game_history_ref = db.reference('game_history')
pending_transfers: Dict[int, Dict] = {}  # {user_id: {'item_id': item_id, 'count': count, 'timestamp': time, 'item_name': name, 'item_emoji': emoji}}
user_inventory: Dict[int, Dict] = {}  # {user_id: {'items': {item_id: count}, 'last_updated': 'timestamp'}}
user_collection: Dict[int, Dict] = {}  # {user_id: {'items': [item_id, ...], 'last_updated': 'timestamp'}}

# --- Функции сохранения данных в локальное хранилище ---
def save_users():
    try:
        users_to_save = {str(k): v for k, v in users_data.items()}
        users_ref.set(users_to_save)
        logging.debug(f"Пользователи сохранены: {len(users_to_save)}")
    except Exception as e:
        logging.error(f"Ошибка при сохранении users_data: {e}", exc_info=True)

def save_leaderboard():
    try:
        leaderboard_ref.set({
            'date': leaderboard_date,
            'data': {str(k): v for k, v in daily_leaderboard.items()}
        })
    except Exception as e:
        logging.error(f"Ошибка при сохранении leaderboard: {e}", exc_info=True)

def save_vip_subscriptions():
    try:
        vip_subscriptions_ref.set({str(k): v for k, v in vip_subscriptions.items()})
    except Exception as e:
        logging.error(f"Ошибка при сохранении vip_subscriptions: {e}", exc_info=True)

def save_promocodes():
    try:
        promos_ref.set(promocodes)
    except Exception as e:
        logging.error(f"Ошибка при сохранении promocodes: {e}", exc_info=True)


def save_promo_broadcasts():
    try:
        promo_broadcast_ref.set({str(k): v for k, v in promo_broadcasts.items()})
    except Exception as e:
        logging.error(f"Ошибка при сохранении promo_broadcasts: {e}", exc_info=True)


def save_user_languages():
    try:
        payload = {str(k): v for k, v in user_languages.items()}
        user_languages_ref.set(payload)
    except Exception as e:
        logging.error(f"Ошибка при сохранении user_languages: {e}", exc_info=True)

def save_marriages():
    try:
        marriages_ref.set({str(k): v for k, v in marriages.items()})
    except Exception as e:
        logging.error(f"Ошибка при сохранении marriages: {e}", exc_info=True)

def save_game_history():
    try:
        game_history_ref.set({str(k): v for k, v in user_game_history.items()})
    except Exception as e:
        logging.error(f"Ошибка при сохранении game_history: {e}", exc_info=True)

def save_fast_promos():
    try:
        FAST_PROMO_REF.set({str(k): v for k, v in active_fast_promos.items()})
    except Exception as e:
        logging.error(f"Ошибка при сохранении fast_promos: {e}", exc_info=True)

# Система модерации чатов
chat_moderators: Dict[int, Dict[int, int]] = {}  # {chat_id: {user_id: rank}}
# Ранги: 1 = может мутить, 2 = может мутить и банить, 3 = создатель (все права)
chat_mutes: Dict[int, Dict[int, float]] = {}  # {chat_id: {user_id: end_timestamp}}
chat_rules: Dict[int, str] = {}  # {chat_id: 'текст правил'}
chat_bans: Dict[int, List[int]] = {}  # {chat_id: [user_id]} - локальные баны в чатах
vip_subscriptions: Dict[int, float] = {}  # {user_id: end_timestamp} - VIP подписки

# --- Загрузка данных из локального хранилища ---
games_text = (
        "🎮 <b>ВСЕ ИГРЫ БОТА MORPH</b> 🎮\n\n"
        
        "🏆 <b>ОСНОВНЫЕ ИГРЫ:</b>\n"
        "💣 <b>Мины</b> - <code>мины [ставка] [количество мин 2-24]</code>\n"
        "🏗️ <b>Башенка</b> - <code>башенка [ставка] [мины 1-4]</code>\n"
        "🎲 <b>Кубик</b> - <code>кубик [ставка] [БОЛЬШЕ/МЕНЬШЕ/ЧЕТ/НЕЧЕТ/1-6]</code>\n"
        "🏴‍☠️ <b>Пират</b> - <code>пират [ставка]</code>\n"
        "🎰 <b>Рулетка</b> - <code>рул [ставка] [на что ставим]</code>\n\n"
        
        "⚡ <b>НОВЫЕ ИГРЫ:</b>\n"
        "🎯 <b>Хило (Hi-Lo)</b> - <code>хило [ставка]</code>\n"
        "💻 <b>Крипто-Хакер</b> - <code>хакер [ставка]</code>\n"
        "🎡 <b>Колесо удачи</b> - <code>колесо [ставка]</code>\n"
        "🚕 <b>Такси</b> - <code>такси [ставка]</code>\n"
        "🎰 <b>Слоты</b> - <code>слоты [ставка]</code>\n"
        "🎲 <b>НВУТИ</b> - <code>нвути [ставка] [М/Р/Б]</code>\n"
        "🎲 <b>Вилин</b> - <code>вилин</code> (всё или ничего)\n"
        "🏗️ <b>Бункер</b> - <code>бункер [ставка] [номер 1-5]</code>\n"
        "🎁 <b>Сокровища</b> - <code>сокровища [ставка/ВСЁ]</code>\n\n"
        
        "🃏 <b>КАРТОЧНЫЕ ИГРЫ:</b>\n"
        "🃏 <b>Блэкджек</b> - <code>блэкджек [ставка]</code>\n\n"
        
        "🏀 <b>СПОРТИВНЫЕ ИГРЫ:</b>\n"
        "🏀 <b>Баскетбол</b> - <code>баскетбол [ставка]</code>\n"
        "⚽ <b>Футбол</b> - <code>футбол [ставка]</code>\n"
        "🎳 <b>Боулинг</b> - <code>боулинг [ставка]</code>\n"
        "🎯 <b>Дартс</b> - <code>дартс [ставка]</code>\n\n"
        
        "🪙 <b>ПРОСТЫЕ ИГРЫ:</b>\n"
        "🪙 <b>Флип</b> - <code>флип [ставка] орел/решка</code>\n\n"
        
        "🎃 <b>СЕЗОННЫЕ ИГРЫ:</b>\n"
        "💡 Используйте <code>помощь</code> и выберите раздел 'Сезонные' для подробностей\n\n"
        
        "🎀 <b>КЕЙСЫ И ПРЕДМЕТЫ:</b>\n"
        "🎁 <b>Hatsune Кейсы</b> - <code>кейсы</code> - магазин кейсов\n"
        "📦 <b>Открыть кейс</b> - <code>кейс [обычный/редкий/эпический/легендарный]</code>\n"
        "💰 <b>Продать предмет</b> - <code>продать [название]</code>\n"
        "🎒 <b>Инвентарь</b> - <code>инвентарь</code> - ваши предметы\n"
        "🎀 <b>Главная награда:</b> Фигурка Хатсуне Мику (500.000 MORPH)!\n\n"
        
        "💡 <b>ПОЛЕЗНЫЕ КОМАНДЫ:</b>\n"
        "• <code>помощь</code> - подробная помощь по всем командам\n"
        "• <code>баланс</code> - проверить баланс\n"
        "• <code>топ</code> - топ игроков\n"
        "• <code>бонус</code> - ежедневный бонус\n\n"
        
        "🎯 <b>Минимальная ставка: 100 MORPH</b>\n"
        "💰 <b>Начальный баланс: 2500 MORPH</b>\n\n"
        "<i>Выберите игру и начинайте играть! Удачи! 🍀</i>"
    )

def save_user_inventory():
    """Сохраняет `user_inventory` в локальную базу."""
    try:
        inventory_to_save = {}
        for user_id, user_data in user_inventory.items():
            if 'items' not in user_data:
                user_data['items'] = {}
            inventory_to_save[str(user_id)] = user_data

        user_inventory_ref.set(inventory_to_save)
        logging.debug(f"Инвентарь успешно сохранен: {len(inventory_to_save)} пользователей")
    except Exception as e:
        logging.error(f"Ошибка при сохранении инвентаря: {e}", exc_info=True)
        raise

def save_user_collection():
    user_collection_ref.set({str(k): v for k, v in user_collection.items()})

# Добавьте после других Firebase ссылок
treasury_ref = db.reference('chat_treasury')

# Функции для работы с казной чата
def init_chat_treasury(chat_id: int):
    """Инициализирует казну чата, если её нет"""
    if chat_id not in chat_treasury:
        chat_treasury[chat_id] = {
            'balance': 0,
            'created_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'members': {},
            'donations': {},
            'reward_amount': 1000  # Награда за приглашение (по умолчанию 1000)
        }
        save_chat_treasury()
    # Если казна существует, но нет поля reward_amount - добавляем его
    elif 'reward_amount' not in chat_treasury[chat_id]:
        chat_treasury[chat_id]['reward_amount'] = 1000
        save_chat_treasury()

def save_chat_treasury():
    """Сохраняет казну чата в локальное хранилище"""
    treasury_ref.set({str(k): v for k, v in chat_treasury.items()})

# Добавьте вместе с другими глобальными переменными
active_hilo_games: Dict[int, Dict] = {}

stocks_ref = db.reference('stocks_data')
stock_prices_ref = db.reference('stock_prices')

# Добавьте в глобальные переменные
chat_treasury: Dict[int, Dict] = {}
user_languages: Dict[int, str] = {}

# Настройки целевого чата для часовых промокодов
_env_hourly_chat = os.getenv("MORPH_HOURLY_CHAT_ID")
try:
    HOURLY_PROMO_CHAT_ID = int(_env_hourly_chat) if _env_hourly_chat else -1002669310047
except ValueError:
    logging.error("MORPH_HOURLY_CHAT_ID должен быть числом (chat_id). Использую значение по умолчанию.")
    HOURLY_PROMO_CHAT_ID = -1002669310047

# Удалить все импорты и функции, связанные с локальными файлами и отдельными save_* функциями (например, from firebase_config import ... и т.д.)

# Словарь для отслеживания кулдаунов команд
command_cooldowns = {}

# Функция для проверки кулдауна команды
def check_cooldown(user_id: int, command: str, cooldown_seconds: int = 2) -> bool:
    current_time = time.time()
    key = f"{user_id}_{command}"
    
    if key in command_cooldowns:
        if current_time - command_cooldowns[key] < cooldown_seconds:
            return False
    
    command_cooldowns[key] = current_time
    return True

def load_all_data():
    global users_data, banned_users, promocodes, roulette_bets, chat_treasury, user_cities, user_stocks, stock_prices, city_names, user_game_history, marriages, user_avatars, daily_leaderboard, leaderboard_date, chat_moderators, chat_mutes, chat_rules, chat_bans, vip_subscriptions, user_inventory, user_collection, disabled_games, promo_broadcasts, user_languages
    
    # Загружаем все данные из локального хранилища
    users_data = users_ref.get() or {}
    banned_users = bans_ref.get() or []
    promocodes = promos_ref.get() or {}
    promo_broadcasts = promo_broadcast_ref.get() or {}
    user_languages = user_languages_ref.get() or {}
    roulette_bets = roulette_ref.get() or {}
    chat_treasury = treasury_ref.get() or {}
    user_cities = cities_ref.get() or {}
    user_stocks = stocks_ref.get() or {}
    stock_prices = stock_prices_ref.get() or {}
    user_game_history = game_history_ref.get() or {}
    marriages = marriages_ref.get() or {}
    user_avatars = avatars_ref.get() or {}
    leaderboard_data = leaderboard_ref.get() or {}
    chat_moderators = moderators_ref.get() or {}
    chat_mutes = mutes_ref.get() or {}
    chat_rules = chat_rules_ref.get() or {}
    chat_bans = chat_bans_ref.get() or {}
    vip_subscriptions = vip_subscriptions_ref.get() or {}
    user_inventory = user_inventory_ref.get() or {}
    user_collection = user_collection_ref.get() or {}

    settings_payload = bot_settings_ref.get() or {}
    raw_disabled = settings_payload.get("disabled_games", [])
    if isinstance(raw_disabled, dict):
        raw_disabled = list(raw_disabled.values())
    disabled_games.clear()
    for code in raw_disabled:
        if isinstance(code, str) and code:
            disabled_games.add(code)

    logging.info("Отключено игр: %d", len(disabled_games))

    roulette_bets = roulette_ref.get() or {}
    
    # Привести ключи к int
    users_data = {int(k): v for k, v in users_data.items()}
    chat_treasury = {int(k): v for k, v in chat_treasury.items()}
    user_cities = {int(k): v for k, v in user_cities.items()}
    user_stocks = {int(k): v for k, v in user_stocks.items()}
    user_game_history = {int(k): v for k, v in user_game_history.items()}
    marriages = {int(k): v for k, v in marriages.items()}
    # Обрабатываем аватары - поддерживаем старый формат (только file_id) и новый (dict)
    processed_avatars = {}
    for k, v in user_avatars.items():
        if isinstance(v, dict):
            # Новый формат - уже dict
            processed_avatars[int(k)] = v
        else:
            # Старый формат - только file_id, конвертируем в новый формат
            processed_avatars[int(k)] = {'file_id': v, 'type': 'photo'}
    user_avatars = processed_avatars
    # Модераторы: {chat_id: {user_id: rank}}
    chat_moderators = {int(k): {int(uk): uv for uk, uv in v.items()} if isinstance(v, dict) else {} for k, v in chat_moderators.items()}
    # Муты: {chat_id: {user_id: end_timestamp}}
    chat_mutes = {int(k): {int(uk): float(uv) for uk, uv in v.items()} if isinstance(v, dict) else {} for k, v in chat_mutes.items()}
    # Правила: {chat_id: 'текст правил'}
    chat_rules = {int(k): str(v) for k, v in chat_rules.items()}
    # Локальные баны: {chat_id: [user_id]}
    chat_bans = {int(k): [int(uid) for uid in v] if isinstance(v, list) else [] for k, v in chat_bans.items()}
    # VIP подписки: {user_id: end_timestamp}
    vip_subscriptions = {int(k): float(v) for k, v in vip_subscriptions.items()}

    user_languages = {
        int(k): (str(v) if isinstance(v, str) else 'ru')
        for k, v in user_languages.items()
    }
    
    # Очищаем истекшие VIP подписки
    current_time = time.time()
    expired_vips = [uid for uid, end_time in vip_subscriptions.items() if end_time < current_time]
    for uid in expired_vips:
        del vip_subscriptions[uid]
    if expired_vips:
        save_vip_subscriptions()
    
    # Инвентарь: {user_id: {'items': {item_id: count}, ...}}
    user_inventory = {int(k): v for k, v in user_inventory.items()}
    
    # Коллекция: {user_id: {'items': [item_id, ...], ...}}
    user_collection = {int(k): v for k, v in user_collection.items()}
    
    # Загружаем лидерборд
    current_date = datetime.now().strftime('%Y-%m-%d')
    if leaderboard_data and leaderboard_data.get('date') == current_date:
        daily_leaderboard = {int(k): v for k, v in leaderboard_data.get('data', {}).items()}
        leaderboard_date = current_date
    else:
        # Новый день - сбрасываем лидерборд
        daily_leaderboard = {}
        leaderboard_date = current_date
        save_leaderboard()
    
    # Заполнить city_names из загруженных городов
    city_names = set()
    for city_data in user_cities.values():
        if isinstance(city_data, dict) and 'name' in city_data:
            city_names.add(city_data['name'].lower())
    
    # Инициализировать stock_prices если пустые
    if not stock_prices:
        stock_prices = {stock: info['base_price'] for stock, info in REAL_STOCKS.items()}
    
    print(f"✅ Загружено: {len(users_data)} игроков, {len(user_cities)} городов, {len(user_stocks)} портфелей")

# Инициализация пользователя
def init_user(user_id: int, username: str = None, referrer_id: int = None):
    if user_id not in users_data:
        users_data[user_id] = {
            'username': username,
            'balance': 2500,  # Начальный баланс 2500 MORPH
            'bank': 0,
            'total_won': 0,
            'registration_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'games_played': 0,
            'referrer_id': referrer_id,  # ID пригласившего пользователя
            'referrals': [],  # Список приглашенных пользователей
            'transfer_limit_level': 0,
            'transfer_daily_spent': 0,
            'transfer_daily_reset': int(time.time())
        }
        # Инициализируем инвентарь и коллекцию для нового пользователя
        if user_id not in user_inventory:
            user_inventory[user_id] = {
                'items': {},
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            save_user_inventory()
        if user_id not in user_collection:
            user_collection[user_id] = {
                'items': [],
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            save_user_collection()
        # Если есть реферер, добавляем бонус и записываем в его список рефералов
        if referrer_id and referrer_id in users_data:
            users_data[referrer_id]['balance'] += 1000
            users_data[referrer_id]['referrals'].append(user_id)
            save_users()
    else:
        # Добавляем недостающие поля для существующих пользователей
        if 'referrer_id' not in users_data[user_id]:
            users_data[user_id]['referrer_id'] = None
        if 'referrals' not in users_data[user_id]:
            users_data[user_id]['referrals'] = []
        if 'transfer_limit_level' not in users_data[user_id]:
            users_data[user_id]['transfer_limit_level'] = 0
        if 'transfer_daily_spent' not in users_data[user_id]:
            users_data[user_id]['transfer_daily_spent'] = 0
        if 'transfer_daily_reset' not in users_data[user_id]:
            users_data[user_id]['transfer_daily_reset'] = int(time.time())
        if username and users_data[user_id].get('username') != username:
            users_data[user_id]['username'] = username

def reset_transfer_counters_if_needed(user_id: int) -> bool:
    ensure_transfer_profile(user_id)
    data = users_data[user_id]
    last_reset = data.get('transfer_daily_reset', 0)
    now = int(time.time())
    if now - last_reset >= TRANSFER_RESET_SECONDS:
        data['transfer_daily_reset'] = now
        data['transfer_daily_spent'] = 0
        return True
    return False

# --- Вспомогательная функция для парсинга суммы с сокращениями ---
def parse_amount(text, user_balance=None):
    """Парсит сумму с поддержкой ключевого слова ВСЁ"""
    if text is None:
        return None
        
    text = str(text).replace(',', '').replace(' ', '').lower()
    
    if text in ['всё', 'все', 'all']:
        if user_balance is not None:
            return user_balance
        else:
            return None
    
    match = re.match(r'([\d\.]+)([кkмm]+|млн|млрд|mln|b|bn|billion|миллиард)?', text)
    if not match:
        return None
    num, suffix = match.groups()
    try:
        num = float(num)
    except Exception:
        return None
    if not suffix:
        return int(num)
    # Поддержка любых сочетаний к/кк/ккк/кккк/м/мм/млн/млрд/К/М/МЛН/МЛРД и т.д.
    suffix = suffix.lower()
    if suffix in ['млрд', 'b', 'bn', 'billion', 'миллиард']:
        return int(num * 1_000_000_000)
    if suffix in ['млн', 'mln']:
        return int(num * 1_000_000)
    if all(c in 'кk' for c in suffix):
        return int(num * (1000 ** len(suffix)))
    if all(c in 'мm' for c in suffix):
        return int(num * (1_000_000 ** len(suffix)))
    return int(num)

def check_bet_amount(amount, user_balance):
    """Проверяет корректность ставки"""
    if amount is None or amount <= 0:
        return False, "❌ Неверная сумма ставки!"
    if amount < 100:
        return False, "❌ Минимальная ставка: 100 MORPH!"
    if amount > user_balance:
        return False, f"❌ Недостаточно MORPH! Ваш баланс: {format_amount(user_balance)} MORPH"
    return True, ""

# Создаем упрощенную клавиатуру для личных сообщений
def get_private_keyboard():
    keyboard = [
        [
            types.KeyboardButton(text="🎄 Игры"),
            types.KeyboardButton(text="💎 Баланс")
        ],
        [
            types.KeyboardButton(text="🎁 Зимний бонус"),
            types.KeyboardButton(text="🧑\u200d🎄 Профиль")
        ],
        [
            types.KeyboardButton(text="🎁 Праздничная рефка"),
            types.KeyboardButton(text="❄️ Помощь")
        ]
    ]
    return types.ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите праздничное действие..."
    )

# Команда /start с клавиатурой
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    # Проверка мута и бана в группах
    if await check_mute_ban_before_message(message):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "start"):
        return
    username = message.from_user.username
    
    # Проверяем реферальную ссылку
    referrer_id = None
    if len(message.text.split()) > 1:
        try:
            referrer_id = int(message.text.split()[1])
            # Проверяем, что реферер существует и это не сам пользователь
            if referrer_id == user_id:
                referrer_id = None
        except ValueError:
            referrer_id = None
    
    # Проверяем, новый ли это пользователь
    is_new_user = user_id not in users_data
    
    if is_new_user:
        init_user(user_id, username, referrer_id)
        if referrer_id and referrer_id in users_data:
            referrer_name = users_data[referrer_id].get('username', f'User{referrer_id}')
            welcome_text = (
                f'❄️ <b>Добро пожаловать на MORPH Frost Festival!</b>\n\n'
                f'🎉 <b>Вы приглашены @{referrer_name} на зимний праздник!</b>\n'
                f'🎁 <b>Ледяной стартовый бонус: 2,500 MORPH</b>\n\n'
                f'🎰 <b>Зимние развлечения MORPH:</b>\n'
                f'• ⛷️ Мины • 🏔️ Башенка • 🧊 Кубик\n'
                f'• 🚢 Пират • 🎯 Хило • 💻 Крипто-Хакер\n'
                f'• 🎡 Колесо • 🚕 Такси • 🎰 Слоты\n'
                f'• ❄️ НВУТИ • 🎲 Вилин • 🃏 Блэкджек\n\n'
                f'🎯 <b>Нажмите праздничные кнопки ниже для быстрого старта!</b>\n'
                f'🌟 Или отправьте <b>помощь</b>, чтобы узнать обо всех зимних активностях\n\n'
                f'<i>Пусть удача искрится, как гирлянды! ✨</i>'
            )
        else:
            welcome_text = (
                '❄️ <b>Добро пожаловать на MORPH Frost Festival!</b>\n\n'
                '🎁 <b>Ледяной стартовый бонус: 2,500 MORPH</b>\n\n'
                '🎰 <b>Зимние развлечения MORPH:</b>\n'
                '• ⛷️ Мины • 🏔️ Башенка • 🧊 Кубик\n'
                '• 🚢 Пират • 🎯 Хило • 💻 Крипто-Хакер\n'
                '• 🎡 Колесо • 🚕 Такси • 🎰 Слоты\n'
                '• ❄️ НВУТИ • 🎲 Вилин • 🃏 Блэкджек\n\n'
                '🎯 <b>Используйте праздничные кнопки ниже для быстрого доступа!</b>\n'
                '🌟 Или напишите <b>помощь</b>, чтобы открыть весь зимний гайд\n\n'
                '<i>Желаем тёплых побед и сияющих выигрышей! ✨</i>'
            )
    else:
        # Существующий пользователь
        init_user(user_id, username)
        if referrer_id and referrer_id in users_data:
            await message.reply('❌ Вы уже зарегистрированы в боте!')
            return
        welcome_text = (
            '🎄 <b>С возвращением на MORPH Frost Festival!</b>\n\n'
            '🎰 <b>Снежные возможности:</b>\n'
            '• Более 15 азартных развлечений с зимним настроением\n'
            '• Развитие своего ледяного мегаполиса и экономики\n'
            '• Игровая биржа с праздничными котировками\n'
            '• Сообщества и события в духе Нового года\n'
            '• Эксклюзивные сезонные активности и подарки\n\n'
            '🎯 <b>Используйте праздничные кнопки для мгновенного старта!</b>\n'
            '🌟 Или напишите <b>помощь</b>, чтобы не пропустить зимние сюрпризы\n\n'
            '<i>Пусть баланс растёт, как снежная гирлянда! 🎆</i>'
        )
    
    # Отправляем сообщение с клавиатурой только в личных сообщениях
    if message.chat.type == 'private':
        await message.reply(welcome_text, parse_mode="HTML", reply_markup=get_private_keyboard())
    else:
        await message.reply(welcome_text, parse_mode="HTML")

# Обработка нажатий на кнопки
@router.message(lambda message: message.text in [
    "🎄 Игры", "💎 Баланс", "🎁 Зимний бонус", "🧑\u200d🎄 Профиль", "🎁 Праздничная рефка", "❄️ Помощь"
])
async def handle_button_click(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    button_text = message.text
    
    # Обрабатываем нажатия кнопок только в личных сообщениях
    if message.chat.type != 'private':
        return
    
    if not check_cooldown(user_id, f"button_{button_text}"):
        return
    
    if button_text == "🎄 Игры":
        await cmd_all_games(message)
    
    elif button_text == "💎 Баланс":
        await cmd_balance(message)
    
    elif button_text == "🎁 Зимний бонус":
        await handle_bonus_button(message)
    
    elif button_text == "🧑\u200d🎄 Профиль":
        await cmd_profile(message)
    
    elif button_text == "🎁 Праздничная рефка":
        await cmd_referral(message)
    
    elif button_text == "❄️ Помощь":
        await cmd_help(message)

# Обработка кнопки бонуса
async def handle_bonus_button(message: types.Message):
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Проверка кулдауна 24 часа
    current_time = time.time()
    last_bonus_time = users_data[user_id].get('last_bonus_time', 0)
    
    if current_time - last_bonus_time < 86400:  # 24 часа в секундах
        time_left = 86400 - (current_time - last_bonus_time)
        hours = int(time_left // 3600)
        minutes = int((time_left % 3600) // 60)
        
        await message.reply(
            f"⏳ <b>Бонус еще не доступен!</b>\n\n"
            f"🕒 Вернитесь через: <b>{hours}ч {minutes}м</b>\n"
            f"💡 Бонус обновляется каждые 24 часа",
            parse_mode="HTML"
        )
        return
    
    # Выдаем бонус от 500 до 7000 MORPH
    bonus_amount = random.randint(500, 7000)
    users_data[user_id]['balance'] += bonus_amount
    users_data[user_id]['last_bonus_time'] = current_time
    users_data[user_id]['total_bonuses_received'] = users_data[user_id].get('total_bonuses_received', 0) + bonus_amount
    
    save_users()
    
    await message.reply(
        f"🎁 <b>Ежедневный бонус получен!</b>\n\n"
        f"💰 +{format_amount(bonus_amount)} MORPH\n"
        f"💳 Ваш баланс: {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"🔄 Следующий бонус через 24 часа\n"
        f"💎 Всего получено бонусов: {format_amount(users_data[user_id]['total_bonuses_received'])} MORPH",
        parse_mode="HTML"
    )

# Также обновим команду помощи чтобы показывать клавиатуру в личных сообщениях
@router.message(lambda message: message.text and message.text.lower() in ["помощь", "help"])
async def cmd_help(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "help"):
        return
    
    # Создаем инлайн клавиатуру для помощи
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Игры", callback_data="help_games"))
    builder.add(InlineKeyboardButton(text="📋 Основное", callback_data="help_main"))
    builder.add(InlineKeyboardButton(text="🎃 Сезонные", callback_data="help_seasonal"))
    # Кнопка для модераторов (в группах)
    if message.chat.type in ['group', 'supergroup']:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if get_moderator_rank(chat_id, user_id) > 0 or user_id in ADMIN_IDS:
            builder.add(InlineKeyboardButton(text="🛡️ Модерация", callback_data="help_moderation"))
    # Кнопка для админа
    if message.from_user.id in ADMIN_IDS:
        builder.add(InlineKeyboardButton(text="🛡️ Админ команды", callback_data="help_admin"))
    builder.adjust(2, 1, 1)
    
    help_message = "<b>❓ Выберите раздел помощи:</b>"
    
    # В ЛЮБОМ ЧАТЕ показываем с инлайн-клавиатурой
    await message.reply(help_message, reply_markup=builder.as_markup(), parse_mode="HTML")

# Команда помощь
@router.message(lambda message: message.text and message.text.lower() in ["помощь", "help"])
async def cmd_help(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "help"):
        return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Игры", callback_data="help_games"))
    builder.add(InlineKeyboardButton(text="📋 Основное", callback_data="help_main"))
    builder.add(InlineKeyboardButton(text="🎃 Сезонные", callback_data="help_seasonal"))
    # Кнопка для модераторов (в группах)
    if message.chat.type in ['group', 'supergroup']:
        chat_id = message.chat.id
        user_id = message.from_user.id
        if get_moderator_rank(chat_id, user_id) > 0 or user_id in ADMIN_IDS:
            builder.add(InlineKeyboardButton(text="🛡️ Модерация", callback_data="help_moderation"))
    # Кнопка для админа
    if message.from_user.id in ADMIN_IDS:
        builder.add(InlineKeyboardButton(text="🛡️ Админ команды", callback_data="help_admin"))
    builder.adjust(2, 1, 1)  # Размещаем кнопки по 2 в ряд, затем по одной
    await message.reply("<b>❓ Выберите раздел помощи:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

# Обработка callback для help
@router.callback_query(lambda c: c.data.startswith("help_") and c.data != "help_back")
async def help_callback(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    elif callback.data == "help_games":
        text = (
            '<b>🎄 ЗИМНИЕ ИГРЫ MORPH</b>\n\n'
            
            '🏆 <b>Классика в снежинках:</b>\n'
            '⛷️ <b>Мины</b> - <code>мины [ставка] [количество мин 2-24]</code>\n'
            '🏔️ <b>Башенка</b> - <code>башенка [ставка] [мины 1-4]</code>\n'
            '🧊 <b>Кубик</b> - <code>кубик [ставка] [БОЛЬШЕ/МЕНЬШЕ/ЧЕТ/НЕЧЕТ/1-6]</code>\n'
            '🚢 <b>Пират</b> - <code>пират [ставка]</code>\n'
            '🎰 <b>Рулетка</b> - <code>рул [ставка] [на что ставим]</code>\n\n'
            
            '✨ <b>Современные морозцы:</b>\n'
            '🎯 <b>Хило (Hi-Lo)</b> - <code>хило [ставка]</code>\n'
            '💻 <b>Крипто-Хакер</b> - <code>хакер [ставка]</code>\n'
            '🎡 <b>Колесо удачи</b> - <code>колесо [ставка]</code>\n'
            '🚕 <b>Такси</b> - <code>такси [ставка]</code>\n'
            '🎰 <b>Слоты</b> - <code>слоты [ставка]</code>\n'
            '❄️ <b>НВУТИ</b> - <code>нвути [ставка] [М/Р/Б]</code>\n'
            '🎲 <b>Вилин</b> - <code>вилин</code> (всё или ничего)\n'
            '🌀 <b>Лабиринт</b> - <code>лабиринт [ставка]</code>\n'
            '🏚️ <b>Бункер</b> - <code>бункер [ставка] [номер 1-5]</code>\n'
            '🎁 <b>Сокровища</b> - <code>сокровища [ставка/ВСЁ]</code>\n\n'
            
            '🃏 <b>Карточный мерцание:</b>\n'
            '🃏 <b>Блэкджек</b> - <code>блэкджек [ставка]</code>\n\n'
            
            '🏟️ <b>Спортивный лёд:</b>\n'
            '🏀 <b>Баскетбол</b> - <code>баскетбол [ставка]</code>\n'
            '⚽ <b>Футбол</b> - <code>футбол [ставка]</code>\n'
            '🎳 <b>Боулинг</b> - <code>боулинг [ставка]</code>\n'
            '🎯 <b>Дартс</b> - <code>дартс [ставка]</code>\n\n'
            
            '🪄 <b>Простые чудеса:</b>\n'
            '🪙 <b>Флип</b> - <code>флип [ставка] орел/решка</code>\n\n'
            
            '🎯 <b>Минимальная ставка: 100 MORPH</b>\n'
            '💎 <b>Стартовый запас: 2,500 MORPH</b>'
        )
    elif callback.data == "help_main":
        text = (
            '<b>📋 СНЕЖНЫЙ ГАЙД ПО КОМАНДАМ:</b>\n\n'
            '💎 <b>баланс</b> / <b>б</b> — Проверить баланс в морозных MORPH\n'
            '🧑\u200d🎄 <b>профиль</b> — Ваш зимний профиль\n'
            '🏦 <b>банк</b> — Посмотреть депозит\n'
            '🏦 <b>банк пополнить [сумма]</b> — Спрятать MORPH под ёлку\n'
            '🏦 <b>банк снять [сумма]</b> — Забрать подарки из банка\n'
            '🏆 <b>топ банк</b> — Снежный топ по банкам\n'
            '🏆 <b>топ</b> — Общий рейтинг игроков\n'
            '🏆 <b>топ дня</b> / <b>лидерборд</b> — Ежедневный ледяной рейтинг\n'
            '📊 <b>игроки</b> — Статистика сообщества\n'
            '🎄 <b>моя рефка</b> — Праздничная рефералка\n'
            '🏓 <b>пинг</b> — Проверить магию бота\n'
            'ℹ️ <b>помощь</b> — Этот гайд\n'
            '📋 <b>правила</b> — Правила в группах\n'
            '🤝 <b>дать [сумма]</b> — Отправить подарок MORPH (ответом)\n'
            '🎁 <b>бонус</b> — Получить зимний бонус\n'
            '📝 <b>ник [имя]</b> — Сменить праздничный ник\n'
            '❌ <b>отменить ставку</b> — Отменить ставку\n'
            '🎟️ <b>промо [код]</b> — Активировать промокод\n'
            '💫 <b>донат</b> — Купить MORPH\n'
            '🎒 <b>инвентарь</b> / <b>инв</b> — Ваши зимние находки\n'
            '📚 <b>коллекция</b> / <b>моя коллекция</b> — Коллекция трофеев\n\n'
            
            '<b>📷 НОВОГОДНИЕ АВАТАРЫ:</b>\n'
            '📷 <b>аватары</b> — Помощь по аватарам\n'
            '📷 <b>установить аватар</b> — Установить (ответ на фото)\n'
            '📷 <b>сменить аватар</b> — Сменить гирлянду профиля\n'
            '📷 <b>удалить аватар</b> — Снять украшение\n\n'
            
            '<b>📜 ИСТОРИЯ И СТАТИСТИКА:</b>\n'
            '📜 <b>история</b> / <b>лог</b> — История игр\n'
            '📜 <b>ласт</b> — Последние игры\n'
            '📊 <b>дроп</b> — История дропов\n'
            '📊 <b>x50стат</b> — Статистика x50\n\n'
            
            '<b>💑 СНЕЖНЫЕ СЕРДЦА:</b>\n'
            '💍 <b>брак предложить</b> — Сделать предложение\n'
            '💑 <b>брак</b> — Информация о браке\n'
            '💔 <b>развод</b> — Завершить союз\n'
            '💑 <b>пары</b> — Список пар\n\n'
            
            '<b>⚡ Фаст-промокоды:</b>\n'
            '• Подписывайтесь на канал для мгновенных подарков\n'
            '• Лимитированные активации\n'
            '• Действуют 24 часа'
        )
    elif callback.data == "help_seasonal":
        text = (
            '<b>🎆 СЕЗОННЫЕ АКТИВНОСТИ</b>\n\n'
            '🎄 Сейчас идёт подготовка к "MORPH Frost Festival"!\n'
            '❄️ В ближайших обновлениях появятся специальные задания, награды и коллекции.\n'
            '🎁 Следите за новостями, чтобы не пропустить запуск зимнего события!'
        )
    elif callback.data == "help_moderation":
        # Проверяем, является ли пользователь модератором
        chat_id = callback.message.chat.id if callback.message.chat.type in ['group', 'supergroup'] else None
        user_id = callback.from_user.id
        is_mod = chat_id and (get_moderator_rank(chat_id, user_id) > 0 or user_id in ADMIN_IDS)
        
        if not is_mod:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        text = (
            '<b>🛡️ КОМАНДЫ МОДЕРАЦИИ</b>\n\n'
            
            '<b>📊 РАНГИ МОДЕРАТОРОВ:</b>\n'
            '1️⃣ <b>Ранг 1</b> — Может мутить (1 час)\n'
            '2️⃣ <b>Ранг 2</b> — Может мутить и банить\n'
            '3️⃣ <b>Ранг 3</b> — Создатель (все права)\n\n'
            
            '<b>👑 УПРАВЛЕНИЕ МОДЕРАТОРАМИ (только создатель):</b>\n'
            '➕ <b>назначить модератора [ранг] [@username/ID]</b> — Назначить модератора\n'
            '➖ <b>убрать модератора [@username/ID]</b> — Убрать модератора\n'
            '📋 <b>модераторы</b> или <b>моды</b> — Список модераторов\n\n'
            
            '<b>🔇 МУТ (ранг 1+):</b>\n'
            '🔇 <b>мут [@username/ID]</b> или <b>замутить [@username/ID]</b> — Замутить на 1 час\n'
            '🔊 <b>размут [@username/ID]</b> или <b>размутить [@username/ID]</b> — Размутить\n\n'
            
            '<b>🚫 БАН (ранг 2+):</b>\n'
            '🚫 <b>бан [@username/ID]</b> или <b>забанить [@username/ID]</b> — Забанить пользователя\n'
            '✅ <b>разбан [@username/ID]</b> или <b>разбанить [@username/ID]</b> — Разбанить пользователя\n\n'
            
            '<b>📋 ПРАВИЛА ЧАТА (только создатель):</b>\n'
            '📝 <b>+правила [текст]</b> — Установить правила чата\n'
            '📋 <b>правила</b> — Просмотреть правила чата\n\n'
            
            '<b>💡 ИСПОЛЬЗОВАНИЕ:</b>\n'
            '• Можно использовать ответ на сообщение вместо @username/ID для всех команд модерации\n'
            '• Модераторы не могут мутить/банить других модераторов с равным или большим рангом\n'
            '• Создатель чата определяется автоматически при первом использовании команд\n'
            '• Все данные сохраняются после перезапуска бота'
        )
    elif callback.data == "help_admin":
        if callback.from_user.id not in ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        text = (
            '<b>🛡️ АДМИН-КОМАНДЫ:</b>\n\n'
            '💸 <b>выдать [сумма]</b> — Выдать MORPH (ответ на сообщение)\n'
            '🧾 <b>забрать [сумма]</b> — Забрать MORPH (ответ на сообщение)\n'
            '💸 <b>обнулить [@username/ID]</b> — Обнулить MORPH пользователя\n'
            '🛡️ <b>banuser [@username/ID]</b> — Бан пользователя\n'
            '✅ <b>unbanuser [@username/ID]</b> — Разбан пользователя\n'
            '🆕 <b>создать промо [код] [сумма] [кол-во]</b> — создать промокод\n'
            '⚡ <b>+фаст [сумма] [активации]</b> — создать фаст-промокод\n'
            '📢 <b>+фастканал [ссылка]</b> — настроить канал для фаст-промокодов\n'
            '🔧 <b>фастканал</b> — проверить текущий канал\n'
            '🔄 <b>обнулить всех</b> — обнулить всех игроков (с подтверждением)\n'
            '⭐ <b>+вип</b> — выдать VIP подписку на месяц (ответ на сообщение)\n'
            '🎁 <b>казну награда [сумма]</b> — изменить награду в казне чата (только в группах)'
        )
    else:
        text = "<b>❓ Неизвестный раздел помощи.</b>"

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="help_back"))
    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# --- Кнопка "Назад" в помощи ---
@router.callback_query(lambda c: c.data == "help_back")
async def help_back(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Игры", callback_data="help_games"))
    builder.add(InlineKeyboardButton(text="📋 Основное", callback_data="help_main"))
    builder.add(InlineKeyboardButton(text="🎃 Сезонные", callback_data="help_seasonal"))
    # Кнопка для админа
    if callback.from_user.id in ADMIN_IDS:
        builder.add(InlineKeyboardButton(text="🛡️ Админ команды", callback_data="help_admin"))
    builder.adjust(2, 1, 1)
    await callback.message.edit_text("<b>❓ Выберите раздел помощи:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

# Команда для показа всех игр
@router.message(lambda message: message.text and message.text.lower() in ["игры", "games", "все игры"])
async def cmd_all_games(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "games"):
        return
    
    games_text = (
        "🎮 <b>ВСЕ ИГРЫ БОТА MORPH</b> 🎮\n\n"
        
        "🏆 <b>ОСНОВНЫЕ ИГРЫ:</b>\n"
        "💣 <b>Мины</b> - <code>мины [ставка] [количество мин 2-24]</code>\n"
        "🏗️ <b>Башенка</b> - <code>башенка [ставка] [мины 1-4]</code>\n"
        "🎲 <b>Кубик</b> - <code>кубик [ставка] [БОЛЬШЕ/МЕНЬШЕ/ЧЕТ/НЕЧЕТ/1-6]</code>\n"
        "🏴‍☠️ <b>Пират</b> - <code>пират [ставка]</code>\n"
        "🎰 <b>Рулетка</b> - <code>рул [ставка] [на что ставим]</code>\n\n"
        
        "⚡ <b>НОВЫЕ ИГРЫ:</b>\n"
        "🎯 <b>Хило (Hi-Lo)</b> - <code>хило [ставка]</code>\n"
        "💻 <b>Крипто-Хакер</b> - <code>хакер [ставка]</code>\n"
        "🎡 <b>Колесо удачи</b> - <code>колесо [ставка]</code>\n"
        "🚕 <b>Такси</b> - <code>такси [ставка]</code>\n"
        "🎰 <b>Слоты</b> - <code>слоты [ставка]</code>\n"
        "🎲 <b>НВУТИ</b> - <code>нвути [ставка] [М/Р/Б]</code>\n"
        "🎲 <b>Вилин</b> - <code>вилин</code> (всё или ничего)\n"
        "🌀 <b>Лабиринт</b> - <code>лабиринт [ставка]</code>\n"
        "🏗️ <b>Бункер</b> - <code>бункер [ставка] [номер 1-5]</code>\n"
        "🎁 <b>Сокровища</b> - <code>сокровища [ставка/ВСЁ]</code>\n\n"
        
        "🃏 <b>КАРТОЧНЫЕ ИГРЫ:</b>\n"
        "🃏 <b>Блэкджек</b> - <code>блэкджек [ставка]</code>\n\n"
        
        "🏀 <b>СПОРТИВНЫЕ ИГРЫ:</b>\n"
        "🏀 <b>Баскетбол</b> - <code>баскетбол [ставка]</code>\n"
        "⚽ <b>Футбол</b> - <code>футбол [ставка]</code>\n"
        "🎳 <b>Боулинг</b> - <code>боулинг [ставка]</code>\n"
        "🎯 <b>Дартс</b> - <code>дартс [ставка]</code>\n\n"
        
        "🪙 <b>ПРОСТЫЕ ИГРЫ:</b>\n"
        "🪙 <b>Флип</b> - <code>флип [ставка] орел/решка</code>\n\n"
        
        "🎃 <b>СЕЗОННЫЕ ИГРЫ:</b>\n"
        "🎉 Сейчас сезонные режимы недоступны\n"
        "💡 Используйте <code>помощь</code> и выберите раздел 'Сезонные' для подробностей\n\n"
        
        "🎀 <b>КЕЙСЫ И ПРЕДМЕТЫ:</b>\n"
        "🎁 <b>Hatsune Кейсы</b> - <code>кейсы</code> - магазин кейсов\n"
        "📦 <b>Открыть кейс</b> - <code>кейс [обычный/редкий/эпический/легендарный]</code>\n"
        "💰 <b>Продать предмет</b> - <code>продать [название]</code>\n"
        "🎒 <b>Инвентарь</b> - <code>инвентарь</code> - ваши предметы\n"
        "🎀 <b>Главная награда:</b> Фигурка Хатсуне Мику (500.000 MORPH)!\n\n"
        
        "💡 <b>ПОЛЕЗНЫЕ КОМАНДЫ:</b>\n"
        "• <code>помощь</code> - подробная помощь по всем командам\n"
        "• <code>баланс</code> - проверить баланс\n"
        "• <code>топ</code> - топ игроков\n"
        "• <code>бонус</code> - ежедневный бонус\n\n"
        
        "🎯 <b>Минимальная ставка: 100 MORPH</b>\n"
        "💰 <b>Начальный баланс: 2500 MORPH</b>\n\n"
        
        "<i>Выберите игру и начинайте играть! Удачи! 🍀</i>"
    )
    
    await message.reply(games_text, parse_mode="HTML")

# Команда кейсы — отключаем систему кейсов, оставляем сообщение
@router.message(lambda message: message.text and message.text.lower() in ["кейсы", "кейс"])
async def cmd_cases(message: types.Message):
    if is_banned(message.from_user.id):
        return
    await message.reply("Доступных кейсов в данный момент нету")

# Команды для отключения/включения ежедневного напоминания о бонусе
@router.message(lambda message: message.text and message.text.lower() in [
    "отключить напоминание бонуса", "выключить напоминание бонуса", "напоминание бонуса выкл"])
async def disable_bonus_reminder(message: types.Message):
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_bonus_reminder_enabled[user_id] = False
    await message.reply("✅ Ежедневные напоминания о бонусе отключены")

@router.message(lambda message: message.text and message.text.lower() in [
    "включить напоминание бонуса", "вкл напоминание бонуса", "напоминание бонуса вкл"])
async def enable_bonus_reminder(message: types.Message):
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_bonus_reminder_enabled[user_id] = True
    await message.reply("✅ Ежедневные напоминания о бонусе включены")

# Команда обнулить всех игроков
@router.message(lambda message: message.text and message.text.lower().startswith('обнулить всех'))
async def admin_reset_all(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    # Подтверждение действия
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Да, обнулить всех", callback_data="confirm_reset_all"))
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_reset_all"))
    
    await message.reply(
        "⚠️ <b>ВНИМАНИЕ! Вы собираетесь обнулить всех игроков!</b>\n\n"
        "💰 Все игроки получат по 5000 MORPH\n"
        "💸 Все текущие балансы будут сброшены\n"
        "🏦 Банки и статистика также обнулятся\n\n"
        "<b>Вы уверены?</b>",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )

# Обработчик подтверждения обнуления всех
@router.callback_query(lambda c: c.data in ["confirm_reset_all", "cancel_reset_all"])
async def handle_reset_all_confirmation(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав!", show_alert=True)
        return
    
    if callback.data == "cancel_reset_all":
        await callback.message.edit_text("❌ Обнуление всех игроков отменено.")
        await callback.answer()
        return
    
    # Обнуляем всех игроков
    reset_count = 0
    for user_id, user_data in users_data.items():
        if isinstance(user_id, int):  # Пропускаем системные записи
            # Сохраняем имя пользователя
            username = user_data.get('username')
            
            # Полностью обнуляем и устанавливаем 5000 MORPH
            users_data[user_id] = {
                'username': username,
                'balance': 60000,  # Новый стартовый баланс
                'bank': 0,
                'total_won': 0,
                'registration_date': user_data.get('registration_date', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                'games_played': user_data.get('games_played', 0),
                'referrer_id': user_data.get('referrer_id'),
                'referrals': user_data.get('referrals', [])
            }
            reset_count += 1
    
    save_users()
    
    await callback.message.edit_text(
        f"✅ <b>Все игроки обнулены!</b>\n\n"
        f"🔄 Обработано игроков: <b>{reset_count}</b>\n"
        f"💰 Новый баланс у всех: <b>5,000 MORPH</b>\n"
        f"💸 Все банки и статистика сброшены",
        parse_mode='HTML'
    )
    await callback.answer()

# --- Кнопка "Назад" в помощи ---
@router.callback_query(lambda c: c.data == "help_back")
async def help_back(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Игры", callback_data="help_games"))
    builder.add(InlineKeyboardButton(text="📋 Основное", callback_data="help_main"))
    # Кнопка для админа
    if callback.from_user.id in ADMIN_IDS:
        builder.add(InlineKeyboardButton(text="🛡️ Админ команды", callback_data="help_admin"))
    await callback.message.edit_text("<b>❓ Выберите раздел помощи:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

# Команда обнулить всё (в ответ на сообщение)
@router.message(lambda message: message.text and message.text.lower() == 'обнулить всё' and message.reply_to_message)
async def admin_reset_user_all(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    # Проверка прав администратора
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    # Получаем пользователя, на чьё сообщение ответили
    target_user = message.reply_to_message.from_user
    target_user_id = target_user.id
    
    # Проверяем, существует ли пользователь
    if target_user_id not in users_data:
        await message.reply('❌ Пользователь не найден в базе!')
        return
    
    # Подтверждение действия
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Да, обнулить полностью", callback_data=f"confirm_reset_all_{target_user_id}"))
    builder.add(InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_reset_all_{target_user_id}"))
    
    # Получаем текущие данные пользователя
    username = users_data[target_user_id].get('username', target_user.first_name)
    current_balance = users_data[target_user_id].get('balance', 0)
    current_bank = users_data[target_user_id].get('bank', 0)
    
    # Проверяем есть ли у пользователя другие активы
    has_city = target_user_id in user_cities
    has_stocks = target_user_id in user_stocks and user_stocks[target_user_id].get('balance', 0) > 0
    has_mines = target_user_id in active_mines_games
    has_tower = target_user_id in active_tower_games
    await message.reply(
        f"⚠️ <b>ВНИМАНИЕ! Вы собираетесь полностью обнулить игрока @{username}</b>\n\n"
        f"👤 <b>Целевой игрок:</b> @{username} (ID: {target_user_id})\n"
        f"💰 <b>Текущий баланс:</b> {format_amount(current_balance)} MORPH\n"
        f"🏦 <b>Банк:</b> {format_amount(current_bank)} MORPH\n"
        f"📊 <b>Статистика:</b> {users_data[target_user_id].get('games_played', 0)} игр, {format_amount(users_data[target_user_id].get('total_won', 0))} выиграно\n\n"
        f"🔍 <b>Активные активы:</b>\n"
        f"{'🏙️ Есть город' if has_city else '🏙️ Нет города'}\n"
        f"{'📈 Есть акции' if has_stocks else '📈 Нет акций'}\n"
        f"{'💣 Активная игра в мины' if has_mines else '💣 Нет активных игр в мины'}\n"
        f"{'🏗️ Активная игра в башенку' if has_tower else '🏗️ Нет активных игр в башенку'}\n\n"
        f"💥 <b>После обнуления:</b>\n"
        f"• Все MORPH будут обнулены\n"
        f"• Активные игры будут отменены\n"
        f"• Город будет удален\n"
        f"• Акции будут проданы/обнулены\n"
        f"• Банк обнулится\n\n"
        f"<b>Вы уверены, что хотите полностью обнулить этого игрока?</b>",
        reply_markup=builder.as_markup(),
        parse_mode='HTML'
    )

# Обработчик подтверждения полного обнуления
@router.callback_query(lambda c: c.data.startswith("confirm_reset_all_") or c.data.startswith("cancel_reset_all_"))
async def handle_reset_all_user_confirmation(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав!", show_alert=True)
        return
    
    # Получаем ID целевого пользователя
    data_parts = callback.data.split("_")
    if len(data_parts) < 4:
        await callback.answer("❌ Ошибка данных!", show_alert=True)
        return
    
    target_user_id = int(data_parts[3])
    
    if data_parts[0] == "cancel":
        await callback.message.edit_text(
            f"❌ Обнуление игрока отменено.\n"
            f"👤 Игрок ID: {target_user_id} сохранен.",
            parse_mode='HTML'
        )
        await callback.answer()
        return
    
    # Проверяем существует ли пользователь
    if target_user_id not in users_data:
        await callback.message.edit_text(
            "❌ Игрок не найден в базе! Возможно, он был удален.",
            parse_mode='HTML'
        )
        await callback.answer()
        return
    
    username = users_data[target_user_id].get('username', f'User{target_user_id}')
    old_balance = users_data[target_user_id].get('balance', 0)
    old_bank = users_data[target_user_id].get('bank', 0)
    
    # 1. Обнуляем основной баланс и банк
    users_data[target_user_id]['balance'] = 0
    users_data[target_user_id]['bank'] = 0
    users_data[target_user_id]['total_won'] = 0
    users_data[target_user_id]['games_played'] = 0
    
    # 2. Удаляем город если есть
    city_deleted = False
    if target_user_id in user_cities:
        city_name = user_cities[target_user_id].get('name', 'Неизвестный город')
        # Удаляем из списка названий
        if city_name.lower() in city_names:
            city_names.remove(city_name.lower())
        # Удаляем город
        del user_cities[target_user_id]
        city_deleted = True
        # Сохраняем изменения
        save_cities()
    
    # 3. Обнуляем акции и биржевой баланс
    stocks_deleted = False
    if target_user_id in user_stocks:
        portfolio_value = calculate_portfolio_value(target_user_id)
        # Обнуляем портфель
        user_stocks[target_user_id] = {
            'balance': 0,
            'stocks': {},
            'total_invested': 0,
            'total_profit': 0,
            'created_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        # Инициализируем все акции с 0
        for stock in REAL_STOCKS:
            user_stocks[target_user_id]['stocks'][stock] = 0
        stocks_deleted = True
        # Сохраняем изменения
        save_stocks()
    
    # 4. Завершаем активные игры
    games_ended = []
    
    if target_user_id in active_mines_games:
        del active_mines_games[target_user_id]
        games_ended.append("💣 Мины")
    
    if target_user_id in active_tower_games:
        del active_tower_games[target_user_id]
        games_ended.append("🏗️ Башенка")
    
    if target_user_id in active_blackjack_games:
        del active_blackjack_games[target_user_id]
        games_ended.append("🃏 Блэкджек")
    
    if target_user_id in active_knb_challenges:
        del active_knb_challenges[target_user_id]
        games_ended.append("✂️ КНБ")
    
    if target_user_id in active_crypto_hacker_games:
        del active_crypto_hacker_games[target_user_id]
        games_ended.append("💻 Крипто-Хакер")
    
    if target_user_id in active_taxi_games:
        del active_taxi_games[target_user_id]
        games_ended.append("🚕 Такси")
    
    if target_user_id in active_poker_games:
        del active_poker_games[target_user_id]
        games_ended.append("🎰 Покер")
    
    if target_user_id in active_reactor_games:
        del active_reactor_games[target_user_id]
        games_ended.append("⚡ Реактор")
    
    if target_user_id in active_hilo_games:
        del active_hilo_games[target_user_id]
        games_ended.append("🎯 Хило")
    
    if target_user_id in active_bunker_games:
        # Ищем все игры бункер для этого пользователя
        bunker_games_to_delete = []
        for game_id, game in active_bunker_games.items():
            if game.get('user_id') == target_user_id:
                bunker_games_to_delete.append(game_id)
        
        for game_id in bunker_games_to_delete:
            del active_bunker_games[game_id]
        
        if bunker_games_to_delete:
            games_ended.append("🏗️ Бункер")
    
    if target_user_id in active_crystal_games:
        del active_crystal_games[target_user_id]
        games_ended.append("🔮 Кристалл Фрирен")
    
    if target_user_id in active_vilin_games:
        del active_vilin_games[target_user_id]
        games_ended.append("🎲 Вилин")
    
    if target_user_id in vilin_cooldowns:
        del vilin_cooldowns[target_user_id]
    
    # 5. Удаляем из активных рулеток (всех чатов)
    for chat_id, roulette_data in active_roulettes.items():
        if target_user_id in roulette_data.get('bets', {}):
            del roulette_data['bets'][target_user_id]
            games_ended.append("🎰 Рулетка")
    
    # 6. Сохраняем изменения основного профиля
    save_users()
    
    # 7. Формируем отчет
    report_parts = []
    
    if old_balance > 0:
        report_parts.append(f"💰 Основной баланс: {format_amount(old_balance)} MORPH → 0 MORPH")
    
    if old_bank > 0:
        report_parts.append(f"🏦 Банк: {format_amount(old_bank)} MORPH → 0 MORPH")
    
    if city_deleted:
        report_parts.append("🏙️ Город: УДАЛЕН")
    
    if stocks_deleted:
        report_parts.append("📈 Портфель акций: ОБНУЛЕН")
    
    if games_ended:
        report_parts.append(f"🎮 Активные игры завершены: {', '.join(games_ended)}")
    
    if not report_parts:
        report_parts.append("ℹ️ Изменений не внесено (игрок уже был обнулен)")
    
    report_text = "\n".join(report_parts)
    
    await callback.message.edit_text(
        f"✅ <b>ИГРОК ПОЛНОСТЬЮ ОБНУЛЕН!</b>\n\n"
        f"👤 <b>Игрок:</b> @{username} (ID: {target_user_id})\n\n"
        f"📋 <b>Выполненные действия:</b>\n"
        f"{report_text}\n\n"
        f"💡 Игрок может начать с чистого листа с 2500 MORPH",
        parse_mode='HTML'
    )
    await callback.answer("Игрок успешно обнулен!")

# Команда ТОП БИРЖА
@router.message(lambda message: message.text and message.text.lower() in ["топ биржа", "топ биржи", "топ акций", "топ акции", "биржевой топ"])
async def cmd_stock_top(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "stock_top", 10):
        return
    
    # Создаем список игроков с биржевыми балансами
    stock_players = []
    
    for player_id, portfolio in user_stocks.items():
        # Проверяем что это данные портфеля
        if isinstance(portfolio, dict) and 'balance' in portfolio:
            # Рассчитываем полную стоимость портфеля (баланс + стоимость акций)
            portfolio_value = calculate_portfolio_value(player_id)
            
            if portfolio_value > 0:
                # Получаем имя пользователя
                username = ""
                
                # Пробуем получить имя из основного профиля
                if player_id in users_data:
                    user_data = users_data[player_id]
                    username = user_data.get('username', f'User{player_id}')
                    if not username or username.startswith('User'):
                        username = f"ID{player_id}"
                else:
                    username = f"ID{player_id}"
                
                # Формируем запись для топа
                player_entry = {
                    'user_id': player_id,
                    'username': username,
                    'portfolio_value': portfolio_value,
                    'stock_balance': portfolio.get('balance', 0),
                    'total_invested': portfolio.get('total_invested', 0),
                    'total_profit': portfolio.get('total_profit', 0)
                }
                
                stock_players.append(player_entry)
    
    if not stock_players:
        await message.reply(
            "📊 <b>ТОП БИРЖА</b>\n\n"
            "😢 Пока никто не инвестировал в биржу!\n\n"
            "💡 <b>Как попасть в топ:</b>\n"
            "1. Пополните биржевой баланс\n"
            "2. Купите акции\n"
            "3. Следите за ростом цен\n\n"
            "📈 <b>Команды:</b>\n"
            "<code>биржа</code> - просмотр биржи\n"
            "<code>мой портфель</code> - ваш портфель\n"
            "<code>пополнить биржу 5000</code> - пополнить баланс",
            parse_mode="HTML"
        )
        return
    
    # Сортируем по стоимости портфеля (по убыванию)
    stock_players.sort(key=lambda x: x['portfolio_value'], reverse=True)
    
    # Берем топ-20
    top_players = stock_players[:20]
    
    # Считаем общую статистику
    total_portfolio_value = sum(p['portfolio_value'] for p in top_players)
    total_balance = sum(p['stock_balance'] for p in top_players)
    total_profit = sum(p['total_profit'] for p in top_players)
    
    # Места с эмодзи
    places = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    # Формируем текст топа
    top_text = "📈 <b>ТОП БИРЖЕВЫХ ИНВЕСТОРОВ</b>\n\n"
    
    # Показываем первые 10 мест с эмодзи
    for i, player in enumerate(top_players[:10]):
        place_emoji = places[i] if i < len(places) else f"{i+1}."
        username_display = player['username'][:20]
        
        # Определяем статус по прибыли
        if player['total_profit'] > 0:
            profit_emoji = "📈"
        elif player['total_profit'] < 0:
            profit_emoji = "📉"
        else:
            profit_emoji = "📊"
        
        top_text += (
            f"{place_emoji} <b>{username_display}</b>\n"
            f"   💰 Портфель: <code>{format_amount(int(player['portfolio_value']))} MORPH</code>\n"
            f"   💵 Баланс: <code>{format_amount(int(player['stock_balance']))} MORPH</code>\n"
            f"   {profit_emoji} Прибыль: <code>{format_amount(int(player['total_profit']))} MORPH</code>\n\n"
        )
    
    # Показываем места 11-20 без эмодзи
    if len(top_players) > 10:
        top_text += "<b>🏆 Другие участники:</b>\n"
        for i, player in enumerate(top_players[10:], 11):
            username_display = player['username'][:15]
            top_text += f"{i}. {username_display}: <code>{format_amount(int(player['portfolio_value']))} MORPH</code>\n"
        top_text += "\n"
    
    # Общая статистика
    top_text += (
        f"📊 <b>ОБЩАЯ СТАТИСТИКА:</b>\n"
        f"👥 Участников: <b>{len(stock_players)}</b>\n"
        f"💰 Общая стоимость портфелей: <b>{format_amount(int(total_portfolio_value))} MORPH</b>\n"
        f"💵 Общий баланс на бирже: <b>{format_amount(int(total_balance))} MORPH</b>\n"
        f"📈 Общая прибыль: <b>{format_amount(int(total_profit))} MORPH</b>\n\n"
    )
    
    # Показываем позицию текущего пользователя
    current_position = None
    for i, player in enumerate(stock_players, 1):
        if player['user_id'] == user_id:
            current_position = i
            current_player = player
            break
    
    if current_position:
        place_emoji = ""
        if current_position == 1:
            place_emoji = "🥇"
        elif current_position == 2:
            place_emoji = "🥈"
        elif current_position == 3:
            place_emoji = "🥉"
        elif current_position <= 10:
            place_emoji = f"{current_position}️⃣"
        
        top_text += (
            f"👤 <b>ВАША ПОЗИЦИЯ:</b>\n"
            f"{place_emoji} Место: <b>{current_position}/{len(stock_players)}</b>\n"
            f"💰 Ваш портфель: <b>{format_amount(int(current_player['portfolio_value']))} MORPH</b>\n"
            f"📊 Ваша прибыль: <b>{format_amount(int(current_player['total_profit']))} MORPH</b>\n\n"
        )
    else:
        top_text += (
            f"👤 <b>ВАША ПОЗИЦИЯ:</b>\n"
            f"😢 Вы еще не в топе!\n"
            f"💡 Начните инвестировать чтобы попасть в рейтинг\n\n"
        )
    
    # Обновление цен
    last_update = "только что"
    top_text += f"🔄 <b>Цены обновляются каждые 5 минут</b>\n"
    top_text += f"📅 <b>Актуально на:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
    
    # Кнопки для быстрого доступа
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Мои акции", callback_data="my_stocks_btn")
    builder.button(text="💰 Биржа", callback_data="stock_market_btn")
    builder.button(text="📊 Полный топ", callback_data="full_stock_top_btn")
    builder.adjust(2, 1)
    
    await message.reply(top_text, parse_mode="HTML", reply_markup=builder.as_markup())

# Обработчики кнопок для топа биржи
@router.callback_query(lambda c: c.data in ["my_stocks_btn", "stock_market_btn", "full_stock_top_btn"])
async def handle_stock_top_buttons(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    
    user_id = callback.from_user.id
    
    if callback.data == "my_stocks_btn":
        # Показываем портфель пользователя
        init_user(user_id, callback.from_user.username)
        init_stock_portfolio(user_id)
        
        portfolio = user_stocks[user_id]
        portfolio_value = calculate_portfolio_value(user_id)
        
        portfolio_text = (
            f"💼 <b>ВАШ ПОРТФЕЛЬ АКЦИЙ</b>\n\n"
            f"💰 Общая стоимость: <b>{format_amount(int(portfolio_value))} MORPH</b>\n"
            f"💵 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>\n"
            f"📈 Всего инвестировано: <b>{format_amount(portfolio['total_invested'])} MORPH</b>\n"
            f"🎯 Общая прибыль: <b>{format_amount(portfolio['total_profit'])} MORPH</b>\n\n"
        )
        
        # Показываем акции
        has_stocks = False
        for stock, quantity in portfolio['stocks'].items():
            if quantity > 0:
                has_stocks = True
                current_price = stock_prices.get(stock, REAL_STOCKS[stock]['base_price'])
                value = current_price * quantity
                stock_info = REAL_STOCKS[stock]
                avg_price = REAL_STOCKS[stock]['base_price']
                profit = (current_price - avg_price) * quantity
                profit_percent = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
                profit_emoji = "📈" if profit >= 0 else "📉"
                
                portfolio_text += (
                    f"{stock_info['emoji']} <b>{stock_info['name']} ({stock})</b>\n"
                    f"📦 {quantity} акций\n"
                    f"💰 Текущая стоимость: {format_amount(int(value))} MORPH\n"
                    f"{profit_emoji} Прибыль: {format_amount(int(profit))} MORPH ({profit_percent:+.1f}%)\n\n"
                )
        
        if not has_stocks:
            portfolio_text += "📭 <b>У вас пока нет акций</b>\n\n"
        
        portfolio_text += "💡 <b>Используйте:</b>\n<code>купить AAPL 10</code> - купить акции\n<code>продать TSLA 5</code> - продать акции"
        
        await callback.message.edit_text(portfolio_text, parse_mode="HTML")
        
    elif callback.data == "stock_market_btn":
        # Показываем биржу
        await show_stock_market(callback.message)
        
    elif callback.data == "full_stock_top_btn":
        # Показываем полный топ (без лимита в 20)
        stock_players = []
        
        for player_id, portfolio in user_stocks.items():
            if isinstance(portfolio, dict) and 'balance' in portfolio:
                portfolio_value = calculate_portfolio_value(player_id)
                
                if portfolio_value > 0:
                    username = ""
                    if player_id in users_data:
                        user_data = users_data[player_id]
                        username = user_data.get('username', f'User{player_id}')
                        if not username or username.startswith('User'):
                            username = f"ID{player_id}"
                    else:
                        username = f"ID{player_id}"
                    
                    player_entry = {
                        'user_id': player_id,
                        'username': username,
                        'portfolio_value': portfolio_value
                    }
                    stock_players.append(player_entry)
        
        if not stock_players:
            await callback.message.edit_text("😢 Нет данных для полного топа!", parse_mode="HTML")
            return
        
        stock_players.sort(key=lambda x: x['portfolio_value'], reverse=True)
        
        full_top_text = "📊 <b>ПОЛНЫЙ ТОП БИРЖЕВЫХ ИНВЕСТОРОВ</b>\n\n"
        
        for i, player in enumerate(stock_players[:50], 1):  # Показываем топ-50
            place = f"{i}." if i > 10 else ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"][i-1]
            username_display = player['username'][:25]
            full_top_text += f"{place} {username_display}: <code>{format_amount(int(player['portfolio_value']))} MORPH</code>\n"
            
            # Разделяем каждые 10 записей пустой строкой
            if i % 10 == 0:
                full_top_text += "\n"
        
        full_top_text += f"\n👥 Всего участников: <b>{len(stock_players)}</b>"
        
        # Кнопка возврата
        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад к топу", callback_data="back_to_stock_top")
        
        await callback.message.edit_text(full_top_text, parse_mode="HTML", reply_markup=builder.as_markup())
    
    await callback.answer()

# Обработчик кнопки возврата
@router.callback_query(lambda c: c.data == "back_to_stock_top")
async def back_to_stock_top(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    
    # Создаем фейковое сообщение чтобы вызвать команду топ
    fake_message = types.Message(
        message_id=callback.message.message_id,
        date=datetime.now(),
        chat=callback.message.chat,
        text="топ биржа",
        from_user=callback.from_user
    )
    
    await cmd_stock_top(fake_message)
    await callback.answer()

# Вспомогательная функция для показа биржи
async def show_stock_market(message: types.Message):
    """Показать биржу (дублирует существующую команду)"""
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username if hasattr(message, 'from_user') else None)
    init_stock_portfolio(user_id)
    
    # Обновляем цены
    global stock_prices
    stock_prices = await get_real_stock_prices()
    
    market_text = "📈 <b>БИРЖА MORPH</b>\n\n"
    market_text += "💹 <b>Котировки в реальном времени:</b>\n\n"
    
    for stock, price in stock_prices.items():
        info = REAL_STOCKS[stock]
        change = ((price - info['base_price']) / info['base_price']) * 100
        change_emoji = "📈" if change >= 0 else "📉"
        
        market_text += (
            f"{info['emoji']} <b>{info['name']}</b> ({stock})\n"
            f"💰 Цена: <b>{price:.2f} MORPH</b>\n"
            f"{change_emoji} Изменение: <b>{change:+.2f}%</b>\n\n"
        )
    
    portfolio = user_stocks[user_id]
    portfolio_value = calculate_portfolio_value(user_id)
    
    market_text += (
        f"💼 <b>ВАШ ПОРТФЕЛЬ:</b>\n"
        f"💰 Общая стоимость: <b>{format_amount(int(portfolio_value))} MORPH</b>\n"
        f"💵 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>\n"
        f"📊 Прибыль/убыток: <b>{format_amount(portfolio['total_profit'])} MORPH</b>\n\n"
        f"🛠️ <b>КОМАНДЫ:</b>\n"
        f"• <code>купить AAPL 10</code> - купить акции\n"
        f"• <code>продать TSLA 5</code> - продать акции\n"
        f"• <code>пополнить биржу 5000</code> - пополнить баланс\n"
        f"• <code>вывести с биржи 3000</code> - вывести средства\n"
        f"• <code>мой портфель</code> - детали портфеля"
    )
    
    await message.reply(market_text, parse_mode="HTML")

#НОВЫЕФУНКЦИИ
#ГОРОДА
# --- ГОРОДА - ИСПРАВЛЕННАЯ ВЕРСИЯ С ЗАЩИТОЙ ОТ ДЮПА ---
BUILDINGS = {
    'house': {
        'name': '🏠 Жилой дом',
        'cost': 10000,
        'income': 80,
        'upgrade_cost_multiplier': 1.8,
        'max_level': 20
    },
    'shop': {
        'name': '🏪 Магазин',
        'cost': 30000,
        'income': 200,
        'upgrade_cost_multiplier': 1.9,
        'max_level': 15
    },
    'factory': {
        'name': '🏭 Фабрика',
        'cost': 100000,
        'income': 600,
        'upgrade_cost_multiplier': 2.0,
        'max_level': 10
    },
    'bank': {
        'name': '🏦 Банк MORPH',
        'cost': 500000,
        'income': 2500,
        'upgrade_cost_multiplier': 2.2,
        'max_level': 5
    },
    'crypto_farm': {
        'name': '⛏️ Крипто-ферма',
        'cost': 2000000,
        'income': 12000,
        'upgrade_cost_multiplier': 2.5,
        'max_level': 3
    }
}

def save_cities():
    cities_ref.set(user_cities)

def calculate_city_income(city):
    """Рассчитывает общий доход города в час с защитой от переполнения"""
    total_income = 0
    base_multiplier = 1.0 + (city['level'] - 1) * 0.1  # +10% за уровень
    
    for building_type, level in city.get('buildings', {}).items():
        if building_type in BUILDINGS:
            building_info = BUILDINGS[building_type]
            # 🔒 Защита от слишком больших значений
            building_income = min(building_info['income'] * level * base_multiplier, 1000000)
            total_income += building_income
    
    return int(total_income)

def calculate_city_value(city):
    """Рассчитывает примерную стоимость города"""
    base_value = city.get('creation_cost', 70000)
    building_value = 0
    level_value = city['level'] * 20000
    population_value = city['population'] * 100
    
    for building_type, level in city.get('buildings', {}).items():
        if building_type in BUILDINGS:
            building_cost = BUILDINGS[building_type]['cost']
            building_value += building_cost * level * 0.7
    
    total_value = base_value + building_value + level_value + population_value
    return int(total_value)

def check_city_cooldown(user_id: int, command: str) -> bool:
    """Проверяет кулдаун на команды города"""
    current_time = time.time()
    key = f"{user_id}_city_{command}"
    
    if key in command_cooldowns:
        if current_time - command_cooldowns[key] < 2:  # 2 секунды между вызовами
            return False
    
    command_cooldowns[key] = current_time
    return True

# Команда создания города
@router.message(lambda message: message.text and message.text.lower().startswith('создать город'))
async def start_city_creation(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type != 'private':
        await message.reply(
            "🔒 <b>Создание города доступно только в личных сообщениях с ботом!</b>\n\n"
            "💡 Перейдите в ЛС к боту и используйте команду там.\n"
            "🏙️ Но вы можете просматривать свой город в любом чате командой: <code>мой город</code>",
            parse_mode="HTML"
        )
        return
    
    user_id = message.from_user.id
    
    if user_id in user_cities:
        await show_city(message)
        return
    
    init_user(user_id, message.from_user.username)
    creation_cost = 70000
    
    if users_data[user_id]['balance'] < creation_cost:
        await message.reply(
            f"❌ <b>Недостаточно MORPH для создания города!</b>\n\n"
            f"💰 <b>Нужно:</b> {format_amount(creation_cost)} MORPH\n"
            f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
            f"💡 Пополните баланс и попробуйте снова!",
            parse_mode="HTML"
        )
        return
    
    parts = message.text.split()
    
    if len(parts) >= 3:
        city_name = ' '.join(parts[2:]).strip()
        
        if len(city_name) > 32:
            await message.reply("❌ Название города не может превышать 32 символа!")
            return
        
        if len(city_name) < 2:
            await message.reply("❌ Название города должно содержать минимум 2 символа!")
            return
        
        if city_name.lower() in city_names:
            await message.reply(
                f"❌ Город с названием <b>'{city_name}'</b> уже существует!\n"
                f"📝 Придумайте другое уникальное название.",
                parse_mode="HTML"
            )
            return
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Да, создать город", callback_data=f"confirm_city_{user_id}_{city_name.replace(' ', '_')}")
        builder.button(text="❌ Отменить", callback_data=f"cancel_city_{user_id}")
        builder.adjust(2)
        
        await message.reply(
            f"🏗️ <b>ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ ГОРОДА</b>\n\n"
            f"🏙️ <b>Название:</b> {city_name}\n"
            f"💰 <b>Стоимость:</b> {format_amount(creation_cost)} MORPH\n"
            f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
            f"📊 <b>После создания вы получите:</b>\n"
            f"• 🏙️ Город {city_name}\n"
            f"• 👥 100 жителей\n"
            f"• 🏗️ Возможность строить здания\n"
            f"• 💰 Пассивный доход\n\n"
            f"<b>Вы уверены, что хотите создать город?</b>",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    else:
        city_creation[user_id] = {'step': 'waiting_name'}
        
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отменить создание", callback_data=f"cancel_city_{user_id}")
        
        await message.reply(
            "🏗️ <b>СОЗДАНИЕ ГОРОДА MORPH</b>\n\n"
            "💰 <b>Стоимость создания:</b> 70,000 MORPH\n"
            "💳 <b>Ваш баланс:</b> " + format_amount(users_data[user_id]['balance']) + " MORPH\n\n"
            "📝 Придумайте название для вашего города:\n"
            "• Максимум 32 символа\n"
            "• Минимум 2 символа\n"
            "• Название должно быть уникальным\n\n"
            "<i>Примеры: Челябинск, Морфоград, Столица Успеха</i>\n\n"
            "💡 Или нажмите кнопку ниже для отмена",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )

# Обработка подтверждения создания города
@router.callback_query(lambda c: c.data.startswith('confirm_city_'))
async def confirm_city_creation(callback: CallbackQuery):
    data = callback.data.split('_')
    user_id = int(data[2])
    city_name = data[3].replace('_', ' ')
    
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша операция!", show_alert=True)
        return
    
    init_user(user_id, callback.from_user.username)
    creation_cost = 70000
    
    if users_data[user_id]['balance'] < creation_cost:
        await callback.message.edit_text(
            f"❌ <b>Недостаточно MORPH для создания города!</b>\n\n"
            f"💰 <b>Нужно:</b> {format_amount(creation_cost)} MORPH\n"
            f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    if city_name.lower() in city_names:
        await callback.message.edit_text(
            f"❌ Город с названием <b>'{city_name}'</b> уже существует!\n"
            f"📝 Придумайте другое уникальное название.",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    users_data[user_id]['balance'] -= creation_cost
    save_users()
    
    user_cities[user_id] = {
        'name': city_name,
        'level': 1,
        'buildings': {},
        'population': 100,
        'balance': 0,
        'last_claim': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'created_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_earned': 0,
        'creation_cost': creation_cost
    }
    city_names.add(city_name.lower())
    save_cities()
    
    if user_id in city_creation:
        del city_creation[user_id]
    
    city = user_cities[user_id]
    
    success_text = (
        f"🎉 <b>ГОРОД УСПЕШНО СОЗДАН!</b>\n\n"
        f"🏙️ <b>Название:</b> {city['name']}\n"
        f"💰 <b>Стоимость создания:</b> {format_amount(creation_cost)} MORPH\n"
        f"👥 <b>Население:</b> {format_amount(city['population'])} человек\n"
        f"📅 <b>Основан:</b> {city['created_date']}\n\n"
        f"🏗️ <b>Доступные действия:</b>\n"
        f"• <code>мой город</code> - управление городом\n"
        f"• <code>построить дом</code> - начать строительство\n"
        f"• <code>собрать налоги</code> - получить доход\n"
        f"• <code>улучшить город</code> - повысить уровень\n"
        f"• <code>продать город</code> - продать город за {format_amount(int(creation_cost * 0.8))} MORPH\n\n"
        f"💡 <b>Совет:</b> стройте здания чтобы увеличивать пассивный доход!"
    )
    
    await callback.message.edit_text(success_text, parse_mode="HTML")
    await callback.answer("Город успешно создан!")

# Команда "мой город" - РАБОТАЕТ В ЛИЧКЕ И ЧАТАХ
@router.message(lambda message: message.text and message.text.lower().strip() in ["мой город", "город"])
async def show_city(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    init_user(user_id, message.from_user.username)
    
    if user_id not in user_cities:
        if message.chat.type == 'private':
            await message.reply(
                "❌ У вас еще нет города!\n\n"
                "🏗️ Чтобы создать город, используйте команду:\n"
                "<code>создать город [название]</code>\n\n"
                "💰 <b>Стоимость создания:</b> 70,000 MORPH",
                parse_mode="HTML"
            )
        else:
            await message.reply(
                "❌ У вас еще нет города!\n\n"
                "🏗️ Чтобы создать город, перейдите в ЛС к боту и используйте:\n"
                "<code>создать город [название]</code>\n\n"
                "💰 <b>Стоимость создания:</b> 70,000 MORPH",
                parse_mode="HTML"
            )
        return
    
    city = user_cities[user_id]
    
    total_income = calculate_city_income(city)
    city_value = calculate_city_value(city)
    sell_price = int(city.get('creation_cost', 70000) * 0.8)
    
    try:
        last_claim = datetime.strptime(city['last_claim'], '%Y-%m-%d %H:%M:%S')
        time_since_last_claim = datetime.now() - last_claim
        hours_passed = int(time_since_last_claim.total_seconds() // 3600)
        available_income = total_income * hours_passed
    except:
        available_income = 0
    
    city_text = (
        f"🏙️ <b>ГОРОД {city['name'].upper()}</b>\n\n"
        f"📊 <b>Уровень города:</b> {city['level']}\n"
        f"👥 <b>Население:</b> {format_amount(city['population'])} чел.\n"
        f"💰 <b>Баланс города:</b> {format_amount(city['balance'])} MORPH\n"
        f"📈 <b>Общий заработок:</b> {format_amount(city['total_earned'])} MORPH\n"
        f"💵 <b>Доход в час:</b> {format_amount(total_income)} MORPH\n"
    )
    
    if available_income > 0:
        city_text += f"🕒 <b>Доступно к сбору:</b> {format_amount(available_income)} MORPH\n"
    
    city_text += f"💎 <b>Стоимость города:</b> ~{format_amount(city_value)} MORPH\n\n"
    
    if city.get('buildings'):
        city_text += "🏗️ <b>ПОСТРОЙКИ:</b>\n"
        for building_type, level in city['buildings'].items():
            if building_type in BUILDINGS:
                building_info = BUILDINGS[building_type]
                income = building_info['income'] * level
                city_text += f"• {building_info['name']} (ур. {level}): +{format_amount(income)} MORPH/час\n"
    else:
        city_text += "🔄 <b>Зданий пока нет</b>\n\n"
    
    city_text += (
        f"\n🛠️ <b>КОМАНДЫ:</b>\n"
        f"• <code>построить дом</code> - построить здание\n"
        f"• <code>собрать налоги</code> - получить доход\n"
        f"• <code>улучшить город</code> - повысить уровень\n"
        f"• <code>продать город</code> - продать за {format_amount(sell_price)} MORPH"
    )
    
    await message.reply(city_text, parse_mode="HTML")

# Команда построить здание
@router.message(lambda message: message.text and message.text.lower().startswith('построить'))
async def build_in_city(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    if user_id not in user_cities:
        await message.reply("❌ У вас нет города! Создайте город командой: <code>создать город [название]</code>", parse_mode="HTML")
        return
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: проверка кулдауна
    if not check_city_cooldown(user_id, "build"):
        await message.reply("⏳ Слишком частые запросы! Подождите 2 секунды.")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "🏗️ <b>ПОСТРОЙКА ЗДАНИЙ</b>\n\n"
            "❌ Использование: <b>построить [тип]</b>\n\n"
            "🏠 <b>Доступные здания:</b>\n"
            "• <code>построить дом</code> - 🏠 Жилой дом (10,000 MORPH)\n"
            "• <code>построить магазин</code> - 🏪 Магазин (30,000 MORPH)\n"
            "• <code>построить фабрику</code> - 🏭 Фабрика (100,000 MORPH)\n"
            "• <code>построить банк</code> - 🏦 Банк (500,000 MORPH)\n"
            "• <code>построить ферму</code> - ⛏️ Крипто-ферма (2,000,000 MORPH)\n\n"
            "💡 Каждое здание приносит пассивный доход!",
            parse_mode="HTML"
        )
        return
    
    building_type = parts[1].lower()
    city = user_cities[user_id]
    
    # Определяем тип здания
    building_map = {
        'дом': 'house',
        'магазин': 'shop', 
        'фабрику': 'factory',
        'фабрика': 'factory',
        'банк': 'bank',
        'ферму': 'crypto_farm',
        'ферма': 'crypto_farm'
    }
    
    if building_type not in building_map:
        await message.reply("❌ Неизвестный тип здания! Используйте: дом, магазин, фабрику, банк, ферму")
        return
    
    building_key = building_map[building_type]
    building_info = BUILDINGS[building_key]
    
    # Проверяем уровень города для некоторых зданий
    if building_key == 'crypto_farm' and city['level'] < 3:
        await message.reply("❌ Для постройки крипто-фермы нужен город 3+ уровня!")
        return
    
    if building_key == 'bank' and city['level'] < 2:
        await message.reply("❌ Для постройки банка нужен город 2+ уровня!")
        return
    
    # Проверяем баланс
    if users_data[user_id]['balance'] < building_info['cost']:
        await message.reply(
            f"❌ Недостаточно MORPH для постройки!\n"
            f"💰 Нужно: {format_amount(building_info['cost'])} MORPH\n"
            f"💳 Ваш баланс: {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode="HTML"
        )
        return
    
    # Проверяем максимальный уровень здания
    current_level = city.get('buildings', {}).get(building_key, 0)
    if current_level >= building_info['max_level']:
        await message.reply(f"❌ Достигнут максимальный уровень для этого здания ({building_info['max_level']})!")
        return
    
    # Строим здание
    users_data[user_id]['balance'] -= building_info['cost']
    
    # Инициализируем buildings если нет
    if 'buildings' not in city:
        city['buildings'] = {}
    
    # Увеличиваем уровень здания
    city['buildings'][building_key] = current_level + 1
    
    # Увеличиваем население
    city['population'] += random.randint(10, 50)
    
    save_users()
    save_cities()
    
    await message.reply(
        f"✅ <b>ЗДАНИЕ ПОСТРОЕНО!</b>\n\n"
        f"{building_info['name']} (уровень {city['buildings'][building_key]})\n"
        f"💰 Стоимость: {format_amount(building_info['cost'])} MORPH\n"
        f"📈 Доход: +{format_amount(building_info['income'])} MORPH/час\n"
        f"👥 Новое население: {format_amount(city['population'])}\n\n"
        f"🏙️ Продолжайте развивать город!",
        parse_mode="HTML"
    )

# Команда сбора налогов - ИСПРАВЛЕННАЯ ВЕРСИЯ С ЗАЩИТОЙ ОТ ДЮПА
@router.message(lambda message: message.text and message.text.lower() in ["ОКНРНИЫКМ5544554545435ААСЫУКМЕ67"])
async def collect_taxes(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: проверка кулдауна
    if not check_city_cooldown(user_id, "taxes"):
        await message.reply("⏳ Слишком частые запросы! Подождите 2 секунды.")
        return
    
    if user_id not in user_cities:
        await message.reply("❌ У вас нет города!")
        return
    
    city = user_cities[user_id]
    
    # Рассчитываем доход
    total_income = calculate_city_income(city)
    
    try:
        last_claim = datetime.strptime(city['last_claim'], '%Y-%m-%d %H:%M:%S')
        time_since_last_claim = datetime.now() - last_claim
        hours_passed = time_since_last_claim.total_seconds() / 3600
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: проверяем прошло ли минимум 24 часа
        if hours_passed < 24:
            time_left = 24 - hours_passed
            hours_left = int(time_left)
            minutes_left = int((time_left - hours_left) * 60)
            
            await message.reply(
                f"⏳ <b>Налоги можно собирать раз в 24 часа!</b>\n\n"
                f"💰 Накопленный доход: {format_amount(int(total_income * hours_passed))} MORPH\n"
                f"🕒 До следующего сбора: <b>{hours_left}ч {minutes_left}м</b>\n\n"
                f"💡 Возвращайтесь через {hours_left} часов для сбора налогов",
                parse_mode="HTML"
            )
            return
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: ограничиваем максимум 24 часами дохода
        hours_for_income = min(hours_passed, 24)  # Не больше 24 часов
        available_income = total_income * hours_for_income
        
    except Exception as e:
        # Если ошибка в данных, устанавливаем минимальный доход за 24 часа
        available_income = total_income * 24
        print(f"Ошибка расчета налогов: {e}")
    
    if available_income <= 0:
        await message.reply("💤 <b>Налоги уже собраны!</b>\n\nПриходите через 24 часа для нового сбора.", parse_mode="HTML")
        return
    
    # Выплачиваем доход
    users_data[user_id]['balance'] += available_income
    city['balance'] += available_income
    city['total_earned'] += available_income
    city['last_claim'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    save_users()
    save_cities()
    
    await message.reply(
        f"💰 <b>НАЛОГИ СОБРАНЫ!</b>\n\n"
        f"🏙️ Город: {city['name']}\n"
        f"💵 Собрано: {format_amount(int(available_income))} MORPH\n"
        f"⏱️ За период: 24 часа\n"
        f"📈 Общий заработок: {format_amount(city['total_earned'])} MORPH\n\n"
        f"💳 Ваш баланс: {format_amount(users_data[user_id]['balance'])} MORPH\n"
        f"🔄 Следующий сбор через 24 часа",
        parse_mode="HTML"
    )

# Команда улучшения города
@router.message(lambda message: message.text and message.text.lower() in ["улучшить город", "улучшить"])
async def upgrade_city(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: проверка кулдауна
    if not check_city_cooldown(user_id, "upgrade"):
        await message.reply("⏳ Слишком частые запросы! Подождите 2 секунды.")
        return
    
    if user_id not in user_cities:
        await message.reply("❌ У вас нет города!")
        return
    
    city = user_cities[user_id]
    current_level = city['level']
    
    # Стоимость улучшения
    upgrade_cost = 50000 * (current_level ** 2)  # Увеличивается с уровнем
    
    if users_data[user_id]['balance'] < upgrade_cost:
        await message.reply(
            f"❌ Недостаточно MORPH для улучшения!\n"
            f"💰 Нужно: {format_amount(upgrade_cost)} MORPH\n"
            f"💳 Ваш баланс: {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode="HTML"
        )
        return
    
    # Улучшаем город
    users_data[user_id]['balance'] -= upgrade_cost
    city['level'] += 1
    city['population'] += random.randint(100, 300)
    
    save_users()
    save_cities()
    
    await message.reply(
        f"🎉 <b>ГОРОД УЛУЧШЕН!</b>\n\n"
        f"🏙️ {city['name']}\n"
        f"📊 Новый уровень: {city['level']}\n"
        f"💰 Стоимость улучшения: {format_amount(upgrade_cost)} MORPH\n"
        f"👥 Новое население: {format_amount(city['population'])}\n\n"
        f"💡 Доход от всех зданий увеличен на 10%!",
        parse_mode="HTML"
    )

# Команда продажи города
@router.message(lambda message: message.text and message.text.lower() == 'продать город')
async def sell_city(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: проверка кулдауна
    if not check_city_cooldown(user_id, "sell"):
        await message.reply("⏳ Слишком частые запросы! Подождите 2 секунды.")
        return
    
    if user_id not in user_cities:
        await message.reply(
            "❌ У вас нет города для продажи!\n\n"
            "🏗️ Чтобы создать город, используйте команду:\n"
            "<code>создать город [название]</code>\n\n"
            "💰 <b>Стоимость создания:</b> 70,000 MORPH",
            parse_mode="HTML"
        )
        return
    
    city = user_cities[user_id]
    sell_price = int(city.get('creation_cost', 70000) * 0.8)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, продать город", callback_data=f"confirm_sell_city_{user_id}")
    builder.button(text="❌ Отменить", callback_data=f"cancel_sell_city_{user_id}")
    builder.adjust(2)
    
    city_value = calculate_city_value(city)
    
    await message.reply(
        f"🏙️ <b>ПОДТВЕРЖДЕНИЕ ПРОДАЖИ ГОРОДА</b>\n\n"
        f"🏙️ <b>Город:</b> {city['name']}\n"
        f"📊 <b>Уровень:</b> {city['level']}\n"
        f"👥 <b>Население:</b> {format_amount(city['population'])}\n"
        f"💰 <b>Стоимость продажи:</b> {format_amount(sell_price)} MORPH\n"
        f"💎 <b>Примерная стоимость города:</b> {format_amount(city_value)} MORPH\n\n"
        f"⚠️ <b>Внимание!</b>\n"
        f"• Вы получите 80% от стоимости создания\n"
        f"• Все здания и прогресс будут утеряны\n"
        f"• Город будет удален безвозвратно\n\n"
        f"<b>Вы уверены, что хотите продать город?</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

# Обработка подтверждения продажи города
@router.callback_query(lambda c: c.data.startswith('confirm_sell_city_'))
async def confirm_sell_city(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[3])
    
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша операция!", show_alert=True)
        return
    
    if user_id not in user_cities:
        await callback.answer("❌ Город не найден!", show_alert=True)
        return
    
    city = user_cities[user_id]
    sell_price = int(city.get('creation_cost', 70000) * 0.8)
    
    users_data[user_id]['balance'] += sell_price
    
    city_name = city['name']
    city_names.remove(city_name.lower())
    del user_cities[user_id]
    
    save_users()
    save_cities()
    
    await callback.message.edit_text(
        f"💰 <b>ГОРОД ПРОДАН!</b>\n\n"
        f"🏙️ <b>Город:</b> {city_name}\n"
        f"💸 <b>Получено:</b> {format_amount(sell_price)} MORPH\n"
        f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"💡 Вы можете создать новый город командой:\n"
        f"<code>создать город [название]</code>",
        parse_mode="HTML"
    )
    await callback.answer("Город успешно продан!")

# Обработка отмены продажи города
@router.callback_query(lambda c: c.data.startswith('cancel_sell_city_'))
async def cancel_sell_city(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[3])
    
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша операция!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "❌ <b>Продажа города отменена</b>\n\n"
        "🏙️ Ваш город сохранен!\n"
        "💡 Продолжайте развивать свой город!",
        parse_mode="HTML"
    )
    await callback.answer("Продажа отменена")

# Обработка отмены создания города
@router.callback_query(lambda c: c.data.startswith('cancel_city_'))
async def cancel_city_creation(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[2])
    
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша операция!", show_alert=True)
        return
    
    if user_id in city_creation:
        del city_creation[user_id]
    
    await callback.message.edit_text(
        "❌ <b>Создание города отменено</b>\n\n"
        "💡 Чтобы начать заново, используйте команду:\n"
        "<code>создать город Название</code>",
        parse_mode="HTML"
    )
    await callback.answer("Создание города отменена")

# Обработка ввода названия города
@router.message(lambda message: message.from_user.id in city_creation and city_creation[message.from_user.id]['step'] == 'waiting_name')
async def process_city_name(message: types.Message):
    user_id = message.from_user.id
    
    init_user(user_id, message.from_user.username)
    creation_cost = 70000
    
    if users_data[user_id]['balance'] < creation_cost:
        await message.reply(
            f"❌ <b>Баланс изменился! Недостаточно MORPH для создания города!</b>\n\n"
            f"💰 <b>Нужно:</b> {format_amount(creation_cost)} MORPH\n"
            f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode="HTML"
        )
        if user_id in city_creation:
            del city_creation[user_id]
        return
    
    city_name = message.text.strip()
    
    if len(city_name) > 32:
        await message.reply("❌ Название города не может превышать 32 символа! Попробуйте снова:")
        return
    
    if len(city_name) < 2:
        await message.reply("❌ Название города должно содержать минимум 2 символа! Попробуйте снова:")
        return
    
    if city_name.lower() in city_names:
        await message.reply(
            f"❌ Город с названием <b>'{city_name}'</b> уже существует!\n"
            f"📝 Придумайте другое уникальное название:",
            parse_mode="HTML"
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, создать город", callback_data=f"confirm_city_{user_id}_{city_name.replace(' ', '_')}")
    builder.button(text="❌ Отменить", callback_data=f"cancel_city_{user_id}")
    builder.adjust(2)
    
    await message.reply(
        f"🏗️ <b>ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ ГОРОДА</b>\n\n"
        f"🏙️ <b>Название:</b> {city_name}\n"
        f"💰 <b>Стоимость:</b> {format_amount(creation_cost)} MORPH\n"
        f"💳 <b>Ваш баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"📊 <b>После создания вы получите:</b>\n"
        f"• 🏙️ Город {city_name}\n"
        f"• 👥 100 жителей\n"
        f"• 🏗️ Возможность строить здания\n"
        f"• 💰 Пассивный доход\n\n"
        f"<b>Вы уверены, что хотите создать город?</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

#РУЛЕТКА
# Функции для рулетки
def get_roulette_color_emoji(number):
    if number == 0:
        return "🟢"
    elif number in [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]:
        return "🔴"
    else:
        return "⚫"

def calculate_roulette_payout(choice, number):
    """Рассчитывает выигрыш для ставки в рулетке"""
    if number == 0:  # Зеро
        if choice == '0':
            return 36
        return 0
    
    # Проверка нескольких чисел (разделенных пробелами)
    if ' ' in choice:
        numbers = choice.split()
        if str(number) in numbers:
            return 36 / len(numbers)
    
    # Проверка конкретного числа
    if choice.isdigit() and int(choice) == number:
        return 36
    
    # Проверка цвета
    if choice in ['красное', 'красный', 'к'] and number in [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]:
        return 2
    if choice in ['черное', 'черный', 'ч'] and number in [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]:
        return 2
    
    # Проверка четности
    if choice in ['чет', 'четное'] and number % 2 == 0 and number != 0:
        return 2
    if choice in ['нечет', 'нечетное'] and number % 2 == 1:
        return 2
    
    # Проверка диапазона
    if choice in ['низкое', 'низкие', 'малое'] and 1 <= number <= 18:
        return 2
    if choice in ['высокое', 'высокие', 'большое'] and 19 <= number <= 36:
        return 2
    
    # Проверка диапазона чисел
    if '-' in choice:
        try:
            start, end = map(int, choice.split('-'))
            if start <= number <= end:
                count = end - start + 1
                return max(2, int(36 / count))
        except:
            pass
    
    return 0

# История рулетки
roulette_history = []

def add_to_roulette_history(number, color_text, color_emoji):
    """Добавляет результат в историю рулетки"""
    global roulette_history
    result = {
        'number': number,
        'color': color_text,
        'emoji': color_emoji,
        'time': datetime.now().strftime('%H:%M:%S')
    }
    roulette_history.append(result)
    # Ограничиваем историю 10 последними результатами
    if len(roulette_history) > 10:
        roulette_history = roulette_history[-10:]

# Обработчики рулетки
@router.message(lambda message: message.text and message.text.lower().startswith(('рулетка ', 'рул ')))
async def roulette_bet(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    
    # Инициализируем рулетку для чата если её нет
    if chat_id not in active_roulettes:
        active_roulettes[chat_id] = {
            'bets': {},
            'spinning': False,
            'end_time': 0
        }
    
    roulette_data = active_roulettes[chat_id]
    
    if roulette_data['spinning']:
        left = max(0, roulette_data['end_time'] - int(time.time()))
        await message.reply(f'⏳ Рулетка уже крутится! До окончания: {left} сек.')
        return
    
    parts = message.text.split()
    if len(parts) < 3:
        help_text = (
            "🎰 <b>ПРОСТАЯ РУЛЕТКА</b>\n\n"
            "🔹 <b>Формат:</b> <code>рул [ставка] [на что ставим]</code>\n\n"
            "🎯 <b>Примеры ставок:</b>\n"
            "• <code>рул 100 7</code> - на число 7\n"
            "• <code>рул 100 1 4 7 12</code> - на несколько чисел\n"
            "• <code>рул 100 1-18</code> - на числа от 1 до 18\n"
            "• <code>рул 100 красное</code> - на красный цвет\n"
            "• <code>рул 100 черное</code> - на черный цвет\n"
            "• <code>рул 100 чет</code> - на четные\n\n"
            "💡 <b>Можно ставить несколько раз!</b>\n"
            "🚀 <b>Запуск:</b> <code>го</code>\n"
            "📋 <b>Ставки:</b> <code>ставки</code>\n"
            "📊 <b>История:</b> <code>лог</code>\n"
            "❌ <b>Отмена:</b> <code>отменить</code>"
        )
        await message.reply(help_text, parse_mode='HTML')
        return
    
    try:
        bet = parse_amount(parts[1])
        if bet is None or bet <= 0:
            await message.reply('❌ Ставка должна быть положительной!')
            return
        
        # Берем все оставшиеся части как выбор
        choice_parts = parts[2:]
        choice = ' '.join(choice_parts).lower()
        
        # Нормализация выбора
        if choice in ['к', 'красный']:
            choice = 'красное'
        elif choice in ['ч', 'черный']:
            choice = 'черное'
        elif choice in ['н', 'low']:
            choice = 'низкое'
        elif choice in ['в', 'high']:
            choice = 'высокое'
        elif choice in ['ч', 'even']:
            choice = 'чет'
        elif choice in ['н', 'odd']:
            choice = 'нечет'
        
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.first_name
        init_user(user_id, username)
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: Проверяем баланс ДО списания
        current_balance = users_data[user_id]['balance']
        if current_balance < bet:
            await message.reply(f'❌ Недостаточно MORPH для ставки! Баланс: {format_amount(current_balance)} MORPH')
            return
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: Проверяем минимальную ставку
        if bet < 100:
            await message.reply('❌ Минимальная ставка: 100 MORPH!')
            return
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: Проверяем валидность выбора
        valid = False
        
        # Проверка нескольких чисел
        if ' ' in choice:
            numbers = choice.split()
            if all(num.isdigit() and 0 <= int(num) <= 36 for num in numbers):
                valid = True
                if len(numbers) > 10:  # Ограничение на количество чисел
                    await message.reply('❌ Можно ставить максимум на 10 чисел за раз!')
                    return
        
        # Проверка одиночного числа
        elif choice.isdigit() and 0 <= int(choice) <= 36:
            valid = True
        
        # Проверка диапазона
        elif '-' in choice:
            try:
                start, end = map(int, choice.split('-'))
                if 0 <= start <= 36 and 0 <= end <= 36 and start <= end:
                    valid = True
                    if (end - start + 1) > 18:  # Ограничение на размер диапазона
                        await message.reply('❌ Слишком большой диапазон! Максимум 18 чисел.')
                        return
            except:
                valid = False
        
        # Проверка цветов и четности
        elif choice in ['красное', 'черное', 'чет', 'нечет', 'низкое', 'высокое']:
            valid = True
        
        if not valid:
            await message.reply('❌ Неверный тип ставки! Используйте: числа (0-36), диапазон (1-18), цвет (красное/черное), чет/нечет')
            return
        
        # 🔒 ЗАЩИТА ОТ ДЮПА: Списываем ставку только после всех проверок
        users_data[user_id]['balance'] -= bet
        save_users()  # Сохраняем сразу после списания
        
        # Добавляем ставку
        if user_id not in roulette_data['bets']:
            roulette_data['bets'][user_id] = []
        
        bet_data = {
            'username': username,
            'bet': bet,
            'choice': choice
        }
        
        roulette_data['bets'][user_id].append(bet_data)
        
        # Форматируем ответ в зависимости от типа ставки
        if ' ' in choice:
            numbers = choice.split()
            choice_text = f"числа: {', '.join(numbers)}"
        elif '-' in choice:
            choice_text = f"диапазон: {choice}"
        else:
            choice_text = choice
        
        await message.reply(
            f'✅ <b>Ставка принята!</b>\n'
            f'👤 Игрок: {username}\n'
            f'💰 Сумма: {format_amount(bet)} MORPH\n'
            f'🎯 На: {choice_text}\n\n'
            f'💡 Можно сделать ещё ставки или запустить рулетку командой <code>го</code>',
            parse_mode='HTML'
        )
        
    except Exception as e:
        await message.reply(f'❌ Ошибка в ставке! Проверьте формат.')

@router.message(lambda message: message.text and message.text.lower() == 'го')
async def roulette_go(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    
    if chat_id not in active_roulettes:
        await message.reply('❌ Нет активных ставок в этом чате!')
        return
    
    roulette_data = active_roulettes[chat_id]
    
    if roulette_data['spinning']:
        left = max(0, roulette_data['end_time'] - int(time.time()))
        await message.reply(f'⏳ Рулетка уже крутится! До окончания: {left} сек.')
        return
    
    # Проверяем есть ли ставки
    total_bets = 0
    for user_bets in roulette_data['bets'].values():
        for bet in user_bets:
            total_bets += bet['bet']
    
    if total_bets == 0:
        await message.reply('❌ Нет ставок для розыгрыша!')
        return
    
    # Запускаем рулетку
    roulette_data['spinning'] = True
    roulette_data['end_time'] = int(time.time()) + 3
    
    # Отправляем сообщение о запуске
    spin_msg = await message.reply('🎰 <b>Рулетка крутится...</b>', parse_mode='HTML')
    await asyncio.sleep(3)
    
    # Генерируем результат
    number = random.randint(0, 36)
    color_emoji = get_roulette_color_emoji(number)
    color_text = "зеленое" if number == 0 else "красное" if color_emoji == "🔴" else "черное"
    
    # Добавляем в историю
    add_to_roulette_history(number, color_text, color_emoji)
    
    # Обрабатываем выигрыши
    result_text = f'🎰 <b>РЕЗУЛЬТАТ РУЛЕТКИ</b>\n\n'
    result_text += f'🎲 Выпало: <b>{number} {color_emoji} ({color_text})</b>\n\n'
    
    total_won = 0
    winners = []
    detailed_results = []
    
    # Создаем копию ставок для безопасной обработки
    bets_copy = roulette_data['bets'].copy()
    
    # Обрабатываем все ставки всех пользователей
    for user_id, user_bets in bets_copy.items():
        user_total_won = 0
        user_bets_details = []
        
        total_bet_amount = sum(b['bet'] for b in user_bets)
        
        for bet in user_bets:
            payout = calculate_roulette_payout(bet['choice'], number)
            if payout > 0:
                win_amount = int(bet['bet'] * payout)  # 🔒 Округляем до целого
                user_total_won += win_amount
                user_bets_details.append(f"✅ {bet['choice']}: +{format_amount(win_amount)} MORPH")
            else:
                user_bets_details.append(f"❌ {bet['choice']}: -{format_amount(bet['bet'])} MORPH")
        
        # Обновляем баланс и статистику через правильные функции
        if user_total_won > 0:
            total_won += user_total_won
            username = user_bets[0]['username'] if user_bets else 'Unknown'
            winners.append(f"👤 {username}: +{format_amount(user_total_won)} MORPH")
            
            # Добавляем детализацию
            detailed_results.append(f"\n<b>{username}:</b>\n" + "\n".join(user_bets_details))
            
            # Используем правильную функцию для обновления баланса и лидерборда
            add_win_to_user(user_id, user_total_won, total_bet_amount)
            users_data[user_id]['games_played'] += 1
            
            # Добавляем в историю игр
            add_game_to_history(user_id, 'Рулетка', total_bet_amount, 'win', user_total_won)
        else:
            # Проигрыш - добавляем в историю
            add_game_to_history(user_id, 'Рулетка', total_bet_amount, 'lose', 0)
            users_data[user_id]['games_played'] += 1
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: Сохраняем изменения балансов
    save_users()
    
    # Формируем итоговое сообщение
    if winners:
        result_text += '🏆 <b>ПОБЕДИТЕЛИ:</b>\n' + '\n'.join(winners) + '\n'
    
    # Добавляем детализацию всех ставок
    if detailed_results:
        result_text += '\n<b>ДЕТАЛИ СТАВОК:</b>' + ''.join(detailed_results)
    
    if not winners:
        result_text += '\n😢 <b>Нет победителей в этом раунде</b>'
    
    result_text += f'\n\n💰 <b>Общий выигрыш:</b> {format_amount(total_won)} MORPH'
    
    await spin_msg.edit_text(result_text, parse_mode='HTML')
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: Очищаем ставки только после успешного розыгрыша
    roulette_data['bets'] = {}
    roulette_data['spinning'] = False
    roulette_data['end_time'] = 0

@router.message(lambda message: message.text and message.text.lower() == 'ставки')
async def roulette_show_bets(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    
    if chat_id not in active_roulettes or not active_roulettes[chat_id]['bets']:
        await message.reply('📊 <b>Ставок пока нет</b>\n💡 Сделайте ставку: <code>рул [ставка] [число/диапазон/цвет]</code>', parse_mode='HTML')
        return
    
    roulette_data = active_roulettes[chat_id]
    text = '📊 <b>ТЕКУЩИЕ СТАВКИ:</b>\n\n'
    total_bets = 0
    
    for user_id, user_bets in roulette_data['bets'].items():
        user_total = sum(bet['bet'] for bet in user_bets)
        total_bets += user_total
        username = user_bets[0]['username'] if user_bets else 'Unknown'
        
        text += f'👤 <b>{username}:</b>\n'
        for bet in user_bets:
            # Форматируем отображение выбора
            if ' ' in bet['choice']:
                numbers = bet['choice'].split()
                choice_text = f"числа: {', '.join(numbers)}"
            elif '-' in bet['choice']:
                choice_text = f"диапазон: {bet['choice']}"
            else:
                choice_text = bet['choice']
                
            text += f'   • {format_amount(bet["bet"])} MORPH на <code>{choice_text}</code>\n'
        text += f'   <b>Всего:</b> {format_amount(user_total)} MORPH\n\n'
    
    text += f'💰 <b>Общая сумма ставок:</b> {format_amount(total_bets)} MORPH\n\n'
    text += '🚀 <b>Запустить рулетку:</b> <code>го</code>'
    
    await message.reply(text, parse_mode='HTML')

@router.message(lambda message: message.text and message.text.lower() in ['отменить', 'отменить ставку'])
async def cancel_roulette_bet(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if chat_id not in active_roulettes or user_id not in active_roulettes[chat_id]['bets']:
        await message.reply('❌ У вас нет активных ставок для отмены!')
        return
    
    roulette_data = active_roulettes[chat_id]
    
    if roulette_data['spinning']:
        await message.reply('❌ Нельзя отменить ставку во время кручения рулетки!')
        return
    
    # Возвращаем все ставки пользователя
    user_bets = roulette_data['bets'][user_id]
    total_returned = 0
    
    for bet in user_bets:
        users_data[user_id]['balance'] += bet['bet']
        total_returned += bet['bet']
    
    # Удаляем ставки пользователя
    del roulette_data['bets'][user_id]
    
    # 🔒 ЗАЩИТА ОТ ДЮПА: Сохраняем изменения баланса
    save_users()
    
    await message.reply(
        f'✅ <b>Все ваши ставки отменены!</b>\n'
        f'💰 Возвращено: {format_amount(total_returned)} MORPH',
        parse_mode='HTML'
    )

@router.message(lambda message: message.text and message.text.lower() in ['лог', 'log', 'история', 'history'])
async def show_roulette_log(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if not roulette_history:
        await message.reply(
            "📊 <b>ИСТОРИЯ РУЛЕТКИ</b>\n\n"
            "📝 Пока нет записей в истории\n"
            "🎰 Сыграйте в рулетку, чтобы появилась статистика",
            parse_mode='HTML'
        )
        return
    
    # Создаем текст с историей
    log_text = "📊 <b>ПОСЛЕДНИЕ 10 РЕЗУЛЬТАТОВ РУЛЕТКИ</b>\n\n"
    
    # Показываем результаты в обратном порядке (последний первый)
    for i, result in enumerate(reversed(roulette_history), 1):
        log_text += f"{i}. 🎲 <b>{result['number']}</b> {result['emoji']} - {result['time']}\n"
    
    await message.reply(log_text, parse_mode='HTML')

# Игра 'Лабиринт' удалена по запросу владельца — код удалён.
#БИРЖА
# Реальные акции для отслеживания
REAL_STOCKS = {
    'AAPL': {'name': 'Apple Inc.', 'emoji': '🍎', 'base_price': 150},
    'TSLA': {'name': 'Tesla Inc.', 'emoji': '⚡', 'base_price': 200},
    'GOOGL': {'name': 'Alphabet Inc.', 'emoji': '🔍', 'base_price': 120},
    'AMZN': {'name': 'Amazon.com Inc.', 'emoji': '📦', 'base_price': 130},
    'MSFT': {'name': 'Microsoft Corp.', 'emoji': '💻', 'base_price': 300},
    'META': {'name': 'Meta Platforms', 'emoji': '👥', 'base_price': 250},
    'NVDA': {'name': 'NVIDIA Corp.', 'emoji': '🎮', 'base_price': 400},
    'BTC-USD': {'name': 'Bitcoin', 'emoji': '₿', 'base_price': 30000},
}

# Функции сохранения
def save_stocks():
    stocks_ref.set({str(k): v for k, v in user_stocks.items()})

def save_stock_prices():
    stock_prices_ref.set(stock_prices)

# Инициализация портфеля акций
def init_stock_portfolio(user_id: int):
    if user_id not in user_stocks:
        user_stocks[user_id] = {
            'balance': 0,
            'stocks': {},
            'total_invested': 0,
            'total_profit': 0,
            'created_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        # Инициализируем все акции с 0
        for stock in REAL_STOCKS:
            user_stocks[user_id]['stocks'][stock] = 0
        save_stocks()

# Получение цен акций
async def get_real_stock_prices():
    """Получаем цены акций с имитацией API"""
    try:
        real_volatilities = {
            'AAPL': 0.02,    # 2% волатильность
            'TSLA': 0.05,    # 5% волатильность  
            'GOOGL': 0.015,  # 1.5% волатильность
            'AMZN': 0.025,   # 2.5% волатильность
            'MSFT': 0.018,   # 1.8% волатильность
            'META': 0.03,    # 3% волатильность
            'NVDA': 0.04,    # 4% волатильность
            'BTC-USD': 0.08, # 8% волатильность
        }
        
        new_prices = {}
        for stock, info in REAL_STOCKS.items():
            if stock in stock_prices:
                current_price = stock_prices[stock]
                volatility = real_volatilities.get(stock, 0.02)
                change_percent = random.uniform(-volatility, volatility)
                new_price = current_price * (1 + change_percent)
            else:
                new_price = info['base_price'] * random.uniform(0.8, 1.2)
            
            new_prices[stock] = round(new_price, 2)
        
        return new_prices
        
    except Exception:
        return {stock: round(info['base_price'] * random.uniform(0.5, 2.0), 2) 
                for stock, info in REAL_STOCKS.items()}

# Обновление цен каждые 5 минут
async def update_stock_prices():
    while True:
        try:
            global stock_prices
            new_prices = await get_real_stock_prices()
            if new_prices:
                stock_prices = new_prices
                save_stock_prices()  # Сохраняем в локальное хранилище
            await asyncio.sleep(300)  # 5 минут
        except Exception as e:
            print(f"Ошибка обновления цен акций: {e}")
            await asyncio.sleep(60)

# Инициализация цен при старте
async def initialize_stock_prices():
    global stock_prices
    # Если цены уже есть в локальной базе, используем их
    if not stock_prices:
        stock_prices = await get_real_stock_prices()
        save_stock_prices()

# Команда биржи
@router.message(lambda message: message.text and message.text.lower() in ["биржа", "акции", "stocks"])
async def show_stock_market(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    # Обновляем цены
    global stock_prices
    stock_prices = await get_real_stock_prices()
    
    market_text = "📈 <b>БИРЖА MORPH</b>\n\n"
    market_text += "💹 <b>Котировки в реальном времени:</b>\n\n"
    
    for stock, price in stock_prices.items():
        info = REAL_STOCKS[stock]
        change = ((price - info['base_price']) / info['base_price']) * 100
        change_emoji = "📈" if change >= 0 else "📉"
        
        market_text += (
            f"{info['emoji']} <b>{info['name']}</b> ({stock})\n"
            f"💰 Цена: <b>{price} MORPH</b>\n"
            f"{change_emoji} Изменение: <b>{change:+.2f}%</b>\n\n"
        )
    
    portfolio = user_stocks[user_id]
    portfolio_value = calculate_portfolio_value(user_id)
    
    market_text += (
        f"💼 <b>ВАШ ПОРТФЕЛЬ:</b>\n"
        f"💰 Общая стоимость: <b>{format_amount(int(portfolio_value))} MORPH</b>\n"
        f"💵 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>\n"
        f"📊 Прибыль/убыток: <b>{format_amount(portfolio['total_profit'])} MORPH</b>\n\n"
        f"🛠️ <b>КОМАНДЫ:</b>\n"
        f"• <code>купить AAPL 10</code> - купить акции\n"
        f"• <code>продать TSLA 5</code> - продать акции\n"
        f"• <code>пополнить биржу 5000</code> - пополнить баланс\n"
        f"• <code>вывести с биржи 3000</code> - вывести средства\n"
        f"• <code>мой портфель</code> - детали портфеля"
    )
    
    await message.reply(market_text, parse_mode="HTML")

# Покупка акций
@router.message(lambda message: message.text and message.text.lower().startswith('купить '))
async def buy_stocks(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply(
            "❌ Использование: <code>купить [АКЦИЯ] [КОЛИЧЕСТВО]</code>\n"
            "Пример: <code>купить AAPL 10</code>",
            parse_mode="HTML"
        )
        return
    
    stock_symbol = parts[1].upper()
    try:
        quantity = int(parts[2])
        if quantity <= 0:
            raise ValueError
    except:
        await message.reply("❌ Количество должно быть положительным числом!")
        return
    
    if stock_symbol not in REAL_STOCKS:
        await message.reply(
            f"❌ Акция <b>{stock_symbol}</b> не найдена!\n"
            f"📋 Доступные акции: {', '.join(REAL_STOCKS.keys())}",
            parse_mode="HTML"
        )
        return
    
    current_price = stock_prices.get(stock_symbol, REAL_STOCKS[stock_symbol]['base_price'])
    total_cost = current_price * quantity
    
    portfolio = user_stocks[user_id]
    
    if portfolio['balance'] < total_cost:
        await message.reply(
            f"❌ Недостаточно средств на биржевом балансе!\n"
            f"💰 Нужно: {format_amount(int(total_cost))} MORPH\n"
            f"💳 На балансе: {format_amount(portfolio['balance'])} MORPH\n\n"
            f"💡 Пополните баланс: <code>пополнить биржу {format_amount(int(total_cost))}</code>",
            parse_mode="HTML"
        )
        return
    
    # Совершаем покупку
    portfolio['balance'] -= total_cost
    portfolio['stocks'][stock_symbol] += quantity
    portfolio['total_invested'] += total_cost
    
    save_stocks()  # Сохраняем изменения
    
    stock_info = REAL_STOCKS[stock_symbol]
    
    await message.reply(
        f"✅ <b>ПОКУПКА УСПЕШНА!</b>\n\n"
        f"{stock_info['emoji']} <b>{stock_info['name']}</b> ({stock_symbol})\n"
        f"📦 Куплено: <b>{quantity} акций</b>\n"
        f"💰 Цена за акцию: <b>{current_price} MORPH</b>\n"
        f"💸 Общая стоимость: <b>{format_amount(int(total_cost))} MORPH</b>\n"
        f"💳 Остаток на бирже: <b>{format_amount(portfolio['balance'])} MORPH</b>",
        parse_mode="HTML"
    )

# Продажа акций
@router.message(lambda message: message.text and message.text.lower().startswith('продать '))
async def sell_stocks(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply(
            "❌ Использование: <code>продать [АКЦИЯ] [КОЛИЧЕСТВО]</code>\n"
            "Пример: <code>продать TSLA 5</code>",
            parse_mode="HTML"
        )
        return
    
    stock_symbol = parts[1].upper()
    try:
        quantity = int(parts[2])
        if quantity <= 0:
            raise ValueError
    except:
        await message.reply("❌ Количество должно быть положительным числом!")
        return
    
    if stock_symbol not in REAL_STOCKS:
        await message.reply(f"❌ Акция <b>{stock_symbol}</b> не найдена!", parse_mode="HTML")
        return
    
    portfolio = user_stocks[user_id]
    
    if portfolio['stocks'][stock_symbol] < quantity:
        await message.reply(
            f"❌ Недостаточно акций для продажи!\n"
            f"📦 У вас есть: <b>{portfolio['stocks'][stock_symbol]} акций</b>\n"
            f"🎯 Хотите продать: <b>{quantity} акций</b>",
            parse_mode="HTML"
        )
        return
    
    current_price = stock_prices.get(stock_symbol, REAL_STOCKS[stock_symbol]['base_price'])
    total_income = current_price * quantity
    
    # Совершаем продажу
    portfolio['balance'] += total_income
    portfolio['stocks'][stock_symbol] -= quantity
    
    # Рассчитываем прибыль
    avg_buy_price = REAL_STOCKS[stock_symbol]['base_price']
    profit = (current_price - avg_buy_price) * quantity
    portfolio['total_profit'] += profit
    
    save_stocks()  # Сохраняем изменения
    
    stock_info = REAL_STOCKS[stock_symbol]
    profit_emoji = "📈" if profit >= 0 else "📉"
    
    await message.reply(
        f"💰 <b>ПРОДАЖА УСПЕШНА!</b>\n\n"
        f"{stock_info['emoji']} <b>{stock_info['name']}</b> ({stock_symbol})\n"
        f"📦 Продано: <b>{quantity} акций</b>\n"
        f"💰 Цена за акцию: <b>{current_price} MORPH</b>\n"
        f"💸 Общий доход: <b>{format_amount(int(total_income))} MORPH</b>\n"
        f"{profit_emoji} Прибыль: <b>{format_amount(int(profit))} MORPH</b>\n"
        f"💳 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>",
        parse_mode="HTML"
    )

# Пополнение биржевого баланса
@router.message(lambda message: message.text and message.text.lower().startswith('пополнить биржу '))
async def deposit_stock_balance(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply("❌ Использование: <code>пополнить биржу [СУММА]</code>", parse_mode="HTML")
        return
    
    amount = parse_amount(parts[2])
    if amount is None or amount <= 0:
        await message.reply("❌ Сумма должна быть положительной!")
        return
    
    if users_data[user_id]['balance'] < amount:
        await message.reply(
            f"❌ Недостаточно MORPH на основном балансе!\n"
            f"💰 Нужно: {format_amount(amount)} MORPH\n"
            f"💳 Ваш баланс: {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode="HTML"
        )
        return
    
    # Переводим средства
    users_data[user_id]['balance'] -= amount
    user_stocks[user_id]['balance'] += amount
    
    save_users()
    save_stocks()  # Сохраняем изменения
    
    await message.reply(
        f"✅ <b>БАЛАНС БИРЖИ ПОПОЛНЕН!</b>\n\n"
        f"💰 Сумма: <b>{format_amount(amount)} MORPH</b>\n"
        f"💳 Баланс биржи: <b>{format_amount(user_stocks[user_id]['balance'])} MORPH</b>\n"
        f"💵 Основной баланс: <b>{format_amount(users_data[user_id]['balance'])} MORPH</b>",
        parse_mode="HTML"
    )

# Вывод с биржи
@router.message(lambda message: message.text and message.text.lower().startswith('вывести с биржи '))
async def withdraw_stock_balance(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    parts = message.text.split()
    if len(parts) != 4:
        await message.reply("❌ Использование: <code>вывести с биржи [СУММА]</code>", parse_mode="HTML")
        return
    
    amount = parse_amount(parts[3])
    if amount is None or amount <= 0:
        await message.reply("❌ Сумма должна быть положительной!")
        return
    
    portfolio = user_stocks[user_id]
    
    if portfolio['balance'] < amount:
        await message.reply(
            f"❌ Недостаточно MORPH на биржевом балансе!\n"
            f"💰 Хотите вывести: {format_amount(amount)} MORPH\n"
            f"💳 Баланс биржи: {format_amount(portfolio['balance'])} MORPH",
            parse_mode="HTML"
        )
        return
    
    # Выводим средства
    portfolio['balance'] -= amount
    users_data[user_id]['balance'] += amount
    
    save_users()
    save_stocks()  # Сохраняем изменения
    
    await message.reply(
        f"✅ <b>СРЕДСТВА ВЫВЕДЕНЫ С БИРЖИ!</b>\n\n"
        f"💰 Сумма: <b>{format_amount(amount)} MORPH</b>\n"
        f"💳 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>\n"
        f"💵 Основной баланс: <b>{format_amount(users_data[user_id]['balance'])} MORPH</b>",
        parse_mode="HTML"
    )

# Мой портфель
@router.message(lambda message: message.text and message.text.lower() in ["мой портфель", "портфель"])
async def show_portfolio(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    init_stock_portfolio(user_id)
    
    portfolio = user_stocks[user_id]
    portfolio_value = calculate_portfolio_value(user_id)
    
    portfolio_text = (
        f"💼 <b>ВАШ ИНВЕСТПОРТФЕЛЬ</b>\n\n"
        f"💰 Общая стоимость: <b>{format_amount(int(portfolio_value))} MORPH</b>\n"
        f"💵 Баланс биржи: <b>{format_amount(portfolio['balance'])} MORPH</b>\n"
        f"📈 Всего инвестировано: <b>{format_amount(portfolio['total_invested'])} MORPH</b>\n"
        f"🎯 Общая прибыль: <b>{format_amount(portfolio['total_profit'])} MORPH</b>\n\n"
    )
    
    # Детали по акциям
    has_stocks = False
    for stock, quantity in portfolio['stocks'].items():
        if quantity > 0:
            has_stocks = True
            current_price = stock_prices.get(stock, REAL_STOCKS[stock]['base_price'])
            value = current_price * quantity
            stock_info = REAL_STOCKS[stock]
            
            portfolio_text += (
                f"{stock_info['emoji']} <b>{stock_info['name']}</b>\n"
                f"📦 Количество: <b>{quantity} акций</b>\n"
                f"💰 Текущая стоимость: <b>{format_amount(int(value))} MORPH</b>\n\n"
            )
    
    if not has_stocks:
        portfolio_text += "📭 <b>У вас пока нет акций</b>\n\n"
    
    portfolio_text += (
        f"💡 <b>СОВЕТ:</b> Диверсифицируйте портфель!\n"
        f"🔄 Цены обновляются каждые 5 минут"
    )
    
    await message.reply(portfolio_text, parse_mode="HTML")

# Расчет стоимости портфеля
def calculate_portfolio_value(user_id):
    portfolio = user_stocks[user_id]
    total_value = portfolio['balance']
    
    for stock, quantity in portfolio['stocks'].items():
        if quantity > 0:
            current_price = stock_prices.get(stock, REAL_STOCKS[stock]['base_price'])
            total_value += current_price * quantity
    
    return total_value

# --- ИГРА БУНКЕР ---
@router.message(lambda message: message.text and message.text.lower().startswith('бункер'))
async def start_bunker_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    enforce_game_enabled("bunker")
    
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "🏗️ <b>МОРФ-БУНКЕР</b>\n\n"
                "🎯 Выбери бункер 1-5:\n"
                "• 1 бункер = ДЖЕКПОТ x2\n"  
                "• 1 бункер = ВЫИГРЫШ x1.5\n"
                "• 1 бункер = ПРОИГРЫШ x0.5\n"  # ИЗМЕНЕНО: был x1.5
                "• 1 бункер = ПРОИГРЫШ x0.8\n"  # ИЗМЕНЕНО: был x1
                "• 1 бункер = ПРОИГРЫШ x0\n\n"
                "💡 <b>Примеры:</b>\n"
                "<code>бункер 1000 3</code>\n"
                "<code>бункер всё 1</code>\n\n"
                "🎰 Минимальная ставка: 100 MORPH\n"
                "📊 Шансы: 20% джекпот, 20% выигрыш, 60% проигрыш",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        bunker_number = int(parts[2])
        
        # Проверки
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
            
        if bunker_number < 1 or bunker_number > 5:
            await message.reply("❌ Выбери бункер от 1 до 5!")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Создаем игру
        game_id = f"bunker_{user_id}_{int(time.time())}"
        
        # Генерируем содержимое бункеров (1 джекпот, 1 выигрыш, 3 проигрыша разных типов)
        bunkers = ["🎰", "💰", "💸", "😢", "💀"]
        random.shuffle(bunkers)
        
        active_bunker_games[game_id] = {
            'user_id': user_id,
            'bet': bet,
            'bunker_number': bunker_number,
            'bunkers': bunkers,
            'timestamp': time.time()
        }
        
        # Анимация открытия
        msg = await message.reply("🏗️ <b>Открываем бункер...</b>", parse_mode='HTML')
        await asyncio.sleep(2)
        
        # Определяем результат
        game = active_bunker_games[game_id]
        result = game['bunkers'][bunker_number - 1]
        
        if result == "🎰":
            # ДЖЕКПОТ x2
            win_amount = int(bet * 2)
            add_win_to_user(user_id, win_amount, bet)
            add_game_to_history(user_id, 'Бункер', bet, 'win', win_amount)
            result_text = f"🎰 <b>ДЖЕКПОТ! +{format_amount(win_amount)} MORPH (x2)</b>"
            
        elif result == "💰":
            # ВЫИГРЫШ x1.5
            win_amount = int(bet * 1.5)
            add_win_to_user(user_id, win_amount, bet)
            add_game_to_history(user_id, 'Бункер', bet, 'win', win_amount)
            result_text = f"💰 <b>ВЫИГРЫШ! +{format_amount(win_amount)} MORPH (x1.5)</b>"
            
        elif result == "💸":
            # ПРОИГРЫШ x0.5 (игрок теряет только половину ставки) - ИСПРАВЛЕНО
            loss_amount = int(bet * 0.5)
            # Возвращаем половину ставки
            users_data[user_id]['balance'] += int(bet * 0.5)
            users_data[user_id]['total_won'] -= loss_amount
            add_game_to_history(user_id, 'Бункер', bet, 'lose', 0)
            save_users()
            result_text = f"💸 <b>ПРОИГРЫШ! -{format_amount(loss_amount)} MORPH (x0.5)</b>"
            
        elif result == "😢":
            # ПРОИГРЫШ x0.8 (возврат 80% ставки) - ИСПРАВЛЕНО
            loss_amount = int(bet * 0.2)
            # Возвращаем 80% ставки
            users_data[user_id]['balance'] += int(bet * 0.8)
            users_data[user_id]['total_won'] -= loss_amount
            add_game_to_history(user_id, 'Бункер', bet, 'lose', 0)
            save_users()
            result_text = f"😢 <b>ПРОИГРЫШ! -{format_amount(loss_amount)} MORPH (x0.8)</b>"
            
        else:
            # ПРОИГРЫШ x0 (полная потеря ставки)
            add_game_to_history(user_id, 'Бункер', bet, 'lose', 0)
            result_text = f"💀 <b>ПОЛНЫЙ ПРОИГРЫШ! -{format_amount(bet)} MORPH (x0)</b>"
        
        users_data[user_id]['games_played'] += 1
        save_users()
        
        # Показываем все бункера
        bunkers_display = ""
        for i, bunker in enumerate(game['bunkers'], 1):
            if i == bunker_number:
                bunkers_display += f"[{bunker}] "
            else:
                bunkers_display += f"{bunker} "
        
        await msg.edit_text(
            f"🏗️ <b>МОРФ-БУНКЕР - РЕЗУЛЬТАТ</b>\n\n"
            f"🎯 Твой выбор: <b>Бункер {bunker_number}</b>\n"
            f"📦 Содержимое: {bunkers_display}\n\n"
            f"{result_text}\n"
            f"💰 Ставка: {format_amount(bet)} MORPH\n"
            f"💳 Баланс: {format_amount(users_data[user_id]['balance'])} MORPH",
            parse_mode='HTML'
        )
        
        # Удаляем игру
        del active_bunker_games[game_id]
        
    except Exception as e:
        await message.reply(f"❌ Ошибка: {str(e)}")

# Альтернативные команды
@router.message(lambda message: message.text and message.text.lower().startswith(('bunker')))
async def bunker_alias(message: types.Message):
    # Заменяем алиас на основную команду
    new_text = 'бункер' + message.text[6:]
    message.text = new_text
    await start_bunker_game(message)

# ИГРА "X50" - АВТОМАТИЧЕСКАЯ РУЛЕТКА
X50_CHAT_ID = -1002669310047  # ⚠️ ЗАМЕНИТЕ НА РЕАЛЬНЫЙ ID ЧАТА ⚠️

active_x50_round = {
    'bets': {'green': [], 'red': [], 'black': [], 'purple': []},
    'total_bets': 0,
    'round_number': 1,
    'is_spinning': False,
    'start_time': None,
    'timer_task': None
}

x50_history = []
x50_colors = {
    'green': {'emoji': '🟩', 'multiplier': 50, 'weight': 1, 'aliases': ['зеленый', 'зелёный', 'з', 'green', 'g']},
    'purple': {'emoji': '🟣', 'multiplier': 3, 'weight': 4, 'aliases': ['фиолетовый', 'ф', 'purple', 'p']},
    'red': {'emoji': '🔴', 'multiplier': 5, 'weight': 3, 'aliases': ['красный', 'к', 'red', 'r']},
    'black': {'emoji': '⚫', 'multiplier': 2, 'weight': 6, 'aliases': ['черный', 'чёрный', 'ч', 'black', 'b']}
}

@router.message(lambda message: message.text and message.text.lower().startswith(("х50", "x50")))
async def x50_place_bet(message: types.Message):
    # 🔒 ЗАЩИТА: проверяем, что команда отправлена в нужном чате
    if message.chat.id != X50_CHAT_ID:
        await message.reply("❌ Игра X50 доступна только в специальной группе!")
        return
        
    if is_banned(message.from_user.id):
        return
    
    try:
        # Убираем "х50" или "x50" из текста и разбиваем на части
        text = message.text.lower().replace('х50', '').replace('x50', '').strip()
        parts = text.split()
        
        if len(parts) < 2:
            await message.reply(
                "🎰 <b>АВТОМАТИЧЕСКАЯ РУЛЕТКА X50</b>\n\n"
                "❌ Использование: <b>х50 [ставка/ВСЁ] [цвет]</b>\n"
                "💡 Примеры:\n"
                "<code>х50 500к ч</code> - 500,000 на черный\n"
                "<code>х50 всё к</code> - всё на красный\n"
                "<code>х50 1000 ф</code> - 1,000 на фиолетовый\n"
                "<code>х50 500 з</code> - 500 на зеленый\n\n"
                "🎯 Минимальная ставка: 100 MORPH\n\n"
                "🎨 <b>Цвета и множители:</b>\n"
                "🟩 <b>Зелёный</b> (з) - x50 (очень редкий)\n"
                "🟣 <b>Фиолетовый</b> (ф) - x3 (редкий)\n"
                "🔴 <b>Красный</b> (к) - x5 (средний)\n"
                "⚫ <b>Чёрный</b> (ч) - x2 (частый)\n\n"
                "⏱ <b>Раунд длится 25-50 секунд</b>\n"
                "💰 <b>Все ставки автоматически участвуют в текущем раунде</b>",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        # Обрабатываем ставку (может быть с "к" например "500к")
        bet_str = parts[0].lower()
        bet = parse_amount(bet_str, user_balance)
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Обрабатываем цвет (берем все оставшиеся части как цвет)
        color_input = ' '.join(parts[1:]).lower()
        
        # Проверяем цвет по алиасам
        color_key = None
        for key, data in x50_colors.items():
            if (color_input in data['aliases'] or 
                any(alias in color_input for alias in data['aliases'])):
                color_key = key
                break
        
        if not color_key:
            await message.reply(
                "❌ Неверный цвет! Доступные цвета:\n"
                "🟩 <b>зеленый/з/green</b> - x50\n"
                "🟣 <b>фиолетовый/ф/purple</b> - x3\n"
                "🔴 <b>красный/к/red</b> - x5\n" 
                "⚫ <b>черный/ч/black</b> - x2\n\n"
                "💡 Пример: <code>х50 500к ф</code>",
                parse_mode="HTML"
            )
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Добавляем ставку в раунд
        bet_info = {
            'user_id': user_id,
            'username': message.from_user.first_name,
            'amount': bet,
            'color': color_key
        }
        
        active_x50_round['bets'][color_key].append(bet_info)
        active_x50_round['total_bets'] += bet
        
        # Отправляем подтверждение ставки
        color_data = x50_colors[color_key]
        await message.reply(
            f"✅ <b>СТАВКА ПРИНЯТА!</b>\n\n"
            f"👤 Игрок: <b>{message.from_user.first_name}</b>\n"
            f"💰 Ставка: <b>{format_amount(bet)} MORPH</b>\n"
            f"🎨 Цвет: {color_data['emoji']} <b>{color_key.upper()}</b>\n"
            f"📈 Множитель: <b>x{color_data['multiplier']}</b>\n"
            f"🎯 Потенциальный выигрыш: <b>{format_amount(bet * color_data['multiplier'])} MORPH</b>\n\n"
            f"⏳ Ожидайте завершения раунда...",
            parse_mode="HTML"
        )
        
        # Запускаем таймер раунда, если он еще не запущен
        if not active_x50_round['is_spinning'] and active_x50_round['total_bets'] > 0:
            await start_x50_round()
            
    except Exception as e:
        print(f"Ошибка в x50_place_bet: {e}")
        await message.reply("❌ Произошла ошибка при размещении ставки!")

async def start_x50_round():
    """Запуск нового раунда X50"""
    if active_x50_round['is_spinning']:
        return
    
    active_x50_round['is_spinning'] = True
    active_x50_round['start_time'] = time.time()
    
    # Случайное время раунда от 25 до 50 секунд
    round_duration = random.randint(25, 50)
    
    # Отправляем сообщение о начале раунда в чат
    bets_text = get_x50_bets_text()
    round_message = await bot.send_message(
        chat_id=X50_CHAT_ID,
        text=f"🎰 <b>РАУНД X50 #{active_x50_round['round_number']} НАЧАЛСЯ!</b>\n\n"
             f"💰 <b>Общая сумма ставок:</b> {format_amount(active_x50_round['total_bets'])} MORPH\n"
             f"⏰ <b>До завершения:</b> {round_duration} секунд\n\n"
             f"🎯 <b>Текущие ставки:</b>\n{bets_text}\n\n"
             f"⚡ <b>Ставки еще принимаются!</b>\n"
             f"💬 Используйте: <code>х50 [ставка] [цвет]</code>",
        parse_mode="HTML"
    )
    
    # Запускаем таймер завершения раунда
    active_x50_round['timer_task'] = asyncio.create_task(
        finish_x50_round(round_duration, round_message.message_id)
    )

def get_x50_bets_text():
    """Получить текст со списком ставок"""
    text = ""
    for color_key, color_data in x50_colors.items():
        bets = active_x50_round['bets'][color_key]
        if bets:
            total_color_bet = sum(bet['amount'] for bet in bets)
            text += f"{color_data['emoji']} {color_key.upper()}: {format_amount(total_color_bet)} MORPH ({len(bets)} ставок)\n"
    
    if not text:
        text = "Ставок пока нет...\n"
    
    return text

async def finish_x50_round(duration: int, message_id: int):
    """Завершение раунда и определение победителя"""
    try:
        # Ждем указанное время
        await asyncio.sleep(duration)
        
        if active_x50_round['total_bets'] == 0:
            # Если ставок не осталось, отменяем раунд
            active_x50_round['is_spinning'] = False
            return
        
        # Определяем выигрышный цвет на основе весов
        weights = [x50_colors[color]['weight'] for color in ['green', 'purple', 'red', 'black']]
        winning_color = random.choices(['green', 'purple', 'red', 'black'], weights=weights, k=1)[0]
        winning_data = x50_colors[winning_color]
        
        # Обновляем историю
        x50_history.append(winning_color)
        if len(x50_history) > 10:
            x50_history.pop(0)
        
        # Обрабатываем выигрыши
        winners = []
        total_payout = 0
        
        # Сначала собираем всех игроков, которые проиграли (для отчета)
        losers = []
        for color in ['green', 'purple', 'red', 'black']:
            if color != winning_color:
                for bet in active_x50_round['bets'][color]:
                    losers.append({
                        'username': bet['username'],
                        'bet': bet['amount'],
                        'color': color
                    })
        
        # Затем обрабатываем победителей
        for bet in active_x50_round['bets'][winning_color]:
            payout = bet['amount'] * winning_data['multiplier']
            users_data[bet['user_id']]['balance'] += payout
            users_data[bet['user_id']]['total_won'] += payout - bet['amount']
            users_data[bet['user_id']]['games_played'] += 1
            
            winners.append({
                'username': bet['username'],
                'bet': bet['amount'],
                'payout': payout,
                'profit': payout - bet['amount']
            })
            total_payout += payout
        
        save_users()
        
        # Формируем детальный отчет о результатах
        winners_text = ""
        if winners:
            for i, winner in enumerate(winners[:15]):  # Показываем первых 15 победителей
                winners_text += f"🏆 {winner['username']}: +{format_amount(winner['profit'])} MORPH\n"
            if len(winners) > 15:
                winners_text += f"📊 ... и еще {len(winners) - 15} игроков\n"
        else:
            winners_text = "😢 Победителей нет\n"
        
        # Добавляем информацию о проигравших
        losers_text = ""
        total_lost = sum(loser['bet'] for loser in losers)
        if losers:
            losers_text = f"💸 Проиграно: {format_amount(total_lost)} MORPH ({len(losers)} игроков)\n"
        
        # Отправляем результат раунда в чат
        result_message = (
            f"🎰 <b>РАУНД X50 #{active_x50_round['round_number']} ЗАВЕРШЕН!</b>\n\n"
            f"🎯 <b>Выпал цвет:</b> {winning_data['emoji']} <b>{winning_color.upper()}</b>\n"
            f"📈 <b>Множитель:</b> x{winning_data['multiplier']}\n\n"
            f"💰 <b>Общая сумма ставок:</b> {format_amount(active_x50_round['total_bets'])} MORPH\n"
            f"🏆 <b>Общий выигрыш:</b> {format_amount(total_payout)} MORPH\n\n"
            f"🎉 <b>ПОБЕДИТЕЛИ:</b>\n{winners_text}\n"
            f"{losers_text}\n"
            f"⚡ <b>Следующий раунд через 10 секунд...</b>"
        )
        
        await bot.edit_message_text(
            chat_id=X50_CHAT_ID,
            message_id=message_id,
            text=result_message,
            parse_mode="HTML"
        )
        
        # Сбрасываем раунд и запускаем следующий через 10 секунд
        await reset_x50_round()
        
        # Запускаем следующий раунд через 10 секунд
        await asyncio.sleep(10)
        if active_x50_round['total_bets'] > 0:
            await start_x50_round()
            
    except Exception as e:
        print(f"Ошибка в finish_x50_round: {e}")
        await reset_x50_round()

async def reset_x50_round():
    """Сброс данных раунда"""
    active_x50_round['bets'] = {'green': [], 'purple': [], 'red': [], 'black': []}
    active_x50_round['total_bets'] = 0
    active_x50_round['is_spinning'] = False
    active_x50_round['round_number'] += 1
    active_x50_round['timer_task'] = None

@router.message(lambda message: message.text and message.text.lower() in ["дроп", "drop", "история"])
async def x50_drop_history(message: types.Message):
    """Показать историю последних выпадений"""
    # 🔒 ЗАЩИТА: проверяем, что команда отправлена в нужном чате
    if message.chat.id != X50_CHAT_ID:
        return
        
    if not x50_history:
        await message.reply(
            "📊 <b>ИСТОРИЯ X50</b>\n\n"
            "История выпадений пока пуста...\n"
            "Сделайте первую ставку командой: <code>х50 100 к</code>",
            parse_mode="HTML"
        )
        return
    
    # Создаем визуальную историю
    history_text = ""
    for color in x50_history:
        emoji = x50_colors[color]['emoji']
        history_text += emoji
    
    # Статистика по цветам
    stats = {
        'green': x50_history.count('green'),
        'purple': x50_history.count('purple'),
        'red': x50_history.count('red'), 
        'black': x50_history.count('black')
    }
    
    stats_text = (
        f"🟩 Зеленый: {stats['green']} раз\n"
        f"🟣 Фиолетовый: {stats['purple']} раз\n"
        f"🔴 Красный: {stats['red']} раз\n"
        f"⚫ Черный: {stats['black']} раз\n"
    )
    
    # Анализ серий
    analysis = ""
    if len(x50_history) >= 2:
        last_color = x50_history[-1]
        streak = 1
        for i in range(len(x50_history)-2, -1, -1):
            if x50_history[i] == last_color:
                streak += 1
            else:
                break
        
        if streak > 1:
            analysis = f"📈 Текущая серия: {x50_colors[last_color]['emoji']} {streak} раз подряд\n"
    
    await message.reply(
        f"📊 <b>ИСТОРИЯ X50</b>\n\n"
        f"🎯 Последние {len(x50_history)} результатов:\n"
        f"{history_text}\n\n"
        f"📈 <b>Статистика:</b>\n{stats_text}\n"
        f"{analysis}\n"
        f"💡 <b>Используйте историю для анализа!</b>",
        parse_mode="HTML"
    )

@router.message(lambda message: message.text and message.text.lower() in ["x50стат", "x50статистика"])
async def x50_stats(message: types.Message):
    """Показать статистику текущего раунда"""
    # 🔒 ЗАЩИТА: проверяем, что команда отправлена в нужном чате
    if message.chat.id != X50_CHAT_ID:
        return
        
    if active_x50_round['total_bets'] == 0:
        await message.reply(
            "📊 <b>СТАТИСТИКА X50</b>\n\n"
            "В текущем раунде ставок нет.\n"
            "Станьте первым! 🎰",
            parse_mode="HTML"
        )
        return
    
    bets_text = get_x50_bets_text()
    time_left = "не активен"
    
    if active_x50_round['is_spinning'] and active_x50_round['start_time']:
        elapsed = time.time() - active_x50_round['start_time']
        time_left = f"{int(30 - elapsed)} сек" if elapsed < 30 else "скоро..."
    
    await message.reply(
        f"📊 <b>СТАТИСТИКА X50</b>\n\n"
        f"🎯 Раунд: #{active_x50_round['round_number']}\n"
        f"💰 Общая сумма: {format_amount(active_x50_round['total_bets'])} MORPH\n"
        f"⏰ До завершения: {time_left}\n\n"
        f"🎨 <b>Распределение ставок:</b>\n{bets_text}\n"
        f"⚡ <b>Ставки еще принимаются!</b>",
        parse_mode="HTML"
    )

# Запускаем автоматическую очистку зависших раундов
async def x50_cleanup_scheduler():
    """Очистка зависших раундов X50"""
    while True:
        await asyncio.sleep(60)  # Проверка каждую минуту
        
        if (active_x50_round['is_spinning'] and 
            active_x50_round['start_time'] and 
            time.time() - active_x50_round['start_time'] > 120):  # 2 минуты - слишком долго
            
            print("Очистка зависшего раунда X50")
            await reset_x50_round()

#НОВЫЕ ИГРЫ
# --- ИГРА НВУТИ (М/Р/Б) ---
@router.message(lambda message: message.text and message.text.lower().startswith('нвути'))
async def start_nvuti_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    enforce_game_enabled("nvuti")
    
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "🎲 <b>ИГРА НВУТИ (М/Р/Б)</b>\n\n"
                "❌ Использование: <b>нвути [ставка] [М/Р/Б]</b>\n"
                "💡 Пример: <b>нвути 1500 М</b>\n"
                "🎯 Минимальная ставка: 100 MORPH\n\n"
                "🏆 <b>Правила:</b>\n"
                "• М\n"
                "• Р\n"
                "• Б\n"
                "• Коэффициент везде: 2x",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        choice = parts[2].upper()
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Проверяем выбор
        valid_choices = ["М", "Р", "Б"]
        if choice not in valid_choices:
            await message.reply("❌ Неверный выбор! Используйте: М, Р или Б")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Генерируем результат с разными шансами
        chances = {
            "М": 45,  # 45%
            "Р": 10,  # 10%
            "Б": 45   # 45%
        }
        
        # Создаем список результатов согласно шансам
        results_pool = []
        for result, chance in chances.items():
            results_pool.extend([result] * chance)
        
        # Выбираем случайный результат
        result = random.choice(results_pool)
        
        # Определяем выигрыш
        multiplier = 2.0
        if choice == result:
            won_amount = int(bet * multiplier)
            add_win_to_user(user_id, won_amount, bet)
            add_game_to_history(user_id, 'НВУТИ', bet, 'win', won_amount)
            win_text = f"🎉 ПОБЕДА! +{format_amount(won_amount)} MORPH"
        else:
            won_amount = 0
            add_game_to_history(user_id, 'НВУТИ', bet, 'lose', 0)
            win_text = f"❌ ПРОИГРЫШ! -{format_amount(bet)} MORPH"
        
        users_data[user_id]['games_played'] += 1
        save_users()
        
        # Эмодзи для результатов
        emoji_map = {
            "М": "",
            "Р": "", 
            "Б": ""
        }
        
        await message.reply(
            f"{win_text}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.reply(f"❌ Ошибка: {str(e)}")

# --- Альтернативные команды для игры ---
@router.message(lambda message: message.text and message.text.lower().startswith(('nwty', 'nuti', 'нвути')))
async def nvuti_aliases(message: types.Message):
    # Заменяем алиасы на основную команду
    if message.text.lower().startswith('nwty'):
        new_text = 'нвути' + message.text[4:]
    elif message.text.lower().startswith('nuti'):
        new_text = 'нвути' + message.text[4:]
    else:
        new_text = message.text
    
    message.text = new_text
    await start_nvuti_game(message)

# --- ИГРА ВИЛИН (Всё или ничего) ---
active_vilin_games = {}
vilin_cooldowns = {}

@router.message(lambda message: message.text and message.text.lower().startswith('вилин'))
async def start_vilin_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    enforce_game_enabled("vilin")
    
    user_id = message.from_user.id
    
    # Проверяем кулдаун
    current_time = time.time()
    if user_id in vilin_cooldowns:
        time_left = vilin_cooldowns[user_id] - current_time
        if time_left > 0:
            await message.reply(f"⏳ Следующая игра через {int(time_left)} секунд")
            return
    
    init_user(user_id, message.from_user.username)
    
    # Получаем баланс на руках (не в банке)
    balance_on_hand = users_data[user_id]['balance']
    
    if balance_on_hand < 100:
        await message.reply("❌ Минимальная сумма для игры: 100 MORPH на руках!")
        return
    
    # Создаем клавиатуру с кнопками
    builder = InlineKeyboardBuilder()
    builder.button(text="🎮 Играть", callback_data=f"vilin_play_{user_id}")
    builder.button(text="❌ Отменить", callback_data=f"vilin_cancel_{user_id}")
    builder.adjust(2)
    
    # Сохраняем игру с защитой
    active_vilin_games[user_id] = {
        'message_id': None,
        'bet_amount': balance_on_hand,
        'played': False,
        'game_id': f"vilin_{user_id}_{int(time.time())}"
    }
    
    msg = await message.reply(
        f"🎲 <b>ВИЛИН - ВСЁ ИЛИ НИЧЕГО</b>\n\n"
        f"💰 На руках: {format_amount(balance_on_hand)} MORPH\n"
        f"🎯 Шанс выигрыша: 50%\n"
        f"📊 Коэффициент: 2x\n\n"
        f"<b>Правила:</b>\n"
        f"• Выигрыш: удваиваете ставку\n"
        f"• Проигрыш: теряете всю ставку\n"
        f"• Ставка: ВСЕ средства на руках\n\n"
        f"⚡ <b>Готовы рискнуть?</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    
    # Сохраняем ID сообщения
    active_vilin_games[user_id]['message_id'] = msg.message_id

# Обработка кнопки "Играть"
@router.callback_query(lambda c: c.data.startswith('vilin_play_'))
async def vilin_play_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[2])
    
    # Проверяем владельца игры
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    
    if user_id not in active_vilin_games:
        await callback.answer("❌ Игра не найдена!", show_alert=True)
        return
    
    game = active_vilin_games[user_id]
    
    # Проверяем, не играл ли уже
    if game['played']:
        await callback.answer("❌ Вы уже играли в этой сессии!", show_alert=True)
        return
    
    init_user(user_id, callback.from_user.username)
    balance_on_hand = users_data[user_id]['balance']
    
    # Проверяем, не изменился ли баланс
    if balance_on_hand != game['bet_amount']:
        await callback.answer("❌ Баланс изменился! Начните заново.", show_alert=True)
        del active_vilin_games[user_id]
        return
    
    if balance_on_hand < 100:
        await callback.answer("❌ Недостаточно средств!", show_alert=True)
        del active_vilin_games[user_id]
        return
    
    # Отмечаем что игра начата
    game['played'] = True
    
    # Списываем ВСЕ средства
    users_data[user_id]['balance'] = 0
    
    # 50% шанс на выигрыш
    if random.random() < 0.5:
        # Выигрыш - x2 от ставки
        win_amount = balance_on_hand * 2
        # Баланс уже 0, просто устанавливаем выигрыш (не вызываем add_win_to_user, так как она добавит еще раз)
        users_data[user_id]['balance'] = win_amount
        # Обновляем статистику вручную
        users_data[user_id]['total_won'] += win_amount - balance_on_hand
        # Обновляем лидерборд (только чистый выигрыш)
        net_win = win_amount - balance_on_hand
        if net_win > 0:
            update_leaderboard(user_id, net_win)
        add_game_to_history(user_id, 'Вилин', balance_on_hand, 'win', win_amount)
        users_data[user_id]['games_played'] += 1
        save_users()
        result_text = f"🎉 ВЫИГРЫШ! +{format_amount(win_amount)} MORPH"
        result_emoji = "💰"
    else:
        # Проигрыш
        add_game_to_history(user_id, 'Вилин', balance_on_hand, 'lose', 0)
        users_data[user_id]['games_played'] += 1
        save_users()
        result_text = f"💀 ПРОИГРЫШ! -{format_amount(balance_on_hand)} MORPH"
        result_emoji = "💀"
    
    # Устанавливаем кулдаун 30 секунд
    vilin_cooldowns[user_id] = time.time() + 30
    
    # Обновляем сообщение
    await callback.message.edit_text(
        f"🎲 <b>ВИЛИН - РЕЗУЛЬТАТ</b>\n\n"
        f"{result_emoji} <b>{result_text}</b>\n\n"
        f"💰 Ставка: {format_amount(balance_on_hand)} MORPH\n"
        f"💳 Новый баланс: {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"⏳ Следующая игра через 30 секунд",
        parse_mode="HTML"
    )
    
    # Удаляем игру из активных
    del active_vilin_games[user_id]
    await callback.answer()

# Обработка кнопки "Отменить"
@router.callback_query(lambda c: c.data.startswith('vilin_cancel_'))
async def vilin_cancel_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[2])
    
    # Проверяем владельца игры
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    
    if user_id in active_vilin_games:
        del active_vilin_games[user_id]
    
    await callback.message.edit_text(
        "❌ <b>Игра отменена</b>\n\n"
        "💫 Чтобы начать заново, напишите: <code>вилин</code>",
        parse_mode="HTML"
    )
    await callback.answer()

# Очистка зависших игр
async def cleanup_vilin_games():
    """Очистка зависших игр Вилин"""
    current_time = time.time()
    expired_games = []
    
    for user_id, game in active_vilin_games.items():
        game_timestamp = int(game['game_id'].split('_')[-1])
        if current_time - game_timestamp > 300:  # 5 минут
            expired_games.append(user_id)
    
    for user_id in expired_games:
        del active_vilin_games[user_id]
    
    # Очистка старых кулдаунов
    expired_cooldowns = []
    for user_id, cooldown_time in vilin_cooldowns.items():
        if current_time > cooldown_time:
            expired_cooldowns.append(user_id)
    
    for user_id in expired_cooldowns:
        del vilin_cooldowns[user_id]

# Запускаем очистку каждую минуту
async def vilin_cleanup_scheduler():
    while True:
        await asyncio.sleep(60)
        await cleanup_vilin_games()

# Добавляем в главную функцию
async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем очистку в фоне
    asyncio.create_task(vilin_cleanup_scheduler())
    
    await dp.start_polling(bot)

# Добавь в глобальные переменные
FAST_PROMO_CONFIG = {
    'bot_channel_id': None,  # Будет устанавливаться через команду
    'min_amount': 1000,
    'max_amount': 5000000,
    'min_activations': 1,
    'max_activations': 999,
    'default_duration_hours': 24
}

# Команда для установки канала
@router.message(lambda message: message.text and message.text.lower().startswith('+фастканал'))
async def set_fast_channel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав! Только администраторы могут настраивать канал.')
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "❌ <b>Использование:</b> +фастканал [ссылка_на_канал]\n\n"
                "💡 <b>Примеры:</b>\n"
                "<code>+фастканал https://t.me/morph_casino</code>\n"
                "<code>+фастканал @morph_casino</code>\n"
                "<code>+фастканал morph_casino</code>\n\n"
                "⚡ <b>Канал должен быть публичным или бот должен быть администратором</b>",
                parse_mode='HTML'
            )
            return
        
        channel_link = parts[1].strip()
        
        # Очищаем ссылку от https://t.me/
        if channel_link.startswith('https://t.me/'):
            channel_link = channel_link.replace('https://t.me/', '')
        elif channel_link.startswith('t.me/'):
            channel_link = channel_link.replace('t.me/', '')
        
        # Убираем @ если есть
        if channel_link.startswith('@'):
            channel_link = channel_link[1:]
        
        # Проверяем валидность username
        if not re.match(r'^[a-zA-Z0-9_]{5,32}$', channel_link):
            await message.reply('❌ Неверный формат ссылки на канал!')
            return
        
        # Пробуем получить информацию о канале
        try:
            chat = await message.bot.get_chat(f"@{channel_link}")
            
            if chat.type != 'channel':
                await message.reply('❌ Это не канал! Укажите ссылку на Telegram канал.')
                return
            
            # Проверяем, что бот является администратором канала
            bot_member = await message.bot.get_chat_member(chat.id, (await message.bot.me()).id)
            if bot_member.status not in ['administrator', 'creator']:
                await message.reply(
                    '❌ Бот не является администратором этого канала!\n\n'
                    '💡 <b>Как исправить:</b>\n'
                    '1. Добавьте бота в канал\n'
                    '2. Дайте права администратора\n'
                    '3. Разрешите публиковать сообщения',
                    parse_mode='HTML'
                )
                return
            
            # Сохраняем ID канала
            FAST_PROMO_CONFIG['bot_channel_id'] = chat.id
            
            await message.reply(
                f"✅ <b>КАНАЛ ДЛЯ ФАСТ-ПРОМОКОДОВ НАСТРОЕН!</b>\n\n"
                f"📢 <b>Канал:</b> {chat.title}\n"
                f"🔗 <b>Ссылка:</b> @{channel_link}\n"
                f"🆔 <b>ID:</b> <code>{chat.id}</code>\n\n"
                f"⚡ <b>Теперь можно создавать фаст-промокоды командой:</b>\n"
                f"<code>+фаст 10000 10</code>",
                parse_mode='HTML'
            )
            
        except Exception as e:
            await message.reply(
                f"❌ Не удалось найти или получить доступ к каналу!\n\n"
                f"💡 <b>Проверьте:</b>\n"
                f"• Канал существует\n"
                f"• Ссылка правильная\n"
                f"• Бот добавлен как администратор\n\n"
                f"🔍 <b>Ошибка:</b> {str(e)}",
                parse_mode='HTML'
            )
            
    except Exception as e:
        await message.reply(f'❌ Ошибка настройки канала: {str(e)}')

# Команда для проверки текущего канала
@router.message(lambda message: message.text and message.text.lower() in ["фастканал", "канал фаст"])
async def show_fast_channel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    if not FAST_PROMO_CONFIG['bot_channel_id']:
        await message.reply(
            "❌ <b>Канал для фаст-промокодов не настроен!</b>\n\n"
            "💡 <b>Чтобы настроить:</b>\n"
            "<code>+фастканал https://t.me/username</code>\n\n"
            "⚡ <b>Канал должен быть публичным или бот должен быть администратором</b>",
            parse_mode='HTML'
        )
        return
    
    try:
        chat = await message.bot.get_chat(FAST_PROMO_CONFIG['bot_channel_id'])
        
        await message.reply(
            f"📢 <b>ТЕКУЩИЙ КАНАЛ ДЛЯ ФАСТ-ПРОМОКОДОВ</b>\n\n"
            f"🏷️ <b>Название:</b> {chat.title}\n"
            f"🔗 <b>Ссылка:</b> @{chat.username if chat.username else 'Приватный'}\n"
            f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
            f"👥 <b>Тип:</b> {chat.type}\n\n"
            f"⚡ <b>Создать промокод:</b> <code>+фаст 10000 10</code>",
            parse_mode='HTML'
        )
        
    except Exception as e:
        await message.reply(
            f"❌ <b>Ошибка доступа к каналу!</b>\n\n"
            f"💡 <b>Возможно:</b>\n"
            f"• Бот удален из канала\n"
            f"• Изменились права доступа\n"
            f"• Канал удален\n\n"
            f"🔧 <b>Исправьте:</b>\n"
            f"<code>+фастканал новая_ссылка</code>\n\n"
            f"🔍 <b>Ошибка:</b> {str(e)}",
            parse_mode='HTML'
        )

# Модифицируем функцию отправки в канал с проверкой
async def send_fast_promo_to_channel(bot: Bot, promo: Dict) -> types.Message:
    """Отправка фаст-промокода в канал"""
    if not FAST_PROMO_CONFIG['bot_channel_id']:
        raise Exception("Канал для фаст-промокодов не настроен! Используйте команду +фастканал")
    
    try:
        # Проверяем доступ к каналу
        chat = await bot.get_chat(FAST_PROMO_CONFIG['bot_channel_id'])
        bot_member = await bot.get_chat_member(chat.id, (await bot.me()).id)
        
        if bot_member.status not in ['administrator', 'creator']:
            raise Exception("Бот не является администратором канала!")
        
        # Создаем клавиатуру с кнопкой активации
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f'🎯 Получить {format_amount(promo["amount"])} MORPH!', 
            callback_data=f'fast_activate_{promo["id"]}'
        )
        
        message_text = (
            f"⚡ <b>ФАСТ-ПРОМОКОД!</b> ⚡\n\n"
            f"💰 <b>Сумма:</b> {format_amount(promo['amount'])} MORPH\n"
            f"👥 <b>Доступно:</b> {promo['max_activations']} активаций\n"
            f"🎁 <b>От:</b> {promo['created_by_name']}\n"
            f"⏰ <b>Действует:</b> 24 часа\n\n"
            f"💡 <b>Нажми кнопку ниже чтобы получить {format_amount(promo['amount'])} MORPH!</b>\n"
            f"🔥 <b>Успей пока не закончился!</b>"
        )
        
        return await bot.send_message(
            chat_id=FAST_PROMO_CONFIG['bot_channel_id'],
            text=message_text,
            reply_markup=builder.as_markup(),
            parse_mode='HTML'
        )
        
    except Exception as e:
        raise Exception(f"Ошибка отправки в канал: {str(e)}")

# Модифицируем команду создания фаст-промокода с проверкой канала
@router.message(lambda message: message.text and message.text.lower().startswith('+фаст'))
async def create_fast_promo(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    # Проверяем права администратора
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав! Только администраторы могут создавать фаст-промокоды.')
        return
    
    # Проверяем настроен ли канал
    if not FAST_PROMO_CONFIG['bot_channel_id']:
        await message.reply(
            "❌ <b>Сначала настройте канал для фаст-промокодов!</b>\n\n"
            "💡 <b>Используйте команду:</b>\n"
            "<code>+фастканал https://t.me/username</code>\n\n"
            "⚡ <b>Канал должен быть публичным или бот должен быть администратором</b>",
            parse_mode='HTML'
        )
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "❌ <b>Использование:</b> +фаст [сумма] [активации]\n\n"
                "💡 <b>Пример:</b> <code>+фаст 10000 10</code>\n"
                "💰 <b>Минимум:</b> 1,000 MORPH\n"
                "👥 <b>Активации:</b> 5-20 игроков\n\n"
                f"📢 <b>Будет отправлено в канал</b>",
                parse_mode='HTML'
            )
            return
        
        amount = parse_amount(parts[1])
        activations = int(parts[2])
        
        # Проверяем валидность параметров
        if amount is None or amount < FAST_PROMO_CONFIG['min_amount']:
            await message.reply(f'❌ Сумма должна быть не менее {format_amount(FAST_PROMO_CONFIG["min_amount"])} MORPH!')
            return
        
        if amount > FAST_PROMO_CONFIG['max_amount']:
            await message.reply(f'❌ Сумма не может превышать {format_amount(FAST_PROMO_CONFIG["max_amount"])} MORPH!')
            return
        
        if activations < FAST_PROMO_CONFIG['min_activations'] or activations > FAST_PROMO_CONFIG['max_activations']:
            await message.reply(f'❌ Количество активаций должно быть от {FAST_PROMO_CONFIG["min_activations"]} до {FAST_PROMO_CONFIG["max_activations"]}!')
            return
        
        # Проверяем баланс администратора
        admin_id = message.from_user.id
        init_user(admin_id, message.from_user.username)
        
        total_cost = amount * activations
        if users_data[admin_id]['balance'] < total_cost:
            await message.reply(
                f'❌ Недостаточно MORPH!\n'
                f'💰 Нужно: {format_amount(total_cost)} MORPH\n'
                f'💳 Ваш баланс: {format_amount(users_data[admin_id]["balance"])} MORPH',
                parse_mode='HTML'
            )
            return
        
        # Списываем средства
        users_data[admin_id]['balance'] -= total_cost
        save_users()
        
        # Создаем фаст-промокод
        promo_id = str(int(time.time()))
        fast_promo = {
            'id': promo_id,
            'amount': amount,
            'max_activations': activations,
            'used_count': 0,
            'used_by': [],
            'created_by': admin_id,
            'created_by_name': message.from_user.first_name,
            'total_cost': total_cost,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'expires_at': (datetime.now() + timedelta(hours=FAST_PROMO_CONFIG['default_duration_hours'])).strftime('%Y-%m-%d %H:%M:%S'),
            'message_id': None
        }
        
        # Сохраняем промокод
        active_fast_promos[promo_id] = fast_promo
        save_fast_promos()
        
        # Отправляем в канал бота
        channel_message = await send_fast_promo_to_channel(message.bot, fast_promo)
        fast_promo['message_id'] = channel_message.message_id
        
        # Обновляем в базе
        active_fast_promos[promo_id] = fast_promo
        save_fast_promos()
        
        # Получаем информацию о канале для красивого сообщения
        try:
            chat = await message.bot.get_chat(FAST_PROMO_CONFIG['bot_channel_id'])
            channel_info = f"📢 {chat.title} (@{chat.username})" if chat.username else f"📢 {chat.title}"
        except:
            channel_info = "📢 Настроенный канал"
        
        # Подтверждение администратору
        await message.reply(
            f"✅ <b>ФАСТ-ПРОМОКОД СОЗДАН!</b>\n\n"
            f"💰 <b>Сумма:</b> {format_amount(amount)} MORPH\n"
            f"👥 <b>Активаций:</b> {activations} игроков\n"
            f"💸 <b>Общая стоимость:</b> {format_amount(total_cost)} MORPH\n"
            f"⏰ <b>Действует:</b> 24 часа\n"
            f"{channel_info}\n\n"
            f"⚡ <b>Промокод опубликован в канале!</b>",
            parse_mode='HTML'
        )
        
    except Exception as e:
        await message.reply(f'❌ Ошибка создания фаст-промокода: {str(e)}')

# Обработчик для кнопки фаст-промокода
@router.callback_query(lambda c: c.data.startswith('fast_activate_'))
async def activate_fast_promo(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены и не можете активировать промокоды!", show_alert=True)
        return
    
    promo_id = callback.data.split('_')[2]
    user_id = callback.from_user.id
    
    if promo_id not in active_fast_promos:
        await callback.answer("❌ Промокод не найден или уже закончился!", show_alert=True)
        return
    
    promo = active_fast_promos[promo_id]
    
    # 🔒 Проверяем не активировал ли уже пользователь
    if user_id in promo['used_by']:
        await callback.answer("❌ Вы уже активировали этот промокод!", show_alert=True)
        return
    
    # Проверяем не закончились ли активации
    if promo['used_count'] >= promo['max_activations']:
        await callback.answer("❌ Промокод уже полностью использован!", show_alert=True)
        # Удаляем промокод
        await remove_expired_fast_promo(promo_id)
        return
    
    # Проверяем не истекло ли время
    expires_at = datetime.strptime(promo['expires_at'], '%Y-%m-%d %H:%M:%S')
    if datetime.now() > expires_at:
        await callback.answer("❌ Время действия промокода истекло!", show_alert=True)
        await remove_expired_fast_promo(promo_id)
        return
    
    # Активируем промокод
    init_user(user_id, callback.from_user.username)
    users_data[user_id]['balance'] += promo['amount']
    
    # Обновляем статистику промокода
    promo['used_count'] += 1
    promo['used_by'].append(user_id)
    
    save_users()
    active_fast_promos[promo_id] = promo
    save_fast_promos()
    
    # Сообщение об успехе
    remaining = promo['max_activations'] - promo['used_count']
    
    success_text = (
        f"🎉 <b>ФАСТ-ПРОМОКОД АКТИВИРОВАН!</b>\n\n"
        f"💰 <b>+{format_amount(promo['amount'])} MORPH</b>\n"
        f"👤 <b>Активаций осталось:</b> {remaining}/{promo['max_activations']}\n"
        f"🎁 <b>От:</b> {promo['created_by_name']}\n\n"
        f"✅ <b>Средства уже на вашем балансе!</b>"
    )
    
    if remaining == 0:
        success_text += "\n\n💥 <b>Промокод полностью использован!</b>"
        await remove_expired_fast_promo(promo_id)
        # Обновляем сообщение в канале
        await update_channel_message(callback.bot, promo)
    
    await callback.answer(f"✅ Получено {format_amount(promo['amount'])} MORPH!", show_alert=True)
    
    # Отправляем сообщение в ЛС
    try:
        await callback.message.bot.send_message(
            chat_id=user_id,
            text=success_text,
            parse_mode='HTML'
        )
    except Exception:
        # Если не удалось отправить в ЛС, показываем в alert
        pass

async def update_channel_message(bot: Bot, promo: Dict):
    """Обновление сообщения в канале когда промокод закончился"""
    if not promo.get('message_id'):
        return
    
    try:
        new_text = (
            f"💤 <b>ФАСТ-ПРОМОКОД ЗАВЕРШЕН</b> 💤\n\n"
            f"💰 <b>Сумма:</b> {format_amount(promo['amount'])} MORPH\n"
            f"👥 <b>Активировано:</b> {promo['used_count']}/{promo['max_activations']}\n"
            f"🎁 <b>От:</b> {promo['created_by_name']}\n\n"
            f"✅ <b>Промокод полностью использован!</b>"
        )
        
        await bot.edit_message_text(
            chat_id=FAST_PROMO_CONFIG['bot_channel_id'],
            message_id=promo['message_id'],
            text=new_text,
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"Ошибка обновления сообщения в канале: {e}")

async def remove_expired_fast_promo(promo_id: str):
    """Удаление просроченного фаст-промокода"""
    if promo_id in active_fast_promos:
        # Удаляем из локальной базы
        del active_fast_promos[promo_id]
        save_fast_promos()

#Ежечасный промокод
async def hourly_promo_scheduler(bot):
    """Планировщик ежечасных промокодов"""
    print("🕒 Планировщик часовых промокодов запущен...")
    
    while True:
        try:
            now = datetime.now()
            current_minute = now.minute
            current_hour_tag = now.strftime("%Y-%m-%d-%H")
            
            # Проверяем начало часа (первые 10 секунд каждой минуты 0)
            if current_minute == 0:
                # Проверяем, не отправляли ли уже промокод в этот час
                last_sent_tag = getattr(hourly_promo_scheduler, 'last_sent_tag', None)

                if last_sent_tag != current_hour_tag:
                    print(f"🎁 Отправка часового промокода для часа {now.hour}:00")
                    await send_hourly_promo(bot)
                    hourly_promo_scheduler.last_sent_tag = current_hour_tag
            
            # Ждем 30 секунд перед следующей проверкой
            await asyncio.sleep(30)
            
        except Exception as e:
            print(f"❌ Ошибка в планировщике промокодов: {e}")
            await asyncio.sleep(60)

async def send_hourly_promo(bot):
    """Отправка ежечасного промокода в чат"""
    try:
        # Генерируем случайные параметры промокода
        amount = random.randint(2700, 8900)  # Сумма от 2700 до 8900 MORPH
        activations = random.randint(5, 15)  # Активации от 5 до 15
        
        current_time = datetime.now()
        promo_code = generate_random_promocode(prefix="FROST", length=6)
        
        # Создаем промокод в системе
        promo_id = str(int(time.time()))
        promo = {
            'id': promo_id,
            'code': promo_code,
            'amount': amount,
            'max_activations': activations,
            'used_count': 0,
            'used_by': [],
            'created_by': 0,  # Система
            'created_by_name': 'Система',
            'created_at': current_time.strftime('%Y-%m-%d %H:%M:%S'),
            'expires_at': (current_time + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Сохраняем промокод
        promocodes[promo_code] = promo
        save_promocodes()
        
        # Форматируем время действия
        expires_time = (current_time + timedelta(hours=1)).strftime('%H:%M')
        
        # Определяем целевой чат: сначала конфиг, потом переменная окружения
        target_chat_id = HOURLY_PROMO_CHAT_ID
        if target_chat_id is None:
            logging.warning("Часовой промокод не отправлен: не задан чат (MORPH_HOURLY_CHAT_ID или значение по умолчанию)")
            return

        # Отправляем сообщение в чат
        message_text = (
            f"🎁 <b>НОВЫЙ ЧАСОВОЙ ПРОМОКОД!</b> 🎁\n\n"
            f"💰 <b>Сумма:</b> {format_amount(amount)} MORPH\n"
            f"👥 <b>Активаций:</b> {activations}\n"
            f"⏰ <b>Действует до:</b> {expires_time}\n\n"
            f"🎯 <b>Промокод:</b> <code>{promo_code}</code>\n\n"
            f"💡 <b>Активируйте командой:</b>\n"
            f"<code>промо {promo_code}</code>\n\n"
            f"⚡ <b>Успейте активировать первыми!</b>"
        )
        
        sent_message = await bot.send_message(
            chat_id=target_chat_id,
            text=message_text,
            parse_mode='HTML'
        )

        # Сохраняем информацию о рассылке
        promo_broadcasts[promo_code] = {
            'chat_id': target_chat_id,
            'message_id': sent_message.message_id,
            'sent_at': current_time.strftime('%Y-%m-%d %H:%M:%S')
        }
        save_promo_broadcasts()
        
        print(f"✅ Отправлен часовой промокод: {promo_code} на {amount} MORPH")
        
    except Exception as e:
        print(f"❌ Ошибка отправки часового промокода: {e}")

# Функция для тестирования - отправляет промокод сразу
async def test_hourly_promo(bot):
    """Тестовая функция для немедленной отправки промокода"""
    print("🧪 Тестовая отправка промокода...")
    await send_hourly_promo(bot)

# Добавьте в функцию main() после инициализации бота:
async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем планировщик часовых промокодов
    asyncio.create_task(hourly_promo_scheduler(bot))
    
    # Для тестирования - раскомментируйте строку ниже для немедленной отправки
    # asyncio.create_task(test_hourly_promo(bot))
    
    # Запускаем другие планировщики очистки
    asyncio.create_task(hilo_cleanup_scheduler())
    asyncio.create_task(mines_cleanup_scheduler())
    asyncio.create_task(pirate_cleanup_scheduler())
    asyncio.create_task(vilin_cleanup_scheduler())
    
    await dp.start_polling(bot)

#Покер
# Карты и комбинации
POKER_SUITS = ['♠', '♥', '♦', '♣']
POKER_VALUES = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
POKER_HANDS = {
    'royal_flush': 10,
    'straight_flush': 9,
    'four_of_a_kind': 8,
    'full_house': 7,
    'flush': 6,
    'straight': 5,
    'three_of_a_kind': 4,
    'two_pairs': 3,
    'one_pair': 2,
    'high_card': 1
}

# Команда покера
@router.message(lambda message: message.text and message.text.lower().startswith(('папауиимпвципкци')))
async def start_poker_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "🎴 <b>ПОКЕР (Техасский Холдем)</b>\n\n"
                "❌ Использование: <b>покер [ставка/ВСЁ]</b>\n"
                "Пример: <b>покер ВСЁ</b>\n"
                "🎯 Минимальная ставка: 100 MORPH\n\n"
                "💡 <b>Правила:</b>\n"
                "• Игра против дилера\n"
                "• 5 общих карт на столе\n"
                "• 2 карты у вас\n"
                "• Собери лучшую комбинацию!",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Создаем колоду и перемешиваем
        deck = [(value, suit) for value in POKER_VALUES for suit in POKER_SUITS]
        random.shuffle(deck)
        
        # Раздаем карты
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]
        community_cards = []
        
        # Стадии игры: preflop, flop, turn, river, showdown
        active_poker_games[user_id] = {
            'deck': deck,
            'player_hand': player_hand,
            'dealer_hand': dealer_hand,
            'community_cards': community_cards,
            'bet': bet,
            'stage': 'preflop',
            'player_folded': False,
            'current_bet': bet,
            'game_owner': user_id  # Владелец игры для защиты
        }
        
        await send_poker_game_state(message, user_id)
        
    except Exception as e:
        await message.reply(f"❌ Ошибка запуска игры: {str(e)}")

async def send_poker_game_state(message_or_callback, user_id, action=None, result=None):
    if user_id not in active_poker_games:
        return
    
    game = active_poker_games[user_id]
    player_hand = game['player_hand']
    community_cards = game['community_cards']
    stage = game['stage']
    bet = game['bet']
    
    # Форматируем карты
    def format_cards(cards, hide=False):
        if hide:
            return "🂠 🂠"
        return " ".join([f"{value}{suit}" for value, suit in cards])
    
    # Текст игры
    text = f"🎴 <b>ПОКЕР ТЕХАССКИЙ ХОЛДЕМ</b>\n\n"
    text += f"💰 Ставка: <b>{format_amount(bet)} MORPH</b>\n"
    text += f"📊 Стадия: <b>{get_stage_name(stage)}</b>\n\n"
    
    text += f"👤 <b>Ваши карты:</b>\n{format_cards(player_hand)}\n\n"
    
    if community_cards:
        text += f"🎯 <b>Общие карты:</b>\n{format_cards(community_cards)}\n\n"
    
    # Показываем карты дилера только в showdown
    if stage == 'showdown':
        text += f"🏦 <b>Карты дилера:</b>\n{format_cards(game['dealer_hand'])}\n\n"
    
    if result:
        text += f"🎯 <b>Результат:</b> {result}\n\n"
    
    # Создаем клавиатуру
    builder = InlineKeyboardBuilder()
    
    if stage == 'preflop':
        if not game['player_folded']:
            builder.button(text='✅ Проверить', callback_data=f'poker_check_{user_id}')
            builder.button(text='📈 Увеличить', callback_data=f'poker_raise_{user_id}')
            builder.button(text='❌ Сбросить', callback_data=f'poker_fold_{user_id}')
        builder.adjust(2, 1)
    
    elif stage in ['flop', 'turn', 'river']:
        if not game['player_folded']:
            builder.button(text='✅ Проверить', callback_data=f'poker_check_{user_id}')
            builder.button(text='📈 Увеличить', callback_data=f'poker_raise_{user_id}')
            builder.button(text='❌ Сбросить', callback_data=f'poker_fold_{user_id}')
            builder.button(text='🎯 Вскрытие', callback_data=f'poker_showdown_{user_id}')
        builder.adjust(2, 2)
    
    elif stage == 'showdown':
        builder.button(text='🔄 Играть снова', callback_data=f'poker_newgame_{user_id}')
        builder.button(text='💰 Забрать выигрыш', callback_data=f'poker_cashout_{user_id}')
        builder.adjust(2)
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.reply(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    else:
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')

def get_stage_name(stage):
    stages = {
        'preflop': 'Префлоп',
        'flop': 'Флоп',
        'turn': 'Терн', 
        'river': 'Ривер',
        'showdown': 'Вскрытие карт'
    }
    return stages.get(stage, stage)

# Функции для определения комбинаций
def evaluate_hand(cards):
    """Оценка комбинации из 7 карт"""
    all_cards = cards
    
    # Сортируем карты по значению
    values = [card[0] for card in all_cards]
    suits = [card[1] for card in all_cards]
    
    value_counts = {value: values.count(value) for value in set(values)}
    suit_counts = {suit: suits.count(suit) for suit in set(suits)}
    
    # Проверяем комбинации от самой сильной к слабой
    if is_royal_flush(all_cards):
        return 'royal_flush'
    elif is_straight_flush(all_cards):
        return 'straight_flush'
    elif 4 in value_counts.values():
        return 'four_of_a_kind'
    elif sorted(value_counts.values()) == [2, 3]:
        return 'full_house'
    elif 5 in suit_counts.values() or 6 in suit_counts.values() or 7 in suit_counts.values():
        return 'flush'
    elif is_straight(values):
        return 'straight'
    elif 3 in value_counts.values():
        return 'three_of_a_kind'
    elif list(value_counts.values()).count(2) >= 2:
        return 'two_pairs'
    elif 2 in value_counts.values():
        return 'one_pair'
    else:
        return 'high_card'

def is_royal_flush(cards):
    """Роял-флэш"""
    return is_straight_flush(cards) and any(card[0] == 'A' for card in cards)

def is_straight_flush(cards):
    """Стрит-флэш"""
    return is_flush(cards) and is_straight([card[0] for card in cards])

def is_flush(cards):
    """Флэш"""
    suits = [card[1] for card in cards]
    return any(suits.count(suit) >= 5 for suit in set(suits))

def is_straight(values):
    """Стрит"""
    value_order = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    unique_values = list(set(values))
    unique_values.sort(key=lambda x: value_order.index(x))
    
    for i in range(len(unique_values) - 4):
        if all(value_order.index(unique_values[i+j]) == value_order.index(unique_values[i]) + j 
               for j in range(5)):
            return True
    
    # Проверяем стрит с тузом как 1
    if 'A' in unique_values:
        low_values = ['A'] + [v for v in unique_values if v != 'A']
        for i in range(len(low_values) - 4):
            if all(value_order.index(low_values[i+j]) == value_order.index(low_values[i]) + j 
                   for j in range(5)):
                return True
    return False

def get_hand_strength(hand_name):
    """Сила комбинации"""
    return POKER_HANDS.get(hand_name, 0)

# Обработчики кнопок с защитой
@router.callback_query(lambda c: c.data.startswith('poker_'))
async def poker_callback(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    
    data = callback.data.split('_')
    action = data[1]
    target_user_id = int(data[2])
    
    # 🔒 ЗАЩИТА: проверяем, что нажимает владелец игры
    if callback.from_user.id != target_user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    
    if target_user_id not in active_poker_games:
        await callback.answer("❌ Игра не найдена!")
        return
    
    game = active_poker_games[target_user_id]
    
    if action == 'check':
        await handle_poker_check(callback, target_user_id)
    
    elif action == 'raise':
        await handle_poker_raise(callback, target_user_id)
    
    elif action == 'fold':
        await handle_poker_fold(callback, target_user_id)
    
    elif action == 'showdown':
        await handle_poker_showdown(callback, target_user_id)
    
    elif action == 'newgame':
        await handle_poker_newgame(callback, target_user_id)
    
    elif action == 'cashout':
        await handle_poker_cashout(callback, target_user_id)

async def handle_poker_check(callback: CallbackQuery, user_id):
    """Проверка/колл"""
    game = active_poker_games[user_id]
    
    if game['stage'] == 'preflop':
        # Раздаем флоп
        game['community_cards'] = [game['deck'].pop() for _ in range(3)]
        game['stage'] = 'flop'
    
    elif game['stage'] == 'flop':
        # Раздаем терн
        game['community_cards'].append(game['deck'].pop())
        game['stage'] = 'turn'
    
    elif game['stage'] == 'turn':
        # Раздаем ривер
        game['community_cards'].append(game['deck'].pop())
        game['stage'] = 'river'
    
    elif game['stage'] == 'river':
        # Переходим к вскрытию
        game['stage'] = 'showdown'
        await evaluate_poker_hand(callback, user_id)
        return
    
    await send_poker_game_state(callback, user_id)
    await callback.answer("✅ Проверка")

async def handle_poker_raise(callback: CallbackQuery, user_id):
    """Увеличение ставки"""
    game = active_poker_games[user_id]
    
    # Увеличиваем ставку на 50%
    raise_amount = int(game['current_bet'] * 0.5)
    game['current_bet'] += raise_amount
    
    if game['stage'] == 'preflop':
        game['community_cards'] = [game['deck'].pop() for _ in range(3)]
        game['stage'] = 'flop'
    
    elif game['stage'] == 'flop':
        game['community_cards'].append(game['deck'].pop())
        game['stage'] = 'turn'
    
    elif game['stage'] == 'turn':
        game['community_cards'].append(game['deck'].pop())
        game['stage'] = 'river'
    
    elif game['stage'] == 'river':
        game['stage'] = 'showdown'
        await evaluate_poker_hand(callback, user_id)
        return
    
    await send_poker_game_state(callback, user_id)
    await callback.answer(f"📈 Ставка увеличена на {format_amount(raise_amount)}")

async def handle_poker_fold(callback: CallbackQuery, user_id):
    """Сброс карт"""
    game = active_poker_games[user_id]
    game['player_folded'] = True
    game['stage'] = 'showdown'
    
    # Игрок проиграл при сбросе
    result_text = "❌ Вы сбросили карты! Проигрыш."
    
    users_data[user_id]['games_played'] += 1
    save_users()
    
    await send_poker_game_state(callback, user_id, result=result_text)
    await callback.answer("❌ Карты сброшены")

async def handle_poker_showdown(callback: CallbackQuery, user_id):
    """Досрочное вскрытие"""
    game = active_poker_games[user_id]
    game['stage'] = 'showdown'
    await evaluate_poker_hand(callback, user_id)
    await callback.answer("🎯 Вскрытие карт")

async def evaluate_poker_hand(callback: CallbackQuery, user_id):
    """Оценка рук и определение победителя"""
    game = active_poker_games[user_id]
    
    if game['player_folded']:
        result_text = "❌ Вы сбросили карты! Проигрыш."
        await send_poker_game_state(callback, user_id, result=result_text)
        return
    
    # Все карты для оценки
    player_all_cards = game['player_hand'] + game['community_cards']
    dealer_all_cards = game['dealer_hand'] + game['community_cards']
    
    # Оцениваем комбинации
    player_hand = evaluate_hand(player_all_cards)
    dealer_hand = evaluate_hand(dealer_all_cards)
    
    player_strength = get_hand_strength(player_hand)
    dealer_strength = get_hand_strength(dealer_hand)
    
    hand_names = {
        'royal_flush': 'Роял-флэш 🏆',
        'straight_flush': 'Стрит-флэш 🔥', 
        'four_of_a_kind': 'Каре 4️⃣',
        'full_house': 'Фулл-хаус 🏠',
        'flush': 'Флэш 💧',
        'straight': 'Стрит 📏',
        'three_of_a_kind': 'Тройка 3️⃣',
        'two_pairs': 'Две пары 2️⃣2️⃣',
        'one_pair': 'Пара 2️⃣',
        'high_card': 'Старшая карта 🃏'
    }
    
    player_hand_name = hand_names.get(player_hand, player_hand)
    dealer_hand_name = hand_names.get(dealer_hand, dealer_hand)
    
    # Определяем победителя
    if player_strength > dealer_strength:
        # Победа
        multiplier = get_poker_multiplier(player_hand)
        win_amount = int(game['bet'] * multiplier)
        add_win_to_user(user_id, win_amount, game['bet'])
        add_game_to_history(user_id, 'Покер', game['bet'], 'win', win_amount)
        result_text = f"🎉 ПОБЕДА! {player_hand_name}\n💰 +{format_amount(win_amount)} MORPH (x{multiplier})"
    
    elif player_strength < dealer_strength:
        # Проигрыш
        add_game_to_history(user_id, 'Покер', game['bet'], 'lose', 0)
        result_text = f"❌ ПРОИГРЫШ! У дилера {dealer_hand_name}"
    
    else:
        # Ничья - возвращаем ставку
        users_data[user_id]['balance'] += game['bet']
        add_game_to_history(user_id, 'Покер', game['bet'], 'draw', game['bet'])
        result_text = f"🤝 НИЧЬЯ! {player_hand_name}"
    
    users_data[user_id]['games_played'] += 1
    save_users()
    
    await send_poker_game_state(callback, user_id, result=result_text)

def get_poker_multiplier(hand_name):
    """Множители для разных комбинаций"""
    multipliers = {
        'royal_flush': 100,
        'straight_flush': 50,
        'four_of_a_kind': 25,
        'full_house': 9,
        'flush': 6,
        'straight': 4,
        'three_of_a_kind': 3,
        'two_pairs': 2,
        'one_pair': 1,
        'high_card': 0.5
    }
    return multipliers.get(hand_name, 1)

async def handle_poker_newgame(callback: CallbackQuery, user_id):
    """Новая игра с той же ставкой"""
    if user_id not in active_poker_games:
        await callback.answer("❌ Игра не найдена!")
        return
    
    old_game = active_poker_games[user_id]
    bet = old_game['bet']
    
    # Проверяем баланс
    if users_data[user_id]['balance'] < bet:
        await callback.answer("❌ Недостаточно MORPH для новой игры!")
        return
    
    # Списываем ставку
    users_data[user_id]['balance'] -= bet
    save_users()
    
    # Создаем новую колоду
    deck = [(value, suit) for value in POKER_VALUES for suit in POKER_SUITS]
    random.shuffle(deck)
    
    # Новая игра
    active_poker_games[user_id] = {
        'deck': deck,
        'player_hand': [deck.pop(), deck.pop()],
        'dealer_hand': [deck.pop(), deck.pop()],
        'community_cards': [],
        'bet': bet,
        'stage': 'preflop',
        'player_folded': False,
        'current_bet': bet,
        'game_owner': user_id
    }
    
    await send_poker_game_state(callback, user_id)
    await callback.answer("🔄 Новая игра!")

async def handle_poker_cashout(callback: CallbackQuery, user_id):
    """Выход из игры"""
    if user_id in active_poker_games:
        del active_poker_games[user_id]
    
    await callback.message.edit_text(
        f"💰 <b>Игра завершена!</b>\n\n"
        f"💸 Возвращайтесь в покер снова!\n"
        f"💰 Ваш баланс: <b>{format_amount(users_data[user_id]['balance'])} MORPH</b>",
        parse_mode='HTML'
    )
    await callback.answer("💰 Выход из игры")

# Команда для предложения брака (ответом на сообщение)
@router.message(lambda message: message.text and message.text.lower() == "брак предложить")
async def propose_marriage(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение пользователя, которому хотите предложить брак!")
        return
    
    sender_id = message.from_user.id
    receiver_id = message.reply_to_message.from_user.id
    
    if sender_id == receiver_id:
        await message.reply("❌ Нельзя предложить брак самому себе!")
        return
    
    # Проверяем, не состоит ли уже в браке
    if sender_id in marriages or receiver_id in marriages:
        await message.reply("❌ Один из вас уже состоит в браке!")
        return
    
    # Проверяем, нет ли уже активного предложения
    if receiver_id in marriage_requests:
        await message.reply("❌ Этому пользователю уже отправлено предложение!")
        return
    
    # Создаем предложение
    marriage_requests[receiver_id] = {
        'sender_id': sender_id,
        'sender_name': message.from_user.first_name,
        'timestamp': time.time()
    }
    
    # Создаем инлайн-клавиатуру
    builder = InlineKeyboardBuilder()
    builder.button(text="💍 Принять", callback_data=f"marriage_accept_{sender_id}")
    builder.button(text="❌ Отклонить", callback_data=f"marriage_reject_{sender_id}")
    
    await message.reply(
        f"💍 <b>ПРЕДЛОЖЕНИЕ БРАКА</b>\n\n"
        f"👤 {message.from_user.first_name} предлагает брак пользователю {message.reply_to_message.from_user.first_name}!\n\n"
        f"💝 Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# Обработка принятия брака
@router.callback_query(lambda c: c.data.startswith('marriage_accept_'))
async def accept_marriage(callback: CallbackQuery):
    receiver_id = callback.from_user.id
    sender_id = int(callback.data.split('_')[2])
    
    # Проверяем существование предложения
    if receiver_id not in marriage_requests:
        await callback.answer("❌ Предложение не найдено или устарело!", show_alert=True)
        return
    
    if marriage_requests[receiver_id]['sender_id'] != sender_id:
        await callback.answer("❌ Это предложение не для вас!", show_alert=True)
        return
    
    # Создаем брак
    marriage_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    marriages[sender_id] = {
        'spouse_id': receiver_id,
        'spouse_name': callback.from_user.first_name,
        'date': marriage_date
    }
    marriages[receiver_id] = {
        'spouse_id': sender_id,
        'spouse_name': marriage_requests[receiver_id]['sender_name'],
        'date': marriage_date
    }
    
    # Сохраняем браки в Firebase
    save_marriages()
    
    # Удаляем предложение
    del marriage_requests[receiver_id]
    
    # Получаем информацию о пользователях
    sender_name = marriages[receiver_id]['spouse_name']
    receiver_name = callback.from_user.first_name
    
    await callback.message.edit_text(
        f"🎉 <b>ПОЗДРАВЛЯЕМ С БРАКОМ!</b>\n\n"
        f"💑 <b>{sender_name}</b> 💞 <b>{receiver_name}</b>\n"
        f"📅 Дата брака: <i>{marriage_date}</i>\n\n"
        f"💝 Теперь вы официальная пара!\n"
        f"💔 Для развода используйте команду: <code>развод</code>",
        parse_mode="HTML"
    )
    await callback.answer("💍 Брак принят!")

# Обработка отклонения брака
@router.callback_query(lambda c: c.data.startswith('marriage_reject_'))
async def reject_marriage(callback: CallbackQuery):
    receiver_id = callback.from_user.id
    sender_id = int(callback.data.split('_')[2])
    
    # Проверяем существование предложения
    if receiver_id not in marriage_requests:
        await callback.answer("❌ Предложение не найдено или устарело!", show_alert=True)
        return
    
    if marriage_requests[receiver_id]['sender_id'] != sender_id:
        await callback.answer("❌ Это предложение не для вас!", show_alert=True)
        return
    
    sender_name = marriage_requests[receiver_id]['sender_name']
    
    # Удаляем предложение
    del marriage_requests[receiver_id]
    
    await callback.message.edit_text(
        f"❌ <b>ПРЕДЛОЖЕНИЕ БРАКА ОТКЛОНЕНО</b>\n\n"
        f"💔 {callback.from_user.first_name} отклонил(а) предложение брака от {sender_name}"
    )
    await callback.answer("❌ Брак отклонен")

# Команда для просмотра информации о браке
@router.message(lambda message: message.text and message.text.lower() == "брак")
async def marriage_info(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    if user_id in marriages:
        # Показываем информацию о браке
        marriage = marriages[user_id]
        spouse_id = marriage['spouse_id']
        spouse_name = marriage['spouse_name']
        
        # Вычисляем длительность брака
        marriage_date = datetime.strptime(marriage['date'], '%Y-%m-%d %H:%M:%S')
        duration = datetime.now() - marriage_date
        days = duration.days
        hours = duration.seconds // 3600
        
        await message.reply(
            f"💑 <b>ВАШ БРАК</b>\n\n"
            f"👤 Супруг(а): <b>{spouse_name}</b>\n"
            f"📅 Дата брака: <i>{marriage['date']}</i>\n"
            f"⏳ Вместе уже: <b>{days}</b> дней, <b>{hours}</b> часов\n\n"
            f"💔 Для развода: <code>развод</code>",
            parse_mode="HTML"
        )
    else:
        # Показываем информацию о предложениях
        if user_id in marriage_requests:
            sender_name = marriage_requests[user_id]['sender_name']
            await message.reply(
                f"💍 <b>У ВАС ЕСТЬ ПРЕДЛОЖЕНИЕ БРАКА!</b>\n\n"
                f"👤 От: <b>{sender_name}</b>\n"
                f"💝 Проверьте предыдущие сообщения с кнопками для ответа\n\n"
                f"💡 Предложение действует 24 часа",
                parse_mode="HTML"
            )
        else:
            await message.reply(
                f"💑 <b>ИНФОРМАЦИЯ О БРАКЕ</b>\n\n"
                f"❌ Вы не состоите в браке\n\n"
                f"💍 Чтобы предложить брак:\n"
                f"Ответьте на сообщение пользователя командой:\n"
                f"<code>брак предложить</code>\n\n"
                f"💝 Чтобы принять/отклонить предложение:\n"
                f"Используйте кнопки в сообщении с предложением",
                parse_mode="HTML"
            )

# Команда для развода
@router.message(lambda message: message.text and message.text.lower() == "развод")
async def divorce(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    if user_id not in marriages:
        await message.reply("❌ Вы не состоите в браке!")
        return
    
    marriage = marriages[user_id]
    spouse_id = marriage['spouse_id']
    spouse_name = marriage['spouse_name']
    
    # Вычисляем длительность брака
    marriage_date = datetime.strptime(marriage['date'], '%Y-%m-%d %H:%M:%S')
    duration = datetime.now() - marriage_date
    days = duration.days
    
    # Удаляем брак
    del marriages[user_id]
    del marriages[spouse_id]
    
    # Сохраняем изменения в Firebase
    save_marriages()
    
    await message.reply(
        f"💔 <b>БРАК РАСТОРГНУТ</b>\n\n"
        f"👤 {message.from_user.first_name} и {spouse_name} больше не вместе\n"
        f"📅 Брак длился: <b>{days}</b> дней\n\n"
        f"💝 Надеемся, вы останетесь друзьями!",
        parse_mode="HTML"
    )

# Команда для просмотра всех пар
@router.message(lambda message: message.text and message.text.lower() == "пары")
async def married_couples(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if not marriages:
        await message.reply("💔 <b>Пока нет ни одной пары</b>", parse_mode="HTML")
        return
    
    # Убираем дубликаты (каждая пара встречается дважды)
    seen = set()
    couples_text = "💑 <b>ВСЕ ПАРЫ</b>\n\n"
    
    for user_id, marriage in marriages.items():
        spouse_id = marriage['spouse_id']
        
        # Проверяем, не показывали ли уже эту пару
        pair = tuple(sorted([user_id, spouse_id]))
        if pair in seen:
            continue
        
        seen.add(pair)
        
        # Получаем имена
        user_name = message.bot.get_chat(user_id).first_name
        spouse_name = marriage['spouse_name']
        
        # Вычисляем длительность брака
        marriage_date = datetime.strptime(marriage['date'], '%Y-%m-%d %H:%M:%S')
        duration = datetime.now() - marriage_date
        days = duration.days
        
        couples_text += f"💞 <b>{user_name}</b> + <b>{spouse_name}</b>\n"
        couples_text += f"   📅 {days} дней вместе\n\n"
    
    await message.reply(couples_text, parse_mode="HTML")

# Очистка просроченных предложений брака
async def cleanup_marriage_requests():
    """Очистка просроченных предложений брака (старше 24 часов)"""
    current_time = time.time()
    expired_requests = []
    
    for receiver_id, request in marriage_requests.items():
        if current_time - request['timestamp'] > 86400:  # 24 часа
            expired_requests.append(receiver_id)
    
    for receiver_id in expired_requests:
        del marriage_requests[receiver_id]
    
    if expired_requests:
        print(f"Очищено {len(expired_requests)} просроченных предложений брака")

# Запускаем очистку каждые 6 часов
async def marriage_cleanup_scheduler():
    while True:
        await asyncio.sleep(21600)  # 6 часов
        await cleanup_marriage_requests()

# Добавь в функцию main() после инициализации бота:
async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем очистку просроченных предложений брака
    asyncio.create_task(marriage_cleanup_scheduler())
    
    # Остальные планировщики...
    asyncio.create_task(hilo_cleanup_scheduler())
    asyncio.create_task(mines_cleanup_scheduler())
    asyncio.create_task(pirate_cleanup_scheduler())
    asyncio.create_task(vilin_cleanup_scheduler())
    
    await dp.start_polling(bot)

# Команда слотов
@router.message(lambda message: message.text and message.text.lower().startswith('слоты'))
async def slot_machine(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("🎰 <b>СЛОТ-МАШИНА</b>\n\n<code>слот [ставка]</code>\nПример: <code>слот 1000</code>", parse_mode="HTML")
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        # Только минимальная проверка
        if bet < 100:
            await message.reply("❌ Минимальная ставка: 100 MORPH")
            return
            
        if users_data[user_id]['balance'] < bet:
            await message.reply(f"❌ Недостаточно MORPH!")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        
        # Анимация
        msg = await message.reply("🎰 | ⚫ | ⚫ | ⚫ |\nКрутим...")
        await asyncio.sleep(1)
        
        await msg.edit_text("🎰 | 🍒 | ⚫ | ⚫ |\nКрутим...")
        await asyncio.sleep(1)
        
        await msg.edit_text("🎰 | 🍒 | 🍋 | ⚫ |\nКрутим...")
        await asyncio.sleep(1)
        
        # Результат
        symbols = ["🍒", "🍋", "🍊", "🍇", "🔔", "💎", "⭐", "7️⃣"]
        reel1 = random.choice(symbols)
        reel2 = random.choice(symbols)
        reel3 = random.choice(symbols)
        
        # Выигрыши
        if reel1 == reel2 == reel3:
            if reel1 == "7️⃣":
                win = bet * 50
            elif reel1 == "💎":
                win = bet * 25
            elif reel1 == "⭐":
                win = bet * 15
            elif reel1 == "🔔":
                win = bet * 10
            else:
                win = bet * 5
        elif reel1 == reel2 or reel2 == reel3:
            win = bet * 2
        else:
            win = 0
        
        # Выплата
        if win > 0:
            add_win_to_user(user_id, win, bet)
            add_game_to_history(user_id, 'Слоты', bet, 'win', win)
            result = f"🎉 ВЫИГРЫШ! +{format_amount(win)} MORPH"
        else:
            add_game_to_history(user_id, 'Слоты', bet, 'lose', 0)
            users_data[user_id]['games_played'] += 1
            save_users()
            result = "❌ ПРОИГРЫШ"
        
        await msg.edit_text(f"🎰 | {reel1} | {reel2} | {reel3} |\n\n{result}\nСтавка: {format_amount(bet)} MORPH")
        
    except Exception as e:
        await message.reply("❌ Ошибка")

#Колесо
# Конфигурация колеса удачи с пониженными шансами (25% выигрыш)
WHEEL_OF_FORTUNE = [
    {"multiplier": 0.0, "emoji": "💀", "name": "Проигрыш", "weight": 40},  # Увеличено с 20
    {"multiplier": 0.5, "emoji": "😢", "name": "Половина", "weight": 30},  # Увеличено с 15
    {"multiplier": 1.0, "emoji": "😐", "name": "Возврат", "weight": 15},   # Уменьшено с 20
    {"multiplier": 1.5, "emoji": "🙂", "name": "Маленький выигрыш", "weight": 6},  # Уменьшено с 15
    {"multiplier": 2.0, "emoji": "😊", "name": "Выигрыш", "weight": 7},    # Уменьшено с 10
    {"multiplier": 3.0, "emoji": "💰", "name": "Крупный выигрыш", "weight": 4},    # Уменьшено с 8
    {"multiplier": 5.0, "emoji": "🎉", "name": "Большой куш", "weight": 3},        # Уменьшено с 5
    {"multiplier": 10.0, "emoji": "🎰", "name": "Джекпот", "weight": 1},           # Уменьшено с 4
    {"multiplier": 0.25, "emoji": "💸", "name": "Большой проигрыш", "weight": 0},  # Убрано
]

# Веса для random.choices
wheel_weights = [sector["weight"] for sector in WHEEL_OF_FORTUNE]

@router.message(lambda message: message.text and message.text.lower().startswith(('колесо', 'wheel')))
async def start_wheel_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                '🎡 <b>Колесо Удачи</b>\n\n'
                '🎯 Крути колесо и получай множители:\n'
                '💀 x0.0 | 😢 x0.5 | 😐 x1.0\n'
                '🙂 x1.5 | 😊 x2.0 | 💰 x3.0\n'
                '🎉 x5.0 | 🎰 x10.0 | 💸 x0.25\n\n'
                '<code>колесо [ставка]</code>\n'
                'Пример: <code>колесо 1000</code>',
                parse_mode='HTML'
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Анимация вращения
        msg = await message.reply('🎡 <b>Колесо вращается...</b>', parse_mode='HTML')
        await asyncio.sleep(2)
        
        # Выбираем результат
        result = random.choices(WHEEL_OF_FORTUNE, weights=wheel_weights)[0]
        win_amount = int(bet * result["multiplier"])
        
        # Выплачиваем выигрыш и обновляем историю/лидерборд
        if result["multiplier"] >= 1.0:
            # Выигрыш или возврат
            add_win_to_user(user_id, win_amount, bet)
            add_game_to_history(user_id, 'Колесо удачи', bet, 'win', win_amount)
        else:
            # Проигрыш (множитель < 1)
            if win_amount > 0:
                users_data[user_id]['balance'] += win_amount
                save_users()
            add_game_to_history(user_id, 'Колесо удачи', bet, 'lose', win_amount)
            users_data[user_id]['games_played'] += 1
            save_users()
        
        # Показываем результат
        if result["multiplier"] == 0.0:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n❌ <b>Ставка сгорела</b>', parse_mode='HTML')
        elif result["multiplier"] == 0.25:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n📉 <b>Возврат: {format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        elif result["multiplier"] == 0.5:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n😢 <b>Возврат: {format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        elif result["multiplier"] == 1.0:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n↩️ <b>Ставка вернулась</b>', parse_mode='HTML')
        elif result["multiplier"] <= 3.0:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n💰 <b>+{format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        elif result["multiplier"] <= 5.0:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n🎉 <b>+{format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        else:
            await msg.edit_text(f'{result["emoji"]} <b>{result["name"]}</b>\n🎰 <b>+{format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        
    except Exception as e:
        await message.reply(f'❌ Ошибка: {str(e)}')

#Такси
# Упрощенная база пассажиров
# УСЛОЖНЕННАЯ база пассажиров с большим количеством поражений
TAXI_PASSENGERS = [
    # 🔴 ПЛОХИЕ ПАССАЖИРЫ (60% шанс) - УВЕЛИЧЕНО С 45%
    {"type": "bad", "name": "быдло", "multiplier": 0.0, "emoji": "💢"},
    {"type": "bad", "name": "пьяный", "multiplier": 0.0, "emoji": "🍺"},
    {"type": "bad", "name": "мошенник", "multiplier": 0.0, "emoji": "🎭"},
    {"type": "bad", "name": "забывчивый", "multiplier": 0.0, "emoji": "🤦"},
    {"type": "bad", "name": "скандалист", "multiplier": 0.0, "emoji": "😠"},
    {"type": "bad", "name": "грязный", "multiplier": 0.0, "emoji": "🤢"},
    {"type": "bad", "name": "вор", "multiplier": 0.0, "emoji": "👿"},
    
    # 🟡 НЕЙТРАЛЬНЫЕ (25% шанс) - УМЕНЬШЕНО С 30%
    {"type": "neutral", "name": "обычный", "multiplier": 1.0, "emoji": "😐"},
    {"type": "neutral", "name": "молчун", "multiplier": 1.0, "emoji": "🤫"},
    
    # 🟢 ХОРОШИЕ (12% шанс) - УМЕНЬШЕНО С 20% И УРЕЗАНЫ КОЭФФИЦИЕНТЫ
    {"type": "good", "name": "щедрый", "multiplier": 1.8, "emoji": "💰"},  # Было 2.0
    {"type": "good", "name": "бизнесмен", "multiplier": 1.6, "emoji": "💼"},  # Было 1.8
    {"type": "good", "name": "турист", "multiplier": 1.9, "emoji": "🧳"},  # Было 2.2
    
    # 🎯 ДЖЕКПОТ (3% шанс) - УМЕНЬШЕНО С 5% И УРЕЗАН КОЭФФИЦИЕНТ
    {"type": "jackpot", "name": "миллионер", "multiplier": 2.5, "emoji": "🎰"},  # Было 3.0
]

@router.message(lambda message: message.text and message.text.lower().startswith(('такси', 'taxi')))
async def start_taxi_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                '🚕 <b>Такси</b>\n\n'
                '🎯 Подбери пассажира и получи x2-x3\n'
                '❌ Или потеряй ставку\n\n'
                '<code>такси [ставка]</code>\n'
                'Пример: <code>такси 1000</code>',
                parse_mode='HTML'
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Показываем поиск
        msg = await message.reply('🚕 <b>Ищем пассажира...</b>', parse_mode='HTML')
        await asyncio.sleep(2)
        
        # Выбираем пассажира
        passenger = random.choice(TAXI_PASSENGERS)
        win_amount = int(bet * passenger["multiplier"])
        
        # Выплачиваем выигрыш и обновляем историю/лидерборд
        if passenger["multiplier"] >= 1.0:
            # Выигрыш или возврат
            add_win_to_user(user_id, win_amount, bet)
            add_game_to_history(user_id, 'Такси', bet, 'win', win_amount)
        else:
            # Проигрыш
            if win_amount > 0:
                users_data[user_id]['balance'] += win_amount
                save_users()
            add_game_to_history(user_id, 'Такси', bet, 'lose', win_amount)
            users_data[user_id]['games_played'] += 1
            save_users()
        
        # Показываем результат
        if passenger["multiplier"] == 0.0:
            await msg.edit_text(f'{passenger["emoji"]} <b>Попался {passenger["name"]}</b>\n❌ <b>Ставка сгорела</b>', parse_mode='HTML')
        elif passenger["multiplier"] == 1.0:
            await msg.edit_text(f'{passenger["emoji"]} <b>Попался {passenger["name"]}</b>\n↩️ <b>Ставка вернулась</b>', parse_mode='HTML')
        elif passenger["multiplier"] == 3.0:
            await msg.edit_text(f'{passenger["emoji"]} <b>Попался {passenger["name"]}</b>\n🎰 <b>+{format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        else:
            await msg.edit_text(f'{passenger["emoji"]} <b>Попался {passenger["name"]}</b>\n💰 <b>+{format_amount(win_amount)} MORPH</b>', parse_mode='HTML')
        
    except Exception as e:
        await message.reply(f'❌ Ошибка: {str(e)}')

@router.callback_query(lambda c: c.data.startswith('taxi_again_'))
async def taxi_again_callback(callback: CallbackQuery):
    data = callback.data.split('_')
    user_id = int(data[2])
    bet = int(data[3])
    
    if user_id not in users_data:
        await callback.answer('❌ Пользователь не найден!')
        return
    
    # Проверяем баланс
    if users_data[user_id]['balance'] < bet:
        await callback.answer('❌ Недостаточно MORPH для ставки!')
        return
    
    # Списываем ставку
    users_data[user_id]['balance'] -= bet
    save_users()
    
    # Создаем новую игру
    active_taxi_games[user_id] = {
        'bet': bet,
        'passenger': None,
        'result': None
    }
    
    await process_taxi_ride(callback, user_id)
    await callback.answer()

@router.message(lambda message: message.text and message.text.lower().startswith(('хакер')))
async def start_crypto_hacker(message: types.Message):
    if is_banned(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply(
                "❌ Использование: <b>хакер [ставка/ВСЁ]</b>\n"
                "Пример: <b>хакер ВСЁ</b>\n"
                "🎯 Минимальная ставка: 100 MORPH",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Создаём игру с новыми балансами
        active_crypto_hacker_games[user_id] = {
            'original_bet': bet,
            'current_bet': bet,
            'level': 1,
            'max_level': 5,
            'multiplier': 1.0,
            'wallet': [],  # Заполним случайно
            'cashout_used': False
        }
        
        # Генерируем кошелёк: 1 = BTC, 0 = VIRUS
        game = active_crypto_hacker_games[user_id]
        for level in range(1, game['max_level'] + 1):
            # УВЕЛИЧЕННЫЙ ШАНС ВИРУСА: 30%, 50%, 70%, 80%, 90%
            virus_chance = min(90, (level - 1) * 20 + 30)  # 1й уровень: 30%, 2й: 50%, 3й: 70%, 4й: 80%, 5й: 90%
            game['wallet'].append(1 if random.randint(1, 100) > virus_chance else 0)
        
        await send_crypto_hacker_game(message, user_id)
        
    except Exception as e:
        await message.reply(f"❌ Ошибка запуска игры: {str(e)}")

async def send_crypto_hacker_game(message_or_callback, user_id, result_level=None, win=None):
    if user_id not in active_crypto_hacker_games:
        return
    
    game = active_crypto_hacker_games[user_id]
    current_level = game['level']
    bet = game['current_bet']
    multiplier = game['multiplier']
    
    # Эмодзи для отображения уровней
    level_emojis = {
        1: "1️⃣",
        2: "2️⃣", 
        3: "3️⃣",
        4: "4️⃣",
        5: "5️⃣"
    }
    
    # Создаём клавиатуру
    builder = InlineKeyboardBuilder()
    
    if result_level is None:
        # Показываем выбор уровней
        for level in range(1, game['max_level'] + 1):
            if level == current_level:
                # Текущий уровень - активная кнопка
                emoji = level_emojis[level]
                builder.button(
                    text=f"{emoji} Уровень {level}",
                    callback_data=f"hacker_level_{level}_{user_id}"
                )
            elif level < current_level:
                # Пройденные уровни - отображаем результат
                was_btc = game['wallet'][level - 1] == 1
                result_emoji = "💎" if was_btc else "🦠"
                builder.button(
                    text=f"{result_emoji} Уровень {level}",
                    callback_data=f"hacker_past_{level}_{user_id}"
                )
            else:
                # Будущие уровни - заблокированы с замком
                builder.button(
                    text=f"🔒 Уровень {level}",
                    callback_data=f"hacker_locked_{level}_{user_id}"
                )
        
        builder.adjust(2)
        
        # Кнопка забрать выигрыш (только после 1го уровня)
        if current_level > 1:
            # УМЕНЬШЕННЫЙ КОЭФФИЦИЕНТ ВЫВОДА
            cashout_multiplier = multiplier * 0.8  # 20% комиссия за досрочный вывод
            builder.row(
                InlineKeyboardButton(
                    text=f"💰 Забрать {format_amount(int(bet * cashout_multiplier))} MORPH",
                    callback_data=f"hacker_cashout_{user_id}"
                )
            )
        
        # УМЕНЬШЕННЫЕ КОЭФФИЦИЕНТЫ В ОПИСАНИИ
        level_multipliers = {1: 1.5, 2: 2.2, 3: 3.0, 4: 3.8, 5: 4.5}
        
        text = (
            f"💻 <b>КРИПТО-ХАКЕР</b>\n\n"
            f"🎯 Текущий уровень: <b>{current_level}/5</b>\n"
            f"💰 Текущая ставка: <b>{format_amount(bet)} MORPH</b>\n"
            f"📊 Коэффициент: <b>{multiplier:.2f}x</b>\n"
            f"🎯 Потенциальный выигрыш: <b>{format_amount(int(bet * multiplier))} MORPH</b>\n\n"
            f"<b>Выбери уровень для взлома:</b>\n"
            f"💎 <b>BTC</b> - Увеличит твой выигрыш!\n"
            f"🦠 <b>VIRUS</b> - Заблокирует кошелёк!\n\n"
            f"💡 <b>Шансы успеха по уровням:</b>\n"
            f"• Уровень 1: 70% успеха (x{level_multipliers[1]})\n"
            f"• Уровень 2: 50% успеха (x{level_multipliers[2]})\n"
            f"• Уровень 3: 30% успеха (x{level_multipliers[3]})\n"
            f"• Уровень 4: 20% успеха (x{level_multipliers[4]})\n"
            f"• Уровень 5: 10% успеха (x{level_multipliers[5]})\n\n"
            f"⚡ <b>Риск растёт с каждым уровнем!</b>"
        )
    
    else:
        # Показываем результат взлома
        was_btc = game['wallet'][result_level - 1] == 1
        result_emoji = "💎" if was_btc else "🦠"
        result_text = "BTC - УСПЕХ!" if was_btc else "VIRUS - ПРОВАЛ!"
        
        if win is not None:
            if win:
                # Успех - предлагаем следующий уровень или вывод
                builder.button(text='🎯 Следующий уровень', callback_data=f'hacker_next_{user_id}')
                builder.button(text='💰 Забрать выигрыш', callback_data=f'hacker_cashout_{user_id}')
                builder.adjust(2)
                
                text = (
                    f"{result_emoji} <b>{result_text}</b>\n\n"
                    f"🎯 Уровень {result_level} взломан!\n"
                    f"💰 Новая ставка: <b>{format_amount(game['current_bet'])} MORPH</b>\n"
                    f"📊 Коэффициент: <b>{game['multiplier']:.2f}x</b>\n"
                    f"🎯 Текущий выигрыш: <b>{format_amount(int(game['current_bet'] * game['multiplier']))} MORPH</b>\n\n"
                    f"<b>Продолжаем взлом?</b>"
                )
            else:
                # Проигрыш - игра окончена
                text = (
                    f"{result_emoji} <b>{result_text}</b>\n\n"
                    f"💻 <b>СИСТЕМА ЗАБЛОКИРОВАНА!</b>\n"
                    f"🎯 Уровень {result_level} содержал вирус!\n\n"
                    f"💸 <b>ИГРА ОКОНЧЕНА!</b>\n"
                    f"💰 Проигрыш: <b>{format_amount(game['original_bet'])} MORPH</b>"
                )
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.reply(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    else:
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')

@router.callback_query(lambda c: c.data.startswith('hacker_level_'))
async def hacker_level_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[-1])
    level = int(callback.data.split('_')[2])
    
    if user_id not in active_crypto_hacker_games:
        await callback.answer("❌ Игра не найдена!")
        return
    
    game = active_crypto_hacker_games[user_id]
    
    # Проверяем, можно ли взламывать этот уровень
    if level != game['level']:
        await callback.answer("❌ Сначала нужно пройти предыдущие уровни!")
        return
    
    # Проверяем результат взлома
    was_btc = game['wallet'][level - 1] == 1
    
    if was_btc:
        # Успех - увеличиваем множитель и ставку
        level_multipliers = {1: 1.5, 2: 1.47, 3: 1.36, 4: 1.27, 5: 1.18}
        
        game['multiplier'] *= level_multipliers.get(level, 1.5)
        game['current_bet'] = int(game['original_bet'] * game['multiplier'])
        game['level'] += 1
        
        # Проверяем максимальный уровень
        if game['level'] > game['max_level']:
            # Автоматический вывод при достижении максимума
            await hacker_cashout_callback(callback, user_id)
            return
        
        await send_crypto_hacker_game(callback, user_id, result_level=level, win=True)
    else:
        # Проигрыш - игра окончена
        await send_crypto_hacker_game(callback, user_id, result_level=level, win=False)
        del active_crypto_hacker_games[user_id]
    
    await callback.answer()

@router.callback_query(lambda c: c.data.startswith('hacker_next_'))
async def hacker_next_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[-1])
    
    if user_id not in active_crypto_hacker_games:
        await callback.answer("❌ Игра не найдена!")
        return
    
    # Просто переходим к выбору следующего уровня
    await send_crypto_hacker_game(callback, user_id)
    await callback.answer("🎯 Переход к выбору уровня!")

@router.callback_query(lambda c: c.data.startswith('hacker_cashout_'))
async def hacker_cashout_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[-1])
    
    # 🔒 ЗАЩИТА: проверяем владельца игры
    if callback.from_user.id != user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    
    if user_id not in active_crypto_hacker_games:
        await callback.answer("❌ Игра не найдена!")
        return
    
    game = active_crypto_hacker_games[user_id]
    
    if game['level'] == 1:
        await callback.answer("❌ Сделайте хотя бы один успешный взлом перед выводом!")
        return
    
    if game.get('cashout_used'):
        await callback.answer("❌ Выигрыш уже забран!")
        return
    
    game['cashout_used'] = True
    
    won_amount = int(game['original_bet'] * game['multiplier'] * 0.8)
    add_win_to_user(user_id, won_amount, game['original_bet'])
    add_game_to_history(user_id, 'Крипто-Хакер', game['original_bet'], 'win', won_amount)
    users_data[user_id]['games_played'] += 1
    save_users()
    
    history_text = ""
    for level in range(1, game['level']):
        if level - 1 < len(game['wallet']):
            emoji = "💎" if game['wallet'][level - 1] == 1 else "🦠"
            history_text += f"Уровень {level}: {emoji} {'BTC' if game['wallet'][level - 1] == 1 else 'VIRUS'}\n"
    
    await callback.message.edit_text(
        f"💰 <b>ВЫВОД УСПЕШЕН!</b>\n\n"
        f"💻 Взломанных уровней: <b>{game['level'] - 1}</b>\n"
        f"💰 Исходная ставка: <b>{format_amount(game['original_bet'])} MORPH</b>\n"
        f"📊 Финальный коэффициент: <b>{game['multiplier']:.2f}x</b>\n"
        f"💸 Комиссия за вывод: <b>20%</b>\n"
        f"🎯 Выигрыш: <b>{format_amount(won_amount)} MORPH</b>\n\n"
        f"📊 <b>ИСТОРИЯ ВЗЛОМОВ:</b>\n{history_text}",
        parse_mode='HTML'
    )
    
    del active_crypto_hacker_games[user_id]
    await callback.answer()

# Обработчики для заблокированных и пройденных уровней
@router.callback_query(lambda c: c.data.startswith(('hacker_past_', 'hacker_locked_')))
async def hacker_info_callback(callback: CallbackQuery):
    await callback.answer("❌ Этот уровень уже пройден или заблокирован!")

@router.message(lambda message: message.new_chat_members)
async def handle_new_members(message: types.Message):
    chat_id = message.chat.id
    inviting_user_id = message.from_user.id
    
    # Инициализируем казну чата если её нет
    init_chat_treasury(chat_id)
    
    # Награда за приглашение (берем из настроек казны)
    reward = chat_treasury[chat_id].get('reward_amount', 1000)
    
    for new_member in message.new_chat_members:
        # Не награждаем за добавление ботов
        if new_member.is_bot:
            continue
            
        new_user_id = new_member.id
        
        # Проверяем, не забанен ли новый участник в этом чате
        if is_banned_in_chat(chat_id, new_user_id):
            try:
                # Удаляем забаненного пользователя из группы
                await message.bot.ban_chat_member(chat_id=chat_id, user_id=new_user_id)
                # Удаляем сообщение о входе
                try:
                    await message.delete()
                except:
                    pass
            except Exception as e:
                print(f"Ошибка при удалении забаненного пользователя из группы: {e}")
            continue
        
        # Инициализируем пользователей если их нет
        init_user(inviting_user_id, message.from_user.username)
        init_user(new_user_id, new_member.username)
        
        # ПРОВЕРКА: был ли участник уже в этом чате ранее
        members = chat_treasury[chat_id].get('members', {})
        if str(new_user_id) in members:
            # Участник уже был в чате - пропускаем награду
            continue
        
        # Проверяем, есть ли средства в казне
        if chat_treasury[chat_id]['balance'] >= reward:
            # Выдаем награду пригласившему
            users_data[inviting_user_id]['balance'] += reward
            chat_treasury[chat_id]['balance'] -= reward
            
            # Записываем информацию о участнике (ТОЛЬКО для новых участников)
            chat_treasury[chat_id]['members'][str(new_user_id)] = {
                'invited_by': inviting_user_id,
                'join_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'first_join_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # Дата первого входа
            }
            
            # Сохраняем изменения
            save_users()
            save_chat_treasury()
            
            inviting_user_name = message.from_user.first_name
            new_user_name = new_member.first_name
            
            await message.reply(
                f"🎉 <b>Новый участник!</b>\n\n"
                f"👤 {inviting_user_name} пригласил(а) {new_user_name}\n"
                f"💰 Награда из казны чата: <b>{format_amount(reward)} MORPH</b>\n"
                f"🏦 Остаток в казне: <b>{format_amount(chat_treasury[chat_id]['balance'])} MORPH</b>",
                parse_mode="HTML"
            )
        else:
            await message.reply(
                f"❌ <b>В казне чата недостаточно средств для награды!</b>\n\n"
                f"💡 Пополните казну командой: <code>казну пополнить [сумма]</code>\n"
                f"🏦 Текущий баланс казны: <b>{format_amount(chat_treasury[chat_id]['balance'])} MORPH</b>",
                parse_mode="HTML"
            )

# Показать состояние казны
@router.message(lambda message: message.text and message.text.lower() in ["казна", "казна чата", "treasury"])
async def show_treasury(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    init_chat_treasury(chat_id)
    
    treasury = chat_treasury[chat_id]
    members_count = len(treasury.get('members', {}))
    
    treasury_text = (
        f"🏦 <b>КАЗНА ЧАТА</b>\n\n"
        f"💰 Баланс: <b>{format_amount(treasury['balance'])} MORPH</b>\n"
        f"👥 Участников: <b>{members_count}</b>\n"
        f"📅 Создана: <i>{treasury['created_date']}</i>\n\n"
        f"💡 <b>Команды:</b>\n"
        f"• <code>казну пополнить [сумма]</code> - пополнить казну\n"
        f"• <code>казну статистика</code> - статистика участников\n"
        f"• <code>мой вклад</code> - ваш вклад в казну\n\n"
        f"🎁 <b>За каждого приглашенного участника:</b>\n"
        f"• Пригласивший получает <b>{format_amount(treasury.get('reward_amount', 1000))} MORPH</b> из казны"
    )
    
    await message.reply(treasury_text, parse_mode="HTML")

# Пополнить казну
@router.message(lambda message: message.text and message.text.lower().startswith("казну пополнить"))
async def donate_to_treasury(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("❌ Использование: <b>казну пополнить [сумма]</b>", parse_mode="HTML")
            return
        
        amount = parse_amount(parts[2])
        if amount is None or amount <= 0:
            await message.reply("❌ Сумма должна быть положительной!", parse_mode="HTML")
            return
        
        user_id = message.from_user.id
        chat_id = message.chat.id
        
        init_user(user_id, message.from_user.username)
        init_chat_treasury(chat_id)
        
        if users_data[user_id]['balance'] < amount:
            await message.reply("❌ Недостаточно MORPH на вашем балансе!")
            return
        
        # Переводим средства в казну
        users_data[user_id]['balance'] -= amount
        chat_treasury[chat_id]['balance'] += amount
        
        # Сохраняем историю вкладов
        if 'donations' not in chat_treasury[chat_id]:
            chat_treasury[chat_id]['donations'] = {}
        
        if str(user_id) not in chat_treasury[chat_id]['donations']:
            chat_treasury[chat_id]['donations'][str(user_id)] = 0
        
        chat_treasury[chat_id]['donations'][str(user_id)] += amount
        
        save_users()
        save_chat_treasury()
        
        await message.reply(
            f"✅ <b>Казна чата пополнена!</b>\n\n"
            f"💰 Сумма: <b>{format_amount(amount)} MORPH</b>\n"
            f"👤 От: <b>{message.from_user.first_name}</b>\n"
            f"🏦 Новый баланс казны: <b>{format_amount(chat_treasury[chat_id]['balance'])} MORPH</b>",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.reply("❌ Ошибка при пополнении казны!")

# Статистика казны
@router.message(lambda message: message.text and message.text.lower() in ["казну статистика", "статистика казны"])
async def treasury_stats(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    chat_id = message.chat.id
    init_chat_treasury(chat_id)
    
    treasury = chat_treasury[chat_id]
    donations = treasury.get('donations', {})
    
    if not donations:
        await message.reply("📊 <b>Статистика казны:</b>\n\nЕщё никто не делал взносов!", parse_mode="HTML")
        return
    
    # Сортируем по сумме взносов
    sorted_donors = sorted(donations.items(), key=lambda x: x[1], reverse=True)
    
    stats_text = "🏆 <b>ТОП ВКЛАДЧИКОВ В КАЗНУ</b>\n\n"
    builder = InlineKeyboardBuilder()
    
    for i, (donor_id, amount) in enumerate(sorted_donors[:10], 1):
        donor_name = "Unknown"
        donor_uid = int(donor_id)
        if donor_uid in users_data:
            donor_name = users_data[donor_uid].get('username', f'User{donor_id}')
            # Убираем @ если есть
            if donor_name and isinstance(donor_name, str) and donor_name.startswith('@'):
                donor_name = donor_name[1:]
        
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        stats_text += f"{emoji} <b>{donor_name}</b>: <b>{format_amount(amount)} MORPH</b>\n"
        
        # Добавляем кнопку для перехода в профиль
        builder.button(
            text=f"{emoji} {donor_name}",
            url=f"tg://user?id={donor_uid}"
        )
    
    stats_text += f"\n💰 <b>Общий баланс казны:</b> <b>{format_amount(treasury['balance'])} MORPH</b>"
    stats_text += "\n\n💡 <i>Нажмите на кнопку ниже, чтобы перейти в профиль вкладчика</i>"
    builder.adjust(1)  # По одной кнопке в ряд
    
    await message.reply(
        stats_text, 
        parse_mode="HTML",
        reply_markup=builder.as_markup() if builder.buttons else None
    )

# Мой вклад
@router.message(lambda message: message.text and message.text.lower() in ["мой вклад", "мой вклад в казну"])
async def my_contribution(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    init_chat_treasury(chat_id)
    
    donations = chat_treasury[chat_id].get('donations', {})
    my_donation = donations.get(str(user_id), 0)
    
    contribution_text = (
        f"👤 <b>ВАШ ВКЛАД В КАЗНУ ЧАТА</b>\n\n"
        f"💰 Ваш вклад: <b>{format_amount(my_donation)} MORPH</b>\n"
        f"🏦 Общий баланс казны: <b>{format_amount(chat_treasury[chat_id]['balance'])} MORPH</b>\n"
        f"🎁 Награда за приглашение: <b>{format_amount(chat_treasury[chat_id].get('reward_amount', 1000))} MORPH</b>"
    )
    
    await message.reply(contribution_text, parse_mode="HTML")

# Команда инвентарь с инлайн-кнопками
@router.message(lambda message: message.text and message.text.lower() in ["инвентарь", "инв", "inventory", "inv"])
async def cmd_inventory(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Инициализируем инвентарь если его нет
    if user_id not in user_inventory:
        user_inventory[user_id] = {
            'items': {},
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        save_user_inventory()
    
    inventory = user_inventory[user_id]
    items = inventory.get('items', {})

    if not items:
        inventory_text = (
            "🎒 <b>ВАШ ИНВЕНТАРЬ</b>\n\n"
            "📦 Инвентарь пуст"
        )
        await message.reply(inventory_text, parse_mode="HTML")
        return

    # Сортируем предметы по редкости (легендарные первыми)
    rarity_order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3, 'unknown': 4}
    sorted_items = sorted(items.items(), key=lambda x: rarity_order.get(get_item_info(x[0])['rarity'], 4))

    # Показываем первую страницу
    await show_inventory_page(message, user_id, sorted_items, page=0)

def get_inventory_page(items: list, page: int, items_per_page: int = 10):
    """Возвращает предметы для указанной страницы"""
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    return items[start_idx:end_idx], len(items)

async def show_inventory_page(message_or_query, user_id: int, sorted_items: list, page: int = 0):
    """Показывает страницу инвентаря с инлайн-кнопками"""
    items_per_page = 10
    page_items, total_items = get_inventory_page(sorted_items, page, items_per_page)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    
    if not page_items:
        inventory_text = "🎒 <b>ВАШ ИНВЕНТАРЬ</b>\n\n📦 Инвентарь пуст"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    else:
        inventory_text = f"🎒 <b>ВАШ ИНВЕНТАРЬ</b>\n\n"
        inventory_text += f"📊 <b>Всего предметов:</b> {total_items}\n"
        inventory_text += f"📄 <b>Страница:</b> {page + 1}/{max(1, total_pages)}\n\n"
        
        keyboard_builder = InlineKeyboardBuilder()
        
        # Добавляем кнопки для каждого предмета
        for item_id, count in page_items:
            item_info = get_item_info(item_id)
            item_name = item_info['name']
            item_emoji = item_info['emoji']
            item_rarity = item_info['rarity']
            
            rarity_emoji = {
                'common': '⚪',
                'rare': '🔵',
                'epic': '🟣',
                'legendary': '🟡'
            }
            
            button_text = f"{item_emoji} {rarity_emoji.get(item_rarity, '⚪')} {item_name} (x{count})"
            # Используем base64 для безопасной передачи item_id
            item_data = base64.b64encode(f"{user_id}:{item_id}".encode()).decode()
            keyboard_builder.button(
                text=button_text,
                callback_data=f"inv_item:{item_data}"
            )
        
        keyboard_builder.adjust(1)  # По одной кнопке в ряд
        
        # Кнопки навигации
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"inv_page:{user_id}:{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"inv_page:{user_id}:{page+1}"))
        
        if nav_buttons:
            keyboard_builder.row(*nav_buttons)
        
        keyboard = keyboard_builder.as_markup()
    
    if isinstance(message_or_query, types.Message):
        await message_or_query.reply(inventory_text, reply_markup=keyboard, parse_mode="HTML")
    elif isinstance(message_or_query, types.CallbackQuery):
        await message_or_query.message.edit_text(inventory_text, reply_markup=keyboard, parse_mode="HTML")
        await message_or_query.answer()

# Callback-обработчик для пагинации инвентаря
@router.callback_query(lambda c: c.data and c.data.startswith("inv_page:"))
async def callback_inventory_page(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    # Проверяем, что пользователь вызывает свою команду
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        callback_user_id = int(parts[1])
        if callback_user_id != user_id:
            await callback.answer("❌ Это не ваш инвентарь!", show_alert=True)
            return
        
        page = int(parts[2])
        
        # Загружаем инвентарь
        if user_id not in user_inventory:
            user_inventory[user_id] = {
                'items': {},
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            save_user_inventory()
        
        inventory = user_inventory[user_id]
        items = inventory.get('items', {})
        
        if not items:
            await callback.answer("📦 Инвентарь пуст", show_alert=True)
            return
        
        # Сортируем предметы по редкости
        rarity_order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3, 'unknown': 4}
        sorted_items = sorted(items.items(), key=lambda x: rarity_order.get(get_item_info(x[0])['rarity'], 4))
        
        await show_inventory_page(callback, user_id, sorted_items, page)
    except Exception as e:
        logging.error(f"Ошибка в callback_inventory_page: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка!", show_alert=True)

# Callback-обработчик для просмотра предмета
@router.callback_query(lambda c: c.data and c.data.startswith("inv_item:"))
async def callback_inventory_item(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 2:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        # Декодируем данные
        item_data = base64.b64decode(parts[1]).decode()
        data_parts = item_data.split(":")
        if len(data_parts) != 2:
            await callback.answer("❌ Ошибка данных!", show_alert=True)
            return
        
        callback_user_id = int(data_parts[0])
        item_id = data_parts[1]
        
        # Проверяем, что пользователь вызывает свою команду
        if callback_user_id != user_id:
            await callback.answer("❌ Это не ваш инвентарь!", show_alert=True)
            return
        
        # Проверяем наличие предмета
        if user_id not in user_inventory or item_id not in user_inventory[user_id].get('items', {}):
            await callback.answer("❌ Предмет не найден!", show_alert=True)
            return
        
        item_count = user_inventory[user_id]['items'][item_id]
        item_info = get_item_info(item_id)
        item_name = item_info['name']
        item_emoji = item_info['emoji']
        item_rarity = item_info['rarity']
        item_description = item_info.get('description', 'Описание отсутствует')
        sell_price = item_info['sell_price']
        
        # Эмодзи и названия редкости
        rarity_info = {
            'common': {'emoji': '⚪', 'name': 'ОБЫЧНЫЙ'},
            'rare': {'emoji': '🔵', 'name': 'РЕДКИЙ'},
            'epic': {'emoji': '🟣', 'name': 'ЭПИЧЕСКИЙ'},
            'legendary': {'emoji': '🟡', 'name': 'ЛЕГЕНДАРНЫЙ'}
        }
        
        rarity_data = rarity_info.get(item_rarity, rarity_info['common'])
        total_price = sell_price * item_count
        
        item_text = (
            f"📦 <b>ИНФОРМАЦИЯ О ПРЕДМЕТЕ</b>\n\n"
            f"{item_emoji} <b>{item_name}</b>\n"
            f"{rarity_data['emoji']} <b>Редкость:</b> {rarity_data['name']}\n"
            f"📊 <b>Количество:</b> {item_count} шт.\n\n"
            f"📝 <b>Описание:</b>\n{item_description}\n\n"
            f"💰 <b>Цена продажи:</b> <code>{format_amount(sell_price)} MORPH</code> за шт.\n"
            f"💎 <b>Всего можно получить:</b> <code>{format_amount(total_price)} MORPH</code>"
        )
        
        # Создаем кнопки
        keyboard_builder = InlineKeyboardBuilder()
        
        # Кнопки выбора количества для продажи
        if item_count > 1:
            item_text += f"\n\n💡 <b>Выберите количество для продажи:</b>"
            # Кнопки: 1, 5, 10, Все
            sell_data_base = base64.b64encode(f"{user_id}:{item_id}".encode()).decode()
            keyboard_builder.button(
                text="💰 Продать 1 шт.",
                callback_data=f"inv_sell_qty:{sell_data_base}:1"
            )
            if item_count >= 5:
                keyboard_builder.button(
                    text="💰 Продать 5 шт.",
                    callback_data=f"inv_sell_qty:{sell_data_base}:5"
                )
            if item_count >= 10:
                keyboard_builder.button(
                    text="💰 Продать 10 шт.",
                    callback_data=f"inv_sell_qty:{sell_data_base}:10"
                )
            keyboard_builder.button(
                text=f"💰 Продать все ({item_count} шт.)",
                callback_data=f"inv_sell_qty:{sell_data_base}:{item_count}"
            )
            keyboard_builder.adjust(2)  # По 2 кнопки в ряд
        else:
            # Если предмет один, сразу продаем
            sell_data = base64.b64encode(f"{user_id}:{item_id}".encode()).decode()
            keyboard_builder.button(
                text="💰 Продать",
                callback_data=f"inv_sell_qty:{sell_data}:1"
            )
        
        # Кнопки передачи предметов
        item_text += f"\n\n💡 <b>Передать предмет другому игроку:</b>"
        transfer_data_base = base64.b64encode(f"{user_id}:{item_id}".encode()).decode()
        if item_count > 1:
            keyboard_builder.button(
                text="🎁 Передать 1 шт.",
                callback_data=f"inv_transfer_qty:{transfer_data_base}:1"
            )
            if item_count >= 5:
                keyboard_builder.button(
                    text="🎁 Передать 5 шт.",
                    callback_data=f"inv_transfer_qty:{transfer_data_base}:5"
                )
            if item_count >= 10:
                keyboard_builder.button(
                    text="🎁 Передать 10 шт.",
                    callback_data=f"inv_transfer_qty:{transfer_data_base}:10"
                )
            keyboard_builder.button(
                text=f"🎁 Передать все ({item_count} шт.)",
                callback_data=f"inv_transfer_qty:{transfer_data_base}:{item_count}"
            )
        else:
            keyboard_builder.button(
                text="🎁 Передать",
                callback_data=f"inv_transfer_qty:{transfer_data_base}:1"
            )
        
        # Кнопка отмены (возврат к инвентарю)
        rarity_order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3, 'unknown': 4}
        items = user_inventory[user_id].get('items', {})
        sorted_items = sorted(items.items(), key=lambda x: rarity_order.get(get_item_info(x[0])['rarity'], 4))
        
        # Находим страницу с этим предметом
        page = 0
        for idx, (iid, _) in enumerate(sorted_items):
            if iid == item_id:
                page = idx // 10
                break
        
        keyboard_builder.button(
            text="◀️ Назад к инвентарю",
            callback_data=f"inv_page:{user_id}:{page}"
        )
        
        keyboard_builder.adjust(2, 2, 1)  # По 2 кнопки в ряд, последняя отдельно
        keyboard = keyboard_builder.as_markup()
        
        await callback.message.edit_text(item_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Ошибка в callback_inventory_item: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка!", show_alert=True)

# Callback-обработчик для продажи предмета
@router.callback_query(lambda c: c.data and c.data.startswith("inv_sell_qty:"))
async def callback_sell_item(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        # Декодируем данные
        item_data = base64.b64decode(parts[1]).decode()
        data_parts = item_data.split(":")
        if len(data_parts) != 2:
            await callback.answer("❌ Ошибка данных!", show_alert=True)
            return
        
        callback_user_id = int(data_parts[0])
        item_id = data_parts[1]
        sell_count = int(parts[2])
        
        # Проверяем, что пользователь вызывает свою команду
        if callback_user_id != user_id:
            await callback.answer("❌ Это не ваш инвентарь!", show_alert=True)
            return
        
        # Проверяем наличие предмета
        if user_id not in user_inventory or item_id not in user_inventory[user_id].get('items', {}):
            await callback.answer("❌ Предмет не найден!", show_alert=True)
            return
        
        item_count = user_inventory[user_id]['items'][item_id]
        
        # Проверяем количество
        if sell_count > item_count:
            await callback.answer("❌ Недостаточно предметов!", show_alert=True)
            return
        
        if sell_count <= 0:
            await callback.answer("❌ Неверное количество!", show_alert=True)
            return
        
        item_info = get_item_info(item_id)
        item_name = item_info['name']
        sell_price = item_info['sell_price']
        
        # Продаем предмет
        total_price = sell_price * sell_count
        users_data[user_id]['balance'] += total_price
        save_users()
        
        # Удаляем предмет из инвентаря
        if sell_count >= item_count:
            # Продаем все
            del user_inventory[user_id]['items'][item_id]
        else:
            # Продаем часть
            user_inventory[user_id]['items'][item_id] -= sell_count
        
        user_inventory[user_id]['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_user_inventory()
        
        sell_text = (
            f"✅ <b>ПРЕДМЕТ ПРОДАН!</b>\n\n"
            f"📦 Предмет: <b>{item_name}</b>\n"
            f"📊 Количество: <b>{sell_count} шт.</b>\n"
            f"💰 Получено: <b>{format_amount(total_price)} MORPH</b>\n\n"
            f"💳 <b>Ваш баланс:</b> <code>{format_amount(users_data[user_id]['balance'])} MORPH</code>"
        )
        
        # Кнопка возврата к инвентарю
        keyboard_builder = InlineKeyboardBuilder()
        keyboard_builder.button(
            text="◀️ Вернуться к инвентарю",
            callback_data=f"inv_page:{user_id}:0"
        )
        keyboard = keyboard_builder.as_markup()
        
        await callback.message.edit_text(sell_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer("✅ Предмет продан!", show_alert=True)
        
    except Exception as e:
        logging.error(f"Ошибка в callback_sell_item: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка при продаже!", show_alert=True)

# Callback-обработчик для передачи предметов
@router.callback_query(lambda c: c.data and c.data.startswith("inv_transfer_qty:"))
async def callback_transfer_item(callback: types.CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        # Декодируем данные
        item_data = base64.b64decode(parts[1]).decode()
        data_parts = item_data.split(":")
        if len(data_parts) != 2:
            await callback.answer("❌ Ошибка данных!", show_alert=True)
            return
        
        callback_user_id = int(data_parts[0])
        item_id = data_parts[1]
        transfer_count = int(parts[2])
        
        # Проверяем, что пользователь вызывает свою команду
        if callback_user_id != user_id:
            await callback.answer("❌ Это не ваш инвентарь!", show_alert=True)
            return
        
        # Проверяем наличие предмета
        if user_id not in user_inventory or item_id not in user_inventory[user_id].get('items', {}):
            await callback.answer("❌ Предмет не найден!", show_alert=True)
            return
        
        item_count = user_inventory[user_id]['items'][item_id]
        
        # Проверяем количество
        if transfer_count > item_count:
            await callback.answer("❌ Недостаточно предметов!", show_alert=True)
            return
        
        if transfer_count <= 0:
            await callback.answer("❌ Неверное количество!", show_alert=True)
            return
        
        item_info = get_item_info(item_id)
        item_name = item_info['name']
        item_emoji = item_info['emoji']
        
        # Сохраняем данные о передаче во временное хранилище
        # Просим пользователя ответить на сообщение получателя
        transfer_text = (
            f"🎁 <b>ПЕРЕДАЧА ПРЕДМЕТА</b>\n\n"
            f"{item_emoji} <b>{item_name}</b>\n"
            f"📊 Количество: <b>{transfer_count} шт.</b>\n\n"
            f"💡 <b>Как передать:</b>\n"
            f"1. Ответьте на сообщение игрока, которому хотите передать предмет\n"
            f"2. Напишите команду: <code>передать</code>\n\n"
            f"⚠️ Или используйте команду:\n"
            f"<code>передать [ID игрока]</code>\n\n"
            f"⏱️ У вас есть 5 минут для передачи предмета."
        )
        
        # Сохраняем данные о передаче
        pending_transfers[user_id] = {
            'item_id': item_id,
            'count': transfer_count,
            'timestamp': time.time(),
            'item_name': item_name,
            'item_emoji': item_emoji
        }
        
        keyboard_builder = InlineKeyboardBuilder()
        keyboard_builder.button(
            text="❌ Отменить",
            callback_data=f"inv_transfer_cancel:{user_id}"
        )
        keyboard = keyboard_builder.as_markup()
        
        await callback.message.edit_text(transfer_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer("💡 Ответьте на сообщение получателя и напишите 'передать'")
        
    except Exception as e:
        logging.error(f"Ошибка в callback_transfer_item: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка!", show_alert=True)

# Callback для отмены передачи
@router.callback_query(lambda c: c.data and c.data.startswith("inv_transfer_cancel:"))
async def callback_transfer_cancel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 2:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        cancel_user_id = int(parts[1])
        
        if cancel_user_id != user_id:
            await callback.answer("❌ Это не ваша передача!", show_alert=True)
            return
        
        if user_id in pending_transfers:
            del pending_transfers[user_id]
        
        await callback.answer("❌ Передача отменена", show_alert=True)
        
        # Возвращаем к инвентарю
        if user_id not in user_inventory:
            user_inventory[user_id] = {
                'items': {},
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            save_user_inventory()
        
        items = user_inventory[user_id].get('items', {})
        if not items:
            await callback.message.edit_text("🎒 <b>ВАШ ИНВЕНТАРЬ</b>\n\n📦 Инвентарь пуст", parse_mode="HTML")
            return
        
        rarity_order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3, 'unknown': 4}
        sorted_items = sorted(items.items(), key=lambda x: rarity_order.get(get_item_info(x[0])['rarity'], 4))
        await show_inventory_page(callback, user_id, sorted_items, page=0)
        
    except Exception as e:
        logging.error(f"Ошибка в callback_transfer_cancel: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка!", show_alert=True)

# Команда для передачи предметов
@router.message(lambda message: message.text and message.text.lower() in ["передать", "transfer", "дать предмет"])
async def cmd_transfer_item(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    if user_id not in pending_transfers:
        await message.reply(
            "❌ <b>Нет активной передачи!</b>\n\n"
            "💡 Выберите предмет в инвентаре и нажмите 'Передать'",
            parse_mode="HTML"
        )
        return
    
    transfer_data = pending_transfers[user_id]
    
    # Проверяем время (5 минут)
    if time.time() - transfer_data['timestamp'] > 300:
        del pending_transfers[user_id]
        await message.reply("❌ Время передачи истекло! Выберите предмет заново.")
        return
    
    # Определяем получателя
    recipient_id = None
    
    # Если есть ответ на сообщение
    if message.reply_to_message:
        recipient_id = message.reply_to_message.from_user.id
    else:
        # Пытаемся найти ID в тексте команды
        parts = message.text.split()
        if len(parts) >= 2:
            try:
                recipient_id = int(parts[1])
            except ValueError:
                pass
    
    if not recipient_id:
        await message.reply(
            "❌ <b>Не указан получатель!</b>\n\n"
            "💡 <b>Способы передачи:</b>\n"
            "1. Ответьте на сообщение игрока и напишите <code>передать</code>\n"
            "2. Напишите <code>передать [ID игрока]</code>\n\n"
            "💡 Чтобы узнать ID игрока, попросите его написать <code>/start</code>",
            parse_mode="HTML"
        )
        return
    
    if recipient_id == user_id:
        await message.reply("❌ Нельзя передать предмет самому себе!")
        return
    
    if is_banned(recipient_id):
        await message.reply("❌ Этот игрок забанен!")
        return
    
    # Инициализируем получателя
    init_user(recipient_id, None)
    
    item_id = transfer_data['item_id']
    transfer_count = transfer_data['count']
    item_name = transfer_data['item_name']
    item_emoji = transfer_data['item_emoji']
    
    # Проверяем наличие предмета у отправителя
    if user_id not in user_inventory or item_id not in user_inventory[user_id].get('items', {}):
        del pending_transfers[user_id]
        await message.reply("❌ Предмет не найден в вашем инвентаре!")
        return
    
    item_count = user_inventory[user_id]['items'][item_id]
    
    if transfer_count > item_count:
        del pending_transfers[user_id]
        await message.reply("❌ Недостаточно предметов!")
        return
    
    # Передаем предмет
    try:
        # Убираем у отправителя
        if transfer_count >= item_count:
            del user_inventory[user_id]['items'][item_id]
        else:
            user_inventory[user_id]['items'][item_id] -= transfer_count
        
        user_inventory[user_id]['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_user_inventory()
        
        # Добавляем получателю
        add_item_to_inventory(recipient_id, item_id, transfer_count)
        
        # Удаляем из pending_transfers
        del pending_transfers[user_id]
        
        # Уведомляем отправителя
        sender_text = (
            f"✅ <b>ПРЕДМЕТ ПЕРЕДАН!</b>\n\n"
            f"{item_emoji} <b>{item_name}</b>\n"
            f"📊 Количество: <b>{transfer_count} шт.</b>\n"
            f"👤 Получатель: <b>ID {recipient_id}</b>"
        )
        await message.reply(sender_text, parse_mode="HTML")
        
        # Уведомляем получателя
        try:
            recipient_text = (
                f"🎁 <b>ВЫ ПОЛУЧИЛИ ПРЕДМЕТ!</b>\n\n"
                f"{item_emoji} <b>{item_name}</b>\n"
                f"📊 Количество: <b>{transfer_count} шт.</b>\n"
                f"👤 От: <b>ID {user_id}</b>\n\n"
                f"💡 Предмет добавлен в ваш инвентарь!"
            )
            await bot.send_message(recipient_id, recipient_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение получателю {recipient_id}: {e}")
        
    except Exception as e:
        logging.error(f"Ошибка при передаче предмета: {e}", exc_info=True)
        await message.reply("❌ Произошла ошибка при передаче предмета!")

# Команда коллекция
@router.message(lambda message: message.text and message.text.lower() in ["коллекция", "моя коллекция", "collection", "my collection"])
async def cmd_collection(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Инициализируем коллекцию если её нет
    if user_id not in user_collection:
        user_collection[user_id] = {
            'items': [],
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        save_user_collection()
    
    collection = user_collection[user_id]
    items = collection.get('items', [])
    
    if not items:
        collection_text = (
            "📚 <b>ВАША КОЛЛЕКЦИЯ</b>\n\n"
            "📖 Коллекция пуста\n\n"
            "💡 В будущем здесь будут появляться коллекционные предметы, карточки и другие уникальные вещи!"
        )
    else:
        collection_text = "📚 <b>ВАША КОЛЛЕКЦИЯ</b>\n\n"
        unique_items = len(set(items))  # Количество уникальных предметов
        total_items = len(items)  # Общее количество предметов
        
        collection_text += f"📊 <b>Уникальных предметов:</b> {unique_items}\n"
        collection_text += f"📦 <b>Всего предметов:</b> {total_items}\n\n"
        
        # Показываем уникальные предметы
        unique_items_list = list(set(items))
        
        # Сортируем по редкости
        rarity_order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3, 'unknown': 4}
        sorted_items = sorted(unique_items_list, key=lambda x: rarity_order.get(get_item_info(x)['rarity'], 4))
        
        if len(sorted_items) <= 20:  # Показываем до 20 предметов
            collection_text += "<b>📋 Предметы в коллекции:</b>\n"
            for item_id in sorted_items:
                count = items.count(item_id)
                item_info = get_item_info(item_id)
                item_name = item_info['name']
                item_emoji = item_info['emoji']
                item_rarity = item_info['rarity']
                
                # Эмодзи редкости
                rarity_emoji = {
                    'common': '⚪',
                    'rare': '🔵',
                    'epic': '🟣',
                    'legendary': '🟡'
                }
                
                if count > 1:
                    collection_text += f"{item_emoji} {rarity_emoji.get(item_rarity, '⚪')} <b>{item_name}</b> (x{count})\n"
                else:
                    collection_text += f"{item_emoji} {rarity_emoji.get(item_rarity, '⚪')} <b>{item_name}</b>\n"
        else:
            collection_text += f"📋 <b>Предметов в коллекции:</b> {unique_items} (показаны первые 20)\n"
            for item_id in sorted_items[:20]:
                count = items.count(item_id)
                item_info = get_item_info(item_id)
                item_name = item_info['name']
                item_emoji = item_info['emoji']
                item_rarity = item_info['rarity']
                
                rarity_emoji = {
                    'common': '⚪',
                    'rare': '🔵',
                    'epic': '🟣',
                    'legendary': '🟡'
                }
                
                if count > 1:
                    collection_text += f"{item_emoji} {rarity_emoji.get(item_rarity, '⚪')} <b>{item_name}</b> (x{count})\n"
                else:
                    collection_text += f"{item_emoji} {rarity_emoji.get(item_rarity, '⚪')} <b>{item_name}</b>\n"
        
        last_updated = collection.get('last_updated', 'Неизвестно')
        collection_text += f"\n🕐 <b>Обновлено:</b> {last_updated}"
    
    await message.reply(collection_text, parse_mode="HTML")

# ========== СИСТЕМА КЕЙСОВ С ТЕМАТИКОЙ ХАТСУНЕ МИКУ ==========

# Словарь предметов: {item_id: {'name': название, 'sell_price': цена продажи, 'rarity': редкость, 'emoji': эмодзи}}
ITEMS_DATABASE = {
    # Легендарные предметы (самые редкие)
    'miku_figure': {
        'name': 'Фигурка Хатсуне Мику',
        'sell_price': 500000,
        'rarity': 'legendary',
        'emoji': '🎀',
        'description': 'Эксклюзивная коллекционная фигурка вокалоида Хатсуне Мику'
    },
    'miku_voice_box': {
        'name': 'Голосовой модуль Мику',
        'sell_price': 300000,
        'rarity': 'legendary',
        'emoji': '🎤',
        'description': 'Уникальный голосовой модуль с голосом Мику'
    },
    
    # Эпические предметы
    'miku_costume': {
        'name': 'Костюм Хатсуне Мику',
        'sell_price': 150000,
        'rarity': 'epic',
        'emoji': '👗',
        'description': 'Официальный костюм вокалоида'
    },
    'miku_wig': {
        'name': 'Парик Мику (бирюзовый)',
        'sell_price': 100000,
        'rarity': 'epic',
        'emoji': '💚',
        'description': 'Бирюзовый парик с двойными хвостиками'
    },
    'vocaloid_microphone': {
        'name': 'Микрофон Vocaloid',
        'sell_price': 120000,
        'rarity': 'epic',
        'emoji': '🎙️',
        'description': 'Профессиональный микрофон для вокалоидов'
    },
    'miku_keyboard': {
        'name': 'Клавиатура Мику',
        'sell_price': 80000,
        'rarity': 'epic',
        'emoji': '⌨️',
        'description': 'Механическая клавиатура с тематикой Мику'
    },
    
    # Редкие предметы
    'miku_poster': {
        'name': 'Постер Хатсуне Мику',
        'sell_price': 50000,
        'rarity': 'rare',
        'emoji': '🖼️',
        'description': 'Официальный постер вокалоида'
    },
    'leek': {
        'name': 'Лук-порей (символ Мику)',
        'sell_price': 30000,
        'rarity': 'rare',
        'emoji': '🥬',
        'description': 'Легендарный лук-порей - символ Мику'
    },
    'miku_badge': {
        'name': 'Значок Мику',
        'sell_price': 25000,
        'rarity': 'rare',
        'emoji': '🎖️',
        'description': 'Коллекционный значок с изображением Мику'
    },
    'vocaloid_cd': {
        'name': 'CD с песнями Мику',
        'sell_price': 40000,
        'rarity': 'rare',
        'emoji': '💿',
        'description': 'Официальный альбом с песнями вокалоида'
    },
    'miku_sticker': {
        'name': 'Стикерпак Мику',
        'sell_price': 35000,
        'rarity': 'rare',
        'emoji': '📱',
        'description': 'Набор стикеров с Мику'
    },
    
    # Обычные предметы
    'miku_keychain': {
        'name': 'Брелок Мику',
        'sell_price': 15000,
        'rarity': 'common',
        'emoji': '🔑',
        'description': 'Миниатюрный брелок с фигуркой Мику'
    },
    'miku_phone_case': {
        'name': 'Чехол для телефона Мику',
        'sell_price': 12000,
        'rarity': 'common',
        'emoji': '📱',
        'description': 'Чехол с принтом Хатсуне Мику'
    },
    'miku_pen': {
        'name': 'Ручка Мику',
        'sell_price': 8000,
        'rarity': 'common',
        'emoji': '✏️',
        'description': 'Ручка с тематикой вокалоида'
    },
    'miku_notebook': {
        'name': 'Тетрадь Мику',
        'sell_price': 10000,
        'rarity': 'common',
        'emoji': '📔',
        'description': 'Тетрадь с обложкой Мику'
    },
    'miku_magnet': {
        'name': 'Магнит Мику',
        'sell_price': 5000,
        'rarity': 'common',
        'emoji': '🧲',
        'description': 'Магнит на холодильник с Мику'
    },
    'miku_pin': {
        'name': 'Брошь Мику',
        'sell_price': 6000,
        'rarity': 'common',
        'emoji': '📌',
        'description': 'Небольшая брошь с изображением Мику'
    }
}

# Хелперы для работы с предметами и инвентарем
def get_item_info(item_id: str) -> Dict:
    """Возвращает информацию о предмете из ITEMS_DATABASE или дефолтную структуру."""
    info = ITEMS_DATABASE.get(item_id)
    if not info:
        return {
            'name': item_id,
            'sell_price': 0,
            'rarity': 'unknown',
            'emoji': '❔',
            'description': ''
        }
    return info

def add_item_to_inventory(user_id: int, item_id: str, count: int = 1):
    """Добавляет предмет в инвентарь пользователя и сохраняет инвентарь."""
    if user_id not in user_inventory:
        user_inventory[user_id] = {'items': {}, 'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    items = user_inventory[user_id].setdefault('items', {})
    items[item_id] = items.get(item_id, 0) + count
    user_inventory[user_id]['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        save_user_inventory()
    except Exception:
        logging.exception('Не удалось сохранить инвентарь после добавления предмета')

# Команда для продажи предметов (должна быть после продажи акций, но с более специфичной проверкой)
@router.message(lambda message: message.text and message.text.lower().startswith('продать ') and len(message.text.split()) == 2)
async def cmd_sell_item(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Инициализируем инвентарь если его нет
    if user_id not in user_inventory:
        user_inventory[user_id] = {
            'items': {},
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        save_user_inventory()
    
    parts = message.text.split(' ', 1)
    if len(parts) < 2:
        await message.reply(
            "❌ <b>Использование:</b> <code>продать [название предмета]</code>\n\n"
            "💡 <b>Пример:</b> <code>продать фигурка хатсуне мику</code>\n"
            "💡 <b>Пример:</b> <code>продать лук-порей</code>\n\n"
            "📦 Используйте <code>инвентарь</code> для просмотра ваших предметов",
            parse_mode="HTML"
        )
        return
    
    item_query = parts[1].lower()
    inventory = user_inventory[user_id]
    items = inventory.get('items', {})
    
    # Ищем предмет по названию
    found_item_id = None
    for item_id in items.keys():
        item_info = get_item_info(item_id)
        item_name_lower = item_info['name'].lower()
        if item_query in item_name_lower or item_name_lower in item_query:
            found_item_id = item_id
            break
    
    if not found_item_id or found_item_id not in items or items[found_item_id] <= 0:
        await message.reply(
            "❌ Предмет не найден в вашем инвентаре!\n\n"
            "💡 Используйте <code>инвентарь</code> для просмотра ваших предметов",
            parse_mode="HTML"
        )
        return
    
    item_info = get_item_info(found_item_id)
    item_name = item_info['name']
    sell_price = item_info['sell_price']
    item_count = items[found_item_id]
    
    # Продаем предмет
    total_price = sell_price * item_count
    users_data[user_id]['balance'] += total_price
    save_users()
    
    # Удаляем предмет из инвентаря
    del user_inventory[user_id]['items'][found_item_id]
    user_inventory[user_id]['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_user_inventory()
    
    sell_text = f"✅ <b>ПРЕДМЕТ ПРОДАН!</b>\n\n"
    sell_text += f"📦 Предмет: <b>{item_name}</b>\n"
    if item_count > 1:
        sell_text += f"📊 Количество: <b>{item_count} шт.</b>\n"
    sell_text += f"💰 Получено: <b>{format_amount(total_price)} MORPH</b>\n\n"
    sell_text += f"💵 Ваш баланс: <b>{format_amount(users_data[user_id]['balance'])} MORPH</b>"
    
    await message.reply(sell_text, parse_mode="HTML")

# Обновляем команду инвентаря для отображения названий предметов
# Команда для изменения награды в казне (только для создателя бота)
@router.message(lambda message: message.text and message.text.lower().startswith("казну награда"))
async def set_treasury_reward(message: types.Message):
    if is_banned(message.from_user.id):
        return

    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Убеждаемся, что создатель чата известен, чтобы корректно определить владельца
    await ensure_creator_set(chat_id, user_id, message.bot)

    is_global_creator = user_id in CREATOR_IDS
    is_chat_owner = can_manage_mods(chat_id, user_id)
    if not (is_global_creator or is_chat_owner):
        await message.reply("⛔ Изменять награду может только владелец чата или создатель бота!")
        return

    # Инициализируем казну если её нет
    init_chat_treasury(chat_id)

    # Парсим команду: "казну награда [сумма]"
    parts = message.text.split()
    if len(parts) < 3:
        current_reward = chat_treasury[chat_id].get('reward_amount', 1000)
        limit_hint = "100-2000" if is_chat_owner and not is_global_creator else "любое значение"
        await message.reply(
            f"❌ <b>Использование:</b> <code>казну награда [сумма]</code>\n\n"
            f"💡 <b>Пример:</b> <code>казну награда 1500</code>\n"
            f"🧭 <b>Доступный диапазон:</b> {limit_hint}\n\n"
            f"🎁 <b>Текущая награда:</b> <b>{format_amount(current_reward)} MORPH</b>",
            parse_mode="HTML"
        )
        return

    try:
        new_reward = int(parts[2])
    except ValueError:
        await message.reply("❌ Неверный формат суммы! Используйте только числа.")
        return

    if is_global_creator:
        if new_reward < 0:
            await message.reply("❌ Награда не может быть отрицательной!")
            return
    else:
        # Владелец чата: лимит 100-2000 MORPH
        if not (100 <= new_reward <= 2000):
            await message.reply("❌ Владелец чата может устанавливать награду только в диапазоне 100-2000 MORPH!")
            return

    # Сохраняем новую награду
    old_reward = chat_treasury[chat_id].get('reward_amount', 1000)
    chat_treasury[chat_id]['reward_amount'] = new_reward
    save_chat_treasury()

    await message.reply(
        f"✅ <b>Награда в казне изменена!</b>\n\n"
        f"🎁 Старая награда: <b>{format_amount(old_reward)} MORPH</b>\n"
        f"🎁 Новая награда: <b>{format_amount(new_reward)} MORPH</b>\n\n"
        f"💡 Теперь за каждого приглашенного участника пригласивший будет получать <b>{format_amount(new_reward)} MORPH</b> из казны.",
        parse_mode="HTML"
    )

# --- КОМАНДА ИГРОКИ (С ЭМОДЗИ) ---
@router.message(lambda message: message.text and message.text.lower() in ["игроки", "players"])
async def cmd_players(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "players"):
        return
    
    total_players = len(users_data)
    active_players = len([uid for uid, data in users_data.items() 
                         if isinstance(uid, int) and data.get('balance', 0) > 0])
    
    players_text = (
        f"📊 <b>СТАТИСТИКА ИГРОКОВ</b>\n\n"
        f"🔹 Всего игроков: <b>{format_amount(total_players)}</b>\n"
        f"🔸 Активных: <b>{format_amount(active_players)}</b>"
    )
    
    await message.reply(players_text, parse_mode="HTML")
#ХИЛО
# словарь 
hilo_games = {}

def create_deck():
    suits = ['❤️', '♦️', '♣️', '♠️']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    deck = [(rank, suit) for suit in suits for rank in ranks]
    random.shuffle(deck)
    return deck

def deal_card(deck):
    return deck.pop() if deck else None

def card_value(card):
    rank, _ = card
    if rank in ['J', 'Q', 'K']:
        return 10
    elif rank == 'A':
        return 11
    else:
        return int(rank)

def card_to_string(card):
    rank, suit = card
    return f"{rank}{suit}"

def calculate_multipliers(current_card):
    """
    Расчёт коэффициентов (x2.4...7.9 и x1.9...4.5) в зависимости от номинала карты.
    """
    current_value = card_value(current_card)
    if current_value is None:
        return None, None

    higher_cards_count = 13 - current_value
    lower_cards_count = current_value - 1
    total_cards_count = 12  # упрощённо по номиналам (2..A)

    probability_higher = higher_cards_count / total_cards_count
    probability_lower = lower_cards_count / total_cards_count

    def calc(prob, min_mult, max_mult):
        inv_prob = 1 - prob
        return round(inv_prob * (max_mult - min_mult) + min_mult, 2)

    multiplier_higher = calc(probability_higher, 1.2, 1.1)
    multiplier_lower = calc(probability_lower, 1.1, 1.3)
    return multiplier_higher, multiplier_lower


class HiLoGame:
    """
    Класс для хранения состояния игры HiLo.
    """
    def __init__(self, user_id, stake):
        self.user_id = user_id
        self.stake = stake
        self.deck = create_deck()
        self.current_card = deal_card(self.deck)
        self.multiplier = 1.0
        self.total_win = 0
        self.can_take = False
        self.message_id = None

    def next_round(self):
        self.current_card = deal_card(self.deck)
        return bool(self.current_card)

# --- вспомогательные функции ---

async def is_command_allowed(user_id):
    """
    Здесь можно проверить, разрешено ли пользователю играть.
    По умолчанию всегда True.
    """
    return True

def format_stake(stake_str):
    """
    Преобразуем строку со ставкой в целое число.
    Допускаем варианты: '100', '1к', '1кк', 'все'.
    """
    try:
        stake_str = stake_str.lower()
        if stake_str == "все":
            return stake_str
        if stake_str.endswith("кк"):
            return int(float(stake_str[:-2]) * 1_000_000)
        elif stake_str.endswith("к"):
            return int(float(stake_str[:-1]) * 1_000)
        else:
            return int(stake_str)
    except Exception as e:
        logging.error(f"Ошибка при форматировании ставки: {e}")
        return None

async def get_user_balance(user_id):
    cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

async def update_user_balance(user_id, amount):
    """
    Обновить баланс пользователя на amount (может быть + или -).
    """
    cursor.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (amount, user_id)
    )
    connection.commit()

# --- основные хендлеры ---

@dp.message_handler(Text(startswith="хило", ignore_case=True))
async def hilo_command(message: types.Message):
    """
    Начало игры "Хило". Пример: "хило 100" или "хило все".
    """
    user_id = message.from_user.id

    # проверяем, может ли пользователь играть
    if not await is_command_allowed(user_id):
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("❌ Ошибка. Используйте: хило {ставка}")
        return

    stake_str = parts[1]
    stake = format_stake(stake_str)
    if stake is None or (isinstance(stake, int) and stake <= 0):
        await message.reply("❌ | Неправильно введена сумма.")
        return

    balance = await get_user_balance(user_id)
    if stake_str.lower() == 'все':
        stake = balance

    if stake > balance:
        await message.reply("Недостаточно средств на балансе.")
        return

    # создаём игру, списываем ставку
    game = HiLoGame(user_id, stake)
    hilo_games[user_id] = game
    await update_user_balance(user_id, -int(stake))

    # отправляем первое сообщение с картой
    await send_hilo_message(message, game, first_game=True)


@dp.callback_query_handler(Text(startswith="hilo_", ignore_case=True))
async def hilo_callback_handler(callback_query: types.CallbackQuery):
    """
    Обработка нажатий кнопок:
    - hilo_higher:12345
    - hilo_lower:12345
    - hilo_take:12345
    - hilo_cancel:12345
    """
    data_parts = callback_query.data.split(":")
    if len(data_parts) < 2:
        await callback_query.answer("Некорректные данные.")
        return

    # извлекаем user_id из callback_data
    try:
        user_id = int(data_parts[1])
    except ValueError:
        await callback_query.answer("Некорректные данные.")
        return

    action = data_parts[0].split("_")[1]  # higher, lower, take, cancel

    # проверка, что это игра того же пользователя
    if user_id != callback_query.from_user.id:
        await callback_query.answer("Это не ваша игра!", show_alert=True)
        return

    # есть ли игра
    if user_id not in hilo_games:
        await callback_query.answer("Игра не найдена.")
        return

    game = hilo_games[user_id]

    if action in ("higher", "lower"):
        await process_hilo_round(callback_query, game, action)
    elif action == "take":
        await process_hilo_take(callback_query, game)
    elif action == "cancel":
        await process_hilo_cancel(callback_query, game)


async def send_hilo_message(message: types.Message, game: HiLoGame, result_text=None, first_game=False):
    """
    Отправляем (или редактируем) сообщение с текущей картой,
    инлайн-кнопками "Выше/Ниже/Забрать/Отмена".
    """
    user_id = game.user_id
    current_card = game.current_card
    higher_multiplier, lower_multiplier = calculate_multipliers(current_card)

    # Формируем инлайн-клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            f"⬆️ Выше x{higher_multiplier:.2f}",
            callback_data=f"hilo_higher:{user_id}"
        ),
        types.InlineKeyboardButton(
            f"⬇️ Ниже x{lower_multiplier:.2f}",
            callback_data=f"hilo_lower:{user_id}"
        )
    )
    if game.can_take:
        keyboard.add(
            types.InlineKeyboardButton(
                "💰 Забрать",
                callback_data=f"hilo_take:{user_id}"
            )
        )
    #keyboard.add(
       # types.InlineKeyboardButton(
        #    "❌ Отмена",
       #     callback_data=f"hilo_cancel:{user_id}"
       # )
   # )

    text = result_text or (
        f"🃏 Выпавшая карта: {card_to_string(current_card)}\n"
        f"\n💰 Ваша ставка: {int(game.stake)} сапфиров\n"
        f"\nСделайте выбор: будет ли следующая карта выше или ниже!"
    )
    if first_game:
        text = "♦️ Вы начали игру в HiLo! ♦️\n" + text

    # если сообщение уже есть, редактируем
    if game.message_id:
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=game.message_id,
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения HiLo: {e}")
    else:
        # иначе отправляем новое
        sent_message = await message.reply(text, reply_markup=keyboard)
        game.message_id = sent_message.message_id


async def process_hilo_round(callback_query: types.CallbackQuery, game: HiLoGame, action: str):
    """
    Обработка нажатия "Выше"/"Ниже".
    """
    user_id = game.user_id
    stake = game.stake
    current_card = game.current_card

    higher_multiplier, lower_multiplier = calculate_multipliers(current_card)

    new_card = deal_card(game.deck)
    if not new_card:
        await callback_query.answer("В колоде больше нет карт!")
        del hilo_games[user_id]
        return

    current_value = card_value(current_card)
    new_value = card_value(new_card)

    win = False
    if action == "higher" and new_value > current_value:
        win = True
        game.total_win += int(stake * higher_multiplier)
    elif action == "lower" and new_value < current_value:
        win = True
        game.total_win += int(stake * lower_multiplier)

    if win:
        # угадал
        result_text = (
            f"Вы угадали! ✨\n\nНовая карта: {card_to_string(new_card)}.\n"
            f"\nТекущий выигрыш: {int(game.total_win)} сапфиров"
        )
        game.current_card = new_card
        game.can_take = True
        await send_hilo_message(callback_query.message, game, result_text)
    else:
        # проиграл
        win_text = "Вы проиграли. 😭"
        del hilo_games[user_id]
        result_text = (
            f"Игра завершена!\nВыпавшая карта: {card_to_string(new_card)}.\n"
            f"{win_text} Повезет в следующий раз."
        )
        # баланс не возвращаем, ставка уже списана
        try:
            await callback_query.message.bot.edit_message_text(
                result_text,
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                reply_markup=None
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании сообщения: {e}")

    await callback_query.answer()


async def process_hilo_take(callback_query: types.CallbackQuery, game: HiLoGame):
    """
    Обработка кнопки "Забрать".
    """
    user_id = game.user_id
    total_win = game.total_win

    # возвращаем выигрыш на баланс
    await update_user_balance(user_id, int(total_win))
    del hilo_games[user_id]

    win_text = "✅ Вы забрали "
    try:
        await callback_query.message.bot.edit_message_text(
            f"{win_text} выигрыш: {int(total_win)} сапфиров в хило!",
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            reply_markup=None
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения: {e}")

    await callback_query.answer()


async def process_hilo_cancel(callback_query: types.CallbackQuery, game: HiLoGame):
    """
    Обработка кнопки "Отмена" — возвращаем ставку и завершаем игру.
    """
    user_id = game.user_id
    stake = game.stake

    # возвращаем ставку
    await update_user_balance(user_id, int(stake))
    del hilo_games[user_id]

    cancel_text = "ℹ️ Игра в Хило отменена. Ваша ставка возвращена на баланс."
    try:
        await callback_query.message.bot.edit_message_text(
            cancel_text,
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            reply_markup=None
        )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения: {e}")

    await callback_query.answer()

# --- Кнопка "Назад" в помощи ---
@router.callback_query(lambda c: c.data == "help_back")
async def help_back(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Игры", callback_data="help_games"))
    builder.add(InlineKeyboardButton(text="📋 Основное", callback_data="help_main"))
    # Кнопка для админа
    if callback.from_user.id in ADMIN_IDS:
        builder.add(InlineKeyboardButton(text="🛡️ Админ команды", callback_data="help_admin"))
    await callback.message.edit_text("<b>❓ Выберите раздел помощи:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

# Команда баланс
@router.message(lambda message: message.text and message.text.lower() in ["баланс", "б", "balance"])
async def cmd_balance(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "balance"):
        return
    init_user(user_id, message.from_user.username)
    balance = users_data[user_id]['balance']
    await message.reply(f"💰 Ваш баланс: <b>{format_amount(balance)} MORPH</b>", parse_mode="HTML")

# --- Профиль ---
@router.message(lambda message: message.text and message.text.lower() in ["установить аватар", "set avatar", "аватар"])
async def cmd_set_avatar(message: types.Message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        return
    
    # Проверяем тип медиа
    if message.photo:
        # Обычное фото - доступно всем
        avatar_file_id = message.photo[-1].file_id
        avatar_type = 'photo'
    elif message.video and is_vip(user_id):
        # Видео - только для VIP
        avatar_file_id = message.video.file_id
        avatar_type = 'video'
    elif message.animation and is_vip(user_id):
        # GIF - только для VIP
        avatar_file_id = message.animation.file_id
        avatar_type = 'animation'
    else:
        if message.video or message.animation:
            await message.answer(
                "❌ Видео и GIF доступны только для VIP пользователей!\n\n"
                "💡 Обратитесь к администратору для получения VIP подписки."
            )
        else:
            await message.answer("📷 Отправьте фото для установки аватара.")
        return
    
    # Сохраняем аватар с типом
    user_avatars[user_id] = {
        'file_id': avatar_file_id,
        'type': avatar_type
    }
    
    # Сохраняем аватар в Firebase
    save_avatars()
    
    media_type_text = "фотографией" if avatar_type == 'photo' else "видео" if avatar_type == 'video' else "GIF"
    await message.answer(f"✅ Аватар успешно установлен!\nТеперь ваш профиль будет отображаться с новой {media_type_text}.")

# Обработчик для смены аватара по команде /change_avatar
@router.message(lambda message: message.text and message.text.lower() in ["сменить аватар", "change avatar"])
async def cmd_change_avatar(message: types.Message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        return
    
    if is_vip(user_id):
        await message.answer("📷 Отправьте новое фото, видео или GIF для аватара.")
    else:
        await message.answer("📷 Отправьте новое фото для аватара.")

# Обработчик медиа для смены аватара (для всех пользователей)
@router.message(lambda message: message.photo or message.video or message.animation)
async def handle_avatar_media(message: types.Message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        return
    
    # Определяем тип медиа
    if message.photo:
        avatar_file_id = message.photo[-1].file_id
        avatar_type = 'photo'
    elif message.video:
        if not is_vip(user_id):
            await message.answer(
                "❌ Видео доступно только для VIP пользователей!\n\n"
                "💡 Обратитесь к администратору для получения VIP подписки."
            )
            return
        avatar_file_id = message.video.file_id
        avatar_type = 'video'
    elif message.animation:
        if not is_vip(user_id):
            await message.answer(
                "❌ GIF доступен только для VIP пользователей!\n\n"
                "💡 Обратитесь к администратору для получения VIP подписки."
            )
            return
        avatar_file_id = message.animation.file_id
        avatar_type = 'animation'
    else:
        return
    
    # Сохраняем аватар с типом
    user_avatars[user_id] = {
        'file_id': avatar_file_id,
        'type': avatar_type
    }
    
    # Сохраняем аватар в Firebase
    save_avatars()
    
    media_type_text = "фотографией" if avatar_type == 'photo' else "видео" if avatar_type == 'video' else "GIF"
    await message.answer(f"✅ Аватар успешно обновлен! Теперь ваш профиль будет отображаться с {media_type_text}.")

# Команда для удаления аватара
@router.message(lambda message: message.text and message.text.lower() in ["удалить аватар", "remove avatar", "сбросить аватар"])
async def cmd_remove_avatar(message: types.Message):
    user_id = message.from_user.id
    
    if is_banned(user_id):
        return
    
    if user_id in user_avatars:
        del user_avatars[user_id]
        # Сохраняем изменения в Firebase
        save_avatars()
        await message.answer("✅ Аватар успешно удален!")
    else:
        await message.answer("ℹ️ У вас нет установленного аватара.")

@router.message(lambda message: message.text and message.text.lower() in ["профиль", "profile", "стата", "stats"])
async def cmd_profile(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "profile"):
        return
    
    init_user(user_id, message.from_user.username)
    user_data = users_data[user_id]
    
    # Получаем информацию о пользователе
    username = message.from_user.username
    first_name = message.from_user.first_name
    display_name = f"@{username}" if username else first_name
    
    # Основная статистика
    balance = user_data['balance']
    bank = user_data.get('bank', 0)
    total_won = user_data.get('total_won', 0)
    games_played = user_data.get('games_played', 0)
    
    # Рассчитываем винрейт (упрощенная формула)
    if games_played > 0 and total_won > 0:
        # Более реалистичный расчет винрейта
        win_rate = min(100, (total_won / (total_won + games_played * 500)) * 100)
    else:
        win_rate = 0
    
    # Определяем статус игрока по винрейту
    if win_rate >= 60:
        status = "💎 Легенда"
    elif win_rate >= 50:
        status = "⭐ Профи" 
    elif win_rate >= 40:
        status = "🔥 Опытный"
    elif win_rate >= 30:
        status = "🚀 Начинающий"
    else:
        status = "🎮 Новичок"
    
    # Дополнительная информация
    referrals_count = len(user_data.get('referrals', []))
    
    # Информация о браке
    marriage_status = "💔 Не в браке"
    if user_id in marriages:
        spouse_name = marriages[user_id]['spouse_name']
        marriage_status = f"💍 В браке с {spouse_name}"
    
    # Информация о городе
    city_status = "🏙️ Нет города"
    if user_id in user_cities:
        city_name = user_cities[user_id]['name']
        city_status = f"🏙️ Город: {city_name}"
    
    # Форматируем дату регистрации
    reg_date = user_data.get('registration_date', 'Неизвестно')
    if reg_date != 'Неизвестно':
        try:
            reg_dt = datetime.strptime(reg_date, '%Y-%m-%d %H:%M:%S')
            reg_date_formatted = reg_dt.strftime('%d.%m.%Y')
        except:
            reg_date_formatted = reg_date
    else:
        reg_date_formatted = "Неизвестно"
    
    # Информация об аватаре
    avatar_status = "📷 Аватар: ❌ Не установлен"
    vip_status = ""
    if is_vip(user_id):
        end_time = vip_subscriptions[user_id]
        end_date = datetime.fromtimestamp(end_time).strftime('%d.%m.%Y')
        vip_status = f"\n⭐ <b>VIP подписка:</b> до {end_date}"
    if user_id in user_avatars:
        avatar_data = user_avatars[user_id]
        # Поддержка старого формата (только file_id) и нового (dict с type)
        if isinstance(avatar_data, dict):
            avatar_type = avatar_data.get('type', 'photo')
            if avatar_type == 'video':
                avatar_status = "📷 Аватар: ✅ Видео"
            elif avatar_type == 'animation':
                avatar_status = "📷 Аватар: ✅ GIF"
            else:
                avatar_status = "📷 Аватар: ✅ Фото"
        else:
            avatar_status = "📷 Аватар: ✅ Установлен"
    
    # Создаем красивый профиль
    profile_text = (
        f"👤 <b>ПРОФИЛЬ ИГРОКА</b>\n\n"
        
        f"🏷️ <b>Игрок:</b> {display_name}\n"
        f"🎯 <b>Статус:</b> {status}\n"
        f"📅 <b>Регистрация:</b> {reg_date_formatted}\n"
        f"{avatar_status}{vip_status}\n\n"
        
        f"💳 <b>Финансы:</b>\n"
        f"   💰 Баланс: <code>{format_amount(balance)} MORPH</code>\n"
        f"   🏦 В банке: <code>{format_amount(bank)} MORPH</code>\n\n"
        
        f"📊 <b>Статистика игр:</b>\n"
        f"   🎮 Сыграно игр: <code>{games_played}</code>\n"
        f"   📈 Винрейт: <code>{win_rate:.1f}%</code>\n"
        f"   💸 Выиграно всего: <code>{format_amount(total_won)} MORPH</code>\n\n"
        
        f"👥 <b>Социальное:</b>\n"
        f"   {marriage_status}\n"
        f"   {city_status}\n"
        f"   👥 Рефералов: <code>{referrals_count}</code>\n\n"
        
        f"<i>ℹ️ Для смены аватара используйте команду \"сменить аватар\"</i>"
    )
    
    # Проверяем есть ли аватар
    if user_id in user_avatars:
        avatar_data = user_avatars[user_id]
        # Поддержка старого формата (только file_id) и нового (dict с type)
        if isinstance(avatar_data, dict):
            avatar_file_id = avatar_data.get('file_id', avatar_data)
            avatar_type = avatar_data.get('type', 'photo')
        else:
            # Старый формат - только фото
            avatar_file_id = avatar_data
            avatar_type = 'photo'
        
        # Отправляем профиль с аватаром в зависимости от типа
        if avatar_type == 'video':
            await message.answer_video(
                video=avatar_file_id,
                caption=profile_text,
                parse_mode="HTML"
            )
        elif avatar_type == 'animation':
            await message.answer_animation(
                animation=avatar_file_id,
                caption=profile_text,
                parse_mode="HTML"
            )
        else:
            await message.answer_photo(
                photo=avatar_file_id,
                caption=profile_text,
                parse_mode="HTML"
            )
    else:
        # Отправляем профиль без аватара
        await message.answer(
            profile_text,
            parse_mode="HTML"
        )

@router.message(lambda message: message.text and message.text.lower() in ["аватары", "avatars", "помощь аватар"])
async def cmd_avatars_help(message: types.Message):
    user_id = message.from_user.id
    vip_info = ""
    if is_vip(user_id):
        end_time = vip_subscriptions[user_id]
        end_date = datetime.fromtimestamp(end_time).strftime('%d.%m.%Y')
        vip_info = f"\n\n⭐ <b>У вас активна VIP подписка до {end_date}!</b>\n"
        vip_info += "🎥 Вы можете устанавливать видео и GIF в качестве аватара!"
    else:
        vip_info = "\n\n💡 <b>VIP подписка</b> позволяет устанавливать видео и GIF в качестве аватара!\n"
        vip_info += "Обратитесь к администратору для получения VIP подписки."
    
    help_text = (
        "📷 <b>Команды для работы с аватарами:</b>\n\n"
        "• <b>аватар</b> или <b>установить аватар</b> - установить новый аватар\n"
        "• <b>сменить аватар</b> - заменить текущий аватар\n"
        "• <b>удалить аватар</b> - удалить установленный аватар\n"
        "• <b>профиль</b> - посмотреть свой профиль с аватаром\n\n"
        "<i>Просто отправьте фото в ответ на команду для установки аватара</i>"
        f"{vip_info}"
    )
    
    await message.answer(help_text, parse_mode="HTML")

# --- Пополнение банка с поддержкой ВСЁ ---
@router.message(lambda m: m.text and m.text.lower().startswith(("банк пополнить ", "банк пополнить")))
async def bank_deposit(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "bank_deposit"):
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            await message.reply("❌ Использование: банк пополнить [сумма/ВСЁ]")
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        amount_text = ' '.join(parts[2:])
        amount = parse_amount(amount_text, user_balance)
        
        if amount is None or amount <= 0:
            await message.reply("❌ Сумма должна быть положительной!")
            return
        
        if users_data[user_id]['balance'] < amount:
            await message.reply(f"❌ Недостаточно MORPH на балансе!")
            return
        
        users_data[user_id]['balance'] -= amount
        users_data[user_id]['bank'] += amount
        save_users()
        
        await message.reply(f"✅ Пополнено банк: {format_amount(amount)} MORPH")
            
    except Exception:
        await message.reply("❌ Использование: банк пополнить [сумма/ВСЁ]")

# --- Снятие из банка с поддержкой ВСЁ ---
@router.message(lambda m: m.text and m.text.lower().startswith(("банк снять ", "банк снять")))
async def bank_withdraw(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "bank_withdraw"):
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            await message.reply("❌ Использование: банк снять [сумма/ВСЁ]")
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        bank_balance = users_data[user_id]['bank']
        
        amount_text = ' '.join(parts[2:])
        amount = parse_amount(amount_text, bank_balance)
        
        if amount is None or amount <= 0:
            await message.reply("❌ Сумма должна быть положительной!")
            return
        
        if users_data[user_id]['bank'] < amount:
            await message.reply(f"❌ Недостаточно MORPH в банке!")
            return
        
        users_data[user_id]['bank'] -= amount
        users_data[user_id]['balance'] += amount
        save_users()
        
        await message.reply(f"✅ Снято из банка: {format_amount(amount)} MORPH")
            
    except Exception:
        await message.reply("❌ Использование: банк снять [сумма/ВСЁ]")

# --- Команда банка ---
@router.message(lambda message: message.text and message.text.lower() in ["банк", "bank", "Банк", "БАНК"])
async def cmd_bank(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "bank"):
        return
    init_user(user_id, message.from_user.username)
    u = users_data[user_id]
    
    bank_text = (
        f"🏦 Банк: {format_amount(u['bank'])} MORPH\n"
        f"💵 На руках: {format_amount(u['balance'])} MORPH"
    )
    await message.reply(bank_text)

# --- Топ ---
@router.message(lambda message: message.text and message.text.lower() in ["топ", "top"])
async def cmd_top(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "top"):
        return
    
    print(f"DEBUG: Начало обработки команды топ для пользователя {user_id}")
    try:
        # Проверяем, что users_data загружен
        if not isinstance(users_data, dict):
            print(f"DEBUG: users_data не является словарём: {type(users_data)}")
            await message.reply("❌ <b>Ошибка при загрузке данных. Попробуйте позже.</b>", parse_mode="HTML")
            return
        
        # Фильтруем пользователей с корректными данными
        print(f"DEBUG: Всего пользователей в базе: {len(users_data)}")
        valid_users = []
        for uid, data in users_data.items():
            try:
                if (isinstance(uid, int) and 
                    isinstance(data, dict) and 
                    'balance' in data):
                    balance = data['balance']
                    # Безопасное преобразование баланса
                    if isinstance(balance, (int, float)):
                        try:
                            balance_float = float(balance)
                            if balance_float >= 0:  # Только неотрицательные балансы
                                valid_users.append((uid, data))
                        except (ValueError, TypeError, OverflowError):
                            continue
            except Exception as e:
                print(f"Ошибка при обработке пользователя {uid} в топе: {e}")
                continue
        
        print(f"DEBUG: Валидных пользователей: {len(valid_users)}")
        if not valid_users:
            await message.reply("📊 <b>Пока нет игроков в рейтинге!</b>", parse_mode="HTML")
            return
        
        # Сортируем по балансу
        try:
            sorted_users = sorted(
                valid_users,
                key=lambda x: x[1]['balance'],
                reverse=True
            )
            print(f"DEBUG: Отсортировано пользователей: {len(sorted_users)}")
        except Exception as e:
            print(f"Ошибка при сортировке пользователей: {e}")
            await message.reply("❌ <b>Ошибка при сортировке данных. Попробуйте позже.</b>", parse_mode="HTML")
            return
        
        top_text = "<b>🏆 ТОП ИГРОКОВ ПО БАЛАНСУ</b>\n\n"
        builder = InlineKeyboardBuilder()
        buttons_added = 0
        
        print(f"DEBUG: Начинаем формирование топа из {min(10, len(sorted_users))} пользователей")
        for i, (uid, user_data) in enumerate(sorted_users[:10], 1):
            try:
                # Безопасное получение имени пользователя
                try:
                    username = user_data.get('username', None)
                    if not username or not isinstance(username, str):
                        username = f'Игрок {uid}'
                    
                    # Очищаем ник от возможных тегов @
                    if username.startswith('@'):
                        username = username[1:]
                    
                    # Ограничиваем длину username
                    if len(username) > 50:
                        username = username[:50]
                    
                    # Экранируем HTML символы в username
                    username = username.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                except Exception as e:
                    print(f"Ошибка при обработке username для пользователя {uid}: {e}")
                    username = f'Игрок {uid}'
                
                balance = user_data.get('balance', 0)
                if not isinstance(balance, (int, float)):
                    balance = 0
                try:
                    balance = int(float(balance))
                    if balance < 0:
                        balance = 0
                except (ValueError, TypeError, OverflowError):
                    balance = 0
                
                # Эмодзи для первых трех мест
                if i == 1:
                    emoji = "🥇"
                elif i == 2:
                    emoji = "🥈" 
                elif i == 3:
                    emoji = "🥉"
                else:
                    emoji = f"{i}."
                
                # Используем просто имя без тега
                top_text += f"{emoji} <b>{username}</b>: <b>{format_amount(balance)} MORPH</b>\n"
                
                # Добавляем кнопку для перехода в профиль
                try:
                    button_text = f"{emoji} {username[:20]}"  # Ограничиваем длину для кнопки
                    # Очищаем текст кнопки от HTML тегов
                    button_text = button_text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                    # Убираем HTML теги из текста кнопки
                    button_text = re.sub(r'<[^>]+>', '', button_text)
                    builder.button(
                        text=button_text,
                        url=f"tg://user?id={uid}"
                    )
                    buttons_added += 1
                except Exception as e:
                    print(f"Ошибка при добавлении кнопки для пользователя {uid}: {e}")
                    continue
                
            except Exception as e:
                # Пропускаем проблемных пользователей
                continue
        
        print(f"DEBUG: Сформирован текст топа, длина: {len(top_text)}, кнопок добавлено: {buttons_added}")
        
        if len(top_text) <= len("<b>🏆 ТОП ИГРОКОВ ПО БАЛАНСУ</b>\n\n"):
            top_text += "📊 <b>Недостаточно данных для составления топа</b>"
            reply_markup = None
        else:
            if buttons_added > 0:
                top_text += "\n💡 <i>Нажмите на кнопку ниже, чтобы перейти в профиль игрока</i>"
                try:
                    builder.adjust(1)  # По одной кнопке в ряд
                    reply_markup = builder.as_markup()
                    print(f"DEBUG: Клавиатура создана успешно")
                except Exception as e:
                    print(f"Ошибка при создании клавиатуры топа: {e}")
                    import traceback
                    traceback.print_exc()
                    reply_markup = None
            else:
                reply_markup = None
        
        print(f"DEBUG: Отправляем сообщение с топом")
        try:
            await message.reply(
                top_text, 
                parse_mode="HTML", 
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
            print(f"DEBUG: Сообщение с топом отправлено успешно")
        except Exception as send_error:
            print(f"Ошибка при отправке сообщения с топом: {send_error}")
            import traceback
            traceback.print_exc()
            # Пытаемся отправить без кнопок
            try:
                await message.reply(
                    top_text, 
                    parse_mode="HTML", 
                    disable_web_page_preview=True
                )
            except Exception as e2:
                print(f"Ошибка при отправке сообщения без кнопок: {e2}")
                raise
        
    except Exception as e:
        print(f"Ошибка при формировании топа: {e}")
        import traceback
        traceback.print_exc()
        await message.reply("❌ <b>Ошибка при формировании топа. Попробуйте позже.</b>", parse_mode="HTML")

# --- Реферальная ссылка ---
@router.message(lambda message: message.text and message.text.lower() in ["моя рефка", "рефка", "реферал"])
async def cmd_referral(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "referral"):
        return
    init_user(user_id, message.from_user.username)
    
    # Получаем информацию о рефералах
    user_data = users_data[user_id]
    referrals_count = len(user_data.get('referrals', []))
    referrer_id = user_data.get('referrer_id')
    
    # Создаем реферальную ссылку
    bot_username = (await message.bot.me()).username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    
    referral_text = (
        f"🎁 <b>ВАША РЕФЕРАЛЬНАЯ ССЫЛКА</b>\n\n"
        f"🔗 <code>{referral_link}</code>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👥 Приглашено игроков: <b>{referrals_count}</b>\n"
        f"💰 Заработано с рефералов: <b>{format_amount(referrals_count * 1000)} MORPH</b>\n\n"
        f"💡 <b>Как это работает:</b>\n"
        f"• За каждого приглашенного игрока вы получаете <b>1000 MORPH</b>\n"
        f"• Приглашенный игрок получает <b>2500 MORPH</b> на старт\n"
        f"• Делитесь ссылкой с друзьями и зарабатывайте вместе!\n\n"
        f"🎯 <b>Минимальная ставка: 100 MORPH</b>"
    )
    
    # Если у пользователя есть реферер, показываем информацию о нем
    if referrer_id and referrer_id in users_data:
        referrer_name = users_data[referrer_id].get('username', f'User{referrer_id}')
        referral_text += f"\n\n🎁 <b>Вас пригласил:</b> @{referrer_name}"
    
    await message.reply(referral_text, parse_mode="HTML")

# Команда пинг
# Команда пинг
@router.message(lambda message: message.text and message.text.lower() in ["пинг", "ping"])
async def cmd_ping(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "ping"):
        return
    
    # Измеряем пинг
    start_time = time.time()
    msg = await message.reply("🏓 Измерение пинга...")
    end_time = time.time()
    
    ping_ms = round((end_time - start_time) * 1000, 2)
    
    # Определяем цвет статуса по пингу
    if ping_ms < 100:
        status = "🟢"
    elif ping_ms < 300:
        status = "🟠"
    else:
        status = "🔴"
    
    # Создаем визуальный индикатор пинга
    bars_count = min(10, max(1, int(ping_ms / 100)))
    ping_bar = "[" + "■" * bars_count + "□" * (10 - bars_count) + "]"
    
    # Текущее время сервера
    server_time = datetime.now().strftime('%H:%M:%S')
    
    ping_text = (
        f"🏓 Пинг: {ping_ms} мс {status}\n"
        f"{ping_bar}\n"
        f"🕒 Сервер: {server_time}"
    )
    
    await msg.edit_text(ping_text)

# --- Команда 'дать' (перевод MORPH) ---
@router.message(lambda message: message.reply_to_message and message.text and message.text.lower().startswith("дать"))
async def transfer_morph(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.reply('❌ Используйте команду в ответ на сообщение пользователя.')
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply("❌ Использование: ответьте на сообщение командой 'дать [сумма/ВСЁ]'", parse_mode="HTML")
        return
    
    from_user_id = message.from_user.id
    init_user(from_user_id, message.from_user.username)
    user_balance = users_data[from_user_id]['balance']
    
    amount = parse_amount(parts[1], user_balance)
    if amount is None or amount <= 0:
        await message.reply("❌ Сумма должна быть положительной!", parse_mode="HTML")
        return
    
    to_user_id = message.reply_to_message.from_user.id
    if from_user_id == to_user_id:
        await message.reply("❌ Нельзя переводить самому себе!", parse_mode="HTML")
        return
    
    init_user(to_user_id, message.reply_to_message.from_user.username)
    
    # ПРОСТАЯ ПРОВЕРКА ВМЕСТО check_bet_amount
    if amount > user_balance:
        await message.reply(f"❌ Недостаточно MORPH! Ваш баланс: {format_amount(user_balance)} MORPH")
        return
    if amount < 100:
        await message.reply("❌ Минимальная сумма перевода: 100 MORPH!")
        return
    
    ensure_transfer_profile(from_user_id)
    reset_transfer_counters_if_needed(from_user_id)
    sender_profile = users_data[from_user_id]
    current_level = sender_profile.get('transfer_limit_level', 0)
    current_limit = get_transfer_limit(current_level)
    spent_today = sender_profile.get('transfer_daily_spent', 0)

    if current_limit is not None and spent_today + amount > current_limit:
        remaining = max(0, current_limit - spent_today)
        next_cost = get_next_transfer_cost(current_level)
        reset_seconds = seconds_until_transfer_reset(from_user_id)
        reset_text = format_duration(reset_seconds) if reset_seconds else 'менее минуты'
        suggestion = ""
        if next_cost is not None:
            next_limit = format_transfer_limit(get_transfer_limit(current_level + 1))
            suggestion = (
                f"\n\n➡️ <b>Следующий уровень:</b> {current_level + 1} — лимит {next_limit} MORPH"
                f"\n💰 Стоимость улучшения: <b>{format_amount(next_cost)}</b> MORPH"
                f"\n🛠 Команда: <code>лимит купить</code>"
            )
        else:
            suggestion = "\n\n🔓 У вас уже максимальный уровень лимита."

        await message.reply(
            "❌ <b>Превышен дневной лимит переводов!</b>\n\n"
            f"📈 Уровень: <b>{current_level}</b>\n"
            f"💼 Лимит: <b>{format_transfer_limit(current_limit)}</b>\n"
            f"💸 Потрачено сегодня: <b>{format_amount(spent_today)}</b> MORPH\n"
            f"🕒 До обновления: <b>{reset_text}</b>\n"
            f"📤 Доступно сейчас: <b>{format_amount(remaining)}</b> MORPH"
            f"{suggestion}",
            parse_mode="HTML"
        )
        return

    users_data[from_user_id]['balance'] -= amount
    users_data[to_user_id]['balance'] += amount
    sender_profile['transfer_daily_spent'] = sender_profile.get('transfer_daily_spent', 0) + amount
    save_users()

    if parts[1].lower() in ['всё', 'все', 'all']:
        await message.reply(f'✅ Переведены ВСЕ средства: {format_amount(amount)} MORPH игроку {message.reply_to_message.from_user.first_name}', parse_mode="HTML")
    else:
        await message.reply(f'✅ Переведено {format_amount(amount)} MORPH игроку {message.reply_to_message.from_user.first_name}', parse_mode="HTML")


@router.message(lambda message: message.text and message.text.lower().startswith(('лимит', 'limit')))
async def transfer_limit_command(message: types.Message):
    if is_banned(message.from_user.id):
        return

    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    ensure_transfer_profile(user_id)
    reset_transfer_counters_if_needed(user_id)

    user_profile = users_data[user_id]
    current_level = user_profile.get('transfer_limit_level', 0)
    current_limit = get_transfer_limit(current_level)
    spent_today = user_profile.get('transfer_daily_spent', 0)
    next_cost = get_next_transfer_cost(current_level)
    reset_seconds = seconds_until_transfer_reset(user_id)
    reset_text = format_duration(reset_seconds) if reset_seconds else 'менее минуты'

    tokens = message.text.lower().split()
    wants_upgrade = len(tokens) > 1 and tokens[1] in {'купить', 'апгрейд', 'upgrade', 'ап', 'buy'}

    if wants_upgrade:
        if next_cost is None:
            await message.reply('🔓 Ваш лимит уже максимальный — улучшать нечего!', parse_mode='HTML')
            return
        if users_data[user_id]['balance'] < next_cost:
            await message.reply(
                "❌ Недостаточно MORPH для улучшения лимита!\n"
                f"💰 Требуется: <b>{format_amount(next_cost)}</b> MORPH",
                parse_mode='HTML'
            )
            return

        users_data[user_id]['balance'] -= next_cost
        user_profile['transfer_limit_level'] = current_level + 1
        user_profile['transfer_daily_spent'] = 0
        user_profile['transfer_daily_reset'] = int(time.time())
        save_users()

        current_level = user_profile['transfer_limit_level']
        current_limit = get_transfer_limit(current_level)
        next_cost = get_next_transfer_cost(current_level)
        reset_seconds = TRANSFER_RESET_SECONDS
        reset_text = format_duration(reset_seconds)

        await message.reply(
            "✅ <b>Лимит переводов улучшен!</b>\n\n"
            f"📈 Новый уровень: <b>{current_level}</b>\n"
            f"💼 Дневной лимит: <b>{format_transfer_limit(current_limit)}</b>\n"
            f"🕒 Лимит обновится через: <b>{reset_text}</b>",
            parse_mode='HTML'
        )
        return

    if next_cost is None:
        next_info = "🔓 Вы уже на максимальном уровне лимита."
    else:
        next_limit = format_transfer_limit(get_transfer_limit(current_level + 1))
        next_info = (
            f"➡️ Следующий уровень: <b>{current_level + 1}</b> — лимит {next_limit} MORPH\n"
            f"💰 Стоимость улучшения: <b>{format_amount(next_cost)}</b> MORPH\n"
            f"🛠 Для улучшения отправьте <code>лимит купить</code>"
        )

    await message.reply(
        "💳 <b>Ваш дневной лимит переводов</b>\n\n"
        f"📈 Уровень: <b>{current_level}</b>\n"
        f"💼 Лимит: <b>{format_transfer_limit(current_limit)}</b>\n"
        f"💸 Потрачено сегодня: <b>{format_amount(spent_today)}</b> MORPH\n"
        f"🕒 До обновления: <b>{reset_text}</b>\n\n"
        f"{next_info}",
        parse_mode='HTML'
    )


# ИГРА "МИНЫ"
# ИГРА "МИНЫ" - ИСПРАВЛЕННАЯ ВЕРСИЯ
active_mines_games = {}


@router.message(lambda message: message.text and message.text.lower().startswith("мины"))
async def start_mines_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    enforce_game_enabled("mines")
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "💣 <b>ИГРА МИНЫ</b>\n\n"
                "❌ Использование: <b>мины [ставка/ВСЁ] [количество мин (2-24)]</b>\n"
                "💡 Пример: <b>мины ВСЁ 5</b>\n"
                "🎯 Минимальная ставка: 100 MORPH\n\n"
                "🏆 <b>Правила игры:</b>\n"
                "• Открывайте безопасные клетки на поле 5x5\n"
                "• Каждая открытая клетка увеличивает множитель\n"
                "• Избегайте мин - они заканчивают игру\n"
                "• Забирайте выигрыш в любой момент!",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        mines_count = int(parts[2])
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        if not (2 <= mines_count <= 24):
            await message.reply("❌ Количество мин должно быть от 2 до 24!")
            return
        
        # Создаем игровое поле 5x5
        field = [[0 for _ in range(5)] for _ in range(5)]
        mines_positions = []
        
        # Размещаем мины случайно
        while len(mines_positions) < mines_count:
            x, y = random.randint(0, 4), random.randint(0, 4)
            if (x, y) not in mines_positions:
                mines_positions.append((x, y))
                field[x][y] = -1  # -1 означает мину
        
        # Создаем клавиатуру с серыми клетками
        builder = InlineKeyboardBuilder()
        for i in range(5):
            row = []
            for j in range(5):
                row.append(InlineKeyboardButton(
                    text="⬜",  # Серые клетки вместо синих
                    callback_data=f"mines_{i}_{j}_{user_id}_{bet}_{mines_count}"
                ))
            builder.row(*row)
        
        # Кнопка "Забрать выигрыш"
        builder.row(InlineKeyboardButton(
            text="💰 Забрать выигрыш (1.0x)",
            callback_data=f"mines_cashout_{user_id}_{bet}_{mines_count}"
        ))
        
        # Сохраняем игру с защитой от дюпа
        active_mines_games[user_id] = {
            'field': field,
            'mines_positions': mines_positions,
            'opened_cells': set(),
            'move_in_progress': False,
            'bet': bet,
            'mines_count': mines_count,
            'multiplier': 1.0,
            'cashout_used': False,
            'game_over': False,
            'game_id': f"mines_{user_id}_{int(time.time())}",
            'game_owner': user_id,  # Владелец игры для защиты
            'message_id': None  # Добавляем хранение ID сообщения
        }
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        sent_message = await message.reply(
            f"💣 <b>ИГРА МИНЫ</b>\n\n"
            f"👤 <b>Игрок:</b> {message.from_user.first_name}\n"
            f"💰 <b>Ставка:</b> {format_amount(bet)} MORPH\n"
            f"💣 <b>Мин на поле:</b> {mines_count}\n"
            f"📊 <b>Текущий коэффициент:</b> 1.0x\n"
            f"🎯 <b>Текущий выигрыш:</b> {format_amount(bet)} MORPH\n\n"
            f"⬜ <b>Выберите клетку для открытия:</b>\n"
            f"• ⬜ - неоткрытая клетка\n"
            f"• 💎 - безопасная клетка\n"
            f"• 💥 - мина\n\n"
            f"⚡ <b>Каждая открытая клетка увеличивает множитель!</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        # Сохраняем ID сообщения для защиты от дублирования
        active_mines_games[user_id]['message_id'] = sent_message.message_id
        
    except ValueError:
        await message.reply("❌ Неверные параметры! Укажите число мин от 2 до 24.")

# ПРОГРЕССИВНЫЕ КОЭФФИЦИЕНТЫ ДЛЯ МИН
def get_mines_multiplier(opened_cells, mines_count, total_cells=25):
    """Сбалансированные коэффициенты для предотвращения накрутки"""
    
    # Базовые множители в зависимости от количества мин
    base_multipliers = {
        2: 1.05,    # Минимальный для 2 мин
        3: 1.10,
        4: 1.15,
        5: 1.20,
        6: 1.25,
        7: 1.30,
        8: 1.35,
        9: 1.40,
        10: 1.45,
        11: 1.50,
        12: 1.55,
        13: 1.60,
        14: 1.65,
        15: 1.70,
        16: 1.75,
        17: 1.80,
        18: 1.85,
        19: 1.90,
        20: 1.95,
        21: 2.00,
        22: 2.05,
        23: 2.10,
        24: 2.15    # Максимальный базовый для 24 мин
    }
    
    base_multiplier = base_multipliers.get(mines_count, 1.25)
    
    if opened_cells == 0:
        return 1.0
    
    # Мягкий прогрессивный рост с ограничением
    safe_cells = total_cells - mines_count
    progress_ratio = opened_cells / safe_cells
    
    # Максимальный множитель в зависимости от количества мин
    max_multipliers = {
        2: 8.0,     # Максимум x8 для 2 мин
        3: 9.0,
        4: 10.0,
        5: 11.0,
        6: 12.0,
        7: 13.0,
        8: 14.0,
        9: 15.0,
        10: 16.0,
        11: 17.0,
        12: 18.0,
        13: 19.0,
        14: 20.0,
        15: 21.0,
        16: 22.0,
        17: 23.0,
        18: 24.0,
        19: 25.0,
        20: 26.0,
        21: 27.0,
        22: 28.0,
        23: 29.0,
        24: 30.0    # Максимум x30 для 24 мин
    }
    
    max_multiplier = max_multipliers.get(mines_count, 15.0)
    
    # Расчет множителя с прогрессивным ростом
    multiplier = 1.0
    for i in range(opened_cells):
        # Каждая следующая клетка дает меньший прирост
        cell_multiplier = base_multiplier * (1.0 - (i * 0.02))  # Уменьшаем прирост на 2% за клетку
        multiplier *= max(1.01, cell_multiplier)  # Минимальный прирост 1%
        
        # Ограничение максимального множителя
        if multiplier > max_multiplier:
            multiplier = max_multiplier
            break
    
    return round(multiplier, 2)

# Обработка нажатий на клетки в игре "Мины" с улучшенной защитой
@router.callback_query(lambda c: c.data.startswith("mines_") and not c.data.startswith("mines_cashout_") and not c.data.startswith("mines_restart_"))
async def mines_callback(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    try:
        data = callback.data.split("_")
        
        # Проверяем, что это координаты клетки
        if len(data) < 6 or not data[1].isdigit() or not data[2].isdigit():
            await callback.answer("❌ Ошибка данных!")
            return
        
        # Обработка нажатия на клетку
        x, y = int(data[1]), int(data[2])
        target_user_id = int(data[3])
        bet = int(data[4])
        mines_count = int(data[5])
        
        # 🔒 ЗАЩИТА: проверяем, что нажимает владелец игры
        if callback.from_user.id != target_user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if target_user_id not in active_mines_games:
            await callback.answer("❌ Игра не найдена или завершена!", show_alert=True)
            return
        
        game = active_mines_games[target_user_id]
        
        # Проверяем, не закончилась ли уже игра
        if game.get('game_over'):
            await callback.answer("❌ Игра уже завершена!", show_alert=True)
            return
        
        # Проверяем, не была ли уже нажата кнопка "Забрать выигрыш"
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        # Проверяем, не открыта ли уже эта клетка
        if (x, y) in game['opened_cells']:
            await callback.answer("❌ Клетка уже открыта!", show_alert=True)
            return
        
        # Проверяем, что ход не обрабатывается
        if game.get('move_in_progress', False):
            await callback.answer("⏳ Ход уже обрабатывается, подождите!", show_alert=True)
            return
        
        # Блокируем повторные нажатия
        game['move_in_progress'] = True
        
        # Добавляем задержку для предотвращения спама
        await callback.answer()
        
        game['opened_cells'].add((x, y))
        
        if game['field'][x][y] == -1:
            # Попали на мину - проигрыш
            game['game_over'] = True
            game['move_in_progress'] = False
            
            # Визуализация поля с минами
            builder = InlineKeyboardBuilder()
            for i in range(5):
                row = []
                for j in range(5):
                    if (i, j) == (x, y):
                        row.append(InlineKeyboardButton(text="💥", callback_data="mines_game_over"))
                    elif (i, j) in game['mines_positions']:
                        row.append(InlineKeyboardButton(text="💣", callback_data="mines_game_over"))
                    elif (i, j) in game['opened_cells']:
                        row.append(InlineKeyboardButton(text="💎", callback_data="mines_game_over"))
                    else:
                        row.append(InlineKeyboardButton(text="⬜", callback_data="mines_game_over"))
                builder.row(*row)
            
            # Кнопка новой игры
            builder.row(InlineKeyboardButton(
                text="🔄 Играть снова", 
                callback_data=f"mines_restart_{target_user_id}"
            ))
            
            await callback.message.edit_text(
                f"💥 <b>БУМ! ВЫ ПРОИГРАЛИ</b>\n\n"
                f"💣 Вы попали на мину в клетке ({x+1}, {y+1})!\n"
                f"💰 Проигрыш: {format_amount(bet)} MORPH\n"
                f"🎯 Открыто клеток: {len(game['opened_cells'])-1}\n"
                f"📊 Максимальный коэффициент: {game['multiplier']:.2f}x\n\n"
                f"🔴 <b>Красным</b> отмечена клетка с миной\n"
                f"💣 <b>Черным</b> отмечены остальные мины\n"
                f"💎 <b>Синим</b> отмечены безопасные клетки",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            
            add_game_to_history(target_user_id, 'Мины', bet, 'lose', 0)
            users_data[target_user_id]['games_played'] += 1
            save_users()
            del active_mines_games[target_user_id]
            return
        
        # Успешно открыли клетку
        # Разблокируем для следующего хода
        game['move_in_progress'] = False
        
        # Рассчитываем новый коэффициент по улучшенной формуле
        opened_cells = len(game['opened_cells'])
        game['multiplier'] = get_mines_multiplier(opened_cells, mines_count)
        
        won_amount = int(bet * game['multiplier'])
        
        # Обновляем клавиатуру с улучшенной визуализацией
        builder = InlineKeyboardBuilder()
        for i in range(5):
            row = []
            for j in range(5):
                if (i, j) in game['opened_cells']:
                    row.append(InlineKeyboardButton(
                        text="💎",
                        callback_data=f"mines_opened_{i}_{j}"
                    ))
                else:
                    row.append(InlineKeyboardButton(
                        text="⬜",
                        callback_data=f"mines_{i}_{j}_{target_user_id}_{bet}_{mines_count}"
                    ))
            builder.row(*row)
        
        # Кнопка "Забрать выигрыш" с актуальной суммой
        builder.row(InlineKeyboardButton(
            text=f"💰 Забрать {format_amount(won_amount)} MORPH ({game['multiplier']:.2f}x)",
            callback_data=f"mines_cashout_{target_user_id}_{bet}_{mines_count}"
        ))
        
        await callback.message.edit_text(
            f"💣 <b>ИГРА МИНЫ - УСПЕХ!</b>\n\n"
            f"👤 <b>Игрок:</b> {callback.from_user.first_name}\n"
            f"💰 <b>Ставка:</b> {format_amount(bet)} MORPH\n"
            f"💣 <b>Мин на поле:</b> {mines_count}\n"
            f"📊 <b>Текущий коэффициент:</b> {game['multiplier']:.2f}x\n"
            f"🎯 <b>Текущий выигрыш:</b> {format_amount(won_amount)} MORPH\n"
            f"✅ <b>Открыто клеток:</b> {opened_cells}/25\n\n"
            f"💎 <b>Клетка ({x+1}, {y+1}) безопасна!</b>\n"
            f"⚡ <b>Продолжайте в том же духе!</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        print(f"Ошибка в mines_callback: {e}")
        await callback.answer("❌ Произошла ошибка, попробуйте снова", show_alert=True)

# Обработка кнопки "Забрать выигрыш"
@router.callback_query(lambda c: c.data.startswith("mines_cashout_"))
async def mines_cashout(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    try:
        data = callback.data.split("_")
        target_user_id = int(data[2])
        bet = int(data[3])
        mines_count = int(data[4])
        
        # Проверяем, что нажимает владелец игры
        if callback.from_user.id != target_user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if target_user_id not in active_mines_games:
            await callback.answer("❌ Игра не найдена!", show_alert=True)
            return
        
        game = active_mines_games[target_user_id]
        
        # Проверяем, не закончилась ли уже игра
        if game.get('game_over'):
            await callback.answer("❌ Игра уже завершена!", show_alert=True)
            return
        
        # Проверяем, не была ли уже нажата кнопка "Забрать выигрыш"
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        # Помечаем, что выигрыш забран
        game['cashout_used'] = True
        game['game_over'] = True
        
        won_amount = int(bet * game['multiplier'])
        
        # Начисляем выигрыш с обновлением лидерборда и истории
        add_win_to_user(target_user_id, won_amount, bet)
        add_game_to_history(target_user_id, 'Мины', bet, 'win', won_amount)
        users_data[target_user_id]['games_played'] += 1
        save_users()
        
        # Показываем финальное поле
        builder = InlineKeyboardBuilder()
        for i in range(5):
            row = []
            for j in range(5):
                if (i, j) in game['mines_positions']:
                    row.append(InlineKeyboardButton(text="💣", callback_data="mines_game_over"))
                elif (i, j) in game['opened_cells']:
                    row.append(InlineKeyboardButton(text="💎", callback_data="mines_game_over"))
                else:
                    row.append(InlineKeyboardButton(text="⬜", callback_data="mines_game_over"))
            builder.row(*row)
        
        # Кнопка новой игры
        builder.row(InlineKeyboardButton(
            text="🔄 Играть снова", 
            callback_data=f"mines_restart_{target_user_id}"
        ))
        
        await callback.message.edit_text(
            f"🎉 <b>ВЫИГРЫШ ЗАБРАН!</b>\n\n"
            f"💰 <b>Ваш выигрыш:</b> {format_amount(won_amount)} MORPH\n"
            f"📊 <b>Коэффициент:</b> {game['multiplier']:.2f}x\n"
            f"🎯 <b>Открыто клеток:</b> {len(game['opened_cells'])}\n"
            f"💣 <b>Мин на поле:</b> {mines_count}\n\n"
            f"💎 <b>Поздравляем с победой!</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        del active_mines_games[target_user_id]
        await callback.answer(f"🎉 Выигрыш {format_amount(won_amount)} MORPH зачислен!")
        
    except Exception as e:
        print(f"Ошибка в mines_cashout: {e}")
        await callback.answer("❌ Произошла ошибка", show_alert=True)

# Обработка кнопки перезапуска игры
@router.callback_query(lambda c: c.data.startswith("mines_restart_"))
async def mines_restart(callback: CallbackQuery):
    user_id = callback.from_user.id
    if is_banned(user_id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    # Удаляем старое сообщение с игрой
    await callback.message.delete()
    
    # Отправляем инструкцию для новой игры
    await callback.message.answer(
        "💣 <b>Чтобы начать новую игру МИНЫ, введите:</b>\n\n"
        "➡️ <b>мины [ставка] [количество мин]</b>\n"
        "💡 Пример: <b>мины 1000 5</b>\n\n"
        "🎯 Количество мин: от 2 до 24",
        parse_mode="HTML"
    )
    
    await callback.answer()
# ======== МНОГОЯЗЫЧНОСТЬ БЕЗ ИЗМЕНЕНИЯ КОМАНД ========
LANGUAGES = {
    'ru': 'Русский',
    'en': 'English',
    'ja': '日本語'
}

ALLOWED_LANGS = {'ru', 'en', 'ja'}
DEFAULT_LANG = 'ru'


def get_user_language(user_id: int) -> str:
    return user_languages.get(user_id, DEFAULT_LANG)


def set_user_language(user_id: int, lang_code: str) -> None:
    lang_code = lang_code.lower()
    if lang_code not in ALLOWED_LANGS:
        logging.warning("Попытка установить неподдерживаемый язык: %s", lang_code)
        lang_code = DEFAULT_LANG
    user_languages[user_id] = lang_code
    save_user_languages()

# Перехватываем метод answer у всех сообщений
from aiogram.types import Message

original_answer = Message.answer


async def translate_text(text: str, target_lang: str) -> str:
    if target_lang == 'ru' or not text:
        return text
    try:
        translated = translator.translate(text, dest=target_lang)
        return translated.text
    except Exception as exc:
        logging.warning("Translation error for lang %s: %s", target_lang, exc)
        return text


async def new_answer(self, text: str, **kwargs):
    user_id = self.chat.id
    user_lang = get_user_language(user_id)
    text = await translate_text(text, user_lang)
    return await original_answer(self, text, **kwargs)


Message.answer = new_answer

original_edit_text = Message.edit_text


async def new_edit_text(self, text: str, **kwargs):
    user_id = self.chat.id
    user_lang = get_user_language(user_id)
    text = await translate_text(text, user_lang)
    return await original_edit_text(self, text, **kwargs)


Message.edit_text = new_edit_text

# ======== КОМАНДА СМЕНЫ ЯЗЫКА ========
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

@router.message(lambda message: message.text and message.text.lower() in ["язык", "language", "言語"])
async def cmd_language(message: Message):  # Убрал types.
    user_id = message.from_user.id
    current_lang = get_user_language(user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🇷🇺 Русский {'✅' if current_lang == 'ru' else ''}", callback_data="lang_ru")],
        [InlineKeyboardButton(text=f"🇬🇧 English {'✅' if current_lang == 'en' else ''}", callback_data="lang_en")],
        [InlineKeyboardButton(text=f"🇯🇵 日本語 {'✅' if current_lang == 'ja' else ''}", callback_data="lang_ja")]
    ])
    
    await message.answer("🌐 Выберите язык / Select language / 言語を選択:", reply_markup=keyboard)

@router.callback_query(lambda callback: callback.data.startswith("lang_"))
async def process_language(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    lang_code = callback.data.split("_")[1]
    set_user_language(user_id, lang_code)

    texts = {
        'ru': "✅ Язык изменен на русский!",
        'en': "✅ Language changed to English!",
        'ja': "✅ 言語が日本語に変更されました！"
    }

    await callback.message.edit_text(texts.get(lang_code, texts['ru']))
    await callback.answer()

# --- Топ по банкам (новая команда) ---
@router.message(lambda message: message.text and message.text.lower() in ["топ банк", "топ банки", "top bank"])
async def cmd_top_bank(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "top_bank"):
        return
    
    try:
        # Проверяем, что users_data загружен
        if not isinstance(users_data, dict):
            print(f"DEBUG: users_data не является словарём: {type(users_data)}")
            await message.reply("❌ <b>Ошибка при загрузке данных. Попробуйте позже.</b>", parse_mode="HTML")
            return
        
        # Фильтруем пользователей с корректными данными банка
        valid_users = []
        for uid, data in users_data.items():
            try:
                if (isinstance(uid, int) and 
                    isinstance(data, dict) and 
                    'bank' in data):
                    bank = data['bank']
                    # Безопасное преобразование банка
                    if isinstance(bank, (int, float)):
                        try:
                            bank_float = float(bank)
                            if bank_float > 0:  # Только с деньгами в банке
                                valid_users.append((uid, data))
                        except (ValueError, TypeError, OverflowError):
                            continue
            except Exception as e:
                print(f"Ошибка при обработке пользователя {uid} в топе банка: {e}")
                continue
        
        if not valid_users:
            await message.reply("🏦 <b>Пока нет игроков с деньгами в банке!</b>", parse_mode="HTML")
            return
        
        # Сортируем по сумме в банке
        sorted_users = sorted(
            valid_users,
            key=lambda x: x[1]['bank'],
            reverse=True
        )
        
        top_text = "<b>🏦 ТОП ИГРОКОВ ПО БАНКУ</b>\n\n"
        builder = InlineKeyboardBuilder()
        buttons_added = 0
        
        for i, (uid, user_data) in enumerate(sorted_users[:10], 1):
            try:
                # Безопасное получение имени пользователя (без тега)
                try:
                    username = user_data.get('username', None)
                    if not username or not isinstance(username, str):
                        username = f'Игрок {uid}'
                    
                    # Очищаем ник от возможных тегов @
                    if username.startswith('@'):
                        username = username[1:]
                    
                    # Ограничиваем длину username
                    if len(username) > 50:
                        username = username[:50]
                    
                    # Экранируем HTML символы в username
                    username = username.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                except Exception as e:
                    print(f"Ошибка при обработке username для пользователя {uid}: {e}")
                    username = f'Игрок {uid}'
                
                # Безопасное получение банка
                bank_balance = user_data.get('bank', 0)
                if not isinstance(bank_balance, (int, float)):
                    bank_balance = 0
                try:
                    bank_balance = float(bank_balance)
                    if bank_balance < 0:
                        bank_balance = 0
                except (ValueError, TypeError, OverflowError):
                    bank_balance = 0
                
                # Эмодзи для первых трех мест
                if i == 1:
                    emoji = "🥇"
                elif i == 2:
                    emoji = "🥈" 
                elif i == 3:
                    emoji = "🥉"
                else:
                    emoji = f"{i}."
                
                # Без тега, просто текст
                try:
                    bank_balance_int = int(bank_balance)
                    top_text += f"{emoji} <b>{username}</b>: <b>{format_amount(bank_balance_int)} MORPH</b>\n"
                except (ValueError, TypeError, OverflowError):
                    continue
                
                # Добавляем кнопку для перехода в профиль
                try:
                    button_text = f"{emoji} {username[:20]}"  # Ограничиваем длину
                    # Очищаем текст кнопки от HTML тегов
                    button_text = button_text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                    # Убираем HTML теги из текста кнопки
                    button_text = re.sub(r'<[^>]+>', '', button_text)
                    builder.button(
                        text=button_text,
                        url=f"tg://user?id={uid}"
                    )
                    buttons_added += 1
                except Exception as e:
                    print(f"Ошибка при добавлении кнопки для пользователя {uid}: {e}")
                    continue
                
            except Exception as e:
                # Пропускаем проблемных пользователей
                continue
        
        if len(top_text) <= len("<b>🏦 ТОП ИГРОКОВ ПО БАНКУ</b>\n\n"):
            top_text += "🏦 <b>Недостаточно данных для составления топа</b>"
            reply_markup = None
        else:
            if buttons_added > 0:
                top_text += "\n💡 <i>Нажмите на кнопку ниже, чтобы перейти в профиль игрока</i>"
                try:
                    builder.adjust(1)  # По одной кнопке в ряд
                    reply_markup = builder.as_markup()
                except Exception as e:
                    print(f"Ошибка при создании клавиатуры топа банка: {e}")
                    reply_markup = None
            else:
                reply_markup = None
        
        # Добавляем общую статистику
        try:
            total_bank = 0
            for user in valid_users:
                try:
                    bank_val = user[1].get('bank', 0)
                    if isinstance(bank_val, (int, float)):
                        total_bank += float(bank_val)
                except (ValueError, TypeError, OverflowError):
                    continue
            if total_bank > 0:
                top_text += f"\n\n💰 <b>Общая сумма в банках:</b> <b>{format_amount(int(total_bank))} MORPH</b>"
        except Exception as e:
            print(f"Ошибка при подсчете общей суммы банков: {e}")
        
        try:
            await message.reply(
                top_text, 
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception as send_error:
            print(f"Ошибка при отправке сообщения с топом банков: {send_error}")
            import traceback
            traceback.print_exc()
            # Пытаемся отправить без кнопок
            try:
                await message.reply(
                    top_text, 
                    parse_mode="HTML"
                )
            except Exception as e2:
                print(f"Ошибка при отправке сообщения без кнопок: {e2}")
                raise
        
    except Exception as e:
        print(f"Ошибка при формировании топа банков: {e}")
        import traceback
        traceback.print_exc()
        await message.reply("❌ <b>Ошибка при формировании топа банков. Попробуйте позже.</b>", parse_mode="HTML")

# ИГРА "КУБИК"
@router.message(lambda message: message.text and message.text.lower().startswith("кубик"))
async def start_dice_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply("❌ Использование: <b>кубик [ставка/ВСЁ] [БОЛЬШЕ/МЕНЬШЕ/ЧЕТ/НЕЧЕТ/1/2/3/4/5/6]</b>\nПример: <b>кубик ВСЁ БОЛЬШЕ</b>\n🎯 Минимальная ставка: 100 MORPH", parse_mode="HTML")
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']  # ДОБАВИТЬ
        
        bet = parse_amount(parts[1], user_balance)  # ИЗМЕНИТЬ
        outcome = parts[2].upper()
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        valid_outcomes = ["БОЛЬШЕ", "МЕНЬШЕ", "ЧЕТ", "НЕЧЕТ", "1", "2", "3", "4", "5", "6"]
        if outcome not in valid_outcomes:
            await message.reply("❌ Неверный исход! Доступные: БОЛЬШЕ, МЕНЬШЕ, ЧЕТ, НЕЧЕТ, 1-6")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Отправляем анимированный кубик
        dice_msg = await message.answer_dice(emoji="🎲")
        dice_result = dice_msg.dice.value
        
        import asyncio
        await asyncio.sleep(4)
        
        # Определяем выигрыш
        won = False
        multiplier = 0
        if outcome == "БОЛЬШЕ":
            won = dice_result > 3
            multiplier = 2.0
        elif outcome == "МЕНЬШЕ":
            won = dice_result < 4
            multiplier = 2.0
        elif outcome == "ЧЕТ":
            won = dice_result % 2 == 0
            multiplier = 2.0
        elif outcome == "НЕЧЕТ":
            won = dice_result % 2 == 1
            multiplier = 2.0
        elif outcome in ["1", "2", "3", "4", "5", "6"]:
            won = str(dice_result) == outcome
            multiplier = 5.0
        
        # Определяем результат
        if won:
            won_amount = int(bet * multiplier)
            add_win_to_user(user_id, won_amount, bet)
            add_game_to_history(user_id, 'Кубик', bet, 'win', won_amount)
            result_text = f"🎉 **ПОБЕДА!**\n💰 Выигрыш: {format_amount(won_amount)} MORPH"
        else:
            add_game_to_history(user_id, 'Кубик', bet, 'lose', 0)
            result_text = f"❌ **ПРОИГРЫШ!**\n💰 Проигрыш: {format_amount(bet)} MORPH"
        
        users_data[user_id]['games_played'] += 1
        save_users()
        
        await message.reply(
            f"🎲 **ИГРА КУБИК**\n\n"
            f"🎯 Исход: {outcome}\n"
            f"📊 Коэффициент: {multiplier}x\n"
            f"🎲 Результат: {dice_result}\n\n"
            f"{result_text}"
        )
    except ValueError:
        await message.reply("❌ Неверные параметры!")

# ИГРА "ПИРАТ"
# ИГРА "ПИРАТ" - ИСПРАВЛЕННАЯ ВЕРСИЯ С ЗАЩИТОЙ ОТ ДЮПА
active_pirate_games = {}  # Добавляем словарь для отслеживания активных игр

@router.message(lambda message: message.text and message.text.lower().startswith("пират"))
async def start_pirate_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("❌ Использование: <b>пират [ставка/ВСЁ]</b>\nПример: <b>пират ВСЁ</b>\n🎯 Минимальная ставка: 100 MORPH", parse_mode="HTML")
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        # Проверяем, нет ли уже активной игры у пользователя
        if user_id in active_pirate_games:
            await message.reply("❌ У вас уже есть активная игра! Дождитесь её завершения.")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Генерируем выигрышную кнопку
        winning_button = random.randint(1, 3)
        
        # Сохраняем игру с защитой от дюпа
        active_pirate_games[user_id] = {
            'bet': bet,
            'winning_button': winning_button,
            'game_id': f"pirate_{user_id}_{int(time.time())}",  # Уникальный ID игры
            'used': False  # Флаг использования
        }
        
        # Создаем клавиатуру с тремя кнопками
        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="🏴‍☠️ Кнопка 1", callback_data=f"pirate_1_{user_id}"))
        builder.add(InlineKeyboardButton(text="🏴‍☠️ Кнопка 2", callback_data=f"pirate_2_{user_id}"))
        builder.add(InlineKeyboardButton(text="🏴‍☠️ Кнопка 3", callback_data=f"pirate_3_{user_id}"))
        
        await message.reply(
            f"🏴‍☠️ **ИГРА ПИРАТ**\n\n"
            f"💰 Ставка: {format_amount(bet)} MORPH\n"
            f"📊 Коэффициент: 2.5x\n"
            f"🎯 Выигрыш: {format_amount(int(bet * 2.5))} MORPH\n\n"
            f"Выберите кнопку:",
            reply_markup=builder.as_markup()
        )
        
    except ValueError:
        await message.reply("❌ Неверные параметры!")

# Обработка нажатий в игре "Пират" с защитой от дюпа
@router.callback_query(lambda c: c.data.startswith("pirate_"))
async def pirate_callback(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        return
    
    data = callback.data.split("_")
    if len(data) < 3:
        await callback.answer("❌ Ошибка данных!")
        return
    
    button_num = int(data[1])
    target_user_id = int(data[2])
    
    # 🔒 ЗАЩИТА: проверяем, что нажимает владелец игры
    if callback.from_user.id != target_user_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    
    # Проверяем существование игры
    if target_user_id not in active_pirate_games:
        await callback.answer("❌ Игра не найдена или уже завершена!", show_alert=True)
        return
    
    game = active_pirate_games[target_user_id]
    
    # 🔒 ЗАЩИТА: проверяем, что игра еще не использована
    if game.get('used'):
        await callback.answer("❌ Вы уже сделали выбор в этой игре!", show_alert=True)
        return
    
    # 🔒 ЗАЩИТА: отмечаем игру как использованную
    game['used'] = True
    
    bet = game['bet']
    winning_button = game['winning_button']
    
    # Удаляем игру из активных сразу после первого нажатия
    del active_pirate_games[target_user_id]
    
    if button_num == winning_button:
        # Победа
        won_amount = int(bet * 2.5)
        add_win_to_user(target_user_id, won_amount, bet)
        add_game_to_history(target_user_id, 'Пират', bet, 'win', won_amount)
        result_text = f"🎉 **ПОБЕДА!**\n💰 Выигрыш: {format_amount(won_amount)} MORPH"
        
        # Показываем все кнопки с результатами
        builder = InlineKeyboardBuilder()
        for i in range(1, 4):
            if i == winning_button:
                builder.add(InlineKeyboardButton(text="💰 ВЫИГРЫШ", callback_data="pirate_completed"))
            else:
                builder.add(InlineKeyboardButton(text="💀 ПРОИГРЫШ", callback_data="pirate_completed"))
        builder.adjust(3)
        
    else:
        # Проигрыш
        add_game_to_history(target_user_id, 'Пират', bet, 'lose', 0)
        users_data[target_user_id]['games_played'] += 1
        save_users()
        result_text = f"❌ **ПРОИГРЫШ!**\n💰 Проигрыш: {format_amount(bet)} MORPH"
        
        # Показываем все кнопки с результатами
        builder = InlineKeyboardBuilder()
        for i in range(1, 4):
            if i == winning_button:
                builder.add(InlineKeyboardButton(text="💰 ВЫИГРЫШНАЯ", callback_data="pirate_completed"))
            elif i == button_num:
                builder.add(InlineKeyboardButton(text="💀 ВАША", callback_data="pirate_completed"))
            else:
                builder.add(InlineKeyboardButton(text="💀 ПРОИГРЫШ", callback_data="pirate_completed"))
        builder.adjust(3)
    
    users_data[target_user_id]['games_played'] += 1
    save_users()
    
    await callback.message.edit_text(
        f"🏴‍☠️ **ИГРА ПИРАТ**\n\n"
        f"🎯 Выбранная кнопка: {button_num}\n"
        f"🏆 Выигрышная кнопка: {winning_button}\n\n"
        f"{result_text}",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

# Обработка завершенной игры
@router.callback_query(lambda c: c.data == "pirate_completed")
async def pirate_completed_callback(callback: CallbackQuery):
    await callback.answer("🎮 Игра завершена!")

# Автоматическая очистка зависших игр (на всякий случай)
async def cleanup_pirate_games():
    """Очистка зависших игр раз в 5 минут"""
    current_time = time.time()
    expired_games = []
    
    for user_id, game in active_pirate_games.items():
        # Если игра висит больше 10 минут - удаляем и возвращаем ставку
        if current_time - int(game['game_id'].split('_')[-1]) > 600:  # 10 минут
            expired_games.append(user_id)
            # Возвращаем ставку
            users_data[user_id]['balance'] += game['bet']
    
    for user_id in expired_games:
        del active_pirate_games[user_id]
    
    if expired_games:
        save_users()
        print(f"Очищено {len(expired_games)} зависших игр в Пирате")

# Запускаем очистку каждые 5 минут
async def pirate_cleanup_scheduler():
    while True:
        await asyncio.sleep(300)  # 5 минут
        await cleanup_pirate_games()

# Добавляем в главную функцию
async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем очистку в фоне
    asyncio.create_task(pirate_cleanup_scheduler())
    
    await dp.start_polling(bot)

# ИГРА "КНБ" (КАМЕНЬ, НОЖНИЦЫ, БУМАГА)
# Словарь для хранения активных вызовов
active_knb_challenges = {}

# СПОРТИВНЫЕ ИГРЫ (Баскетбол, Футбол, Боулинг, Дартс)
@router.message(lambda message: message.text and message.text.split()[0].lower() in ["баскетбол", "футбол", "боулинг", "дартс"])
async def start_sport_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            await message.reply("❌ Использование: <b>[игра] [ставка/ВСЁ]</b>\nПример: <b>баскетбол ВСЁ</b>\n🎯 Минимальная ставка: 100 MORPH", parse_mode="HTML")
            return
        
        sport = parts[0].lower()
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']  # ДОБАВИТЬ
        
        bet = parse_amount(parts[1], user_balance)  # ИЗМЕНИТЬ
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        users_data[user_id]['balance'] -= bet
        save_users()
        sport_dice = {
            "баскетбол": {"emoji": "🏀", "win": [4, 5], "multiplier": 2.0, "name": "Баскетбол"},
            "футбол": {"emoji": "⚽", "win": [3], "multiplier": 2.0, "name": "Футбол"},
            "боулинг": {"emoji": "🎳", "win": [6], "multiplier": 2.5, "name": "Боулинг"},
            "дартс": {"emoji": "🎯", "win": [6], "multiplier": 2.5, "name": "Дартс"}
        }
        config = sport_dice.get(sport)
        if not config:
            await message.reply("❌ Неизвестная спортивная игра!")
            return
        dice_msg = await message.answer_dice(emoji=config["emoji"])
        dice_value = dice_msg.dice.value
        import asyncio
        await asyncio.sleep(4)
        if dice_value in config["win"]:
            won_amount = int(bet * config["multiplier"])
            add_win_to_user(user_id, won_amount, bet)
            add_game_to_history(user_id, sport.capitalize(), bet, 'win', won_amount)
            users_data[user_id]['games_played'] += 1
            save_users()
            if sport == "футбол":
                result_text = f"⚽ Гол!\n+{won_amount - bet} MORPH"
            elif sport == "баскетбол":
                result_text = f"🏀 Попадание!\n+{won_amount - bet} MORPH"
            elif sport == "боулинг":
                result_text = f"🎳 Страйк!\n+{won_amount - bet} MORPH"
            elif sport == "дартс":
                result_text = f"🎯 В яблочко!\n+{won_amount - bet} MORPH"
            else:
                result_text = f"Победа!\n+{won_amount - bet} MORPH"
        else:
            add_game_to_history(user_id, sport.capitalize(), bet, 'lose', 0)
            users_data[user_id]['games_played'] += 1
            save_users()
            result_text = f"[🎯] Мимо\n[❌] Вы проиграли {bet} MORPH"
        await message.reply(result_text)
    except ValueError:
        await message.reply("❌ Неверные параметры!")

# ИГРА "БАШЕНКА" - ИСПРАВЛЕННАЯ ВЕРСИЯ
active_tower_games = {}

@router.message(lambda message: message.text and message.text.lower().startswith("башенка"))
async def start_tower_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            await message.reply(
                "🏗️ <b>БАШЕНКА</b>\n\n"
                "❌ Использование: <b>башенка [ставка/ВСЁ] [мины: 1-4]</b>\n"
                "💡 Пример: <b>башенка ВСЁ 3</b>\n"
                "🎯 Минимальная ставка: 100 MORPH\n\n"
                "🏆 <b>Правила:</b>\n"
                "• Поднимайтесь по уровням башни\n"
                "• На каждом уровне 5 клеток и мины\n"
                "• Выбирайте безопасные клетки\n"
                "• Чем выше подниметесь - тем больше выигрыш!",
                parse_mode="HTML"
            )
            return
        
        user_id = message.from_user.id
        init_user(user_id, message.from_user.username)
        user_balance = users_data[user_id]['balance']
        
        bet = parse_amount(parts[1], user_balance)
        mines_count = int(parts[2])
        
        # Проверяем ставку
        is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
        if not is_valid:
            await message.reply(error_msg)
            return
        
        if not (1 <= mines_count <= 4):
            await message.reply("❌ Количество мин должно быть от 1 до 4!")
            return
        
        # Списываем ставку
        users_data[user_id]['balance'] -= bet
        save_users()
        
        # Инициализация игры с защитой от дюпа
        active_tower_games[user_id] = {
            'bet': bet,
            'mines_count': mines_count,
            'level': 1,
            'max_level': 10,
            'opened': [],  # [(level, cell)]
            'mines': {},   # {level: [mine_positions]}
            'multiplier': 1.0,
            'cashout_used': False,
            'game_over': False,
            'game_id': f"tower_{user_id}_{int(time.time())}",
            'awaiting_next_level': False,  # Флаг ожидания перехода на следующий уровень
            'move_in_progress': False  # Блокировка повторных нажатий
        }
        
        await send_tower_level(message, user_id)
        
    except ValueError:
        await message.reply("❌ Неверные параметры! Укажите число мин от 1 до 4.")

async def send_tower_level(message_or_callback, user_id, reveal=None, win=None):
    if user_id not in active_tower_games:
        return
    
    game = active_tower_games[user_id]
    current_level = game['level']
    mines_count = game['mines_count']
    bet = game['bet']
    max_level = game['max_level']
    
    # Генерируем мины для уровня, если ещё не были
    if current_level not in game['mines']:
        mines = set()
        while len(mines) < mines_count:
            mines.add(random.randint(0, 4))
        game['mines'][current_level] = list(mines)
    
    mines = game['mines'][current_level]
    
    # Рассчитываем множитель
    opened_on_current_level = len([opened for opened in game['opened'] if opened[0] == current_level])
    base_multiplier = 5 / (5 - mines_count)
    game['multiplier'] = base_multiplier ** len(game['opened'])
    
    won_amount = int(bet * game['multiplier'])
    
    # Создаем клавиатуру
    builder = InlineKeyboardBuilder()
    
    if reveal is not None:
        # Показываем результат хода
        result_emoji = "🟩" if win else "💥"
        result_text = "БЕЗОПАСНО!" if win else "МИНА!"
        
        # Визуализация текущего уровня с результатом
        for i in range(5):
            if i == reveal:
                builder.add(InlineKeyboardButton(text=result_emoji, callback_data="tower_wait"))
            elif i in mines:
                builder.add(InlineKeyboardButton(text="💣", callback_data="tower_wait"))
            else:
                # Проверяем, была ли клетка открыта на этом уровне
                is_opened = any(level == current_level and cell == i for level, cell in game['opened'])
                builder.add(InlineKeyboardButton(text="🟩" if is_opened else "⬜", callback_data="tower_wait"))
        builder.adjust(5)
        
        if win:
            # Предлагаем действия после успешного хода
            if current_level < max_level:
                builder.row(InlineKeyboardButton(
                    text=f"🔼 Уровень {current_level + 1} (+{format_amount(won_amount)})",
                    callback_data=f"tower_next_{user_id}"
                ))
            else:
                builder.row(InlineKeyboardButton(
                    text=f"🏆 ЗАБРАТЬ {format_amount(won_amount)} MORPH",
                    callback_data=f"tower_final_{user_id}"
                ))
            
            # Кнопка забрать выигрыш всегда доступна после успешного хода
            builder.row(InlineKeyboardButton(
                text=f"💰 Забрать {format_amount(won_amount)} MORPH",
                callback_data=f"tower_cashout_{user_id}"
            ))
        else:
            # При проигрыше показываем только информацию
            builder.row(InlineKeyboardButton(
                text="🔄 Играть заново",
                callback_data=f"tower_restart_{user_id}"
            ))
    
    else:
        # Показываем поле для выбора клетки
        for i in range(5):
            # Проверяем, была ли клетка открыта на этом уровне
            is_opened = any(level == current_level and cell == i for level, cell in game['opened'])
            if is_opened:
                builder.add(InlineKeyboardButton(text="🟩", callback_data="tower_wait"))
            else:
                builder.add(InlineKeyboardButton(
                    text="⬜",
                    callback_data=f"tower_pick_{i}_{user_id}"
                ))
        builder.adjust(5)
        
        # Кнопка забрать выигрыш (только если есть прогресс)
        if len(game['opened']) > 0:
            builder.row(InlineKeyboardButton(
                text=f"💰 Забрать {format_amount(won_amount)} MORPH",
                callback_data=f"tower_cashout_{user_id}"
            ))
    
    # Текст сообщения
    if reveal is not None:
        if win:
            text = (
                f"🏗️ <b>БАШЕНКА - УРОВЕНЬ {current_level}</b>\n\n"
                f"✅ <b>{result_text}</b>\n"
                f"🎯 Клетка {reveal + 1} безопасна!\n\n"
                f"💰 Текущий выигрыш: <b>{format_amount(won_amount)} MORPH</b>\n"
                f"📈 Коэффициент: <b>{game['multiplier']:.2f}x</b>\n\n"
            )
            if current_level < max_level:
                text += f"<b>Переходим на уровень {current_level + 1}?</b>"
            else:
                text += f"<b>🏆 ВЫ ДОСТИГЛИ ВЕРШИНЫ БАШНИ!</b>"
        else:
            text = (
                f"🏗️ <b>БАШЕНКА - УРОВЕНЬ {current_level}</b>\n\n"
                f"💥 <b>{result_text}</b>\n"
                f"🎯 Клетка {reveal + 1} содержала мину!\n\n"
                f"💸 Проигрыш: <b>{format_amount(bet)} MORPH</b>\n\n"
                f"<b>Игра окончена!</b>"
            )
    else:
        text = (
            f"🏗️ <b>БАШЕНКА - УРОВЕНЬ {current_level}</b>\n\n"
            f"💣 Мин на уровне: <b>{mines_count}</b>\n"
            f"💰 Ставка: <b>{format_amount(bet)} MORPH</b>\n"
            f"📈 Коэффициент: <b>{game['multiplier']:.2f}x</b>\n"
            f"🎯 Текущий выигрыш: <b>{format_amount(won_amount)} MORPH</b>\n\n"
            f"<b>Выберите безопасную клетку:</b>\n"
            f"🟩 - безопасная клетка\n"
            f"💣 - мина\n"
            f"⬜ - неоткрытая клетка"
        )
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.reply(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    else:
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')

@router.callback_query(lambda c: c.data.startswith("tower_pick_"))
async def tower_pick_callback(callback: CallbackQuery):
    if is_banned(callback.from_user.id):
        await callback.answer("❌ Вы забанены!", show_alert=True)
        return
    
    try:
        data = callback.data.split("_")
        cell = int(data[2])
        user_id = int(data[3])
        
        # 🔒 ЗАЩИТА: проверяем владельца игры
        if callback.from_user.id != user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if user_id not in active_tower_games:
            await callback.answer("❌ Игра не найдена или завершена!", show_alert=True)
            return
        
        game = active_tower_games[user_id]
        current_level = game['level']
        
        # 🔒 ЗАЩИТА: проверяем состояние игры
        if game.get('game_over'):
            await callback.answer("❌ Игра уже завершена!", show_alert=True)
            return
        
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        if game.get('awaiting_next_level'):
            await callback.answer("❌ Завершите текущий уровень!", show_alert=True)
            return
        
        # 🔒 ЗАЩИТА: проверяем, не была ли уже нажата клетка на ТЕКУЩЕМ уровне
        if any(level == current_level and opened_cell == cell for level, opened_cell in game['opened']):
            await callback.answer("❌ Эта клетка уже открыта!", show_alert=True)
            return
        
        # Проверяем, что ход не обрабатывается
        if game.get('move_in_progress', False):
            await callback.answer("⏳ Ход уже обрабатывается, подождите!", show_alert=True)
            return
        
        # Блокируем повторные нажатия
        game['move_in_progress'] = True
        
        mines = game['mines'][current_level]
        
        # Добавляем задержку для предотвращения спама
        await callback.answer()
        
        if cell in mines:
            # Проигрыш
            game['game_over'] = True
            game['move_in_progress'] = False
            add_game_to_history(user_id, 'Башенка', game['bet'], 'lose', 0)
            users_data[user_id]['games_played'] += 1
            save_users()
            await send_tower_level(callback, user_id, reveal=cell, win=False)
            return
        
        # Успех - добавляем в открытые клетки
        game['opened'].append((current_level, cell))
        game['awaiting_next_level'] = True
        game['move_in_progress'] = False
        
        await send_tower_level(callback, user_id, reveal=cell, win=True)
        
    except Exception as e:
        print(f"Ошибка в tower_pick_callback: {e}")
        await callback.answer("❌ Произошла ошибка, попробуйте снова", show_alert=True)

@router.callback_query(lambda c: c.data.startswith("tower_next_"))
async def tower_next_callback(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[2])
        
        # 🔒 ЗАЩИТА: проверяем владельца игры
        if callback.from_user.id != user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if user_id not in active_tower_games:
            await callback.answer("❌ Игра не найдена!", show_alert=True)
            return
        
        game = active_tower_games[user_id]
        
        # 🔒 ЗАЩИТА: проверяем состояние игры
        if game.get('game_over'):
            await callback.answer("❌ Игра уже завершена!", show_alert=True)
            return
        
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        # Переходим на следующий уровень
        game['level'] += 1
        game['awaiting_next_level'] = False
        
        if game['level'] > game['max_level']:
            # Автоматический вывод при достижении максимума
            await tower_final_callback(callback)
            return
        
        await send_tower_level(callback, user_id)
        await callback.answer(f"🎯 Уровень {game['level']}!")
        
    except Exception as e:
        print(f"Ошибка в tower_next_callback: {e}")
        await callback.answer("❌ Произошла ошибка", show_alert=True)

@router.callback_query(lambda c: c.data.startswith("tower_final_"))
async def tower_final_callback(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[2])
        
        # 🔒 ЗАЩИТА: проверяем владельца игры
        if callback.from_user.id != user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if user_id not in active_tower_games:
            await callback.answer("❌ Игра не найдена!", show_alert=True)
            return
        
        game = active_tower_games[user_id]
        
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        game['cashout_used'] = True
        game['game_over'] = True
        
        bet = game['bet']
        won_amount = int(bet * game['multiplier'])
        
        # Начисляем выигрыш с обновлением лидерборда и истории
        add_win_to_user(user_id, won_amount, bet)
        add_game_to_history(user_id, 'Башенка', bet, 'win', won_amount)
        users_data[user_id]['games_played'] += 1
        save_users()
        
        await callback.message.edit_text(
            f"🏆 <b>ПОБЕДА! ВЫ ДОСТИГЛИ ВЕРШИНЫ БАШНИ!</b>\n\n"
            f"🎯 Пройдено уровней: <b>{game['level']}/{game['max_level']}</b>\n"
            f"💰 Исходная ставка: <b>{format_amount(bet)} MORPH</b>\n"
            f"📈 Финальный коэффициент: <b>{game['multiplier']:.2f}x</b>\n"
            f"🎯 Выигрыш: <b>{format_amount(won_amount)} MORPH</b>\n\n"
            f"💫 <b>Поздравляем с победой!</b>",
            parse_mode='HTML'
        )
        
        del active_tower_games[user_id]
        await callback.answer("🏆 Победа!")
        
    except Exception as e:
        print(f"Ошибка в tower_final_callback: {e}")
        await callback.answer("❌ Произошла ошибка", show_alert=True)

@router.callback_query(lambda c: c.data.startswith("tower_cashout_"))
async def tower_cashout_callback(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[2])
        
        # 🔒 ЗАЩИТА: проверяем владельца игры
        if callback.from_user.id != user_id:
            await callback.answer("❌ Это не ваша игра!", show_alert=True)
            return
        
        if user_id not in active_tower_games:
            await callback.answer("❌ Игра не найдена!", show_alert=True)
            return
        
        game = active_tower_games[user_id]
        
        # 🔒 ЗАЩИТА: проверяем состояние игры
        if game.get('game_over'):
            await callback.answer("❌ Игра уже завершена!", show_alert=True)
            return
        
        if game.get('cashout_used'):
            await callback.answer("❌ Выигрыш уже забран!", show_alert=True)
            return
        
        game['cashout_used'] = True
        game['game_over'] = True
        
        bet = game['bet']
        won_amount = int(bet * game['multiplier'])
        
        # Начисляем выигрыш с обновлением лидерборда и истории
        add_win_to_user(user_id, won_amount, bet)
        add_game_to_history(user_id, 'Башенка', bet, 'win', won_amount)
        users_data[user_id]['games_played'] += 1
        save_users()
        
        await callback.message.edit_text(
            f"💰 <b>ВЫ ЗАБРАЛИ ВЫИГРЫШ!</b>\n\n"
            f"🎯 Пройдено уровней: <b>{len(game['opened'])}</b>\n"
            f"💰 Исходная ставка: <b>{format_amount(bet)} MORPH</b>\n"
            f"📈 Коэффициент: <b>{game['multiplier']:.2f}x</b>\n"
            f"🎯 Выигрыш: <b>{format_amount(won_amount)} MORPH</b>\n\n"
            f"💫 <b>Отличный результат!</b>",
            parse_mode='HTML'
        )
        
        del active_tower_games[user_id]
        await callback.answer("💰 Выигрыш получен!")
        
    except Exception as e:
        print(f"Ошибка в tower_cashout_callback: {e}")
        await callback.answer("❌ Произошла ошибка", show_alert=True)

@router.callback_query(lambda c: c.data.startswith("tower_restart_"))
async def tower_restart_callback(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[2])
        
        if user_id in active_tower_games:
            del active_tower_games[user_id]
        
        await callback.message.edit_text(
            "🔄 <b>Игра завершена</b>\n\n"
            "💫 Начните новую игру командой:\n"
            "<code>башенка [ставка] [мины]</code>",
            parse_mode='HTML'
        )
        await callback.answer()
        
    except Exception as e:
        print(f"Ошибка в tower_restart_callback: {e}")
        await callback.answer("❌ Произошла ошибка", show_alert=True)

@router.callback_query(lambda c: c.data == "tower_wait")
async def tower_wait_callback(callback: CallbackQuery):
    await callback.answer("⏳ Ожидайте...")

# Автоматическая очистка зависших игр в башенке
async def cleanup_tower_games():
    """Очистка зависших игр в башенке"""
    current_time = time.time()
    expired_games = []
    
    for user_id, game in active_tower_games.items():
        # Если игра висит больше 10 минут - удаляем и возвращаем ставку
        game_timestamp = int(game['game_id'].split('_')[-1])
        if current_time - game_timestamp > 600:  # 10 минут
            expired_games.append(user_id)
            # Возвращаем ставку
            users_data[user_id]['balance'] += game['bet']
    
    for user_id in expired_games:
        del active_tower_games[user_id]
    
    if expired_games:
        save_users()
        print(f"Очищено {len(expired_games)} зависших игр в Башенке")

# Запускаем очистку каждые 5 минут
async def tower_cleanup_scheduler():
    while True:
        await asyncio.sleep(300)  # 5 минут
        await cleanup_tower_games()

# Добавляем в главную функцию
async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем очистку в фоне
    asyncio.create_task(tower_cleanup_scheduler())
    
    await dp.start_polling(bot)

# Команда рассчитать
@router.message(lambda message: message.text and message.text.lower().startswith('рассчитать'))
async def calculate_command(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    try:
        # Убираем слово "рассчитать" и берем остальную часть сообщения
        expression = message.text[11:].strip()
        
        if not expression:
            await message.reply(
                "🧮 <b>КАЛЬКУЛЯТОР</b>\n\n"
                "❌ Использование: <b>рассчитать [выражение]</b>\n"
                "💡 Примеры:\n"
                "• <code>рассчитать 8*1500</code>\n"
                "• <code>рассчитать 1000+500-200</code>\n"
                "• <code>рассчитать 10000/5</code>\n"
                "• <code>рассчитать 2**10</code> (возведение в степень)\n"
                "• <code>рассчитать (100+50)*3</code>\n\n"
                "🔢 <b>Поддерживаемые операции:</b>\n"
                "+ сложение, - вычитание\n"
                "* умножение, / деление\n"
                "** возведение в степень\n"
                "() скобки",
                parse_mode="HTML"
            )
            return
        
        # Заменяем запятые на точки для десятичных чисел
        expression = expression.replace(',', '.')
        
        # Безопасная проверка выражения
        allowed_chars = set('0123456789+-*/.() ')
        if not all(c in allowed_chars for c in expression):
            await message.reply(
                "❌ <b>Недопустимые символы в выражении!</b>\n\n"
                "💡 Используйте только: цифры, +, -, *, /, ., ()",
                parse_mode="HTML"
            )
            return
        
        # Вычисляем результат
        try:
            result = eval(expression)
            
            # Проверяем на специальные случаи
            if isinstance(result, (int, float)):
                if result == float('inf') or result == float('-inf'):
                    await message.reply("❌ <b>Результат слишком большой!</b>", parse_mode="HTML")
                    return
                
                # Форматируем результат
                if isinstance(result, int):
                    formatted_result = format_amount(result)
                else:
                    # Для дробных чисел ограничиваем до 2 знаков после запятой
                    formatted_result = f"{result:,.2f}".replace(',', ' ').replace('.', ',')
                
                await message.reply(
                    f"🧮 <b>РЕЗУЛЬТАТ ВЫЧИСЛЕНИЯ</b>\n\n"
                    f"📊 <b>Выражение:</b> <code>{expression}</code>\n"
                    f"✅ <b>Результат:</b> <code>{formatted_result}</code>\n\n"
                    f"💡 <b>Форматировано:</b> {formatted_result}",
                    parse_mode="HTML"
                )
            else:
                await message.reply("❌ <b>Некорректное выражение!</b>", parse_mode="HTML")
                
        except ZeroDivisionError:
            await message.reply("❌ <b>Ошибка: деление на ноль!</b>", parse_mode="HTML")
        except SyntaxError:
            await message.reply("❌ <b>Синтаксическая ошибка в выражении!</b>", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ <b>Ошибка вычисления:</b> {str(e)}", parse_mode="HTML")
            
    except Exception as e:
        await message.reply("❌ <b>Произошла ошибка при обработке команды!</b>", parse_mode="HTML")

# Альтернативные команды для калькулятора
@router.message(lambda message: message.text and message.text.lower().startswith(('посчитать', 'calc', 'калькулятор')))
async def calculate_aliases(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    # Перенаправляем на основную функцию калькулятора
    if message.text.lower().startswith('посчитать'):
        new_text = 'рассчитать' + message.text[9:]
    elif message.text.lower().startswith('calc'):
        new_text = 'рассчитать' + message.text[4:]
    else:  # калькулятор
        new_text = 'рассчитать' + message.text[11:]
    
    # Создаем новое сообщение с измененным текстом
    message.text = new_text
    await calculate_command(message)

#Бонус
# Конфигурация канала бота
BOT_CHANNEL = "@MorphOfficialChannel"  # Замените на username вашего канала
CHANNEL_ID = -1002546397194    # Замените на ID вашего канала

# Функция проверки подписки на канал
async def check_channel_subscription(user_id: int, bot: Bot) -> bool:
    """Проверяет, подписан ли пользователь на канал бота"""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

# Модифицированная команда бонуса с проверкой подписки
@router.message(lambda m: m.text and m.text.lower() == "бонус")
async def bonus_command(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    if not check_cooldown(user_id, "bonus"):
        return
    
    # Проверяем подписку на канал
    is_subscribed = await check_channel_subscription(user_id, message.bot)
    
    if not is_subscribed:
        builder = InlineKeyboardBuilder()
        builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{BOT_CHANNEL[1:]}")
        builder.button(text="✅ Я подписался", callback_data="check_subscription_bonus")
        builder.adjust(1)
        
        await message.reply(
            f"🎁 <b>ДОСТУП К БОНУСУ</b>\n\n"
            f"❌ Для получения бонуса нужно быть подписанным на наш канал!\n\n"
            f"📢 Канал: {BOT_CHANNEL}\n"
            f"💎 Там много интересного:\n"
            f"• Новые игры и обновления\n"
            f"• Эксклюзивные промокоды\n"
            f"• Турниры и конкурсы\n\n"
            f"⬇️ Подпишитесь и нажмите кнопку ниже:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return
    
    # Если подписан - выдаем бонус
    init_user(user_id, message.from_user.username)
    now = int(time.time())
    last_bonus = users_data[user_id].get('last_bonus', 0)
    
    if now - last_bonus < 86400:
        hours = int((86400 - (now - last_bonus)) // 3600)
        minutes = int(((86400 - (now - last_bonus)) % 3600) // 60)
        await message.reply(f"⏳ <b>Бонус можно получить через {hours} ч. {minutes} мин.</b>", parse_mode="HTML")
        return
    
    bonus = random.randint(500, 5000)
    users_data[user_id]['balance'] += bonus
    users_data[user_id]['last_bonus'] = now
    save_users()
    
    await message.reply(
        f"🎁 <b>БОНУС АКТИВИРОВАН!</b>\n\n"
        f"💰 <b>Получено:</b> {format_amount(bonus)} MORPH\n"
        f"💳 <b>Текущий баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"💫 Возвращайтесь завтра за новым бонусом!",
        parse_mode="HTML"
    )

# Обработка кнопки проверки подписки для бонуса
@router.callback_query(lambda c: c.data == "check_subscription_bonus")
async def check_subscription_bonus(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    is_subscribed = await check_channel_subscription(user_id, callback.bot)
    
    if not is_subscribed:
        await callback.answer("❌ Вы еще не подписались на канал!", show_alert=True)
        return
    
    # Если подписан - выдаем бонус
    init_user(user_id, callback.from_user.username)
    now = int(time.time())
    last_bonus = users_data[user_id].get('last_bonus', 0)
    
    if now - last_bonus < 86400:
        hours = int((86400 - (now - last_bonus)) // 3600)
        minutes = int(((86400 - (now - last_bonus)) % 3600) // 60)
        await callback.message.edit_text(
            f"⏳ <b>Бонус можно получить через {hours} ч. {minutes} мин.</b>",
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    bonus = random.randint(500, 5000)
    users_data[user_id]['balance'] += bonus
    users_data[user_id]['last_bonus'] = now
    save_users()
    
    await callback.message.edit_text(
        f"🎁 <b>БОНУС АКТИВИРОВАН!</b>\n\n"
        f"💰 <b>Получено:</b> {format_amount(bonus)} MORPH\n"
        f"💳 <b>Текущий баланс:</b> {format_amount(users_data[user_id]['balance'])} MORPH\n\n"
        f"💫 Возвращайтесь завтра за новым бонусом!",
        parse_mode="HTML"
    )
    await callback.answer("🎁 Бонус активирован!")

# --- Смена ника ---
@router.message(lambda m: m.text and m.text.lower().startswith("ник "))
async def change_nick(message: types.Message):
    if is_banned(message.from_user.id):
        return
    user_id = message.from_user.id
    if not check_cooldown(user_id, "nick"):
        return
    new_nick = message.text[4:].strip()
    if not new_nick or len(new_nick) > 32:
        await message.reply("❌ Введите корректный ник (до 32 символов).", parse_mode="HTML")
        return
    init_user(user_id, message.from_user.username)
    users_data[user_id]['username'] = new_nick
    save_users()
    await message.reply(f" <b>Ваш ник успешно изменён на:</b> <b>{new_nick}</b>", parse_mode="HTML")

# --- Админ-функции ---

# Бан-лист
if 'ban_list' not in users_data:
    users_data['ban_list'] = []

def is_banned(user_id):
    return user_id in banned_users

# ========== СИСТЕМА МОДЕРАЦИИ ЧАТОВ ==========

def get_moderator_rank(chat_id: int, user_id: int) -> int:
    """Получить ранг модератора в чате. 0 = не модератор"""
    if chat_id not in chat_moderators:
        return 0
    return chat_moderators[chat_id].get(user_id, 0)

def is_creator(chat_id: int, user_id: int) -> bool:
    """Проверка, является ли пользователь создателем чата"""
    return get_moderator_rank(chat_id, user_id) == 3

async def is_chat_admin_or_creator(chat_id: int, user_id: int, bot: Bot) -> bool:
    """Проверка, является ли пользователь создателем или админом чата через Telegram API"""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.id == user_id:
                # Создатель или админ с правами на баны/муты
                if admin.status == 'creator':
                    return True
                # Проверяем права админа
                if admin.status == 'administrator':
                    # Если админ может банить или ограничивать права, значит может мутить/банить
                    if admin.can_restrict_members or admin.can_ban_members:
                        return True
    except Exception as e:
        print(f"Ошибка при проверке прав администратора для пользователя {user_id} в чате {chat_id}: {e}")
    return False

def can_ban(chat_id: int, user_id: int) -> bool:
    """Проверка, может ли пользователь банить (ранг 2+)"""
    rank = get_moderator_rank(chat_id, user_id)
    return rank >= 2

async def can_ban_async(chat_id: int, user_id: int, bot: Bot) -> bool:
    """Проверка, может ли пользователь банить (ранг 2+ или админ/создатель чата)"""
    # Проверяем ранг модератора
    if can_ban(chat_id, user_id):
        return True
    # Проверяем права через Telegram API
    return await is_chat_admin_or_creator(chat_id, user_id, bot)

def can_mute(chat_id: int, user_id: int) -> bool:
    """Проверка, может ли пользователь мутить (ранг 1+)"""
    rank = get_moderator_rank(chat_id, user_id)
    return rank >= 1

async def can_mute_async(chat_id: int, user_id: int, bot: Bot) -> bool:
    """Проверка, может ли пользователь мутить (ранг 1+ или админ/создатель чата)"""
    # Проверяем ранг модератора
    if can_mute(chat_id, user_id):
        return True
    # Проверяем права через Telegram API
    return await is_chat_admin_or_creator(chat_id, user_id, bot)

def can_manage_mods(chat_id: int, user_id: int) -> bool:
    """Проверка, может ли пользователь управлять модераторами (только создатель)"""
    return is_creator(chat_id, user_id)

async def auto_detect_creator(chat_id: int, bot: Bot) -> Optional[int]:
    """Автоматически определяет создателя чата через Telegram API"""
    try:
        # Получаем список администраторов чата
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            # Ищем создателя (статус 'creator')
            if admin.status == 'creator':
                return admin.user.id
    except Exception as e:
        print(f"Ошибка при определении создателя чата {chat_id}: {e}")
    return None

async def ensure_creator_set(chat_id: int, user_id: int, bot: Bot) -> bool:
    """Убеждается, что создатель чата установлен. Если нет - определяет автоматически."""
    # Если создатель уже установлен, возвращаем True
    if chat_id in chat_moderators:
        for mod_id, rank in chat_moderators[chat_id].items():
            if rank == 3:
                return True
    
    # Пытаемся автоматически определить создателя
    creator_id = await auto_detect_creator(chat_id, bot)
    if creator_id:
        if chat_id not in chat_moderators:
            chat_moderators[chat_id] = {}
        chat_moderators[chat_id][creator_id] = 3
        save_moderators()
        return True
    
    return False

def is_muted(chat_id: int, user_id: int) -> bool:
    """Проверка, замучен ли пользователь"""
    if chat_id not in chat_mutes:
        return False
    if user_id not in chat_mutes[chat_id]:
        return False
    # Проверяем, не истек ли мут
    end_time = chat_mutes[chat_id][user_id]
    if time.time() > end_time:
        # Мут истек, удаляем
        del chat_mutes[chat_id][user_id]
        if not chat_mutes[chat_id]:
            del chat_mutes[chat_id]
        save_mutes()
        return False
    return True

def is_banned_in_chat(chat_id: int, user_id: int) -> bool:
    """Проверка, забанен ли пользователь в конкретном чате"""
    if chat_id not in chat_bans:
        return False
    return user_id in chat_bans[chat_id]

def is_vip(user_id: int) -> bool:
    """Проверка, есть ли у пользователя активная VIP подписка"""
    if user_id not in vip_subscriptions:
        return False
    end_time = vip_subscriptions[user_id]
    current_time = time.time()
    if end_time < current_time:
        # Подписка истекла, удаляем
        del vip_subscriptions[user_id]
        save_vip_subscriptions()
        return False
    return True

def get_target_user(message: types.Message, skip_words: int = 1):
    """Получить ID целевого пользователя из команды (reply, @username или ID)
    
    Args:
        message: Сообщение с командой
        skip_words: Количество слов команды, которые нужно пропустить (по умолчанию 1)
    """
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        parts = message.text.split()
        if len(parts) < skip_words + 1:
            return None
        target_arg = parts[skip_words]  # Берем аргумент после пропущенных слов
        
        if target_arg.startswith('@'):
            username = target_arg[1:]
            for user_id, user_data in users_data.items():
                if isinstance(user_id, int) and user_data.get('username') == username:
                    target = user_id
                    break
        else:
            try:
                target = int(target_arg)
            except ValueError:
                return None
    return target

# --- Команды модерации ---

# Установить создателя чата (только глобальный админ) - оставляем для ручной установки
@router.message(lambda message: message.text and (message.text.lower().startswith('setcreator') or message.text.lower().startswith('установить создателя')))
async def set_creator(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    user_id = message.from_user.id
    
    # Только глобальный админ может установить создателя
    if user_id not in ADMIN_IDS:
        await message.reply("⛔ Только администратор бота может установить создателя чата!")
        return
    
    chat_id = message.chat.id
    target = get_target_user(message)
    
    if not target:
        await message.reply("❌ Использование: <code>setcreator [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    # Инициализируем структуру чата
    if chat_id not in chat_moderators:
        chat_moderators[chat_id] = {}
    
    # Устанавливаем создателя (ранг 3)
    chat_moderators[chat_id][target] = 3
    save_moderators()
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    await message.reply(
        f"👑 <b>Создатель чата установлен!</b>\n\n"
        f"👤 Пользователь: <b>@{target_username}</b>\n"
        f"📊 Ранг: <b>3 - Создатель (все права)</b>",
        parse_mode="HTML"
    )

# Назначить модератора (только создатель)
@router.message(lambda message: message.text and (message.text.lower().startswith('setmod') or message.text.lower().startswith('назначить модератора') or message.text.lower().startswith('добавить модератора')))
async def set_moderator(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    # Только в группах
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверка прав (только создатель или глобальный админ)
    if not can_manage_mods(chat_id, user_id) and user_id not in ADMIN_IDS:
        await message.reply("⛔ Только создатель чата может назначать модераторов!")
        return
    
    parts = message.text.split()
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('назначить модератора') or message.text.lower().startswith('добавить модератора')
    
    # Если есть ответ на сообщение, ранг может быть указан после команды
    if message.reply_to_message:
        if is_russian:
            # Русская команда с ответом: "назначить модератора [ранг]"
            if len(parts) < 3:
                await message.reply(
                    "❌ <b>Использование:</b> <code>назначить модератора [ранг]</code> (ответ на сообщение)\n\n"
                    "📊 <b>Ранги:</b>\n"
                    "1️⃣ Ранг 1 - может мутить (1 час)\n"
                    "2️⃣ Ранг 2 - может мутить и банить\n"
                    "3️⃣ Ранг 3 - создатель (все права)\n\n"
                    "💡 <b>Пример:</b> Ответьте на сообщение пользователя и напишите <code>назначить модератора 1</code>",
                    parse_mode="HTML"
                )
                return
            rank_arg = parts[2]  # Ранг на третьей позиции
        else:
            # Английская команда с ответом: "setmod [ранг]"
            if len(parts) < 2:
                await message.reply(
                    "❌ <b>Использование:</b> <code>setmod [ранг]</code> (ответ на сообщение)\n\n"
                    "📊 <b>Ранги:</b>\n"
                    "1️⃣ Ранг 1 - может мутить (1 час)\n"
                    "2️⃣ Ранг 2 - может мутить и банить\n"
                    "3️⃣ Ранг 3 - создатель (все права)\n\n"
                    "💡 <b>Пример:</b> Ответьте на сообщение пользователя и напишите <code>setmod 1</code>",
                    parse_mode="HTML"
                )
                return
            rank_arg = parts[1]  # Ранг на второй позиции
    else:
        # Без ответа на сообщение
        if is_russian:
            # Русская команда: "назначить модератора [ранг] [@username/ID]"
            if len(parts) < 4:
                await message.reply(
                    "❌ <b>Использование:</b> <code>назначить модератора [ранг] [@username/ID]</code> или ответ на сообщение\n\n"
                    "📊 <b>Ранги:</b>\n"
                    "1️⃣ Ранг 1 - может мутить (1 час)\n"
                    "2️⃣ Ранг 2 - может мутить и банить\n"
                    "3️⃣ Ранг 3 - создатель (все права)\n\n"
                    "💡 <b>Пример:</b> <code>назначить модератора 1 @username</code>",
                    parse_mode="HTML"
                )
                return
            rank_arg = parts[2]  # Ранг на третьей позиции
        else:
            # Английская команда: "setmod [ранг] [@username/ID]"
            if len(parts) < 3:
                await message.reply(
                    "❌ <b>Использование:</b> <code>setmod [ранг] [@username/ID]</code> или ответ на сообщение\n\n"
                    "📊 <b>Ранги:</b>\n"
                    "1️⃣ Ранг 1 - может мутить (1 час)\n"
                    "2️⃣ Ранг 2 - может мутить и банить\n"
                    "3️⃣ Ранг 3 - создатель (все права)\n\n"
                    "💡 <b>Пример:</b> <code>setmod 1 @username</code>",
                    parse_mode="HTML"
                )
                return
            rank_arg = parts[1]  # Ранг на второй позиции
    
    try:
        rank = int(rank_arg)
        if rank not in [1, 2, 3]:
            await message.reply("❌ Ранг должен быть 1, 2 или 3!")
            return
    except ValueError:
        await message.reply("❌ Неверный ранг! Используйте число 1, 2 или 3.")
        return
    
    # Получаем целевого пользователя
    # Если есть ответ на сообщение, используем его
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        # Для русской команды нужно пропустить первые 2 слова ("назначить модератора") + ранг
        if is_russian:
            target = get_target_user(message, skip_words=3)  # Пропускаем "назначить", "модератора", ранг
        else:
            target = get_target_user(message, skip_words=2)  # Пропускаем "setmod", ранг
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>назначить модератора [ранг] [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>setmod [ранг] [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    # Инициализируем структуру чата, если её нет
    if chat_id not in chat_moderators:
        chat_moderators[chat_id] = {}
    
    # Назначаем модератора
    old_rank = chat_moderators[chat_id].get(target, 0)
    chat_moderators[chat_id][target] = rank
    save_moderators()
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    rank_names = {1: "Модератор (мут)", 2: "Модератор (мут + бан)", 3: "Создатель"}
    
    if old_rank == 0:
        await message.reply(
            f"✅ <b>Модератор назначен!</b>\n\n"
            f"👤 Пользователь: <b>@{target_username}</b>\n"
            f"📊 Ранг: <b>{rank} - {rank_names[rank]}</b>",
            parse_mode="HTML"
        )
    else:
        await message.reply(
            f"✅ <b>Ранг модератора изменен!</b>\n\n"
            f"👤 Пользователь: <b>@{target_username}</b>\n"
            f"📊 Старый ранг: {old_rank}\n"
            f"📊 Новый ранг: <b>{rank} - {rank_names[rank]}</b>",
            parse_mode="HTML"
        )

# Убрать модератора (только создатель)
@router.message(lambda message: message.text and (message.text.lower().startswith('delmod') or message.text.lower().startswith('убрать модератора') or message.text.lower().startswith('удалить модератора')))
async def del_moderator(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    if not can_manage_mods(chat_id, user_id) and user_id not in ADMIN_IDS:
        await message.reply("⛔ Только создатель чата может убирать модераторов!")
        return
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('убрать модератора') or message.text.lower().startswith('удалить модератора')
    
    # Если есть ответ на сообщение, используем его, иначе парсим команду
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        if is_russian:
            target = get_target_user(message, skip_words=2)  # Пропускаем "убрать", "модератора"
        else:
            target = get_target_user(message, skip_words=1)  # Пропускаем "delmod"
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>убрать модератора [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>delmod [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    if chat_id not in chat_moderators or target not in chat_moderators[chat_id]:
        await message.reply("❌ Этот пользователь не является модератором!")
        return
    
    # Нельзя убрать создателя
    if chat_moderators[chat_id][target] == 3:
        await message.reply("❌ Нельзя убрать создателя чата!")
        return
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    del chat_moderators[chat_id][target]
    
    # Если больше нет модераторов, удаляем чат
    if not chat_moderators[chat_id]:
        del chat_moderators[chat_id]
    
    save_moderators()
    await message.reply(f"✅ Модератор <b>@{target_username}</b> убран!", parse_mode="HTML")

# Список модераторов
@router.message(lambda message: message.text and message.text.lower() in ['modlist', 'модлист', 'список модераторов', 'модераторы', 'моды', 'админы', 'администраторы'])
async def mod_list(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, message.from_user.id, message.bot)
    
    if chat_id not in chat_moderators or not chat_moderators[chat_id]:
        await message.reply("📋 <b>Список администраторов пуст</b>\n\nВ этом чате пока нет администраторов.", parse_mode="HTML")
        return
    
    rank_names = {1: "Модератор (мут)", 2: "Модератор (мут + бан)", 3: "Создатель"}
    text = "👑 <b>СПИСОК АДМИНИСТРАТОРОВ</b>\n\n"
    
    # Сортируем по рангу (от большего к меньшему)
    sorted_mods = sorted(chat_moderators[chat_id].items(), key=lambda x: x[1], reverse=True)
    
    for mod_id, rank in sorted_mods:
        username = users_data.get(mod_id, {}).get('username', f'User{mod_id}')
        emoji = "👑" if rank == 3 else "🛡️" if rank == 2 else "⚔️"
        text += f"{emoji} <b>@{username}</b> - Ранг {rank} ({rank_names[rank]})\n"
    
    await message.reply(text, parse_mode="HTML")

# Мут пользователя (ранг 1+)
@router.message(lambda message: message.text and (message.text.lower().startswith('mute') or message.text.lower().startswith('мут') or message.text.lower().startswith('замутить')))
async def mute_user(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверяем права: модератор бота, админ бота, или админ/создатель чата через Telegram API
    has_rights = await can_mute_async(chat_id, user_id, message.bot) or user_id in ADMIN_IDS
    if not has_rights:
        await message.reply("⛔ У вас нет прав для мута!")
        return
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('мут') or message.text.lower().startswith('замутить')
    
    # Если есть ответ на сообщение, используем его, иначе парсим команду
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        if is_russian:
            target = get_target_user(message, skip_words=1)  # Пропускаем "мут" или "замутить"
        else:
            target = get_target_user(message, skip_words=1)  # Пропускаем "mute"
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>мут [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>mute [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    # Нельзя мутить самого себя
    if target == user_id:
        await message.reply("❌ Нельзя замутить самого себя!")
        return
    
    # Нельзя мутить модераторов с равным или большим рангом (только для модераторов бота)
    target_rank = get_moderator_rank(chat_id, target)
    user_rank = get_moderator_rank(chat_id, user_id)
    # Проверяем, является ли пользователь админом/создателем чата через Telegram API
    is_user_admin = await is_chat_admin_or_creator(chat_id, user_id, message.bot)
    # Если пользователь - модератор бота (не админ чата), проверяем ранги
    if user_rank > 0 and not is_user_admin:
        if target_rank > 0 and target_rank >= user_rank:
            await message.reply("❌ Нельзя замутить модератора с равным или большим рангом!")
            return
    
    # Определяем длительность мута через аргументы или по умолчанию 1 час
    parts = message.text.split()
    duration_raw = None
    if message.reply_to_message:
        # Если есть ответ на сообщение, длина может идти вторым аргументом
        if len(parts) >= 2:
            duration_raw = parts[1]
    else:
        # Без ответа: команда, цель, длительность
        if is_russian:
            # мут [@user] [длительность]
            if len(parts) >= 3:
                duration_raw = parts[3] if parts[1].startswith('@') or parts[1].isdigit() else parts[2]
        else:
            if len(parts) >= 3:
                duration_raw = parts[2]

    mute_duration = parse_duration(duration_raw) if duration_raw else 3600
    if mute_duration is None or mute_duration <= 0:
        await message.reply("❌ Неверный формат длительности! Используйте числа (в минутах) или комбинации вида 30m, 2h, 1d.")
        return
    if mute_duration > MAX_DURATION_SECONDS:
        mute_duration = MAX_DURATION_SECONDS

    end_time = time.time() + mute_duration
    
    # Ограничиваем права пользователя (не может отправлять сообщения)
    try:
        from datetime import timedelta
        until_date = datetime.now() + timedelta(seconds=mute_duration)
        await message.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target,
            permissions=types.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
            ),
            until_date=until_date
        )
    except Exception as e:
        print(f"Ошибка при ограничении прав пользователя: {e}")
    
    if chat_id not in chat_mutes:
        chat_mutes[chat_id] = {}
    
    chat_mutes[chat_id][target] = end_time
    save_mutes()
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    await message.reply(
        f"🔇 <b>Пользователь замучен!</b>\n\n"
        f"👤 Пользователь: <b>@{target_username}</b>\n"
        f"⏰ Длительность: <b>{format_duration(mute_duration)}</b>\n"
        f"🕐 Размут: <b>{datetime.fromtimestamp(end_time).strftime('%H:%M:%S')}</b>",
        parse_mode="HTML"
    )

# Размут пользователя (ранг 1+)
@router.message(lambda message: message.text and (message.text.lower().startswith('unmute') or message.text.lower().startswith('размут') or message.text.lower().startswith('размутить')))
async def unmute_user(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверяем права: модератор бота, админ бота, или админ/создатель чата через Telegram API
    has_rights = await can_mute_async(chat_id, user_id, message.bot) or user_id in ADMIN_IDS
    if not has_rights:
        await message.reply("⛔ У вас нет прав для размута!")
        return
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('размут') or message.text.lower().startswith('размутить')
    
    # Если есть ответ на сообщение, используем его, иначе парсим команду
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        if is_russian:
            target = get_target_user(message, skip_words=1)  # Пропускаем "размут" или "размутить"
        else:
            target = get_target_user(message, skip_words=1)  # Пропускаем "unmute"
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>размут [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>unmute [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    if chat_id not in chat_mutes or target not in chat_mutes[chat_id]:
        await message.reply("❌ Этот пользователь не замучен!")
        return
    
    # Восстанавливаем права пользователя
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target,
            permissions=types.ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
            )
        )
    except Exception as e:
        print(f"Ошибка при восстановлении прав пользователя: {e}")
    
    del chat_mutes[chat_id][target]
    if not chat_mutes[chat_id]:
        del chat_mutes[chat_id]
    save_mutes()
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    await message.reply(f"🔊 <b>Пользователь @{target_username} размучен!</b>", parse_mode="HTML")

# Бан пользователя в чате (ранг 2+)
@router.message(lambda message: message.text and (
    (message.text.lower().startswith('ban') and not message.text.lower().startswith('banuser')) or
    message.text.lower().startswith('бан') or
    message.text.lower().startswith('забанить')
))
async def ban_user_chat(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверяем права: модератор бота, админ бота, или админ/создатель чата через Telegram API
    has_rights = await can_ban_async(chat_id, user_id, message.bot) or user_id in ADMIN_IDS
    if not has_rights:
        await message.reply("⛔ У вас нет прав для бана!")
        return
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('бан') or message.text.lower().startswith('забанить')
    
    # Если есть ответ на сообщение, используем его, иначе парсим команду
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        if is_russian:
            target = get_target_user(message, skip_words=1)  # Пропускаем "бан" или "забанить"
        else:
            target = get_target_user(message, skip_words=1)  # Пропускаем "ban"
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>бан [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>ban [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    # Нельзя банить самого себя
    if target == user_id:
        await message.reply("❌ Нельзя забанить самого себя!")
        return
    
    # Нельзя банить модераторов с равным или большим рангом (только для модераторов бота)
    target_rank = get_moderator_rank(chat_id, target)
    user_rank = get_moderator_rank(chat_id, user_id)
    # Проверяем, является ли пользователь админом/создателем чата через Telegram API
    is_user_admin = await is_chat_admin_or_creator(chat_id, user_id, message.bot)
    # Если пользователь - модератор бота (не админ чата), проверяем ранги
    if user_rank > 0 and not is_user_admin:
        if target_rank > 0 and target_rank >= user_rank:
            await message.reply("❌ Нельзя забанить модератора с равным или большим рангом!")
            return
    
    # Бан только в этом чате (локальный бан)
    if chat_id not in chat_bans:
        chat_bans[chat_id] = []
    
    if target not in chat_bans[chat_id]:
        chat_bans[chat_id].append(target)
        save_chat_bans()
        
        # Удаляем пользователя из группы
        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=target)
        except Exception as e:
            print(f"Ошибка при бане пользователя в группе: {e}")
        
        target_username = users_data.get(target, {}).get('username', f'User{target}')
        await message.reply(f"🚫 <b>Пользователь @{target_username} забанен в этом чате и удален из группы!</b>", parse_mode="HTML")
    else:
        await message.reply("❌ Пользователь уже забанен в этом чате.")

# Разбан пользователя (ранг 2+)
@router.message(lambda message: message.text and (
    (message.text.lower().startswith('unban') and not message.text.lower().startswith('unbanuser')) or
    message.text.lower().startswith('разбан') or
    message.text.lower().startswith('разбанить')
))
async def unban_user_chat(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверяем права: модератор бота, админ бота, или админ/создатель чата через Telegram API
    has_rights = await can_ban_async(chat_id, user_id, message.bot) or user_id in ADMIN_IDS
    if not has_rights:
        await message.reply("⛔ У вас нет прав для разбана!")
        return
    
    # Определяем, какая команда использована
    is_russian = message.text.lower().startswith('разбан') or message.text.lower().startswith('разбанить')
    
    # Если есть ответ на сообщение, используем его, иначе парсим команду
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        if is_russian:
            target = get_target_user(message, skip_words=1)  # Пропускаем "разбан" или "разбанить"
        else:
            target = get_target_user(message, skip_words=1)  # Пропускаем "unban"
    
    if not target:
        if is_russian:
            await message.reply("❌ Использование: <code>разбан [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        else:
            await message.reply("❌ Использование: <code>unban [@username/ID]</code> или ответ на сообщение", parse_mode="HTML")
        return
    
    if chat_id not in chat_bans or target not in chat_bans[chat_id]:
        await message.reply("❌ Пользователь не был забанен в этом чате.")
        return
    
    # Разбаниваем в чате
    try:
        await message.bot.unban_chat_member(chat_id=chat_id, user_id=target)
    except Exception as e:
        print(f"Ошибка при разбане пользователя: {e}")
    
    chat_bans[chat_id].remove(target)
    if not chat_bans[chat_id]:
        del chat_bans[chat_id]
    save_chat_bans()
    
    target_username = users_data.get(target, {}).get('username', f'User{target}')
    await message.reply(f"✅ <b>Пользователь @{target_username} разбанен в этом чате!</b>", parse_mode="HTML")

# --- Команды для правил чата ---

# Установить правила чата (только создатель/админ)
@router.message(lambda message: message.text and message.text.lower().startswith('+правила'))
async def set_chat_rules(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Автоматически определяем создателя, если он не установлен
    await ensure_creator_set(chat_id, user_id, message.bot)
    
    # Проверка прав (только создатель или глобальный админ)
    if not can_manage_mods(chat_id, user_id) and user_id not in ADMIN_IDS:
        await message.reply("⛔ Только создатель чата может устанавливать правила!")
        return
    
    # Получаем текст правил (всё после "+правила")
    parts = message.text.split('+правила', 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "❌ <b>Использование:</b> <code>+правила [текст правил]</code>\n\n"
            "💡 <b>Пример:</b>\n"
            "<code>+правила\n"
            "1. Не использовать мат\n"
            "2. Уважать других участников\n"
            "3. Не спамить</code>",
            parse_mode="HTML"
        )
        return
    
    rules_text = parts[1].strip()
    
    # Сохраняем правила
    chat_rules[chat_id] = rules_text
    save_rules()
    
    await message.reply(
        f"✅ <b>Правила чата установлены!</b>\n\n"
        f"📋 <b>Правила:</b>\n{rules_text}",
        parse_mode="HTML"
    )

# Просмотр правил чата
@router.message(lambda message: message.text and message.text.lower() in ['правила', 'rules', 'правила чата'])
async def show_chat_rules(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.reply("❌ Эта команда работает только в группах!")
        return
    
    chat_id = message.chat.id
    
    if chat_id not in chat_rules or not chat_rules[chat_id]:
        await message.reply(
            "📋 <b>ПРАВИЛА ЧАТА</b>\n\n"
            "❌ Правила для этого чата ещё не установлены.\n\n"
            "💡 Создатель чата может установить правила командой:\n"
            "<code>+правила [текст правил]</code>",
            parse_mode="HTML"
        )
        return
    
    await message.reply(
        f"📋 <b>ПРАВИЛА ЧАТА</b>\n\n{chat_rules[chat_id]}",
        parse_mode="HTML"
    )

# Автоматическая проверка и размут пользователей
async def check_and_unmute_users():
    """Проверяет истекшие муты и размучивает пользователей"""
    current_time = time.time()
    chats_to_clean = []
    has_changes = False
    
    for chat_id, mutes in list(chat_mutes.items()):
        users_to_remove = []
        for user_id, end_time in list(mutes.items()):
            if current_time > end_time:
                users_to_remove.append(user_id)
                has_changes = True
                
                # Восстанавливаем права пользователя
                try:
                    await bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=types.ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_polls=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                            can_change_info=False,
                            can_invite_users=False,
                            can_pin_messages=False
                        )
                    )
                except Exception as e:
                    print(f"Ошибка при автоматическом размуте пользователя {user_id} в чате {chat_id}: {e}")
        
        for user_id in users_to_remove:
            del mutes[user_id]
        
        if not mutes:
            chats_to_clean.append(chat_id)
    
    for chat_id in chats_to_clean:
        del chat_mutes[chat_id]
    
    if has_changes:
        save_mutes()

# Проверка мута и бана перед обработкой сообщения в группе
async def check_mute_ban_before_message(message: types.Message) -> bool:
    """Проверяет, замучен или забанен ли пользователь. Удаляет сообщения и пользователей."""
    if message.chat.type not in ['group', 'supergroup']:
        return False
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Модераторы не могут быть замучены/забанены
    if get_moderator_rank(chat_id, user_id) > 0:
        return False
    
    # Проверяем бан (глобальный) - удаляем из группы
    if is_banned(user_id):
        try:
            # Удаляем сообщение
            await message.delete()
            # Удаляем пользователя из группы
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            print(f"Ошибка при удалении забаненного пользователя: {e}")
        return True
    
    # Проверяем мут - удаляем все сообщения
    if is_muted(chat_id, user_id):
        try:
            await message.delete()
        except Exception as e:
            print(f"Ошибка при удалении сообщения замученного пользователя: {e}")
        return True
    
    return False

# --- Админ-команды (работают по reply, юзернейму и ID) ---
@router.message(lambda message: message.text and message.text.lower().startswith('banuser'))
async def ban_user(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    # Получаем цель бана
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply('❌ Использование: banuser [@username/ID] или ответ на сообщение')
            return
        target_arg = parts[1]
        
        # Проверяем, это ID или юзернейм
        if target_arg.startswith('@'):
            username = target_arg[1:]
            # Ищем пользователя по юзернейму
            for user_id, user_data in users_data.items():
                if isinstance(user_id, int) and user_data.get('username') == username:
                    target = user_id
                    break
            if not target:
                await message.reply(f'❌ Пользователь @{username} не найден!')
                return
        else:
            try:
                target = int(target_arg)
            except ValueError:
                await message.reply('❌ Неверный ID пользователя!')
                return
    
    # Защита от само-бана
    if target == message.from_user.id:
        await message.reply('❌ Нельзя забанить самого себя!')
        return
    
    if target not in banned_users:
        banned_users.append(target)
        save_banned_users()
        username = users_data.get(target, {}).get('username', f'User{target}')
        await message.reply(f'🚫 Пользователь <b>@{username}</b> (ID: {target}) забанен.', parse_mode='HTML')
    else:
        await message.reply('Пользователь уже в бане.')

@router.message(lambda message: message.text and message.text.lower().startswith('unbanuser'))
async def unban_user(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    # Получаем цель разбана
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply('❌ Использование: unbanuser [@username/ID] или ответ на сообщение')
            return
        target_arg = parts[1]
        
        # Проверяем, это ID или юзернейм
        if target_arg.startswith('@'):
            username = target_arg[1:]
            # Ищем пользователя по юзернейму
            for user_id, user_data in users_data.items():
                if isinstance(user_id, int) and user_data.get('username') == username:
                    target = user_id
                    break
            if not target:
                await message.reply(f'❌ Пользователь @{username} не найден!')
                return
        else:
            try:
                target = int(target_arg)
            except ValueError:
                await message.reply('❌ Неверный ID пользователя!')
                return
    
    if target in banned_users:
        banned_users.remove(target)
        save_banned_users()
        username = users_data.get(target, {}).get('username', f'User{target}')
        await message.reply(f'✅ Пользователь <b>@{username}</b> (ID: {target}) разбанен.', parse_mode='HTML')
    else:
        await message.reply('Пользователь не был в бане.')

@router.message(lambda message: message.reply_to_message and message.text and message.text.upper().startswith('ВЫДАТЬ '))
async def admin_give_morph(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    if not message.reply_to_message:
        await message.reply('❌ Используйте команду в ответ на сообщение пользователя.')
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply('❌ Укажите сумму: ВЫДАТЬ [сумма]', parse_mode='HTML')
        return
    amount = parse_amount(parts[1])
    if amount is None or amount <= 0:
        await message.reply('❌ Сумма должна быть положительной.', parse_mode='HTML')
        return
    to_id = message.reply_to_message.from_user.id
    init_user(to_id, message.reply_to_message.from_user.username)
    users_data[to_id]['balance'] += amount
    save_users()
    await message.reply(f'💸 <b>Выдано {format_amount(amount)} MORPH игроку {to_id}</b>', parse_mode='HTML')

@router.message(lambda message: message.reply_to_message and message.text and message.text.upper().startswith('ЗАБРАТЬ '))
async def admin_take_morph(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    if not message.reply_to_message:
        await message.reply('❌ Используйте команду в ответ на сообщение пользователя.')
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply('❌ Укажите сумму: ЗАБРАТЬ [сумма]', parse_mode='HTML')
        return
    amount = parse_amount(parts[1])
    if amount is None or amount <= 0:
        await message.reply('❌ Сумма должна быть положительной.', parse_mode='HTML')
        return
    to_id = message.reply_to_message.from_user.id
    init_user(to_id, message.reply_to_message.from_user.username)
    users_data[to_id]['balance'] = max(0, users_data[to_id]['balance'] - amount)
    save_users()
    await message.reply(f'💰 <b>Забрано {format_amount(amount)} MORPH у игрока {to_id}</b>', parse_mode='HTML')

# Команда выдачи VIP подписки
@router.message(lambda message: message.reply_to_message and message.text and message.text.lower().startswith('+вип'))
async def admin_give_vip(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    if not message.reply_to_message:
        await message.reply('❌ Используйте команду в ответ на сообщение пользователя.')
        return
    
    target_id = message.reply_to_message.from_user.id
    init_user(target_id, message.reply_to_message.from_user.username)
    
    # Выдаем VIP подписку на месяц (30 дней)
    current_time = time.time()
    month_in_seconds = 30 * 24 * 60 * 60  # 30 дней
    end_time = current_time + month_in_seconds
    
    vip_subscriptions[target_id] = end_time
    save_vip_subscriptions()
    
    end_date = datetime.fromtimestamp(end_time).strftime('%d.%m.%Y %H:%M')
    target_username = users_data.get(target_id, {}).get('username', f'User{target_id}')
    
    await message.reply(
        f'⭐ <b>VIP подписка выдана!</b>\n\n'
        f'👤 Пользователь: <b>@{target_username}</b> (ID: {target_id})\n'
        f'⏰ Действует до: <b>{end_date}</b>\n'
        f'🎁 Теперь пользователь может устанавливать видео и GIF в качестве аватара!',
        parse_mode='HTML'
    )

# Команда обнулить MORPH пользователя
# Команда обнулить MORPH пользователя (с обнулением банка)
@router.message(lambda message: message.text and message.text.lower().startswith('обнулить'))
async def admin_reset_morph(message: types.Message):
    if is_banned(message.from_user.id):
        return
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    
    # Проверяем не "обнулить всех"
    if message.text.lower().startswith('обнулить всех'):
        return  # Это обработается другой функцией
    
    # Получаем цель обнуления
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
    else:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply('❌ Использование: обнулить [@username/ID] или ответ на сообщение')
            return
        target_arg = parts[1]
        
        # Проверяем, это ID или юзернейм
        if target_arg.startswith('@'):
            username = target_arg[1:]
            # Ищем пользователя по юзернейму
            for user_id, user_data in users_data.items():
                if isinstance(user_id, int) and user_data.get('username') == username:
                    target = user_id
                    break
            if not target:
                await message.reply(f'❌ Пользователь @{username} не найден!')
                return
        else:
            try:
                target = int(target_arg)
            except ValueError:
                await message.reply('❌ Неверный ID пользователя!')
                return
    
    # Защита от само-обнуления
    if target == message.from_user.id:
        await message.reply('❌ Нельзя обнулить самого себя!')
        return
    
    init_user(target, None)
    old_balance = users_data[target]['balance']
    old_bank = users_data[target]['bank']
    users_data[target]['balance'] = 0
    users_data[target]['bank'] = 0  # Банк тоже обнуляется
    users_data[target]['total_won'] = 0
    save_users()
    
    username = users_data[target].get('username', f'User{target}')
    await message.reply(
        f'💸 <b>Пользователь @{username} (ID: {target}) обнулен!</b>\n'
        f'💰 Было на балансе: {format_amount(old_balance)} MORPH\n'
        f'🏦 Было в банке: {format_amount(old_bank)} MORPH\n'
        f'💸 Стало: 0 MORPH (баланс + банк)',
        parse_mode='HTML'
    )

# --- ПРОМОКОДЫ ---
# Модифицированная команда промокода с проверкой подписки
@router.message(lambda message: message.text and message.text.lower().startswith('промо '))
async def activate_promocode(message: types.Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply('❌ Использование: промо [код]')
        return
    
    user_id = message.from_user.id
    code = parts[1]
    
    # Проверяем подписку на канал
    is_subscribed = await check_channel_subscription(user_id, message.bot)
    
    if not is_subscribed:
        builder = InlineKeyboardBuilder()
        builder.button(text="📢 Подписаться на канал", url=f"https://t.me/{BOT_CHANNEL[1:]}")
        builder.button(text="✅ Я подписался", callback_data=f"check_subscription_promo_{code}")
        builder.adjust(1)
        
        await message.reply(
            f"🎁 <b>АКТИВАЦИЯ ПРОМОКОДА</b>\n\n"
            f"❌ Для активации промокода нужно быть подписанным на наш канал!\n\n"
            f"📢 Канал: {BOT_CHANNEL}\n"
            f"💎 Там много интересного:\n"
            f"• Новые игры и обновления\n"
            f"• Эксклюзивные промокоды\n"
            f"• Турниры и конкурсы\n\n"
            f"⬇️ Подпишитесь и нажмите кнопку ниже:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return
    
    # Если подписан - активируем промокод
    if code not in promocodes:
        await message.reply('❌ Промокод не найден!')
        return
    
    promo = promocodes[code]
    if user_id in promo['used']:
        await message.reply('❌ Вы уже активировали этот промокод!')
        return
    
    if promo['activations'] <= 0:
        await message.reply('❌ Промокод больше не активен!')
        return
    
    init_user(user_id, message.from_user.username)
    users_data[user_id]['balance'] += promo['amount']
    promo['activations'] -= 1
    promo['used'].append(user_id)
    save_users()
    save_promocodes()
    
    await message.reply(
        f'🎁 <b>ПРОМОКОД АКТИВИРОВАН!</b>\n\n'
        f'💰 <b>Получено:</b> {format_amount(promo["amount"])} MORPH\n'
        f'💳 <b>Текущий баланс:</b> {format_amount(users_data[user_id]["balance"])} MORPH',
        parse_mode="HTML"
    )

# Обработка кнопки проверки подписки для промокода
@router.callback_query(lambda c: c.data.startswith('check_subscription_promo_'))
async def check_subscription_promo(callback: CallbackQuery):
    code = callback.data.split('_')[3]
    user_id = callback.from_user.id
    
    is_subscribed = await check_channel_subscription(user_id, callback.bot)
    
    if not is_subscribed:
        await callback.answer("❌ Вы еще не подписались на канал!", show_alert=True)
        return
    
    # Если подписан - активируем промокод
    if code not in promocodes:
        await callback.message.edit_text('❌ Промокод не найден!')
        await callback.answer()
        return
    
    promo = promocodes[code]
    if user_id in promo['used']:
        await callback.message.edit_text('❌ Вы уже активировали этот промокод!')
        await callback.answer()
        return
    
    if promo['activations'] <= 0:
        await callback.message.edit_text('❌ Промокод больше не активен!')
        await callback.answer()
        return
    
    init_user(user_id, callback.from_user.username)
    users_data[user_id]['balance'] += promo['amount']
    promo['activations'] -= 1
    promo['used'].append(user_id)
    save_users()
    save_promocodes()
    
    await callback.message.edit_text(
        f'🎁 <b>ПРОМОКОД АКТИВИРОВАН!</b>\n\n'
        f'💰 <b>Получено:</b> {format_amount(promo["amount"])} MORPH\n'
        f'💳 <b>Текущий баланс:</b> {format_amount(users_data[user_id]["balance"])} MORPH',
        parse_mode="HTML"
    )
    await callback.answer("🎁 Промокод активирован!")

# Команда создания промокода
@router.message(lambda message: message.text and message.text.lower().startswith('создать промо '))
async def create_promocode(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply('⛔ Нет прав!')
        return
    parts = message.text.split()
    if len(parts) != 5:
        await message.reply('Использование: создать промо [код] [сумма] [кол-во активаций]')
        return
    _, _, code, amount, activations = parts
    try:
        amount = int(amount)
        activations = int(activations)
        if amount <= 0 or activations <= 0:
            raise ValueError
    except:
        await message.reply('Сумма и количество активаций должны быть положительными числами!')
        return
    promocodes[code] = {'amount': amount, 'activations': activations, 'used': []}
    save_promocodes()
    await message.reply(f'Промокод <b>{code}</b> создан! Сумма: {format_amount(amount)} MORPH, активаций: {activations}', parse_mode='HTML')


# --- БЛЭКДЖЕК ---
active_blackjack_games = {}

CARD_SUITS = ['♠️', '♥️', '♦️', '♣️']
CARD_VALUES = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
CARD_EMOJIS = {
    'A': '🅰️', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣', '5': '5️⃣', '6': '6️⃣', '7': '7️⃣', '8': '8️⃣', '9': '9️⃣', '10': '🔟', 'J': '🃏', 'Q': '👸', 'K': '🤴'
}
SUIT_EMOJIS = {'♠️': '♠️', '♥️': '♥️', '♦️': '♦️', '♣️': '♣️'}

def draw_card(deck):
    card = deck.pop()
    return card

def get_card_value(card, ace_high=True):
    value = card[0]
    if value in ['J', 'Q', 'K']:
        return 10
    if value == 'A':
        return 11 if ace_high else 1
    return int(value)

def hand_value(hand):
    total = 0
    aces = 0
    for card in hand:
        if card[0] == 'A':
            aces += 1
        total += get_card_value(card)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def format_hand(hand, hide_first=False):
    if hide_first:
        return '🂠 ' + ' '.join([f"{v}{s}" for v, s in hand[1:]])
    return ' '.join([f"{v}{s}" for v, s in hand])

@router.message(lambda message: message.text and (message.text.lower().startswith('блэкджек') or message.text.lower().startswith('бж')))
async def start_blackjack(message: types.Message):
    if is_banned(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply('❌ Использование: <b>блэкджек [ставка/ВСЁ]</b>\nПример: <b>блэкджек ВСЁ</b>\n🎯 Минимальная ставка: 100 MORPH', parse_mode="HTML")
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_balance = users_data[user_id]['balance']  # ДОБАВИТЬ
    
    bet = parse_amount(parts[1], user_balance)  # ИЗМЕНИТЬ
    
    # Проверяем ставку
    is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
    if not is_valid:
        await message.reply(error_msg)
        return
    users_data[user_id]['balance'] -= bet
    save_users()
    # Создаём колоду и сдаём карты
    deck = [(v, s) for v in CARD_VALUES for s in CARD_SUITS]
    random.shuffle(deck)
    player_hand = [draw_card(deck), draw_card(deck)]
    dealer_hand = [draw_card(deck), draw_card(deck)]
    active_blackjack_games[user_id] = {
        'deck': deck,
        'player': player_hand,
        'dealer': dealer_hand,
        'bet': bet,
        'finished': False,
        'move_in_progress': False
    }
    await send_blackjack_state(message, user_id)

def get_blackjack_result(player, dealer):
    player_val = hand_value(player)
    dealer_val = hand_value(dealer)
    if player_val > 21:
        return 'lose'
    if dealer_val > 21:
        return 'win'
    if player_val > dealer_val:
        return 'win'
    if player_val < dealer_val:
        return 'lose'
    return 'draw'

async def send_blackjack_state(message_or_callback, user_id, reveal_dealer=False, final=False):
    # Проверяем, что игра существует
    if user_id not in active_blackjack_games:
        return
    
    game = active_blackjack_games[user_id]
    
    # Проверяем, что игра не завершена
    if game.get('finished', False) and not final:
        return
    
    player = game['player']
    dealer = game['dealer']
    bet = game['bet']
    text = f"<b>🃏 БЛЭКДЖЕК</b>\n\n"
    text += f"Ваша рука: {format_hand(player)}  <b>({hand_value(player)})</b>\n"
    if reveal_dealer or final:
        text += f"Крупье: {format_hand(dealer)}  <b>({hand_value(dealer)})</b>\n"
    else:
        text += f"Крупье: {format_hand(dealer, hide_first=True)}\n"
    if not final:
        if hand_value(player) == 21:
            text += '\n<b>У вас БЛЭКДЖЕК!</b>'
        elif hand_value(player) > 21:
            text += '\n❌ Перебор! Вы проиграли.'
        else:
            text += '\nВыберите действие:'
    else:
        result = get_blackjack_result(player, dealer)
        if result == 'win':
            win_amount = int(bet * 2)
            add_win_to_user(user_id, win_amount, bet)
            add_game_to_history(user_id, 'Блэкджек', bet, 'win', win_amount)
            users_data[user_id]['games_played'] += 1
            save_users()
            text += f"\n\n🎉 <b>Вы выиграли!</b> +{format_amount(win_amount)} MORPH"
        elif result == 'draw':
            users_data[user_id]['balance'] += bet
            add_game_to_history(user_id, 'Блэкджек', bet, 'draw', bet)
            users_data[user_id]['games_played'] += 1
            save_users()
            text += f"\n\n🤝 <b>Ничья!</b> Ставка возвращена."
        else:
            add_game_to_history(user_id, 'Блэкджек', bet, 'lose', 0)
            users_data[user_id]['games_played'] += 1
            save_users()
            text += f"\n\n❌ <b>Вы проиграли!</b>"
        
        # Помечаем игру как завершенную
        game['finished'] = True
        
        # Удаляем игру после небольшой задержки
        await asyncio.sleep(0.5)
        if user_id in active_blackjack_games:
            del active_blackjack_games[user_id]
    builder = InlineKeyboardBuilder()
    if not final and hand_value(player) < 21:
        builder.button(text='➕ Взять', callback_data=f'blackjack_hit_{user_id}')
        builder.button(text='🛑 Стоп', callback_data=f'blackjack_stand_{user_id}')
        builder.adjust(2)
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.reply(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    else:
        await message_or_callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')

@router.callback_query(lambda c: c.data.startswith('blackjack_hit_'))
async def blackjack_hit_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[-1])
    if user_id != callback.from_user.id:
        await callback.answer('❌ Это не ваша игра!', show_alert=True)
        return
    if user_id not in active_blackjack_games:
        await callback.answer('❌ Игра не найдена!', show_alert=True)
        return
    game = active_blackjack_games[user_id]
    
    # Проверяем, что игра не завершена
    if game.get('finished', False):
        await callback.answer('❌ Игра уже завершена!', show_alert=True)
        return
    
    # Проверяем, что ход не обрабатывается
    if game.get('move_in_progress', False):
        await callback.answer('⏳ Ход уже обрабатывается!', show_alert=True)
        return
    
    # Блокируем повторные нажатия
    game['move_in_progress'] = True
    
    card = draw_card(game['deck'])
    game['player'].append(card)
    if hand_value(game['player']) >= 21:
        game['finished'] = True
        await send_blackjack_state(callback, user_id, reveal_dealer=True, final=True)
    else:
        game['move_in_progress'] = False
        await send_blackjack_state(callback, user_id)
    await callback.answer()

@router.callback_query(lambda c: c.data.startswith('blackjack_stand_'))
async def blackjack_stand_callback(callback: CallbackQuery):
    user_id = int(callback.data.split('_')[-1])
    if user_id != callback.from_user.id:
        await callback.answer('❌ Это не ваша игра!', show_alert=True)
        return
    if user_id not in active_blackjack_games:
        await callback.answer('❌ Игра не найдена!', show_alert=True)
        return
    game = active_blackjack_games[user_id]
    
    # Проверяем, что игра не завершена
    if game.get('finished', False):
        await callback.answer('❌ Игра уже завершена!', show_alert=True)
        return
    
    # Проверяем, что ход не обрабатывается
    if game.get('move_in_progress', False):
        await callback.answer('⏳ Ход уже обрабатывается!', show_alert=True)
        return
    
    # Помечаем игру как завершенную
    game['finished'] = True
    
    # Крупье добирает карты по правилам
    while hand_value(game['dealer']) < 17:
        game['dealer'].append(draw_card(game['deck']))
    await send_blackjack_state(callback, user_id, reveal_dealer=True, final=True)
    await callback.answer()

# Основная функция запуска
async def main():
    load_all_data()
    dp.include_router(router)
    await dp.start_polling(bot)

@router.message(lambda message: message.text and message.text.lower().startswith('флип'))
async def flip_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply('❌ Использование: <b>флип [ставка/ВСЁ] орел/решка</b>\nПример: <b>флип ВСЁ орел</b>\n🎯 Минимальная ставка: 100 MORPH', parse_mode="HTML")
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_balance = users_data[user_id]['balance']  # ДОБАВИТЬ
    
    bet = parse_amount(parts[1], user_balance)  # ИЗМЕНИТЬ
    choice = parts[2].lower()
    
    # Проверяем ставку
    is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
    if not is_valid:
        await message.reply(error_msg)
        return
    
    # Поддержка сокращений
    if choice in ['о', 'орёл', 'орел']:
        choice = 'орел'
    elif choice in ['р', 'решка']:
        choice = 'решка'
    else:
        await message.reply('❌ Выберите: орел (О) или решка (Р)')
        return
    users_data[user_id]['balance'] -= bet
    save_users()
    result = random.choice(['орел', 'решка'])
    win = (choice == result)
    if win:
        win_amount = bet * 2
        add_win_to_user(user_id, win_amount, bet)
        add_game_to_history(user_id, 'Флип', bet, 'win', win_amount)
        users_data[user_id]['games_played'] += 1
        save_users()
        await message.reply(f'🪙 Флип: {result.capitalize()}!\n🎉 Победа! +{format_amount(win_amount)} MORPH')
    else:
        add_game_to_history(user_id, 'Флип', bet, 'lose', 0)
        users_data[user_id]['games_played'] += 1
        save_users()
        await message.reply(f'🪙 Флип: {result.capitalize()}!\n❌ Проигрыш: {format_amount(bet)} MORPH')

@router.message(lambda message: message.text and (message.text.lower().startswith('блэкджек') or message.text.lower().startswith('бж')))
async def start_blackjack(message: types.Message):
    if is_banned(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply('❌ Использование: <b>блэкджек [ставка]</b>\nПример: <b>блэкджек 1000</b>\n🎯 Минимальная ставка: 100 MORPH', parse_mode="HTML")
        return
    bet = parse_amount(parts[1])
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Проверяем ставку
    is_valid, error_msg = check_bet_amount(bet, users_data[user_id]['balance'])
    if not is_valid:
        await message.reply(error_msg)
        return
    users_data[user_id]['balance'] -= bet
    save_users()
    # Создаём колоду и сдаём карты
    deck = [(v, s) for v in CARD_VALUES for s in CARD_SUITS]
    random.shuffle(deck)
    player_hand = [draw_card(deck), draw_card(deck)]
    dealer_hand = [draw_card(deck), draw_card(deck)]
    active_blackjack_games[user_id] = {
        'deck': deck,
        'player': player_hand,
        'dealer': dealer_hand,
        'bet': bet,
        'finished': False,
        'move_in_progress': False
    }
    await send_blackjack_state(message, user_id)

# ==================== НОВОЕ ОБНОВЛЕНИЕ ====================

# Вспомогательные функции для новых функций
def save_last_game(user_id: int, command: str, bet: int, params: dict = None):
    """Сохраняет данные последней игры"""
    last_game_data[user_id] = {
        'command': command,
        'bet': bet,
        'params': params or {},
        'timestamp': time.time()
    }

def add_game_to_history(user_id: int, game_name: str, bet: int, result: str, amount: int = 0):
    """Добавляет игру в историю пользователя"""
    if user_id not in user_game_history:
        user_game_history[user_id] = []
    
    game_entry = {
        'game': game_name,
        'bet': bet,
        'result': result,  # 'win', 'lose', 'draw'
        'amount': amount,  # итоговая сумма (выигрыш или 0)
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    user_game_history[user_id].append(game_entry)
    
    # Оставляем только последние 50 игр (чтобы не перегружать)
    if len(user_game_history[user_id]) > 50:
        user_game_history[user_id] = user_game_history[user_id][-50:]
    
    save_game_history()

def track_user_action(user_id: int):
    """Отслеживает действия пользователя для ежедневного бонуса"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    if user_id not in user_daily_actions:
        user_daily_actions[user_id] = {'count': 0, 'date': today}
    
    # Если новый день, сбрасываем счетчик
    if user_daily_actions[user_id]['date'] != today:
        user_daily_actions[user_id] = {'count': 0, 'date': today}
    
    # Увеличиваем счетчик
    user_daily_actions[user_id]['count'] += 1
    
    # Если достигли 3 действий, выдаем бонус
    if user_daily_actions[user_id]['count'] == 3:
        if user_id in users_data:
            bonus = 5000
            users_data[user_id]['balance'] += bonus
            save_users()
            
            # Отправляем уведомление (асинхронно через задачу)
            asyncio.create_task(send_activity_bonus_notification(user_id, bonus))

async def send_activity_bonus_notification(user_id: int, bonus: int):
    """Отправляет уведомление о получении бонуса за активность"""
    try:
        await bot.send_message(
            user_id,
            f'🎁 <b>Бонус за активность!</b>\n\n'
            f'Вы выполнили 3 действия сегодня!\n'
            f'💰 +{format_amount(bonus)} MORPH',
            parse_mode="HTML"
        )
    except:
        pass  # Пользователь заблокировал бота или ошибка

# 🎮 1. ИГРА "ТРИ СОКРОВИЩА"
@router.message(lambda message: message.text and message.text.lower().startswith(('БРБРПАТАПИМАЛОЛКЕК')))
async def start_treasures_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply(
            '🎁 <b>ТРИ СОКРОВИЩА</b>\n\n'
            '❌ Использование: <b>сокровища [ставка/ВСЁ]</b>\n'
            '💡 Пример: <b>сокровища 1000</b>\n'
            '🎯 Минимальная ставка: 100 MORPH\n\n'
            '📖 <b>Правила:</b>\n'
            '• Выберите один из 3 сундуков\n'
            '• Можете выиграть x2, проиграть или получить редкий бонус x5!',
            parse_mode="HTML"
        )
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_balance = users_data[user_id]['balance']
    
    bet = parse_amount(parts[1], user_balance)
    is_valid, error_msg = check_bet_amount(bet, user_balance)
    if not is_valid:
        await message.reply(error_msg)
        return
    
    users_data[user_id]['balance'] -= bet
    save_users()
    
    # Сохраняем для команды "повторить"
    save_last_game(user_id, 'сокровища', bet)
    
    # Инициализируем игру
    active_treasure_games[user_id] = {'finished': False, 'bet': bet}
    
    # Увеличиваем счетчик действий
    track_user_action(user_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Сундук 1", callback_data=f"treasure_{user_id}_1_{bet}")
    builder.button(text="📦 Сундук 2", callback_data=f"treasure_{user_id}_2_{bet}")
    builder.button(text="📦 Сундук 3", callback_data=f"treasure_{user_id}_3_{bet}")
    builder.adjust(3)
    
    await message.reply(
        f'🎁 <b>ТРИ СОКРОВИЩА</b>\n\n'
        f'💰 Ставка: {format_amount(bet)} MORPH\n\n'
        f'📦 Выберите один из сундуков:',
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@router.callback_query(lambda c: c.data.startswith('treasure_'))
async def treasure_callback(callback: CallbackQuery):
    parts = callback.data.split('_')
    user_id = int(parts[1])
    chest_num = int(parts[2])
    bet = int(parts[3])
    
    if user_id != callback.from_user.id:
        await callback.answer('❌ Это не ваша игра!', show_alert=True)
        return
    
    # Проверяем, что игра существует и не завершена
    if user_id not in active_treasure_games:
        await callback.answer('❌ Игра не найдена!', show_alert=True)
        return
    
    game = active_treasure_games[user_id]
    
    # Проверяем, что игра не завершена
    if game.get('finished', False):
        await callback.answer('❌ Игра уже завершена!', show_alert=True)
        return
    
    # Проверяем, что ставка совпадает
    if game.get('bet') != bet:
        await callback.answer('❌ Неверная ставка!', show_alert=True)
        return
    
    # Помечаем игру как завершенную перед обработкой
    game['finished'] = True
    
    # Результат: 40% проигрыш, 50% x2, 10% x5 (редкий бонус)
    rand = random.random()
    if rand < 0.4:
        result = 'lose'
        multiplier = 0
        win_amount = 0
    elif rand < 0.9:
        result = 'win'
        multiplier = 2
        win_amount = bet * multiplier
    else:
        result = 'jackpot'
        multiplier = 5
        win_amount = bet * multiplier
    
    if result == 'lose':
        users_data[user_id]['games_played'] += 1
        text = f'📦 <b>Сундук {chest_num}</b>\n\n❌ Пусто! Вы проиграли {format_amount(bet)} MORPH'
        add_game_to_history(user_id, 'Три Сокровища', bet, 'lose', 0)
    elif result == 'win':
        add_win_to_user(user_id, win_amount, bet)
        users_data[user_id]['games_played'] += 1
        text = f'📦 <b>Сундук {chest_num}</b>\n\n🎉 Выигрыш x{multiplier}!\n💰 +{format_amount(win_amount)} MORPH'
        add_game_to_history(user_id, 'Три Сокровища', bet, 'win', win_amount)
    else:
        add_win_to_user(user_id, win_amount, bet)
        users_data[user_id]['games_played'] += 1
        text = f'📦 <b>Сундук {chest_num}</b>\n\n🎁✨ РЕДКИЙ БОНУС! ✨🎁\n💰 +{format_amount(win_amount)} MORPH (x{multiplier})'
        add_game_to_history(user_id, 'Три Сокровища', bet, 'win', win_amount)
    
    save_users()
    
    # Добавляем кнопки обратной связи
    builder = InlineKeyboardBuilder()
    builder.button(text="👍", callback_data=f"feedback_like_{user_id}")
    builder.button(text="👎", callback_data=f"feedback_dislike_{user_id}")
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    # Удаляем игру из активных после небольшой задержки
    await asyncio.sleep(0.5)
    if user_id in active_treasure_games:
        del active_treasure_games[user_id]
    
    await callback.answer()

# 🎲 2. ИГРА "РОВНЫЙ ШАНС"
@router.message(lambda message: message.text and message.text.lower().startswith('ошещцщцишжегор45789784383480943'))
async def start_even_chance(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        await message.reply(
            '🎲 <b>РОВНЫЙ ШАНС</b>\n\n'
            '❌ Использование: <b>ровно [ставка/ВСЁ]</b>\n'
            '💡 Пример: <b>ровно 1000</b>\n'
            '🎯 Минимальная ставка: 100 MORPH\n\n'
            '📖 <b>Правила:</b>\n'
            '• 50% шанс выиграть x2\n'
            '• 45% шанс проиграть\n'
            '• 5% шанс выиграть x3!',
            parse_mode="HTML"
        )
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    user_balance = users_data[user_id]['balance']
    
    bet = parse_amount(parts[1], user_balance)
    is_valid, error_msg = check_bet_amount(bet, user_balance)
    if not is_valid:
        await message.reply(error_msg)
        return
    
    users_data[user_id]['balance'] -= bet
    save_users()
    
    # Сохраняем для команды "повторить"
    save_last_game(user_id, 'ровно', bet)
    
    # Увеличиваем счетчик действий
    track_user_action(user_id)
    
    # Результат: 50% x2, 45% проигрыш, 5% x3
    rand = random.random()
    if rand < 0.5:
        result = 'win_x2'
        multiplier = 2
        win_amount = bet * multiplier
    elif rand < 0.95:
        result = 'lose'
        multiplier = 0
        win_amount = 0
    else:
        result = 'win_x3'
        multiplier = 3
        win_amount = bet * multiplier
    
    if result == 'lose':
        users_data[user_id]['games_played'] += 1
        text = f'🎲 <b>РОВНЫЙ ШАНС</b>\n\n❌ Проигрыш!\n💰 -{format_amount(bet)} MORPH'
        add_game_to_history(user_id, 'Ровный Шанс', bet, 'lose', 0)
    elif result == 'win_x2':
        add_win_to_user(user_id, win_amount, bet)
        users_data[user_id]['games_played'] += 1
        text = f'🎲 <b>РОВНЫЙ ШАНС</b>\n\n🎉 Победа x{multiplier}!\n💰 +{format_amount(win_amount)} MORPH'
        add_game_to_history(user_id, 'Ровный Шанс', bet, 'win', win_amount)
    else:
        add_win_to_user(user_id, win_amount, bet)
        users_data[user_id]['games_played'] += 1
        text = f'🎲 <b>РОВНЫЙ ШАНС</b>\n\n🎁 Удача! Победа x{multiplier}!\n💰 +{format_amount(win_amount)} MORPH'
        add_game_to_history(user_id, 'Ровный Шанс', bet, 'win', win_amount)
    
    save_users()
    
    # Добавляем кнопки обратной связи
    builder = InlineKeyboardBuilder()
    builder.button(text="👍", callback_data=f"feedback_like_{user_id}")
    builder.button(text="👎", callback_data=f"feedback_dislike_{user_id}")
    
    await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# ⚡ 3. КОМАНДА "ПОВТОРИТЬ"
@router.message(lambda message: message.text and message.text.lower() in ['повторить', 'repeat', 'ре'])
async def repeat_last_game(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    if user_id not in last_game_data:
        await message.reply('❌ У вас нет последней игры для повторения!')
        return
    
    last_game = last_game_data[user_id]
    command = last_game['command']
    bet = last_game['bet']
    
    # Проверяем баланс
    if users_data[user_id]['balance'] < bet:
        await message.reply(f'❌ Недостаточно средств! Нужно {format_amount(bet)} MORPH')
        return
    
    # Повторяем игру
    if command == 'сокровища':
        # Создаем новое сообщение с командой
        message.text = f"сокровища {bet}"
        await start_treasures_game(message)
    elif command == 'ровно':
        # Создаем новое сообщение с командой
        message.text = f"ровно {bet}"
        await start_even_chance(message)
    else:
        await message.reply(f'❌ Игра "{command}" пока не поддерживает повтор')

# 🏆 КОМАНДА "ЛИДЕРБОРД" - Топ игроков по выигранным морфам за день
@router.message(lambda message: message.text and message.text.lower() in ['лидерборд', 'leaderboard', 'топ дня', 'топ за день'])
async def show_leaderboard(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Проверяем, не новый ли день
    current_date = datetime.now().strftime('%Y-%m-%d')
    global leaderboard_date, daily_leaderboard
    
    if leaderboard_date != current_date:
        daily_leaderboard = {}
        leaderboard_date = current_date
        save_leaderboard()
    
    if not daily_leaderboard:
        await message.reply(
            '🏆 <b>ЕЖЕДНЕВНЫЙ ЛИДЕРБОРД</b>\n\n'
            '📊 Пока нет данных за сегодня.\n'
            'Начните играть, чтобы попасть в лидерборд!\n\n'
            '💰 <b>Награды:</b>\n'
            '🥇 1 место: 500.000 MORPH\n'
            '🥈 2 место: 250.000 MORPH\n'
            '🥉 3 место: 125.000 MORPH\n'
            '4️⃣ 4 место: 75.000 MORPH\n'
            '5️⃣ 5 место: 50.000 MORPH\n\n'
            '⏰ Обновление в 00:00',
            parse_mode="HTML"
        )
        return
    
    # Сортируем по выигранным морфам
    sorted_players = sorted(daily_leaderboard.items(), key=lambda x: x[1], reverse=True)
    
    text = '🏆 <b>ЕЖЕДНЕВНЫЙ ЛИДЕРБОРД</b>\n\n'
    text += f'📅 Дата: <b>{leaderboard_date}</b>\n\n'
    
    # Показываем топ-10
    for i, (uid, won_amount) in enumerate(sorted_players[:10], 1):
        username = f'Игрок {uid}'
        if uid in users_data:
            username = users_data[uid].get('username', f'Игрок {uid}')
            if not username or not isinstance(username, str):
                username = f'Игрок {uid}'
            if username.startswith('@'):
                username = username[1:]
        
        # Экранируем HTML символы в username
        username = username.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # Эмодзи для первых трех мест
        if i == 1:
            emoji = "🥇"
        elif i == 2:
            emoji = "🥈"
        elif i == 3:
            emoji = "🥉"
        else:
            emoji = f"{i}."
        
        # Проверяем, что won_amount - число
        if not isinstance(won_amount, (int, float)):
            won_amount = 0
        won_amount = int(won_amount)
        
        text += f'{emoji} <b>{username}</b>: <b>{format_amount(won_amount)} MORPH</b>\n'
    
    text += '\n💰 <b>Награды:</b>\n'
    text += '🥇 1 место: 500.000 MORPH\n'
    text += '🥈 2 место: 250.000 MORPH\n'
    text += '🥉 3 место: 125.000 MORPH\n'
    text += '4️⃣ 4 место: 75.000 MORPH\n'
    text += '5️⃣ 5 место: 50.000 MORPH\n\n'
    text += '⏰ Обновление в 00:00'
    
    # Показываем позицию пользователя, если он в топе
    user_position = None
    for pos, (uid, _) in enumerate(sorted_players, 1):
        if uid == user_id:
            user_position = pos
            break
    
    if user_position:
        user_won = daily_leaderboard[user_id]
        text += f'\n\n👤 <b>Ваша позиция:</b> {user_position}. Выиграно: <b>{format_amount(user_won)} MORPH</b>'
    
    await message.reply(text, parse_mode="HTML")

# 📜 КОМАНДА "ЛАСТ" - История последних игр
@router.message(lambda message: message.text and message.text.lower() in ['ласт', 'last', 'история'])
async def show_game_history(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    if user_id not in user_game_history or len(user_game_history[user_id]) == 0:
        await message.reply(
            '📜 <b>ИСТОРИЯ ИГР</b>\n\n'
            '❌ У вас пока нет сыгранных игр.\n'
            'Начните играть, чтобы увидеть историю!',
            parse_mode="HTML"
        )
        return
    
    # Берем последние 10 игр
    history = user_game_history[user_id][-10:]
    history.reverse()  # Показываем от новых к старым
    
    text = '📜 <b>ПОСЛЕДНИЕ 10 ИГР</b>\n\n'
    
    for i, game in enumerate(history, 1):
        game_name = game.get('game', 'Неизвестная игра')
        bet = game.get('bet', 0)
        result = game.get('result', 'unknown')
        amount = game.get('amount', 0)
        game_time = game.get('time', '')
        
        # Эмодзи для результата
        if result == 'win':
            result_emoji = '✅'
            result_text = f'+{format_amount(amount)} MORPH'
        elif result == 'lose':
            result_emoji = '❌'
            result_text = f'-{format_amount(bet)} MORPH'
        else:
            result_emoji = '🤝'
            result_text = 'Ничья'
        
        # Форматируем время (только время, без даты для краткости)
        if game_time:
            try:
                time_only = game_time.split(' ')[1] if ' ' in game_time else game_time
            except:
                time_only = game_time
        else:
            time_only = ''
        
        text += f'{i}. {result_emoji} <b>{game_name}</b>\n'
        text += f'   Ставка: {format_amount(bet)} → {result_text}\n'
        if time_only:
            text += f'   🕒 {time_only}\n'
        text += '\n'
    
    await message.reply(text, parse_mode="HTML")

# 🎯 4. КОМАНДА "ЧТО ПОИГРАТЬ"
@router.message(lambda message: message.text and message.text.lower() in ['что поиграть', 'что играть', 'рекомендации'])
async def game_recommendations(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    
    # Список рекомендуемых игр
    recommendations = [
        "🎁 <b>Три Сокровища</b> - сокровища [ставка]\n   Быстрая игра с шансом на редкий бонус!",
        "🎲 <b>Ровный Шанс</b> - ровно [ставка]\n   Простая игра 50/50 с шансом x3!",
        "💎 <b>Мины</b> - мины [ставка] [кол-во мин]\n   Классическая игра на удачу!",
        "🃏 <b>Блэкджек</b> - блэкджек [ставка]\n   Карточная игра против крупье!",
        "🎰 <b>Слоты</b> - слоты [ставка]\n   Крути барабаны и выигрывай!",
        "🎯 <b>Hi-Lo</b> - хайло [ставка]\n   Угадай следующую карту!"
    ]
    
    selected = random.sample(recommendations, min(4, len(recommendations)))
    text = "🎮 <b>РЕКОМЕНДУЕМЫЕ ИГРЫ</b>\n\n" + "\n\n".join(selected)
    
    await message.reply(text, parse_mode="HTML")

# 👍 5. ОБРАТНАЯ СВЯЗЬ (нравится/не нравится)
@router.callback_query(lambda c: c.data.startswith('feedback_'))
async def feedback_callback(callback: CallbackQuery):
    parts = callback.data.split('_')
    action = parts[1]  # like или dislike
    user_id = int(parts[2])
    
    if user_id != callback.from_user.id:
        await callback.answer('❌ Это не ваша игра!', show_alert=True)
        return
    
    if action == 'like':
        await callback.answer('👍 Спасибо за отзыв!', show_alert=False)
    else:
        await callback.answer('👎 Спасибо за отзыв!', show_alert=False)
    
    # Удаляем кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# 🧹 6. АВТОМАТИЧЕСКАЯ ОЧИСТКА СТАРЫХ ДАННЫХ
async def cleanup_old_data():
    """Очищает старые данные для разгрузки Firebase"""
    current_time = time.time()
    cleaned = 0
    
    # Очистка старых игр (старше 1 часа)
    for game_type in [active_mines_games, active_tower_games, active_blackjack_games, 
                      active_crypto_hacker_games, active_taxi_games, active_poker_games,
                      active_reactor_games, active_hilo_games, active_bunker_games]:
        to_remove = []
        for user_id, game_data in game_type.items():
            if isinstance(game_data, dict) and 'start_time' in game_data:
                if current_time - game_data['start_time'] > 3600:  # 1 час
                    to_remove.append(user_id)
        for user_id in to_remove:
            del game_type[user_id]
            cleaned += 1
    
    # Очистка старых записей last_game_data (старше 24 часов)
    to_remove = []
    for user_id, game_data in last_game_data.items():
        if 'timestamp' in game_data and current_time - game_data['timestamp'] > 86400:
            to_remove.append(user_id)
    for user_id in to_remove:
        del last_game_data[user_id]
        cleaned += 1
    
    # Очистка режима "тихо" (уже истекшие)
    to_remove = []
    for user_id, end_time in user_quiet_mode.items():
        if current_time > end_time:
            to_remove.append(user_id)
    for user_id in to_remove:
        del user_quiet_mode[user_id]
    
    if cleaned > 0:
        print(f"🧹 Очищено {cleaned} устаревших записей")

# 🕊 8. РЕЖИМ "ТИХО"
@router.message(lambda message: message.text and message.text.lower() in ['тихо', 'quiet', 'silent'])
async def toggle_quiet_mode(message: types.Message):
    if is_banned(message.from_user.id):
        return
    
    user_id = message.from_user.id
    current_time = time.time()
    
    # Включаем режим "тихо" на 5 минут
    user_quiet_mode[user_id] = current_time + 300  # 5 минут
    
    await message.reply(
        '🕊 <b>Режим "Тихо" включен</b>\n\n'
        'Бот будет писать меньше сообщений в течение 5 минут.\n'
        'Только самое важное!',
        parse_mode="HTML"
    )

def is_quiet_mode(user_id: int) -> bool:
    """Проверяет, включен ли режим 'тихо' для пользователя"""
    if user_id not in user_quiet_mode:
        return False
    if time.time() > user_quiet_mode[user_id]:
        del user_quiet_mode[user_id]
        return False
    return True

# 🔔 9. НАПОМИНАНИЕ О БОНУСЕ (1 раз в сутки)
async def send_bonus_reminder(bot: Bot, user_id: int):
    """Отправляет напоминание о бонусе один раз в сутки"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Проверяем, отправляли ли уже сегодня
    if user_id in user_bonus_reminder_sent and user_bonus_reminder_sent[user_id] == today:
        return
    
    # Проверяем, доступен ли бонус
    if user_id not in users_data:
        return
    
    last_bonus_time = users_data[user_id].get('last_bonus_time', 0)
    current_time = time.time()
    
    # Если бонус доступен (прошло 24 часа)
    if current_time - last_bonus_time >= 86400:
        try:
            await bot.send_message(
                user_id,
                '🔔 <b>Напоминание</b>\n\n'
                '🎁 Ваш ежедневный бонус доступен!\n'
                'Используйте кнопку "🎁 Бонус" или команду /start',
                parse_mode="HTML"
            )
            user_bonus_reminder_sent[user_id] = today
        except:
            pass  # Пользователь заблокировал бота или ошибка

# 🎁 10. ЕЖЕДНЕВНЫЙ МИНИ-БОНУС ЗА АКТИВНОСТЬ
# (функция track_user_action уже определена выше)

# Функция для начисления выигрыша с обновлением лидерборда
def add_win_to_user(user_id: int, win_amount: int, bet: int = 0):
    """Начисляет выигрыш пользователю и обновляет лидерборд"""
    if user_id not in users_data:
        return
    
    users_data[user_id]['balance'] += win_amount
    if bet > 0:
        users_data[user_id]['total_won'] += win_amount - bet
    else:
        users_data[user_id]['total_won'] += win_amount
    
    # Обновляем лидерборд (только чистый выигрыш, без ставки)
    net_win = win_amount - bet if bet > 0 else win_amount
    if net_win > 0:
        update_leaderboard(user_id, net_win)
    
    save_users()

# Функция для обновления лидерборда
def update_leaderboard(user_id: int, won_amount: int):
    """Обновляет лидерборд при выигрыше"""
    current_date = datetime.now().strftime('%Y-%m-%d')
    global leaderboard_date, daily_leaderboard
    
    # Если новый день, сбрасываем лидерборд
    if leaderboard_date != current_date:
        daily_leaderboard = {}
        leaderboard_date = current_date
    
    # Добавляем выигрыш к текущему счету пользователя
    if user_id not in daily_leaderboard:
        daily_leaderboard[user_id] = 0
    daily_leaderboard[user_id] += won_amount
    
    save_leaderboard()

# Функция для обновления лидерборда в 00:00 и выдачи наград
async def reset_leaderboard_and_reward():
    """Сбрасывает лидерборд и выдает награды победителям"""
    global daily_leaderboard, leaderboard_date
    
    if not daily_leaderboard:
        return
    
    # Сортируем по выигранным морфам
    sorted_players = sorted(daily_leaderboard.items(), key=lambda x: x[1], reverse=True)
    
    # Награды для топ-5
    rewards = {
        1: 500000,  # 1 место: 500.000 MORPH
        2: 250000,  # 2 место: 250.000 MORPH
        3: 125000,  # 3 место: 125.000 MORPH
        4: 75000,   # 4 место: 75.000 MORPH
        5: 50000    # 5 место: 50.000 MORPH
    }
    
    # Выдаем награды
    for place, (user_id, won_amount) in enumerate(sorted_players[:5], 1):
        if place in rewards:
            reward = rewards[place]
            if user_id in users_data:
                users_data[user_id]['balance'] += reward
                save_users()
                
                # Отправляем уведомление
                try:
                    await bot.send_message(
                        user_id,
                        f'🏆 <b>ПОЗДРАВЛЯЕМ!</b>\n\n'
                        f'Вы заняли <b>{place} место</b> в ежедневном лидерборде!\n\n'
                        f'💰 Выиграно за день: <b>{format_amount(won_amount)} MORPH</b>\n'
                        f'🎁 Награда: <b>+{format_amount(reward)} MORPH</b>\n\n'
                        f'💎 Ваш баланс: <b>{format_amount(users_data[user_id]["balance"])} MORPH</b>',
                        parse_mode="HTML"
                    )
                except:
                    pass  # Пользователь заблокировал бота
    
    # Сбрасываем лидерборд
    daily_leaderboard = {}
    leaderboard_date = datetime.now().strftime('%Y-%m-%d')
    save_leaderboard()
    
    print(f"✅ Лидерборд обновлен, награды выданы топ-5 игрокам")

# Планировщик для очистки и напоминаний
async def scheduler_task():
    """Фоновая задача для очистки данных и напоминаний"""
    last_leaderboard_reset = None
    last_bonus_reminder = None
    
    while True:
        try:
            await asyncio.sleep(60)  # Проверяем каждую минуту
            current_time = datetime.now()
            current_date = current_time.strftime('%Y-%m-%d')
            
            # Очистка данных каждый час
            if current_time.minute == 0:
                await cleanup_old_data()
            
            # Обновляем лидерборд в 00:00
            if current_time.hour == 0 and current_time.minute == 0:
                if last_leaderboard_reset != current_date:
                    await reset_leaderboard_and_reward()
                    last_leaderboard_reset = current_date
                    await asyncio.sleep(60)  # Ждем минуту, чтобы не сработать дважды
            
            # Отправляем напоминания о бонусе в 12:00 (один раз в сутки)
            if current_time.hour == 12 and current_time.minute == 0:
                if last_bonus_reminder != current_date:
                    for user_id in list(users_data.keys()):
                        await send_bonus_reminder(bot, user_id)
                    last_bonus_reminder = current_date
                    await asyncio.sleep(60)  # Ждем минуту, чтобы не сработать дважды
            
            # Проверяем истекшие муты каждую минуту
            await check_and_unmute_users()
            
            # Проверяем истекшие VIP подписки каждую минуту
            current_time = time.time()
            expired_vips = [uid for uid, end_time in list(vip_subscriptions.items()) if end_time < current_time]
            if expired_vips:
                for uid in expired_vips:
                    del vip_subscriptions[uid]
                save_vip_subscriptions()
                print(f"Очищено {len(expired_vips)} истекших VIP подписок")
                
        except Exception as e:
            print(f"Ошибка в планировщике: {e}")

# (Функции save_last_game и track_user_action уже определены выше)

print("Бот сделан компанией -ARGUS-")

# Обработчик для всех обычных сообщений (не команд) - должен быть последним
# Этот обработчик срабатывает только если другие хендлеры не обработали сообщение
# Используем фильтр, который исключает все известные команды
@router.message(
    lambda m: m.text and 
    not m.text.startswith('/') and
    not any(m.text.lower().startswith(cmd) for cmd in [
        'топ', 'top', 'правила', 'rules', 'модераторы', 'админы', 'моды',
        'мут', 'бан', 'размут', 'разбан', 'mute', 'ban', 'unmute', 'unban',
        'назначить модератора', 'убрать модератора', 'setmod', 'delmod',
        '+правила', 'помощь', 'help', 'игры', 'games', 'баланс', 'б',
        'профиль', 'банк', 'bank', 'бонус', 'моя рефка', 'рефка',
        'лидерборд', 'leaderboard', 'топ дня', 'топ за день'
    ])
)
async def handle_all_messages(message: types.Message):
    """Обрабатывает все сообщения, которые не были обработаны другими хендлерами"""
    # Проверяем мут/бан только для обычных сообщений в группах
    if message.chat.type in ['group', 'supergroup']:
        blocked = await check_mute_ban_all_messages(message)
        if blocked:
            return  # Сообщение заблокировано, не обрабатываем дальше

async def main():
    load_all_data()
    dp.include_router(router)
    
    # Запускаем планировщик в фоне
    asyncio.create_task(scheduler_task())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
