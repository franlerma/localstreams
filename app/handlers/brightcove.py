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
    
    logger.debug(f"Extracting Brightcove streams from: {url}")
    
    m3u8_urls = []
    all_requests = []  # Para debug
    api_captured = False  # Flag para salir rápido cuando capturamos la API
    
    try:
        async with async_playwright() as p:
            # Lanzar navegador headless con opciones optimizadas
            logger.debug("Starting headless browser...")
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
                
                # Log all responses containing brightcove or m3u
                if any(keyword in response_url.lower() for keyword in ['brightcove', 'fastly', 'cloudfront', 'm3u']):
                    logger.debug(f"📡 Response: {response_url[:200]}...")
                    all_requests.append(response_url)
                
                # Capture Brightcove playback API response
                if 'edge.api.brightcove.com/playback/v1/accounts' in response_url:
                    try:
                        logger.debug(f"🎯 Capturing Brightcove API response...")
                        data = await response.json()
                        
                        # Extract sources from JSON
                        sources = data.get('sources', [])
                        logger.debug(f"Found {len(sources)} sources in API")
                        
                        for source in sources:
                            src_url = source.get('src', '')
                            if '.m3u8' in src_url:
                                logger.debug(f"🎯 M3U8 URL from API: {src_url[:200]}...")
                                if src_url not in m3u8_urls:
                                    m3u8_urls.append(src_url)
                        
                        # Mark API as captured
                        api_captured = True
                                    
                    except Exception as e:
                        logger.error(f"Error parsing API response: {str(e)}")
                
                # Also capture direct M3U8 URLs from requests
                if '.m3u8' in response_url and not any(x in response_url.lower() for x in ['metrics', 'analytics', 'tracker']):
                    logger.info(f"🎯 Direct M3U8 URL captured: {response_url}")
                    if response_url not in m3u8_urls:
                        m3u8_urls.append(response_url)
                    api_captured = True
            
            # Escuchar todas las respuestas
            page.on("response", handle_response)
            
            # Navigate to page
            logger.debug(f"Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            
            # Wait only until we capture API or max 5 seconds
            logger.debug("Waiting for API capture...")
            max_wait = 50  # 5 seconds total (50 x 100ms)
            for i in range(max_wait):
                if api_captured:
                    logger.debug(f"✅ API captured in ~{i * 100}ms")
                    break
                await page.wait_for_timeout(100)  # Check every 100ms
            
            if not api_captured:
                logger.debug("API response not captured in 5 seconds")
            
            # Final log of all captured requests
            logger.debug(f"Total M3U8 URLs captured: {len(m3u8_urls)}")
            logger.debug(f"Total streaming-related requests: {len(all_requests)}")
            
            # If no M3U8 captured, show streaming requests for debug
            if not m3u8_urls and all_requests:
                logger.debug("No M3U8 captured, but these requests were detected:")
                for req_url in all_requests[:10]:  # Show first 10
                    logger.debug(f"  - {req_url[:300]}")
            
            # Cerrar navegador
            await browser.close()
            
    except Exception as e:
        logger.error(f"Error using Playwright: {str(e)}")
        raise HTTPException(502, f"Error extracting streams with Playwright: {str(e)}")
    
    if not m3u8_urls:
        logger.error("No M3U8 URLs captured during navigation")
        raise HTTPException(500, "No M3U8 streams found on page")
    
    logger.debug(f"Captured {len(m3u8_urls)} M3U8 URLs")
    
    # Filter Brightcove URLs
    brightcove_urls = [
        url for url in m3u8_urls 
        if any(keyword in url.lower() for keyword in ['brightcove', 'cloudfront', 'fastly'])
        and 'chunklist' in url.lower()
    ]
    
    if not brightcove_urls:
        # If no chunklist, use all Brightcove URLs
        brightcove_urls = [
            url for url in m3u8_urls 
            if any(keyword in url.lower() for keyword in ['brightcove', 'cloudfront', 'fastly'])
        ]
    
    if not brightcove_urls:
        logger.debug("No specific Brightcove URLs found, using all M3U8")
        brightcove_urls = m3u8_urls
    
    logger.debug(f"Brightcove URLs filtered: {len(brightcove_urls)}")
    for i, u in enumerate(brightcove_urls[:5], 1):
        logger.debug(f"  Stream {i}: {u}...")
    
    # Identify video and audio
    video_url = None
    audio_url = None
    
    for u in brightcove_urls:
        if 'audio' in u.lower():
            audio_url = u
        elif video_url is None:
            video_url = u
    
    # If only one stream, use it directly
    if len(brightcove_urls) == 1:
        logger.info(f"✓ Stream: {brightcove_urls[0]}")
        return RedirectResponse(url=brightcove_urls[0])
    
    # If both not identified, use first two
    if not video_url or not audio_url:
        # if len(brightcove_urls) >= 2:
        #     video_url = brightcove_urls[0]
        #     audio_url = brightcove_urls[1]
        #     logger.debug("Video/audio not clearly identified, using first two streams")
        #     logger.info(f"✓ Video: {video_url}")
        #     logger.info(f"✓ Audio: {audio_url}")
        # else:
        logger.warning("Video/audio not clearly identified, using first two streams")
        logger.info(f"✓ Stream: {brightcove_urls[0]}")
        return RedirectResponse(url=brightcove_urls[0])
    else:
        logger.info(f"✓ Video: {video_url}")
        logger.info(f"✓ Audio: {audio_url}")
    
    # Redirect to mux
    mux_url = f"/hls/mux?video={quote(video_url)}&audio={quote(audio_url)}"
    logger.info(f"Redirecting to: {mux_url}...")
    
    return RedirectResponse(url=mux_url)

