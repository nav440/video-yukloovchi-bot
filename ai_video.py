"""
AI Video — Telegram Video Downloader Bot
----------------------------------------
YouTube, Instagram, TikTok, Twitter/X va boshqa saytlardan video linkini
yuborsangiz, bot videoni yuklab, sizga qaytarib beradi.

TALAB QILINADIGAN KUTUBXONALAR:
    pip install python-telegram-bot yt-dlp requests

Bundan tashqari tizimda ffmpeg o'rnatilgan bo'lishi kerak.

ISHGA TUSHIRISH:
    BOT_TOKEN muhit o'zgaruvchisiga @BotFather bergan tokenni qo'ying
    python ai_video.py

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

# Telegram oddiy Bot API cheklovi ~50MB. yt-dlp'ga biroz pastroq chegara
# beramiz, chunki konteyner/birlashtirishdan keyin hajm ozgina oshishi mumkin.
MAX_FILE_SIZE_MB = 50
TARGET_SIZE_MB = 45

RETRY_COUNT = 3
RETRY_DELAY_SEC = 2

# Telegramga yuborish uchun timeout (katta fayllar sekin ketadi)
UPLOAD_TIMEOUT = 600

# Instagram/Facebook kabi login talab qiladigan saytlar uchun ixtiyoriy
# cookies fayli. YouTube "bot emasligingizni tasdiqlang" desa ham shu yordam
# beradi. Bo'lmasa None qoldiring.
COOKIES_FILE = os.environ.get("COOKIES_FILE") or None

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

# Qayta urinishdan foyda yo'q bo'lgan doimiy xatolar
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
)


# ============ YORDAMCHI FUNKSIYALAR ============

def _find_downloaded(file_id: str, prefer_ext: str | None = None) -> str:
    """
    Yuklangan faylni papkadan topadi.

    ydl.prepare_filename() birlashtirish/konvertatsiyadan OLDINGI kengaytmani
    qaytaradi (masalan .webm), diskda esa .mp4 yotishi mumkin — shuning uchun
    fayl nomini taxmin qilmasdan, haqiqiy faylni qidiramiz.
    """
    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*"))
    files = [f for f in files if not f.endswith((".part", ".ytdl", ".temp"))]

    if not files:
        raise FileNotFoundError(
            "Yuklangan fayl diskda topilmadi (yt-dlp fayl yaratmadi)."
        )

    if prefer_ext:
        preferred = [f for f in files if f.lower().endswith(prefer_ext.lower())]
        if preferred:
            return preferred[0]

    # Eng kattasi — asosiy media fayl (qolganlari .jpg, .json kabi qo'shimchalar)
    return max(files, key=os.path.getsize)


def _cleanup(file_id: str) -> None:
    """file_id bilan boshlanadigan barcha vaqtinchalik fayllarni o'chiradi."""
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*")):
        try:
            os.remove(f)
        except OSError:
            pass


def _build_ydl_opts(out_path: str, audio_only: bool = False) -> dict:
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

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

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
        # Hajmni boshidanoq cheklaymiz — aks holda bot 500MB faylni yuklab,
        # keyin "juda katta" deb rad etadi va vaqt behuda ketadi.
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
    """yt-dlp yordamida videoni yuklaydi. Vaqtinchalik xatolarda qayta urinadi."""
    ydl_opts = _build_ydl_opts(out_path, audio_only=False)
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
    """yt-dlp yordamida faqat audio (mp3) yuklaydi."""
    ydl_opts = _build_ydl_opts(out_path, audio_only=True)
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


def diagnose_error(e: Exception) -> str:
    """Xato matnidan foydalanuvchiga tushunarli sabab chiqaradi."""
    msg = str(e).lower()

    if "sign in to confirm" in msg or "not a bot" in msg:
        return (
            "🤖 YouTube server IP manzilini bloklamoqda "
            "(bot emasligini tasdiqlashni so'rayapti). Cookies fayli kerak."
        )
    if "private" in msg or "login" in msg:
        return "🔒 Bu video/post shaxsiy (private) yoki ko'rish uchun login talab qiladi."
    if "age" in msg and "restrict" in msg:
        return "🔞 Video yosh chekloviga ega — cookies faylisiz yuklab bo'lmaydi."
    if "removed" in msg or "not found" in msg or "unavailable" in msg:
        return "🚫 Video o'chirilgan yoki mavjud emas."
    if "unsupported url" in msg or "no extractor" in msg:
        return "❔ Bu havola turi hali qo'llab-quvvatlanmaydi."
    if "no video formats" in msg:
        return "📹 Bu havolada yuklab olinadigan video formati topilmadi."
    if "429" in msg or "too many requests" in msg:
        return "⏱ Platforma juda ko'p so'rov sababli vaqtincha bloklamoqda. Birozdan so'ng urinib ko'ring."
    if "timeout" in msg or "timed out" in msg:
        return "🌐 Ulanish vaqti tugadi. Qayta urinib ko'ring."
    if "geo" in msg or "country" in msg:
        return "🌍 Bu video mintaqangizda cheklangan bo'lishi mumkin."
    if "token" in msg or "expired" in msg or "signature" in msg:
        return "⏳ Havoladagi vaqtinchalik token eskirgan. Havolani qaytadan nusxalab yuboring."
    if "topilmadi" in msg or isinstance(e, FileNotFoundError):
        return "⚠️ Video yuklandi, lekin fayl saqlanmadi. Qayta urinib ko'ring."

    return "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki platforma cheklagan bo'lishi mumkin."


