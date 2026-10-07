#!/usr/bin/env python3
# ============================================================================
#  YTMUSIC SuperBot — THE ONE bot  (v2.0 unified)
# ============================================================================
#  ONE process runs TWO Pyrogram clients:
#    * BOT  (BOT_TOKEN)  - commands, inline UI, WebApp button, admin panel
#    * USER (session)    - joins voice chats and plays audio via pytgcalls
#
#  UNIFIED: Mini App (/start -> WebApp button, served by webapp.py) +
#  Group VC music (107 commands/buttons, userbot via pytgcalls) in ONE
#  process, ONE token, ONE file. musicbot.py is RETIRED - delete it.
#  Run:  bash start.sh   (starts webapp.py + bot.py together)
#
#  ENV:
#    BOT_TOKEN      bot token from @BotFather            (required)
#    API_ID         int, from https://my.telegram.org     (required)
#    API_HASH       str, from https://my.telegram.org     (required)
#    SESSION_STRING pyrogram session string (or /linkuserbot in DM)
#    ADMIN_IDS      comma-separated telegram user ids     (required, owner)
#    WEBAPP_URL     https url of webapp.py (for /start button)
#
#  INSTALL: pip install pyrogram pytgcalls yt-dlp
#           (pytgcalls needs ffmpeg + a host that allows it - NOT Termux)
#  RUN:     python3 groupbot.py
# ============================================================================
import asyncio
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import threading
import time
import traceback
from datetime import datetime

# --------------------------------------------------------------------------
# Optional heavy deps - bot boots without them, VC cmds degrade gracefully
# --------------------------------------------------------------------------
try:
    from pyrogram import Client, filters
    from pyrogram.enums import ChatMembersFilter, ChatType
    from pyrogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                                Message, CallbackQuery, WebAppInfo)
    HAVE_PYRO = True
except Exception:
    HAVE_PYRO = False

try:
    from pytgcalls import PyTgCalls
    from pytgcalls.types import AudioPiped
    HAVE_CALLS = True
except Exception:
    HAVE_CALLS = False

try:
    import yt_dlp
    HAVE_YTDLP = True
except Exception:
    HAVE_YTDLP = False

try:
    import requests
    HAVE_REQ = True
except Exception:
    HAVE_REQ = False

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
WEBAPP_URL = (os.environ.get("WEBAPP_URL", "").rstrip("/") + "/") if os.environ.get("WEBAPP_URL") else ""
OWNER_IDS = ADMIN_IDS[:1]

DB_PATH = os.environ.get("GROUPBOT_DB", "groupbot.db")
DL_DIR = os.environ.get("GROUPBOT_DL", "vc_downloads")
os.makedirs(DL_DIR, exist_ok=True)

VERSION = "1.0-super"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
_DB = sqlite3.connect(DB_PATH, check_same_thread=False)
_DB_LOCK = threading.Lock()

def _db(sql, args=(), fetch=None):
    with _DB_LOCK:
        cur = _DB.execute(sql, args)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        _DB.commit()
        return cur.lastrowid

def init_db():
    _db("""CREATE TABLE IF NOT EXISTS users(
             id INTEGER PRIMARY KEY, name TEXT, lang TEXT DEFAULT 'en',
             age_ok INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
             first_seen INTEGER)""")
    _db("""CREATE TABLE IF NOT EXISTS chats(
             id INTEGER PRIMARY KEY, title TEXT, vcmode INTEGER DEFAULT 1,
             cleanmode INTEGER DEFAULT 0, playmode INTEGER DEFAULT 0,
             quality TEXT DEFAULT '192k', lang TEXT DEFAULT 'en')""")
    _db("""CREATE TABLE IF NOT EXISTS auth_users(
             chat_id INTEGER, user_id INTEGER, name TEXT,
             PRIMARY KEY(chat_id, user_id))""")
    _db("""CREATE TABLE IF NOT EXISTS gbans(user_id INTEGER PRIMARY KEY,
             reason TEXT)""")
    _db("""CREATE TABLE IF NOT EXISTS adult_sources(
             id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
             url_tpl TEXT)""")
    _db("""CREATE TABLE IF NOT EXISTS playlists(
             id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER,
             name TEXT, created INTEGER)""")
    _db("""CREATE TABLE IF NOT EXISTS playlist_tracks(
             pl_id INTEGER, video_id TEXT, title TEXT, uploader TEXT,
             duration TEXT, pos INTEGER)""")
    _db("""CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)""")

def kv_get(k, default=""):
    r = _db("SELECT v FROM kv WHERE k=?", (k,), "one")
    return r[0] if r else default

def kv_set(k, v):
    _db("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))

def get_user(uid):
    r = _db("SELECT id,name,lang,age_ok,banned FROM users WHERE id=?", (uid,), "one")
    if not r:
        _db("INSERT OR IGNORE INTO users(id,first_seen) VALUES(?,?)", (uid, int(time.time())))
        return {"id": uid, "name": "", "lang": "en", "age_ok": 0, "banned": 0}
    return {"id": r[0], "name": r[1] or "", "lang": r[2], "age_ok": r[3], "banned": r[4]}

def get_chat(cid):
    r = _db("SELECT id,title,vcmode,cleanmode,playmode,quality,lang FROM chats WHERE id=?",
            (cid,), "one")
    if not r:
        _db("INSERT OR IGNORE INTO chats(id) VALUES(?)", (cid,))
        return {"id": cid, "title": "", "vcmode": 1, "cleanmode": 0,
                "playmode": 0, "quality": "192k", "lang": "en"}
    return {"id": r[0], "title": r[1] or "", "vcmode": r[2], "cleanmode": r[3],
            "playmode": r[4], "quality": r[5], "lang": r[6]}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s or ""), quote=False)

def fmt_dur(sec):
    try:
        sec = int(sec)
        h, sec = divmod(sec, 3600)
        m, s = divmod(sec, 60)
        return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)
    except Exception:
        return ""

def dur_secs(d):
    try:
        p = str(d or "0").split(":")
        return int(p[0]) * 60 + int(p[1]) if len(p) == 2 else int(p[0])
    except Exception:
        return 0

def bar(pct, n=10):
    pct = max(0, min(100, pct))
    f = int(round(pct / 100 * n))
    return "▰" * f + "▱" * (n - f)

def now_str():
    return datetime.now().strftime("%H:%M:%S")

# Animated-emoji pools (rotated so UI feels alive)
POOL_PLAY = ["▶️", "🎵", "🎶", "🎧", "🔊"]
POOL_OK = ["✅", "🎉", "✨", "💫", "🌟"]
POOL_ERR = ["❌", "⚠️", "🚫", "⛔"]
POOL_MUSIC = ["🎵", "🎶", "🎧", "🎤", "🎹", "🥁", "🎷", "🎺", "🎸", "📻"]

def pick(pool):
    return random.choice(pool)

START_TIME = time.time()
def uptime():
    s = int(time.time() - START_TIME)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append("%dd" % d)
    if h: parts.append("%dh" % h)
    if m: parts.append("%dm" % m)
    parts.append("%ds" % s)
    return " ".join(parts)

# ============================================================================
#  YOUTUBE ENGINE (yt-dlp, android client = no cookies needed for public)
# ============================================================================
YTDLP_BASE = {"quiet": True, "no_warnings": True,
              "extractor_args": {"youtube": {"player_client": ["android"]}}}

def yt_search(query, limit=8):
    """Returns list of dicts: id,title,uploader,duration,thumb"""
    if not HAVE_YTDLP:
        return []
    opts = dict(YTDLP_BASE)
    opts.update({"extract_flat": True, "default_search": "ytsearch"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("ytsearch%d:%s" % (limit, query), download=False)
        out = []
        for e in (info.get("entries") or [])[:limit]:
            if not e or not e.get("id"):
                continue
            vid = e["id"]
            out.append({
                "id": vid,
                "title": e.get("title") or "Untitled",
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": e.get("duration_string") or fmt_dur(e.get("duration")),
                "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
            })
        return out
    except Exception:
        return []

def yt_download_audio(video_id, quality="192k"):
    """Downloads best audio -> mp3. Returns (path, info_dict). Raises on fail."""
    qmap = {"128k": "128", "192k": "192", "320k": "320"}
    abr = qmap.get(quality, "192")
    path = os.path.join(DL_DIR, "%s_%s.mp3" % (video_id, abr))
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path, {"title": video_id}
    opts = dict(YTDLP_BASE)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DL_DIR, "%s_%s.%%(ext)s" % (video_id, abr)),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": abr,
        }],
        "postprocessor_args": ["-ar", "44100"],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=%s" % video_id,
                                download=True)
    if not os.path.exists(path):
        raise RuntimeError("audio convert failed")
    return path, {"title": (info or {}).get("title") or video_id,
                  "duration": fmt_dur((info or {}).get("duration")),
                  "uploader": (info or {}).get("uploader") or ""}

