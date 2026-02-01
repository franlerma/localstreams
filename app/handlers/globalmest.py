import re
import logging
import time
import json
import os
import base64
from typing import Optional, Dict
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from aiohttp import ClientSession
from playwright.async_api import async_playwright

router = APIRouter()
logger = logging.getLogger("LocalStreams.globalmest")

http_session: ClientSession = None

# Cache: {source_url: (master_m3u8_url, expiry_timestamp)}
cache: Dict[str, tuple[str, float]] = {}
GLOBALMEST_CACHE_TTL = int(os.getenv("GLOBALMEST_CACHE_TTL", "300"))


def set_http_session(session: ClientSession):
    global http_session
    http_session = session


def _get_jwt_expiry(jwt: str) -> Optional[float]:
    """Extrae el campo 'exp' del payload JWT."""
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp")
    except Exception:
        return None


def _get_from_cache(url: str) -> Optional[str]:
    if url in cache:
        redirect_url, exp = cache[url]
        if time.time() < exp:
            logger.info(f"✓ Cache hit (exp en {int(exp - time.time())}s): {redirect_url[:100]}...")
            return redirect_url
        else:
            logger.debug(f"Cache expirada para {url}")
            del cache[url]
    return None


def _save_to_cache(source_url: str, master_url: str, jwt: str):
    jwt_exp = _get_jwt_expiry(jwt)
    # Usar la expiración del JWT si existe, sino TTL fijo
    exp = jwt_exp if jwt_exp else time.time() + GLOBALMEST_CACHE_TTL
    cache[source_url] = (master_url, exp)
    logger.debug(f"Cached: {source_url} (exp: {exp})")


def _extract_jwt_from_html(html: str) -> Optional[str]:
    """Extrae JWT del script account.globalmest.com incrustado en el HTML."""
    match = re.search(
        r'account\.globalmest\.com/[^"\']+[?&]token=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)',
        html
    )
    return match.group(1) if match else None


# Mapeo calidad → bitrate en nombre de chunklist
QUALITY_BITRATE = {
    "1080": "b3300000",
    "720": "b1280000",
    "480": "b565000",
}


def _apply_quality(master_url: str, quality: str) -> str:
    """
    Reemplaza el playlist master por el chunklist de la calidad solicitada.
    El master es: .../playlist.m3u8?auth_token=...
    El chunklist: .../chunklist_w770024458_{bitrate}.m3u8?auth_token=...
    Mismo auth_token, mismo path base → solo cambia el nombre del archivo.
    """
    bitrate = QUALITY_BITRATE.get(quality)
    if not bitrate:
        return master_url

    return master_url.replace(
        "playlist.m3u8",
        f"chunklist_w770024458_{bitrate}.m3u8"
    )


