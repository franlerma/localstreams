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

ACESTREAM_CACHE_DIR = get_env("ACESTREAM_CACHE_DIR", "/tmp/acestream-cache", str)
APP_PORT = get_env("ACESTREAM_APP_PORT", "15123", int)
STREAMLINK_BINARY = get_env("ACESTREAM_STREAMLINK_BINARY", "/app/venv/bin/streamlink", str)

M3U_DIR = get_env("APP_M3U_DIR", "/data/m3u", str)
LOG_LEVEL = get_env("APP_LOG_LEVEL", "INFO", str)

MAX_CONNECTIONS = get_env("APP_MAX_CONNECTIONS", "100", int)

# Configuración de logging estructurado
logging.basicConfig(
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}',
    level=LOG_LEVEL
)
logger = logging.getLogger("AceStreamManager")

# Cache para templates M3U
templates = Jinja2Templates(directory=M3U_DIR)
templates.env.cache = None

class AceStreamManager:
    def __init__(self):
        self.app: Optional[FastAPI] = None
        self.http_session: Optional[ClientSession] = None
        self.acestream_process: Optional[asyncio.subprocess.Process] = None
        self.acexy_process: Optional[asyncio.subprocess.Process] = None

    async def start_acestream(self):
        if ACESTREAM_PROXY_HOST != "127.0.0.1":
            self.acestream_process = Optional.empty()
            return None
        else :
            await self.__start_acestream()

    async def __start_acestream(self):
        await self.clean_cache()
        
        # command = [
        #     ACESTREAM_BINARY,
        #     "--use-ffmpeg=1",
        #     "--client-console",
        #     "--port", f"{ACESTREAM_ENGINE_PORT}",
        #     "--http-port", f"{ACESTREAM_PROXY_PORT}",
        #     "--cache-dir", ACESTREAM_CACHE_DIR,
        #     "--bind-all",
        #     ACESTREAM_ARGS
        # ]

        command_ace_engine = [ "/bin/bash", "/run.sh", "&" ]
        command_acexy = [ "/acexy" ]

        try:
            self.acestream_process = await asyncio.create_subprocess_exec(*command_ace_engine)
            logger.info(f"Ace Engine iniciado con PID: {self.acestream_process.pid}")
            
            self.acexy_process = await asyncio.create_subprocess_exec(*command_acexy)
            logger.info(f"Acexy iniciado con PID: {self.acestream_process.pid}")
            
            await self.__wait_until_healthy()
        except Exception as e:
            logger.error(f"Error crítico al iniciar Acestream: {str(e)}")
            raise

    async def __wait_until_healthy(self, timeout: int = 30):
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if await self.check_health():
                return
            await asyncio.sleep(1)
        raise TimeoutError("El servicio no se inició correctamente")

    async def check_health(self) -> bool:
        try:
            async with self.http_session.get(
                f"http://{ACESTREAM_PROXY_HOST}:{ACESTREAM_PROXY_PORT}/ace/status",
                timeout=ClientTimeout(total=3)
            ) as response:
                return response.status == 200
                # if response.status != 200:
                #     return False
                
                # data = await response.json()
                # logger.debug(f"Respuesta de salud: {data}")

                # return data.get("error") is None
        except Exception as e:
            logger.debug(f"Error de salud: {str(e)}")
            return False

    async def clean_cache(self) -> None:
        if ACESTREAM_PROXY_HOST != "127.0.0.1":
            return
        if self.acestream_process and not self.acestream_process.isempty():
            return
        shutil.rmtree(ACESTREAM_CACHE_DIR, ignore_errors=True)

    async def restart_service(self):
        if self.acestream_process and not self.acestream_process.isempty():
            return
        await self.__restart_service()

    async def __restart_service(self):
        logger.info("Iniciando reinicio del servicio...")
        if self.acestream_process and self.acestream_process.returncode is None:
            try:
                self.acestream_process.terminate()
                await self.acestream_process.wait()
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

        if os.path.exists(ACESTREAM_CACHE_DIR):
            shutil.rmtree(ACESTREAM_CACHE_DIR, ignore_errors=True)
            os.makedirs(ACESTREAM_CACHE_DIR, exist_ok=True)

        # Iniciar nuevo proceso
        await self.__start_acestream()

