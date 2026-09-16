"""
Telegram Video Downloader Bot
------------------------------
YouTube, Instagram, TikTok, Twitter/X va boshqa ko'plab saytlardan
video linkini yuborsangiz, bot videoni yuklab, sizga qaytarib beradi.

TALAB QILINADIGAN KUTUBXONALAR:
    pip install python-telegram-bot yt-dlp requests

Bundan tashqari, tizimingizda ffmpeg o'rnatilgan bo'lishi kerak
(video/audio formatlarni birlashtirish uchun):
    - Windows: https://ffmpeg.org/download.html dan yuklab, PATH ga qo'shing
    - Linux:   sudo apt install ffmpeg
    - Mac:     brew install ffmpeg

ISHGA TUSHIRISH:
    1. @BotFather orqali Telegram bot yarating va tokenni oling
    2. Quyidagi BOT_TOKEN o'zgaruvchisiga tokenni qo'ying
    3. python video_downloader_bot.py

ESLATMA (MUHIM):
    Faqat mualliflik huquqiga ega bo'lmagan yoki yuklab olishga ruxsat
    berilgan videolarni yuklang. Ko'pgina platformalarning foydalanish
    shartlari ruxsatsiz yuklab olishni taqiqlaydi — botdan foydalanish
    va uning oqibatlari uchun javobgarlik foydalanuvchining o'zida bo'ladi.
"""

import logging
import os
import re
import time
import uuid

import requests
import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============ SOZLAMALAR ============
# Railway'da: Variables bo'limiga BOT_TOKEN nomi bilan tokeningizni qo'shing.
# Kompyuteringizda lokal ishga tushirsangiz ham, muhit o'zgaruvchisi orqali
# yoki quyidagi "yoki" qismidagi qatorga to'g'ridan-to'g'ri yozib qo'yishingiz mumkin.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ_BU_YERGA")
DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE_MB = 50  # Telegram bot API cheklovi (oddiy bot uchun ~50MB)
RETRY_COUNT = 3        # Muvaffaqiyatsiz bo'lsa necha marta qayta urinish
RETRY_DELAY_SEC = 2    # Urinishlar orasidagi kutish vaqti

# Instagram/Facebook kabi login talab qiladigan saytlar uchun ixtiyoriy
# cookies fayli (brauzerdan "Get cookies.txt" kengaytmasi bilan eksport
# qilinadi). Bo'lmasa None qoldiring — bot baribir ishlaydi, lekin ba'zi
# shaxsiy/himoyalangan postlarni ochib bo'lmasligi mumkin.
COOKIES_FILE = None  # masalan: "instagram_cookies.txt"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://\S+")


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
        "Telegram orqali yuborib bo'lmasligi mumkin."
    )


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
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
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
        opts["format"] = "best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    return opts


def download_video(url: str, out_path: str) -> str:
    """yt-dlp yordamida videoni yuklaydi. Vaqtinchalik xatolarda qayta urinadi."""
    ydl_opts = _build_ydl_opts(out_path, audio_only=False)
    last_error = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            msg = str(e).lower()
            # Login/shaxsiy/o'chirilgan kabi doimiy xatolarda qayta urinishning
            # foydasi yo'q — darhol chiqamiz.
            if any(
                key in msg
                for key in ["private", "login", "removed", "unavailable", "not found"]
            ):
                break
            logger.warning(f"Urinish {attempt}/{RETRY_COUNT} muvaffaqiyatsiz: {e}")
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)

    raise last_error


def download_audio(url: str, out_path: str) -> str:
    """yt-dlp yordamida faqat audio (mp3) yuklaydi."""
    ydl_opts = _build_ydl_opts(out_path, audio_only=True)
    last_error = None

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                return base + ".mp3"
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            msg = str(e).lower()
            if any(
                key in msg
                for key in ["private", "login", "removed", "unavailable", "not found"]
            ):
                break
            logger.warning(f"Urinish {attempt}/{RETRY_COUNT} muvaffaqiyatsiz: {e}")
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)

    raise last_error


