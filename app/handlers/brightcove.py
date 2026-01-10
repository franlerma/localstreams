import re
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from aiohttp import ClientSession, ClientTimeout
from urllib.parse import quote
from playwright.async_api import async_playwright

router = APIRouter()
logger = logging.getLogger("LocalStreams.brightcove")

# Variable global para la sesión HTTP (será inyectada desde main)
http_session: ClientSession = None

def set_http_session(session: ClientSession):
    global http_session
    http_session = session

@router.get("/brightcove/extract")
async def brightcove_extract(
    url: str = Query(..., description="URL de la página web que contiene el reproductor Brightcove")
):
    """
    Extrae automáticamente las URLs de los streams de Brightcove usando Playwright
    para ejecutar JavaScript y capturar las peticiones de red.
    """
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL inválida")
    
    logger.info(f"Extrayendo streams de Brightcove desde: {url}")
    
    m3u8_urls = []
    all_requests = []  # Para debug
    
    try:
        async with async_playwright() as p:
            # Lanzar navegador headless
            logger.info("Iniciando navegador headless...")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            
            # Capturar respuestas de la API de Brightcove
            async def handle_response(response):
                response_url = response.url
                
                # Log de todas las respuestas que contengan brightcove o m3u
                if any(keyword in response_url.lower() for keyword in ['brightcove', 'fastly', 'cloudfront', 'm3u']):
                    logger.debug(f"📡 Response: {response_url[:200]}...")
                    all_requests.append(response_url)
                
                # Capturar la respuesta de la API de playback de Brightcove
                if 'edge.api.brightcove.com/playback/v1/accounts' in response_url:
                    try:
                        logger.info(f"🎯 Capturando respuesta de API Brightcove...")
                        data = await response.json()
                        
                        # Extraer sources del JSON
                        sources = data.get('sources', [])
                        logger.info(f"Encontrados {len(sources)} sources en la API")
                        
                        for source in sources:
                            src_url = source.get('src', '')
                            if '.m3u8' in src_url:
                                logger.info(f"🎯 URL M3U8 desde API: {src_url[:200]}...")
                                if src_url not in m3u8_urls:
                                    m3u8_urls.append(src_url)
                                    
                    except Exception as e:
                        logger.error(f"Error parseando respuesta de API: {str(e)}")
                
                # También capturar URLs M3U8 directas de las peticiones
                if '.m3u8' in response_url and not any(x in response_url.lower() for x in ['metrics', 'analytics', 'tracker']):
                    logger.info(f"🎯 URL M3U8 directa capturada: {response_url}")
                    if response_url not in m3u8_urls:
                        m3u8_urls.append(response_url)
            
            # Escuchar todas las respuestas
            page.on("response", handle_response)
            
            # Navegar a la página
            logger.info(f"Navegando a: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Esperar a que el video player esté presente
            logger.info("Esperando a que el player se inicialice...")
            try:
                await page.wait_for_selector('video, .video-js, [data-video-id]', timeout=10000)
                logger.info("Player detectado")
            except Exception as e:
                logger.warning(f"No se detectó player de video: {str(e)}")
            
            # Dar tiempo para que cargue
            await page.wait_for_timeout(5000)
            
            # Intentar hacer clic en play si hay un botón
            try:
                # Buscar diferentes tipos de botones de play
                play_selectors = [
                    'button.vjs-big-play-button',
                    'button[aria-label*="Play"]',
                    'button[aria-label*="Reproducir"]',
                    '.vjs-play-control',
                    'button[title*="Play"]',
                    'button[title*="Reproducir"]'
                ]
                
                for selector in play_selectors:
                    play_button = await page.query_selector(selector)
                    if play_button:
                        logger.info(f"Haciendo clic en botón de reproducción ({selector})...")
                        await play_button.click()
                        await page.wait_for_timeout(8000)  # Esperar más después del click
                        break
                else:
                    logger.info("No se encontró botón de play, el video podría reproducirse automáticamente")
                    await page.wait_for_timeout(5000)
                    
            except Exception as e:
                logger.warning(f"Error al intentar hacer clic en play: {str(e)}")
            
            # Log final de todas las peticiones capturadas
            logger.info(f"Total de URLs M3U8 capturadas: {len(m3u8_urls)}")
            logger.info(f"Total de peticiones relacionadas con streaming: {len(all_requests)}")
            
            # Si no capturamos M3U8, mostrar todas las peticiones de streaming para debug
            if not m3u8_urls and all_requests:
                logger.warning("No se capturaron M3U8, pero se detectaron estas peticiones:")
                for req_url in all_requests[:10]:  # Mostrar las primeras 10
                    logger.warning(f"  - {req_url[:300]}")
            
            # Cerrar navegador
            await browser.close()
            
    except Exception as e:
        logger.error(f"Error usando Playwright: {str(e)}")
        raise HTTPException(502, f"Error extrayendo streams con Playwright: {str(e)}")
    
    if not m3u8_urls:
        logger.error("No se capturaron URLs M3U8 durante la navegación")
        raise HTTPException(500, "No se encontraron streams M3U8 en la página")
    
    logger.info(f"Se capturaron {len(m3u8_urls)} URLs M3U8")
    
    # Filtrar URLs de Brightcove
    brightcove_urls = [
        url for url in m3u8_urls 
        if any(keyword in url.lower() for keyword in ['brightcove', 'cloudfront', 'fastly'])
        and 'chunklist' in url.lower()
    ]
    
    if not brightcove_urls:
        # Si no hay chunklist, usar todas las URLs de Brightcove
        brightcove_urls = [
            url for url in m3u8_urls 
            if any(keyword in url.lower() for keyword in ['brightcove', 'cloudfront', 'fastly'])
        ]
    
    if not brightcove_urls:
        logger.warning("No se encontraron URLs de Brightcove específicas, usando todas las M3U8")
        brightcove_urls = m3u8_urls
    
    logger.info(f"URLs de Brightcove filtradas: {len(brightcove_urls)}")
    for i, u in enumerate(brightcove_urls[:5], 1):
        logger.debug(f"  Stream {i}: {u}...")
    
    # Identificar video y audio
    video_url = None
    audio_url = None
    
    for u in brightcove_urls:
        if 'audio' in u.lower():
            audio_url = u
        elif video_url is None:
            video_url = u
    
    # Si solo hay un stream, usarlo directamente
    if len(brightcove_urls) == 1:
        logger.info("Solo un stream encontrado, redirigiendo directamente")
        return RedirectResponse(url=brightcove_urls[0])
    
    # Si no identificamos ambos, usar los primeros dos
    if not video_url or not audio_url:
        if len(brightcove_urls) >= 2:
            video_url = brightcove_urls[0]
            audio_url = brightcove_urls[1]
            logger.warning("No se identificaron claramente video/audio, usando primeros dos streams")
        else:
            logger.info("Solo un stream disponible")
            return RedirectResponse(url=brightcove_urls[0])
    
    logger.info(f"✓ Video: {video_url[:100]}...")
    logger.info(f"✓ Audio: {audio_url[:100]}...")
    
    # Redirigir al mux
    mux_url = f"/hls/mux?video={quote(video_url)}&audio={quote(audio_url)}"
    logger.info(f"Redirigiendo a: {mux_url[:200]}...")
    
    return RedirectResponse(url=mux_url)


@router.get("/apunt/live")
async def apunt_live_alias():
    """
    Alias de conveniencia para À Punt Media.
    """
    apunt_url = "https://www.apuntmedia.es/directe/directe-tv_136_1392524.html"
    return RedirectResponse(url=f"/brightcove/extract?url={quote(apunt_url)}")
