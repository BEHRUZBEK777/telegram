import os
import re
import uuid
import json
import time
import shutil
import asyncio
import logging
import sqlite3
import urllib.request
from typing import Optional, Dict, Any

import redis.asyncio as aioredis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from yt_dlp import YoutubeDL
from shazamio import Shazam

# ==========================================
# 1. LOGGING VA ASOSIY SOZLAMALAR
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token va Redis manzilini muhit o'zgaruvchilaridan olish
TOKEN = os.getenv("BOT_TOKEN", "8989465930:AAGfYIMR-Sk9PGz0ldDLraeO4_Xq-sCSSqg")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ⚠️ YOPIQ KANAL ID-SI (Audio fayllarni doimiy saqlash uchun)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001788015387"))

# Vaqtinchalik yuklamalar papkasi
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# SQLite Lokal Bazasini Yaratish
DB_PATH = "music_base.db"

def init_db():
    """Lokal SQLite bazasi jadvalini shakllantirish"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channel_music (
                song_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT,
                performer TEXT,
                duration INTEGER,
                channel_msg_id INTEGER
            )
        """)
        conn.commit()
        conn.close()
        logger.info("✅ SQLite bazasi tayyor va ulandi.")
    except Exception as e:
        logger.error(f"❌ DB init error: {e}")

init_db()

def db_get_song(song_id: str) -> Optional[Dict[str, Any]]:
    """Bazada qo'shiq file_id-si bor-yo'qligini tekshirish"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_id, title, performer, duration, channel_msg_id FROM channel_music WHERE song_id = ?",
            (song_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "file_id": row[0],
                "title": row[1],
                "performer": row[2],
                "duration": row[3],
                "channel_msg_id": row[4]
            }
    except Exception as e:
        logger.error(f"DB Read Error: {e}")
    return None

def db_save_song(song_id: str, file_id: str, title: str, performer: str, duration: int, channel_msg_id: int):
    """Yangi qo'shiqni bazaga va keshga saqlash"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO channel_music (song_id, file_id, title, performer, duration, channel_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
            (song_id, file_id, title, performer, duration, channel_msg_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"💾 Qo'shiq bazaga saqlandi: ID={song_id}")
    except Exception as e:
        logger.error(f"DB Save Error: {e}")

# Cheklovlar va Vaqt Parametrlari
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # Telegram Bot API 50MB chegarasi
MAX_CONCURRENT_DOWNLOADS = 15          # Bir vaqtning o'zidagi parallel yuklanishlar
RATE_LIMIT_SECONDS = 2                 # Anti-Spam vaqti (sekund)
SEARCH_CACHE_TTL = 1800                # Qidiruv natijalari kesh davri (30 daqiqa)
MEDIA_CACHE_TTL = 86400 * 7            # Instagram/TikTok kesh davri (7 kun)

# Proksi (zarur bo'lsa kiriting, masalan: "http://user:pass@ip:port")
ROTATING_PROXY = None

HAS_FFMPEG = shutil.which("ffmpeg") is not None
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
redis_client: Optional[aioredis.Redis] = None
local_cache: Dict[str, Any] = {}
local_rate_limit: Dict[int, float] = {}

COOKIES_FILE = os.getenv("COOKIES_PATH", "cookies.txt")
HAS_COOKIES = os.path.exists(COOKIES_FILE)

# YT-DLP Standart Sozlamalari (Anti-Block va Client Spoofing qo'shildi)
YDL_GENERAL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "geo_bypass": True,
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "mweb", "web"],
            "skip": ["hls", "dash"]
        }
    }
}

if HAS_COOKIES:
    YDL_GENERAL_OPTS["cookiefile"] = COOKIES_FILE
    logger.info("🍪 'cookies.txt' fayli topildi va yt-dlp tizimiga ulandi!")

if ROTATING_PROXY:
    YDL_GENERAL_OPTS["proxy"] = ROTATING_PROXY


# ==========================================
# 2. YORDAMCHI FUNKSIYALAR VA KESH TIZIMI
# ==========================================
def seconds_to_min(seconds: int) -> str:
    """Sekundlarni MM:SS formatiga o'tkazish"""
    if not seconds:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

async def check_rate_limit(user_id: int) -> bool:
    """Anti-Spam check: ketma-ket so'rov yuborishni cheklash"""
    now = time.time()
    if redis_client:
        try:
            key = f"rate_limit:{user_id}"
            is_limited = await redis_client.get(key)
            if is_limited:
                return False
            await redis_client.setex(key, RATE_LIMIT_SECONDS, "1")
            return True
        except Exception:
            pass

    last_time = local_rate_limit.get(user_id, 0)
    if now - last_time < RATE_LIMIT_SECONDS:
        return False
    local_rate_limit[user_id] = now
    return True

