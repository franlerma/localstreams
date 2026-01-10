# LocalStreams - Arquitectura Modular

## Estructura del Proyecto

```
app/
├── localstream.py          # Aplicación FastAPI principal
├── config.py               # Configuración y variables de entorno
├── handlers/               # Handlers para diferentes endpoints
│   ├── __init__.py
│   ├── streamlink.py       # Streaming con streamlink
│   ├── acestream.py        # Streaming con acestream
│   ├── hls_mux.py          # Multiplexado HLS (combinar video+audio)
│   └── brightcove.py       # Extractor genérico para sitios con Brightcove
├── utils/                  # Utilidades
│   ├── __init__.py
│   └── logging.py          # Configuración de logging
└── requirements.txt
```

## Endpoints Disponibles

### 1. Streamlink (`/streamlink/video`)
**Parámetros:**
- `url`: URL del stream a reproducir
- `quality`: Calidad del stream (opcional, default: `best`)

**Ejemplo:**
```
http://localhost:15123/streamlink/video?url=https://example.com/stream&quality=720p
```

### 2. Acestream (`/acestream/video`)
**Parámetros:**
- `id`: ID de Acestream (40 caracteres hexadecimales)
- `quality`: Calidad del stream (opcional)

**Ejemplo:**
```
http://localhost:15123/acestream/video?id=abc123...&quality=720p
```

### 3. HLS Mux (`/hls/mux`)
**Parámetros:**
- `video`: URL del stream M3U8 de video
- `audio`: URL del stream M3U8 de audio

**Descripción:** Combina dos streams HLS separados (video y audio) en uno solo usando FFmpeg.

**Ejemplo:**
```
http://localhost:15123/hls/mux?video=https://...video.m3u8&audio=https://...audio.m3u8
```

### 4. Brightcove Extractor (`/brightcove/extract`)
**Parámetros:**
- `url`: URL de la página web que contiene el reproductor Brightcove

**Descripción:** Extrae automáticamente streams de Brightcove desde cualquier página web. Compatible con múltiples sitios de streaming que usan la plataforma Brightcove.

**Características:**
- Detecta automáticamente streams de video y audio
- Soporta múltiples CDNs (Brightcove, Cloudfront, Fastly)
- Renueva tokens automáticamente en cada petición
- Combina video y audio si están separados
- Maneja diferentes configuraciones de stream

**Ejemplos:**
```
# Sitio web con reproductor Brightcove
http://localhost:15123/brightcove/extract?url=https://example.com/live

# Otro canal
http://localhost:15123/brightcove/extract?url=https://channel.tv/directo
```

**Uso en TiviMate:**
```m3u
#EXTINF:-1 tvg-name="Canal 1",Canal 1
http://192.168.1.100:15123/brightcove/extract?url=https://canal1.tv/directo

#EXTINF:-1 tvg-name="Canal 2",Canal 2
http://192.168.1.100:15123/brightcove/extract?url=https://canal2.tv/live
```

### 6. M3U Dinámico (`/m3u/{nombre}.m3u`)
**Descripción:** Genera listas M3U dinámicas basadas en templates Jinja2.

**Ejemplo:**
```
http://localhost:15123/m3u/playlist.m3u
```

### 7. Health Check (`/check_health`)
**Descripción:** Verifica el estado del servicio acestream.

## Uso en TiviMate

### Sitios con Brightcove

Para cualquier canal que use Brightcove, simplemente usa el endpoint `/brightcove/extract`:

```
http://TU_IP:15123/brightcove/extract?url=URL_DE_LA_PAGINA_CON_PLAYER
```

**Ejemplo de lista M3U:**
```m3u
#EXTM3U
#EXTINF:-1 tvg-id="" tvg-name="Canal TV",Canal TV
http://192.168.1.100:15123/brightcove/extract?url=https://canaltv.com/directo

#EXTINF:-1 tvg-id="" tvg-name="Otro Canal",Otro Canal
http://192.168.1.100:15123/brightcove/extract?url=https://otrocanal.tv/live
```

### Streams HLS manuales

Si ya tienes las URLs directas de video y audio:

```m3u
#EXTM3U
#EXTINF:-1 tvg-name="Mi Canal",Mi Canal
http://192.168.1.100:15123/hls/mux?video=https://cdn.com/video.m3u8&audio=https://cdn.com/audio.m3u8
```

### Ventajas del endpoint Brightcove

El servidor automáticamente:
1. ✅ Extrae las URLs actualizadas de la página web
2. ✅ Renueva los tokens automáticamente en cada petición
3. ✅ Combina los streams de video y audio si están separados
4. ✅ Sirve el stream unificado en formato compatible (MPEG-TS)
5. ✅ No requiere mantenimiento manual de URLs

**Beneficios:**
- Sin mantenimiento: No necesitas actualizar URLs manualmente
- Universal: Funciona con cualquier sitio que use Brightcove
- Tokens automáticos: Se renuevan en cada acceso
- Bajo consumo: FFmpeg usa `-c copy` (no recodifica)

## Configuración

Todas las variables de entorno están centralizadas en `config.py`:

```python
# Acestream
ACESTREAM_PROXY_HOST = "127.0.0.1"
ACESTREAM_PROXY_PORT = 8080

# Streamlink
STREAMLINK_BINARY = "streamlink"
STREAMLINK_CHUNKSIZE = 131072

# App
APP_PORT = 15123
M3U_DIR = "/data/m3u"
LOG_LEVEL = "INFO"
MAX_CONNECTIONS = 100
```

## Extensibilidad

Para añadir un nuevo handler:

1. Crear archivo en `handlers/nuevo_handler.py`
2. Definir un `router = APIRouter()`
3. Añadir endpoints con decoradores `@router.get()`, etc.
4. Exportar en `handlers/__init__.py`
5. Registrar en `localstream.py` con `app.include_router()`

**Ejemplo:**

```python
# handlers/nuevo_handler.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/nuevo/endpoint")
async def mi_endpoint():
    return {"status": "ok"}
```

```python
# handlers/__init__.py
from .nuevo_handler import router as nuevo_router
__all__ = [..., 'nuevo_router']
```

```python
# localstream.py
from handlers import ..., nuevo_router
app.include_router(nuevo_router, tags=["nuevo"])
```

## Logging

El sistema de logging está centralizado en `utils/logging.py`. Todos los handlers usan:

```python
logger = logging.getLogger("LocalStreams.nombre_modulo")
```

Formato JSON estructurado para facilitar el análisis de logs.

## Dependencias

Ver `requirements.txt` para la lista completa. Principales:

- FastAPI
- uvicorn
- aiohttp
- streamlink
- ffmpeg (binario del sistema)
