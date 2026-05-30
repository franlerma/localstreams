# LocalStreams 📺

LocalStreams is a FastAPI application designed to facilitate the generation and streaming of M3U playlists for TV channels. This project allows users to easily access a personalized playlist with television channels broadcasting over the internet, supporting multiple streaming protocols including AceStream, StreamLink, HLS, and Brightcove through a containerized microservices architecture. 🌐

## Main Features ✨

- **FastAPI** as main web framework for high performance
- **Real-time streaming** of AceStream and StreamLink content
- **Brightcove extractor** with automatic token renewal
- **HLS multiplexing** to combine separate video and audio streams
- **Dynamic M3U templates** with Jinja2 variable support
- **Modular architecture** with separate handlers for each streaming protocol
- **Multi-container architecture** with Docker Compose
- **Integrated health checks** for monitoring
- **Custom StreamLink plugins** (e.g., Mitele)
- **Security middleware** and robust error handling
- **Optimized cache management** for streaming
- **Channel icon support** (picons)
- **Structured JSON logging** for easier log analysis

## Architecture 🏗️

### Services

The project consists of four main services:

- **localstreams**: Main FastAPI application (port 15123)
- **acestream-resolver**: AceStream hash resolution microservice (port 15124)
- **acexy**: AceStream proxy (port 8080)
- **acestream**: AceStream HTTP engine (internal port 6878)

### Project Structure

```
localstreams/                    # Repository root
├── localstreams/                # Main FastAPI application
│   ├── app/
│   │   ├── localstream.py       # Main FastAPI application
│   │   ├── config.py            # Configuration and environment variables
│   │   ├── resolver_client/     # HTTP client for acestream-resolver
│   │   ├── handlers/            # Handlers for different endpoints
│   │   │   ├── __init__.py
│   │   │   ├── streamlink.py    # StreamLink streaming
│   │   │   ├── acestream.py     # AceStream streaming
│   │   │   ├── hls_mux.py       # HLS multiplexing (video+audio)
│   │   │   └── brightcove.py    # Generic Brightcove extractor
│   │   ├── utils/               # Utilities
│   │   │   ├── __init__.py
│   │   │   └── logging.py       # Logging configuration
│   │   └── requirements.txt     # Python dependencies
│   └── Dockerfile               # localstreams image
├── acestream-resolver/          # AceStream hash resolver microservice
│   ├── app/
│   │   ├── main.py              # FastAPI HTTP API
│   │   ├── config.py            # Resolver configuration
│   │   ├── resolver.py          # Resolution orchestrator
│   │   ├── cache.py             # In-memory TTL cache
│   │   ├── source.py            # External M3U source manager
│   │   ├── matcher.py           # Fuzzy channel name matcher
│   │   ├── prober.py            # Stream resolution & stability prober
│   │   └── requirements.txt     # Resolver Python dependencies
│   └── Dockerfile               # Resolver image
├── data/
│   └── m3u/                     # Example M3U templates
├── resources/
│   └── plugins/                 # Custom StreamLink plugins
├── docker-compose.yml           # Multi-service configuration
├── Makefile                     # Simplified commands
└── README.md                    # This file
```

## Installation ⚙️

### Prerequisites

- 🐳 Docker
- 🐙 Docker Compose
- 🛠️ Make (optional, for simplified commands)

### Step-by-step Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/franlerma/localstreams.git
   cd localstreams
   ```

2. **Create volume directories:**
   ```bash
   make volumes-create
   # Or manually:
   sudo mkdir -p /opt/docker/volumes/localstreams/{m3u,picon,tmp}
   ```

3. **Create your M3U playlists:**
   
   Create your playlist files in the M3U directory:
   ```bash
   # Example: create a basic playlist
   sudo nano /opt/docker/volumes/localstreams/m3u/channels.m3u
   ```
   
   See the [M3U Template Example](#m3u-template-example) section below for detailed instructions on creating playlists.

4. **Build the images:**
   ```bash
   make build              # localstreams
   make build-resolver     # acestream-resolver
   # Or directly with docker:
   docker build -t franlerma/localstreams localstreams/
   docker build -t franlerma/acestream-resolver acestream-resolver/
   ```

5. **Start services:**
   ```bash
   make up
   # Or with docker-compose:
   docker compose up -d
   ```

## Makefile Commands 🛠️

The project includes a Makefile to simplify common operations:

```bash
make help              # Show help
make build             # Build localstreams image
make build-resolver    # Build acestream-resolver image
make up                # Start all services
make down              # Stop all services
make restart           # Restart all services
make logs              # View localstreams logs
make logs-resolver     # View acestream-resolver logs
make status            # View service status
make shell             # Access localstreams container
make shell-resolver    # Access acestream-resolver container
make volumes-create    # Create volume directories
make clean             # Docker cleanup
make info              # System information
```

## Configuration 📋

### Main Environment Variables

#### localstreams (`localstreams/app/config.py`)

```bash
# FastAPI application port
ACESTREAM_APP_PORT=15123

