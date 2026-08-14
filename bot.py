import asyncio
from difflib import SequenceMatcher
import html
import logging
import os
import re
import secrets
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import certifi
from aiohttp import ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError


load_dotenv()


def configure_curl_certificates() -> None:
    certificate_dir = Path(tempfile.gettempdir()) / "telegram_media_bot"
    certificate_dir.mkdir(exist_ok=True)
    certificate_path = certificate_dir / "cacert.pem"
    source = Path(certifi.where())
    if not certificate_path.exists() or certificate_path.stat().st_size != source.stat().st_size:
        shutil.copy2(source, certificate_path)
    os.environ["CURL_CA_BUNDLE"] = str(certificate_path)
    os.environ["SSL_CERT_FILE"] = str(certificate_path)


configure_curl_certificates()

ARGOS_DATA_DIR = Path(__file__).parent / ".argos"
os.environ.setdefault("XDG_DATA_HOME", str(ARGOS_DATA_DIR / "data"))
os.environ.setdefault("XDG_CONFIG_HOME", str(ARGOS_DATA_DIR / "config"))
os.environ.setdefault("XDG_CACHE_HOME", str(ARGOS_DATA_DIR / "cache"))
os.environ.setdefault("ARGOS_PACKAGES_DIR", str(ARGOS_DATA_DIR / "packages"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "5618399651"))
MAX_DURATION = int(os.getenv("MAX_VIDEO_DURATION", "3600"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "49")) * 1024 * 1024
LRCLIB_API_URL = os.getenv("LRCLIB_API_URL", "https://lrclib.net/api").rstrip("/")
ARGOS_SOURCE_LANGUAGE = os.getenv("ARGOS_SOURCE_LANGUAGE", "en").strip()
ARGOS_TARGET_LANGUAGE = os.getenv("ARGOS_TARGET_LANGUAGE", "ru").strip()

router = Router()
processing_lock = asyncio.Lock()
user_modes: dict[int, str] = {}
music_search_results: dict[int, list[dict]] = {}
MUSIC_SEARCHES_FILE = Path(__file__).parent / "music-searches.json"
pending_video_urls: dict[int, str] = {}
downloaded_tracks: dict[str, dict] = {}
lyrics_cache: dict[str, dict] = {}
lyrics_messages: dict[str, list[Message]] = {}
active_cancel_event: threading.Event | None = None
WELCOME_IMAGE = Path(__file__).parent / "assets" / "welcome-image.jpg"
WORK_STICKERS_FILE = Path(__file__).parent / "work-stickers.json"


def load_work_stickers() -> list[str]:
    try:
        data = json.loads(WORK_STICKERS_FILE.read_text(encoding="utf-8"))
        return [value for value in data if isinstance(value, str)]
    except (OSError, ValueError, TypeError):
        return []


work_stickers = load_work_stickers()


def load_music_searches() -> dict[str, list[dict]]:
    try:
        data = json.loads(MUSIC_SEARCHES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


music_searches = load_music_searches()


def save_music_searches() -> None:
    MUSIC_SEARCHES_FILE.write_text(
        json.dumps(music_searches, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def remember_music_search(entries: list[dict]) -> str:
    search_id = secrets.token_hex(4)
    music_searches[search_id] = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "webpage_url": item.get("webpage_url"),
            "duration": item.get("duration"),
        }
        for item in entries
    ]
    save_music_searches()
    return search_id


def save_work_stickers() -> None:
    WORK_STICKERS_FILE.write_text(
        json.dumps(work_stickers, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def send_work_sticker(message: Message) -> Message | None:
    if not work_stickers:
        return None
    try:
        return await message.answer_sticker(work_stickers[-1])
    except Exception:
        logging.exception("Unable to send work sticker")
        return None


async def delete_message_safely(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass

main_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Скачать видео", callback_data="download_video")],
        [InlineKeyboardButton(text="🎵 Найти музыку", callback_data="find_music")],
    ]
)

back_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Вернуться в главное меню", callback_data="main_menu")]
    ]
)

video_quality_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="1080p", callback_data="video_quality:1080"),
            InlineKeyboardButton(text="720p", callback_data="video_quality:720"),
            InlineKeyboardButton(text="480p", callback_data="video_quality:480"),
        ],
        [InlineKeyboardButton(text="🎵 MP3", callback_data="video_quality:mp3")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="main_menu")],
    ]
)

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "rutube.ru",
    "tiktok.com",
    "instagram.com",
)
MUSIC_LINK_DOMAINS = ("youtube.com", "youtu.be", "tiktok.com")


def allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ALLOWED_USER_ID)


