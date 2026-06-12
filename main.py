"""
Telegram APK Store Bot (Aiogram 3.7.0+)
Python 3.10+ | SQLite3 | Pillow

Негізгі мүмкіндіктер:
- Әкімші үшін мод қосу (жаңартылған 7 қадамдық FSM):
  1) APK
  2) Иконка (Auto-Preview)
  3) Атауы
  4) Сипаттамасы
  5) Нұсқасы
  6) Мод мүмкіндіктері
  7) Санат/тегтер
- Иконкадан автоматты preview генерациясы (1920x1080) Pillow арқылы
- Арнаға әдемі пост (preview + Қазақша HTML caption + Download deep-link)
- Міндетті жазылу тексерісі (mandatory channels)
- /search (пайдаланушы) — атауы/сипаттамасы бойынша іздеу
- "📝 Мод сұрау" — админге сұраныс жіберу
- /stats (админ) — қолданушы/қолданба/ең көп жүктелген
- /send (админ) — кеңейтілген тарату:
  Түрі: мәтін / фото / видео / GIF
  Caption/Text
  Inline батырмалар (әр жол: Атауы - URL)
  Мақсат: Арнаға немесе Барлық қолданушыға
- Файл беру логикасы: алдымен ұран, кейін APK

Тәуелділіктер:
pip install aiogram pillow
"""

import asyncio
import io
import logging
import hashlib
import html
import sqlite3
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import quote

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from PIL import Image, ImageDraw, ImageFont


# =========================
# CONFIG
# =========================
BOT_TOKEN = "807..."
ADMIN_ID = 63......
CHANNEL_ID = -100.....

DB_PATH = "apk_store.db"

ASSETS_DIR = Path("assets")
ASSET_LOGO = ASSETS_DIR / "logo.png"
ASSET_BADGE = ASSETS_DIR / "badge.png"
ASSET_FONT = ASSETS_DIR / "font.ttf"

MAX_BYTES_TO_HASH = 25 * 1024 * 1024  # 25MB
PREVIEW_W, PREVIEW_H = 1920, 1080
PREVIEW_BG = (0, 0, 51)  # #000033

SLOGAN = "QAZAZ: Стандарттан тыс. Шектеуден биік."


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("apk_store_bot")