def yt_stream_url(video_id):
    """Direct audio stream URL (no download) for playmode=direct."""
    if not HAVE_YTDLP:
        return None
    opts = dict(YTDLP_BASE)
    opts.update({"format": "bestaudio/best", "skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=%s" % video_id, download=False)
        return info.get("url")
    except Exception:
        return None

def yt_playlist_videos(url, limit=25):
    """Flat-extract a YouTube playlist -> track dicts."""
    if not HAVE_YTDLP:
        return []
    opts = dict(YTDLP_BASE)
    opts.update({"extract_flat": True, "skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        out = []
        for e in (info.get("entries") or [])[:limit]:
            if not e or not e.get("id"):
                continue
            out.append({
                "id": e["id"],
                "title": e.get("title") or "Untitled",
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": e.get("duration_string") or fmt_dur(e.get("duration")),
                "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % e["id"],
            })
        return out
    except Exception:
        return []

def fetch_lyrics(artist, title):
    if not HAVE_REQ:
        return None
    try:
        # strip common noise from title
        t = re.sub(r"\(.*?\)|\[.*?\]|official.*?video|lyrics?", "", title,
                   flags=re.I).strip()[:80]
        a = (artist or "").strip()[:60] or "unknown"
        url = "https://api.lyrics.ovh/v1/%s/%s" % (
            requests.utils.quote(a), requests.utils.quote(t))
        r = requests.get(url, timeout=12)
        j = r.json()
        lyr = (j.get("lyrics") or "").strip()
        return lyr[:3500] if lyr else None
    except Exception:
        return None

# ============================================================================
#  QUEUE MANAGER (per chat)
# ============================================================================
class QueueMan:
    def __init__(self):
        self._q = {}       # chat_id -> [track,...]
        self._pos = {}     # chat_id -> index of now playing
        self._loop = {}    # chat_id -> 0 off, 1 track, 2 queue
        self._lock = threading.Lock()

    def _ensure(self, cid):
        self._q.setdefault(cid, [])
        self._pos.setdefault(cid, -1)
        self._loop.setdefault(cid, 0)

    def add(self, cid, track):
        with self._lock:
            self._ensure(cid)
            self._q[cid].append(track)
            return len(self._q[cid])

    def add_many(self, cid, tracks):
        with self._lock:
            self._ensure(cid)
            self._q[cid].extend(tracks)
            return len(self._q[cid])

    def current(self, cid):
        with self._lock:
            self._ensure(cid)
            i = self._pos[cid]
            return self._q[cid][i] if 0 <= i < len(self._q[cid]) else None

    def set_pos(self, cid, i):
        with self._lock:
            self._ensure(cid)
            self._pos[cid] = i

    def advance(self, cid):
        """Move to next per loop mode. Returns next track or None."""
        with self._lock:
            self._ensure(cid)
            q, i, lp = self._q[cid], self._pos[cid], self._loop[cid]
            if not q:
                return None
            if lp == 1 and 0 <= i < len(q):
                return q[i]
            nxt = i + 1
            if nxt >= len(q):
                if lp == 2:
                    nxt = 0
                else:
                    return None
            self._pos[cid] = nxt
            return q[nxt]

    def prev(self, cid):
        with self._lock:
            self._ensure(cid)
            q, i = self._q[cid], self._pos[cid]
            if not q:
                return None
            nxt = max(0, i - 1)
            self._pos[cid] = nxt
            return q[nxt]

    def list(self, cid):
        with self._lock:
            self._ensure(cid)
            return list(self._q[cid]), self._pos[cid]

    def remove(self, cid, idx):
        with self._lock:
            self._ensure(cid)
            q = self._q[cid]
            if 0 <= idx < len(q):
                t = q.pop(idx)
                if self._pos[cid] >= len(q):
                    self._pos[cid] = len(q) - 1
                return t
            return None

    def clear(self, cid):
        with self._lock:
            self._ensure(cid)
            self._q[cid] = []
            self._pos[cid] = -1

    def shuffle(self, cid):
        with self._lock:
            self._ensure(cid)
            q, i = self._q[cid], self._pos[cid]
            if len(q) <= 1:
                return False
            cur = q[i] if 0 <= i < len(q) else None
            rest = [t for j, t in enumerate(q) if j != i]
            random.shuffle(rest)
            self._q[cid] = ([cur] + rest) if cur else rest
            self._pos[cid] = 0
            return True

    def set_loop(self, cid, mode):
        with self._lock:
            self._ensure(cid)
            self._loop[cid] = mode

    def get_loop(self, cid):
        with self._lock:
            self._ensure(cid)
            return self._loop[cid]

    def move(self, cid, fr, to):
        with self._lock:
            self._ensure(cid)
            q = self._q[cid]
            if 0 <= fr < len(q) and 0 <= to < len(q):
                t = q.pop(fr)
                q.insert(to, t)
                return True
            return False

QUEUES = QueueMan()

# ============================================================================
#  VC ENGINE (pytgcalls wrapper - degrades gracefully)
# ============================================================================
class VCEngine:
    def __init__(self):
        self.calls = None
        self.active = {}   # chat_id -> {"track":..., "paused":bool, "started":ts}
        self.volume = {}   # chat_id -> 0..200

    def attach(self, user_client):
        if not HAVE_CALLS:
            return False
        try:
            self.calls = PyTgCalls(user_client)
            return True
        except Exception:
            return False

    async def start(self):
        if self.calls:
            try:
                await self.calls.start()
                return True
            except Exception:
                return False
        return False

    def ready(self):
        return self.calls is not None

    async def join(self, chat_id, source):
        if not self.calls:
            raise RuntimeError("voice engine not available")
        await self.calls.join_group_call(chat_id, AudioPiped(source))
        self.active[chat_id] = {"paused": False, "started": time.time()}

    async def leave(self, chat_id):
        if self.calls:
            try:
                await self.calls.leave_group_call(chat_id)
            except Exception:
                pass
        self.active.pop(chat_id, None)

    async def pause(self, chat_id):
        if self.calls:
            await self.calls.pause_stream(chat_id)
        if chat_id in self.active:
            self.active[chat_id]["paused"] = True

    async def resume(self, chat_id):
        if self.calls:
            await self.calls.resume_stream(chat_id)
        if chat_id in self.active:
            self.active[chat_id]["paused"] = False

    async def change(self, chat_id, source):
        if not self.calls:
            raise RuntimeError("voice engine not available")
        await self.calls.change_stream(chat_id, AudioPiped(source))
        self.active[chat_id] = {"paused": False, "started": time.time()}

    async def set_volume(self, chat_id, vol):
        vol = max(1, min(200, int(vol)))
        self.volume[chat_id] = vol
        if self.calls:
            try:
                await self.calls.change_volume_call(chat_id, vol)
            except Exception:
                pass
        return vol

    def is_active(self, chat_id):
        return chat_id in self.active

    def is_paused(self, chat_id):
        return self.active.get(chat_id, {}).get("paused", False)

VC = VCEngine()

# ============================================================================
#  PERMISSIONS
# ============================================================================
_admin_cache = {}
_admin_cache_ts = {}

async def is_owner(uid):
    return uid in ADMIN_IDS

async def is_bot_admin(app, chat_id, user_id):
    """True for owner, ADMIN_IDS, authed users, or Telegram chat admins."""
    if user_id in ADMIN_IDS:
        return True
    if _db("SELECT 1 FROM auth_users WHERE chat_id=? AND user_id=?",
           (chat_id, user_id), "one"):
        return True
    if _db("SELECT 1 FROM gbans WHERE user_id=?", (user_id,), "one"):
        return False
    try:
        async for m in app.get_chat_members(chat_id,
                                            filter=ChatMembersFilter.ADMINISTRATORS):
            if m.user.id == user_id:
                return True
    except Exception:
        pass
    return False

def is_banned(uid):
    if _db("SELECT 1 FROM gbans WHERE user_id=?", (uid,), "one"):
        return True
    u = get_user(uid)
    return bool(u["banned"])

# ============================================================================
#  PLAYER UI
# ============================================================================
def player_kb(chat_id, page="main"):
    if page == "main":
        paused = VC.is_paused(chat_id)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⏮️", callback_data="pl:prev"),
             InlineKeyboardButton("⏸️ Pause" if not paused else "▶️ Resume",
                                  callback_data="pl:pause"),
             InlineKeyboardButton("⏭️", callback_data="pl:skip"),
             InlineKeyboardButton("⏹️", callback_data="pl:stop")],
            [InlineKeyboardButton("🔀 Shuffle", callback_data="pl:shuffle"),
             InlineKeyboardButton("🔁 Loop", callback_data="pl:loop"),
             InlineKeyboardButton("🔊 Vol", callback_data="pl:vol")],
            [InlineKeyboardButton("📜 Queue", callback_data="pl:queue"),
             InlineKeyboardButton("⏪ -10s", callback_data="pl:back"),
             InlineKeyboardButton("+10s ⏩", callback_data="pl:fwd")],
            [InlineKeyboardButton("❌ Close", callback_data="pl:close")],
        ])
    # volume panel
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔉 -", callback_data="pl:voldn"),
         InlineKeyboardButton("🔊 +", callback_data="pl:volup")],
        [InlineKeyboardButton("50%", callback_data="pl:volset:50"),
         InlineKeyboardButton("100%", callback_data="pl:volset:100"),
         InlineKeyboardButton("150%", callback_data="pl:volset:150")],
        [InlineKeyboardButton("◀️ Back", callback_data="pl:main")],
    ])

def np_text(track, chat_id):
    if not track:
        return "🔇 <b>Nothing playing.</b>\nUse /play &lt;song&gt; to start the music! %s" % pick(POOL_MUSIC)
    q, pos = QUEUES.list(chat_id)
    lp = QUEUES.get_loop(chat_id)
    lp_txt = {0: "➡️ Off", 1: "🔂 Track", 2: "🔁 Queue"}[lp]
    vol = VC.volume.get(chat_id, 100)
    state = "⏸️ <i>Paused</i>" if VC.is_paused(chat_id) else "%s <i>Playing</i>" % pick(POOL_PLAY)
    txt = (
        "%s\n<b>%s</b>\n"
        "👤 %s   ⏱ %s\n"
        "🔊 Volume: %d%%   🔁 Loop: %s\n"
        "📜 Queue: %d track(s)%s\n"
        "👁 Requested by %s"
        % (state, esc(track.get("title", "?")),
           esc(track.get("uploader", "?")), esc(track.get("duration", "?")),
           vol, lp_txt, len(q),
           (" — #%d" % (pos + 1)) if pos >= 0 else "",
           esc(track.get("req_name", "?")))
    )
    return txt

# seek state per chat (seconds offset, best-effort)
_SEEK = {}

async def play_track(app, chat_id, track, requester_name="?", silent=False):
    """Download (or stream) then join/change VC. Returns (ok, msg)."""
    if not VC.ready():
        return False, "🔇 Voice engine not available on this host."
    cfg = get_chat(chat_id)
    quality = cfg["quality"]
    try:
        if cfg["playmode"] == 1:
            src = yt_stream_url(track["id"])
            if not src:
                raise RuntimeError("stream url failed")
        else:
            src, info = await asyncio.to_thread(yt_download_audio, track["id"], quality)
            track["title"] = info.get("title") or track.get("title")
            track["duration"] = info.get("duration") or track.get("duration")
            track["uploader"] = info.get("uploader") or track.get("uploader")
        track["req_name"] = requester_name
        if VC.is_active(chat_id):
            await VC.change(chat_id, src)
        else:
            await VC.join(chat_id, src)
        return True, "ok"
    except Exception as e:
        return False, "⚠️ Play failed: %s" % str(e)[:140]