def is_url_for(text: str, domains: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(text.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and any(
            host == domain or host.endswith(f".{domain}") for domain in domains
        )
    except ValueError:
        return False


def normalize_media_url(url: str) -> str:
    value = url.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    host = (parsed.hostname or "").lower()
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        match = re.search(r"/video/(\d+)", parsed.path)
        if match:
            return f"https://www.tiktok.com{parsed.path}"
    return value


def site_download_options(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return {
            "impersonate": ImpersonateTarget(
                client="chrome", version="131", os="android", os_version="14"
            )
        }
    return {}


def safe_title(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or "video"


def cancellation_hook(cancel_event: threading.Event):
    def hook(_status: dict) -> None:
        if cancel_event.is_set():
            raise DownloadError("Операция отменена командой /start")

    return hook


def reset_user_session(user_id: int) -> None:
    global active_cancel_event
    if active_cancel_event:
        active_cancel_event.set()
    user_modes.pop(user_id, None)
    pending_video_urls.pop(user_id, None)
    downloaded_tracks.clear()
    lyrics_cache.clear()
    lyrics_messages.clear()


def begin_processing() -> threading.Event:
    global active_cancel_event
    active_cancel_event = threading.Event()
    return active_cancel_event


async def delete_later(message: Message, delay: int = 8) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


def schedule_delete(message: Message, delay: int = 8) -> None:
    asyncio.create_task(delete_later(message, delay))


def split_text(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        addition = len(line) + 1
        if current and current_length + addition > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            chunks.extend(line[index:index + limit] for index in range(0, len(line), limit))
        else:
            current.append(line)
            current_length += addition
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


async def send_long_text(message: Message, heading: str, text: str, reply_markup=None) -> list[Message]:
    chunks = split_text(text)
    sent_messages: list[Message] = []
    for index, chunk in enumerate(chunks):
        prefix = f"{heading}\n\n" if index == 0 else f"{heading} — часть {index + 1}\n\n"
        sent_messages.append(
            await message.answer(
                prefix + chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        )
    return sent_messages


async def send_greeting_with_retry(message: Message, greeting: str) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            if WELCOME_IMAGE.exists():
                await message.answer_photo(
                    photo=FSInputFile(WELCOME_IMAGE),
                    caption=greeting,
                    parse_mode="HTML",
                    reply_markup=main_menu,
                )
            else:
                await message.answer(greeting, parse_mode="HTML", reply_markup=main_menu)
            return
        except TelegramNetworkError as error:
            last_error = error
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error


def ffmpeg_executable(target_dir: Path) -> Path:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return Path(system_ffmpeg)

    directory = target_dir / "ffmpeg"
    directory.mkdir(exist_ok=True)
    bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    executable = directory / bundled.name
    if not executable.exists():
        shutil.copy2(bundled, executable)
    return executable


def ffmpeg_location(target_dir: Path) -> str:
    return str(ffmpeg_executable(target_dir))


def download_video(
    url: str,
    target_dir: Path,
    cancel_event: threading.Event,
    height: int = 1080,
) -> tuple[Path, str, int | None]:
    common = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 5,
        "progress_hooks": [cancellation_hook(cancel_event)],
        **site_download_options(url),
    }
    options = {
        **common,
        "format": (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(target_dir / "video.%(ext)s"),
        "ffmpeg_location": ffmpeg_location(target_dir),
        "overwrites": True,
    }
    with YoutubeDL(options) as ydl:
        result = ydl.extract_info(url, download=True)
        requested = Path(ydl.prepare_filename(result))
    if result.get("_type") == "playlist":
        raise ValueError("Плейлисты пока не поддерживаются.")
    duration = result.get("duration")
    if duration and duration > MAX_DURATION:
        raise ValueError(f"Видео длиннее разрешённых {MAX_DURATION // 60} минут.")
    title = safe_title(result.get("title") or "video")
    output = target_dir / "video.mp4"
    if not output.exists() and requested.exists():
        output = requested
    if not output.exists():
        candidates = [path for path in target_dir.glob("video.*") if path.is_file()]
        if not candidates:
            raise RuntimeError("Скачанный файл не найден.")
        output = max(candidates, key=lambda path: path.stat().st_size)
    return output, title, duration


def download_audio(url: str, target_dir: Path, cancel_event: threading.Event) -> Path:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "retries": 5,
        "extractor_retries": 5,
        "outtmpl": str(target_dir / "source.%(ext)s"),
        "ffmpeg_location": ffmpeg_location(target_dir),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        "overwrites": True,
        "progress_hooks": [cancellation_hook(cancel_event)],
        **site_download_options(url),
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            raise ValueError(f"Видео длиннее разрешённых {MAX_DURATION // 60} минут.")
        ydl.download([url])
    output = target_dir / "source.mp3"
    if not output.exists():
        raise RuntimeError("Не удалось извлечь аудиодорожку.")
    return output


def extract_audio(video_path: Path, target_dir: Path) -> Path:
    output = target_dir / "uploaded.mp3"
    executable = ffmpeg_executable(target_dir)
    result = subprocess.run(
        [str(executable), "-y", "-i", str(video_path), "-vn", "-t", "120", "-ac", "1", "-ar", "44100", "-b:a", "128k", str(output)],
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0 or not output.exists():
        raise ValueError("В MP4 не удалось найти подходящую аудиодорожку.")
    return output


def search_music(query: str) -> list[dict]:
    options = {"quiet": True, "no_warnings": True, "extract_flat": True, "noplaylist": True}
    with YoutubeDL(options) as ydl:
        data = ydl.extract_info(f"ytsearch10:{query}", download=False)
    return list(data.get("entries") or [])


def format_search_results(entries: list[dict]) -> str:
    if not entries:
        return "Ничего не нашлось. Попробуйте указать исполнителя и название точнее."
    lines = ["🎵 <b>Результаты поиска:</b>", ""]
    for index, item in enumerate(entries, 1):
        title = html.escape(item.get("title") or "Без названия")
        duration = item.get("duration")
        minutes = f" {int(duration) // 60}:{int(duration) % 60:02d}" if duration else ""
        lines.append(f"<b>{index}.</b> {title}<b>{minutes}</b>")
    return "\n".join(lines)


def format_compact_variants(entries: list[dict]) -> str:
    lines = ["", "⬇️ <b>Выберите вариант для скачивания:</b>"]
    for index, item in enumerate(entries, 1):
        raw_title = item.get("title") or "Без названия"
        short_title = raw_title if len(raw_title) <= 48 else raw_title[:45].rstrip() + "…"
        duration = item.get("duration")
        length = f" <b>{int(duration) // 60}:{int(duration) % 60:02d}</b>" if duration else ""
        lines.append(f"<b>{index}.</b> {html.escape(short_title)}{length}")
    return "\n".join(lines)


def music_results_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    search_id = remember_music_search(entries)
    number_buttons = [
        InlineKeyboardButton(
            text=str(index), callback_data=f"music_pick:{search_id}:{index - 1}"
        )
        for index in range(1, len(entries) + 1)
    ]
    rows = [number_buttons[index:index + 5] for index in range(0, len(number_buttons), 5)]
    rows.append([InlineKeyboardButton(text="↩️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_url(item: dict) -> str:
    video_id = item.get("id")
    url = item.get("webpage_url") or item.get("url") or ""
    if url and url.startswith("http"):
        return url
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    if url:
        return f"https://www.youtube.com/watch?v={url}"
    raise ValueError("У выбранной композиции нет ссылки для загрузки.")


def download_selected_track(
    url: str, target_dir: Path, cancel_event: threading.Event
) -> tuple[Path, str, str | None, int | None]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "retries": 5,
        "extractor_retries": 5,
        "outtmpl": str(target_dir / "track.%(ext)s"),
        "ffmpeg_location": ffmpeg_location(target_dir),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "overwrites": True,
        "progress_hooks": [cancellation_hook(cancel_event)],
        **site_download_options(url),
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    output = target_dir / "track.mp3"
    if not output.exists():
        raise RuntimeError("Не удалось подготовить аудиофайл.")
    title = safe_title(info.get("track") or info.get("title") or "music")
    performer = info.get("artist") or info.get("uploader")
    return output, title, performer, info.get("duration")


def format_recognition(result: dict) -> tuple[str, str | None]:
    track = result.get("track") or {}
    if not track:
        return "Не удалось распознать композицию. Попробуйте другой фрагмент, где музыка звучит громче.", None
    title = html.escape(track.get("title") or "Неизвестная композиция")
    artist = html.escape(track.get("subtitle") or "Неизвестный исполнитель")
    link = track.get("url") or (track.get("share") or {}).get("href")
    text = f"✅ <b>{title}</b>\nИсполнитель: {artist}"
    if link:
        text += f'\n<a href="{html.escape(link, quote=True)}">Открыть композицию</a>'
    cover = ((track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"))
    return text, cover


def lyrics_button(track_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Текст и перевод", callback_data=f"lyrics:{track_id}")]
        ]
    )


def lyrics_switch_keyboard(track_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇬🇧 Оригинал", callback_data=f"lyrics_original:{track_id}"),
                InlineKeyboardButton(text="🇷🇺 Перевод", callback_data=f"lyrics_translation:{track_id}"),
            ],
            [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="find_music")],
        ]
    )


async def fetch_lyrics(title: str, artist: str | None) -> dict | None:
    clean_title = clean_track_name(title)
    clean_artist = clean_artist_name(artist or "")
    queries = [
        {"track_name": clean_title, "artist_name": clean_artist},
        {"q": f"{clean_artist} {clean_title}".strip()},
    ]
    if clean_title.casefold() != title.casefold():
        queries.append({"track_name": title, "artist_name": clean_artist})

    candidates: dict[str, dict] = {}
    headers = {"User-Agent": "SkachaykaMediaBot/1.0 (https://github.com/ksebsiefiu23fsd/tg-bottt)"}
    async with ClientSession(headers=headers) as session:
        for index, params in enumerate(queries):
            async with session.get(
                f"{LRCLIB_API_URL}/search",
                params=params,
                timeout=30,
            ) as response:
                if response.status == 429:
                    raise RuntimeError("LRCLIB временно ограничил частоту запросов")
                if response.status != 200:
                    raise RuntimeError(f"LRCLIB вернул ошибку {response.status}")
                data = await response.json()
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        candidates[str(item.get("id") or id(item))] = item
            if index + 1 < len(queries):
                await asyncio.sleep(0.25)

    ranked = sorted(
        candidates.values(),
        key=lambda item: lyrics_match_score(item, clean_title, clean_artist),
        reverse=True,
    )
    lyrics = next((item for item in ranked if lyrics_body(item)), None)
    if lyrics is None:
        return None
    body = lyrics_body(lyrics)
    return {
        "original": body,
        "language": detect_lyrics_language(body),
        "copyright": "Источник текста: LRCLIB",
        "matched_title": lyrics.get("trackName"),
        "matched_artist": lyrics.get("artistName"),
    }


def detect_lyrics_language(text: str) -> str:
    latin_count = len(re.findall(r"[A-Za-z]", text))
    cyrillic_count = len(re.findall(r"[А-Яа-яЁё]", text))
    return "ru" if cyrillic_count > latin_count else "en"


def clean_track_name(value: str) -> str:
    value = html.unescape(value).strip()
    value = re.sub(
        r"\s*[\[(](?:[^\])]*(?:official|video|audio|lyrics?|visuali[sz]er|remaster|live)[^\])]*)[\])]",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*[-–—|]\s*(?:official\s+)?(?:music\s+)?(?:video|audio|lyrics?|visuali[sz]er)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip(" -–—|")


def clean_artist_name(value: str) -> str:
    value = html.unescape(value).strip()
    value = re.sub(r"\s*[-–—]\s*Topic\s*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def comparable(value: str) -> str:
    value = clean_track_name(value).casefold()
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def text_similarity(left: str, right: str) -> float:
    left_value = comparable(left)
    right_value = comparable(right)
    if not left_value or not right_value:
        return 0.0
    sequence = SequenceMatcher(None, left_value, right_value).ratio()
    left_words = set(left_value.split())
    right_words = set(right_value.split())
    overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
    return sequence * 0.65 + overlap * 0.35


def lyrics_match_score(item: dict, title: str, artist: str) -> float:
    score = text_similarity(str(item.get("trackName") or ""), title) * 0.72
    if artist:
        score += text_similarity(str(item.get("artistName") or ""), artist) * 0.28
    if item.get("instrumental"):
        score -= 1.0
    if lyrics_body(item):
        score += 0.1
    return score


def lyrics_body(item: dict) -> str:
    plain = str(item.get("plainLyrics") or "").strip()
    if plain:
        return plain
    synced = str(item.get("syncedLyrics") or "").strip()
    if not synced:
        return ""
    lines = [re.sub(r"^(?:\[\d{1,2}:\d{2}(?:\.\d+)?\])+\s*", "", line) for line in synced.splitlines()]
    return "\n".join(lines).strip()


def translate_with_argos(text: str) -> str:
    import argostranslate.package
    import ctranslate2
    import sentencepiece

    package = next(
        (
            item
            for item in argostranslate.package.get_installed_packages()
            if item.from_code == ARGOS_SOURCE_LANGUAGE
            and item.to_code == ARGOS_TARGET_LANGUAGE
        ),
        None,
    )
    if package is None:
        raise RuntimeError("Модель Argos Translate en-ru не установлена")

    # SentencePiece and CTranslate2 cannot reliably open model files from a
    # Windows path containing non-ASCII characters. Keep an ASCII-only runtime
    # copy for projects whose directory name contains Cyrillic characters.
    runtime_package = Path(tempfile.gettempdir()) / "jelly_telegram_bot" / package.package_path.name
    runtime_model = runtime_package / "model"
    source_model = package.package_path / "model"
    source_sentencepiece = package.package_path / "sentencepiece.model"
    runtime_sentencepiece = runtime_package / "sentencepiece.model"
    source_binary = source_model / "model.bin"
    runtime_binary = runtime_model / "model.bin"
    if (
        not runtime_binary.exists()
        or runtime_binary.stat().st_size != source_binary.stat().st_size
        or not runtime_sentencepiece.exists()
        or runtime_sentencepiece.stat().st_size != source_sentencepiece.stat().st_size
    ):
        runtime_package.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_model, runtime_model, dirs_exist_ok=True)
        shutil.copy2(source_sentencepiece, runtime_sentencepiece)

    processor = sentencepiece.SentencePieceProcessor(
        model_file=str(runtime_sentencepiece)
    )
    translator = ctranslate2.Translator(str(runtime_model))
    lines = text.splitlines()
    unique_lines = list(dict.fromkeys(line for line in lines if line.strip()))
    batches = [processor.encode(line, out_type=str) for line in unique_lines]
    hypotheses = translator.translate_batch(batches, beam_size=4)
    translated_lines = {
        line: processor.decode(result.hypotheses[0])
        for line, result in zip(unique_lines, hypotheses)
    }
    result = [translated_lines.get(line, "") for line in lines]

    translation = "\n".join(result).strip()
    if not translation:
        raise RuntimeError("Argos Translate не вернул перевод")
    return translation


async def translate_to_russian(text: str) -> str:
    return await asyncio.to_thread(translate_with_argos, text)


def bilingual_lyrics_chunks(original: str, translation: str, limit: int = 3600) -> list[str]:
    original_lines = original.splitlines()
    translated_lines = translation.splitlines()
    blocks: list[str] = []
    for index, original_line in enumerate(original_lines):
        translated_line = translated_lines[index] if index < len(translated_lines) else ""
        if not original_line.strip() and not translated_line.strip():
            blocks.append("")
            continue
        blocks.append(
            f"{html.escape(original_line)}\n"
            f"  <i>{html.escape(translated_line)}</i>"
        )

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        addition = len(block) + 2
        if current and current_size + addition > limit:
            chunks.append("\n\n".join(current))
            current = []
            current_size = 0
        current.append(block)
        current_size += addition
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [""]


async def send_bilingual_lyrics(
    message: Message,
    heading: str,
    original: str,
    translation: str,
    reply_markup=None,
) -> list[Message]:
    chunks = bilingual_lyrics_chunks(original, translation)
    sent: list[Message] = []
    for index, chunk in enumerate(chunks):
        part = "" if index == 0 else f" — часть {index + 1}"
        sent.append(
            await message.answer(
                f"<b>{html.escape(heading + part)}</b>\n\n{chunk}",
                parse_mode="HTML",
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        )
    return sent


async def delete_lyrics_messages(track_id: str, current: Message | None = None) -> None:
    messages = lyrics_messages.pop(track_id, [])
    if current and all(item.message_id != current.message_id for item in messages):
        messages.append(current)
    for item in messages:
        await delete_message_safely(item)


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    user = message.from_user
    reset_user_session(user.id)
    name = user.first_name if user and user.first_name else "друг"
    greeting = (
        f"👋 Привет, <b>{html.escape(name)}</b>!\n\n"
        "Я помогу скачать видео или найти музыку.\n\n"
        "🎬 <b>Видео:</b> YouTube, RuTube, TikTok и Instagram\n"
        "🎵 <b>Музыка:</b> ссылка YouTube/TikTok, название песни или MP4-файл\n\n"
        "Выберите действие:"
    )
    await send_greeting_with_retry(message, greeting)


@router.message(F.sticker)
async def register_work_sticker(message: Message) -> None:
    if not allowed(message) or not message.sticker:
        return
    file_id = message.sticker.file_id
    if file_id not in work_stickers:
        work_stickers.append(file_id)
        save_work_stickers()
    await message.answer(
        "✅ Стикер сохранён. Теперь он будет показываться во время загрузки и поиска."
    )


@router.callback_query(F.data == "download_video")
async def choose_video(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.from_user:
        user_modes[callback.from_user.id] = "video"
    if callback.message:
        await callback.message.answer(
            "🎬 Отправьте ссылку на YouTube, RuTube, TikTok или Instagram.",
            reply_markup=back_keyboard,
        )


@router.callback_query(F.data == "find_music")
async def choose_music(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.from_user:
        user_modes[callback.from_user.id] = "music"
    if callback.message:
        await callback.message.answer(
            "🎵 Отправьте:\n"
            "• ссылку на YouTube или TikTok;\n"
            "• название песни/имя исполнителя;\n"
            "• видео в формате MP4.",
            reply_markup=back_keyboard,
        )


@router.callback_query(F.data == "main_menu")
async def show_main_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    user_modes.pop(callback.from_user.id, None)
    pending_video_urls.pop(callback.from_user.id, None)
    if callback.message:
        await callback.message.answer("Выберите действие:", reply_markup=main_menu)


@router.callback_query(F.data.startswith("music_pick:"))
async def choose_search_result(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        parts = (callback.data or "").split(":")
        if len(parts) == 3:
            results = music_searches.get(parts[1], [])
            index = int(parts[2])
        else:
            results = music_search_results.get(callback.from_user.id, [])
            index = int(parts[1])
            if not results and callback.message:
                result_text = callback.message.text or callback.message.caption or ""
                line_match = re.search(
                    rf"(?m)^{index + 1}\.\s*(.+?)(?:\s+\d+:\d{{2}})?$", result_text
                )
                if line_match:
                    results = await asyncio.to_thread(search_music, line_match.group(1).strip())
                    index = 0
        item = results[index]
        url = result_url(item)
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.answer("Не удалось восстановить этот результат. Выполните поиск ещё раз.")
        return
    if not callback.message:
        return
    if processing_lock.locked():
        await callback.message.answer("Предыдущий файл ещё обрабатывается. Попробуйте чуть позже.")
        return

    work_sticker = await send_work_sticker(callback.message)
    status = await callback.message.answer("⬇️ Скачиваю выбранную композицию…")
    cancel_event = begin_processing()
    async with processing_lock:
        temp_path = Path(tempfile.mkdtemp(prefix="music_track_"))
        try:
            audio_path, title, performer, duration = await asyncio.to_thread(
                download_selected_track, url, temp_path, cancel_event
            )
            if cancel_event.is_set():
                await status.delete()
                return
            size = audio_path.stat().st_size
            if size > MAX_FILE_SIZE:
                await status.edit_text(
                    f"Аудиофайл слишком большой ({size / 1024 / 1024:.1f} МБ)."
                )
                schedule_delete(status)
                return
            await status.edit_text("📤 Отправляю аудиофайл…")
            track_id = secrets.token_hex(6)
            downloaded_tracks[track_id] = {
                "title": title,
                "artist": performer,
            }
            await callback.message.answer_audio(
                audio=FSInputFile(audio_path, filename=f"{title}.mp3"),
                title=title,
                performer=performer,
                duration=int(duration) if duration else None,
                caption="💜 Музыка найдена",
                reply_markup=lyrics_button(track_id),
            )
            await status.delete()
        except DownloadError:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Selected music download failed")
            await status.edit_text("Не удалось скачать выбранную композицию.")
            schedule_delete(status)
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Selected music processing failed")
            await status.edit_text("Произошла ошибка при обработке композиции.")
            schedule_delete(status)
        finally:
            await delete_message_safely(work_sticker)
            shutil.rmtree(temp_path, ignore_errors=True)


@router.callback_query(F.data.startswith("lyrics:"))
async def request_lyrics(callback: CallbackQuery) -> None:
    await callback.answer()
    track_id = (callback.data or "").split(":", 1)[1]
    track = downloaded_tracks.get(track_id)
    if not callback.message or not track:
        if callback.message:
            await callback.message.answer("Данные композиции устарели. Выполните поиск ещё раз.")
        return
    status = await callback.message.answer("📝 Ищу текст песни…")
    try:
        lyrics = await fetch_lyrics(track["title"], track.get("artist"))
        if not lyrics:
            await status.edit_text(
                "Текст этой песни пока не найден в LRCLIB. Проверьте название и исполнителя или попробуйте другой вариант композиции.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="find_music")]]
                ),
            )
            return
        lyrics_cache[track_id] = lyrics
        await status.delete()
        if lyrics["language"] == "ru":
            lyrics_messages[track_id] = await send_long_text(
                callback.message,
                f"📝 {track['artist'] or ''} — {track['title']}",
                f"{lyrics['original']}\n\n{lyrics['copyright']}",
            )
            return
        lyrics_messages[track_id] = await send_long_text(
            callback.message,
            f"🇬🇧 {track['artist'] or ''} — {track['title']}",
            f"{lyrics['original']}\n\n{lyrics['copyright']}",
            reply_markup=lyrics_switch_keyboard(track_id),
        )
    except Exception:
        logging.exception("Lyrics lookup failed")
        await status.edit_text("Не удалось получить текст песни. Попробуйте позже.")
        schedule_delete(status, 12)


@router.callback_query(F.data.startswith("lyrics_original:"))
async def show_original_lyrics(callback: CallbackQuery) -> None:
    await callback.answer()
    track_id = (callback.data or "").split(":", 1)[1]
    lyrics = lyrics_cache.get(track_id)
    track = downloaded_tracks.get(track_id)
    if not callback.message or not lyrics or not track:
        return
    await delete_lyrics_messages(track_id, callback.message)
    lyrics_messages[track_id] = await send_long_text(
        callback.message,
        f"🇬🇧 {track.get('artist') or ''} — {track['title']}",
        f"{lyrics['original']}\n\n{lyrics['copyright']}",
        reply_markup=lyrics_switch_keyboard(track_id),
    )


@router.callback_query(F.data.startswith("lyrics_translation:"))
async def show_translated_lyrics(callback: CallbackQuery) -> None:
    await callback.answer()
    track_id = (callback.data or "").split(":", 1)[1]
    lyrics = lyrics_cache.get(track_id)
    track = downloaded_tracks.get(track_id)
    if not callback.message or not lyrics or not track:
        return
    status = await callback.message.answer("🇷🇺 Перевожу текст…")
    try:
        if not lyrics.get("translation"):
            lyrics["translation"] = await translate_to_russian(lyrics["original"])
        await status.delete()
        await delete_lyrics_messages(track_id, callback.message)
        lyrics_messages[track_id] = await send_bilingual_lyrics(
            callback.message,
            f"🇬🇧 🇷🇺 {track.get('artist') or ''} — {track['title']}",
            lyrics["original"],
            lyrics["translation"],
            reply_markup=lyrics_switch_keyboard(track_id),
        )
    except Exception:
        logging.exception("Lyrics translation failed")
        await status.edit_text(
            "Не удалось выполнить перевод через Argos Translate. "
            "Запустите setup.ps1, чтобы установить модель en-ru."
        )
        schedule_delete(status, 12)


async def recognize_and_reply(
    message: Message,
    audio_path: Path,
    status: Message,
    cancel_event: threading.Event,
) -> None:
    from shazamio import Shazam

    await status.edit_text("🎧 Распознаю композицию…")
    result = await Shazam(language="ru-RU", endpoint_country="RU").recognize(str(audio_path))
    if cancel_event.is_set():
        await status.delete()
        return
    text, cover = format_recognition(result)
    track = result.get("track") or {}
    if not track:
        if cover:
            await message.answer_photo(cover, caption=text, parse_mode="HTML")
            await status.delete()
        else:
            await status.edit_text(text, parse_mode="HTML")
        return

    title = track.get("title") or ""
    artist = track.get("subtitle") or ""
    await status.edit_text("✅ Композиция найдена. Ищу варианты для скачивания…")
    try:
        entries = await asyncio.to_thread(search_music, f"{artist} {title}".strip())
        if cancel_event.is_set():
            await status.delete()
            return
        if not entries:
            if cover:
                await message.answer_photo(cover, caption=text, parse_mode="HTML")
                await status.delete()
            else:
                await status.edit_text(text, parse_mode="HTML")
            return
        if message.from_user:
            music_search_results[message.from_user.id] = entries
        caption = text + "\n" + format_compact_variants(entries)
        if cover:
            await message.answer_photo(
                cover,
                caption=caption,
                parse_mode="HTML",
                reply_markup=music_results_keyboard(entries),
            )
            await status.delete()
        else:
            await status.edit_text(
                caption,
                parse_mode="HTML",
                reply_markup=music_results_keyboard(entries),
            )
    except Exception:
        logging.exception("Recognized track variants search failed")
        if cover:
            await message.answer_photo(cover, caption=text, parse_mode="HTML")
            await status.delete()
        else:
            await status.edit_text(text, parse_mode="HTML")


async def process_music_link(message: Message, url: str) -> None:
    if not is_url_for(url, MUSIC_LINK_DOMAINS):
        await message.answer("Для поиска музыки пришлите ссылку на YouTube или TikTok.")
        return
    if processing_lock.locked():
        await message.answer("Предыдущий файл ещё обрабатывается. Попробуйте чуть позже.")
        return
    work_sticker = await send_work_sticker(message)
    status = await message.answer("⬇️ Загружаю аудиодорожку…")
    cancel_event = begin_processing()
    async with processing_lock:
        temp_path = Path(tempfile.mkdtemp(prefix="music_bot_"))
        try:
            audio_path = await asyncio.to_thread(download_audio, url, temp_path, cancel_event)
            if cancel_event.is_set():
                await status.delete()
                return
            await recognize_and_reply(message, audio_path, status, cancel_event)
        except (ValueError, DownloadError) as error:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Music link processing failed")
            await status.edit_text(str(error) if isinstance(error, ValueError) else "Не удалось загрузить аудио по этой ссылке.")
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Music recognition failed")
            await status.edit_text("Не удалось распознать музыку. Попробуйте другую ссылку или фрагмент.")
        finally:
            await delete_message_safely(work_sticker)
            shutil.rmtree(temp_path, ignore_errors=True)


async def process_uploaded_mp4(message: Message) -> None:
    media = message.video or message.document
    if not media:
        return
    mime_type = getattr(media, "mime_type", None)
    file_name = (getattr(media, "file_name", None) or "").lower()
    if mime_type != "video/mp4" and not file_name.endswith(".mp4"):
        await message.answer("Поддерживается видео только в формате MP4.")
        return
    if processing_lock.locked():
        await message.answer("Предыдущий файл ещё обрабатывается. Попробуйте чуть позже.")
        return
    work_sticker = await send_work_sticker(message)
    status = await message.answer("⬇️ Загружаю MP4…")
    cancel_event = begin_processing()
    async with processing_lock:
        temp_path = Path(tempfile.mkdtemp(prefix="music_upload_"))
        try:
            video_path = temp_path / "input.mp4"
            await message.bot.download(media, destination=video_path, timeout=120)
            await status.edit_text("🎧 Извлекаю аудиодорожку…")
            audio_path = await asyncio.to_thread(extract_audio, video_path, temp_path)
            if cancel_event.is_set():
                await status.delete()
                return
            await recognize_and_reply(message, audio_path, status, cancel_event)
        except ValueError as error:
            await status.edit_text(str(error))
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Uploaded MP4 recognition failed")
            await status.edit_text("Не удалось обработать MP4. Проверьте, что в видео есть слышимая музыка.")
        finally:
            await delete_message_safely(work_sticker)
            shutil.rmtree(temp_path, ignore_errors=True)


async def process_video_download(
    message: Message,
    url: str,
    height: int,
) -> None:
    if processing_lock.locked():
        await message.answer("Предыдущий файл ещё обрабатывается. Нажмите /start для сброса.")
        return
    work_sticker = await send_work_sticker(message)
    status = await message.answer(f"⬇️ Скачиваю видео до {height}p…")
    cancel_event = begin_processing()
    async with processing_lock:
        temp_path = Path(tempfile.mkdtemp(prefix="video_bot_"))
        try:
            video_path, title, duration = await asyncio.to_thread(
                download_video, url, temp_path, cancel_event, height
            )
            if cancel_event.is_set():
                await status.delete()
                return
            size = video_path.stat().st_size
            if size > MAX_FILE_SIZE:
                await status.edit_text(
                    f"Файл получился слишком большим ({size / 1024 / 1024:.1f} МБ). "
                    f"Максимум — {MAX_FILE_SIZE / 1024 / 1024:.0f} МБ. "
                    "Выберите более низкое качество.",
                    reply_markup=video_quality_keyboard,
                )
                return
            await status.edit_text("📤 Отправляю видео…")
            media = FSInputFile(video_path, filename=f"{title}.mp4")
            await message.answer_video(
                video=media,
                caption=title,
                duration=int(duration) if duration else None,
                supports_streaming=True,
            )
            await status.delete()
        except ValueError as error:
            await status.edit_text(str(error), reply_markup=video_quality_keyboard)
        except DownloadError:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Video download failed")
            await status.edit_text(
                "Не удалось скачать видео в выбранном качестве. "
                "Попробуйте другой вариант.",
                reply_markup=video_quality_keyboard,
            )
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Unexpected video processing error")
            await status.edit_text(
                "Произошла ошибка при обработке видео. Попробуйте другое качество.",
                reply_markup=video_quality_keyboard,
            )
        finally:
            await delete_message_safely(work_sticker)
            shutil.rmtree(temp_path, ignore_errors=True)


async def process_video_audio_download(message: Message, url: str) -> None:
    if processing_lock.locked():
        await message.answer("Предыдущий файл ещё обрабатывается. Нажмите /start для сброса.")
        return
    work_sticker = await send_work_sticker(message)
    status = await message.answer("🎵 Извлекаю аудио в MP3…")
    cancel_event = begin_processing()
    async with processing_lock:
        temp_path = Path(tempfile.mkdtemp(prefix="video_audio_"))
        try:
            audio_path, title, performer, duration = await asyncio.to_thread(
                download_selected_track, url, temp_path, cancel_event
            )
            if cancel_event.is_set():
                await status.delete()
                return
            size = audio_path.stat().st_size
            if size > MAX_FILE_SIZE:
                await status.edit_text(
                    f"Аудиофайл слишком большой ({size / 1024 / 1024:.1f} МБ).",
                    reply_markup=video_quality_keyboard,
                )
                return
            await status.edit_text("📤 Отправляю аудио…")
            await message.answer_audio(
                audio=FSInputFile(audio_path, filename=f"{title}.mp3"),
                title=title,
                performer=performer,
                duration=int(duration) if duration else None,
            )
            await status.delete()
        except DownloadError:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Video audio download failed")
            await status.edit_text(
                "Не удалось извлечь аудио. Попробуйте другое качество видео.",
                reply_markup=video_quality_keyboard,
            )
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Unexpected video audio processing error")
            await status.edit_text(
                "Произошла ошибка при подготовке MP3.",
                reply_markup=video_quality_keyboard,
            )
        finally:
            await delete_message_safely(work_sticker)
            shutil.rmtree(temp_path, ignore_errors=True)


@router.callback_query(F.data.startswith("video_quality:"))
async def choose_video_quality(callback: CallbackQuery) -> None:
    await callback.answer()
    url = pending_video_urls.get(callback.from_user.id)
    if not callback.message or not url:
        if callback.message:
            await callback.message.answer("Ссылка устарела. Отправьте её ещё раз.")
        return
    quality = (callback.data or "").split(":", 1)[1]
    if quality in {"mp3", "mp4"}:
        await process_video_audio_download(callback.message, url)
        return
    else:
        try:
            height = int(quality)
        except ValueError:
            await callback.message.answer("Неизвестное качество.")
            return
    await process_video_download(callback.message, url, height)


@router.message(F.video | F.document)
async def handle_media(message: Message) -> None:
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    if not message.from_user or user_modes.get(message.from_user.id) != "music":
        await message.answer("Сначала нажмите «🎵 Найти музыку», затем отправьте MP4.")
        return
    await process_uploaded_mp4(message)


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    text = normalize_media_url((message.text or "").strip())
    if text == "↩️ Вернуться в главное меню":
        if message.from_user:
            user_modes.pop(message.from_user.id, None)
        await message.answer("Главное меню", reply_markup=ReplyKeyboardRemove())
        await message.answer("Выберите действие:", reply_markup=main_menu)
        return
    mode = user_modes.get(message.from_user.id if message.from_user else 0, "video")
    if mode == "music":
        if text.startswith(("http://", "https://")):
            await process_music_link(message, text)
            return
        work_sticker = await send_work_sticker(message)
        status = await message.answer("🔎 Ищу композиции…")
        cancel_event = begin_processing()
        try:
            entries = await asyncio.to_thread(search_music, text)
            if cancel_event.is_set():
                await status.delete()
                return
            if message.from_user:
                music_search_results[message.from_user.id] = entries
            await status.edit_text(
                format_search_results(entries),
                parse_mode="HTML",
                reply_markup=music_results_keyboard(entries),
            )
        except Exception:
            if cancel_event.is_set():
                await status.delete()
                return
            logging.exception("Text music search failed")
            await status.edit_text("Не удалось выполнить поиск. Попробуйте ещё раз чуть позже.")
        finally:
            await delete_message_safely(work_sticker)
        return

    if not is_url_for(text, SUPPORTED_DOMAINS):
        await message.answer("Пришлите корректную ссылку на YouTube, RuTube, TikTok или Instagram.")
        return
    if message.from_user:
        pending_video_urls[message.from_user.id] = text
    await message.answer(
        "🎞 Выберите качество видео:\n\n"
        "1080p будет доступно, если оно есть у источника.\n"
        "MP3 извлекает из видео только аудиодорожку.",
        reply_markup=video_quality_keyboard,
    )


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "ADD_TOKEN_HERE":
        raise RuntimeError("Добавьте BOT_TOKEN в файл .env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    bot = Bot(BOT_TOKEN)
    await bot.set_my_description(
        description=(
            "💜 Бот для скачивания и поиска любимой музыки.\n\n"
            "Для начала нажми /start"
        )
    )
    await bot.set_my_commands(
        commands=[
            BotCommand(command="start", description="Запуск"),
        ]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
