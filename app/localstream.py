import asyncio
import os
import re
import shutil
import signal
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
import aiohttp
from aiohttp import ClientSession, ClientTimeout

# Configuración de constantes con validación
def get_env(key: str, default: str, type_cast: type = str) -> str:
    value = os.getenv(key, default)
    try:
        return type_cast(value)
    except ValueError:
        logging.error(f"Valor inválido para {key}: {value}. Usando default.")
        return type_cast(default)

ACEXY_LISTEN_ADDR = get_env("ACEXY_LISTEN_ADDR", ":8080", str)

ACESTREAM_PROXY_HOST = get_env("ACESTREAM_PROXY_HOST", "127.0.0.1", str)
ACESTREAM_PROXY_PORT = get_env("ACESTREAM_PROXY_PORT", ACEXY_LISTEN_ADDR.split(":")[1], int)
ACESTREAM_CACHE_LIMIT = get_env("ACESTREAM_CACHE_LIMIT", "1", str)
ACESTREAM_ARGS = get_env("ACESTREAM_ARGS", "", str)
ACESTREAM_RETRY_BACKOFF_FACTOR = get_env("ACESTREAM_RETRY_BACKOFF_FACTOR", "2.0", float)
ACESTREAM_RETRY_TOTAL = get_env("ACESTREAM_RETRY_TOTAL", "10", int)
STREAMLINK_CHUNKSIZE = get_env("STREAMLINK_CHUNKSIZE", "131072", int)

# ACESTREAM_CACHE_DIR no es necesario ya que el cache es manejado por el contenedor
APP_PORT = get_env("ACESTREAM_APP_PORT", "15123", int)
STREAMLINK_BINARY = get_env("STREAMLINK_BINARY", "streamlink", str)

M3U_DIR = get_env("APP_M3U_DIR", "/data/m3u", str)
LOG_LEVEL = get_env("APP_LOG_LEVEL", "INFO", str)

MAX_CONNECTIONS = get_env("APP_MAX_CONNECTIONS", "100", int)

# Configuración de logging estructurado
logging.basicConfig(
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}',
    level=LOG_LEVEL
)
logger = logging.getLogger("LocalStreams")

# Cache para templates M3U
templates = Jinja2Templates(directory=M3U_DIR)
templates.env.cache = None

# Variable global para la sesión HTTP
http_session: Optional[ClientSession] = None

def print_available_playlists():
    """Imprime las URLs de acceso de todas las listas de reproducción disponibles"""
    try:
        m3u_path = Path(M3U_DIR)
        if not m3u_path.exists():
            logger.warning(f"Directorio M3U no encontrado: {M3U_DIR}")
            return
        
        m3u_files = list(m3u_path.glob("*.m3u"))
        
        if not m3u_files:
            logger.info("No se encontraron archivos .m3u en el directorio")
            return
        
        logger.info("=" * 60)
        logger.info("URLs de listas de reproducción disponibles:")
        logger.info("=" * 60)
        
        for m3u_file in sorted(m3u_files):
            playlist_name = m3u_file.stem
            url = f"http://127.0.0.1:{APP_PORT}/m3u/{playlist_name}.m3u"
            logger.info(f"  - {playlist_name}: {url}")
        
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Error al listar playlists: {str(e)}")

async def check_health() -> bool:
    """Verifica el estado del servicio acestream externo"""
    try:
        async with http_session.get(
            f"http://{ACESTREAM_PROXY_HOST}:{ACESTREAM_PROXY_PORT}/ace/status",
            timeout=ClientTimeout(total=3)
        ) as response:
            return response.status == 200
    except Exception as e:
        logger.debug(f"Error de salud del servicio acestream: {str(e)}")
        return False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_session
    
    # Configurar manejo de señales
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    # Inicializar sesión HTTP
    http_session = ClientSession(
        connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, ssl=False),
        timeout=ClientTimeout(total=30)
    )
    
    logger.info("Aplicación iniciada - conectando a acestream externo")
    
    # Imprimir URLs de playlists disponibles
    print_available_playlists()
    
    # Verificar disponibilidad del servicio acestream externo (opcional)
    try:
        if await check_health():
            logger.info("Servicio acestream externo está disponible")
        else:
            logger.warning("Servicio acestream externo no disponible al inicio")
    except Exception as e:
        logger.warning(f"No se pudo verificar el servicio acestream: {str(e)}")

    yield

    # Finalización ordenada
    await shutdown()