# =========================
# DB
# =========================
class Database:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._connect() as con:
            cur = con.cursor()

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mandatory_channels (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    invite_link TEXT NOT NULL
                );
                """
            )

            # apps: description және category қосылды
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS apps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    description TEXT,
                    version TEXT,
                    features TEXT NOT NULL,
                    category TEXT,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    sha256 TEXT,
                    vt_url TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    app_id INTEGER NOT NULL,
                    downloaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            con.commit()

        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Ескі DB болса, apps кестесіне жетіспейтін бағандарды қосу."""
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("PRAGMA table_info(apps);")
            cols = {row[1] for row in cur.fetchall()}

            def add_col(col: str, ddl: str):
                if col not in cols:
                    logger.info("Миграция: apps кестесіне '%s' бағаны қосылуда...", col)
                    cur.execute(ddl)

            add_col("version", "ALTER TABLE apps ADD COLUMN version TEXT;")
            add_col("description", "ALTER TABLE apps ADD COLUMN description TEXT;")
            add_col("category", "ALTER TABLE apps ADD COLUMN category TEXT;")
            con.commit()

    # -------- users --------
    def upsert_user(self, user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name;
                """,
                (user_id, username, first_name, last_name),
            )
            con.commit()

    def list_user_ids(self) -> List[int]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT user_id FROM users;")
            return [r[0] for r in cur.fetchall()]

    def count_users(self) -> int:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM users;")
            return int(cur.fetchone()[0])

    # -------- mandatory channels --------
    def add_channel(self, chat_id: int, title: str, invite_link: str) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO mandatory_channels (chat_id, title, invite_link)
                VALUES (?, ?, ?);
                """,
                (chat_id, title, invite_link),
            )
            con.commit()

    def remove_channel(self, chat_id: int) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM mandatory_channels WHERE chat_id = ?;", (chat_id,))
            con.commit()

    def list_channels(self) -> List[Tuple[int, str, str]]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT chat_id, title, invite_link FROM mandatory_channels ORDER BY title;")
            return cur.fetchall()

    # -------- apps --------
    def add_app(
        self,
        app_name: str,
        description: str,
        version: str,
        features: str,
        category: str,
        file_id: str,
        file_name: Optional[str],
        sha256: Optional[str],
        vt_url: Optional[str],
    ) -> int:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO apps (app_name, description, version, features, category, file_id, file_name, sha256, vt_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (app_name, description, version, features, category, file_id, file_name, sha256, vt_url),
            )
            con.commit()
            return int(cur.lastrowid)

    def get_app(self, app_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, app_name, description, version, features, category, file_id, file_name, sha256, vt_url
                FROM apps
                WHERE id = ?;
                """,
                (app_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            keys = ["id", "app_name", "description", "version", "features", "category", "file_id", "file_name", "sha256", "vt_url"]
            return dict(zip(keys, row))

    def count_apps(self) -> int:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM apps;")
            return int(cur.fetchone()[0])

    def search_apps(self, query: str, limit: int = 10) -> List[Tuple[int, str, Optional[str], Optional[str]]]:
        q = f"%{query.strip()}%"
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, app_name, version, category
                FROM apps
                WHERE app_name LIKE ? OR description LIKE ?
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (q, q, limit),
            )
            return cur.fetchall()

    # -------- downloads --------
    def add_download(self, user_id: int, app_id: int) -> None:
        with self._connect() as con:
            con.execute("INSERT INTO downloads (user_id, app_id) VALUES (?, ?);", (user_id, app_id))
            con.commit()

    def most_downloaded(self) -> Optional[Tuple[int, str, int]]:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT a.id, a.app_name, COUNT(d.id) AS c
                FROM downloads d
                JOIN apps a ON a.id = d.app_id
                GROUP BY a.id
                ORDER BY c DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            return int(row[0]), str(row[1]), int(row[2])


db = Database(DB_PATH)


# =========================
# FSM
# =========================
class AddAppFSM(StatesGroup):
    waiting_apk = State()
    waiting_icon = State()
    waiting_name = State()
    waiting_description = State()
    waiting_version = State()
    waiting_features = State()
    waiting_category = State()


class ChannelFSM(StatesGroup):
    waiting_channel = State()


class RequestFSM(StatesGroup):
    waiting_text = State()


class SearchFSM(StatesGroup):
    waiting_query = State()


class SendFSM(StatesGroup):
    choosing_target = State()
    choosing_type = State()
    waiting_content = State()
    waiting_buttons = State()
    confirm = State()


# =========================
# KEYBOARDS
# =========================
def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Жаңа мод қосу", callback_data="admin:add_app")
    kb.button(text="📢 Арналарды басқару", callback_data="admin:manage_channels")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="📣 /send — Тарату", callback_data="admin:send")
    kb.adjust(1)
    return kb.as_markup()


def user_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Іздеу", callback_data="user:search")
    kb.button(text="📝 Мод сұрау", callback_data="user:request")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb(cb: str = "common:cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Болдырмау", callback_data=cb)]])


def manage_channels_kb(channels: List[Tuple[int, str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Міндетті арна қосу", callback_data="admin:add_channel")
    if channels:
        for chat_id, title, _link in channels:
            kb.button(text=f"❌ Өшіру: {title}", callback_data=f"admin:remove_channel:{chat_id}")
    kb.button(text="⬅️ Артқа", callback_data="admin:back")
    kb.adjust(1)
    return kb.as_markup()


def subscription_check_kb(missing: List[Tuple[int, str, str]], app_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for _cid, title, link in missing:
        kb.row(InlineKeyboardButton(text=f"Қосылу: {title}", url=link))
    kb.row(InlineKeyboardButton(text="Тексеру ✅", callback_data=f"user:verify:{app_id}"))
    return kb.as_markup()


def download_button_kb(bot_username: str, app_id: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=app_{app_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Жүктеу 📥", url=url)]])


def send_target_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📣 Арнаға жіберу", callback_data="send:target:channel")
    kb.button(text="👥 Барлық қолданушыға", callback_data="send:target:users")
    kb.button(text="⬅️ Болдырмау", callback_data="common:cancel")
    kb.adjust(1)
    return kb.as_markup()


def send_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Мәтін", callback_data="send:type:text")
    kb.button(text="🖼 Фото", callback_data="send:type:photo")
    kb.button(text="🎥 Видео", callback_data="send:type:video")
    kb.button(text="🎞 GIF", callback_data="send:type:gif")
    kb.button(text="⬅️ Болдырмау", callback_data="common:cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def send_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Жіберу", callback_data="send:confirm")
    kb.button(text="⬅️ Болдырмау", callback_data="common:cancel")
    kb.adjust(2)
    return kb.as_markup()


# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def safe(text: str) -> str:
    return html.escape(text or "")


def virustotal_url(sha256: Optional[str], file_name: Optional[str]) -> str:
    if sha256:
        return f"https://www.virustotal.com/gui/file/{sha256}"
    q = (file_name or "apk").strip()
    return f"https://www.virustotal.com/gui/search/{quote(q)}"


async def download_file_bytes(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(tg_file.file_path, buf)
    return buf.getvalue()


async def try_compute_sha256(bot: Bot, file_id: str, file_size: Optional[int]) -> Optional[str]:
    try:
        if file_size is not None and file_size > MAX_BYTES_TO_HASH:
            return None
        data = await download_file_bytes(bot, file_id)
        return hashlib.sha256(data).hexdigest()
    except Exception as e:
        logger.warning("SHA256 есептеу сәтсіз болды: %r", e)
        return None


async def check_user_subscriptions(bot: Bot, user_id: int) -> Tuple[bool, List[Tuple[int, str, str]]]:
    channels = db.list_channels()
    if not channels:
        return True, []

    missing: List[Tuple[int, str, str]] = []
    for chat_id, title, link in channels:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append((chat_id, title, link))
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning("Жазылым тексерілмеді (ботта рұқсат жоқ болуы мүмкін): %s (%s): %r", title, chat_id, e)
            missing.append((chat_id, title, link))
        except Exception as e:
            logger.exception("Жазылым тексерісінде күтпеген қате: %r", e)
            missing.append((chat_id, title, link))

    return (len(missing) == 0), missing


def _rounded_corners_rgba(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def generate_preview_image(icon_bytes: bytes, app_name: str) -> bytes:
    """
    1920x1080 preview:
    - Фон: #000033
    - TL: badge.png
    - TR: logo.png
    - Center: icon (<=500x500, rounded)
    - Below: app_name (auto-fit)
    """
    bg = Image.new("RGB", (PREVIEW_W, PREVIEW_H), PREVIEW_BG)
    canvas = bg.convert("RGBA")

    logo = Image.open(ASSET_LOGO).convert("RGBA")
    badge = Image.open(ASSET_BADGE).convert("RGBA")

    margin = 40
    max_h = 140

    if logo.height > max_h:
        r = max_h / float(logo.height)
        logo = logo.resize((int(logo.width * r), int(logo.height * r)), Image.LANCZOS)
    if badge.height > max_h:
        r = max_h / float(badge.height)
        badge = badge.resize((int(badge.width * r), int(badge.height * r)), Image.LANCZOS)

    canvas.alpha_composite(badge, (margin, margin))
    canvas.alpha_composite(logo, (PREVIEW_W - logo.width - margin, margin))

    icon = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
    icon.thumbnail((500, 500), Image.LANCZOS)
    icon = _rounded_corners_rgba(icon, radius=60)

    icon_x = (PREVIEW_W - icon.width) // 2
    icon_y = 240
    canvas.alpha_composite(icon, (icon_x, icon_y))

    draw = ImageDraw.Draw(canvas)
    max_text_width = 1600
    font_size = 96

    try:
        font = ImageFont.truetype(str(ASSET_FONT), font_size)
    except Exception:
        font = ImageFont.load_default()
        font_size = 24

    if isinstance(font, ImageFont.FreeTypeFont):
        while font_size >= 24:
            font = ImageFont.truetype(str(ASSET_FONT), font_size)
            bbox = draw.textbbox((0, 0), app_name, font=font)
            text_w = bbox[2] - bbox[0]
            if text_w <= max_text_width:
                break
            font_size -= 4

    bbox = draw.textbbox((0, 0), app_name, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (PREVIEW_W - text_w) // 2
    text_y = icon_y + icon.height + 50

    draw.text((text_x + 2, text_y + 2), app_name, font=font, fill=(0, 0, 0, 140))
    draw.text((text_x, text_y), app_name, font=font, fill=(255, 255, 255, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=92, optimize=True, progressive=True)
    return out.getvalue()


def _truncate(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def build_channel_caption_kz(
    name: str,
    description: str,
    version: str,
    features: str,
    category: str,
    vt_url: str,
) -> str:
    """
    Талаптағы caption (Қазақша, HTML).
    Photo caption лимиті: 1024. Қажет болса мәтіндер қысқартылады.
    """
    desc = _truncate(description, 320)
    feat = _truncate(features, 420)
    cat = _truncate(category, 140)

    def render(d: str, f: str, c: str) -> str:
        return (
            f"<b>📦 Қолданба атауы:</b> {safe(name)}\n"
            f"<b>📝 Қолданба сипаттамасы:</b> {safe(d)}\n"
            f"<b>🆙 Нұсқасы:</b> {safe(version)}\n"
            f"<b>✨ Мод мүмкіндіктері:</b> {safe(f)}\n"
            f"<b>📂 Санат:</b> {safe(c)}\n\n"
            f"<b>🔎 VirusTotal нәтижесі:</b> <a href=\"{vt_url}\">Тексеру есебі</a>\n\n"
            f"<i>{safe(SLOGAN)}</i>"
        )

    cap = render(desc, feat, cat)
    if len(cap) <= 1024:
        return cap

    # Әлі сыймаса – қысқарту цикл
    for _ in range(120):
        if len(cap) <= 1024:
            break
        if len(feat) > 80:
            feat = _truncate(feat, len(feat) - 10)
        elif len(desc) > 80:
            desc = _truncate(desc, len(desc) - 10)
        elif len(cat) > 30:
            cat = _truncate(cat, len(cat) - 5)
        else:
            break
        cap = render(desc, feat, cat)

    return cap[:1024]


def build_user_doc_caption_kz(app: Dict[str, Any]) -> str:
    return (
        f"<b>{safe(app.get('app_name') or '')}</b>\n"
        f"<b>Нұсқасы:</b> {safe(app.get('version') or '-')}\n"
        f"<b>Санат:</b> {safe(app.get('category') or '-')}\n\n"
        f"<b>Сипаттамасы:</b> {safe(app.get('description') or '-')}\n\n"
        f"<b>Мод мүмкіндіктері:</b>\n{safe(app.get('features') or '')}"
    )


def parse_inline_buttons(text: str, limit: int = 20) -> Tuple[Optional[InlineKeyboardMarkup], List[str]]:
    """
    Формат: Әр жол => "Атауы - URL"
    Қате жолдар еленбейді, тізімі қайтарылады (бот құламайды).
    """
    if not text or not text.strip():
        return None, []

    kb = InlineKeyboardBuilder()
    bad: List[str] = []
    count = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "-" not in line:
            bad.append(raw)
            continue

        name, url = [p.strip() for p in line.split("-", 1)]
        if not name or not url:
            bad.append(raw)
            continue

        if url.startswith("t.me/"):
            url = "https://" + url

        # минимал URL sanity
        if not (url.startswith("http://") or url.startswith("https://")):
            bad.append(raw)
            continue

        try:
            kb.row(InlineKeyboardButton(text=name, url=url))
            count += 1
            if count >= limit:
                break
        except Exception:
            bad.append(raw)

    if count == 0:
        return None, bad

    return kb.as_markup(), bad


# =========================
# ROUTER
# =========================
router = Router()
BOT_USERNAME: Optional[str] = None


# =========================
# COMMON CANCEL
# =========================
@router.callback_query(F.data == "common:cancel")
async def common_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Болдырмау орындалды.")
    if is_admin(callback.from_user.id):
        await callback.message.edit_text("Әкімші панелі:", reply_markup=admin_menu_kb())
    else:
        await callback.message.edit_text("Мәзір:", reply_markup=user_menu_kb())


# =========================
# START + DEEP-LINK + VERIFY
# =========================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    if message.from_user:
        db.upsert_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name,
        )

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    # deep-link: app_<id>
    if payload:
        if not payload.startswith("app_"):
            await message.answer("Қате параметр. Сілтеме дұрыс емес.")
            return

        try:
            app_id = int(payload.replace("app_", "", 1))
        except ValueError:
            await message.answer("Қате қолданба ID.")
            return

        app = db.get_app(app_id)
        if not app:
            await message.answer("Бұл қолданба табылмады (өшірілген болуы мүмкін).")
            return

        ok, missing = await check_user_subscriptions(bot, message.from_user.id)
        if not ok:
            lines = ["Файлды жүктеу үшін алдымен мына арналарға тіркеліңіз:\n"]
            for _cid, title, link in missing:
                lines.append(f"• {safe(title)} — {safe(link)}")
            await message.answer(
                "\n".join(lines),
                reply_markup=subscription_check_kb(missing=missing, app_id=app_id),
                disable_web_page_preview=True,
            )
            return

        # алдымен ұран
        await message.answer(f"<i>{safe(SLOGAN)}</i>")
        try:
            await message.answer_document(document=app["file_id"], caption=build_user_doc_caption_kz(app))
            db.add_download(message.from_user.id, app_id)
        except TelegramBadRequest as e:
            logger.warning("Пайдаланушыға APK жіберу сәтсіз: %r", e)
            await message.answer("APK жіберу сәтсіз болды. Кейінірек қайта көріңіз.")
        return

    # admin menu
    if message.from_user and is_admin(message.from_user.id):
        await message.answer("Әкімші панелі:", reply_markup=admin_menu_kb())
        return

    await message.answer(
        "Қош келдіңіз!\n\n"
        "Жүктеу үшін арнадағы посттан «Жүктеу 📥» батырмасын басыңыз.\n\n"
        "Қосымша:",
        reply_markup=user_menu_kb(),
    )


@router.callback_query(F.data.startswith("user:verify:"))
async def user_verify(callback: CallbackQuery, bot: Bot):
    db.upsert_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
        callback.from_user.last_name,
    )

    try:
        app_id = int(callback.data.split(":")[-1])
    except Exception:
        await callback.answer("Қате сұрау.", show_alert=True)
        return

    app = db.get_app(app_id)
    if not app:
        await callback.answer("Қолданба табылмады.", show_alert=True)
        return

    ok, missing = await check_user_subscriptions(bot, callback.from_user.id)
    if not ok:
        await callback.answer("Әлі де барлық арналарға тіркелмегенсіз.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=subscription_check_kb(missing=missing, app_id=app_id))
        except Exception:
            pass
        return

    await callback.answer("Тіркелу расталды ✅")

    await callback.message.answer(f"<i>{safe(SLOGAN)}</i>")
    try:
        await callback.message.answer_document(document=app["file_id"], caption=build_user_doc_caption_kz(app))
        db.add_download(callback.from_user.id, app_id)
    except TelegramBadRequest:
        await callback.message.answer("APK жіберу сәтсіз болды. Кейінірек қайта көріңіз.")


# =========================
# USER: SEARCH
# =========================
@router.callback_query(F.data == "user:search")
async def user_search_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SearchFSM.waiting_query)
    await callback.message.edit_text("Іздеу үшін қолданба атауын жазыңыз:", reply_markup=cancel_kb())
    await callback.answer()


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    if message.from_user:
        db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await state.clear()
        await state.set_state(SearchFSM.waiting_query)
        await message.answer("Іздеу үшін қолданба атауын жазыңыз:", reply_markup=cancel_kb())
        return
    await do_search(message, parts[1].strip())


@router.message(SearchFSM.waiting_query, F.text)
async def user_search_query(message: Message, state: FSMContext):
    await do_search(message, message.text.strip())
    await state.clear()


async def do_search(message: Message, query: str):
    if not query or len(query) < 2:
        await message.answer("Іздеу сөзі тым қысқа. Кемінде 2 әріп енгізіңіз.")
        return

    results = db.search_apps(query, limit=10)
    if not results:
        await message.answer("Ештеңе табылмады. Басқа сөзбен іздеп көріңіз.")
        return

    bot_username = BOT_USERNAME or "YourBot"
    kb = InlineKeyboardBuilder()
    lines = ["<b>Табылған қолданбалар:</b>\n"]

    for app_id, app_name, version, category in results:
        ver = version or "-"
        cat = category or "-"
        lines.append(f"• <b>{safe(app_name)}</b> (<code>{safe(ver)}</code>)  <i>{safe(cat)}</i>")
        kb.row(InlineKeyboardButton(text=f"Жүктеу 📥 — {app_name}", url=f"https://t.me/{bot_username}?start=app_{app_id}"))

    kb.row(InlineKeyboardButton(text="📝 Мод сұрау", callback_data="user:request"))
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), disable_web_page_preview=True)


# =========================
# USER: REQUEST MOD
# =========================
@router.callback_query(F.data == "user:request")
async def user_request_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(RequestFSM.waiting_text)
    await callback.message.edit_text(
        "Қандай мод керек екенін жазыңыз.\n\n"
        "Мысал:\n"
        "• Қолданба атауы\n"
        "• Нұсқасы\n"
        "• Қажет мүмкіндіктер\n",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(RequestFSM.waiting_text, F.text)
async def user_request_send(message: Message, bot: Bot, state: FSMContext):
    db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)

    req = message.text.strip()
    if len(req) < 5:
        await message.answer("Сұраныс тым қысқа. Толығырақ жазыңыз.")
        return

    u = message.from_user
    user_link = f"<a href='tg://user?id={u.id}'>{safe(u.first_name or 'Пайдаланушы')}</a>"
    uname = f"@{u.username}" if u.username else "—"

    text = (
        "<b>📝 Жаңа мод сұранысы</b>\n\n"
        f"<b>Кімнен:</b> {user_link}\n"
        f"<b>Username:</b> {safe(uname)}\n"
        f"<b>ID:</b> <code>{u.id}</code>\n\n"
        f"<b>Сұраныс:</b>\n{safe(req)}"
    )

    try:
        await bot.send_message(ADMIN_ID, text)
        await message.answer("✅ Сұранысыңыз қабылданды! Әкімші қарайды.")
    except Exception as e:
        logger.error("Әкімшіге сұраныс жіберу сәтсіз: %s", e, exc_info=True)
        await message.answer("❌ Қате болды. Кейінірек қайта көріңіз.")
    finally:
        await state.clear()


# =========================
# ADMIN: STATS
# =========================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await send_stats(message, edit=False)


@router.callback_query(F.data == "admin:stats")
async def admin_stats_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return
    await callback.answer()
    await send_stats(callback.message, edit=True)


async def send_stats(msg: Message, edit: bool):
    total_users = db.count_users()
    total_apps = db.count_apps()
    top = db.most_downloaded()

    top_text = "—"
    if top:
        _id, name, cnt = top
        top_text = f"{safe(name)} — <b>{cnt}</b> рет"

    text = (
        "<b>📊 Статистика</b>\n\n"
        f"👥 <b>Қолданушылар:</b> {total_users}\n"
        f"📦 <b>Қолданбалар:</b> {total_apps}\n"
        f"🏆 <b>Ең көп жүктелген:</b> {top_text}"
    )
    if edit:
        await msg.edit_text(text, reply_markup=admin_menu_kb())
    else:
        await msg.answer(text, reply_markup=admin_menu_kb())


# =========================
# ADMIN: MENU BACK + MANAGE CHANNELS
# =========================
@router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return
    await callback.message.edit_text("Әкімші панелі:", reply_markup=admin_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "admin:manage_channels")
async def admin_manage_channels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return

    channels = db.list_channels()
    text = "<b>📢 Міндетті арналар</b>\n\n"
    if not channels:
        text += "— Міндетті арналар орнатылмаған.\n"
    else:
        for chat_id, title, link in channels:
            text += f"• <b>{safe(title)}</b> (<code>{chat_id}</code>)\n{safe(link)}\n\n"

    await callback.message.edit_text(text, disable_web_page_preview=True, reply_markup=manage_channels_kb(channels))
    await callback.answer()


@router.callback_query(F.data == "admin:add_channel")
async def admin_add_channel_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return
    await state.clear()
    await state.set_state(ChannelFSM.waiting_channel)
    await callback.message.edit_text(
        "Арнаны @username (ашық арна) немесе сандық Channel ID (жабық арна) түрінде жіберіңіз.\n\n"
        "Мысал:\n• @MyChannel\n• -1001234567890\n\n"
        "Ескерту: жабық арна болса, бот әкімші болуы тиіс (invite құқықтары керек).",
        disable_web_page_preview=True,
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(ChannelFSM.waiting_channel, F.text)
async def admin_add_channel_receive(message: Message, bot: Bot, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    raw = message.text.strip()
    chat_ref: Any
    if raw.startswith("@"):
        chat_ref = raw
    else:
        try:
            chat_ref = int(raw)
        except ValueError:
            await message.answer("Қате енгізу. @username немесе сандық Channel ID жіберіңіз.")
            return

    try:
        chat = await bot.get_chat(chat_ref)
        if chat.type != ChatType.CHANNEL:
            await message.answer("Бұл чат арна емес. Тек арнаны жіберіңіз.")
            return

        title = chat.title or "Арна"
        if chat.username:
            invite_link = f"https://t.me/{chat.username}"
        else:
            inv = await bot.create_chat_invite_link(chat.id, creates_join_request=False)
            invite_link = inv.invite_link

        db.add_channel(chat_id=chat.id, title=title, invite_link=invite_link)
        await state.clear()

        await message.answer(
            f"✅ Міндетті арна қосылды: <b>{safe(title)}</b>\n{safe(invite_link)}",
            disable_web_page_preview=True,
            reply_markup=manage_channels_kb(db.list_channels()),
        )
    except Exception as e:
        logger.error("Арнаны қосу кезінде қате: %s", e, exc_info=True)
        await message.answer(f"❌ Қате: {e}")


@router.callback_query(F.data.startswith("admin:remove_channel:"))
async def admin_remove_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return

    try:
        chat_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Қате арна ID.", show_alert=True)
        return

    db.remove_channel(chat_id)
    channels = db.list_channels()
    await callback.message.edit_text("✅ Міндетті арна өшірілді.", reply_markup=manage_channels_kb(channels))
    await callback.answer("Өшірілді ✅")


# =========================
# ADMIN: ADD APP FLOW (UPDATED 7 STEPS)
# =========================
@router.callback_query(F.data == "admin:add_app")
async def admin_add_app_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddAppFSM.waiting_apk)
    await callback.message.edit_text(
        "1/7) APK файлын <b>құжат</b> (document) ретінде жіберіңіз (.apk).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddAppFSM.waiting_apk, F.document)
async def admin_add_app_apk(message: Message, bot: Bot, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    doc = message.document
    file_name = doc.file_name or ""
    if not file_name.lower().endswith(".apk"):
        await message.answer("Бұл APK сияқты емес. .apk файлын жіберіңіз.", reply_markup=cancel_kb())
        return

    await state.update_data(file_id=doc.file_id, file_name=file_name, file_size=doc.file_size)
    sha256 = await try_compute_sha256(bot, doc.file_id, doc.file_size)
    await state.update_data(sha256=sha256)

    await state.set_state(AddAppFSM.waiting_icon)
    await message.answer("2/7) Енді иконканы жіберіңіз (Photo).", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_apk)
async def admin_add_app_apk_invalid(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer("APK-ны құжат (document) ретінде жіберіңіз (.apk).", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_icon, F.photo)
async def admin_add_app_icon(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    icon_file_id = message.photo[-1].file_id
    await state.update_data(icon_file_id=icon_file_id)

    await state.set_state(AddAppFSM.waiting_name)
    await message.answer("3/7) Қолданба атауын жазыңыз:", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_icon)
async def admin_add_app_icon_invalid(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer("Иконканы Photo ретінде жіберіңіз.", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_name, F.text)
async def admin_add_app_name(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Атауы тым қысқа. Қайта жазыңыз.")
        return
    await state.update_data(app_name=name)
    await state.set_state(AddAppFSM.waiting_description)
    await message.answer("4/7) Қолданба сипаттамасын жазыңыз (жалпы ақпарат):", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_description, F.text)
async def admin_add_app_description(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    desc = message.text.strip()
    if len(desc) < 5:
        await message.answer("Сипаттама тым қысқа. Толығырақ жазыңыз.")
        return
    await state.update_data(description=desc)
    await state.set_state(AddAppFSM.waiting_version)
    await message.answer("5/7) Нұсқасын енгізіңіз (мысалы: 1.2.3):", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_version, F.text)
async def admin_add_app_version(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    version = message.text.strip()
    if not version:
        await message.answer("Нұсқа бос болмауы керек. Қайта енгізіңіз.")
        return
    await state.update_data(version=version)
    await state.set_state(AddAppFSM.waiting_features)
    await message.answer("6/7) Мод мүмкіндіктерін жазыңыз:", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_features, F.text)
async def admin_add_app_features(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    features = message.text.strip()
    if len(features) < 2:
        await message.answer("Мәтін тым қысқа. Қайта жазыңыз.")
        return
    await state.update_data(features=features)
    await state.set_state(AddAppFSM.waiting_category)
    await message.answer("7/7) Санат/тегтерді енгізіңіз (мысалы: #Games #Action):", reply_markup=cancel_kb())


@router.message(AddAppFSM.waiting_category, F.text)
async def admin_add_app_category_and_post(message: Message, bot: Bot, state: FSMContext):
    """
    Соңғы қадам:
    - preview генерациясы (иконкадан)
    - DB-ға сақтау (description, category қоса)
    - Арнаға фото пост + Download батырма
    - Админге нәтиже
    """
    if not message.from_user or not is_admin(message.from_user.id):
        return

    category = message.text.strip()
    if len(category) < 2:
        await message.answer("Санат/тег тым қысқа. Қайта енгізіңіз.")
        return

    data = await state.get_data()
    file_id = data["file_id"]
    file_name = data.get("file_name")
    sha256 = data.get("sha256")
    icon_file_id = data.get("icon_file_id")
    app_name = data.get("app_name") or ""
    description = data.get("description") or ""
    version = data.get("version") or ""
    features = data.get("features") or ""

    vt_url = virustotal_url(sha256=sha256, file_name=file_name)

    app_id = db.add_app(
        app_name=app_name,
        description=description,
        version=version,
        features=features,
        category=category,
        file_id=file_id,
        file_name=file_name,
        sha256=sha256,
        vt_url=vt_url,
    )

    caption = build_channel_caption_kz(
        name=app_name,
        description=description,
        version=version,
        features=features,
        category=category,
        vt_url=vt_url,
    )

    try:
        bot_username = BOT_USERNAME or (await bot.get_me()).username

        # Preview генерациясы, құласа fallback ретінде иконканы қолдану
        preview_photo: Any
        try:
            if not (ASSET_LOGO.exists() and ASSET_BADGE.exists() and ASSET_FONT.exists()):
                raise FileNotFoundError(
                    f"assets табылмады: logo={ASSET_LOGO.exists()}, badge={ASSET_BADGE.exists()}, font={ASSET_FONT.exists()}"
                )
            icon_bytes = await download_file_bytes(bot, icon_file_id)
            preview_bytes = generate_preview_image(icon_bytes=icon_bytes, app_name=app_name)
            preview_photo = BufferedInputFile(preview_bytes, filename="preview.jpg")
        except Exception as e:
            logger.error("Preview генерациясы сәтсіз болды, fallback қолданылды: %s", e, exc_info=True)
            preview_photo = icon_file_id

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=preview_photo,
            caption=caption,
            reply_markup=download_button_kb(bot_username=bot_username, app_id=app_id),
        )

        await message.answer("✅ Мод арнаға сәтті жарияланды!", reply_markup=admin_menu_kb())

    except Exception as e:
        logger.error("Арнаға жариялау сәтсіз болды: %s", e, exc_info=True)
        await message.answer(f"❌ Арнаға жариялау сәтсіз болды. Қате: {e}", reply_markup=admin_menu_kb())
    finally:
        await state.clear()


# =========================
# ADMIN: ADVANCED BROADCAST /send
# =========================
@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(SendFSM.choosing_target)
    await message.answer("Қайда жібереміз?", reply_markup=send_target_kb())


@router.callback_query(F.data == "admin:send")
async def admin_send_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return
    await state.clear()
    await state.set_state(SendFSM.choosing_target)
    await callback.message.edit_text("Қайда жібереміз?", reply_markup=send_target_kb())
    await callback.answer()


@router.callback_query(SendFSM.choosing_target, F.data.startswith("send:target:"))
async def send_choose_target(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return

    target = callback.data.split(":")[-1]  # channel/users
    await state.update_data(target=target)
    await state.set_state(SendFSM.choosing_type)
    await callback.message.edit_text("Хабарлама түрін таңдаңыз:", reply_markup=send_type_kb())
    await callback.answer()


@router.callback_query(SendFSM.choosing_type, F.data.startswith("send:type:"))
async def send_choose_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return

    mtype = callback.data.split(":")[-1]  # text/photo/video/gif
    await state.update_data(mtype=mtype)
    await state.set_state(SendFSM.waiting_content)

    if mtype == "text":
        prompt = "Мәтінді жіберіңіз:"
    elif mtype == "photo":
        prompt = "Фото жіберіңіз (қаласаңыз caption қосыңыз):"
    elif mtype == "video":
        prompt = "Видео жіберіңіз (қаласаңыз caption қосыңыз):"
    else:
        prompt = "GIF жіберіңіз (animation ретінде, қаласаңыз caption қосыңыз):"

    await callback.message.edit_text(prompt, reply_markup=cancel_kb())
    await callback.answer()


@router.message(SendFSM.waiting_content)
async def send_receive_content(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    mtype = data.get("mtype")

    payload: Dict[str, Any] = {}

    if mtype == "text":
        if not message.text:
            await message.answer("Тек мәтін жіберіңіз.")
            return
        payload["text"] = message.text

    elif mtype == "photo":
        if not message.photo:
            await message.answer("Фото жіберіңіз.")
            return
        payload["file_id"] = message.photo[-1].file_id
        payload["caption"] = message.caption or ""

    elif mtype == "video":
        if not message.video:
            await message.answer("Видео жіберіңіз.")
            return
        payload["file_id"] = message.video.file_id
        payload["caption"] = message.caption or ""

    elif mtype == "gif":
        if message.animation:
            payload["file_id"] = message.animation.file_id
            payload["caption"] = message.caption or ""
        elif message.document and (message.document.mime_type or "").lower() in ("video/mp4", "image/gif"):
            payload["file_id"] = message.document.file_id
            payload["caption"] = message.caption or ""
        else:
            await message.answer("GIF жіберіңіз (animation ретінде).")
            return

    else:
        await message.answer("Белгісіз түр. /send қайта бастаңыз.")
        await state.clear()
        return

    await state.update_data(payload=payload)
    await state.set_state(SendFSM.waiting_buttons)

    await message.answer(
        "Inline батырмалар қосқыңыз келе ме?\n\n"
        "Формат: <code>Батырма атауы - URL</code> (әр жолға бір батырма)\n"
        "Мысал:\n"
        "<code>Telegram - https://t.me</code>\n"
        "<code>Сайт - https://example.com</code>\n\n"
        "Қоспасаңыз, <b>жоқ</b> деп жазыңыз.",
        reply_markup=cancel_kb(),
        disable_web_page_preview=True,
    )


@router.message(SendFSM.waiting_buttons, F.text)
async def send_receive_buttons(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    raw = message.text.strip()
    if raw.lower() in ("жоқ", "нет", "no", "none", "skip", "өткізу", "өткізіп жіберу"):
        await state.update_data(buttons=None, bad_lines=[])
    else:
        kb, bad = parse_inline_buttons(raw)
        await state.update_data(buttons=kb, bad_lines=bad)

    await state.set_state(SendFSM.confirm)

    data = await state.get_data()
    target = data.get("target")
    mtype = data.get("mtype")
    bad_lines = data.get("bad_lines") or []

    target_text = "Арнаға" if target == "channel" else "Барлық қолданушыға"
    type_text = {"text": "Мәтін", "photo": "Фото", "video": "Видео", "gif": "GIF"}.get(mtype, "Белгісіз")

    warn = ""
    if bad_lines:
        warn = "\n\n<b>Ескерту:</b> Кейбір батырма жолдары танылмады және еленбеді:\n" + "\n".join(
            f"• <code>{safe(line)}</code>" for line in bad_lines[:10]
        )

    await message.answer(
        f"<b>Жіберуге дайын:</b>\n"
        f"• <b>Мақсат:</b> {target_text}\n"
        f"• <b>Түрі:</b> {type_text}"
        f"{warn}\n\n"
        "Жібереміз бе?",
        reply_markup=send_confirm_kb(),
    )


@router.callback_query(SendFSM.confirm, F.data == "send:confirm")
async def send_confirm(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Қатынауға рұқсат жоқ.", show_alert=True)
        return

    data = await state.get_data()
    target = data.get("target")
    mtype = data.get("mtype")
    payload: Dict[str, Any] = data.get("payload") or {}
    buttons: Optional[InlineKeyboardMarkup] = data.get("buttons")

    await callback.answer("Жіберілуде...")

    async def send_one(chat_id: int):
        if mtype == "text":
            await bot.send_message(chat_id=chat_id, text=payload["text"], reply_markup=buttons)
        elif mtype == "photo":
            await bot.send_photo(chat_id=chat_id, photo=payload["file_id"], caption=payload.get("caption") or "", reply_markup=buttons)
        elif mtype == "video":
            await bot.send_video(chat_id=chat_id, video=payload["file_id"], caption=payload.get("caption") or "", reply_markup=buttons)
        elif mtype == "gif":
            await bot.send_animation(chat_id=chat_id, animation=payload["file_id"], caption=payload.get("caption") or "", reply_markup=buttons)
        else:
            raise RuntimeError("Белгісіз түр")

    sent = 0
    failed = 0

    try:
        if target == "channel":
            await send_one(CHANNEL_ID)
            sent = 1
        else:
            user_ids = [uid for uid in db.list_user_ids() if uid != ADMIN_ID]
            if not user_ids:
                await callback.message.answer("Қолданушылар табылмады.")
                await state.clear()
                return

            for uid in user_ids:
                try:
                    await send_one(uid)
                    sent += 1
                except (TelegramForbiddenError, TelegramBadRequest):
                    failed += 1
                except Exception as e:
                    failed += 1
                    logger.warning("Жіберу қате (user_id=%s): %r", uid, e)
                await asyncio.sleep(0.05)

        await callback.message.answer(
            "✅ Жіберу аяқталды.\n\n"
            f"Жіберілді: <b>{sent}</b>\n"
            f"Сәтсіз: <b>{failed}</b>",
            reply_markup=admin_menu_kb(),
        )
    except Exception as e:
        logger.error("send операциясы сәтсіз: %s", e, exc_info=True)
        await callback.message.answer(f"❌ Жіберу сәтсіз болды. Қате: {e}", reply_markup=admin_menu_kb())
    finally:
        await state.clear()


# =========================
# STARTUP
# =========================
async def main():
    global BOT_USERNAME

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    db.ensure_schema()

    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Бот іске қосылды: @%s", BOT_USERNAME)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await bot.send_message(ADMIN_ID, "✅ Бот іске қосылды және жұмыс істеп тұр.")
    except Exception:
        logger.warning("Әкімшіге старт хабарламасын жіберу мүмкін болмады.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот тоқтатылды (KeyboardInterrupt).")