def diagnose_error(e: Exception) -> str:
    """Xato matnidan foydalanuvchiga tushunarli sabab chiqaradi."""
    msg = str(e).lower()

    if "private" in msg or "login" in msg:
        return "🔒 Bu video/post shaxsiy (private) yoki ko'rish uchun login talab qiladi."
    if "removed" in msg or "not found" in msg or "unavailable" in msg:
        return "🚫 Video o'chirilgan yoki mavjud emas."
    if "unsupported url" in msg or "no extractor" in msg:
        return "❔ Bu havola turi hali qo'llab-quvvatlanmaydi."
    if "429" in msg or "rate" in msg or "too many requests" in msg:
        return "⏱ Platforma vaqtincha juda ko'p so'rov sababli bloklamoqda. Birozdan so'ng qayta urinib ko'ring."
    if "timeout" in msg or "timed out" in msg:
        return "🌐 Ulanish vaqti tugadi. Internet aloqasini tekshirib, qayta urinib ko'ring."
    if "geo" in msg or "country" in msg:
        return "🌍 Bu video sizning mintaqangizda cheklangan bo'lishi mumkin."
    if "token" in msg or "expired" in msg or "signature" in msg:
        return "⏳ Havoladagi vaqtinchalik token eskirgan. Postni ochib, havolani qaytadan nusxalab yuboring."

    return "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki platforma cheklagan bo'lishi mumkin."


def download_direct_file(url: str, out_dir: str, file_id: str) -> str:
    """
    yt-dlp qo'llab-quvvatlamaydigan havolalar uchun zaxira usul:
    havola to'g'ridan-to'g'ri video fayl (.mp4, .mov, .mkv, .webm ...) ga
    ishora qilsa yoki Content-Type video bo'lsa, oddiy HTTP orqali yuklaydi.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }

    with requests.get(url, headers=headers, stream=True, timeout=30) as resp:
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        is_video_ct = "video" in content_type or "octet-stream" in content_type
        is_video_ext = bool(
            re.search(r"\.(mp4|mov|mkv|webm|avi|m4v)(\?|$)", url, re.IGNORECASE)
        )

        if not (is_video_ct or is_video_ext):
            raise ValueError(
                f"Havola video fayl emas ko'rinadi (Content-Type: {content_type or 'nomaʼlum'})."
            )

        # Kengaytmani aniqlash
        ext_match = re.search(r"\.(mp4|mov|mkv|webm|avi|m4v)(\?|$)", url, re.IGNORECASE)
        ext = ext_match.group(1).lower() if ext_match else "mp4"

        filepath = os.path.join(out_dir, f"{file_id}.{ext}")
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        return filepath


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Iltimos, to'g'ri video havolasini yuboring."
        )
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
            filepath = download_video(url, out_template)
        except yt_dlp.utils.DownloadError:
            # 2-urinish: yt-dlp tanimagan sayt bo'lsa, to'g'ridan-to'g'ri
            # video fayl sifatida yuklab ko'rish
            logger.info("yt-dlp muvaffaqiyatsiz, to'g'ridan-to'g'ri yuklash sinovi...")
            await status_msg.edit_text("⏳ Muqobil usul bilan yuklanmoqda...")
            filepath = download_direct_file(url, DOWNLOAD_DIR, file_id)

        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ Fayl hajmi juda katta ({file_size_mb:.1f}MB). "
                f"Telegram orqali yuborib bo'lmaydi (limit {MAX_FILE_SIZE_MB}MB)."
            )
            os.remove(filepath)
            return

        await status_msg.edit_text("📤 Yuborilmoqda...")
        with open(filepath, "rb") as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                supports_streaming=True,
            )
        await status_msg.delete()

    except (yt_dlp.utils.DownloadError, requests.RequestException, ValueError) as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(diagnose_error(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status_msg.edit_text("❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
    finally:
        # Vaqtinchalik faylni tozalash
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/audio <havola> — videodan faqat audio (mp3) ajratib beradi."""
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Iltimos, quyidagicha yozing:\n"
            "/audio https://www.youtube.com/watch?v=..."
        )
        return

    url = match.group(0)
    chat_id = update.effective_chat.id

    status_msg = await update.message.reply_text("⏳ Audio ajratilmoqda, kuting...")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_AUDIO)

    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    filepath = None
    try:
        filepath = download_audio(url, out_template)

        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ Fayl hajmi juda katta ({file_size_mb:.1f}MB)."
            )
            return

        await status_msg.edit_text("📤 Yuborilmoqda...")
        with open(filepath, "rb") as audio_file:
            await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
        await status_msg.delete()

    except (yt_dlp.utils.DownloadError, requests.RequestException, ValueError) as e:
        logger.error(f"Audio download error: {e}")
        await status_msg.edit_text(diagnose_error(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status_msg.edit_text("❌ Kutilmagan xatolik yuz berdi. Keyinroq qayta urinib ko'ring.")
    finally:
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


def main():
    if BOT_TOKEN == "SIZNING_BOT_TOKENINGIZ_BU_YERGA":
        print(
            "❗ Iltimos, avval BOT_TOKEN o'zgaruvchisiga haqiqiy tokeningizni kiriting."
        )
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