# AceStream configuration
ACESTREAM_PROXY_HOST=acexy
ACESTREAM_PROXY_PORT=8080
ACESTREAM_RETRY_TOTAL=10
ACESTREAM_ARGS=--live-cache-type memory

# StreamLink
STREAMLINK_BINARY=streamlink
STREAMLINK_CHUNKSIZE=131072

# Directories
APP_M3U_DIR=/data/m3u
APP_LOG_LEVEL=INFO
APP_MAX_CONNECTIONS=100

# Acestream Hash Resolver (external service)
RESOLVER_SERVICE_URL=http://acestream-resolver:15124
```

#### acestream-resolver (`acestream-resolver/app/config.py`)

```bash
# Resolver service port
RESOLVER_PORT=15124

# Base URL of the localstreams app (used for probing)
LOCALSTREAMS_BASE_URL=http://localstreams:15123

# External M3U sources (comma-separated)
ACESTREAM_RESOLVER_SOURCES=https://...

# Refresh interval in seconds (default: 1800)
ACESTREAM_RESOLVER_REFRESH_INTERVAL=1800

# Timeouts
ACESTREAM_RESOLVER_DOWNLOAD_TIMEOUT=10
ACESTREAM_RESOLVER_PROBE_TIMEOUT=20
ACESTREAM_RESOLVER_TOTAL_TIMEOUT=30
ACESTREAM_RESOLVER_STABILITY_SAMPLE=5

# Resolution mode: 'probe' (ffprobe) or 'metadata' (heuristic)
ACESTREAM_RESOLVER_RESOLUTION_MODE=probe
```

### Volume Structure

```
/opt/docker/volumes/localstreams/
├── m3u/          # M3U playlist templates
├── picon/        # Channel icons
└── tmp/          # AceStream temporary cache
```

## API Endpoints 🔌

### 1. StreamLink (`/streamlink/video`)

Stream content using StreamLink.

**Parameters:**
- `url`: URL of the stream to play (required)
- `quality`: Stream quality (optional, default: `best`)

**Example:**
```
http://localhost:15123/streamlink/video?url=https://example.com/stream&quality=720p
```

### 2. AceStream (`/acestream/video`)

Stream content using AceStream protocol.

**Parameters:**
- `id`: AceStream ID (40 hexadecimal characters, required)
- `quality`: Stream quality (optional)

**Example:**
```
http://localhost:15123/acestream/video?id=b897de3e62d7c6bee9ef1107d972f3d1075e03ff
```

### 3. HLS Multiplexer (`/hls/mux`)

Combine separate HLS video and audio streams into a single stream using FFmpeg.

**Parameters:**
- `video`: M3U8 video stream URL (required)
- `audio`: M3U8 audio stream URL (required)

**Example:**
```
http://localhost:15123/hls/mux?video=https://cdn.com/video.m3u8&audio=https://cdn.com/audio.m3u8
```

**Features:**
- Uses FFmpeg with `-c copy` (no re-encoding, low CPU usage)
- Automatically combines video and audio streams
- Outputs MPEG-TS format compatible with most players

### 4. Brightcove Extractor (`/brightcove/extract`)

Automatically extract and stream content from any website using Brightcove player.

**Parameters:**
- `url`: URL of the webpage containing the Brightcove player (required)

**Features:**
- ✅ Automatically detects video and audio streams
- ✅ Supports multiple CDNs (Brightcove, Cloudfront, Fastly)
- ✅ Automatically renews tokens on each request
- ✅ Combines video and audio if separated
- ✅ Handles different stream configurations
- ✅ No manual URL maintenance required

**Examples:**
```
# Website with Brightcove player
http://localhost:15123/brightcove/extract?url=https://example.com/live

# Another channel
http://localhost:15123/brightcove/extract?url=https://channel.tv/directo
```

**TiviMate Usage:**
```m3u
#EXTINF:-1 tvg-name="Channel 1",Channel 1
http://192.168.1.100:15123/brightcove/extract?url=https://channel1.tv/directo

