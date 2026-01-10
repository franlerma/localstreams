import re
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from aiohttp import ClientSession, ClientTimeout
from urllib.parse import quote

router = APIRouter()
logger = logging.getLogger("LocalStreams.brightcove_simple")

# Variable global para la sesión HTTP
http_session: ClientSession = None

def set_http_session(session: ClientSession):
    global http_session
    http_session = session

@router.get("/brightcove/simple")
async def brightcove_simple(
    video: str = Query(..., description="URL del stream M3U8 de video"),
    audio: str = Query(None, description="URL del stream M3U8 de audio (opcional)")
):
    """
    Versión simple sin Playwright - requiere que proporciones las URLs directamente.
    
    Uso: /brightcove/simple?video=URL_VIDEO&audio=URL_AUDIO
    """
    if not video.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL de video inválida")
    
    if audio:
        if not audio.startswith(('http://', 'https://')):
            raise HTTPException(400, "URL de audio inválida")
        
        # Redirigir al mux
        mux_url = f"/hls/mux?video={quote(video)}&audio={quote(audio)}"
        logger.info(f"Redirigiendo a mux: video={video[:100]}... audio={audio[:100]}...")
        return RedirectResponse(url=mux_url)
    else:
        # Solo video
        logger.info(f"Redirigiendo directamente a: {video[:100]}...")
        return RedirectResponse(url=video)
