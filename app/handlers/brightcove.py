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
    api_captured = False  # Flag para salir rápido cuando capturamos la API
    
    try:
        async with async_playwright() as p:
            # Lanzar navegador headless con opciones optimizadas
            logger.info("Iniciando navegador headless...")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            
            # Bloquear recursos innecesarios para acelerar carga
            await page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"]
                else route.continue_()
            ))
            
            # Capturar respuestas de la API de Brightcove
            async def handle_response(response):
                nonlocal api_captured
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
                        
                        # Marcar que ya capturamos la API
                        api_captured = True
                                    
                    except Exception as e:
                        logger.error(f"Error parseando respuesta de API: {str(e)}")
                
                # También capturar URLs M3U8 directas de las peticiones
                if '.m3u8' in response_url and not any(x in response_url.lower() for x in ['metrics', 'analytics', 'tracker']):
                    logger.info(f"🎯 URL M3U8 directa capturada: {response_url}")
                    if response_url not in m3u8_urls:
                        m3u8_urls.append(response_url)
                    api_captured = True
            
            # Escuchar todas las respuestas
            page.on("response", handle_response)
            
            # Navegar a la página
            logger.info(f"Navegando a: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            
            # Esperar solo hasta que capturemos la API o máximo 5 segundos
            logger.info("Esperando captura de API...")
            max_wait = 50  # 5 segundos total (50 x 100ms)
            for i in range(max_wait):
                if api_captured:
                    logger.info(f"✅ API capturada en ~{i * 100}ms")
                    break
                await page.wait_for_timeout(100)  # Check cada 100ms
            
            if not api_captured:
                logger.warning("No se capturó respuesta de API en 5 segundos")
            
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