def download_direct_file(url: str, out_dir: str, file_id: str) -> str:
    """
    yt-dlp qo'llab-quvvatlamaydigan havolalar uchun zaxira usul:
    havola to'g'ridan-to'g'ri video faylga ishora qilsa, HTTP orqali yuklaydi.
    """
    headers = {"User-Agent": USER_AGENT}
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    with requests.get(url, headers=headers, stream=True, timeout=30) as resp:
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
                    raise ValueError(
                        f"Fayl hajmi {MAX_FILE_SIZE_MB}MB dan katta — yuborib bo'lmaydi."
                    )
                f.write(chunk)

        return filepath


# ============ HANDLERLAR ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! 👋\n\n"
        "Menga video havolasini yuboring (YouTube, Instagram, TikTok, "
        "Twitter/X va h.k.), men uni yuklab, shu yerga tashlab beraman.\n\n"
        "⚠️ Faqat yuklab olishga ruxsat etilgan/mualliflik huquqi "
        "muammosi bo'lmagan videolarni yuboring."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Foydalanish:\n"
        "1. Video havolasini yuboring — men videoni yuklab beraman\n"
        "2. /audio <havola> — faqat musiqa/audio (mp3) kerak bo'lsa\n\n"
        f"Eslatma: fayl hajmi {MAX_FILE_SIZE_MB}MB dan katta bo'lsa, "
        "Telegram orqali yuborib bo'lmaydi. Video 720p gacha siqiladi."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Iltimos, to'g'ri video havolasini yuboring.")
        return

    url = match.group(0)
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text("⏳ Video yuklanmoqda, kuting...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    filepath = None

    try:
        try:
            # 1-urinish: yt-dlp (YouTube, Instagram, TikTok va h.k.)
            filepath = download_video(url, out_template, file_id)
        except yt_dlp.utils.DownloadError as yt_err:
            # 2-urinish: yt-dlp tanimagan sayt bo'lsa, to'g'ridan-to'g'ri yuklash
            logger.info("yt-dlp muvaffaqiyatsiz (%s), to'g'ridan-to'g'ri sinov...", yt_err)
            _cleanup(file_id)
            await status_msg.edit_text("⏳ Muqobil usul bilan yuklanmoqda...")
            try:
                filepath = download_direct_file(url, DOWNLOAD_DIR, file_id)
            except (requests.RequestException, ValueError):
                # Muqobil usul ham ishlamadi — asl yt-dlp xatosini ko'rsatamiz,
                # chunki u foydalanuvchiga ko'proq ma'no beradi.
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
        logger.error("Download error: %s", e)
        await status_msg.edit_text(diagnose_error(e))

    except TelegramError as e:
        logger.exception("Telegram error")
        await status_msg.edit_text(
            "📮 Faylni Telegramga yuborib bo'lmadi (hajmi katta yoki ulanish uzildi). "
            "Qayta urinib ko'ring."
        )

    except Exception:
        # To'liq traceback logga tushadi — sabab loglardan ko'rinadi
        logger.exception("Unexpected error")
        await status_msg.edit_text(
            "❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring."
        )

    finally:
        _cleanup(file_id)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/audio <havola> — videodan faqat audio (mp3) ajratib beradi."""
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Iltimos, quyidagicha yozing:\n/audio https://www.youtube.com/watch?v=..."
        )
        return

    url = match.group(0)
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
            await status_msg.edit_text(
                f"❌ Fayl hajmi juda katta ({file_size_mb:.1f}MB)."
            )
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
        logger.error("Audio download error: %s", e)
        await status_msg.edit_text(diagnose_error(e))

    except TelegramError:
        logger.exception("Telegram error")
        await status_msg.edit_text(
            "📮 Faylni Telegramga yuborib bo'lmadi. Qayta urinib ko'ring."
        )

    except Exception:
        logger.exception("Unexpected error")
        await status_msg.edit_text(
            "❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring."
        )

    finally:
        _cleanup(file_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handler ichida ushlanmagan xatolarni logga yozadi (bot to'xtab qolmaydi)."""
    logger.exception("Handler xatosi: %s", context.error)


# ============ ISHGA TUSHIRISH ============

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

    logger.info("🤖 Bot ishga tushdi...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
