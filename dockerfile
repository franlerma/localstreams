# Dockerfile optimizado para localstreams
FROM python:3.11-alpine3.18

LABEL \
    com.centurylinklabs.watchtower.enable="false" \
    wud.watch="false" \
    org.opencontainers.image.authors="Fran Lerma" \
    org.opencontainers.image.title="LocalStreams" \
    org.opencontainers.image.description="FastAPI proxy for AceStream services" \
    org.opencontainers.image.version="1.0.0"

# Variables de entorno para optimización
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Crear usuario no-root para seguridad
RUN addgroup -g 1001 -S appgroup && \
    adduser -u 1001 -S appuser -G appgroup

# Instalar dependencias del sistema en una sola capa
RUN apk update && \
    apk add --no-cache \
        ffmpeg \
        wget \
        curl \
        ca-certificates \
        gcc \
        musl-dev \
        libffi-dev && \
    rm -rf /var/cache/apk/*

# Crear directorios con permisos correctos
RUN mkdir -p /app /data && \
    chown -R appuser:appgroup /app /data

# Cambiar a usuario no-root
USER appuser

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements primero para aprovechar cache de Docker
COPY --chown=appuser:appgroup app/requirements.txt /app/

# Instalar dependencias Python en el directorio del usuario
RUN pip install --user --no-cache-dir -r requirements.txt

# Copiar código de aplicación
COPY --chown=appuser:appgroup app/ /app/
COPY --chown=appuser:appgroup data/ /data/

# Exponer puerto
EXPOSE 15123

# Actualizar PATH para incluir binarios de usuario
ENV PATH="/home/appuser/.local/bin:$PATH"

# Health check mejorado
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD wget -q -t1 -O- 'http://127.0.0.1:15123/check_health' | grep -q '{"healthy":true}' || exit 1

# Punto de entrada optimizado
ENTRYPOINT ["python", "-u", "localstream.py"]
