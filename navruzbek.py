"""
Navruzbek — Telegram Video Downloader Bot
------------------------------------------
YouTube, Instagram, TikTok, Twitter/X va boshqa saytlardan video linkini
yuborsangiz, bot videoni yuklab, sizga qaytarib beradi.

TALAB QILINADIGAN KUTUBXONALAR:
    pip install python-telegram-bot yt-dlp requests

Bundan tashqari tizimda ffmpeg o'rnatilgan bo'lishi kerak.

MUHIT O'ZGARUVCHILARI:
    BOT_TOKEN          - @BotFather bergan token (majburiy)
    YT_COOKIES_FILE     - YouTube uchun cookies.txt yo'li (ixtiyoriy, tavsiya etiladi)
    IG_COOKIES_FILE     - Instagram uchun cookies.txt yo'li (ixtiyoriy, tavsiya etiladi)
    PROXY_URL           - masalan http://user:pass@host:port (ixtiyoriy)

    Cookie fayllarni qanday olish (brauzerdan "Get cookies.txt LOCALLY"
    kengaytmasi bilan, tegishli saytga KIRGAN holda eksport qiling):
        - youtube.com  -> YT_COOKIES_FILE
        - instagram.com -> IG_COOKIES_FILE
    Cookie'siz ham bot ishlaydi, lekin YouTube/Instagram ba'zan
    "login kerak" yoki "bot emasligingizni tasdiqlang" deb rad etadi —
    bunda mos cookie fayl muammoni hal qiladi.

ISHGA TUSHIRISH:
    python navruzbek.py

ESLATMA:
    Faqat yuklab olishga ruxsat etilgan / mualliflik huquqi muammosi
    bo'lmagan videolarni yuklang. Javobgarlik foydalanuvchida.
"""

import glob
import logging
import os
import re
import time
import uuid

import requests
import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============ SOZLAMALAR ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ_BU_YERGA")
DOWNLOAD_DIR = "downloads"

MAX_FILE_SIZE_MB = 50
TARGET_SIZE_MB = 45

RETRY_COUNT = 3
RETRY_DELAY_SEC = 2
UPLOAD_TIMEOUT = 600

# Platformaga mos cookie fayllar — har biri o'z hisobidan tizimga kirgan
# holatda eksport qilingan bo'lishi kerak.
YT_COOKIES_FILE = os.environ.get("YT_COOKIES_FILE") or None
IG_COOKIES_FILE = os.environ.get("IG_COOKIES_FILE") or None

# Ixtiyoriy proxy — cloud IP YouTube/Instagram tomonidan bloklansa foydali.
# Masalan: http://user:pass@host:port
PROXY_URL = os.environ.get("PROXY_URL") or None

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://\S+")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PERMANENT_ERROR_KEYS = (
    "private",
    "login",
    "removed",
    "unavailable",
    "not found",
    "unsupported url",
    "no video formats",
    "sign in to confirm",
    "age-restricted",
    "rate-limit reached",
)


# ============ PLATFORMANI ANIQLASH ============

def detect_platform(url: str) -> str:
    host = url.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    return "other"


def _cookies_for(platform: str) -> str | None:
    if platform == "youtube":
        return YT_COOKIES_FILE
    if platform == "instagram":
        return IG_COOKIES_FILE
    return None


# ============ YORDAMCHI FUNKSIYALAR ============

def _find_downloaded(file_id: str, prefer_ext: str | None = None) -> str:
    """
    Yuklangan faylni papkadan topadi (fayl nomini taxmin qilmasdan) —
    chunki yt-dlp merge/convert qilgandan keyingi haqiqiy kengaytma
    prepare_filename() qaytargan nomdan farq qilishi mumkin.
    """
    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*"))
    files = [f for f in files if not f.endswith((".part", ".ytdl", ".temp"))]

    if not files:
        raise FileNotFoundError("Yuklangan fayl diskda topilmadi.")

    if prefer_ext:
        preferred = [f for f in files if f.lower().endswith(prefer_ext.lower())]
        if preferred:
            return preferred[0]

    return max(files, key=os.path.getsize)


def _cleanup(file_id: str) -> None:
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass


