# -*- coding: utf-8 -*-
# VERSION: VK BUTTON MENU FIXED — main persistent keyboard enabled
"""
VK-версия SborTeleBot.

Положи этот файл рядом с:
- schedule.py
- schedule.txt
- songs_pdf/
- songs_txt/
- super_secret.jpg

Перед запуском создай .env или переменные окружения:
VK_TOKEN=токен_сообщества_вк
VK_GROUP_ID=id_сообщества_без_минуса
DATABASE_URL=postgresql://...   # если нужна БД
ADMIN_IDS=279020893,373690508   # VK ID админов через запятую
TIME_SHIFT_HOURS=0              # 0 для локального времени; 3 если сервер в UTC, а расписание по МСК
"""

import os
import re
import random
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv необязателен: на хостинге переменные окружения часто задаются напрямую.
    pass

import vk_api
from vk_api import VkUpload
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

try:
    import psycopg2
except Exception:
    psycopg2 = None

from schedule import Event, Schedule


# -----------------------------------------------------------------------------
# НАСТРОЙКИ
# -----------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent

VK_TOKEN = os.getenv("VK_TOKEN")
VK_GROUP_ID = os.getenv("VK_GROUP_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "279020893,373690508").split(",")
    if x.strip()
}

TIME_SHIFT_HOURS = int(os.getenv("TIME_SHIFT_HOURS", "0"))

SONGS_LIST = [
    "Весеннее танго",
    "Голубые паруса",
    "Дом",
    "Зеленый поезд",
    "Зелёный поезд",
    "Люди улыбайтесь",
    "Милая моя",
    "Мы живы",
    "Оранжевый кот",
    "Прощальная",
    "Ребята на сборы хочу",
    "Фестивальная",
    "Хорошие люди",
]

HELP_TEXT = (
    "Вот что я умею. Теперь можно не писать команды руками — просто нажимай кнопки меню:\n"
    "🎵 Песни — выбрать песню или получить случайную песню\n"
    "⚙️ Формат песен — выбрать, как присылать песни: файлом или текстом\n"
    "📅 Программа — получить программу сбора\n"
    "⏭ Ближайшее — узнать текущее и следующее мероприятие\n"
    "🗝 Квест — секретная команда\n"
    "❓ Помощь — показать эту подсказку\n"
    "\n"
    "Если знаешь название песни, можешь просто написать его в чат."
)

START_TEXT = (
    "👋 Привет, я первый сборовский бот.\n"
    "Я могу пригодиться тебе в разных задачах, так что не стесняйся писать :)\n"
    "Теперь команды можно не вводить руками — основные функции есть на кнопках снизу.\n"
    "Например, нажми «🎵 Песни», чтобы разучить песни, или «📅 Программа», чтобы посмотреть расписание.\n"
    "Ещё я буду иногда присылать сообщения от ДК или отряда стариков 😉\n"
    "Для более подробной информации нажми «❓ Помощь»."
)

MAIN_MENU_TEXT = "Выбери действие на кнопках ниже 👇"


# -----------------------------------------------------------------------------
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# -----------------------------------------------------------------------------

users_dict: Dict[int, str] = {}
user_states: Dict[int, Dict[str, str]] = {}
schedule = Schedule()

vk_session = None
vk = None
upload = None
longpoll = None


# -----------------------------------------------------------------------------
# УТИЛИТЫ
# -----------------------------------------------------------------------------

def require_settings() -> None:
    """Проверяет, что главные переменные окружения заданы."""
    missing = []
    if not VK_TOKEN:
        missing.append("VK_TOKEN")
    if not VK_GROUP_ID:
        missing.append("VK_GROUP_ID")

    if missing:
        raise RuntimeError(
            "Не заданы переменные окружения: " + ", ".join(missing) + "\n"
            "Создай файл .env рядом с vk_bot.py или задай переменные в настройках хостинга."
        )


def normalize_text(text: str) -> str:
    """Нормализация текста для сравнения названий песен."""
    text = text.strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def decode_sharp_u_name(name: str) -> str:
    """
    Декодирует имена файлов вида #U0434#U043e#U043c.pdf -> дом.pdf.
    У тебя в архиве часть песен распаковалась именно в таком виде.
    """
    return re.sub(
        r"#U([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)),
        name,
    )