async def set_user_search(chat_id: int, data: Dict[str, Any]):
    """Qidiruv natijalarini keshga saqlash"""
    if redis_client:
        try:
            await redis_client.setex(f"search:{chat_id}", SEARCH_CACHE_TTL, json.dumps(data))
            return
        except Exception:
            pass
    local_cache[f"search:{chat_id}"] = data

async def get_user_search(chat_id: int) -> Optional[Dict[str, Any]]:
    """Keshdan qidiruv ma'lumotlarini olish"""
    if redis_client:
        try:
            val = await redis_client.get(f"search:{chat_id}")
            if val:
                return json.loads(val)
        except Exception:
            pass
    return local_cache.get(f"search:{chat_id}")

async def fetch_lyrics(song_title: str) -> Optional[str]:
    """ShazamIO orqali qo'shiq matnini (lyrics) topish"""
    shazam = Shazam()
    try:
        search_res = await shazam.search_track(name=song_title, limit=1)
        tracks = search_res.get("tracks", {}).get("hits", [])
        if tracks:
            track_key = tracks[0].get("track", {}).get("key")
            if track_key:
                about = await shazam.track_about(track_id=track_key)
                sections = about.get("sections", [])
                for sec in sections:
                    if sec.get("type") == "LYRICS":
                        return "\n".join(sec.get("text", []))
    except Exception as e:
        logger.error(f"Lyrics fetch error: {e}")
    return None

async def fetch_ig_profile(username: str) -> Optional[Dict[str, Any]]:
    """Instagram profil ma'lumotlarini tahlil qilish"""
    loop = asyncio.get_running_loop()

    def _scrape():
        url = f"https://www.instagram.com/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                html = response.read().decode("utf-8", errors="ignore")

            title_m = re.search(r'<meta\s+(?:property|name)="og:title"\s+content="([^"]+)"', html, re.IGNORECASE)
            desc_m = re.search(r'<meta\s+(?:property|name)="og:description"\s+content="([^"]+)"', html, re.IGNORECASE)
            img_m = re.search(r'<meta\s+(?:property|name)="og:image"\s+content="([^"]+)"', html, re.IGNORECASE)

            full_name = username
            followers, following, posts = "Noma'lum", "Noma'lum", "Noma'lum"

            if title_m:
                raw_title = title_m.group(1)
                full_name = raw_title.split("(@")[0].strip() if "(@" in raw_title else raw_title.split("•")[0].strip()

            if desc_m:
                desc = desc_m.group(1)
                stats_match = re.search(r'([\d\,\.KkMm]+)\s+Followers,\s*([\d\,\.KkMm]+)\s+Following,\s*([\d\,\.KkMm]+)\s+Posts', desc, re.IGNORECASE)
                if stats_match:
                    followers = stats_match.group(1)
                    following = stats_match.group(2)
                    posts = stats_match.group(3)

            avatar_url = img_m.group(1) if img_m else None
            return {
                "username": username,
                "full_name": full_name,
                "followers": followers,
                "following": following,
                "posts": posts,
                "avatar_url": avatar_url,
            }
        except Exception as e:
            logger.error(f"IG profile scrape error: {e}")
            return None

    return await loop.run_in_executor(None, _scrape)

def render_search_page(chat_id: int, data: Dict[str, Any], page: int = 0):
    """Qidiruv natijalari menyusini tayyorlash"""
    query_title = data.get("query", "Musiqa qidiruvi")
    results = data.get("results", [])

    start_idx = page * 10
    end_idx = start_idx + 10
    page_items = results[start_idx:end_idx]

    if not page_items:
        return None, None

    res_text = f"🔍 **Qidiruv:** *{query_title}*\n\n"
    for idx, item in enumerate(page_items, 1):
        dur = seconds_to_min(item.get("duration", 0))
        res_text += f"**{idx}.** {item.get('title')} **[{dur}]**\n"

    row1, row2 = [], []
    for idx in range(1, len(page_items) + 1):
        global_idx = start_idx + (idx - 1)
        btn = InlineKeyboardButton(str(idx), callback_data=f"song_{global_idx}")
        if idx <= 5:
            row1.append(btn)
        else:
            row2.append(btn)

    keyboard = []
    if row1:
        keyboard.append(row1)
    if row2:
        keyboard.append(row2)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Orqaga", callback_data=f"page_{page-1}"))
    if end_idx < len(results):
        nav_row.append(InlineKeyboardButton("Olg'a ➡️", callback_data=f"page_{page+1}"))

    if nav_row:
        keyboard.append(nav_row)

    if len(results) == 1:
        keyboard.append([InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data="lyrics_0")])

    return res_text, InlineKeyboardMarkup(keyboard)