def _build_ydl_opts(out_path: str, platform: str, audio_only: bool = False) -> dict:
    opts = {
        "outtmpl": out_path,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "geo_bypass": True,
        "restrictfilenames": True,
        "writethumbnail": False,
        "http_headers": {"User-Agent": USER_AGENT},
    }

    cookies = _cookies_for(platform)
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies

    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    # YouTube uchun qo'shimcha mijoz — ba'zi bloklarni chetlab o'tishga yordam beradi
    if platform == "youtube":
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}

    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["format"] = (
            f"bv*[height<=720][filesize<{TARGET_SIZE_MB}M]+ba/"
            f"b[height<=720][filesize<{TARGET_SIZE_MB}M]/"
            f"bv*[height<=480]+ba/b[height<=480]/b"
        )
        opts["merge_output_format"] = "mp4"

    return opts


def _is_permanent_error(msg: str) -> bool:
    return any(key in msg for key in PERMANENT_ERROR_KEYS)


def download_video(url: str, out_path: str, file_id: str) -> str:
    platform = detect_platform(url)
    ydl_opts = _build_ydl_opts(out_path, platform, audio_only=False)
    last_error: Exception | None = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            return _find_downloaded(file_id)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            if _is_permanent_error(str(e).lower()):
                break
            logger.warning("Urinish %s/%s muvaffaqiyatsiz: %s", attempt, RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)

    raise last_error if last_error else RuntimeError("Noma'lum yuklash xatosi")


def download_audio(url: str, out_path: str, file_id: str) -> str:
    platform = detect_platform(url)
    ydl_opts = _build_ydl_opts(out_path, platform, audio_only=True)
    last_error: Exception | None = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            return _find_downloaded(file_id, prefer_ext=".mp3")
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            if _is_permanent_error(str(e).lower()):
                break
            logger.warning("Urinish %s/%s muvaffaqiyatsiz: %s", attempt, RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)

    raise last_error if last_error else RuntimeError("Noma'lum yuklash xatosi")


def diagnose_error(e: Exception, platform: str = "other") -> str:
    msg = str(e).lower()

    if "sign in to confirm" in msg or "not a bot" in msg:
        if platform == "youtube" and not YT_COOKIES_FILE:
            return (
                "🤖 YouTube bu server IP'sini bloklamoqda. "
                "Botga YT_COOKIES_FILE (YouTube cookies.txt) qo'shilishi kerak."
            )
        return "🤖 Platforma so'rovni bloklamoqda (bot-tekshiruv). Cookie/proxy yangilanishi kerak."
    if "private" in msg or "login" in msg:
        if platform == "instagram" and not IG_COOKIES_FILE:
            return (
                "🔒 Bu Instagram post login talab qiladi. "
                "Botga IG_COOKIES_FILE (Instagram cookies.txt) qo'shilishi kerak."
            )
        return "🔒 Bu video/post shaxsiy (private) yoki ko'rish uchun login talab qiladi."
    if "age" in msg and "restrict" in msg:
        return "🔞 Video yosh chekloviga ega — cookies faylisiz yuklab bo'lmaydi."
    if "removed" in msg or "not found" in msg or "unavailable" in msg:
        return "🚫 Video o'chirilgan yoki mavjud emas."
    if "unsupported url" in msg or "no extractor" in msg:
        return "❔ Bu havola turi hali qo'llab-quvvatlanmaydi."
    if "no video formats" in msg:
        return "📹 Bu havolada yuklab olinadigan video formati topilmadi."
    if "429" in msg or "too many requests" in msg or "rate-limit" in msg:
        return "⏱ Platforma juda ko'p so'rov sababli vaqtincha bloklamoqda. Birozdan so'ng urinib ko'ring."
    if "timeout" in msg or "timed out" in msg:
        return "🌐 Ulanish vaqti tugadi. Qayta urinib ko'ring."
    if "geo" in msg or "country" in msg:
        return "🌍 Bu video mintaqangizda cheklangan bo'lishi mumkin."
    if "token" in msg or "expired" in msg or "signature" in msg:
        return "⏳ Havoladagi vaqtinchalik token eskirgan. Havolani qaytadan nusxalab yuboring."
    if isinstance(e, FileNotFoundError):
        return "⚠️ Video yuklandi, lekin fayl saqlanmadi. Qayta urinib ko'ring."

    return "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki platforma cheklagan bo'lishi mumkin."


