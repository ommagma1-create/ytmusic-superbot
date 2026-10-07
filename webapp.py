#!/usr/bin/env python3
# ============================================================================
#  YTMUSIC Studio — Telegram Mini App backend (Flask)
# ============================================================================
#  Serves the Mini App page + JSON API:
#    GET  /                      -> Mini App (static/index.html)
#    POST /api/search            -> YouTube search (yt-dlp)
#    POST /api/download          -> start download job {video_id, quality}
#    GET  /api/job/<job_id>      -> job status / progress
#    GET  /api/cookies/status    -> is YouTube account linked?
#    POST /api/cookies           -> upload cookies.txt (link account)
#    DELETE /api/cookies         -> unlink account
#
#  ENV:
#    BOT_TOKEN   Telegram bot token (also used to validate WebApp initData)
#    APP_SECRET  random string for job ids (optional)
#    PORT        default 8080
#
#  Run:  python3 webapp.py
# ============================================================================
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from urllib.parse import parse_qsl

import requests
import yt_dlp
from flask import Flask, jsonify, request, send_from_directory

BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN") or ""
if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN env var first.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
COOKIE_DIR = os.path.join(DATA_DIR, "cookies")
DL_DIR = os.path.join(DATA_DIR, "downloads")
for d in (COOKIE_DIR, DL_DIR):
    os.makedirs(d, exist_ok=True)

TG_API = "https://api.telegram.org/bot%s" % BOT_TOKEN
MAX_TG_BYTES = 49 * 1024 * 1024  # stay under Bot API 50MB cap

app = Flask(__name__, static_folder="static", static_url_path="/static")