async def shutdown():
    global http_session
    logger.info("Iniciando apagado controlado...")

    # Cerrar sesión HTTP
    if http_session and not http_session.closed:
        await http_session.close()
        logger.info("Sesión HTTP cerrada")

    logger.info("Apagado completado correctamente")
    os._exit(0)

app = FastAPI(lifespan=lifespan)

# Middleware de seguridad mejorado
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as e:
        logger.error(f"Error en solicitud: {str(e)}")
        response = HTTPException(500, "Error interno del servidor")

    security_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'",
        #"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Cache-Control": "no-store, max-age=0",
        "Access-Control-Allow-Origin": "*"
    }

    if isinstance(response, StreamingResponse):
        response.headers.update(security_headers)
    elif not isinstance(response, HTTPException):
        response.headers.update(security_headers)

    return response

# Endpoints mejorados
@app.get("/m3u/{m3u_file}.m3u")
async def generate_m3u(request: Request, m3u_file: str):
    try:
        hostname = request.base_url.hostname
        port = request.base_url.port
        scheme = request.base_url.scheme

        port = port if port != None else "443" if scheme == "https" else "80"
        
        params = request.query_params
        base_url = f"{scheme}://{hostname}:{port}"
        args = {    
            "request": request, 
            "hostname": hostname, 
            "port": port, 
            "scheme": scheme,
            "base_url": base_url
        }
        args.update(params)

        return templates.TemplateResponse(f"{m3u_file}.m3u", args, media_type='text/plain')

    except Exception as e:
        logger.error(f"Error generando M3U: {str(e)}")
        raise HTTPException(404, "Archivo M3U no encontrado")

@app.get("/check_health")
async def health():
    healthy = await check_health()
    return JSONResponse({"healthy": healthy})

@app.get("/picon/{piconFileName}")
async def piconFile(piconFileName: str):
    filename = f"/data/picon/{piconFileName}"
    return FileResponse(filename, media_type='image/gif')

