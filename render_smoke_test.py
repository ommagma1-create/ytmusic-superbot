#!/usr/bin/env python3
"""
YTMUSIC Studio -- Render release-gate smoke test.

Usage:
    python3 render_smoke_test.py https://your-app.onrender.com
    # or:  RENDER_URL="https://your-app.onrender.com" python3 render_smoke_test.py

What it checks (no secrets needed, touches nothing on your account):
  1. GET  /                    -> 200, serves the Mini App page.
                                 Also reports whether the page carries the
                                 "v3.0" marker (i.e. the mega build, not the
                                 old folder, got deployed).
  2. POST /api/search          -> 401 {"error":"auth"} when called WITHOUT a
  3. GET  /api/cookies/status  -> Telegram-signed initData.

Checks 2-3 are *expected* 401s: they prove the API layer is up AND the auth
middleware is wired (an API that answers 200 to unauthenticated calls would
mean auth is broken open -- that is the thing we are guarding against).

Auth'd functional checks (real search, real download, cookies link) need a
genuine Telegram-signed initData, which only the Telegram client inside the
Mini App can produce. Those are tested by opening the Mini App and tapping
Search -- nothing an outside script can fake.

Note: Render's free plan sleeps after ~15 min idle, so the first request may
take up to a minute (cold start). The first check uses a generous timeout.

Exit code: 0 = gate passed, 1 = something needs a look.
"""

import json
import os
import sys
import urllib.request
import urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("RENDER_URL", "")).rstrip("/")
if not BASE.startswith("http"):
    print("Usage: python3 render_smoke_test.py https://your-app.onrender.com")
    sys.exit(2)

COLD_TIMEOUT = 90   # first request: Render may be waking up
WARM_TIMEOUT = 25   # everything after


def call(method, path, data=None, timeout=WARM_TIMEOUT):
    """Returns (http_status, body_text, content_type). Never raises."""
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), \
                r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, e.headers.get("Content-Type", "")
    except Exception as e:  # timeout, DNS, refused, ...
        return -1, "request failed: %s" % e, ""


results = []


def check(name, ok, detail):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name)
    if detail:
        print("       " + detail)


# --- 1. the page ------------------------------------------------------------
st, body, ctype = call("GET", "/", timeout=COLD_TIMEOUT)
if st == 200 and "YTMUSIC Studio" in body:
    detail = "HTTP 200, Mini App page served (%d bytes)" % len(body)
    if "v3.0" in body:
        detail += " -- v3.0 marker present, mega build deployed"
        check("1. page loads", True, detail)
    else:
        check("1. page loads", True,
              detail + " -- WARNING: no v3.0 marker; "
              "you may have deployed the OLD folder, not the mega build")
elif st == -1:
    check("1. page loads", False, body + " (is the Render URL right? "
          "is the service still deploying?)")
else:
    check("1. page loads", False,
          "HTTP %d, Content-Type: %s" % (st, ctype or "?"))

# --- 2. search without initData -> must be 401 auth ---------------------------
st, body, _ = call("POST", "/api/search", data={"q": "test song"})
if st == 401 and '"auth"' in body:
    check("2. /api/search auth-gated", True,
          "HTTP 401 'auth' as expected -- API is up, auth middleware wired")
elif st == -1:
    check("2. /api/search auth-gated", False, body)
else:
    check("2. /api/search auth-gated", False,
          "expected 401/auth, got HTTP %d: %s" % (st, body[:120]))

# --- 3. cookies status without initData -> must be 401 auth ------------------
st, body, _ = call("GET", "/api/cookies/status")
if st == 401 and '"auth"' in body:
    check("3. /api/cookies/status auth-gated", True,
          "HTTP 401 'auth' as expected")
elif st == -1:
    check("3. /api/cookies/status auth-gated", False, body)
else:
    check("3. /api/cookies/status auth-gated", False,
          "expected 401/auth, got HTTP %d: %s" % (st, body[:120]))

# --- summary ----------------------------------------------------------------
print()
passed = sum(1 for _, ok, _ in results if ok)
print("%d/%d checks passed" % (passed, len(results)))
if passed == len(results):
    print("Gate PASSED: page up, API up, auth correctly enforced.")
    print("Next: open the Mini App in Telegram and tap Search -- "
          "that runs the auth'd checks no outside script can fake.")
    sys.exit(0)
print("Gate needs a look: see FAIL lines above.")
sys.exit(1)
