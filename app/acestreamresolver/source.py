"""External M3U source manager — downloads and parses AceStream entries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING, Optional

import aiohttp
from aiohttp import ClientTimeout

from . import AcestreamEntry

if TYPE_CHECKING:
    from .config import ResolverConfig

logger = logging.getLogger("LocalStreams.resolver.source")

ACESTREAM_RE = re.compile(r"acestream://([a-fA-F0-9]{40})")
ID_RE = re.compile(r"[?&]id=([a-fA-F0-9]{40})")
EXTINF_RE = re.compile(r'#EXTINF:(?:\d+(?:\.\d+)?)?(.*)')
TVG_NAME_RE = re.compile(r'tvg-name="([^"]*)"')
TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')
TVG_RESOLUTION_RE = re.compile(r'tvg-resolution="([^"]*)"')


class M3USourceManager:
    """Downloads external M3U lists and parses acestream entries."""

    def __init__(self, config: ResolverConfig, http_session: aiohttp.ClientSession) -> None:
        self.config = config
        self.http = http_session
        # Per-URL tracking: url -> {etag, last_modified, content_hash}
        self._source_state: dict[str, dict[str, str | None]] = {}
        # Cached parsed entries per URL (used when server returns 304)
        self._source_entries: dict[str, list[AcestreamEntry]] = {}

    def get_all_source_urls(self) -> list[str]:
        """Return configured source URLs."""
        return self.config.source_urls

    async def download_source(
        self, url: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Download single M3U source.

        Returns (content, etag) on success, (None, None) on failure.
        Sends conditional request headers if the URL was previously tracked.
        """
        headers: dict[str, str] = {}
        state = self._source_state.get(url)
        if state:
            etag = state.get("etag")
            if etag:
                headers["If-None-Match"] = etag
            last_modified = state.get("last_modified")
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        timeout = ClientTimeout(total=self.config.download_timeout)
        try:
            async with self.http.get(url, timeout=timeout, headers=headers) as resp:
                if resp.status == 304:
                    logger.debug("Source not modified (304): %s", url)
                    return (None, state.get("etag") if state else None)

                if resp.status != 200:
                    logger.warning(
                        "HTTP %d downloading M3U source: %s",
                        resp.status,
                        url,
                    )
                    return (None, None)

                content = await resp.text()
                etag = resp.headers.get("ETag")
                last_modified = resp.headers.get("Last-Modified")
                content_hash = hashlib.md5(content.encode()).hexdigest()

                self._source_state[url] = {
                    "etag": etag,
                    "last_modified": last_modified,
                    "content_hash": content_hash,
                }
                logger.debug(
                    "Downloaded M3U source (%d bytes): %s",
                    len(content),
                    url,
                )
                return (content, etag)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Failed to download M3U source: %s -- %s", url, exc)
            return (None, None)

    async def fetch_all(self) -> list[AcestreamEntry]:
        """Download all configured sources (in parallel), parse entries, return merged list."""
        urls = self.get_all_source_urls()
        if not urls:
            logger.info("No M3U source URLs configured")
            return []

        async def fetch_one(source_url: str) -> list[AcestreamEntry]:
            content, _ = await self.download_source(source_url)
            if content is None:
                # Server returned 304 — use cached entries from last successful parse
                return self._source_entries.get(source_url, [])
            entries = self._parse_m3u(content, source_url)
            self._source_entries[source_url] = entries
            return entries

        results = await asyncio.gather(
            *[fetch_one(u) for u in urls],
            return_exceptions=True,
        )

        entries: list[AcestreamEntry] = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.warning("❌ %s — error: %s", url, result)
                continue
            count = len(result)
            status = "✅" if count > 0 else "❌ (sin IDs acestream)"
            logger.info("%s %s — %d IDs", status, url, count)
            entries.extend(result)

        return entries

    def _parse_m3u(self, content: str, source_url: str) -> list[AcestreamEntry]:
        """Parse M3U content into AcestreamEntry list."""
        entries: list[AcestreamEntry] = []
        lines = content.splitlines()

        for i, line in enumerate(lines):
            stripped = line.strip()
            acestream_hash: Optional[str] = None

            # Match acestream:// scheme
            m = ACESTREAM_RE.search(stripped)
            if m:
                acestream_hash = m.group(1).lower()
            else:
                # Match /ace/getstream?id= or any ?id= parameter
                m = ID_RE.search(stripped)
                if m:
                    acestream_hash = m.group(1).lower()

            if not acestream_hash:
                continue

            # Default to hash as display name when no EXTINF is found
            name: str = acestream_hash
            tvg_name: Optional[str] = None
            tvg_id: Optional[str] = None
            tvg_resolution: Optional[str] = None

            # Look backward for preceding #EXTINF line
            for j in range(i - 1, -1, -1):
                prev_line = lines[j].strip()
                if prev_line.startswith("#EXTINF:"):
                    extinf_match = EXTINF_RE.match(prev_line)
                    if extinf_match:
                        extinf_body = extinf_match.group(1)

                        # Extract tvg-* attributes
                        tvg_name_m = TVG_NAME_RE.search(extinf_body)
                        if tvg_name_m:
                            tvg_name = tvg_name_m.group(1)

                        tvg_id_m = TVG_ID_RE.search(extinf_body)
                        if tvg_id_m:
                            tvg_id = tvg_id_m.group(1)

                        tvg_res_m = TVG_RESOLUTION_RE.search(extinf_body)
                        if tvg_res_m:
                            tvg_resolution = tvg_res_m.group(1)

                        # Display name: everything after the last comma
                        if "," in extinf_body:
                            name = extinf_body.rsplit(",", 1)[1].strip()
                    break

            entry = AcestreamEntry(
                hash=acestream_hash,
                name=name,
                tvg_name=tvg_name,
                tvg_id=tvg_id,
                tvg_resolution=tvg_resolution,
                source_url=source_url,
            )
            entries.append(entry)

        # Deduplicate by hash within this source (keep first occurrence)
        seen: set[str] = set()
        unique: list[AcestreamEntry] = []
        for e in entries:
            if e.hash not in seen:
                seen.add(e.hash)
                unique.append(e)

        return unique

    def has_changed(self, url: str) -> bool:
        """Check if a source has changed since last fetch.

        Returns True if the URL has never been tracked (no previous
        ETag or content hash stored).
        """
        return url not in self._source_state
