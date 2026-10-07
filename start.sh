#!/bin/bash
# ============================================================================
#  YTMUSIC SuperBot — ONE command starts everything
#  (webapp.py [Mini App backend] + bot.py [the one bot])
# ============================================================================
#  Edit these ONCE, then just:  bash start.sh
# ============================================================================
export BOT_TOKEN="${BOT_TOKEN:-PASTE_YOUR_BOT_TOKEN}"
export API_ID="${API_ID:-PASTE_API_ID}"
export API_HASH="${API_HASH:-PASTE_API_HASH}"
export ADMIN_IDS="${ADMIN_IDS:-8156053366}"
export WEBAPP_URL="${WEBAPP_URL:-}"

if [ "$BOT_TOKEN" = "PASTE_YOUR_BOT_TOKEN" ]; then
  echo "!! BOT_TOKEN set karo pehle (file ke upar dekho) !!"
  exit 1
fi

cd "$(dirname "$0")"

echo "==> starting webapp (Mini App backend) on :8080 ..."
python3 webapp.py > webapp.log 2>&1 &
WEBAPP_PID=$!
sleep 3
if ! kill -0 $WEBAPP_PID 2>/dev/null; then
  echo "!! webapp failed to start - see webapp.log !!"
  exit 1
fi
echo "==> webapp running (PID $WEBAPP_PID)"

echo "==> starting the bot ..."
python3 bot.py
STATUS=$?

kill $WEBAPP_PID 2>/dev/null
echo "==> stopped (bot exit: $STATUS)"