# ==========================================
# 3. HANDLERLAR VA ISHLOV MANTIQI
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start buyrug'i"""
    welcome_text = (
        "👋 **Salom! Men yuqori tezlikdagi multimediya yuklovchi va musiqa qidiruvchi botman!**\n\n"
        "✨ **Mening imkoniyatlarim:**\n\n"
        "🎵 **Musiqa qidirish:** Qo'shiq nomi yoki matnini yuboring.\n"
        "🎬 **Video yuklash:** Instagram, TikTok va YouTube havolasini tashlang.\n"
        "🎧 **Shazam / Ovozli aniqlash:** Ovozli xabar, video yoki musiqali fayl yuboring — topib beraman!\n\n"
        "⚡️ *Istalgan havola yoki nomni yuborib ko'ring!*"
    )
    await update.message.reply_text(welcome_text, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Matnlar, havolalar va qidiruv so'rovlariga ishlov berish"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text

    if not text:
        return

    # Anti-Spam Check
    if not await check_rate_limit(user_id):
        await update.message.reply_text("⚠️ juda ko'p so'rov yubordingiz. Biroz kuting.")
        return

    # 1. Instagram Profil Tekshiruvi
    clean_text = text.strip().split("?")[0].rstrip("/")
    ig_profile_pattern = r"^https?://(?:www\.)?instagram\.com/([a-zA-Z0-9_\.]+)$"
    ig_profile_match = re.match(ig_profile_pattern, clean_text)

    if ig_profile_match:
        username = ig_profile_match.group(1).lower()
        reserved = ["p", "reel", "reels", "stories", "tv", "explore", "direct", "accounts"]

        if username not in reserved:
            msg = await update.message.reply_text(f"🔍 Instagram profili tahlil qilinmoqda: @{username}...")
            profile = await fetch_ig_profile(username)

            if profile:
                caption = (
                    f"👤 **Instagram Profil:**\n\n"
                    f"📌 **Ism:** {profile['full_name']}\n"
                    f"🔗 **Username:** @{username}\n"
                    f"👥 **Obunachilar:** {profile['followers']}\n"
                    f"➡️ **Obunalar:** {profile['following']}\n"
                    f"🖼 **Postlar:** {profile['posts']}\n"
                )
                if profile.get("avatar_url"):
                    try:
                        await update.message.reply_photo(photo=profile["avatar_url"], caption=caption, parse_mode="Markdown")
                    except Exception:
                        await update.message.reply_text(caption, parse_mode="Markdown")
                else:
                    await update.message.reply_text(caption, parse_mode="Markdown")
                await msg.delete()
            else:
                await msg.edit_text(f"ℹ️ Profil: @{username}\n(Profil yopiq yoki ma'lumot olish cheklangan)")
            return

    # 2. URL Havolalariga Ishlov Berish
    url_pattern = r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)"
    urls = re.findall(url_pattern, text)

    if urls:
        url = urls[0]
        url_lower = url.lower()

        # Redis Keshidan Tekshirish
        if redis_client:
            try:
                cached_file_id = await redis_client.get(f"media_cache:{url}")
                if cached_file_id:
                    await update.message.reply_video(video=cached_file_id.decode("utf-8"), caption="⚡️ Tezkor yuborildi!")
                    return
            except Exception:
                pass

        # 2-A. YouTube Video Havolasi
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            msg = await update.message.reply_text("🔎 YouTube videosi tahlil qilinmoqda...")
            loop = asyncio.get_running_loop()

            try:
                def get_yt_info():
                    opts = dict(YDL_GENERAL_OPTS)
                    opts.update({"skip_download": True, "extract_flat": False})
                    with YoutubeDL(opts) as ydl:
                        return ydl.extract_info(url, download=False)

                info = await loop.run_in_executor(None, get_yt_info)
                if not info:
                    await msg.edit_text("❌ YouTube ma'lumotlarini olib bo'lmadi.")
                    return

                title = info.get("title", "YouTube Video")
                video_id = info.get("id")

                keyboard = [
                    [
                        InlineKeyboardButton("🎬 360p", callback_data=f"yt_360p_{video_id}"),
                        InlineKeyboardButton("🎬 480p", callback_data=f"yt_480p_{video_id}"),
                    ],
                    [
                        InlineKeyboardButton("🎬 720p 🌟", callback_data=f"yt_720p_{video_id}"),
                        InlineKeyboardButton("🎬 1080p ⚡️", callback_data=f"yt_1080p_{video_id}"),
                    ],
                    [
                        InlineKeyboardButton("🎵 Faqat Audio (MP3)", callback_data=f"yt_audio_{video_id}"),
                        InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data=f"lyrics_yt_{video_id}"),
                    ],
                ]

                if redis_client:
                    try:
                        await redis_client.setex(f"yt_info:{video_id}", SEARCH_CACHE_TTL, json.dumps({"url": url, "title": title}))
                    except Exception:
                        pass
                local_cache[f"yt_info:{video_id}"] = {"url": url, "title": title}

                caption_text = f"🎬 **{title}**\n\n📌 *Videoni qaysi format/sifatda yuklashni xohlaysiz?*"
                await msg.edit_text(caption_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except Exception as e:
                err_str = str(e)
                if "Sign in to confirm" in err_str or "Bot Verification" in err_str or "429" in err_str:
                    await msg.edit_text("❌ **YouTube IP blokirovkasi!** Server IP-si vaqtincha cheklandi. `cookies.txt` fayli zarur.")
                else:
                    await msg.edit_text(f"❌ YouTube havolasida xatolik: {err_str[:100]}")
            return

        # 2-B. Instagram / TikTok Mediasini Yuklash
        msg = await update.message.reply_text("📥 Media yuklanmoqda...")
        loop = asyncio.get_running_loop()

        async with download_semaphore:
            prefix_id = f"soc_{uuid.uuid4().hex[:8]}"

            def download_social_media():
                opts = dict(YDL_GENERAL_OPTS)
                opts.update({"outtmpl": f"{DOWNLOAD_DIR}/{prefix_id}_%(no)s.%(ext)s"})
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    downloaded_files = [
                        os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) if f.startswith(prefix_id)
                    ]
                    return downloaded_files, info

            try:
                files, info = await loop.run_in_executor(None, download_social_media)
                if files:
                    photos = [f for f in files if f.endswith((".jpg", ".jpeg", ".png", ".webp"))]
                    videos = [f for f in files if f.endswith((".mp4", ".mkv", ".webm", ".mov"))]

                    for vid in videos:
                        if os.path.getsize(vid) > MAX_FILE_SIZE_BYTES:
                            await update.message.reply_text("⚠️ Video hajmi 50MB dan katta bo'lgani uchun yuklab bo'lmadi.")
                            if os.path.exists(vid):
                                os.remove(vid)
                            continue

                        kb = [[InlineKeyboardButton("🔍 🎵 Videodagi musiqani topish", callback_data="shazam_video")]]

                        with open(vid, "rb") as video_file:
                            sent_msg = await update.message.reply_video(
                                video=video_file,
                                caption="✅ Video yuklandi!",
                                reply_markup=InlineKeyboardMarkup(kb),
                            )
                        if sent_msg.video and redis_client:
                            try:
                                await redis_client.setex(f"media_cache:{url}", MEDIA_CACHE_TTL, sent_msg.video.file_id)
                            except Exception:
                                pass

                        if os.path.exists(vid):
                            os.remove(vid)

                    for img in photos:
                        with open(img, "rb") as photo_file:
                            await update.message.reply_photo(photo=photo_file, caption="🖼 Rasm yuklandi!")
                        if os.path.exists(img):
                            os.remove(img)

                    await msg.delete()
                else:
                    await msg.edit_text("❌ Mediani yuklab bo'lmadi.")
            except Exception as e:
                await msg.edit_text(f"❌ Yuklashda xatolik: {str(e)[:100]}")
        return

    # 3. Qo'shiq Nomi Orqali Izlash
    msg = await update.message.reply_text("🎧 Qo'shiqlar qidirilmoqda...")
    loop = asyncio.get_running_loop()

    try:
        def search_music():
            opts = dict(YDL_GENERAL_OPTS)
            opts.update({"extract_flat": True, "skip_download": True})
            with YoutubeDL(opts) as ydl:
                res = ydl.extract_info(f"ytsearch20:{text}", download=False)
                raw_entries = res.get("entries", []) if res else []

                filtered_entries = []
                for entry in raw_entries:
                    if not entry:
                        continue
                    dur = entry.get("duration") or 0
                    if dur > 0 and dur < 30: # Qisqa videolarni (Shorts) o'tkazib yuborish
                        continue
                    filtered_entries.append({
                        "id": entry.get("id"),
                        "title": entry.get("title"),
                        "duration": dur
                    })
                    if len(filtered_entries) == 20:
                        break
                return filtered_entries

        results = await loop.run_in_executor(None, search_music)

        if not results:
            await msg.edit_text("😔 Qo'shiq topilmadi. Qaytadan urinib ko'ring.")
            return

        search_data = {
            "query": text,
            "results": results,
            "page": 0
        }
        await set_user_search(chat_id, search_data)

        res_text, reply_markup = render_search_page(chat_id, search_data, page=0)
        await msg.edit_text(res_text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Search error: {e}")
        await msg.edit_text("❌ Qidiruv jarayonida xatolik yuz berdi.")

async def handle_direct_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice, Video, Audio xabarlaridan Shazam orqali musiqani aniqlash"""
    message = update.message
    status_msg = await message.reply_text("🎧 Media qabul qilindi! Shazam orqali musiqa aniqlanmoqda...")

    file_obj = None
    file_ext = ".mp4"

    if message.voice:
        file_obj = await message.voice.get_file()
        file_ext = ".ogg"
    elif message.audio:
        file_obj = await message.audio.get_file()
        file_ext = ".mp3"
    elif message.video:
        file_obj = await message.video.get_file()
        file_ext = ".mp4"
    elif message.video_note:
        file_obj = await message.video_note.get_file()
        file_ext = ".mp4"

    if not file_obj:
        await status_msg.edit_text("❌ Faylni yuklab bo'lmadi.")
        return

    file_path = os.path.join(DOWNLOAD_DIR, f"direct_{uuid.uuid4().hex[:8]}{file_ext}")
    await file_obj.download_to_drive(file_path)

    if not HAS_FFMPEG:
        await status_msg.edit_text("⚠️ **Xatolik: FFmpeg o'rnatilmagan!** Shazam ishlashi uchun FFmpeg talab etiladi.")
        if os.path.exists(file_path):
            os.remove(file_path)
        return

    shazam = Shazam()
    try:
        out = await shazam.recognize(file_path)
        track = out.get("track")

        if track:
            title = track.get("title", "Noma'lum")
            subtitle = track.get("subtitle", "Noma'lum ijrochi")
            query_text = f"{subtitle} {title}"
            await status_msg.edit_text(f"✅ Topildi: **{subtitle} - {title}**\n\n🔎 Variantlar qidirilmoqda...", parse_mode="Markdown")

            loop = asyncio.get_running_loop()

            def search_shazam_variants():
                opts = dict(YDL_GENERAL_OPTS)
                opts.update({"extract_flat": True, "skip_download": True})
                with YoutubeDL(opts) as ydl:
                    res = ydl.extract_info(f"ytsearch5:{query_text}", download=False)
                    entries = res.get("entries", []) if res else []
                    return [
                        {"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration") or 0}
                        for e in entries if e
                    ]

            results = await loop.run_in_executor(None, search_shazam_variants)
            if results:
                search_data = {
                    "query": query_text,
                    "results": results,
                    "page": 0
                }
                await set_user_search(update.effective_chat.id, search_data)
                res_text, reply_markup = render_search_page(update.effective_chat.id, search_data, page=0)
                await status_msg.edit_text(res_text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await status_msg.edit_text(f"🎵 Musiqa: **{subtitle} - {title}**", parse_mode="Markdown")
        else:
            await status_msg.edit_text("⚠️ Musiqani aniqlab bo'lmadi.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Tahlil xatoligi: `{str(e)[:100]}`", parse_mode="Markdown")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tugmalar bosilgandagi callback hodisalariga ishlov berish"""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    # 1. Sahifalash (Pagination)
    if data.startswith("page_"):
        page_num = int(data.split("_")[1])
        search_data = await get_user_search(chat_id)
        if search_data:
            search_data["page"] = page_num
            await set_user_search(chat_id, search_data)
            res_text, reply_markup = render_search_page(chat_id, search_data, page=page_num)
            if res_text and reply_markup:
                await query.message.edit_text(res_text, reply_markup=reply_markup, parse_mode="Markdown")
        return

    # 2. Qo'shiq so'zlari (Lyrics)
    elif data.startswith("lyrics_"):
        parts = data.split("_")
        status_msg = await query.message.reply_text("📜 Qo'shiq so'zlari izlanmoqda...")

        song_title = ""
        if len(parts) > 2 and parts[1] == "yt":
            video_id = parts[2]
            yt_raw = None
            if redis_client:
                try:
                    yt_raw = await redis_client.get(f"yt_info:{video_id}")
                except Exception:
                    pass
            if yt_raw:
                song_title = json.loads(yt_raw).get("title", "")
            else:
                yt_obj = local_cache.get(f"yt_info:{video_id}")
                if yt_obj:
                    song_title = yt_obj.get("title", "")
        else:
            search_data = await get_user_search(chat_id)
            if search_data and search_data.get("results"):
                song_title = search_data["results"][0].get("title", "")

        lyrics = await fetch_lyrics(song_title) if song_title else None
        if lyrics:
            await status_msg.edit_text(f"📜 **Qo'shiq so'zlari:**\n🎵 *{song_title}*\n\n{lyrics[:3500]}", parse_mode="Markdown")
        else:
            await status_msg.edit_text(f"❌ **{song_title}** uchun matn topilmadi.", parse_mode="Markdown")
        return

    # 3. Videodagi Musiqani Shazam Orqali Topish
    elif data.startswith("shazam_"):
        if not HAS_FFMPEG:
            await query.message.reply_text("⚠️ **Xatolik: FFmpeg o'rnatilmagan!** Shazam ishlashi uchun FFmpeg talab etiladi.")
            return

        status_msg = await query.message.reply_text("🎧 Shazam videodagi musiqani aniqlamoqda...")

        video_obj = query.message.video or query.message.video_note or query.message.audio or query.message.voice
        if not video_obj:
            try:
                await status_msg.edit_text("❌ Musiqa izlash uchun media fayli topilmadi.")
            except Exception:
                pass
            return

        temp_file_path = os.path.join(DOWNLOAD_DIR, f"shazam_{uuid.uuid4().hex[:8]}.mp4")
        try:
            tg_file = await video_obj.get_file(read_timeout=120, write_timeout=120)
            await tg_file.download_to_drive(temp_file_path)

            shazam = Shazam()
            out = await asyncio.wait_for(shazam.recognize(temp_file_path), timeout=30.0)
            track = out.get("track")

            if track:
                title = track.get("title", "Noma'lum")
                subtitle = track.get("subtitle", "Noma'lum ijrochi")
                query_text = f"{subtitle} {title}"
                try:
                    await status_msg.edit_text(f"✅ Topildi: **{subtitle} - {title}**\n\n🔎 Variantlar qidirilmoqda...", parse_mode="Markdown")
                except Exception:
                    pass

                loop = asyncio.get_running_loop()

                def search_shazam_variants():
                    opts = dict(YDL_GENERAL_OPTS)
                    opts.update({"extract_flat": True, "skip_download": True})
                    with YoutubeDL(opts) as ydl:
                        res = ydl.extract_info(f"ytsearch5:{query_text}", download=False)
                        entries = res.get("entries", []) if res else []
                        return [
                            {"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration") or 0}
                            for e in entries if e
                        ]

                results = await loop.run_in_executor(None, search_shazam_variants)
                if results:
                    search_data = {
                        "query": query_text,
                        "results": results,
                        "page": 0
                    }
                    await set_user_search(chat_id, search_data)
                    res_text, reply_markup = render_search_page(chat_id, search_data, page=0)
                    try:
                        await status_msg.edit_text(res_text, reply_markup=reply_markup, parse_mode="Markdown")
                    except Exception:
                        pass
                else:
                    try:
                        await status_msg.edit_text(f"🎵 Musiqa: **{subtitle} - {title}**", parse_mode="Markdown")
                    except Exception:
                        pass
            else:
                try:
                    await status_msg.edit_text("⚠️ Videodagi musiqa aniqlanmadi.")
                except Exception:
                    pass
        except asyncio.TimeoutError:
            logger.error("Shazam Timeout")
            try:
                await status_msg.edit_text("⏰ Server bilan aloqa sekinlashdi. Qayta urinib ko'ring.")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Shazam video error: {e}")
            try:
                await status_msg.edit_text(f"❌ Tahlil xatoligi: `{str(e)[:100]}`", parse_mode="Markdown")
            except Exception:
                pass
        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
        return

    # 4. YouTube Video/Audio Yuklash
    elif data.startswith("yt_"):
        parts = data.split("_")
        quality = parts[1]
        video_id = parts[2]

        yt_data = None
        if redis_client:
            try:
                yt_raw = await redis_client.get(f"yt_info:{video_id}")
                if yt_raw:
                    yt_data = json.loads(yt_raw)
            except Exception:
                pass

        if not yt_data:
            yt_data = local_cache.get(f"yt_info:{video_id}")

        if not yt_data:
            await query.message.reply_text("⚠️ Havola eskirgan. Qaytadan yuboring.")
            return

        # Agar audio bo'lsa va bazada bor bo'lsa - darhol yuboramiz
        if quality == "audio":
            cached_db_song = db_get_song(video_id)
            if cached_db_song:
                kb = [[InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data=f"lyrics_yt_{video_id}")]]
                await query.message.reply_audio(
                    audio=cached_db_song["file_id"],
                    title=cached_db_song["title"],
                    performer=cached_db_song["performer"],
                    caption="⚡️ **Muvix Bazasidan darhol yuborildi!**",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
                return

        url = yt_data["url"]
        status_msg = await query.message.reply_text(f"⏳ YouTube ({quality}) yuklanmoqda...")
        loop = asyncio.get_running_loop()

        async with download_semaphore:
            file_unique_id = f"yt_{video_id}_{uuid.uuid4().hex[:6]}"

            def download_yt():
                opts = dict(YDL_GENERAL_OPTS)
                if quality == "audio":
                    opts.update({
                        "format": "bestaudio[ext=m4a]/bestaudio/best",
                        "outtmpl": f"{DOWNLOAD_DIR}/{file_unique_id}.%(ext)s",
                    })
                    if HAS_FFMPEG:
                        opts["postprocessors"] = [{
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }]
                else:
                    height = quality.replace("p", "")
                    opts.update({
                        "format": f"best[height<={height}][ext=mp4]/best[height<={height}]/best",
                        "outtmpl": f"{DOWNLOAD_DIR}/{file_unique_id}.%(ext)s",
                    })

                with YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
                    for file in os.listdir(DOWNLOAD_DIR):
                        if file.startswith(file_unique_id) and not file.endswith(".temp"):
                            full_p = os.path.join(DOWNLOAD_DIR, file)
                            is_aud = quality == "audio" or file.endswith((".mp3", ".m4a", ".aac"))
                            return full_p, ("audio" if is_aud else "video")
                    return None, None

            try:
                file_path, file_type = await loop.run_in_executor(None, download_yt)

                if file_path and os.path.exists(file_path):
                    if os.path.getsize(file_path) > MAX_FILE_SIZE_BYTES:
                        await status_msg.edit_text("⚠️ Fayl hajmi 50MB dan katta bo'lgani uchun yuborib bo'lmaydi.")
                        os.remove(file_path)
                        return

                    kb = [[InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data=f"lyrics_yt_{video_id}")]]
                    if file_type == "audio":
                        channel_msg = None
                        try:
                            with open(file_path, "rb") as ch_audio:
                                channel_msg = await context.bot.send_audio(
                                    chat_id=CHANNEL_ID,
                                    audio=ch_audio,
                                    title=yt_data["title"],
                                    performer="Muvix Music",
                                    caption=f"🎵 **{yt_data['title']}**\n🆔 `{video_id}`",
                                    parse_mode="Markdown"
                                )
                        except Exception as ch_err:
                            logger.error(f"Channel Upload Error: {ch_err}")

                        with open(file_path, "rb") as audio_file:
                            sent_audio = await query.message.reply_audio(
                                audio=audio_file,
                                title=yt_data["title"],
                                performer="YouTube Music",
                                reply_markup=InlineKeyboardMarkup(kb),
                            )

                        if channel_msg and channel_msg.audio:
                            db_save_song(
                                song_id=video_id,
                                file_id=channel_msg.audio.file_id,
                                title=yt_data["title"],
                                performer="YouTube Music",
                                duration=channel_msg.audio.duration or 0,
                                channel_msg_id=channel_msg.message_id
                            )
                        elif sent_audio and sent_audio.audio:
                            db_save_song(
                                song_id=video_id,
                                file_id=sent_audio.audio.file_id,
                                title=yt_data["title"],
                                performer="YouTube Music",
                                duration=sent_audio.audio.duration or 0,
                                channel_msg_id=0
                            )
                    else:
                        with open(file_path, "rb") as video_file:
                            await query.message.reply_video(
                                video=video_file,
                                caption=f"✅ YouTube ({quality})\n🎬 {yt_data['title']}",
                                reply_markup=InlineKeyboardMarkup(kb),
                            )
                    await status_msg.delete()
                    if os.path.exists(file_path):
                        os.remove(file_path)
                else:
                    await status_msg.edit_text("❌ Faylni yuklab bo'lmadi.")
            except Exception as e:
                await status_msg.edit_text(f"❌ Yuklash xatoligi: {str(e)[:100]}")
        return

    # 5. Qidiruv Natijalaridan MP3 Yuklash
    elif data.startswith("song_"):
        idx = int(data.split("_")[1])
        search_data = await get_user_search(chat_id)

        if not search_data or "results" not in search_data:
            await query.message.reply_text("⚠️ Qidiruv natijalari eskirgan. Qaytadan qidiring.")
            return

        results = search_data["results"]
        if idx >= len(results):
            await query.message.reply_text("⚠️ Natija topilmadi.")
            return

        item = results[idx]
        song_id = item["id"]

        # 1-Navbatda SQLite Bazani Tekshirish
        cached_db_song = db_get_song(song_id)
        if cached_db_song:
            kb = [[InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data=f"lyrics_yt_{song_id}")]]
            await query.message.reply_audio(
                audio=cached_db_song["file_id"],
                title=cached_db_song["title"],
                performer=cached_db_song["performer"],
                caption="⚡️ **Muvix Bazasidan bir zumda yuborildi!**",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return

        url = f"https://www.youtube.com/watch?v={song_id}"
        status_msg = await query.message.reply_text("⏳ Qo'shiq yuklanmoqda...")
        loop = asyncio.get_running_loop()

        async with download_semaphore:
            file_unique_id = f"song_{song_id}_{uuid.uuid4().hex[:6]}"

            def download_mp3():
                opts = dict(YDL_GENERAL_OPTS)
                opts.update({
                    "format": "bestaudio/best",
                    "outtmpl": f"{DOWNLOAD_DIR}/{file_unique_id}.%(ext)s",
                })
                if HAS_FFMPEG:
                    opts["postprocessors"] = [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }]
                with YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
                    for file in os.listdir(DOWNLOAD_DIR):
                        if file.startswith(file_unique_id) and not file.endswith(".temp"):
                            return os.path.join(DOWNLOAD_DIR, file)
                    return None

            try:
                audio_path = await loop.run_in_executor(None, download_mp3)

                if audio_path and os.path.exists(audio_path):
                    if os.path.getsize(audio_path) > MAX_FILE_SIZE_BYTES:
                        await status_msg.edit_text("⚠️ Audio hajmi 50MB dan katta.")
                        os.remove(audio_path)
                        return

                    kb = [[InlineKeyboardButton("📜 Qo'shiq so'zlari", callback_data=f"lyrics_yt_{song_id}")]]

                    channel_msg = None
                    try:
                        with open(audio_path, "rb") as ch_audio:
                            channel_msg = await context.bot.send_audio(
                                chat_id=CHANNEL_ID,
                                audio=ch_audio,
                                title=item.get("title"),
                                performer="Muvix Music",
                                caption=f"🎵 **{item.get('title')}**\n🆔 `{song_id}`",
                                parse_mode="Markdown"
                            )
                    except Exception as ch_err:
                        logger.error(f"Channel Upload Error: {ch_err}")

                    with open(audio_path, "rb") as audio_file:
                        sent_msg = await query.message.reply_audio(
                            audio=audio_file,
                            title=item.get("title"),
                            performer="Muvix Music",
                            reply_markup=InlineKeyboardMarkup(kb),
                        )

                    file_id_to_save = channel_msg.audio.file_id if (channel_msg and channel_msg.audio) else (sent_msg.audio.file_id if sent_msg.audio else "")
                    msg_id_to_save = channel_msg.message_id if channel_msg else 0

                    if file_id_to_save:
                        db_save_song(
                            song_id=song_id,
                            file_id=file_id_to_save,
                            title=item.get("title", ""),
                            performer="Muvix Music",
                            duration=item.get("duration", 0),
                            channel_msg_id=msg_id_to_save
                        )

                    await status_msg.delete()
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                else:
                    await status_msg.edit_text("❌ Qo'shiqni yuklab bo'lmadi.")
            except Exception as e:
                logger.error(f"Song download error: {e}")
                await status_msg.edit_text("❌ Qo'shiqni yuklashda xatolik yuz berdi.")
        return