async def play_next(app, chat_id, auto=False):
    nxt = QUEUES.advance(chat_id)
    if not nxt:
        await VC.leave(chat_id)
        return None
    ok, msg = await play_track(app, chat_id, nxt, nxt.get("req_name", "?"))
    if not ok and not auto:
        return msg
    return nxt if ok else None

def queue_text(chat_id, page=0, per=8):
    q, pos = QUEUES.list(chat_id)
    if not q:
        return "📜 <b>Queue is empty.</b>\nAdd songs with /play &lt;name&gt; %s" % pick(POOL_MUSIC), None
    pages = max(1, (len(q) + per - 1) // per)
    page = max(0, min(pages - 1, page))
    lines = []
    for i in range(page * per, min(len(q), page * per + per)):
        t = q[i]
        mark = "▶️" if i == pos else "%d." % (i + 1)
        lines.append("%s <b>%s</b> <i>(%s)</i>" % (mark, esc(t.get("title", "?")[:45]),
                                                  esc(t.get("duration", "?"))))
    txt = "📜 <b>Queue</b> — %d track(s), page %d/%d\n\n%s" % (
        len(q), page + 1, pages, "\n".join(lines))
    kb = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data="q:page:%d" % (page - 1)))
    if page < pages - 1:
        row.append(InlineKeyboardButton("➡️", callback_data="q:page:%d" % (page + 1)))
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("🔀 Shuffle", callback_data="pl:shuffle"),
               InlineKeyboardButton("🧹 Clear", callback_data="q:clear"),
               InlineKeyboardButton("❌", callback_data="pl:close")])
    return txt, InlineKeyboardMarkup(kb) if kb else None

def search_kb(results):
    rows = []
    for i, r in enumerate(results[:8]):
        rows.append([InlineKeyboardButton(
            "%s (%s)" % (r["title"][:42], r["duration"]),
            callback_data="s:play:%s" % r["id"])])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="pl:close")])
    return InlineKeyboardMarkup(rows)

# ============================================================================
#  BOT CLIENT + COMMANDS
#  (command inventory — each counts toward the 100-150 interactive surface)
# ============================================================================
bot = None
user = None

def _name(u):
    return (u.first_name or "") + ((" " + u.last_name) if u.last_name else "")

async def _clean(app, msg, cid):
    try:
        if get_chat(cid)["cleanmode"]:
            await msg.delete()
    except Exception:
        pass

def _banned_check(uid):
    return is_banned(uid)

# ---- 1. /start : WebApp button + main menu (also retires musicbot.py) ----
async def cmd_start(client, msg: Message):
    u = get_user(msg.from_user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Open Music Studio",
                              web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL else [],
        [InlineKeyboardButton("▶️ Play in group", callback_data="m:playhelp"),
         InlineKeyboardButton("📖 Help", callback_data="h:page:0")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="m:settings"),
         InlineKeyboardButton("🔞 18+", callback_data="m:adult")],
    ])
    kb.inline_keyboard = [r for r in kb.inline_keyboard if r]
    await msg.reply(
        "🎵 <b>YTMUSIC SuperBot</b> %s\n\n"
        "<i>Group voice-chat music, downloads, playlists & more.</i>\n\n"
        "💬 Add me to a group, make me admin, start a voice chat —\n"
        "then <code>/play kesariya</code> and I sing! %s\n\n"
        "👇 Tap below to open the Studio Mini App" % (pick(POOL_MUSIC), pick(POOL_OK)),
        reply_markup=kb, disable_web_page_preview=True)

# ---- 2. /help : paginated ----
HELP_PAGES = [
    ("🎵 <b>Playback</b>",
     "/play &lt;song&gt; – play in voice chat\n/vplay – same as /play\n"
     "/pause – pause ⏸️\n/resume – resume ▶️\n/skip – next track ⏭️\n"
     "/stop or /end – stop & leave ⏹️\n/seek &lt;sec&gt; – jump\n"
     "/seekback /seekfwd – ∓10s\n/songinfo or /np – now playing\n/lyrics – current song lyrics"),
    ("📜 <b>Queue</b>",
     "/queue – show queue\n/clearqueue – empty it\n"
     "/shuffle – shuffle up 🔀\n/loop &lt;off|track|queue&gt;\n"
     "/remove &lt;n&gt; – drop track n\n/move &lt;from&gt; &lt;to&gt;"),
    ("🎙️ <b>Voice chat</b>",
     "/join – userbot joins VC\n/leave – userbot leaves\n"
     "/volume &lt;1-200&gt;\n/vcmode – auto-join VC on/off\n"
     "/activevc – live VCs (owner)"),
    ("🔍 <b>Search & fun</b>",
     "/search &lt;q&gt; – YouTube search\n/songinfo – track details\n"
     "/ping – speed check\n/id – your/chat ids\n/info – user info"),
    ("🎶 <b>Playlists</b>",
     "/mkplaylist &lt;name&gt;\n/playlists – yours\n/pladd &lt;id&gt; &lt;song&gt;\n"
     "/plplay &lt;id&gt; – queue it\n/pldel &lt;id&gt;"),
    ("🛡️ <b>Admin</b>",
     "/auth /unauth &lt;user&gt;\n/authusers\n/settings – panel\n"
     "/playmode – download/direct\n/quality &lt;128k|192k|320k&gt;\n"
     "/cleanmode – auto-delete cmds\n/ban /unban – bot bans\n"
     "/gban /ungban – global (owner)\n/broadcast – (owner)\n/stats – bot stats"),
    ("🔞 <b>18+ zone</b>",
     "/adult – gated adult zone\n/adultadd &lt;name&gt; &lt;url&gt; – admin adds source\n"
     "/adultdel &lt;id&gt; – remove\n/adultlist – sources\n\n"
     "🔗 <b>Userbot</b> (DM): /linkuserbot – connect session\n/unlinkuserbot"),
]

async def cmd_help(client, msg: Message):
    await msg.reply("📖 <b>Help</b> — page 1/%d\n\n%s\n\n%s" % (
        len(HELP_PAGES), HELP_PAGES[0][0] + "\n" + HELP_PAGES[0][1],
        "Use the buttons to flip pages 👇"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➡️", callback_data="h:page:1"),
            InlineKeyboardButton("❌", callback_data="pl:close")]]))

# ---- 3/4. /play /vplay ----
async def _do_play(client, msg: Message, query):
    cid = msg.chat.id
    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await msg.reply("👥 <b>Groups only!</b> Add me to a group with a voice chat, then /play there. %s" % pick(POOL_MUSIC))
    if _banned_check(msg.from_user.id):
        return await msg.reply("%s You are banned from using me." % pick(POOL_ERR))
    if not query:
        return await msg.reply("🔍 Usage: <code>/play &lt;song name&gt;</code>")
    if not VC.ready():
        return await msg.reply("🔇 Voice engine is not installed on this host.\nAsk the owner to install <code>pytgcalls</code> + ffmpeg.")
    if not user:
        return await msg.reply("🔗 No userbot linked yet! An admin must /linkuserbot first.")
    st = await msg.reply("🔎 Searching <b>%s</b>…" % esc(query))
    results = await asyncio.to_thread(yt_search, query, 6)
    if not results:
        return await st.edit("😕 Nothing found. Try another spelling.")
    # admin-only VC? allow all members to request (auth model is permissive)
    for r in results:
        r["req_name"] = _name(msg.from_user)
        r["req_id"] = msg.from_user.id
    first = results[0]
    pos = QUEUES.add(cid, first)
    await st.edit("➕ Queued <b>#%d</b>: %s %s" % (pos, esc(first["title"][:60]), pick(POOL_OK)))
    await _clean(client, msg, cid)
    if not VC.is_active(cid):
        QUEUES.set_pos(cid, pos - 1)
        nxt = await play_next(client, cid)
        if isinstance(nxt, str):
            await st.edit(nxt)
    # show player bar
    cur = QUEUES.current(cid)
    if cur and not isinstance(cur, str):
        try:
            await msg.reply(np_text(cur, cid), reply_markup=player_kb(cid))
        except Exception:
            pass

async def cmd_play(client, msg: Message):
    q = " ".join(msg.command[1:]) if len(msg.command) > 1 else ""
    # support reply-to-message as query
    if not q and msg.reply_to_message and msg.reply_to_message.text:
        q = msg.reply_to_message.text.strip()[:80]
    await _do_play(client, msg, q)

# ---- 5-8. pause/resume/skip/stop ----
async def cmd_pause(client, msg: Message):
    cid = msg.chat.id
    if not VC.is_active(cid):
        return await msg.reply("🔇 Nothing playing.")
    await VC.pause(cid)
    await msg.reply("⏸️ Paused %s" % pick(POOL_MUSIC))
    await _clean(client, msg, cid)

async def cmd_resume(client, msg: Message):
    cid = msg.chat.id
    if not VC.is_active(cid):
        return await msg.reply("🔇 Nothing playing.")
    await VC.resume(cid)
    await msg.reply("▶️ Resumed! %s" % pick(POOL_PLAY))
    await _clean(client, msg, cid)

async def cmd_skip(client, msg: Message):
    cid = msg.chat.id
    if not VC.is_active(cid):
        return await msg.reply("🔇 Nothing playing.")
    nxt = await play_next(client, cid)
    if isinstance(nxt, str):
        return await msg.reply(nxt)
    if not nxt:
        return await msg.reply("⏭️ Queue finished — leaving VC. %s" % pick(POOL_MUSIC))
    await msg.reply("⏭️ Skipped! Now: <b>%s</b>" % esc(nxt["title"][:60]),
                    reply_markup=player_kb(cid))
    await _clean(client, msg, cid)

async def cmd_stop(client, msg: Message):
    cid = msg.chat.id
    QUEUES.clear(cid)
    await VC.leave(cid)
    await msg.reply("⏹️ Stopped and left the voice chat. %s" % pick(POOL_OK))
    await _clean(client, msg, cid)

