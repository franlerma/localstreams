import asyncio
import re
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from aiohttp import ClientSession, ClientTimeout
from config import ACESTREAM_PROXY_HOST, ACESTREAM_PROXY_PORT

router = APIRouter()
logger = logging.getLogger("LocalStreams.acestream")

# Variable global para la sesión HTTP (será inyectada desde main)
http_session: ClientSession = None

def set_http_session(session: ClientSession):
    global http_session
    http_session = session

@router.get("/acestream/video")
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