manager = AceStreamManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configurar manejo de señales
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(app)))

    # Inicializar manejo de HTTP
    manager.http_session = ClientSession(
        connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, ssl=False),
        timeout=ClientTimeout(total=30)
    )
    manager.app = app
    app.state.manager = manager

    try:
        await manager.start_acestream()
    except Exception as e:
        logger.critical(f"No se pudo iniciar el servicio: {str(e)}")
        raise

    yield

    # Finalización ordenada
    await shutdown(app)

async def shutdown(app: FastAPI):
    logger.info("Iniciando apagado controlado...")

    # Detener proceso Acestream
    logger.info("Deteniendo proceso Acestream...")
    if manager.acestream_process and manager.acestream_process.returncode is None:
        try:
            manager.acestream_process.terminate()
            await manager.acestream_process.wait()
            logger.info("Proceso Acestream terminado")
        except ProcessLookupError:
            pass

    # Cerrar sesión HTTP
    logger.info("Cerrando sesión HTTP...")
    if manager.http_session and not manager.http_session.closed:
        await manager.http_session.close()
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
    healthy = await manager.check_health()
    return JSONResponse({"healthy": healthy})

@app.get("/picon/{piconFileName}")
async def piconFile(piconFileName: str):
    filename = f"/data/picon/{piconFileName}"
    return FileResponse(filename, media_type='image/gif')

@app.get("/streamlink/video")
async def stream_video(request: Request):
    """Streaming mejorado con gestión de buffers y tiempo de espera"""
    from datetime import datetime

    url = request.query_params.get('url')
    if not url or not url.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL inválida")

    start_time = datetime.now()
    logger.info(f"Iniciando stream para {url}")
    
    quality = request.query_params.get('quality')[:-1] if request.query_params.get('quality') else 'best'

    proc = await asyncio.create_subprocess_exec(
        STREAMLINK_BINARY, url, quality, '--stdout',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    async def stream_generator():
        try:
            while not proc.stdout.at_eof():
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(ACESTREAM_STREAM_CHUNKSIZE),
                        timeout=10.0
                    )
                    if chunk:
                        yield chunk
                    else:
                        logger.warning("Chunk vacío recibido")
                        break
                except asyncio.TimeoutError:
                    logger.error("Timeout leyendo del stream")
                    break
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            logger.info(f"Stream finalizado - Duración: {datetime.now() - start_time}")

    return StreamingResponse(
        stream_generator(),
        media_type='video/mp4',
        headers={
            "Connection": "keep-alive",
            'Cache-Control': 'no-store',
            'X-Stream-Duration': str(datetime.now() - start_time)
        }
    )

@app.get("/acestream/video")
async def ace_stream(request: Request):
    stream_id = request.query_params.get('id')

    # Validación mejorada del ID
    if not stream_id or len(stream_id) != 40 or not re.match(r"^[a-fA-F0-9]+$", stream_id):
        raise HTTPException(400, "ID de stream inválido")

    ace_url = f"http://{ACESTREAM_PROXY_HOST}:{ACESTREAM_PROXY_PORT}/ace/getstream?id={stream_id}"

    if request.query_params.get('quality') and request.query_params.get('quality') != 'best':
        ace_url += f"&quality={request.query_params.get('quality')[:-1]}"
                    
    async def stream_content(acestream_url: str) -> AsyncGenerator[bytes, None]:
        while True: 
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(acestream_url) as response:
                        if response.status == 200:
                            while True:
                                chunk = await response.content.read(8192)
                                if not chunk:
                                    break
                                yield chunk
                        else:
                            logger.error(f"Error en la respuesta: {response.status}")
                            await asyncio.sleep(1)
                            continue

            except Exception as e:
                logger.error(f"Error al conectar con acestream: {str(e)}")
                await asyncio.sleep(1)
                continue
    
    try:
        return StreamingResponse(
            stream_content(ace_url),
            media_type='video/mp4',
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
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
    