FROM python:3.12-slim

# ffprobe lets the prober measure real resolution/fps/codecs instead of trusting playlist claims
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .

ENV KURDIPTV_CONFIG_DIR=/app/config \
    KURDIPTV_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir /data && chown app:app /data
VOLUME /data
USER app
EXPOSE 8787

# /health answers 503 until the first playlist exists, so give the first refresh time before judging
HEALTHCHECK --interval=60s --timeout=5s --start-period=300s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"

CMD ["kurdiptv", "serve", "--host", "0.0.0.0", "--port", "8787"]
