#!/usr/bin/env python3
"""
Telegram Video Downloader Bot
- python-telegram-bot 20.7 (async)
- yt-dlp, maksimal 720p
- Kirim sebagai document, lalu hapus file dari server
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------ konfigurasi

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Batas ukuran file yang mau dikirim (Bot API resmi = 50 MB)
MAX_FILESIZE = 49 * 1024 * 1024

# Berapa download yang boleh jalan bersamaan (HP kentang -> kecilkan)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))

# Timeout total per download (detik)
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "900"))

# Opsional: batasi user. Kosongkan = semua orang boleh pakai.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(",", " ").split()
}

URL_RE = re.compile(r"https?://[^\s<>\"']+")
UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
HAS_FFMPEG = shutil.which("ffmpeg") is not None

# Kalau ada ffmpeg -> boleh merge video+audio. Kalau tidak -> pakai file progresif.
if HAS_FFMPEG:
    FORMAT = "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
else:
    FORMAT = "b[height<=720][ext=mp4]/b[height<=720]/b"

SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)
logger = logging.getLogger("downloader")

# ------------------------------------------------------------------ util


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and "." in p.netloc


def human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _download_sync(url: str, workdir: str) -> tuple[str, str]:
    """Fungsi blocking yt-dlp. Dijalankan di thread terpisah."""
    opts = {
        "format": FORMAT,
        "outtmpl": os.path.join(workdir, "%(title).80s-%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "restrictfilenames": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 3,
        "max_filesize": MAX_FILESIZE,
        "http_headers": {"User-Agent": UA},
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError("Tidak ada media yang bisa diunduh dari link itu.")

    title = (info.get("title") or "video").strip()

    # Ambil file hasil unduhan (abaikan .part / .ytdl / subtitle)
    files = [
        p
        for p in Path(workdir).iterdir()
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".vtt", ".srt")
    ]
    if not files:
        raise RuntimeError("Unduhan selesai tapi file tidak ditemukan.")

    biggest = max(files, key=lambda p: p.stat().st_size)
    return str(biggest), title


def safe_filename(title: str, ext: str) -> str:
    name = re.sub(r"[^\w\s.-]", "", title, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name).strip("._-") or "video"
    return f"{name[:80]}{ext}"


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "unsupported url" in low:
        return "Situs itu belum didukung yt-dlp."
    if "private" in low or "login" in low or "cookies" in low:
        return "Video privat / butuh login, jadi tidak bisa diunduh."
    if "requested format is not available" in low:
        return "Tidak ada format di bawah 720p / ukurannya kebesaran."
    if "not available in your country" in low or "geo" in low:
        return "Video dibatasi untuk region lain."
    if "timed out" in low or "timeout" in low:
        return "Koneksi timeout. Coba lagi."
    if "http error 404" in low:
        return "Link tidak ditemukan (404)."
    # potong pesan panjang
    text = text.replace("ERROR: ", "").strip()
    return text[:300] if text else "Gagal mengunduh."


# ------------------------------------------------------------------ handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>📥 Video Downloader Bot</b>\n\n"
        "Kirim link video apa saja, bot akan mengunduhnya (maks <b>720p</b>) "
        "lalu mengirimnya kembali sebagai file dokumen.\n\n"
        "<b>Cara pakai:</b>\n"
        "1. Salin link video dari aplikasi (TikTok, Instagram, Facebook, "
        "YouTube, X, dll).\n"
        "2. Tempel link ke chat ini, kirim.\n"
        "3. Tunggu proses selesai, file akan dikirim.\n\n"
        "<b>Catatan:</b>\n"
        "• Batas ukuran file <b>49 MB</b> (batas Bot API).\n"
        "• Video yang lebih besar dari itu akan ditolak.\n"
        "• File otomatis dihapus dari server setelah terkirim.\n"
        "• Playlist hanya diambil video pertamanya.\n\n"
        "Situs yang didukung: hampir semua yang didukung yt-dlp."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(f"ID kamu: <code>{user.id}</code>",
                                              parse_mode=ParseMode.HTML)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await message.reply_text("Kamu belum diizinkan memakai bot ini.")
        return

    match = URL_RE.search(message.text)
    if not match:
        return

    url = match.group(0).rstrip(".,;)]}>\"'")
    if not is_valid_url(url):
        await message.reply_text("Link tidak valid. Harus diawali http:// atau https://")
        return

    status = await message.reply_text("⏳ Masuk antrean...")

    async with SEMAPHORE:
        workdir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            await status.edit_text("⬇️ Mengunduh (maks 720p)...")

            try:
                filepath, title = await asyncio.wait_for(
                    asyncio.to_thread(_download_sync, url, workdir),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await status.edit_text("⌛ Waktu habis. Video terlalu besar / koneksi lambat.")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Download gagal: %s", url)
                await status.edit_text(f"❌ Gagal: {friendly_error(exc)}")
                return

            path = Path(filepath)
            size = path.stat().st_size

            if size > MAX_FILESIZE:
                await status.edit_text(
                    f"❌ File terlalu besar ({human_size(size)}), batasnya 49 MB.\n"
                    "Coba link dengan kualitas lebih rendah."
                )
                return

            await status.edit_text("📤 Mengirim file...")

            filename = safe_filename(title, path.suffix or ".mp4")
            caption = f"✅ {title}\n📦 {human_size(size)}"

            with path.open("rb") as fh:
                await message.reply_document(
                    document=fh,
                    filename=filename,
                    caption=caption[:1000],
                    read_timeout=120,
                    write_timeout=600,
                    connect_timeout=30,
                )

            await status.delete()

        finally:
            # Selalu bersihkan, walau error / user blokir bot
            shutil.rmtree(workdir, ignore_errors=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Terjadi error internal. Coba kirim ulang linknya."
            )
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ main


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN belum diset.\n"
            'Jalankan: export BOT_TOKEN="123456:ABC..." lalu ulangi.'
        )

    if not HAS_FFMPEG:
        logger.warning(
            "ffmpeg tidak ditemukan -> kualitas bisa lebih rendah. "
            "Install dengan: pkg install ffmpeg"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(600)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(on_error)

    logger.info("Bot jalan. Tekan Ctrl+C untuk berhenti.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
