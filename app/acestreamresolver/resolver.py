"""StreamResolver — orchestrator for the acestream-hash-resolver feature.

Coordinates source fetching, fuzzy matching, stream probing, caching,
and background cache refresh. This is the single public API entry point
for the resolver package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml

import aiohttp

from . import AcestreamEntry, ProbeResult, ResolvedChannel
from .cache import ResolverCache
from .config import ResolverConfig
from .matcher import ChannelMatcher
from .prober import StreamProber
from .source import M3USourceManager

logger = logging.getLogger("LocalStreams.resolver")

RESOLVE_CALL_RE = re.compile(r"""acestream_resolve\(['"](.+?)['"]\)""")
_TEMPLATE_POLL_INTERVAL = 30  # seconds between template file checks


class StreamResolver:
    """Orchestrates M3U source fetching, fuzzy matching, stream probing,
    caching, and background cache refresh.

    This is the single public API entry point for the resolver package.
    """

    def __init__(self, config: ResolverConfig) -> None:
        self.config = config
        self._http: Optional[aiohttp.ClientSession] = None
        self._source_manager: Optional[M3USourceManager] = None
        self._matcher = ChannelMatcher()
        self._prober: Optional[StreamProber] = None
        self._cache = ResolverCache(ttl=config.refresh_interval)
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        # Template tracking for fast activation/deactivation
        self._template_mtimes: dict[str, float] = {}
        self._active_templates_known: Optional[bool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the resolver: create HTTP session, warm up sources,
        detect template usage, start background refresh loop."""
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.download_timeout),
        )
        self._source_manager = M3USourceManager(self.config, self._http)
        self._prober = StreamProber(self.config, self._http)

        # Warm up: initial source download (non-blocking if sources not set)
        try:
            entries = await self._source_manager.fetch_all()
            logger.info("Warmup: fetched %d AceStream entries", len(entries))
        except Exception as e:
            logger.warning("Warmup source fetch failed: %s", e)

        # Load persisted cache from disk
        self._load_cache()

        # Immediately resolve all channels from active templates
        await self.pre_resolve_all()

        # Persist any newly resolved entries
        self._save_cache()

        # Start background refresh loop
        self._stop_event.clear()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Shutdown: cancel background task, close HTTP session."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._http and not self._http.closed:
            await self._http.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup_from_cache(self, name: str) -> str:
        """Synchronous cache-only lookup. Returns hash or ``""`` if not cached.

        This is the **only** method called from the template rendering path.
        It never does I/O — instant return.
        """
        cached = self._cache.get(name)
        return cached.hash if cached else ""

    async def pre_resolve_all(self) -> None:
        """Scan all templates, collect all ``acestream_resolve`` names,
        fetch sources, and resolve every uncached name.

        Called from the background refresh loop only — never from the
        request path.
        """
        m3u_dir = Path(self.config.m3u_dir)
        if not m3u_dir.exists():
            return

        all_names: set[str] = set()
        for f in m3u_dir.glob("*.m3u"):
            try:
                content = f.read_text(encoding="utf-8")
                if "acestream_resolve" not in content:
                    continue
                names = RESOLVE_CALL_RE.findall(content)
                for n in names:
                    key = n.strip().lower()
                    if key:
                        all_names.add(key)
            except Exception:
                continue

        if not all_names:
            logger.debug("pre_resolve_all: no acestream_resolve calls found in templates")
            return

        missing = [n for n in all_names if self._cache.is_expired(n)]
        if not missing:
            logger.debug("pre_resolve_all: all %d names already cached", len(all_names))
            return

        logger.info(
            "pre_resolve_all: resolving %d/%d uncached names",
            len(missing),
            len(all_names),
        )

        entries: list[AcestreamEntry] = []
        if self._source_manager:
            try:
                entries = await self._source_manager.fetch_all()
            except Exception as e:
                logger.warning("pre_resolve_all: source fetch failed: %s", e)

        if not entries:
            logger.warning("pre_resolve_all: no source entries available")
            return

        resolved = await self._resolve_inner(missing, entries)
        successful = sum(1 for v in resolved.values() if v)
        logger.info(
            "pre_resolve_all: resolved %d/%d names, now running silence check",
            successful,
            len(missing),
        )

        # Run silence checks in background
        if resolved:
            asyncio.create_task(self._refine_in_background(entries, resolved))

        self._save_cache()

    async def resolve_batch(self, names: list[str]) -> dict[str, str]:
        """Resolve multiple channel names in parallel.

        1. Deduplicate names, normalize
        2. Check cache — return fresh hits immediately
        3. If misses/expired: fetch sources, fuzzy match all missing names,
           collect unique candidate hashes, probe all in parallel,
           select best per name, cache results
        4. Return {original_name: hash} dict

        Respects total_timeout config using asyncio.wait_for on probe batch.
        On timeout, returns whatever was resolved so far (cache hits +
        any probes that completed before the deadline). Does not crash.
        """
        # Step 1: deduplicate and normalize, preserving original casing
        normalized_names: dict[str, str] = {}
        for name in names:
            key = name.strip().lower()
            if key not in normalized_names:
                normalized_names[key] = name

        # Step 2: check cache — return fresh hits immediately
        result: dict[str, str] = {}
        missing: list[str] = []
        for norm_name, original_name in normalized_names.items():
            cached = self._cache.get(norm_name)
            if cached is not None:
                result[original_name] = cached.hash
            else:
                missing.append(norm_name)

        if not missing:
            logger.debug("All %d names resolved from cache", len(result))
            return result

        logger.info(
            "Cache misses: %d/%d names — resolving",
            len(missing),
            len(normalized_names),
        )

        # Step 3: fetch sources
        entries: list[AcestreamEntry] = []
        if self._source_manager:
            try:
                entries = await self._source_manager.fetch_all()
            except Exception as e:
                logger.warning("Source fetch failed during resolve_batch: %s", e)

        if not entries:
            logger.warning(
                "No source entries available — returning empty hashes for unresolved names"
            )
            for norm_name in missing:
                result[normalized_names.get(norm_name, norm_name)] = ""
            return result

        # Step 4: resolve cache misses with timeout
        try:
            resolved = await asyncio.wait_for(
                self._resolve_inner(missing, entries),
                timeout=self.config.total_timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                "Resolve batch timed out after %ds — returning partial results",
                self.config.total_timeout,
            )
            resolved = {}

        for norm_name, hash_value in resolved.items():
            original = normalized_names.get(norm_name, norm_name)
            result[original] = hash_value

        return result

    # ------------------------------------------------------------------
    # Internal helpers — resolution pipeline
    # ------------------------------------------------------------------

    async def _resolve_inner(
        self,
        missing: list[str],
        entries: list[AcestreamEntry],
    ) -> dict[str, str]:
        """Core resolution logic for cache-missed names.

        Matches names against entries, probes unique candidate hashes
        (with total_timeout for partial results), selects best per name,
        caches results, returns ``{normalized_name: hash}``.

        This is a subtask of ``resolve_batch`` and may be cancelled on
        timeout.  Returns whatever was resolved before cancellation.
        """
        # 1. Fuzzy match all missing names against source entries
        name_to_candidates: dict[str, list[AcestreamEntry]] = {}
        all_candidate_hashes: set[str] = set()

        for name in missing:
            candidates = self._matcher.find_matches(name, entries)

            # Also consider the currently cached hash as a candidate
            cached = self._cache.get_fallback(name)
            if cached and cached.hash:
                if not any(c.hash == cached.hash for c in candidates):
                    synthetic = AcestreamEntry(
                        hash=cached.hash,
                        name=name,
                        tvg_name=None,
                        tvg_id=None,
                        tvg_resolution=cached.resolution,
                        source_url=None,
                    )
                    candidates.append(synthetic)

            if candidates:
                name_to_candidates[name] = candidates
                for c in candidates:
                    all_candidate_hashes.add(c.hash)

        if not all_candidate_hashes:
            logger.debug("No matching candidates found for any missing name")
            return {}

        # 2. Probe all unique candidate hashes in parallel
        #    Use asyncio.wait for graceful partial-results on timeout.
        logger.debug(
            "Probing %d unique candidate hashes for %d names",
            len(all_candidate_hashes),
            len(missing),
        )

        probe_tasks: dict[asyncio.Task[Optional[ProbeResult]], str] = {
            asyncio.create_task(self._probe_candidate(h, name_to_candidates)): h
            for h in all_candidate_hashes
        }

        done: set[asyncio.Task[Optional[ProbeResult]]]
        pending: set[asyncio.Task[Optional[ProbeResult]]]

        try:
            done, pending = await asyncio.wait(
                set(probe_tasks.keys()),
                timeout=self.config.total_timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Both running and completed tasks are captured below;
            # treat everything still running as pending.
            done = {t for t in probe_tasks.keys() if t.done()}
            pending = {t for t in probe_tasks.keys() if not t.done()}

        # Cancel pending tasks (they timed out)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if pending:
            logger.info(
                "Probe batch partially completed: %d done, %d timed out",
                len(done),
                len(pending),
            )

        # Collect successful probe results
        probe_results: dict[str, ProbeResult] = {}
        for task in done:
            h = probe_tasks[task]
            try:
                pr = task.result()
                if isinstance(pr, ProbeResult):
                    probe_results[h] = pr
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("Probe task failed for hash %s: %s", h, e)

        # 3. For each missing name, pick best hash and cache
        resolved: dict[str, str] = {}
        for name in missing:
            candidates = name_to_candidates.get(name, [])
            if not candidates:
                resolved[name] = ""
                continue

            best_hash = self._pick_best_hash(name, candidates, probe_results)
            if best_hash is not None:
                resolved[name] = best_hash
            else:
                # All probed candidates failed — keep cached hash as fallback
                fallback = self._cache.get_fallback(name)
                resolved[name] = fallback.hash if fallback else ""

            if best_hash is not None:
                pr = probe_results.get(best_hash)
                if pr is not None:
                    resolution_str = (
                        f"{pr.width}x{pr.height}"
                        if pr.width and pr.height
                        else "unknown"
                    )
                    channel = ResolvedChannel(
                        channel_name=name,
                        hash=pr.hash,
                        resolution=resolution_str,
                        score=pr.score,
                        cached_at=time.time(),
                        expires_at=time.time() + self.config.refresh_interval,
                    )
                    self._cache.set(name, channel)

        return resolved

    async def _probe_candidate(
        self,
        hash: str,
        name_to_candidates: dict[str, list[AcestreamEntry]],
    ) -> Optional[ProbeResult]:
        """Probe a single hash, deriving metadata resolution from entry name heuristics.

        Returns ``None`` when the prober is not available (should not
        happen after ``start()``).
        """
        if self._prober is None:
            return None

        # Derive metadata resolution from any candidate entry that has this hash
        metadata_resolution: tuple[int, int] = (0, 0)
        for candidates in name_to_candidates.values():
            for entry in candidates:
                if entry.hash == hash:
                    res = StreamProber._resolution_from_name(entry.name)
                    if res != (0, 0):
                        metadata_resolution = res
                        break
            if metadata_resolution != (0, 0):
                break

        return await self._prober.probe(
            hash=hash,
            resolution_mode=self.config.resolution_mode,
            metadata_resolution=metadata_resolution,
        )

    def _pick_best_hash(
        self,
        name: str,
        entries: list[AcestreamEntry],
        probe_results: dict[str, ProbeResult],
    ) -> Optional[str]:
        """Pick the best hash for a given channel name from already-probed results.

        Preference order:
        1. Probed entries with no error and non-zero score (highest score wins).
        2. Probed-but-errored or unprobed entries, ranked by fuzzy match score.
        3. ``None`` — no viable candidate.

        Returns hash string or ``None``.
        """
        scored_probed: list[tuple[float, str]] = []
        scored_fallback: list[tuple[int, str]] = []

        for entry in entries:
            pr = probe_results.get(entry.hash)
            if pr is not None and pr.error is None and pr.score > 0:
                # Successful probe — use composite score (pixels * stability)
                scored_probed.append((pr.score, entry.hash))
            elif pr is None:
                # Never probed — use fuzzy match score as weak fallback
                match_score = self._matcher.score(name, entry)
                scored_fallback.append((match_score, entry.hash))
            # else: pr is not None but errored or score=0 → candidate is DEAD, skip it

        if scored_probed:
            scored_probed.sort(key=lambda x: -x[0])
            return scored_probed[0][1]

        if scored_fallback:
            scored_fallback.sort(key=lambda x: -x[0])
            return scored_fallback[0][1]

        return None

    # ------------------------------------------------------------------
    # Background refinement (silence detection)
    # ------------------------------------------------------------------

    async def _refine_in_background(
        self,
        entries: list[AcestreamEntry],
        resolved: dict[str, str],
    ) -> None:
        """Check audio silence on resolved streams in the background.

        For each resolved name, runs ``check_silence()`` on the selected hash.
        If the stream is silent, probes the next best candidate and updates
        the cache.  The user never waits for this — results are available on
        the next template render.
        """
        for norm_name, current_hash in resolved.items():
            if not current_hash:
                continue

            # Silence check on the currently cached stream
            url = f"{self.config.acexy_base}/ace/getstream?id={current_hash}"
            is_silent, max_db = await self._prober.check_silence(url)

            if not is_silent:
                logger.debug(
                    "Background refine: '%s' (hash=%s…) audio OK (max_volume=%.1f dB)",
                    norm_name,
                    current_hash[:8],
                    max_db,
                )
                continue  # Stream is fine

            logger.warning(
                "Background refine: '%s' (hash=%s…) is SILENT (max_volume=%.1f dB) — searching alternatives",
                norm_name,
                current_hash[:8],
                max_db,
            )

            # Find alternative candidates for this name
            candidates = self._matcher.find_matches(norm_name, entries)
            alt_candidates = [c for c in candidates if c.hash != current_hash]

            if not alt_candidates:
                logger.info(
                    "Background refine: no alternative for '%s' — keeping silent stream",
                    norm_name,
                )
                continue

            # Probe the first alternative
            alt = alt_candidates[0]
            metadata_res = StreamProber._resolution_from_name(alt.name)
            pr = await self._prober.probe(
                hash=alt.hash,
                resolution_mode=self.config.resolution_mode,
                metadata_resolution=metadata_res,
            )

            if pr.error or pr.score <= 0:
                logger.info(
                    "Background refine: alternative %s… failed probe — keeping silent stream",
                    alt.hash[:8],
                )
                continue

            # Update cache with the working alternative
            resolution_str = (
                f"{pr.width}x{pr.height}"
                if pr.width and pr.height
                else "unknown"
            )
            channel = ResolvedChannel(
                channel_name=norm_name,
                hash=pr.hash,
                resolution=resolution_str,
                score=pr.score,
                cached_at=time.time(),
                expires_at=time.time() + self.config.refresh_interval,
            )
            self._cache.set(norm_name, channel)
            self._save_cache()
            logger.info(
                "Background refine: swapped silent stream %s… → %s… for '%s'",
                current_hash[:8],
                alt.hash[:8],
                norm_name,
            )

    # ------------------------------------------------------------------
    # Template utilities
    # ------------------------------------------------------------------

    @staticmethod
    def find_channel_names_in_template(template_path: str) -> list[str]:
        """Read an M3U template and extract all ``acestream_resolve('...')`` channel names.

        Returns deduplicated list (preserving first-occurrence order).
        """
        try:
            content = Path(template_path).read_text(encoding="utf-8")
        except (OSError, IOError) as e:
            logger.warning("Cannot read template %s: %s", template_path, e)
            return []

        matches = RESOLVE_CALL_RE.findall(content)
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in matches:
            key = m.strip().lower()
            if key not in seen:
                seen.add(key)
                deduped.append(m.strip())
        return deduped

    def _has_active_templates(self) -> bool:
        """Scan ``M3U_DIR`` for ``.m3u`` files containing ``acestream_resolve``.

        Returns ``True`` if at least one template uses the macro.
        Also updates internal ``_template_mtimes`` for change detection.
        """
        m3u_dir = Path(self.config.m3u_dir)
        if not m3u_dir.exists():
            return False
        found = False
        for f in m3u_dir.glob("*.m3u"):
            try:
                stat = f.stat()
                self._template_mtimes[f.name] = stat.st_mtime
                content = f.read_text(encoding="utf-8")
                if "acestream_resolve" in content:
                    found = True
            except Exception:
                continue
        return found

    def _templates_changed_since_last_check(self) -> bool:
        """Check if any template file was modified since last ``_has_active_templates()`` call.

        Lightweight stat-only check (no content reading).
        """
        m3u_dir = Path(self.config.m3u_dir)
        if not m3u_dir.exists():
            return False
        for f in m3u_dir.glob("*.m3u"):
            try:
                new_mtime = f.stat().st_mtime
                old_mtime = self._template_mtimes.get(f.name)
                if old_mtime is None or new_mtime > old_mtime:
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Cache persistence
    # ------------------------------------------------------------------

    def _save_cache(self) -> None:
        """Persist cache to YAML file so it survives container restarts."""
        try:
            data = {}
            for name in self._cache.get_all_names():
                entry = self._cache.get_fallback(name)
                if entry:
                    data[name] = {
                        "channel_name": entry.channel_name,
                        "hash": entry.hash,
                        "resolution": entry.resolution,
                        "score": entry.score,
                    }
            Path(self.config.cache_file).parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config.cache_file + ".tmp"
            with open(tmp, "w") as f:
                yaml.dump(
                    data,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            os.replace(tmp, self.config.cache_file)  # atomic on Unix
            logger.debug("Cache saved: %d entries", len(data))
        except Exception as e:
            logger.warning("Failed to save cache: %s", e)

    def _load_cache(self) -> None:
        """Load persisted cache from YAML (or legacy JSON) file."""
        try:
            path = Path(self.config.cache_file)
            if not path.exists():
                return
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            logger.info("Cache loaded from disk: %d entries", len(data))
        except Exception:
            # Legacy JSON fallback
            try:
                path = Path(str(self.config.cache_file).replace(".yaml", ".json"))
                if not path.exists():
                    return
                with open(path, "r") as f:
                    blob = json.load(f)
                data = blob.get("channels", {})
                logger.info("Cache loaded from legacy JSON: %d entries", len(data))
            except Exception:
                return

        now = time.time()
        loaded = 0
        for key, value in data.items():
            try:
                channel = ResolvedChannel(
                    channel_name=value.get("channel_name", key),
                    hash=value.get("hash", ""),
                    resolution=value.get("resolution", "unknown"),
                    score=value.get("score", 0.0),
                    cached_at=now,
                    expires_at=now + self.config.refresh_interval,
                )
                self._cache.set(key, channel)
                loaded += 1
            except Exception:
                continue
        if loaded:
            logger.info("Cache entries restored: %d", loaded)

    # ------------------------------------------------------------------
    # Background refresh
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Periodic background refresh loop.

        Polls template files every ``_TEMPLATE_POLL_INTERVAL`` seconds to
        detect when templates start or stop using ``acestream_resolve``.
        Only downloads sources and re-probes when active templates exist.
        Full source refresh happens at most once per ``refresh_interval``.

        When templates transition from inactive to active, the first
        refresh is triggered immediately.
        """
        time_since_refresh = 0  # start() already ran pre_resolve_all()
        was_active = False
        while not self._stop_event.is_set():
            try:
                # Quick check: if templates changed, re-evaluate active state
                if self._templates_changed_since_last_check():
                    logger.info("Template files changed — re-evaluating active state")
                    self._active_templates_known = None
                    # Trigger re-resolution on next cycle (new channels may have been added)
                    time_since_refresh = self.config.refresh_interval

                # Lazy-evaluate active state (cached between template changes)
                if self._active_templates_known is None:
                    self._active_templates_known = self._has_active_templates()

                # Trigger refresh immediately when templates become active
                if self._active_templates_known and not was_active:
                    logger.info("Templates now use acestream_resolve — starting background refresh")

                was_active = bool(self._active_templates_known)

                if self._active_templates_known:
                    # Full refresh only at configured interval
                    if time_since_refresh >= self.config.refresh_interval:
                        time_since_refresh = 0
                        await self.pre_resolve_all()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Refresh loop error: %s", e)

            await asyncio.sleep(_TEMPLATE_POLL_INTERVAL)
            time_since_refresh += _TEMPLATE_POLL_INTERVAL

    async def _refresh_cache(self) -> None:
        """Re-fetch sources.  If changed, re-probe all cached channels
        and auto-upgrade if better candidates appear.

        Uses ``fetch_all`` directly (which handles conditional requests
        internally) to avoid double-download.
        """
        source_manager = self._source_manager
        if not source_manager:
            return

        try:
            entries = await source_manager.fetch_all()
        except Exception as e:
            logger.warning("Refresh source fetch failed: %s", e)
            return

        if not entries:
            logger.debug("Refresh: no source changes detected")
            return

        logger.info("Source changed — re-probing cached channels")

        # Get all cached channel names (including expired — upgrade them too)
        cached_names = self._cache.get_all_names()
        if not cached_names:
            logger.debug("Refresh: no cached channels to re-probe")
            return

        # Re-resolve with fresh entries.
        # _resolve_inner updates the cache for successful re-resolutions.
        # Names that fail keep their old cached entries (not evicted).
        resolved = await self._resolve_inner(cached_names, entries)
        successful = sum(1 for v in resolved.values() if v)
        logger.info(
            "Cache refresh: %d/%d channels re-resolved",
            successful,
            len(cached_names),
        )

        # Also run silence checks in background (fire-and-forget)
        if resolved:
            asyncio.create_task(self._refine_in_background(entries, resolved))

    async def _check_sources_changed(self) -> bool:
        """Quick check: download sources with ETag/If-Modified-Since.

        Returns ``True`` if any source has new content.

        Note: this method mutates ``_source_state`` inside the source
        manager (on 200 responses), so subsequent calls to ``fetch_all``
        will see 304.  It is primarily useful for external monitoring;
        internal refresh uses ``fetch_all`` directly.
        """
        source_manager = self._source_manager
        if not source_manager:
            return False

        urls = source_manager.get_all_source_urls()
        if not urls:
            return False

        async def check_one(url: str) -> bool:
            try:
                content, _ = await source_manager.download_source(url)
                # content is None on 304 (no change), not-None on 200 (changed)
                return content is not None
            except Exception:
                return False

        results = await asyncio.gather(
            *[check_one(u) for u in urls],
            return_exceptions=True,
        )
        return any(r is True for r in results)