# job_id -> {status, progress, title, error, user_id, chat_id}
JOBS = {}
JOBS_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Telegram WebApp initData validation
# ----------------------------------------------------------------------------
def validate_init_data(init_data):
    """Returns the Telegram user dict, or None if invalid."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        if not recv_hash:
            return None
        check_str = "\n".join("%s=%s" % (k, pairs[k])
                              for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(),
                          hashlib.sha256).digest()
        calc = hmac.new(secret, check_str.encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        # auth_date freshness (1h) — initData older than this is replayable
        if abs(time.time() - int(pairs.get("auth_date", 0))) > 3600:
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


def authed_user():
    init_data = (request.form.get("initData")
                 or (request.get_json(silent=True) or {}).get("initData")
                 or request.args.get("initData") or "")
    user = validate_init_data(init_data)
    return user


def cookie_path(uid):
    p = os.path.join(COOKIE_DIR, "%d.txt" % int(uid))
    return p if os.path.exists(p) else None


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ----------------------------------------------------------------------------
# API: search
# ----------------------------------------------------------------------------
@app.route("/api/search", methods=["POST"])
def api_search():
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("q") or "").strip()[:120]
    if not query:
        return jsonify({"ok": False, "error": "empty query"}), 400
    try:
        opts = {"quiet": True, "no_warnings": True,
                "extract_flat": True, "default_search": "ytsearch",
                "extractor_args": {"youtube": {"player_client": ["android"]}}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("ytsearch25:%s" % query,
                                    download=False)
        out = []
        for e in (info.get("entries") or [])[:20]:
            if not e or not e.get("id"):
                continue
            vid = e["id"]
            out.append({
                "id": vid,
                "title": e.get("title") or "Untitled",
                "duration": e.get("duration_string")
                or _fmt_dur(e.get("duration")),
                "uploader": e.get("uploader") or e.get("channel") or "",
                "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
            })
        return jsonify({"ok": True, "results": out})
    except Exception as exc:
        return jsonify({"ok": False,
                        "error": str(exc)[:160]}), 500


def _fmt_dur(sec):
    try:
        sec = int(sec)
        return "%d:%02d" % (sec // 60, sec % 60)
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# API: cookies (link / status / unlink YouTube account)
# ----------------------------------------------------------------------------
@app.route("/api/cookies/status", methods=["GET"])
def api_cookie_status():
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    return jsonify({"ok": True,
                    "linked": cookie_path(user["id"]) is not None})


@app.route("/api/cookies", methods=["POST"])
def api_cookie_upload():
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    f = request.files.get("cookies")
    if not f:
        return jsonify({"ok": False,
                        "error": "no file"}), 400
    head = f.stream.read(2048).decode("utf-8", "replace")
    f.stream.seek(0)
    # basic sanity: Netscape cookie file
    if ("youtube.com" not in head.lower()
            and "# http" not in head.lower()
            and "netscape" not in head.lower()):
        return jsonify({"ok": False, "error":
                        "That doesn't look like a YouTube cookies.txt. "
                        "Export it with a 'Get cookies.txt' browser "
                        "extension while on youtube.com."}), 400
    path = os.path.join(COOKIE_DIR, "%d.txt" % int(user["id"]))
    f.save(path)
    os.chmod(path, 0o600)
    return jsonify({"ok": True, "linked": True})


@app.route("/api/cookies", methods=["DELETE"])
def api_cookie_delete():
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    p = os.path.join(COOKIE_DIR, "%d.txt" % int(user["id"]))
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
    return jsonify({"ok": True, "linked": False})


# ----------------------------------------------------------------------------
# API: download jobs
# ----------------------------------------------------------------------------
QUALITIES = {
    "mp3_128": ("mp3", "128"),
    "mp3_320": ("mp3", "320"),
    "mp4_720": ("mp4", None),
}


@app.route("/api/download", methods=["POST"])
def api_download():
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    data = request.get_json(force=True, silent=True) or {}
    video_id = (data.get("video_id") or "").strip()[:32]
    quality = data.get("quality") or "mp3_320"
    if not video_id or quality not in QUALITIES:
        return jsonify({"ok": False, "error": "bad request"}), 400
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0,
                        "title": "", "error": "",
                        "user_id": int(user["id"])}
    th = threading.Thread(target=_run_job,
                          args=(job_id, int(user["id"]), video_id,
                                quality),
                          daemon=True)
    th.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/job/<job_id>", methods=["GET"])
def api_job(job_id):
    user = authed_user()
    if not user:
        return jsonify({"ok": False, "error": "auth"}), 401
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
    if not job or job.get("user_id") != int(user["id"]):
        return jsonify({"ok": False, "error": "not found"}), 404
    job.pop("user_id", None)
    return jsonify({"ok": True, "job": job})


def _set_job(job_id, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def _run_job(job_id, uid, video_id, quality):
    _set_job(job_id, status="downloading", progress=1)
    kind, q = QUALITIES[quality]
    url = "https://www.youtube.com/watch?v=%s" % video_id
    ext = "mp3" if kind == "mp3" else "mp4"
    out = os.path.join(DL_DIR, "%s.%%(ext)s" % job_id)

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = int(done * 100 / total) if total else 0
            _set_job(job_id, progress=max(1, min(99, pct)))
        elif d.get("status") == "finished":
            _set_job(job_id, progress=100)

    opts = {
        "quiet": True, "no_warnings": True,
        "outtmpl": out,
        "progress_hooks": [hook],
        # android player client bypasses YouTube's "sign in to confirm
        # you're not a bot" wall for most public videos
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    ck = cookie_path(uid)
    if ck:
        opts["cookiefile"] = ck
    if kind == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": q,
        }]
    else:
        opts["format"] = ("bestvideo[height<=720]+bestaudio/best"
                          "[height<=720]/best")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = (info or {}).get("title") or video_id
        final = os.path.join(DL_DIR, "%s.%s" % (job_id, ext))
        if not os.path.exists(final):
            # find whatever was produced
            cands = [f for f in os.listdir(DL_DIR)
                     if f.startswith(job_id + ".")]
            final = os.path.join(DL_DIR, cands[0]) if cands else None
        if not final:
            raise RuntimeError("download produced no file")
        if os.path.getsize(final) > MAX_TG_BYTES:
            os.remove(final)
            raise RuntimeError("File is bigger than Telegram's 50MB "
                               "limit — try 128k.")
        _set_job(job_id, status="sending", title=title[:80])
        _send_to_user(uid, final, title, kind)
        _set_job(job_id, status="done", progress=100,
                 title=title[:80])
    except Exception as exc:
        _set_job(job_id, status="error",
                 error=str(exc)[:200])
    finally:
        # cleanup file + forget old jobs
        try:
            for f in os.listdir(DL_DIR):
                if f.startswith(job_id):
                    os.remove(os.path.join(DL_DIR, f))
        except Exception:
            pass


def _send_to_user(uid, path, title, kind):
    method = "sendAudio" if kind == "mp3" else "sendVideo"
    caption = ("🎵 %s" % title[:200]) if kind == "mp3" else ("🎬 %s" % title[:200])
    with open(path, "rb") as fh:
        files = {("audio" if kind == "mp3" else "video"):
                 (os.path.basename(path), fh)}
        r = requests.post(
            TG_API + "/" + method,
            data={"chat_id": uid, "caption": caption,
                  "title": title[:120] if kind == "mp3" else None,
                  "performer": "YouTube"},
            files=files, timeout=180)
    try:
        j = r.json()
    except Exception:
        j = {}
    if not j.get("ok"):
        raise RuntimeError("Telegram send failed: %s"
                           % str(j.get("description"))[:120])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
