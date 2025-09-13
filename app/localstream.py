import asyncio
import os
import re
import shutil
import signal
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional
from typing import AsyncGenerator
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
ACESTREAM_STREAM_CHUNKSIZE = get_env("ACESTREAM_STREAM_CHUNKSIZE", "8192", int)

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
                        proc.stdout.read(ACESTREAM_STREAM_CHUNKSIZE),
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
    from datetime import datetime
    
    stream_id = request.query_params.get('id')

    # Validación mejorada del ID
    if not stream_id or len(stream_id) != 40 or not re.match(r"^[a-fA-F0-9]+$", stream_id):
        raise HTTPException(400, "ID de stream inválido")

    ace_url = f"http://{ACESTREAM_PROXY_HOST}:{ACESTREAM_PROXY_PORT}/ace/getstream?id={stream_id}"

    if request.query_params.get('quality') and request.query_params.get('quality') != 'best':
        ace_url += f"&quality={request.query_params.get('quality')[:-1]}"

    start_time = datetime.now()
    logger.info(f"Iniciando stream acestream para ID: {stream_id}")

    async def wait_for_acestream_ready(url: str, max_wait: int = 20) -> bool:
        """Espera a que acestream esté listo para servir el stream con estrategia optimizada"""
        logger.info("Verificando disponibilidad de acestream...")
        
        # Fase 1: Verificación rápida de disponibilidad (primeros 5 segundos)
        for attempt in range(5):
            try:
                async with http_session.get(url, timeout=ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        logger.info(f"Acestream respondió OK después de {attempt + 1} segundos")
                        # En lugar de esperar datos, verificamos headers de contenido
                        content_type = response.headers.get('content-type', '')
                        if 'video' in content_type or 'octet-stream' in content_type:
                            logger.info("Acestream listo - tipo de contenido válido detectado")
                            return True
                        
                        # Si no hay headers de video, intentamos leer un pequeño chunk
                        try:
                            chunk = await asyncio.wait_for(
                                response.content.read(512), 
                                timeout=1.0
                            )
                            if chunk:
                                logger.info("Acestream listo - datos iniciales recibidos")
                                return True
                        except asyncio.TimeoutError:
                            logger.debug("Acestream responde pero sin datos aún")
                            
                    elif response.status == 404:
                        logger.debug(f"Stream no encontrado aún (intento {attempt + 1})")
                    elif response.status == 503:
                        logger.debug(f"Acestream ocupado (intento {attempt + 1})")
                    else:
                        logger.warning(f"Estado inesperado: {response.status}")
                        
            except asyncio.TimeoutError:
                logger.debug(f"Timeout en verificación rápida (intento {attempt + 1})")
            except Exception as e:
                logger.debug(f"Error en verificación rápida {attempt + 1}: {str(e)}")
                
            await asyncio.sleep(0.5)  # Intervalos más cortos en fase rápida
        
        # Fase 2: Verificación con intervalos más largos (siguientes 15 segundos)
        logger.info("Acestream no listo en verificación rápida, esperando inicialización...")
        
        for attempt in range(max_wait - 5):
            try:
                async with http_session.get(url, timeout=ClientTimeout(total=3)) as response:
                    if response.status == 200:
                        logger.info(f"Acestream listo después de {attempt + 6} segundos totales")
                        return True
                    elif response.status in [404, 503]:
                        logger.debug(f"Acestream aún preparando stream...")
                    else:
                        logger.warning(f"Estado inesperado de acestream: {response.status}")
                        
            except Exception as e:
                logger.debug(f"Verificación lenta {attempt + 1}: {str(e)}")
                
            await asyncio.sleep(1)
        
        logger.warning(f"Acestream no estuvo listo después de {max_wait} segundos, intentando conexión directa")
        return False  # Cambio: permitir intentar conexión aunque no esté "listo"
                    
    async def stream_content(acestream_url: str) -> AsyncGenerator[bytes, None]:
        max_retries = 3
        retry_count = 0
        chunks_sent = 0
        last_chunk_time = datetime.now()
        
        # Verificar acestream y proceder incluso si no está completamente listo
        is_ready = await wait_for_acestream_ready(acestream_url)
        if not is_ready:
            logger.info("Intentando conexión directa aunque acestream no parezca completamente listo")
        
        while retry_count < max_retries:
            try:
                logger.info(f"Conectando a acestream (intento {retry_count + 1}/{max_retries})")
                
                async with http_session.get(
                    acestream_url,
                    timeout=ClientTimeout(total=60, connect=10)
                ) as response:
                    if response.status != 200:
                        logger.error(f"Error HTTP {response.status} desde acestream")
                        retry_count += 1
                        if retry_count < max_retries:
                            await asyncio.sleep(min(retry_count * 2, 10))
                        continue
                    
                    logger.info("Conexión establecida con acestream, iniciando transmisión")
                    retry_count = 0  # Reset en conexión exitosa
                    
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                response.content.read(ACESTREAM_STREAM_CHUNKSIZE),
                                timeout=30.0
                            )
                            
                            if not chunk:
                                logger.info("Stream terminado - no más datos disponibles")
                                return
                                
                            chunks_sent += 1
                            last_chunk_time = datetime.now()
                            
                            # Log periódico del progreso
                            if chunks_sent % 200 == 0:
                                duration = datetime.now() - start_time
                                logger.info(f"Stream activo - {chunks_sent} chunks enviados, duración: {duration}")
                            
                            yield chunk
                            
                        except asyncio.TimeoutError:
                            time_since_last = datetime.now() - last_chunk_time
                            logger.warning(f"Timeout leyendo chunk - {time_since_last.seconds}s sin datos")
                            
                            # Si han pasado más de 60 segundos sin datos, reintentar
                            if time_since_last.seconds > 60:
                                logger.error("Stream parece estar muerto, reintentando conexión")
                                break
                            continue
                            
                        except Exception as e:
                            logger.error(f"Error leyendo chunk del stream: {str(e)}")
                            break

            except asyncio.CancelledError:
                logger.info("Stream cancelado por el cliente")
                return
            except Exception as e:
                logger.error(f"Error de conexión con acestream: {str(e)}")
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = min(retry_count * 2, 10)
                    logger.info(f"Reintentando en {wait_time} segundos...")
                    await asyncio.sleep(wait_time)
        
        # Si llegamos aquí, se agotaron los reintentos
        duration = datetime.now() - start_time
        logger.error(f"Stream falló después de {max_retries} intentos - Duración: {duration}, Chunks enviados: {chunks_sent}")
        raise HTTPException(504, "No se pudo establecer conexión estable con acestream")
    
    try:
        return StreamingResponse(
            stream_content(ace_url),
            media_type='video/mp4',
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Accept-Ranges": "none"
            }
        )
    except Exception as e:
        logger.error(f"Error en stream Acestream: {str(e)}")
        raise HTTPException(504, "Error en la conexión del stream")

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
