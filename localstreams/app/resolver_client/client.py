"""Async HTTP client for the acestream-resolver microservice."""

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import aiohttp
from aiohttp import ClientSession

logger = logging.getLogger("LocalStreams.resolver_client")


class ResolverClient:
    """Async HTTP client for the acestream-resolver microservice.

    Makes HTTP GET requests to the resolver's ``/api/v1/resolve`` endpoint
    and returns cached hash results as a ``{name: hash}`` dict.
    """

    def __init__(self, base_url: str, http_session: ClientSession) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_session

    async def resolve(self, names: list[str]) -> dict[str, str]:
        """Resolve channel names via the resolver service.

        Makes a single HTTP GET with all names as a comma-separated query
        parameter.  Returns ``{name: hash}`` where unresolved names have
        an empty hash.

        On any failure (connection error, timeout, non-200), logs a warning
        and returns an empty dict — templates still render without hashes.
        """
        if not names:
            return {}

        # Deduplicate preserving order
        seen: set[str] = set()
        unique_names: list[str] = []
        for n in names:
            key = n.strip().lower()
            if key not in seen:
                seen.add(key)
                unique_names.append(n.strip())

        # URL-encode each name individually so +, spaces, commas are safe
        encoded_names = ",".join(quote(n, safe="") for n in unique_names)
        url = f"{self._base_url}/api/v1/resolve?names={encoded_names}"

        timeout = aiohttp.ClientTimeout(total=5)

        try:
            async with self._http.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Resolver returned HTTP %d for %d names",
                        resp.status,
                        len(unique_names),
                    )
                    return {}

                data = await resp.json()
                results: dict[str, str] = data.get("results", {})
                cached_count = sum(1 for v in results.values() if v)
                logger.debug(
                    "Resolver: %d/%d names cached",
                    cached_count,
                    len(unique_names),
                )
                return results

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                "Resolver unavailable (%s) — returning empty results for %d names",
                exc,
                len(unique_names),
            )
            return {}
