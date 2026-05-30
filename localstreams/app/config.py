import os
import logging

def get_env(key: str, default: str, type_cast: type = str):
    """Obtiene variable de entorno con validación de tipo"""
    value = os.getenv(key, default)
    try:
        return type_cast(value)
    except ValueError:
        logging.error(f"Valor inválido para {key}: {value}. Usando default.")
        return type_cast(default)

# Configuración de Acestream
ACEXY_LISTEN_ADDR = get_env("ACEXY_LISTEN_ADDR", ":8080", str)
ACESTREAM_PROXY_HOST = get_env("ACESTREAM_PROXY_HOST", "127.0.0.1", str)
ACESTREAM_PROXY_PORT = get_env("ACESTREAM_PROXY_PORT", ACEXY_LISTEN_ADDR.split(":")[1], int)
ACESTREAM_CACHE_LIMIT = get_env("ACESTREAM_CACHE_LIMIT", "1", str)
ACESTREAM_ARGS = get_env("ACESTREAM_ARGS", "", str)
ACESTREAM_RETRY_BACKOFF_FACTOR = get_env("ACESTREAM_RETRY_BACKOFF_FACTOR", "2.0", float)
ACESTREAM_RETRY_TOTAL = get_env("ACESTREAM_RETRY_TOTAL", "10", int)

# Configuración de Streamlink
STREAMLINK_CHUNKSIZE = get_env("STREAMLINK_CHUNKSIZE", "131072", int)
STREAMLINK_BINARY = get_env("STREAMLINK_BINARY", "streamlink", str)

# Configuración general de la app
APP_PORT = get_env("ACESTREAM_APP_PORT", "15123", int)
M3U_DIR = get_env("APP_M3U_DIR", "/data/m3u", str)
LOG_LEVEL = get_env("APP_LOG_LEVEL", "INFO", str)
MAX_CONNECTIONS = get_env("APP_MAX_CONNECTIONS", "100", int)

# Configuración del Acestream Hash Resolver (servicio externo)
RESOLVER_SERVICE_URL = get_env("RESOLVER_SERVICE_URL", "http://acestream-resolver:15124", str)
