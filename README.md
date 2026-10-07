# 🎵 YTMUSIC SuperBot v2.0 — ONE bot, everything combined

**Ek hi bot, ek hi token, ek hi run command.** `musicbot.py` ab nahi hai —
delete kar do. Sab kuch `bot.py` me hai.

## Kya-kya hai isme

| Feature | Kahan |
|---|---|
| 🎵 Mini App (Studio) — `/start` → Open Music Studio button | `bot.py` + `webapp.py` + `static/index.html` |
| 🎙️ Group voice-chat music — userbot auto-join, `/play` | `bot.py` (Pyrogram + pytgcalls) |
| 📥 MP3/MP4 downloads Telegram me | Mini App + `/getmp3` |
| 🎶 Playlists, queue, lyrics, search | `bot.py` |
| 🛡️ Admin panel, auth, bans, broadcast, settings | `bot.py` |
| 🔞 18+ zone (gated) + admin adult-source connector | `bot.py` |

**107 interactive elements** — 70 commands + 37 inline-button actions.

## Run (Termux / VPS / PC)

```bash
unzip -o ~/downloads/ytmusic-superbot.zip -d ~
cd ~/ytmusic-superbot
pip install -r requirements.txt
```

`start.sh` ke upar apne values bharo (ek baar):
- `BOT_TOKEN`, `API_ID` + `API_HASH` (my.telegram.org se), `ADMIN_IDS`

Phir:

```bash
bash start.sh        # webapp (:8080) + bot, dono ek saath
```

**Session 2** me tunnel:
```bash
cloudflared tunnel --url http://localhost:8080
```

Tunnel URL milte hi `start.sh` me `WEBAPP_URL="https://xxxx.trycloudflare.com/"`
set karke dobara `bash start.sh` chalao — `/start` me Studio button aa jayega.

**Session 3 ki zaroorat nahi** — bot ab `start.sh` ke andar hi chalta hai. ✅

## Userbot link (voice chat ke liye)

1. Bot ke **DM** me `/linkuserbot` bhejo
2. Session string bhejo (auto-delete) — `@SessionStringGeneratorBot` se lo
3. `start.sh` dobara chalao (restart)

Phir group me: bot ko **admin** banao → voice chat start karo → `/play kesariya` 🎵

## Notes

- **Sirf `bot.py` chalao** — yehi ek bot hai.
- Voice chat ke liye host me `ffmpeg` + `pytgcalls` chahiye (Termux pe VC nahi jamega, Mini App chalega).
- Public YouTube videos ke liye cookies nahi chahiye; age-restricted ke liye Studio ke Account tab se `cookies.txt` lagao.
- Render deploy: `render.yaml` webapp ke liye ready hai (bot Render pe nahi, apne host pe chalao).
- `render_smoke_test.py` — deploy ke baad URL test karne ke liye.
- `keep_awake.py` — external always-on host ke liye.
