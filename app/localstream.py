import asyncio
import os
import signal
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import aiohttp
from aiohttp import ClientSession, ClientTimeout

# Importar configuración
from config import (
    APP_PORT, M3U_DIR, LOG_LEVEL, MAX_CONNECTIONS,
    ACESTREAM_PROXY_HOST, ACESTREAM_PROXY_PORT
)

# Importar utilidades
from utils import setup_logging

# Importar routers
from handlers import streamlink_router, acestream_router, hls_router, brightcove_router
from handlers.acestream import set_http_session as set_acestream_session
from handlers.brightcove import set_http_session as set_brightcove_session

# Configurar logging
logger = setup_logging(LOG_LEVEL)

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

async def shutdown():
    global http_session
    logger.info("Iniciando apagado controlado...")

    # Cerrar sesión HTTP
    if http_session and not http_session.closed:
        await http_session.close()
        logger.info("Sesión HTTP cerrada")

    logger.info("Apagado completado correctamente")
    os._exit(0)

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
    
    # Inyectar sesión en handlers que la necesitan
    set_acestream_session(http_session)
    set_brightcove_session(http_session)
    
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

# Crear app FastAPI
app = FastAPI(lifespan=lifespan)

# Registrar routers
app.include_router(streamlink_router, tags=["streamlink"])
app.include_router(acestream_router, tags=["acestream"])
app.include_router(hls_router, tags=["hls"])
app.include_router(brightcove_router, tags=["brightcove"])

# Middleware de seguridad
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
        "Cache-Control": "no-store, max-age=0",
        "Access-Control-Allow-Origin": "*"
    }

    if hasattr(response, 'headers'):
        response.headers.update(security_headers)

    return response

# Endpoints básicos
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

        # Renderizar el template
        response = templates.TemplateResponse(f"{m3u_file}.m3u", args, media_type='text/plain')
        
        # Filtrar líneas que empiezan con # pero no con #EXT
        rendered_content = response.body.decode('utf-8')
        filtered_lines = [
            line for line in rendered_content.split('\n')
            if not (line.startswith('#') and not line.startswith('#EXT'))
        ]
        filtered_content = '\n'.join(filtered_lines)
        
        return Response(content=filtered_content, media_type='text/plain', headers=response.headers)

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