#EXTINF:-1 tvg-name="Channel 2",Channel 2
http://192.168.1.100:15123/brightcove/extract?url=https://channel2.tv/live
```

**Benefits:**
- **Zero maintenance**: No need to manually update URLs
- **Universal**: Works with any site using Brightcove
- **Automatic tokens**: Renewed on each access
- **Low consumption**: FFmpeg uses `-c copy` (no re-encoding)

### 5. Dynamic M3U (`/m3u/{filename}.m3u`)

Generate dynamic M3U playlists based on Jinja2 templates.

**Template Variables:**
- `{{scheme}}` - Protocol (http/https)
- `{{hostname}}` - Hostname
- `{{port}}` - Port
- `{{base_url}}` - Complete base URL
- Any parameter passed via query string

**Example:**
```
http://localhost:15123/m3u/playlist.m3u?custom_param=value
```

### 6. Picons (`/picon/{filename}`)

Serve channel icons.

**Example:**
```
http://localhost:15123/picon/channel_logo.png
```

### 7. Health Check (`/check_health`)

Verify the status of the AceStream service.

**Example:**
```
http://localhost:15123/check_health
```

## Usage 🖥️

### Application Access

Once services are started, the application will be available at:
- **Main port**: http://localhost:15123
- **Health check**: http://localhost:15123/check_health

### M3U Template Example

**File**: `/opt/docker/volumes/localstreams/m3u/example.m3u`

#### Available Macros

LocalStreams includes pre-defined Jinja2 macros in `app/templates/macros.j2` that are **automatically imported** into all M3U templates. These macros simplify URL generation:

**Available macros:**
- `streamlink(url)` - Generate StreamLink endpoint URL
- `acestream(id)` - Generate AceStream endpoint URL
- `brightcove(url)` - Generate Brightcove extractor endpoint URL
- `globalmest(url)` - Generate GlobalMEST extractor endpoint URL
- `acestream_resolve(name)` - Auto-resolve AceStream hash by channel name (see below)

**Resolución automática de canales AceStream:**

La macro `acestream_resolve()` permite buscar y resolver automáticamente el mejor hash de AceStream para un canal, consultando listas M3U externas:

```m3u
#EXTINF:-1 tvg-name="Movistar LaLiga", Movistar LaLiga
{{ acestream_resolve('Movistar LaLiga') }}
```

La resolución se delega al microservicio **acestream-resolver** (puerto 15124), que se ejecuta en su propio contenedor. La app principal realiza una llamada HTTP al resolver por cada renderizado de plantilla — latencia típica: ~1ms en Docker network local.

**Configuración del resolver (variables de entorno):**
- `ACESTREAM_RESOLVER_SOURCES` — URLs de listas M3U externas (separadas por coma)
- `ACESTREAM_RESOLVER_REFRESH_INTERVAL` — Segundos entre refrescos de caché (default: 1800)
- `ACESTREAM_RESOLVER_RESOLUTION_MODE` — `probe` (ffprobe) o `metadata` (tvg-attributes)
- Ver todas las variables en la sección [Environment Variables](#acestream-resolver-acestream-resolverappconfigpy)

**Comportamiento:**
- Los canales se resuelven en paralelo antes de renderizar la plantilla
- El sistema prueba múltiples candidatos y selecciona el de mayor resolución estable
- Los resultados se cachean en el resolver y se refrescan periódicamente
- Si ningún stream funciona, se devuelve el mejor candidato por metadatos
- **Tolerancia a fallos**: si el resolver está caído, la plantilla se renderiza sin hashes (canales sin resolver)

**Template variables available in macros:**
- `{{scheme}}` - Protocol (http/https)
- `{{hostname}}` - Server hostname
- `{{port}}` - Server port

#### Example without macros (verbose):

```m3u
#EXTM3U
#EXTVLCOPT--http-reconnect=true

#EXTINF:-1 tvg-logo="{{base_url}}/picon/la1.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
{{scheme}}://{{hostname}}:{{port}}/streamlink/video?url=https://www.rtve.es/play/videos/directo/canales-lineales/la-1/

#EXTINF:-1 tvg-logo="{{base_url}}/picon/telecinco.png" tvg-name="Telecinco" tvg-id="TELE5.es", Telecinco
{{scheme}}://{{hostname}}:{{port}}/acestream/video?id=b897de3e62d7c6bee9ef1107d972f3d1075e03ff

#EXTINF:-1 tvg-logo="{{base_url}}/picon/brightcove.png" tvg-name="Brightcove Channel" tvg-id="BC.es", Brightcove
{{scheme}}://{{hostname}}:{{port}}/brightcove/extract?url=https://example.com/live
```

#### Example with macros (recommended):

```m3u
#EXTM3U
#EXTVLCOPT--http-reconnect=true

#EXTINF:-1 tvg-logo="{{base_url}}/picon/la1.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
{{ streamlink('https://www.rtve.es/play/videos/directo/canales-lineales/la-1/') }}