def split_text(text: str, limit: int = 3500) -> List[str]:
    """VK не любит слишком длинные сообщения, поэтому длинный текст дробим."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.splitlines(True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))

    return chunks


def get_now() -> datetime:
    """
    Возвращает текущее время для /next_event.
    Если бот крутится на UTC-сервере, поставь TIME_SHIFT_HOURS=3 для МСК.
    """
    return datetime.now() + timedelta(hours=TIME_SHIFT_HOURS)


def is_admin(from_id: int) -> bool:
    return int(from_id) in ADMIN_IDS


# -----------------------------------------------------------------------------
# БАЗА ДАННЫХ
# -----------------------------------------------------------------------------

def db_enabled() -> bool:
    return bool(DATABASE_URL and psycopg2)


def db_connect():
    if not db_enabled():
        return None
    return psycopg2.connect(DATABASE_URL)


def init_database() -> None:
    """
    Создает нужные таблицы, если их еще нет.
    Если DATABASE_URL не задан, бот будет работать без постоянного хранения пользователей.
    """
    if not db_enabled():
        print("[DB] DATABASE_URL не задан или psycopg2 не установлен. Бот работает без постоянной БД.")
        return

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                songs_output_type TEXT NOT NULL DEFAULT 'file'
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS day (
                id INTEGER PRIMARY KEY,
                day_num INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            """
            INSERT INTO day (id, day_num)
            VALUES (1, 0)
            ON CONFLICT (id) DO NOTHING;
            """
        )
        con.commit()
        print("[DB] Таблицы проверены.")
    except Exception as error:
        print("[DB] Ошибка инициализации БД:", error)
    finally:
        if con:
            con.close()


def db_get_users() -> List[Tuple[int, str]]:
    if not db_enabled():
        return []

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT id, songs_output_type FROM users;")
        return cur.fetchall()
    except Exception as error:
        print("[DB] Ошибка чтения пользователей:", error)
        return []
    finally:
        if con:
            con.close()


def db_add_user(peer_id: int) -> None:
    if not db_enabled():
        return

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users (id, songs_output_type)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING;
            """,
            (int(peer_id), "file"),
        )
        con.commit()
    except Exception as error:
        print("[DB] Ошибка добавления пользователя:", error)
    finally:
        if con:
            con.close()


def db_update_songs_type(peer_id: int, songs_type: str) -> None:
    if not db_enabled():
        return

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users (id, songs_output_type)
            VALUES (%s, %s)
            ON CONFLICT (id)
            DO UPDATE SET songs_output_type = EXCLUDED.songs_output_type;
            """,
            (int(peer_id), songs_type),
        )
        con.commit()
    except Exception as error:
        print("[DB] Ошибка обновления формата песен:", error)
    finally:
        if con:
            con.close()


def db_fetch_current_day() -> int:
    if not db_enabled():
        return 0

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT day_num FROM day WHERE id = 1;")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as error:
        print("[DB] Ошибка чтения текущего дня:", error)
        return 0
    finally:
        if con:
            con.close()


def db_update_current_day(day_num: int) -> None:
    if not db_enabled():
        return

    con = None
    try:
        con = db_connect()
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO day (id, day_num)
            VALUES (1, %s)
            ON CONFLICT (id)
            DO UPDATE SET day_num = EXCLUDED.day_num;
            """,
            (int(day_num),),
        )
        con.commit()
    except Exception as error:
        print("[DB] Ошибка обновления текущего дня:", error)
    finally:
        if con:
            con.close()


# -----------------------------------------------------------------------------
# VK: ОТПРАВКА СООБЩЕНИЙ, ФАЙЛОВ, КЛАВИАТУР
# -----------------------------------------------------------------------------

def make_keyboard(
    buttons: List[str],
    per_line: int = 2,
    color=VkKeyboardColor.PRIMARY,
    one_time: bool = True,
) -> str:
    keyboard = VkKeyboard(one_time=one_time)

    for i, button in enumerate(buttons):
        if i > 0 and i % per_line == 0:
            keyboard.add_line()
        keyboard.add_button(str(button), color=color)

    return keyboard.get_keyboard()