async def _resolve_globalmest_url(url: str) -> tuple[str, str]:
    """
    Navega la página con Playwright, espera a que el player haga peticiones
    de red y captura la URL del playlist master m3u8 ya firmada.

    El player hace la petición al origin (vod-origin.globalmest.com) con el
    auth_token, y ese origin actúa como proxy que firma la petición al S3
    Wasabi. La URL que capturamos del tráfico de red ya tiene la firma válida.

    Returns:
        Tupla (master_m3u8_url, jwt)
    """
    logger.debug(f"Extracting GlobalMEST stream from: {url}")

    m3u8_urls = []
    master_captured = False
    capture_time = None

    try:
        async with async_playwright() as p:
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
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = await context.new_page()

            # Bloquear solo recursos que no afectan al player
            async def handle_route(route):
                if route.request.resource_type in ["image", "stylesheet", "font"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", handle_route)

            # Capturar peticiones m3u8 del player (síncrono, .url es property)
            def on_request(request):
                nonlocal master_captured, capture_time
                req_url = request.url
                if '.m3u8' in req_url and 'globalmest.com' in req_url:
                    logger.debug(f"🎯 M3U8 captured: {req_url[:120]}...")
                    m3u8_urls.append(req_url)
                    if 'playlist.m3u8' in req_url:
                        master_captured = True
                        capture_time = time.time()

            page.on("request", on_request)

            start_time = time.time()
            logger.debug(f"Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            logger.debug(f"Page loaded in {time.time() - start_time:.2f}s")

            # El sitio usa OnmLazyLoadScripts: todos los scripts tienen
            # type="onmlazyloadscript" y no se ejecutan hasta que el CMS
            # los activa. En headless los triggers de interacción no disparan,
            # así que el player GlobalMEST nunca se inicializa.
            # Solución: extraer la URL del script del player del DOM y
            # ejecutarlo manualmente mediante un <script> dinámico.
            player_script_url = await page.evaluate("""() => {
                const scripts = document.querySelectorAll('script[type="onmlazyloadscript"]');
                for (const s of scripts) {
                    const src = s.getAttribute('src') || '';
                    if (src.includes('account.globalmest.com')) return src;
                }
                return null;
            }""")

            if player_script_url:
                logger.debug(f"Player script found: {player_script_url[:80]}...")
                # Inyectar el script como script estándar para que se ejecute
                await page.evaluate("""(url) => {
                    return new Promise((resolve, reject) => {
                        const s = document.createElement('script');
                        s.src = url;
                        s.onload = () => resolve(true);
                        s.onerror = (e) => reject(new Error('Script load failed: ' + e));
                        document.head.appendChild(s);
                    });
                }""", player_script_url)
                logger.debug("Player script injected and loaded")
            else:
                logger.debug("WARNING: Player script not found in DOM")

            # Esperar a que el player haga peticiones m3u8.
            # Si el script se inyectó bien pero no hay actividad de red tras 3s,
            # el player no va a hacer peticiones (necesita interacción del usuario
            # o contexto que no existe en headless). En ese caso el fallback con
            # JWT resuelve el stream, así que no tiene sentido esperar más.
            logger.debug("Waiting for m3u8 capture...")
            wait_limit = 30 if not player_script_url else 3  # 3s si script inyectado, 30s si no
            iterations = wait_limit * 10
            for i in range(iterations):
                if master_captured:
                    logger.debug(f"✅ Master captured in ~{i * 100}ms after inject")
                    break
                await page.wait_for_timeout(100)

            if not master_captured:
                logger.debug(f"Master not captured in {wait_limit}s, total m3u8 URLs: {len(m3u8_urls)}")

            # Obtener HTML para extraer JWT
            html_content = await page.content()

            await browser.close()

    except Exception as e:
        logger.error(f"Error using Playwright: {e}")
        raise HTTPException(502, f"Error extracting page with Playwright: {e}")

    logger.debug(f"Total m3u8 URLs captured: {len(m3u8_urls)}")

    # Extraer JWT del HTML (siempre, para el cache)
    jwt = _extract_jwt_from_html(html_content) if html_content else None
    if jwt:
        jwt_exp = _get_jwt_expiry(jwt)
        logger.debug(f"✓ JWT extracted (exp: {jwt_exp})")
    else:
        logger.debug("JWT not found in HTML")

    # Si capturamos el master desde el tráfico de red, usarlo directamente
    master_url = None
    for u in m3u8_urls:
        if 'playlist.m3u8' in u:
            master_url = u
            break

    # Si no hay master pero sí otros m3u8 (chunklists), usar el primero
    if not master_url and m3u8_urls:
        master_url = m3u8_urls[0]
        logger.debug(f"No master capturado, usando primer m3u8: {master_url[:100]}...")

    # Fallback: si no capturamos nada del tráfico pero tenemos JWT,
    # construir la URL del master (funciona porque el master sí va al proxy
    # que valida solo el JWT, es el chunklist donde S3 firma)
    if not master_url and jwt:
        # Extraer origin del script JS (excluir account.globalmest.com)
        origin_match = re.search(r'https?://((?!account)[a-zA-Z0-9_-]+\.globalmest\.com)', html_content or "")
        if origin_match:
            origin_host = origin_match.group(1)
            master_url = f"https://{origin_host}/principal/smil:principal.smil/playlist.m3u8?auth_token={jwt}"
            logger.debug(f"Fallback: master construido desde HTML (origin: {origin_host})")
        else:
            logger.error("Fallback failed: no streaming origin found in HTML (only account.globalmest.com)")

    if not master_url:
        logger.error("No m3u8 URLs captured and fallback failed")
        raise HTTPException(500, "No se pudieron capturar URLs m3u8 del player GlobalMEST")

    if not jwt:
        logger.warning("JWT not found in HTML, caching disabled")

    logger.info(f"✓ Stream resolved: {master_url[:120]}...")
    return master_url, jwt or ""


@router.get("/globalmest/extract")
async def globalmest_extract(
    url: str = Query(..., description="URL de la página con el reproductor GlobalMEST"),
    quality: str = Query("1080", description="Calidad: 1080, 720, 480")
):
    """
    Extrae la URL del stream m3u8 de GlobalMEST capturando el tráfico de red
    del player con Playwright.

    El origin GlobalMEST actúa como proxy que firma las peticiones al S3 Wasabi,
    por lo que las URLs capturadas son las únicas que funcionan.

    Cache con invalidación automática basada en la expiración del JWT.
    """
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL inválida")

    quality = quality.replace("p", "")
    if quality not in QUALITY_BITRATE:
        raise HTTPException(400, f"Calidad no válida. Use: {', '.join(QUALITY_BITRATE.keys())}")

    logger.debug(f"GlobalMEST extract requested - URL: {url}, quality: {quality}p")

    # Cache
    cached_url = _get_from_cache(url)
    if cached_url:
        final_url = _apply_quality(cached_url, quality)
        logger.info(f"✓ Redirecting (cached) to: {final_url[:120]}...")
        return RedirectResponse(url=final_url)

    # Resolver
    master_url, jwt = await _resolve_globalmest_url(url)

    # Cachear el master (la calidad se aplica al servir)
    if jwt:
        _save_to_cache(url, master_url, jwt)

    final_url = _apply_quality(master_url, quality)
    logger.info(f"✓ Redirecting to: {final_url[:120]}...")
    return RedirectResponse(url=final_url)
