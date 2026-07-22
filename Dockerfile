FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg: Sicherheitsnetz fuer die Audio-Dekodierung (faster-whisper bringt
# ueber sein av-Abhaengigkeit i.d.R. schon eigene Decoder mit, aber ein
# System-ffmpeg zusaetzlich kostet nur ein paar MB und beseitigt jedes
# Format-Risiko fuer Telegrams OGG/Opus-Sprachnachrichten).
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/whisper-models \
    && chown -R appuser:appuser /app /data
USER appuser

VOLUME ["/data"]

EXPOSE 4567

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
port=os.environ.get('MCP_PORT','4567'); \
sys.exit(0) if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else sys.exit(1)"

CMD ["python", "-m", "app.server"]