# ==========================================
# 4. BOTNI ISHGA TUSHIRISH (MAIN)
# ==========================================
async def post_init(application: Application):
    """Bot ishga tushganida Redis aloqasini o'rnatish"""
    global redis_client
    try:
        redis_client = aioredis.from_url(REDIS_URL)
        await redis_client.ping()
        logger.info("✅ Redis ma'lumotlar bazasiga ulandi!")
    except Exception as e:
        logger.warning(f"⚠️ Redis ulanmadi ({e}). Bot lokal kesh rejimida ishlaydi.")

def main():
    if TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or ":" not in TOKEN:
        print("\n❌ XATOLIK: Bot TOKEN kiritilmagan!")
        print("💡 Telegram'da @BotFather'dan olgan tokeningizni kiriting.\n")
        return

    builder = Application.builder().token(TOKEN)

    # Local Telegram Bot API Server ishlatilsa (2GB gacha fayl yuklash uchun)
    local_bot_api_url = os.getenv("BOT_API_URL")
    if local_bot_api_url:
        builder.base_url(local_bot_api_url)
        builder.local_mode(True)
        logger.info(f"🚀 Local Bot API Server ishga tushirildi: {local_bot_api_url}")

    app = (
        builder
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(60)
        .pool_timeout(120)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE, handle_direct_media))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("🤖 Bot muvaffaqiyatli ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
