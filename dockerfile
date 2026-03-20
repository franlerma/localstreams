FROM python:3.11-slim

LABEL \
    com.centurylinklabs.watchtower.enable="false" \
    wud.watch="false" \
    org.opencontainers.image.authors="Fran Lerma" \
    org.opencontainers.image.title="LocalStreams" \
    org.opencontainers.image.description="FastAPI proxy for AceStream StreamLink and Brightcove" \
    org.opencontainers.image.version="2.0.0"

# Variables de entorno
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Crear usuario no-root
RUN groupadd -g 1001 localstreams && \
    useradd -u 1001 -g localstreams -m localstreams

# Instalar dependencias del sistema base
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
        curl \
        ca-certificates \
        # Dependencias para Playwright/Chromium
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libatspi2.0-0 \
        fonts-liberation \
        libappindicator3-1 \
        xdg-utils && \
    rm -rf /var/lib/apt/lists/*

# Crear directorios
RUN mkdir -p /app /data /ms-playwright && \
    chown -R localstreams:localstreams /app /data /ms-playwright

USER localstreams
WORKDIR /app

# Copiar requirements y plugins
COPY --chown=localstreams:localstreams app/requirements.txt /app/
COPY --chown=localstreams:localstreams resources/plugins /home/localstreams/.local/share/streamlink/plugins

# Instalar dependencias Python y Playwright (sin --with-deps porque ya instalamos las deps del sistema)
RUN pip install --user --no-cache-dir -r requirements.txt && \
    python -m playwright install chromium

# Copiar código de aplicación
COPY --chown=localstreams:localstreams app/ /app/
COPY --chown=localstreams:localstreams data/ /data/

EXPOSE 15123

ENV PATH="/home/localstreams/.local/bin:${PATH}"

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD wget -q -t1 -O- 'http://127.0.0.1:15123/check_health' | grep -q '{"healthy":true}' || exit 1

ENTRYPOINT ["python", "-u", "localstream.py"]
