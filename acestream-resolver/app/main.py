"""Acestream Hash Resolver — standalone microservice.

Exposes a FastAPI HTTP API for resolving AceStream hashes by channel name.
Resolution runs in the background; the API only returns cached results.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel
from fastapi.responses import JSONResponse


class ResolveRequest(BaseModel):
    names: list[str]

from config import ResolverConfig
from resolver import StreamResolver

# Configure logging (must be done before any logger is used)
logging.basicConfig(
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    level=os.getenv("APP_LOG_LEVEL", "INFO").upper(),
)

logger = logging.getLogger("LocalStreams.resolver")

config: Optional[ResolverConfig] = None
resolver_instance: Optional[StreamResolver] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, resolver_instance

    config = ResolverConfig()

    # Log configured sources
    sources = config.source_urls
    if sources:
        logger.info("Monitoring %d M3U source(s):", len(sources))
        for i, url in enumerate(sources, 1):
            logger.info("  Source %d: %s", i, url)
    else:
        logger.warning("No ACESTREAM_RESOLVER_SOURCES configured — no lists to monitor")

    logger.info(
        "Resolver config: refresh_interval=%ds, probe_timeout=%ds, resolution_mode=%s",
        config.refresh_interval,
        config.probe_timeout,
        config.resolution_mode,
    )

    resolver_instance = StreamResolver(config)
    await resolver_instance.start()

    logger.info(
        "Acestream Resolver started on port %d, probing via acexy at %s",
        config.resolver_port,
        config.acexy_base,
    )

    yield

    if resolver_instance:
        await resolver_instance.stop()
        logger.info("Acestream Resolver stopped")


app = FastAPI(
    title="Acestream Hash Resolver",
    description="Resolves AceStream hashes by channel name with background cache refresh",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})


@app.get("/api/v1/resolve")
async def resolve(names: str = Query(..., description="Comma-separated list of channel names")):
    """Resolve channel names to AceStream hashes.

    Fast path: returns cached results instantly.
    For uncached names, triggers resolution on demand and returns
    whatever was resolved within the timeout window.
    """
    if resolver_instance is None:
        return JSONResponse({"results": {}})

    name_list = [n.strip() for n in names.split(",") if n.strip()]
    logger.info("Resolve request: %d name(s) — %s", len(name_list), name_list)

    # Fast path: all from cache
    results = {}
    for name in name_list:
        h = resolver_instance.lookup_from_cache(name)
        results[name] = h

    cached_count = sum(1 for v in results.values() if v)

    # Name-only: fuzzy match for INSTANT result (no probing)
    if cached_count < len(name_list):
        missing = [n for n in name_list if not results[n]]
        logger.info(
            "Cache miss for %d name(s) — name-only resolve (probe in background)",
            len(missing),
        )
        try:
            resolved = await resolver_instance.resolve_by_name_only(missing)
            results.update(resolved)

            # Fire-and-forget: probe the resolved hashes in background
            # to refine quality without blocking the response
            asyncio.create_task(
                resolver_instance.probe_and_refine(missing, resolved)
            )
        except Exception as e:
            logger.warning("Name resolution failed: %s", e)

    resolved_count = sum(1 for v in results.values() if v)
    for name, h in results.items():
        if h:
            logger.info("  => %s → %s", name, h[:8])
    logger.info(
        "Resolve response: %d/%d resolved (instant, no probe)",
        resolved_count,
        len(name_list),
    )
    return JSONResponse({"results": results})


@app.post("/api/v1/resolve")
async def resolve_post(body: ResolveRequest):
    """Resolve channel names via POST (JSON body).

    Same logic as GET but accepts ``{"names": ["name1", "name2"]}``.
    Preferred over GET for cleaner handling of special characters.
    """
    # Delegate to GET handler
    names_str = ",".join(body.names)
    return await resolve(names=names_str)


if __name__ == "__main__":
    import uvicorn

    cfg = ResolverConfig()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=cfg.resolver_port,
        log_level="info",
    )