def main_menu_keyboard(from_id: Optional[int] = None) -> str:
    """Постоянная клавиатура с основными функциями бота."""
    keyboard = VkKeyboard(one_time=False)

    keyboard.add_button("🎵 Песни", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("📅 Программа", color=VkKeyboardColor.PRIMARY)

    keyboard.add_line()
    keyboard.add_button("⏭ Ближайшее", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("⚙️ Формат песен", color=VkKeyboardColor.SECONDARY)

    keyboard.add_line()
    keyboard.add_button("🗝 Квест", color=VkKeyboardColor.SECONDARY)
    keyboard.add_button("❓ Помощь", color=VkKeyboardColor.SECONDARY)

    # Админские кнопки показываем только администраторам.
    if from_id is not None and is_admin(int(from_id)):
        keyboard.add_line()
        keyboard.add_button("📢 Рассылка", color=VkKeyboardColor.NEGATIVE)
        keyboard.add_button("🗓 День", color=VkKeyboardColor.SECONDARY)

        keyboard.add_line()
        keyboard.add_button("✏️ Расписание", color=VkKeyboardColor.SECONDARY)
        keyboard.add_button("🔄 Обновить", color=VkKeyboardColor.SECONDARY)

    return keyboard.get_keyboard()


def empty_keyboard() -> str:
    return VkKeyboard.get_empty_keyboard()


def send_message(peer_id: int, text: str = "", keyboard: Optional[str] = None, attachment: Optional[str] = None) -> None:
    params = {
        "peer_id": int(peer_id),
        "random_id": get_random_id(),
        "message": text or "",
    }

    if keyboard is not None:
        params["keyboard"] = keyboard
    if attachment:
        params["attachment"] = attachment

    vk.messages.send(**params)


def send_long_message(peer_id: int, text: str, keyboard: Optional[str] = None) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        send_message(peer_id, chunk, keyboard=keyboard if i == 0 else None)


def build_doc_attachment(doc_result: dict) -> str:
    doc = doc_result.get("doc", doc_result)
    owner_id = doc["owner_id"]
    doc_id = doc["id"]
    access_key = doc.get("access_key")
    attachment = f"doc{owner_id}_{doc_id}"
    if access_key:
        attachment += f"_{access_key}"
    return attachment


def build_photo_attachment(photo: dict) -> str:
    owner_id = photo["owner_id"]
    photo_id = photo["id"]
    access_key = photo.get("access_key")
    attachment = f"photo{owner_id}_{photo_id}"
    if access_key:
        attachment += f"_{access_key}"
    return attachment


def send_document(peer_id: int, file_path: Path, title: Optional[str] = None) -> None:
    if not file_path.exists():
        send_message(peer_id, "Файл не найден 😔")
        return

    doc = upload.document_message(
        doc=str(file_path),
        title=title or file_path.name,
        peer_id=int(peer_id),
    )
    attachment = build_doc_attachment(doc)
    send_message(peer_id, attachment=attachment)


def send_photo(peer_id: int, file_path: Path) -> None:
    if not file_path.exists():
        send_message(peer_id, "Картинка не найдена 😔")
        return

    photo = upload.photo_messages(photos=str(file_path))[0]
    attachment = build_photo_attachment(photo)
    send_message(peer_id, attachment=attachment)


def vk_attachment_to_string(attachment: dict) -> Optional[str]:
    """Делает строку attachment для пересылки вложений из /send_all."""
    attachment_type = attachment.get("type")
    if not attachment_type:
        return None

    item = attachment.get(attachment_type)
    if not isinstance(item, dict):
        return None

    owner_id = item.get("owner_id")
    item_id = item.get("id")
    access_key = item.get("access_key")

    if owner_id is None or item_id is None:
        return None

    result = f"{attachment_type}{owner_id}_{item_id}"
    if access_key:
        result += f"_{access_key}"
    return result


def collect_attachments(message: dict) -> str:
    items = []
    for attachment in message.get("attachments", []) or []:
        item = vk_attachment_to_string(attachment)
        if item:
            items.append(item)
    return ",".join(items)


# -----------------------------------------------------------------------------
# ПОЛЬЗОВАТЕЛИ
# -----------------------------------------------------------------------------

def prepare_users() -> None:
    users_dict.clear()
    for peer_id, songs_output_type in db_get_users():
        users_dict[int(peer_id)] = songs_output_type or "file"


def ensure_user(peer_id: int) -> None:
    if int(peer_id) in users_dict:
        return

    users_dict[int(peer_id)] = "file"
    db_add_user(int(peer_id))

    for admin_id in ADMIN_IDS:
        try:
            send_message(admin_id, f"Такс, у нас новенький, вот его peer_id: {peer_id}")
        except Exception as error:
            print(f"[VK] Не смог отправить уведомление админу {admin_id}:", error)


def get_user_songs_type(peer_id: int) -> str:
    return users_dict.get(int(peer_id), "file")


def set_user_songs_type(peer_id: int, songs_type: str) -> None:
    songs_type = songs_type.lower().strip()
    if songs_type not in {"file", "text"}:
        songs_type = "file"

    users_dict[int(peer_id)] = songs_type
    db_update_songs_type(int(peer_id), songs_type)


# -----------------------------------------------------------------------------
# ПЕСНИ
# -----------------------------------------------------------------------------

def unique_song_buttons() -> List[str]:
    """Убираем дубль Зеленый/Зелёный поезд из кнопок, но обе версии оставляем для поиска."""
    result = []
    seen = set()
    for song in SONGS_LIST:
        key = normalize_text(song)
        if key in seen:
            continue
        seen.add(key)
        result.append(song)
    return result


def is_known_song(text: str) -> bool:
    normalized = normalize_text(text)
    return normalized in {normalize_text(song) for song in SONGS_LIST}


def find_song_file(song_name: str, folder: str, extension: str) -> Optional[Path]:
    """Ищет файл песни даже если имя файла распаковано как #U0434#U043e#U043c."""
    directory = ROOT_DIR / folder
    if not directory.exists():
        return None

    target = normalize_text(song_name)

    for file_path in directory.iterdir():
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() != extension.lower():
            continue

        original_stem = file_path.stem
        decoded_stem = decode_sharp_u_name(original_stem)

        if normalize_text(original_stem) == target or normalize_text(decoded_stem) == target:
            return file_path

    return None


def read_text_file(file_path: Path) -> str:
    for encoding in ("utf-8", "cp1251"):
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def send_song(peer_id: int, song_name: str, from_id: Optional[int] = None) -> None:
    ensure_user(peer_id)

    if not is_known_song(song_name):
        send_message(peer_id, "Эту песню я еще не выучил 😫", keyboard=main_menu_keyboard(from_id or peer_id))
        return

    songs_type = get_user_songs_type(peer_id)

    if songs_type == "text":
        txt_path = find_song_file(song_name, "songs_txt", ".txt")
        if not txt_path:
            send_message(peer_id, "Текст этой песни не найден 😔", keyboard=main_menu_keyboard(from_id or peer_id))
            return
        send_long_message(peer_id, read_text_file(txt_path))
        send_message(peer_id, "Готово. Можешь выбрать следующее действие:", keyboard=main_menu_keyboard(from_id or peer_id))
        return

    pdf_path = find_song_file(song_name, "songs_pdf", ".pdf")
    if not pdf_path:
        send_message(peer_id, "PDF этой песни не найден 😔", keyboard=main_menu_keyboard(from_id or peer_id))
        return

    send_document(peer_id, pdf_path, title=f"{song_name}.pdf")
    send_message(peer_id, "Готово. Можешь выбрать следующее действие:", keyboard=main_menu_keyboard(from_id or peer_id))


def send_random_song(peer_id: int, from_id: Optional[int] = None) -> None:
    song = random.choice(unique_song_buttons())
    send_message(peer_id, f"🎲 Случайная песня: {song}")
    send_song(peer_id, song, from_id=from_id)


# -----------------------------------------------------------------------------
# РАСПИСАНИЕ
# -----------------------------------------------------------------------------

def parse_raw_events(raw_events: List[str]) -> List[Event]:
    events = []
    need_end_time = False

    for raw_line in raw_events:
        line = raw_line.strip()
        if not line:
            continue

        parts = [s.strip() for s in line.split(" - ")]
        if len(parts) >= 3:
            event = Event(parts[0].replace(".", ":"), " - ".join(parts[2:]))
            event.set_end_time(parts[1].replace(".", ":"))

            if need_end_time and events:
                events[-1].set_end_time(parts[0].replace(".", ":"))

            events.append(event)
            need_end_time = False
        elif len(parts) == 2:
            event = Event(parts[0].replace(".", ":"), parts[1])

            if need_end_time and events:
                events[-1].set_end_time(parts[0].replace(".", ":"))

            events.append(event)
            need_end_time = True

    if events and not events[-1].end_time:
        events[-1].set_end_time("23:59")

    return events


def parse_schedule_file() -> None:
    schedule_path = ROOT_DIR / "schedule.txt"
    if not schedule_path.exists():
        print("[SCHEDULE] schedule.txt не найден.")
        schedule.set_current_day(0)
        return

    content = schedule_path.read_text(encoding="utf-8")
    all_lines = [line.rstrip() for line in content.splitlines()]

    header_indexes = []
    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if stripped and " - " not in stripped:
            header_indexes.append(i)

    day_blocks = []
    for i, header_index in enumerate(header_indexes[:3]):
        start = header_index + 1
        end = header_indexes[i + 1] if i + 1 < len(header_indexes) else len(all_lines)
        day_blocks.append(all_lines[start:end])

    while len(day_blocks) < 3:
        day_blocks.append([])

    schedule.days[0].set_events(parse_raw_events(day_blocks[0]))
    schedule.days[1].set_events(parse_raw_events(day_blocks[1]))
    schedule.days[2].set_events(parse_raw_events(day_blocks[2]))
    schedule.set_current_day(db_fetch_current_day())

    print("[SCHEDULE] Расписание загружено.")


def build_timetable_text() -> str:
    timetable = "📅 Программа сбора:\n"
    for index, day in enumerate(schedule.days, start=1):
        timetable += f"\nДень {index}:\n"
        if not day.events:
            timetable += "Расписание не задано.\n"
            continue
        for event in day.events:
            timetable += event.pretty_print() + "\n"
    return timetable


def send_next_event(peer_id: int, from_id: Optional[int] = None) -> None:
    if schedule.current_day == "Не начался":
        send_message(
            peer_id,
            "Сбор ещё не начался, не торопись 😉\n"
            "Можешь пока посмотреть всю программу через кнопку «📅 Программа».",
            keyboard=main_menu_keyboard(from_id or peer_id),
        )
        return

    if schedule.current_day == "Закончися":
        send_message(
            peer_id,
            "Сбор закончился 😭\n"
            "Я, как и ты, с нетерпением жду следующего.\n"
            "Можешь посмотреть прошлую программу через кнопку «📅 Программа»\n"
            "Или разучить песни через кнопку «🎵 Песни».",
            keyboard=main_menu_keyboard(from_id or peer_id),
        )
        return

    now = get_now()
    found_current = False
    text = ""

    for event in schedule.current_day.events:
        try:
            start_time = datetime.strptime(event.start_time, "%H:%M")
            end_time = datetime.strptime(event.end_time, "%H:%M")
        except Exception:
            continue

        start_time = now.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
        end_time = now.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)

        if found_current:
            send_message(peer_id, text + f"Потом в {event.start_time}: {event.description}", keyboard=main_menu_keyboard(from_id or peer_id))
            return

        if start_time <= now <= end_time:
            text = f"Сейчас: {event.description} до {event.end_time}\n"
            found_current = True

    if found_current:
        send_message(peer_id, text + "Следующее мероприятие не найдено.", keyboard=main_menu_keyboard(from_id or peer_id))
    else:
        send_message(peer_id, "Сейчас по расписанию ничего не найдено. Проверь полную программу через кнопку «📅 Программа».", keyboard=main_menu_keyboard(from_id or peer_id))


# -----------------------------------------------------------------------------
# СОСТОЯНИЯ ДИАЛОГА
# -----------------------------------------------------------------------------

def set_state(peer_id: int, state_name: str, **data) -> None:
    state = {"name": state_name}
    for key, value in data.items():
        state[key] = str(value)
    user_states[int(peer_id)] = state


def get_state(peer_id: int) -> Optional[Dict[str, str]]:
    return user_states.get(int(peer_id))


def clear_state(peer_id: int) -> None:
    user_states.pop(int(peer_id), None)


def handle_state(peer_id: int, from_id: int, text: str, message: dict) -> bool:
    state = get_state(peer_id)
    if not state:
        return False

    state_name = state.get("name")
    normalized = normalize_text(text)

    if state_name == "songs_menu":
        if normalized == normalize_text("Случайная песня"):
            clear_state(peer_id)
            send_random_song(peer_id, from_id=from_id)
            return True

        if normalized == normalize_text("Найти песню"):
            set_state(peer_id, "song_search")
            send_message(
                peer_id,
                "👌 Окей, давай искать. Выбери песню:",
                keyboard=make_keyboard(unique_song_buttons(), per_line=2, color=VkKeyboardColor.SECONDARY),
            )
            return True

        send_message(peer_id, "Выбери кнопку: «Случайная песня» или «Найти песню».")
        return True

    if state_name == "song_search":
        clear_state(peer_id)
        send_song(peer_id, text, from_id=from_id)
        return True

    if state_name == "set_songs_type":
        if normalized in {"file", "файл"}:
            set_user_songs_type(peer_id, "file")
            clear_state(peer_id)
            send_message(peer_id, "Окей, буду присылать песни файлом.", keyboard=main_menu_keyboard(from_id))
            return True

        if normalized in {"text", "текст"}:
            set_user_songs_type(peer_id, "text")
            clear_state(peer_id)
            send_message(peer_id, "Окей, буду присылать песни текстом в чат.", keyboard=main_menu_keyboard(from_id))
            return True

        send_message(peer_id, "Напиши или выбери: file/text.")
        return True

    if state_name == "quest_answer":
        clear_state(peer_id)
        if normalized == normalize_text("знамя"):
            send_message(peer_id, "Абсолютно верно, держи подсказку!")
            send_photo(peer_id, ROOT_DIR / "super_secret.jpg")
            send_message(peer_id, "Что дальше?", keyboard=main_menu_keyboard(from_id))
        else:
            send_message(peer_id, "Похоже, что ты ошибся 😭\nПопробуй ещё раз через кнопку «🗝 Квест».", keyboard=main_menu_keyboard(from_id))
        return True

    if state_name == "set_current_day":
        if not is_admin(from_id):
            clear_state(peer_id)
            send_message(peer_id, "У тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
            return True

        day_names = {
            "0": "сбор не начался",
            "1": "первый день",
            "2": "второй день",
            "3": "третий день",
            "4": "сбор закончился",
        }

        if normalized not in day_names:
            send_message(
                peer_id,
                "Выбери день кнопкой: 0, 1, 2, 3 или 4.",
                keyboard=make_keyboard(["0", "1", "2", "3", "4"], per_line=5),
            )
            return True

        day_num = int(normalized)
        schedule.set_current_day(day_num)
        db_update_current_day(day_num)
        clear_state(peer_id)
        send_message(
            peer_id,
            f"Установлено: {day_names[normalized]}.",
            keyboard=main_menu_keyboard(from_id),
        )
        return True

    if state_name == "set_timetable_day":
        if not is_admin(from_id):
            clear_state(peer_id)
            send_message(peer_id, "У тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
            return True

        if normalized == "exit":
            clear_state(peer_id)
            send_message(peer_id, "Кайф, расписание сбора готово!", keyboard=main_menu_keyboard(from_id))
            return True

        if normalized not in {"1", "2", "3"}:
            send_message(peer_id, "Выбери номер дня: 1, 2 или 3. Для выхода напиши exit.")
            return True

        schedule.set_current_day(int(normalized))
        set_state(peer_id, "set_timetable_events", day_num=normalized)
        send_message(
            peer_id,
            "Окей, напиши события в формате:\n"
            "время - название_события\n"
            "или\n"
            "время - время_окончания - название_события\n\n"
            "Каждое событие — с новой строки.",
            keyboard=empty_keyboard(),
        )
        return True

    if state_name == "set_timetable_events":
        if not is_admin(from_id):
            clear_state(peer_id)
            send_message(peer_id, "У тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
            return True

        events = parse_raw_events(text.splitlines())
        if not events:
            send_message(peer_id, "Не смог распознать события. Проверь формат и отправь ещё раз.")
            return True

        schedule.current_day.set_events(events)
        set_state(peer_id, "set_timetable_day")
        send_message(
            peer_id,
            "Четко. Выбирай новый день или напиши exit для выхода.",
            keyboard=make_keyboard(["1", "2", "3", "exit"], per_line=4),
        )
        return True

    if state_name == "broadcast_message":
        if not is_admin(from_id):
            clear_state(peer_id)
            send_message(peer_id, "У тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
            return True

        clear_state(peer_id)
        attachment = collect_attachments(message)
        broadcast_text = text.strip()

        if not broadcast_text and not attachment:
            send_message(peer_id, "Ты отправил пустое сообщение. Рассылка отменена.", keyboard=main_menu_keyboard(from_id))
            return True

        success = 0
        failed = 0
        for target_peer_id in list(users_dict.keys()):
            if int(target_peer_id) == int(peer_id):
                continue
            try:
                send_message(target_peer_id, broadcast_text, attachment=attachment)
                success += 1
            except Exception as error:
                print(f"[BROADCAST] Ошибка отправки в {target_peer_id}:", error)
                failed += 1

        send_message(peer_id, f"Отправил рассылку. Успешно: {success}. Ошибок: {failed}.", keyboard=main_menu_keyboard(from_id))
        return True

    return False


# -----------------------------------------------------------------------------
# КОМАНДЫ
# -----------------------------------------------------------------------------

def handle_start(peer_id: int, from_id: Optional[int] = None) -> None:
    ensure_user(peer_id)
    send_message(peer_id, START_TEXT, keyboard=main_menu_keyboard(from_id or peer_id))


def handle_help(peer_id: int, from_id: Optional[int] = None) -> None:
    send_message(peer_id, HELP_TEXT, keyboard=main_menu_keyboard(from_id or peer_id))


def handle_main_menu(peer_id: int, from_id: Optional[int] = None) -> None:
    send_message(peer_id, MAIN_MENU_TEXT, keyboard=main_menu_keyboard(from_id or peer_id))


def handle_songs(peer_id: int) -> None:
    ensure_user(peer_id)
    set_state(peer_id, "songs_menu")
    send_message(
        peer_id,
        "Круто, выбирай что хочешь сделать:",
        keyboard=make_keyboard(["Случайная песня", "Найти песню", "🏠 Главное меню"], per_line=2),
    )


def handle_set_songs_type(peer_id: int) -> None:
    ensure_user(peer_id)
    set_state(peer_id, "set_songs_type")
    send_message(
        peer_id,
        "Ты хочешь получать песни файлом или текстом в чат?",
        keyboard=make_keyboard(["file", "text", "🏠 Главное меню"], per_line=2),
    )


def handle_quest(peer_id: int) -> None:
    set_state(peer_id, "quest_answer")
    send_message(
        peer_id,
        "Ага, ты узнал про мою секретную команду!\n"
        "Ну что ж, искатель приключений, пришли мне верный ключ.",
        keyboard=make_keyboard(["🏠 Главное меню"], per_line=1),
    )


def handle_set_current_day(peer_id: int, from_id: int, args: List[str]) -> None:
    if not is_admin(from_id):
        send_message(peer_id, "Извини, у тебя нет доступа к этой команде.")
        return

    if not args:
        send_message(
            peer_id,
            "Напиши номер дня после команды.\n"
            "0 — сбор не начался\n"
            "1 — первый день\n"
            "2 — второй день\n"
            "3 — третий день\n"
            "4 — сбор закончился\n\n"
            "Пример: /set_current_day 1",
        )
        return

    try:
        day_num = int(args[0])
    except ValueError:
        send_message(peer_id, "День должен быть числом: 0, 1, 2, 3 или 4.")
        return

    if day_num not in {0, 1, 2, 3, 4}:
        send_message(peer_id, "День должен быть: 0, 1, 2, 3 или 4.")
        return

    schedule.set_current_day(day_num)
    db_update_current_day(day_num)
    send_message(peer_id, f"Установлен день: {day_num}", keyboard=main_menu_keyboard(from_id))


def handle_choose_current_day(peer_id: int, from_id: int) -> None:
    if not is_admin(from_id):
        send_message(peer_id, "Извини, у тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
        return

    set_state(peer_id, "set_current_day")
    send_message(
        peer_id,
        "Выбери текущий день сбора:\n"
        "0 — сбор не начался\n"
        "1 — первый день\n"
        "2 — второй день\n"
        "3 — третий день\n"
        "4 — сбор закончился",
        keyboard=make_keyboard(["0", "1", "2", "3", "4", "🏠 Главное меню"], per_line=5),
    )


def handle_set_timetable(peer_id: int, from_id: int) -> None:
    if not is_admin(from_id):
        send_message(peer_id, "Извини, у тебя нет доступа к этой команде.")
        return

    set_state(peer_id, "set_timetable_day")
    send_message(
        peer_id,
        "Давай зададим расписание. Выбери номер дня:",
        keyboard=make_keyboard(["1", "2", "3", "exit"], per_line=4),
    )


def handle_send_all(peer_id: int, from_id: int) -> None:
    if not is_admin(from_id):
        send_message(peer_id, "Извини, у тебя нет доступа к этой команде.")
        return

    set_state(peer_id, "broadcast_message")
    send_message(peer_id, "Напиши сообщение, которое хочешь отправить во все чаты ниже.", keyboard=make_keyboard(["🏠 Главное меню"], per_line=1))


def handle_button_action(peer_id: int, from_id: int, text: str) -> bool:
    """Обрабатывает нажатия на кнопки основного меню без slash-команд."""
    value = normalize_text(text)

    if value in {normalize_text("🏠 Главное меню"), "главное меню", "меню", "назад", "отмена", "cancel"}:
        clear_state(peer_id)
        handle_main_menu(peer_id, from_id)
        return True

    if value in {normalize_text("🎵 Песни"), "песни", "song", "songs"}:
        clear_state(peer_id)
        handle_songs(peer_id)
        return True

    if value in {normalize_text("📅 Программа"), "программа", "расписание сбора"}:
        clear_state(peer_id)
        send_long_message(peer_id, build_timetable_text(), keyboard=main_menu_keyboard(from_id))
        return True

    if value in {normalize_text("⏭ Ближайшее"), "ближайшее", "следующее", "сейчас"}:
        clear_state(peer_id)
        send_next_event(peer_id, from_id=from_id)
        return True

    if value in {normalize_text("⚙️ Формат песен"), "формат песен", "формат"}:
        clear_state(peer_id)
        handle_set_songs_type(peer_id)
        return True

    if value in {normalize_text("🗝 Квест"), "квест", "quest"}:
        clear_state(peer_id)
        handle_quest(peer_id)
        return True

    if value in {normalize_text("❓ Помощь"), "помощь", "help"}:
        clear_state(peer_id)
        handle_help(peer_id, from_id)
        return True

    if value in {normalize_text("📢 Рассылка"), "рассылка"}:
        clear_state(peer_id)
        handle_send_all(peer_id, from_id)
        return True

    if value in {normalize_text("🗓 День"), "день", "текущий день"}:
        clear_state(peer_id)
        handle_choose_current_day(peer_id, from_id)
        return True

    if value in {normalize_text("✏️ Расписание"), "изменить расписание", "задать расписание"}:
        clear_state(peer_id)
        handle_set_timetable(peer_id, from_id)
        return True

    if value in {normalize_text("🔄 Обновить"), "обновить", "обновить расписание"}:
        clear_state(peer_id)
        if is_admin(from_id):
            parse_schedule_file()
            send_message(peer_id, "Расписание перечитано из schedule.txt.", keyboard=main_menu_keyboard(from_id))
        else:
            send_message(peer_id, "Извини, у тебя нет доступа к этой команде.", keyboard=main_menu_keyboard(from_id))
        return True

    return False


def handle_command(peer_id: int, from_id: int, text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False

    parts = normalized.split()
    command = parts[0].lower()
    args = parts[1:]

    # VK иногда отправляет кнопку "Начать" вместо /start.
    if normalize_text(normalized) in {"начать", "start"}:
        handle_start(peer_id, from_id)
        return True

    if not command.startswith("/"):
        return False

    clear_state(peer_id)

    if command == "/start":
        handle_start(peer_id, from_id)
    elif command == "/help":
        handle_help(peer_id, from_id)
    elif command == "/songs":
        handle_songs(peer_id)
    elif command == "/set_songs_type":
        handle_set_songs_type(peer_id)
    elif command == "/timetable":
        send_long_message(peer_id, build_timetable_text(), keyboard=main_menu_keyboard(from_id))
    elif command == "/next_event":
        send_next_event(peer_id, from_id=from_id)
    elif command == "/quest":
        handle_quest(peer_id)
    elif command == "/set_current_day":
        handle_set_current_day(peer_id, from_id, args)
    elif command == "/set_timetable":
        handle_set_timetable(peer_id, from_id)
    elif command == "/parse_schedule":
        if is_admin(from_id):
            parse_schedule_file()
            send_message(peer_id, "Расписание перечитано из schedule.txt.", keyboard=main_menu_keyboard(from_id))
        else:
            send_message(peer_id, "Извини, у тебя нет доступа к этой команде.")
    elif command == "/send_all":
        handle_send_all(peer_id, from_id)
    elif command == "/cancel":
        clear_state(peer_id)
        send_message(peer_id, "Окей, отменил текущее действие.", keyboard=main_menu_keyboard(from_id))
    else:
        send_message(peer_id, "Не знаю такую команду. Нажми «❓ Помощь», чтобы посмотреть список функций.", keyboard=main_menu_keyboard(from_id))

    return True


# -----------------------------------------------------------------------------
# ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# -----------------------------------------------------------------------------

def extract_message(event) -> Optional[dict]:
    """Достает dict сообщения из события VK. Сделано чуть надежнее под разные версии vk_api."""
    obj = getattr(event, "object", None) or getattr(event, "obj", None)
    if obj is None:
        return None

    message = getattr(obj, "message", None)
    if isinstance(message, dict):
        return message

    if isinstance(obj, dict):
        if isinstance(obj.get("message"), dict):
            return obj["message"]
        return obj

    return None


def handle_message_event(event) -> None:
    message = extract_message(event)
    if not message:
        return

    peer_id = int(message.get("peer_id") or message.get("from_id"))
    from_id = int(message.get("from_id") or peer_id)
    text = (message.get("text") or "").strip()

    ensure_user(peer_id)

    if text and handle_button_action(peer_id, from_id, text):
        return

    # Если админ делает рассылку вложением без текста, состояние все равно должно обработаться.
    if get_state(peer_id) and handle_state(peer_id, from_id, text, message):
        return

    if text and handle_command(peer_id, from_id, text):
        return

    if text and is_known_song(text):
        send_song(peer_id, text, from_id=from_id)
        return

    if text:
        send_message(peer_id, "Я не совсем понял 😅\nНажми «❓ Помощь», чтобы посмотреть список функций.", keyboard=main_menu_keyboard(from_id))


def prepare_for_start() -> None:
    init_database()
    prepare_users()
    parse_schedule_file()


def main() -> None:
    global vk_session, vk, upload, longpoll

    require_settings()

    group_id = abs(int(VK_GROUP_ID))

    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    upload = VkUpload(vk_session)
    longpoll = VkBotLongPoll(vk_session, group_id)

    prepare_for_start()

    print("[VK] Бот запущен. Жду сообщения...")

    for event in longpoll.listen():
        try:
            if event.type == VkBotEventType.MESSAGE_NEW:
                handle_message_event(event)
        except Exception as error:
            print("[VK] Ошибка обработки сообщения:", error)


if __name__ == "__main__":
    main()
