# repryntt - Autonomous AI Agent Framework

FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    REPRYNTT_DATA_DIR=/data \
    REPRYNTT_PROVIDER=local

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libasound2 \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/sh repryntt

WORKDIR /app

COPY pyproject.toml README.md ./
COPY config ./config
COPY repryntt ./repryntt
COPY docker/entrypoint.sh /usr/local/bin/repryntt-docker-entrypoint

RUN chmod +x /usr/local/bin/repryntt-docker-entrypoint \
    && pip install -e . \
    && mkdir -p /data /app/logs \
    && chown -R repryntt:repryntt /data /app

USER repryntt

EXPOSE 5000 8089

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8089/health || exit 1

ENTRYPOINT ["repryntt-docker-entrypoint"]
CMD ["repryntt", "start", "--no-llm", "--no-evolution", "--no-blockchain"]
