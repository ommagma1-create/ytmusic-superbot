# YTMUSIC SuperBot — combined webapp + bot on Render (free tier OK)
FROM python:3.9-slim

# ffmpeg (yt-dlp MP3 conversion + pytgcalls) and deno (YouTube JS challenge)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# VC engine (best-effort): wheels na mile to build phir bhi pass, VC disabled
RUN pip install --no-cache-dir pytgcalls==2.1.0 || echo "pytgcalls skipped - VC disabled"
COPY . .
RUN chmod +x start.sh

# start.sh: webapp on $PORT (background) + bot.py (foreground)
CMD ["bash", "start.sh"]
