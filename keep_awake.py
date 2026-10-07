#!/usr/bin/env python3
"""
keep_awake.py — keep a free Render web service from sleeping.

Render's free plan puts a web service to sleep after ~15 minutes with no
traffic, so the first Telegram Mini App open after idle is slow. This loop
pings your public URL every few minutes so the app stays warm.

Run it on a machine that is always on (e.g. your own host) — NOT on Render:

    RENDER_URL="https://ytmusic-studio.onrender.com" nohup python3 keep_awake.py &

Env vars:
    RENDER_URL     public https URL of the Render service (required)
    PING_INTERVAL  seconds between pings (default 300 = 5 min; keep >= 60)

Stdlib only — no pip install needed.
"""

import datetime
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("RENDER_URL", "").rstrip("/")
INTERVAL = int(os.environ.get("PING_INTERVAL", "300") or 300)


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[keep_awake %s] %s" % (ts, msg), flush=True)


def ping(target):
    """Return the HTTP status, or None on a network failure."""
    req = urllib.request.Request(
        target, headers={"User-Agent": "ytmusic-keepalive/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code  # any response at all proves the app is awake
    except Exception as exc:  # network blip — log it and keep looping
        log("ping failed: %s" % exc)
        return None


def main():
    if not URL:
        sys.exit("Set RENDER_URL first, e.g.\n"
                 "  RENDER_URL=https://your-app.onrender.com python3 keep_awake.py")
    if INTERVAL < 60:
        sys.exit("PING_INTERVAL too small — keep it at 60s or more to be polite.")
    log("keeping %s awake, ping every %ds" % (URL, INTERVAL))
    failures = 0
    while True:
        status = ping(URL + "/")
        if status is None:
            failures += 1
        else:
            if failures:
                log("awake again (HTTP %s) after %d failed ping(s)"
                    % (status, failures))
            failures = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