@app.get("/streamlink/video")
async def stream_video(request: Request):
    """Streaming mejorado con gestión de buffers y tiempo de espera"""
    from datetime import datetime
    import shutil

    url = request.query_params.get('url')
    if not url or not url.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL inválida")

    # Verificar que streamlink esté disponible
    streamlink_path = shutil.which(STREAMLINK_BINARY)
    if not streamlink_path:
        logger.error(f"Streamlink no encontrado en PATH: {STREAMLINK_BINARY}")
        raise HTTPException(500, "Streamlink no está disponible")

    start_time = datetime.now()
    logger.info(f"Iniciando stream para {url} usando {streamlink_path}")
    
    quality = request.query_params.get('quality')
    if quality and quality.endswith('p'):
        quality = quality[:-1]
    else:
        quality = 'best'

    # Comando con parámetros adicionales para mejor compatibilidad
    cmd = [
        streamlink_path,
        url,
        quality,
        '--stdout',
        '--retry-streams', '3',
        '--retry-max', '5',
        '--http-timeout', '60'
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        logger.info(f"Proceso streamlink iniciado con PID: {proc.pid}")
        
    except Exception as e:
        logger.error(f"Error al iniciar streamlink: {str(e)}")
        raise HTTPException(500, f"Error al iniciar streamlink: {str(e)}")

    async def stream_generator():
        error_output = ""
        chunks_sent = 0  # Inicializar aquí para evitar UnboundLocalError
        
        try:
            # Leer stderr en paralelo para capturar errores
            async def read_stderr():
                nonlocal error_output
                try:
                    stderr_data = await proc.stderr.read()
                    if stderr_data:
                        error_output = stderr_data.decode('utf-8', errors='ignore')
                        logger.warning(f"Streamlink stderr: {error_output}")
                except Exception as e:
                    logger.error(f"Error leyendo stderr: {str(e)}")

            # Iniciar lectura de stderr en background
            stderr_task = asyncio.create_task(read_stderr())
            
            # Esperar un poco para que streamlink se inicialice
            await asyncio.sleep(2)
            
            # Verificar si el proceso sigue vivo
            if proc.returncode is not None:
                await stderr_task
                logger.error(f"Streamlink terminó prematuramente con código: {proc.returncode}")
                logger.error(f"Error output: {error_output}")
                raise HTTPException(500, f"Streamlink falló: {error_output}")

            while not proc.stdout.at_eof():
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(STREAMLINK_CHUNKSIZE),
                        timeout=30.0
                    )
                    if chunk:
                        chunks_sent += 1
                        if chunks_sent % 100 == 0:  # Log cada 100 chunks
                            logger.debug(f"Enviados {chunks_sent} chunks")
                        yield chunk
                    else:
                        logger.info("Stream terminado - chunk vacío recibido")
                        break
                except asyncio.TimeoutError:
                    logger.error("Timeout leyendo del stream")
                    await asyncio.sleep(2)
                    break
                except Exception as e:
                    logger.error(f"Error leyendo chunk: {str(e)}")
                    break
                    
        except Exception as e:
            logger.error(f"Error en stream generator: {str(e)}")
            raise
        finally:
            # Cleanup del proceso
            if proc.returncode is None:
                try:
                    logger.info("Terminando proceso streamlink...")
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("Forzando terminación de streamlink...")
                        proc.kill()
                        await proc.wait()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.error(f"Error terminando proceso: {str(e)}")
            
            duration = datetime.now() - start_time
            logger.info(f"Stream finalizado - Duración: {duration}, Chunks enviados: {chunks_sent}")

    return StreamingResponse(
        stream_generator(),
        media_type='video/mp4',
        headers={
            "Connection": "keep-alive",
            'Cache-Control': 'no-store',
            'Accept-Ranges': 'none'
        }
    )

@app.get("/acestream/video")
async def ace_stream(request: Request):
    stream_id = request.query_params.get('id')

    # Validación del ID
    if not stream_id or len(stream_id) != 40 or not re.match(r"^[a-fA-F0-9]+$", stream_id):
        raise HTTPException(400, "ID de stream inválido")

    ace_url = f"http://{ACESTREAM_PROXY_HOST}:{ACESTREAM_PROXY_PORT}/ace/getstream?id={stream_id}"

    if request.query_params.get('quality') and request.query_params.get('quality') != 'best':
        ace_url += f"&quality={request.query_params.get('quality')[:-1]}"

    logger.info(f"Proxy concurrente para acestream ID: {stream_id}")
                    
    async def pure_proxy():
        """Proxy completamente transparente - copia exacta del comportamiento de acestream"""
        try:
            # Conexión directa sin timeouts artificiales
            async with http_session.get(
                ace_url,
                timeout=ClientTimeout(total=None, connect=30)
            ) as response:
                
                if response.status != 200:
                    raise HTTPException(504, f"Acestream devolvió: {response.status}")
                
                logger.info(f"Proxy transparente activo para {stream_id}")
                
                # Stream directo byte a byte sin modificaciones
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
                        
        except asyncio.CancelledError:
            logger.info("Stream cancelado por cliente")
            return
        except Exception as e:
            logger.error(f"Error en proxy transparente: {str(e)}")
            raise HTTPException(504, "Error de conexión con acestream")
    
    return StreamingResponse(
        pure_proxy(),
        media_type='video/mp4',
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

if __name__ == '__main__':
    import uvicorn

    uvicorn.run(
        app,
        host='0.0.0.0',
        port=APP_PORT,
        http='httptools',
        loop='uvloop',
        timeout_keep_alive=30,
        log_config=None
    )