def download_direct_file(url: str, out_dir: str, file_id: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    with requests.get(url, headers=headers, stream=True, timeout=30, proxies=proxies) as resp:
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        is_video_ct = "video" in content_type or "octet-stream" in content_type
        ext_match = re.search(r"\.(mp4|mov|mkv|webm|avi|m4v)(\?|$)", url, re.IGNORECASE)

        if not (is_video_ct or ext_match):
            raise ValueError(
                f"Havola video fayl emas ko'rinadi (Content-Type: {content_type or 'nomaʼlum'})."
            )

        ext = ext_match.group(1).lower() if ext_match else "mp4"
        filepath = os.path.join(out_dir, f"{file_id}.{ext}")

        written = 0
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    raise ValueError(f"Fayl hajmi {MAX_FILE_SIZE_MB}MB dan katta.")
                f.write(chunk)

        return filepath


# ============ HANDLERLAR ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! 👋 Men Navruzbek — video yuklovchi bot.\n\n"
        "Menga YouTube, Instagram, TikTok, Twitter/X havolasini yuboring, "
        "men uni yuklab shu yerga tashlab beraman.\n\n"
        "⚠️ Faqat yuklab olishga ruxsat etilgan/mualliflik huquqi "
        "muammosi bo'lmagan videolarni yuboring."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Foydalanish:\n"
        "1. Video havolasini yuboring — men videoni yuklab beraman\n"
        "2. /audio <havola> — faqat musiqa/audio (mp3) kerak bo'lsa\n\n"
        f"Eslatma: fayl hajmi {MAX_FILE_SIZE_MB}MB dan katta bo'lsa yuborilmaydi, "
        "video 720p gacha siqiladi."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Iltimos, to'g'ri video havolasini yuboring.")
        return

    url = match.group(0)
    platform = detect_platform(url)
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text("⏳ Video yuklanmoqda, kuting...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    filepath = None

    try:
        try:
            filepath = download_video(url, out_template, file_id)
        except yt_dlp.utils.DownloadError as yt_err:
            logger.info("yt-dlp muvaffaqiyatsiz (%s), to'g'ridan-to'g'ri sinov...", yt_err)
            _cleanup(file_id)
            await status_msg.edit_text("⏳ Muqobil usul bilan yuklanmoqda...")
            try:
                filepath = download_direct_file(url, DOWNLOAD_DIR, file_id)
            except (requests.RequestException, ValueError):
                raise yt_err

        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ Fayl hajmi juda katta ({file_size_mb:.1f}MB). "
                f"Telegram orqali yuborib bo'lmaydi (limit {MAX_FILE_SIZE_MB}MB)."
            )
            return

        await status_msg.edit_text(f"📤 Yuborilmoqda... ({file_size_mb:.1f}MB)")
        with open(filepath, "rb") as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                supports_streaming=True,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=60,
                pool_timeout=UPLOAD_TIMEOUT,
            )
        await status_msg.delete()

    except (
        yt_dlp.utils.DownloadError,
        requests.RequestException,
        ValueError,
        FileNotFoundError,
    ) as e:
        logger.error("Download error [%s]: %s", platform, e)
        await status_msg.edit_text(diagnose_error(e, platform))

    except TelegramError:
        logger.exception("Telegram error")
        await status_msg.edit_text(
            "📮 Faylni Telegramga yuborib bo'lmadi (hajmi katta yoki ulanish uzildi). Qayta urinib ko'ring."
        )

    except Exception:
        logger.exception("Unexpected error")
        await status_msg.edit_text("❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")

    finally:
        _cleanup(file_id)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Iltimos, quyidagicha yozing:\n/audio https://www.youtube.com/watch?v=..."
        )
        return

    url = match.group(0)
    platform = detect_platform(url)
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text("⏳ Audio ajratilmoqda, kuting...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    filepath = None

    try:
        filepath = download_audio(url, out_template, file_id)

        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(f"❌ Fayl hajmi juda katta ({file_size_mb:.1f}MB).")
            return

        await status_msg.edit_text("📤 Yuborilmoqda...")
        with open(filepath, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=60,
                pool_timeout=UPLOAD_TIMEOUT,
            )
        await status_msg.delete()

    except (
        yt_dlp.utils.DownloadError,
        requests.RequestException,
        ValueError,
        FileNotFoundError,
    ) as e:
        logger.error("Audio download error [%s]: %s", platform, e)
        await status_msg.edit_text(diagnose_error(e, platform))

    except TelegramError:
        logger.exception("Telegram error")
        await status_msg.edit_text("📮 Faylni Telegramga yuborib bo'lmadi. Qayta urinib ko'ring.")

    except Exception:
        logger.exception("Unexpected error")
        await status_msg.edit_text("❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")

    finally:
        _cleanup(file_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Handler xatosi: %s", context.error)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "SIZNING_BOT_TOKENINGIZ_BU_YERGA":
        print("❗ Avval BOT_TOKEN muhit o'zgaruvchisiga tokeningizni kiriting.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(UPLOAD_TIMEOUT)
        .connect_timeout(30)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🤖 Navruzbek ishga tushdi...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