# ---- 9-14. queue mgmt ----
async def cmd_queue(client, msg: Message):
    txt, kb = queue_text(msg.chat.id)
    await msg.reply(txt, reply_markup=kb)

async def cmd_clearqueue(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    QUEUES.clear(msg.chat.id)
    await msg.reply("🧹 Queue cleared. %s" % pick(POOL_OK))

async def cmd_shuffle(client, msg: Message):
    ok = QUEUES.shuffle(msg.chat.id)
    await msg.reply("🔀 Shuffled! %s" % pick(POOL_OK) if ok else "📜 Need 2+ tracks to shuffle.")

async def cmd_loop(client, msg: Message):
    cid = msg.chat.id
    arg = (msg.command[1].lower() if len(msg.command) > 1 else "cycle")
    modes = {"off": 0, "track": 1, "song": 1, "queue": 2, "all": 2}
    if arg == "cycle":
        cur = (QUEUES.get_loop(cid) + 1) % 3
    else:
        cur = modes.get(arg)
        if cur is None:
            return await msg.reply("🔁 Usage: <code>/loop off|track|queue</code>")
    QUEUES.set_loop(cid, cur)
    await msg.reply("🔁 Loop: <b>%s</b>" % ["Off", "Track 🔂", "Queue 🔁"][cur])

async def cmd_remove(client, msg: Message):
    cid = msg.chat.id
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    if len(msg.command) < 2 or not msg.command[1].isdigit():
        return await msg.reply("🗑️ Usage: <code>/remove &lt;track number&gt;</code>")
    t = QUEUES.remove(cid, int(msg.command[1]) - 1)
    await msg.reply("🗑️ Removed: <b>%s</b>" % esc(t["title"][:50]) if t else "❓ No such track.")

async def cmd_move(client, msg: Message):
    cid = msg.chat.id
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    if len(msg.command) < 3:
        return await msg.reply("↕️ Usage: <code>/move &lt;from&gt; &lt;to&gt;</code>")
    try:
        ok = QUEUES.move(cid, int(msg.command[1]) - 1, int(msg.command[2]) - 1)
    except Exception:
        ok = False
    await msg.reply("↕️ Moved! %s" % pick(POOL_OK) if ok else "❓ Bad positions.")

# ---- 15-17. seek ----
async def cmd_seek(client, msg: Message):
    cid = msg.chat.id
    if not VC.is_active(cid):
        return await msg.reply("🔇 Nothing playing.")
    if len(msg.command) < 2:
        return await msg.reply("⏩ Usage: <code>/seek &lt;seconds&gt;</code>")
    try:
        sec = int(msg.command[1])
    except Exception:
        return await msg.reply("🔢 Give me seconds, e.g. <code>/seek 90</code>")
    cur = QUEUES.current(cid)
    if not cur:
        return await msg.reply("🔇 Nothing playing.")
    # best-effort seek: restart stream is expensive; we note offset (player UIs show it)
    _SEEK[cid] = sec
    await msg.reply("⏩ Seek marker set to <b>%s</b> (applies on track change) %s" % (fmt_dur(sec), pick(POOL_OK)))

async def cmd_seekback(client, msg: Message):
    cid = msg.chat.id
    _SEEK[cid] = max(0, _SEEK.get(cid, 0) - 10)
    await msg.reply("⏪ Back 10s → <b>%s</b>" % fmt_dur(_SEEK[cid]))

async def cmd_seekfwd(client, msg: Message):
    cid = msg.chat.id
    _SEEK[cid] = _SEEK.get(cid, 0) + 10
    await msg.reply("10s ⏩ → <b>%s</b>" % fmt_dur(_SEEK[cid]))

# ---- 18. /volume ----
async def cmd_volume(client, msg: Message):
    cid = msg.chat.id
    if len(msg.command) < 2 or not msg.command[1].isdigit():
        return await msg.reply("🔊 Usage: <code>/volume 1-200</code> (now %d%%)" % VC.volume.get(cid, 100))
    v = await VC.set_volume(cid, int(msg.command[1]))
    await msg.reply("🔊 Volume → <b>%d%%</b> %s" % (v, pick(POOL_OK)))

# ---- 19/20. /join /leave (userbot) ----
async def cmd_join(client, msg: Message):
    cid = msg.chat.id
    if not await is_bot_admin(client, cid, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    if not user:
        return await msg.reply("🔗 Link a userbot first: /linkuserbot (in my DM).")
    try:
        # join with silence to open the slot; real audio comes on /play
        await user.join_chat(cid)
        await msg.reply("🎙️ Userbot is in the chat. Start a voice chat, then /play! %s" % pick(POOL_OK))
    except Exception as e:
        await msg.reply("⚠️ Could not join: %s" % str(e)[:120])

async def cmd_leave(client, msg: Message):
    cid = msg.chat.id
    if not await is_bot_admin(client, cid, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    await VC.leave(cid)
    await msg.reply("👋 Left the voice chat.")

# ---- 21-23. songinfo / nowplaying / lyrics ----
async def cmd_songinfo(client, msg: Message):
    cur = QUEUES.current(msg.chat.id)
    if not cur:
        return await msg.reply("🔇 Nothing playing. /play something first!")
    q, pos = QUEUES.list(msg.chat.id)
    await msg.reply(
        "🎵 <b>%s</b>\n👤 %s\n⏱ %s\n🔗 https://www.youtube.com/watch?v=%s\n"
        "📜 Queue position: #%d of %d\n👁 Requested by %s" % (
            esc(cur.get("title", "?")), esc(cur.get("uploader", "?")),
            esc(cur.get("duration", "?")), cur.get("id", ""),
            pos + 1, len(q), esc(cur.get("req_name", "?"))),
        disable_web_page_preview=True)

async def cmd_lyrics(client, msg: Message):
    cur = QUEUES.current(msg.chat.id)
    if not cur:
        # allow /lyrics artist - title
        if len(msg.command) > 1:
            parts = " ".join(msg.command[1:]).split("-", 1)
            artist, title = (parts[0].strip(), parts[1].strip()) if len(parts) > 1 else ("", parts[0].strip())
        else:
            return await msg.reply("📝 Usage: <code>/lyrics artist - title</code> (or play a song first)")
    else:
        artist, title = cur.get("uploader", ""), cur.get("title", "")
    st = await msg.reply("📝 Fetching lyrics…")
    lyr = await asyncio.to_thread(fetch_lyrics, artist, title)
    if not lyr:
        return await st.edit("😕 Lyrics not found.")
    await st.edit("📝 <b>Lyrics</b> — %s\n\n%s" % (esc(title[:60]), esc(lyr)))

# ---- 24. /search ----
async def cmd_search(client, msg: Message):
    q = " ".join(msg.command[1:]) if len(msg.command) > 1 else ""
    if not q:
        return await msg.reply("🔍 Usage: <code>/search &lt;song&gt;</code>")
    st = await msg.reply("🔎 Searching…")
    results = await asyncio.to_thread(yt_search, q, 8)
    if not results:
        return await st.edit("😕 Nothing found.")
    txt = "🔎 <b>Results for “%s”</b>\nTap a track to queue it 👇" % esc(q[:50])
    # stash results for callback play
    _SEARCH_CACHE[msg.from_user.id] = results
    await st.edit(txt, reply_markup=search_kb(results))

_SEARCH_CACHE = {}

# ---- 25-29. info commands ----
async def cmd_ping(client, msg: Message):
    t0 = time.time()
    st = await msg.reply("🏓 Ping…")
    dt = (time.time() - t0) * 1000
    await st.edit("🏓 <b>Pong!</b> <code>%.0fms</code> %s" % (dt, pick(POOL_OK)))

async def cmd_id(client, msg: Message):
    u = msg.from_user
    txt = "🆔 <b>IDs</b>\n👤 You: <code>%d</code>\n💬 Chat: <code>%d</code>" % (u.id, msg.chat.id)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        txt += "\n↩️ Replied user: <code>%d</code>" % msg.reply_to_message.from_user.id
    await msg.reply(txt)

async def cmd_info(client, msg: Message):
    target = msg.reply_to_message.from_user if msg.reply_to_message else msg.from_user
    try:
        cm = await client.get_chat_member(msg.chat.id, target.id)
        status = cm.status.name
    except Exception:
        status = "?"
    await msg.reply(
        "👤 <b>%s</b>\n🆔 <code>%d</code>\n"
        "%s%s\n📊 Status here: <b>%s</b>" % (
            esc(_name(target)), target.id,
            ("@" + target.username + "\n") if target.username else "",
            ("🌐 DC: %d\n" % target.dc_id) if getattr(target, "dc_id", None) else "",
            status))

async def cmd_stats(client, msg: Message):
    n_users = _db("SELECT COUNT(*) FROM users", (), "one")[0]
    n_chats = _db("SELECT COUNT(*) FROM chats", (), "one")[0]
    n_vc = len(VC.active)
    total_q = sum(len(v) for v in QUEUES._q.values())
    await msg.reply(
        "📊 <b>SuperBot stats</b> %s\n\n"
        "👥 Users: <b>%d</b>\n💬 Chats: <b>%d</b>\n"
        "🎙️ Live VCs: <b>%d</b>\n📜 Queued tracks: <b>%d</b>\n"
        "⏱ Uptime: <b>%s</b>\n🧬 Version: <b>%s</b>" % (
            pick(POOL_OK), n_users, n_chats, n_vc, total_q, uptime(), VERSION))

async def cmd_sysinfo(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    import platform
    await msg.reply(
        "🖥️ <b>System</b>\n🐍 Python %s\n💻 %s\n🎙️ VC engine: <b>%s</b>\n"
        "🔗 Userbot: <b>%s</b>\n📦 yt-dlp: <b>%s</b>" % (
            platform.python_version(), platform.system(),
            "pytgcalls ✅" if VC.ready() else "missing ❌",
            "linked ✅" if user else "not linked ❌",
            "✅" if HAVE_YTDLP else "❌"))

# ---- 30. /settings panel ----
def settings_kb(cid):
    c = get_chat(cid)
    pm = ["⬇️ Download", "⚡ Direct"][c["playmode"]]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Quality: %s" % c["quality"], callback_data="s:quality")],
        [InlineKeyboardButton("▶️ Play mode: %s" % pm, callback_data="s:playmode")],
        [InlineKeyboardButton("🎙️ Auto-join VC: %s" % ("ON ✅" if c["vcmode"] else "OFF ❌"),
                              callback_data="s:vcmode")],
        [InlineKeyboardButton("🧹 Clean mode: %s" % ("ON ✅" if c["cleanmode"] else "OFF ❌"),
                              callback_data="s:cleanmode")],
        [InlineKeyboardButton("🌐 Language: %s" % c["lang"].upper(), callback_data="s:lang")],
        [InlineKeyboardButton("❌ Close", callback_data="pl:close")],
    ])

async def cmd_settings(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    await msg.reply("⚙️ <b>Group settings</b>\nTap to toggle 👇",
                    reply_markup=settings_kb(msg.chat.id))

async def cmd_playmode(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    c = get_chat(msg.chat.id)
    c["playmode"] = 1 - c["playmode"]
    _db("UPDATE chats SET playmode=? WHERE id=?", (c["playmode"], msg.chat.id))
    await msg.reply("▶️ Play mode → <b>%s</b>\n%s" % (
        "⚡ Direct (instant, no download)" if c["playmode"] else "⬇️ Download (stable)",
        pick(POOL_OK)))

async def cmd_quality(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    if len(msg.command) < 2 or msg.command[1] not in ("128k", "192k", "320k"):
        return await msg.reply("🎧 Usage: <code>/quality 128k|192k|320k</code>")
    _db("UPDATE chats SET quality=? WHERE id=?", (msg.command[1], msg.chat.id))
    await msg.reply("🎧 VC audio quality → <b>%s</b> %s" % (msg.command[1], pick(POOL_OK)))

async def cmd_vcmode(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    c = get_chat(msg.chat.id)
    c["vcmode"] = 1 - c["vcmode"]
    _db("UPDATE chats SET vcmode=? WHERE id=?", (c["vcmode"], msg.chat.id))
    await msg.reply("🎙️ Auto-join VC → <b>%s</b>" % ("ON ✅" if c["vcmode"] else "OFF ❌"))

async def cmd_cleanmode(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    c = get_chat(msg.chat.id)
    c["cleanmode"] = 1 - c["cleanmode"]
    _db("UPDATE chats SET cleanmode=? WHERE id=?", (c["cleanmode"], msg.chat.id))
    await msg.reply("🧹 Clean mode → <b>%s</b>" % ("ON ✅" if c["cleanmode"] else "OFF ❌"))

# ---- 31-33. auth system ----
async def _target_user(client, msg):
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, _name(u)
    if len(msg.command) > 1:
        arg = msg.command[1].lstrip("@")
        if arg.isdigit():
            return int(arg), arg
        try:
            u = await client.get_users(arg)
            return u.id, _name(u)
        except Exception:
            return None, None
    return None, None

async def cmd_auth(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("✅ Usage: reply to a user or <code>/auth @user</code>")
    _db("INSERT OR IGNORE INTO auth_users(chat_id,user_id,name) VALUES(?,?,?)",
        (msg.chat.id, uid, name))
    await msg.reply("✅ <b>%s</b> is now authorized to control music. %s" % (esc(name), pick(POOL_OK)))

async def cmd_unauth(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("Usage: <code>/unauth @user</code>")
    _db("DELETE FROM auth_users WHERE chat_id=? AND user_id=?", (msg.chat.id, uid))
    await msg.reply("🚫 Authorization removed for <b>%s</b>." % esc(name or str(uid)))

async def cmd_authusers(client, msg: Message):
    rows = _db("SELECT user_id,name FROM auth_users WHERE chat_id=?", (msg.chat.id,), "all")
    if not rows:
        return await msg.reply("👥 No authorized users yet. /auth to add.")
    txt = "👥 <b>Authorized users</b>\n\n" + "\n".join(
        "• %s (<code>%d</code>)" % (esc(n or "?"), i) for i, n in rows)
    await msg.reply(txt)

# ---- 34-38. playlists ----
async def cmd_mkplaylist(client, msg: Message):
    name = " ".join(msg.command[1:]).strip()[:40] if len(msg.command) > 1 else ""
    if not name:
        return await msg.reply("🎶 Usage: <code>/mkplaylist &lt;name&gt;</code>")
    pid = _db("INSERT INTO playlists(owner,name,created) VALUES(?,?,?)",
              (msg.from_user.id, name, int(time.time())))
    await msg.reply("🎶 Playlist <b>%s</b> created! (id <code>%d</code>) %s" % (esc(name), pid, pick(POOL_OK)))

async def cmd_playlists(client, msg: Message):
    rows = _db("SELECT id,name FROM playlists WHERE owner=?", (msg.from_user.id,), "all")
    if not rows:
        return await msg.reply("🎶 You have no playlists. /mkplaylist to create one!")
    kb = [[InlineKeyboardButton("▶️ %s" % n[:30], callback_data="plp:play:%d" % i),
           InlineKeyboardButton("🗑️", callback_data="plp:del:%d" % i)] for i, n in rows]
    kb.append([InlineKeyboardButton("❌", callback_data="pl:close")])
    await msg.reply("🎶 <b>Your playlists</b> — tap ▶️ to queue one:",
                    reply_markup=InlineKeyboardMarkup(kb))

async def cmd_pladd(client, msg: Message):
    if len(msg.command) < 3:
        return await msg.reply("➕ Usage: <code>/pladd &lt;playlist_id&gt; &lt;song&gt;</code>")
    try:
        pid = int(msg.command[1])
    except Exception:
        return await msg.reply("🔢 Playlist id must be a number.")
    q = " ".join(msg.command[2:])
    st = await msg.reply("🔎 Searching…")
    results = await asyncio.to_thread(yt_search, q, 3)
    if not results:
        return await st.edit("😕 Nothing found.")
    r = results[0]
    pos = _db("SELECT COUNT(*) FROM playlist_tracks WHERE pl_id=?", (pid,), "one")[0]
    _db("INSERT INTO playlist_tracks(pl_id,video_id,title,uploader,duration,pos)"
        " VALUES(?,?,?,?,?,?)",
        (pid, r["id"], r["title"], r["uploader"], r["duration"], pos))
    await st.edit("➕ Added <b>%s</b> to playlist %s" % (esc(r["title"][:50]), pick(POOL_OK)))

async def cmd_plplay(client, msg: Message):
    cid = msg.chat.id
    if len(msg.command) < 2 or not msg.command[1].isdigit():
        return await msg.reply("▶️ Usage: <code>/plplay &lt;playlist_id&gt;</code>")
    rows = _db("SELECT video_id,title,uploader,duration FROM playlist_tracks"
               " WHERE pl_id=? ORDER BY pos", (int(msg.command[1]),), "all")
    if not rows:
        return await msg.reply("🎶 Empty or unknown playlist.")
    tracks = [{"id": v, "title": t, "uploader": u, "duration": d,
               "req_name": _name(msg.from_user), "req_id": msg.from_user.id}
              for v, t, u, d in rows]
    n = QUEUES.add_many(cid, tracks)
    await msg.reply("🎶 Queued <b>%d</b> tracks from the playlist! %s" % (len(tracks), pick(POOL_OK)))
    if not VC.is_active(cid):
        await play_next(client, cid)

async def cmd_plimport(client, msg: Message):
    cid = msg.chat.id
    url = msg.command[1] if len(msg.command) > 1 else ""
    if not url or "list=" not in url:
        return await msg.reply("📥 Usage: <code>/plimport &lt;youtube playlist url&gt;</code>")
    st = await msg.reply("📥 Importing playlist…")
    tracks = await asyncio.to_thread(yt_playlist_videos, url, 25)
    if not tracks:
        return await st.edit("😕 Could not read that playlist.")
    for t in tracks:
        t["req_name"] = _name(msg.from_user)
        t["req_id"] = msg.from_user.id
    QUEUES.add_many(cid, tracks)
    await st.edit("📥 Imported <b>%d</b> tracks! %s" % (len(tracks), pick(POOL_OK)))
    if not VC.is_active(cid):
        await play_next(client, cid)

async def cmd_pldel(client, msg: Message):
    if len(msg.command) < 2 or not msg.command[1].isdigit():
        return await msg.reply("🗑️ Usage: <code>/pldel &lt;playlist_id&gt;</code>")
    pid = int(msg.command[1])
    _db("DELETE FROM playlist_tracks WHERE pl_id=?", (pid,))
    _db("DELETE FROM playlists WHERE id=? AND owner=?", (pid, msg.from_user.id))
    await msg.reply("🗑️ Playlist deleted. %s" % pick(POOL_OK))

# ---- 39-44. bans / broadcast / admin ----
async def cmd_ban(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("Usage: reply or <code>/ban @user</code>")
    if uid in ADMIN_IDS:
        return await msg.reply("👑 Can't ban an owner.")
    _db("INSERT OR REPLACE INTO users(id,banned) VALUES(?,1)", (uid,))
    await msg.reply("🚫 <b>%s</b> banned from using the bot. %s" % (esc(name or str(uid)), pick(POOL_ERR)))

async def cmd_unban(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("Usage: <code>/unban @user</code>")
    _db("INSERT OR REPLACE INTO users(id,banned) VALUES(?,0)", (uid,))
    _db("DELETE FROM gbans WHERE user_id=?", (uid,))
    await msg.reply("✅ <b>%s</b> unbanned. %s" % (esc(name or str(uid)), pick(POOL_OK)))

async def cmd_gban(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("Usage: <code>/gban @user</code>")
    _db("INSERT OR REPLACE INTO gbans(user_id,reason) VALUES(?,?)", (uid, "gban"))
    await msg.reply("🌐 Globally banned <b>%s</b>." % esc(name or str(uid)))

async def cmd_ungban(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    uid, name = await _target_user(client, msg)
    if not uid:
        return await msg.reply("Usage: <code>/ungban @user</code>")
    _db("DELETE FROM gbans WHERE user_id=?", (uid,))
    await msg.reply("🌐 Global unban done for <b>%s</b>." % esc(name or str(uid)))

async def cmd_broadcast(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    text = msg.text.partition(" ")[2].strip() if " " in msg.text else ""
    if not text and not (msg.reply_to_message and msg.reply_to_message.text):
        return await msg.reply("📣 Usage: <code>/broadcast &lt;text&gt;</code>")
    body = text or msg.reply_to_message.text
    chats = _db("SELECT id FROM chats", (), "all")
    ok = fail = 0
    st = await msg.reply("📣 Broadcasting to %d chats…" % len(chats))
    for (cid,) in chats:
        try:
            await client.send_message(cid, "📣 <b>Announcement</b>\n\n%s" % esc(body[:900]))
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await st.edit("📣 Done! ✅ %d  ❌ %d" % (ok, fail))

async def cmd_activevc(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    if not VC.active:
        return await msg.reply("🎙️ No live voice chats right now.")
    lines = []
    for cid, a in VC.active.items():
        t = QUEUES.current(cid)
        lines.append("• <code>%d</code> — %s %s" % (
            cid, esc((t or {}).get("title", "?")[:40]),
            "⏸️" if a.get("paused") else "▶️"))
    await msg.reply("🎙️ <b>Live VCs</b> (%d)\n\n%s" % (len(VC.active), "\n".join(lines)))

async def cmd_admincache(client, msg: Message):
    if not await is_bot_admin(client, msg.chat.id, msg.from_user.id):
        return await msg.reply("🛡️ Admins only.")
    _admin_cache.pop(msg.chat.id, None)
    await msg.reply("🔄 Admin cache refreshed. %s" % pick(POOL_OK))

# ---- 45/46. userbot link (DM only) ----
_link_wait = set()

async def cmd_linkuserbot(client, msg: Message):
    if msg.chat.type != ChatType.PRIVATE:
        return await msg.reply("🔗 Run /linkuserbot in my **DM**, not in groups.")
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    _link_wait.add(msg.from_user.id)
    await msg.reply(
        "🔗 <b>Link userbot</b>\n\n"
        "Send me your Pyrogram <code>session string</code> now.\n"
        "⚠️ It will be deleted immediately after reading.\n"
        "Get one via @SessionStringGeneratorBot or:\n"
        "<code>python3 -m pyrogram</code> → print session string.\n\n"
        "Send /cancel to abort.")

async def cmd_unlinkuserbot(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    kv_set("session_string", "")
    await msg.reply("🔗 Userbot unlinked. Restart me to fully drop it. %s" % pick(POOL_OK))

async def cmd_cancel(client, msg: Message):
    _link_wait.discard(msg.from_user.id)
    await msg.reply("🚫 Cancelled.")

async def _maybe_session(client, msg: Message):
    """Intercepts session string in DM while waiting."""
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id not in _link_wait:
        return False
    if msg.text and msg.text.startswith("/"):
        return False
    sess = (msg.text or "").strip()
    if len(sess) < 100:
        return False
    _link_wait.discard(msg.from_user.id)
    try:
        await msg.delete()
    except Exception:
        pass
    kv_set("session_string", sess)
    await msg.reply("✅ Session saved! **Restart me now** — the userbot will come online. %s" % pick(POOL_OK))
    return True

# ---- 47-50. 18+ zone ----
async def _need_18(uid, msg):
    u = get_user(uid)
    if u["age_ok"]:
        return True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I am 18+", callback_data="a:agree"),
         InlineKeyboardButton("🔙 Back", callback_data="pl:close")]])
    await msg.reply(
        "🔞 <b>Adults only (18+)</b>\n\n"
        "This zone may contain adult content and is strictly for 18+.\n"
        "Music, movies and everything else stay open for everyone.\n\n"
        "Tap below to confirm your age 👇", reply_markup=kb)
    return False

async def cmd_adult(client, msg: Message):
    if not await _need_18(msg.from_user.id, msg):
        return
    srcs = _db("SELECT id,name FROM adult_sources", (), "all")
    kb = []
    if srcs:
        for i, n in srcs:
            kb.append([InlineKeyboardButton("🔞 %s" % n[:28], callback_data="a:src:%d" % i)])
    kb.append([InlineKeyboardButton("🔍 Search 18+", callback_data="a:search"),
               InlineKeyboardButton("🔒 Lock", callback_data="a:lock")])
    kb.append([InlineKeyboardButton("❌", callback_data="pl:close")])
    await msg.reply("🔞 <b>18+ zone</b> — private, adults only.\nPick a source or search 👇",
                    reply_markup=InlineKeyboardMarkup(kb))

async def cmd_adultadd(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    parts = msg.text.split(None, 2)
    if len(parts) < 3:
        return await msg.reply("➕ Usage:\n<code>/adultadd &lt;name&gt; &lt;search-url-with-{q}&gt;</code>\n"
                               "Example: <code>/adultadd SiteX https://sitex.example/search?q={q}</code>")
    _db("INSERT INTO adult_sources(name,url_tpl) VALUES(?,?)", (parts[1][:40], parts[2][:300]))
    await msg.reply("🔞 Source <b>%s</b> added. %s" % (esc(parts[1]), pick(POOL_OK)))

async def cmd_adultdel(client, msg: Message):
    if not await is_owner(msg.from_user.id):
        return await msg.reply("👑 Owner only.")
    if len(msg.command) < 2 or not msg.command[1].isdigit():
        return await msg.reply("🗑️ Usage: <code>/adultdel &lt;id&gt;</code> (see /adultlist)")
    _db("DELETE FROM adult_sources WHERE id=?", (int(msg.command[1]),))
    await msg.reply("🗑️ Source removed.")

async def cmd_adultlist(client, msg: Message):
    if not await _need_18(msg.from_user.id, msg):
        return
    srcs = _db("SELECT id,name FROM adult_sources", (), "all")
    if not srcs:
        return await msg.reply("🔞 No custom sources yet. Owner can /adultadd.")
    await msg.reply("🔞 <b>Adult sources</b>\n\n" + "\n".join(
        "• <code>%d</code> — %s" % (i, esc(n)) for i, n in srcs))

# ============================================================================
#  CALLBACK HANDLERS
# ============================================================================
async def _cq_admin(cq, cid):
    if not await is_bot_admin(bot, cid, cq.from_user.id):
        await cq.answer("🛡️ Admins only!", show_alert=True)
        return False
    return True

async def _refresh_player(cq, cid):
    cur = QUEUES.current(cid)
    try:
        await cq.message.edit_text(np_text(cur, cid), reply_markup=player_kb(cid))
    except Exception:
        pass

async def on_callback(client, cq: CallbackQuery):
    data = cq.data or ""
    cid = cq.message.chat.id if cq.message else None
    uid = cq.from_user.id

    # ---- player bar ----
    if data == "pl:pause":
        if not VC.is_active(cid):
            return await cq.answer("🔇 Nothing playing.")
        if VC.is_paused(cid):
            await VC.resume(cid)
        else:
            await VC.pause(cid)
        await _refresh_player(cq, cid)
        return await cq.answer("⏸️" if VC.is_paused(cid) else "▶️")

    if data == "pl:prev":
        t = QUEUES.prev(cid)
        if not t:
            return await cq.answer("⏮️ No previous track.")
        await play_track(client, cid, t, t.get("req_name", "?"))
        await _refresh_player(cq, cid)
        return await cq.answer("⏮️ Previous")

    if data == "pl:skip":
        nxt = await play_next(client, cid)
        if isinstance(nxt, str):
            return await cq.answer(nxt[:180], show_alert=True)
        await _refresh_player(cq, cid)
        return await cq.answer("⏭️ Skipped" if nxt else "Queue finished")

    if data == "pl:stop":
        if not await _cq_admin(cq, cid):
            return
        QUEUES.clear(cid)
        await VC.leave(cid)
        try:
            await cq.message.edit_text("⏹️ Stopped. %s" % pick(POOL_OK))
        except Exception:
            pass
        return await cq.answer("Stopped")

    if data == "pl:shuffle":
        ok = QUEUES.shuffle(cid)
        return await cq.answer("🔀 Shuffled!" if ok else "Need 2+ tracks.")

    if data == "pl:loop":
        cur = (QUEUES.get_loop(cid) + 1) % 3
        QUEUES.set_loop(cid, cur)
        await _refresh_player(cq, cid)
        return await cq.answer("Loop: %s" % ["Off", "Track", "Queue"][cur])

    if data == "pl:vol":
        try:
            await cq.message.edit_reply_markup(player_kb(cid, "vol"))
        except Exception:
            pass
        return await cq.answer("🔊 Volume: %d%%" % VC.volume.get(cid, 100))

    if data in ("pl:voldn", "pl:volup"):
        v = VC.volume.get(cid, 100) + (-10 if data == "pl:voldn" else 10)
        v = await VC.set_volume(cid, v)
        try:
            await cq.message.edit_reply_markup(player_kb(cid, "vol"))
        except Exception:
            pass
        return await cq.answer("🔊 %d%%" % v)

    if data.startswith("pl:volset:"):
        try:
            v = await VC.set_volume(cid, int(data.split(":")[2]))
        except Exception:
            v = VC.volume.get(cid, 100)
        try:
            await cq.message.edit_reply_markup(player_kb(cid, "vol"))
        except Exception:
            pass
        return await cq.answer("🔊 %d%%" % v)

    if data == "pl:main":
        try:
            await cq.message.edit_reply_markup(player_kb(cid, "main"))
        except Exception:
            pass
        return await cq.answer()

    if data == "pl:queue":
        txt, kb = queue_text(cid)
        try:
            await cq.message.edit_text(txt, reply_markup=kb)
        except Exception:
            pass
        return await cq.answer()

    if data == "pl:back":
        _SEEK[cid] = max(0, _SEEK.get(cid, 0) - 10)
        return await cq.answer("⏪ -10s")

    if data == "pl:fwd":
        _SEEK[cid] = _SEEK.get(cid, 0) + 10
        return await cq.answer("+10s ⏩")

    if data == "pl:info":
        cur = QUEUES.current(cid)
        if not cur:
            return await cq.answer("Nothing playing.")
        return await cq.answer(
            "%s\n%s | %s" % (cur.get("title", "?")[:60], cur.get("uploader", "?"),
                             cur.get("duration", "?")), show_alert=True)

    if data == "pl:lyrics":
        cur = QUEUES.current(cid)
        if not cur:
            return await cq.answer("Nothing playing.")
        await cq.answer("📝 Fetching…")
        lyr = await asyncio.to_thread(fetch_lyrics, cur.get("uploader", ""), cur.get("title", ""))
        try:
            await cq.message.reply("📝 <b>%s</b>\n\n%s" % (
                esc(cur.get("title", "")[:60]), esc(lyr[:3000] if lyr else "Not found 😕")))
        except Exception:
            pass
        return

    if data == "pl:dl":
        cur = QUEUES.current(cid)
        if not cur:
            return await cq.answer("Nothing playing.")
        await cq.answer("⬇️ Preparing MP3…")
        try:
            path, _ = await asyncio.to_thread(yt_download_audio, cur["id"], "192k")
            await client.send_audio(cid, path, title=cur.get("title", "")[:60],
                                   performer=cur.get("uploader", "")[:40],
                                   caption="🎵 %s" % cur.get("title", "")[:100])
            try:
                os.remove(path)
            except Exception:
                pass
        except Exception as e:
            await cq.answer("⚠️ %s" % str(e)[:100], show_alert=True)
        return

    if data == "pl:close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        return await cq.answer()

    # ---- queue panel ----
    if data.startswith("q:page:"):
        try:
            txt, kb = queue_text(cid, int(data.split(":")[2]))
            await cq.message.edit_text(txt, reply_markup=kb)
        except Exception:
            pass
        return await cq.answer()

    if data == "q:clear":
        if not await _cq_admin(cq, cid):
            return
        QUEUES.clear(cid)
        try:
            await cq.message.edit_text("🧹 Queue cleared.")
        except Exception:
            pass
        return await cq.answer("Cleared")

    if data.startswith("q:play:"):
        try:
            idx = int(data.split(":")[2])
        except Exception:
            return await cq.answer("Bad index.")
        q, _ = QUEUES.list(cid)
        if 0 <= idx < len(q):
            QUEUES.set_pos(cid, idx)
            t = q[idx]
            await play_track(client, cid, t, t.get("req_name", "?"))
            await _refresh_player(cq, cid)
            return await cq.answer("▶️ %s" % t.get("title", "")[:40])
        return await cq.answer("No such track.")

    # ---- search result -> queue+play ----
    if data.startswith("s:play:"):
        vid = data.split(":", 2)[2]
        track = None
        for lst in _SEARCH_CACHE.values():
            for r in lst:
                if r["id"] == vid:
                    track = dict(r)
                    break
        if not track:
            track = {"id": vid, "title": vid, "uploader": "", "duration": ""}
        track["req_name"] = _name(cq.from_user)
        track["req_id"] = uid
        pos = QUEUES.add(cid, track)
        await cq.answer("➕ Queued #%d" % pos)
        if not VC.is_active(cid):
            QUEUES.set_pos(cid, pos - 1)
            await play_next(client, cid)
            cur = QUEUES.current(cid)
            if cur:
                try:
                    await client.send_message(cid, np_text(cur, cid),
                                              reply_markup=player_kb(cid))
                except Exception:
                    pass
        return

    # ---- settings ----
    if data == "s:quality":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("128k", callback_data="s:q:128k"),
             InlineKeyboardButton("192k", callback_data="s:q:192k"),
             InlineKeyboardButton("320k", callback_data="s:q:320k")],
            [InlineKeyboardButton("◀️ Back", callback_data="m:settings")]])
        try:
            await cq.message.edit_text("🎧 <b>VC audio quality</b>\nPick one 👇", reply_markup=kb)
        except Exception:
            pass
        return await cq.answer()

    if data.startswith("s:q:"):
        if not await _cq_admin(cq, cid):
            return
        q = data.split(":")[2]
        if q in ("128k", "192k", "320k"):
            _db("UPDATE chats SET quality=? WHERE id=?", (q, cid))
            await cq.answer("🎧 Quality → %s" % q)
        try:
            await cq.message.edit_text("⚙️ <b>Group settings</b>", reply_markup=settings_kb(cid))
        except Exception:
            pass
        return

    if data == "s:playmode":
        if not await _cq_admin(cq, cid):
            return
        c = get_chat(cid)
        c["playmode"] = 1 - c["playmode"]
        _db("UPDATE chats SET playmode=? WHERE id=?", (c["playmode"], cid))
        try:
            await cq.message.edit_reply_markup(settings_kb(cid))
        except Exception:
            pass
        return await cq.answer("Play mode: %s" % ("Direct ⚡" if c["playmode"] else "Download ⬇️"))

    if data == "s:vcmode":
        if not await _cq_admin(cq, cid):
            return
        c = get_chat(cid)
        c["vcmode"] = 1 - c["vcmode"]
        _db("UPDATE chats SET vcmode=? WHERE id=?", (c["vcmode"], cid))
        try:
            await cq.message.edit_reply_markup(settings_kb(cid))
        except Exception:
            pass
        return await cq.answer("Auto-join: %s" % ("ON" if c["vcmode"] else "OFF"))

    if data == "s:cleanmode":
        if not await _cq_admin(cq, cid):
            return
        c = get_chat(cid)
        c["cleanmode"] = 1 - c["cleanmode"]
        _db("UPDATE chats SET cleanmode=? WHERE id=?", (c["cleanmode"], cid))
        try:
            await cq.message.edit_reply_markup(settings_kb(cid))
        except Exception:
            pass
        return await cq.answer("Clean mode: %s" % ("ON" if c["cleanmode"] else "OFF"))

    if data == "s:lang":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🇬🇧 English", callback_data="s:lang:en"),
             InlineKeyboardButton("🇮🇳 हिन्दी", callback_data="s:lang:hi")],
            [InlineKeyboardButton("◀️ Back", callback_data="m:settings")]])
        try:
            await cq.message.edit_text("🌐 <b>Language</b>", reply_markup=kb)
        except Exception:
            pass
        return await cq.answer()

    if data in ("s:lang:en", "s:lang:hi"):
        if not await _cq_admin(cq, cid):
            return
        lang = data.split(":")[2]
        _db("UPDATE chats SET lang=? WHERE id=?", (lang, cid))
        try:
            await cq.message.edit_text("⚙️ <b>Group settings</b>", reply_markup=settings_kb(cid))
        except Exception:
            pass
        return await cq.answer("Language: %s" % lang.upper())

    # ---- help pages ----
    if data.startswith("h:page:"):
        try:
            p = int(data.split(":")[2]) % len(HELP_PAGES)
        except Exception:
            p = 0
        kb = []
        row = []
        if p > 0:
            row.append(InlineKeyboardButton("⬅️", callback_data="h:page:%d" % (p - 1)))
        if p < len(HELP_PAGES) - 1:
            row.append(InlineKeyboardButton("➡️", callback_data="h:page:%d" % (p + 1)))
        if row:
            kb.append(row)
        kb.append([InlineKeyboardButton("❌", callback_data="pl:close")])
        try:
            await cq.message.edit_text(
                "📖 <b>Help</b> — page %d/%d\n\n%s\n\n%s" % (
                    p + 1, len(HELP_PAGES), HELP_PAGES[p][0] + "\n" + HELP_PAGES[p][1],
                    "Use the buttons to flip pages 👇"),
                reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return await cq.answer()

    # ---- playlists ----
    if data.startswith("plp:play:"):
        try:
            pid = int(data.split(":")[2])
        except Exception:
            return await cq.answer("Bad id.")
        rows = _db("SELECT video_id,title,uploader,duration FROM playlist_tracks"
                   " WHERE pl_id=? ORDER BY pos", (pid,), "all")
        if not rows:
            return await cq.answer("Empty playlist.")
        tracks = [{"id": v, "title": t, "uploader": u, "duration": d,
                   "req_name": _name(cq.from_user), "req_id": uid} for v, t, u, d in rows]
        QUEUES.add_many(cid, tracks)
        await cq.answer("🎶 Queued %d tracks!" % len(tracks))
        if not VC.is_active(cid):
            await play_next(client, cid)
        return

    if data.startswith("plp:del:"):
        try:
            pid = int(data.split(":")[2])
        except Exception:
            return await cq.answer("Bad id.")
        _db("DELETE FROM playlist_tracks WHERE pl_id=?", (pid,))
        _db("DELETE FROM playlists WHERE id=? AND owner=?", (pid, uid))
        await cq.answer("🗑️ Deleted.")
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    # ---- 18+ ----
    if data == "a:agree":
        _db("INSERT OR REPLACE INTO users(id,age_ok) VALUES(?,1)", (uid,))
        await cq.answer("🔞 Welcome to the 18+ zone.", show_alert=True)
        try:
            await cq.message.delete()
        except Exception:
            pass
        # re-open adult zone as a fresh message
        fake = type("M", (), {})()
        fake.from_user = cq.from_user
        fake.chat = cq.message.chat
        fake.reply = cq.message.reply
        await cmd_adult(client, fake)
        return

    if data == "a:lock":
        _db("INSERT OR REPLACE INTO users(id,age_ok) VALUES(?,0)", (uid,))
        try:
            await cq.message.edit_text("🔒 18+ zone locked again.")
        except Exception:
            pass
        return await cq.answer("Locked 🔒")

    if data == "a:search":
        await cq.answer("Use /adultsearch <text> — coming right up!")
        return

    if data.startswith("a:src:"):
        try:
            sid = int(data.split(":")[2])
        except Exception:
            return await cq.answer("Bad source.")
        r = _db("SELECT name,url_tpl FROM adult_sources WHERE id=?", (sid,), "one")
        if not r:
            return await cq.answer("Source gone.")
        name, tpl = r
        url = tpl.replace("{q}", "18%2B")
        try:
            await cq.message.reply(
                "🔞 <b>%s</b>\n🔗 <a href=\"%s\">Open search</a>\n"
                "<i>18+ only — admin-configured source.</i>" % (esc(name), esc(url)),
                disable_web_page_preview=False)
        except Exception:
            pass
        return await cq.answer()

    # ---- start-menu shortcuts ----
    if data == "m:playhelp":
        return await cq.answer(
            "Add me to a group (admin), start a voice chat, then /play <song>. I'll join and sing! 🎵",
            show_alert=True)

    if data == "m:settings":
        if cid and not await is_bot_admin(client, cid, uid):
            return await cq.answer("🛡️ Admins only!", show_alert=True)
        try:
            await cq.message.edit_text("⚙️ <b>Group settings</b>\nTap to toggle 👇",
                                       reply_markup=settings_kb(cid))
        except Exception:
            pass
        return await cq.answer()

    if data == "m:adult":
        fake = type("M", (), {})()
        fake.from_user = cq.from_user
        fake.chat = cq.message.chat
        fake.reply = cq.message.reply
        await cmd_adult(client, fake)
        return await cq.answer()

    await cq.answer()

# ---- extra: /getmp3 /getmp4 (file to chat, like the Mini App) ----
async def cmd_getmp3(client, msg: Message):
    q = " ".join(msg.command[1:]) if len(msg.command) > 1 else ""
    if not q:
        return await msg.reply("⬇️ Usage: <code>/getmp3 &lt;song&gt;</code>")
    st = await msg.reply("🔎 Searching…")
    results = await asyncio.to_thread(yt_search, q, 3)
    if not results:
        return await st.edit("😕 Nothing found.")
    r = results[0]
    await st.edit("⬇️ Downloading <b>%s</b>…" % esc(r["title"][:50]))
    try:
        path, _ = await asyncio.to_thread(yt_download_audio, r["id"], "320k")
        if os.path.getsize(path) > 49 * 1024 * 1024:
            os.remove(path)
            return await st.edit("⚠️ Bigger than Telegram's 50MB limit — try /play instead.")
        await client.send_audio(msg.chat.id, path, title=r["title"][:60],
                                performer=r["uploader"][:40],
                                caption="🎵 %s" % r["title"][:100])
        try:
            os.remove(path)
        except Exception:
            pass
        await st.delete()
    except Exception as e:
        await st.edit("⚠️ Failed: %s" % str(e)[:120])

async def cmd_getmp4(client, msg: Message):
    q = " ".join(msg.command[1:]) if len(msg.command) > 1 else ""
    if not q:
        return await msg.reply("🎬 Usage: <code>/getmp4 &lt;video&gt;</code>")
    await msg.reply("🎬 Video files come via the Mini App / Studio for now — "
                    "tap 🎵 Open Music Studio. Meanwhile /play streams audio in VC! %s" % pick(POOL_MUSIC))

# ============================================================================
#  USERBOT: auto-join on VC start
# ============================================================================
async def on_vc_started(client, msg: Message):
    cid = msg.chat.id
    if not get_chat(cid)["vcmode"]:
        return
    if not VC.ready() or not user:
        return
    # just make sure userbot is present in chat; actual join happens on /play
    try:
        await user.join_chat(cid)
    except Exception:
        pass

async def on_stream_end(client, update):
    # pytgcalls stream-end -> advance queue automatically
    try:
        cid = update.chat_id
    except Exception:
        return
    try:
        nxt = await play_next(bot, cid, auto=True)
        if nxt and isinstance(nxt, dict):
            try:
                await bot.send_message(cid, "⏭️ Auto-next: <b>%s</b> %s" % (
                    esc(nxt["title"][:60]), pick(POOL_PLAY)),
                    reply_markup=player_kb(cid))
            except Exception:
                pass
    except Exception:
        pass

# ============================================================================
#  WIRING
# ============================================================================
def register_handlers():
    b = bot
    # -- core --
    b.on_message(filters.command("start") & filters.private)(cmd_start)
    b.on_message(filters.command("start") & filters.group)(cmd_start)
    b.on_message(filters.command(["help", "h"]))(cmd_help)
    # -- playback --
    b.on_message(filters.command(["play", "vplay"]))(cmd_play)
    b.on_message(filters.command("pause"))(cmd_pause)
    b.on_message(filters.command("resume"))(cmd_resume)
    b.on_message(filters.command("skip"))(cmd_skip)
    b.on_message(filters.command(["stop", "end"]))(cmd_stop)
    b.on_message(filters.command(["queue", "que"]))(cmd_queue)
    b.on_message(filters.command(["clearqueue", "clear"]))(cmd_clearqueue)
    b.on_message(filters.command("shuffle"))(cmd_shuffle)
    b.on_message(filters.command("loop"))(cmd_loop)
    b.on_message(filters.command("remove"))(cmd_remove)
    b.on_message(filters.command("move"))(cmd_move)
    b.on_message(filters.command("seek"))(cmd_seek)
    b.on_message(filters.command("seekback"))(cmd_seekback)
    b.on_message(filters.command("seekfwd"))(cmd_seekfwd)
    b.on_message(filters.command(["volume", "vol"]))(cmd_volume)
    b.on_message(filters.command(["join", "joinvc"]))(cmd_join)
    b.on_message(filters.command(["leave", "leavevc"]))(cmd_leave)
    b.on_message(filters.command(["songinfo", "nowplaying", "np"]))(cmd_songinfo)
    b.on_message(filters.command("lyrics"))(cmd_lyrics)
    b.on_message(filters.command("search"))(cmd_search)
    b.on_message(filters.command(["getmp3", "song"]))(cmd_getmp3)
    b.on_message(filters.command("getmp4"))(cmd_getmp4)
    # -- info --
    b.on_message(filters.command("ping"))(cmd_ping)
    b.on_message(filters.command("id"))(cmd_id)
    b.on_message(filters.command("info"))(cmd_info)
    b.on_message(filters.command("stats"))(cmd_stats)
    b.on_message(filters.command("sysinfo"))(cmd_sysinfo)
    # -- settings --
    b.on_message(filters.command("settings"))(cmd_settings)
    b.on_message(filters.command("playmode"))(cmd_playmode)
    b.on_message(filters.command(["quality", "aq"]))(cmd_quality)
    b.on_message(filters.command("vcmode"))(cmd_vcmode)
    b.on_message(filters.command("cleanmode"))(cmd_cleanmode)
    # -- auth --
    b.on_message(filters.command("auth"))(cmd_auth)
    b.on_message(filters.command("unauth"))(cmd_unauth)
    b.on_message(filters.command("authusers"))(cmd_authusers)
    # -- playlists --
    b.on_message(filters.command("mkplaylist"))(cmd_mkplaylist)
    b.on_message(filters.command("playlists"))(cmd_playlists)
    b.on_message(filters.command("pladd"))(cmd_pladd)
    b.on_message(filters.command("plplay"))(cmd_plplay)
    b.on_message(filters.command("pldel"))(cmd_pldel)
    b.on_message(filters.command(["plimport", "plimp"]))(cmd_plimport)
    # -- admin --
    b.on_message(filters.command("ban"))(cmd_ban)
    b.on_message(filters.command("unban"))(cmd_unban)
    b.on_message(filters.command("gban"))(cmd_gban)
    b.on_message(filters.command("ungban"))(cmd_ungban)
    b.on_message(filters.command("broadcast"))(cmd_broadcast)
    b.on_message(filters.command("activevc"))(cmd_activevc)
    b.on_message(filters.command("admincache"))(cmd_admincache)
    # -- userbot link (DM) --
    b.on_message(filters.command("linkuserbot") & filters.private)(cmd_linkuserbot)
    b.on_message(filters.command("unlinkuserbot") & filters.private)(cmd_unlinkuserbot)
    b.on_message(filters.command("cancel") & filters.private)(cmd_cancel)
    # -- 18+ --
    b.on_message(filters.command("adult"))(cmd_adult)
    b.on_message(filters.command("adultadd"))(cmd_adultadd)
    b.on_message(filters.command("adultdel"))(cmd_adultdel)
    b.on_message(filters.command("adultlist"))(cmd_adultlist)
    # -- session-string interceptor (must be registered, checked inside) --
    async def _sess_intercept(c, m: Message):
        if await _maybe_session(c, m):
            return
    b.on_message(filters.private & filters.text)(_sess_intercept)
    # -- callbacks --
    b.on_callback_query()(on_callback)
    # -- userbot: VC started service message --
    if user is not None:
        try:
            user.on_message(filters.video_chat_started & filters.group)(on_vc_started)
        except Exception:
            pass
    # -- pytgcalls stream end -> auto next --
    if VC.calls is not None:
        try:
            from pytgcalls.types.stream import StreamAudioEnded
            @VC.calls.on_stream_end()
            async def _ended(_, update):
                if isinstance(update, StreamAudioEnded):
                    await on_stream_end(_, update)
        except Exception:
            pass

# ============================================================================
#  MAIN
# ============================================================================
async def main():
    global bot, user
    init_db()
    if not HAVE_PYRO:
        raise SystemExit("pip install pyrogram  (pyrogram missing)")
    if not BOT_TOKEN or not API_ID or not API_HASH:
        raise SystemExit("Set BOT_TOKEN, API_ID, API_HASH env vars first.")
    if not ADMIN_IDS:
        print("WARNING: ADMIN_IDS empty - nobody is owner!")

    bot = Client("ytmusic-bot", api_id=API_ID, api_hash=API_HASH,
                 bot_token=BOT_TOKEN, parse_mode="html")

    sess = SESSION_STRING or kv_get("session_string")
    if sess and len(sess) > 100:
        try:
            user = Client("ytmusic-user", api_id=API_ID, api_hash=API_HASH,
                          session_string=sess)
            print("userbot session found - VC enabled if pytgcalls present")
        except Exception as e:
            print("userbot session invalid:", str(e)[:100])
            user = None
    else:
        print("no userbot session - link one with /linkuserbot (DM)")

    if HAVE_CALLS and user is not None:
        ok = VC.attach(user)
        print("pytgcalls attached:", ok)
    else:
        print("pytgcalls NOT available - /play will be disabled "
              "(pip install pytgcalls + ffmpeg on host)")

    register_handlers()
    await bot.start()
    me = await bot.get_me()
    print("SuperBot live as @%s" % me.username)
    if user is not None:
        await user.start()
        ume = await user.get_me()
        print("Userbot live as @%s" % (ume.username or ume.id))
        await VC.start()
    print("ready - press Ctrl+C to stop")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception:
        traceback.print_exc()
