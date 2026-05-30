FROM python:3.11-slim

LABEL \
    com.centurylinklabs.watchtower.enable="false" \
    wud.watch="false" \
    org.opencontainers.image.authors="Fran Lerma" \
    org.opencontainers.image.title="Acestream Hash Resolver" \
    org.opencontainers.image.description="Standalone AceStream hash resolution service" \
    org.opencontainers.image.version="1.0.0"

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Create non-root user
RUN groupadd -g 1001 resolver && \
    useradd -u 1001 -g resolver -m resolver

# Install system dependencies (ffmpeg needed for ffprobe/probing)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /app /data && \
    chown -R resolver:resolver /app /data

USER resolver
WORKDIR /app

# Copy and install Python dependencies
COPY --chown=resolver:resolver app/requirements.txt /app/
RUN pip install --user --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=resolver:resolver app/ /app/

EXPOSE 15124

ENV PATH="/home/resolver/.local/bin:${PATH}"

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget -q -t1 -O- 'http://127.0.0.1:15124/health' | grep -q '"status":"ok"' || exit 1

ENTRYPOINT ["python", "-u", "main.py"]