#EXTINF:-1 tvg-logo="{{base_url}}/picon/telecinco.png" tvg-name="Telecinco" tvg-id="TELE5.es", Telecinco
{{ acestream('b897de3e62d7c6bee9ef1107d972f3d1075e03ff') }}

#EXTINF:-1 tvg-logo="{{base_url}}/picon/brightcove.png" tvg-name="Brightcove Channel" tvg-id="BC.es", Brightcove
{{ brightcove('https://example.com/live') }}

#EXTINF:-1 tvg-logo="{{base_url}}/picon/globalmest.png" tvg-name="7 Region de Murcia" tvg-id="7RM.es", 7RM
{{ globalmest('https://www.la7tv.es/video/a-la-carta/7tv-en-directo/20220810150726000939.html') }}

#EXTINF:-1 tvg-logo="{{base_url}}/picon/external.png" tvg-name="External Server" tvg-id="EXT.es", External Channel
http://{{iptvserver}}/stream.ts
```

#### Benefits of using macros:
- ✅ **Cleaner templates**: Less repetitive code
- ✅ **Easier maintenance**: Change URL structure in one place
- ✅ **No imports needed**: Macros are auto-imported in all `.m3u` files
- ✅ **Type safety**: Prevents typos in endpoint URLs

**Access with variables**:
```
http://localhost:15123/m3u/example.m3u?iptvserver=192.168.1.100:8080
```

#### Adding custom macros:

To add your own macros, edit `app/templates/macros.j2`:

```jinja2
{% macro my_custom_service(param) -%}
{{scheme}}://{{hostname}}:{{port}}/my_service/endpoint?param={{param}}
{%- endmacro %}
```

The macro will be automatically available in all M3U templates without any imports.

### StreamLink Plugins

The project supports custom StreamLink plugins located in `/resources/plugins/`. Currently includes:

- **mitele.py**: Plugin for Mediaset España (Mitele)

## Development 🔧

### Local Development

1. **Initial setup**:
   ```bash
   make dev-up
   ```

2. **View logs**:
   ```bash
   make logs
   ```

3. **Container access**:
   ```bash
   make shell
   ```

### Adding New Handlers

The modular architecture makes it easy to add new streaming protocols:

1. Create a new file in `app/handlers/new_handler.py`
2. Define a `router = APIRouter()`
3. Add endpoints with decorators `@router.get()`, etc.
4. Export in `app/handlers/__init__.py`
5. Register in `app/localstream.py` with `app.include_router()`

**Example:**

```python
# app/handlers/new_handler.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/new/endpoint")
async def my_endpoint():
    return {"status": "ok"}
```

```python
# app/handlers/__init__.py
from .new_handler import router as new_router
__all__ = [..., 'new_router']
```

```python
# app/localstream.py
from handlers import ..., new_router
app.include_router(new_router, tags=["new"])
```

### Logging

The logging system is centralized in `app/utils/logging.py`. All handlers use:

```python
logger = logging.getLogger("LocalStreams.module_name")
```

Structured JSON format for easier log analysis.

### Customization

- **Add plugins**: Place `.py` files in `resources/plugins/`
- **Modify templates**: Edit files in `/opt/docker/volumes/localstreams/m3u/`
- **Configure variables**: Modify `docker-compose.yml` or `app/config.py`

## Troubleshooting 🔍

### Common Issues

1. **Volume permission errors**:
   ```bash
   sudo chown -R 1001:1001 /opt/docker/volumes/localstreams/
   ```

2. **Port already in use**:
   ```bash
   make down
   # Change port in docker-compose.yml if necessary
   make up
   ```

3. **Network issues**:
   ```bash
   make logs
   # Verify connectivity between services
   ```

4. **AceStream cache full**:
   ```bash
   make volumes-clean
   ```

### Logs and Monitoring

```bash
make logs              # Real-time logs
make logs-tail         # Last 100 lines
make status            # Service status
```

## Dependencies 📦

See `app/requirements.txt` for the complete list. Main dependencies:

- FastAPI
- uvicorn
- aiohttp
- streamlink
- ffmpeg (system binary)

## Credits 🙏

- **AceXY Proxy**: Original project by [Javinator9889](https://github.com/Javinator9889/acexy)

## License 📜

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Contributing 🤝

Contributions are welcome. Please:

1. Fork the project
2. Create a branch for your feature
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

## Support 💬

If you encounter issues or have questions:
- Open an [issue](https://github.com/franlerma/localstreams/issues)
- Review the documentation and logs
- Verify Docker configuration and permissions
