"""StreamResolver — orchestrator for the acestream-hash-resolver service.

Coordinates source fetching, fuzzy matching, stream probing, caching,
and background cache refresh. This is the single public API entry point
for the resolver package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import yaml

import aiohttp

from models import AcestreamEntry, ProbeResult, ResolvedChannel
from cache import ResolverCache
from config import ResolverConfig
from matcher import ChannelMatcher
from prober import StreamProber
from source import M3USourceManager

logger = logging.getLogger("LocalStreams.resolver")

_REFRESH_INTERVAL = 60  # seconds between background refresh checks


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
        # Cached source entries for instant name-only resolution (no I/O)
        self._entries: list[AcestreamEntry] = []
        # Limit concurrent probes to avoid flooding acexy
        self._probe_semaphore = asyncio.Semaphore(2)
        # Track failed hashes so they get deprioritized on re-check
        self._failed_hashes: dict[str, float] = {}  # hash -> time() when failed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the resolver: create HTTP session, warm up sources,
        load persisted cache, start background refresh loop."""
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.download_timeout),
        )
        self._source_manager = M3USourceManager(self.config, self._http)
        self._prober = StreamProber(self.config, self._http)

        # Load persisted cache from disk
        self._load_cache()

        # Warm up source download so first on-demand resolve is fast
        if self._source_manager:
            try:
                entries = await self._source_manager.fetch_all()
                if entries:
                    self._entries = entries
                logger.info(
                    "Sources warmed up — %d entries from %d URLs",
                    len(self._entries),
                    len(self._source_manager.get_all_source_urls()),
                )
            except Exception as e:
                logger.warning("Initial source fetch failed: %s", e)

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

        This is the **only** method called from the request path.
        It never does I/O — instant return.
        """
        cached = self._cache.get(name)
        return cached.hash if cached else ""

    def _source_priority(self, source_url: Optional[str]) -> int:
        """Return priority index for a source URL (lower = better, 0 = best).
        Returns ``len(source_urls)`` for unknown sources.
        """
        if not source_url:
            return len(self.config.source_urls)
        for i, url in enumerate(self.config.source_urls):
            if url in source_url or source_url in url:
                return i
        return len(self.config.source_urls)

    async def resolve_by_name_only(self, names: list[str]) -> dict[str, str]:
        """Resolve names using only fuzzy matching against source entries.

        **No probing** — returns the best match by name similarity instantly.
        Results are cached so subsequent ``lookup_from_cache()`` calls hit.
        Background refresh will probe and refine quality.
        """
        if not self._entries:
            logger.warning("No source entries available for name resolution")
            return {name: "" for name in names}

        results = {}
        for name in names:
            norm_name = name.strip().lower()
            candidates = self._matcher.find_matches(norm_name, self._entries)
            if candidates:
                # Score by name + source priority + resolution
                scored_candidates = []
                for c in candidates:
                    s = self._matcher.score(norm_name, c)
                    sp = self._source_priority(c.source_url)
                    # Source boost: first source strongly preferred
                    source_boost = {0: 30, 1: 5}.get(sp, 0)
                    # Penalize hashes that failed recently
                    fail_time = self._failed_hashes.get(c.hash)
                    if fail_time and (time.time() - fail_time) < self.config.refresh_interval:
                        s -= 100
                    w, h = StreamProber._resolution_from_name(c.name)
                    pixels = w * h if w and h else 0
                    scored_candidates.append((s, sp, source_boost, pixels, c))

                # Sort: best name score → source boost → highest resolution
                scored_candidates.sort(key=lambda x: (-x[0], -x[2], -x[3]))
                best = scored_candidates[0][4]
                results[name] = best.hash

                # Log all candidates
                for i, (s, sp, boost, px, c) in enumerate(scored_candidates[:5]):
                    w, h = StreamProber._resolution_from_name(c.name)
                    res_str = f"{w}x{h}" if w and h else "?"
                    src = c.source_url.split("/")[-1] if c.source_url else "?"
                    tag = " ← SELECTED" if c == best else ""
                    logger.info(
                        "  Candidate %d: '%s' → %s (name='%s', score=%d, res=%s, source=%s)%s",
                        i + 1,
                        name,
                        c.hash[:8],
                        c.name,
                        s,
                        res_str,
                        src,
                        tag,
                    )
                if len(scored_candidates) > 5:
                    logger.info("  ... and %d more candidates", len(scored_candidates) - 5)

                # Cache so later lookups are instant
                channel = ResolvedChannel(
                    channel_name=norm_name,
                    hash=best.hash,
                    resolution=best.tvg_resolution or "unknown",
                    score=0.5,  # tentative — updated by background probe
                    cached_at=time.time(),
                    expires_at=time.time() + self.config.refresh_interval,
                )
                self._cache.set(norm_name, channel)
            else:
                results[name] = ""

        matched = sum(1 for v in results.values() if v)
        logger.info(
            "Name-only resolve: %d/%d matched (0 probes)",
            matched,
            len(names),
        )
        return results

    async def resolve_with_probe(self, names: list[str]) -> dict[str, str]:
        """Resolve names: fuzzy match + quick probe of top candidate.

        Probes the best name+resolution candidate. If it responds, uses it.
        If not, falls back to the next candidate. If all fail, keeps the
        best name match as fallback (better than nothing).
        """
        if not self._entries:
            return {name: "" for name in names}
        if self._prober is None:
            return await self.resolve_by_name_only(names)

        results = {}
        for name in names:
            norm_name = name.strip().lower()
            candidates = self._matcher.find_matches(norm_name, self._entries)
            if not candidates:
                results[name] = ""
                continue

            # Score by name + source priority + resolution
            scored = []
            for c in candidates:
                s = self._matcher.score(norm_name, c)
                sp = self._source_priority(c.source_url)
                source_boost = {0: 30, 1: 5}.get(sp, 0)
                w, h = StreamProber._resolution_from_name(c.name)
                pixels = w * h if w and h else 0
                scored.append((s, sp, source_boost, pixels, c))
            scored.sort(key=lambda x: (-x[0], -x[2], -x[3]))

            # Log all candidates
            logger.info("  Probing candidates for '%s':", name)
            for i, (s, sp, boost, px, c) in enumerate(scored[:5]):
                w, h = StreamProber._resolution_from_name(c.name)
                res_str = f"{w}x{h}" if w and h else "?"
                src = c.source_url.split("/")[-1] if c.source_url else "?"
                logger.info(
                    "    Candidate %d: %s (name='%s', score=%d, src_prio=%d, res=%s, source=%s)",
                    i + 1, c.hash[:8], c.name, s, sp, res_str, src,
                )

            # Quick-probe top candidates until one works (semaphore limits concurrency)
            selected = None
            probed_hashes = set()
            for s, sp, boost, px, c in scored:
                if c.hash in probed_hashes:
                    continue
                probed_hashes.add(c.hash)
                try:
                    async with self._probe_semaphore:
                        pr = await asyncio.wait_for(
                            self._prober.probe(
                                hash=c.hash,
                                resolution_mode=self.config.resolution_mode,
                            ),
                            timeout=min(self.config.probe_timeout, 8),
                        )
                    if pr is not None and pr.error is None and pr.score > 0:
                        # Verify bitrate is reasonable for this resolution
                        if pr.width > 0 and pr.height > 0:
                            pixels = pr.width * pr.height
                            min_bps = (pixels / (1920 * 1080)) * 500_000
                            if pr.avg_bitrate_bps < min_bps:
                                logger.info(
                                    "    ❌ %s low bitrate (%.0f bps < %.0f) — trying next",
                                    c.hash[:8], pr.avg_bitrate_bps, min_bps,
                                )
                                continue
                        selected = c
                        res_str = f"{pr.width}x{pr.height}" if pr.width and pr.height else "?"
                        logger.info(
                            "    ✅ %s works! (res=%s, bitrate=%.0f, score=%.0f)",
                            c.hash[:8], res_str, pr.avg_bitrate_bps, pr.score,
                        )
                        # Cache with real probe data
                        channel = ResolvedChannel(
                            channel_name=norm_name,
                            hash=c.hash,
                            resolution=res_str,
                            score=pr.score,
                            cached_at=time.time(),
                            expires_at=time.time() + self.config.refresh_interval,
                        )
                        self._cache.set(norm_name, channel)
                        break
                except (asyncio.TimeoutError, Exception) as e:
                    err_type = type(e).__name__
                    err_msg = str(e) or "(no message)"
                    logger.info("    ❌ %s failed [%s]: %s", c.hash[:8], err_type, err_msg)

            if selected is None:
                # Fallback: best name match (unverified)
                fallback = scored[0][4]
                logger.warning(
                    "    No working stream for '%s' — using unverified %s",
                    name, fallback.hash[:8],
                )
                results[name] = fallback.hash
                channel = ResolvedChannel(
                    channel_name=norm_name,
                    hash=fallback.hash,
                    resolution="unknown",
                    score=0.5,
                    cached_at=time.time(),
                    expires_at=time.time() + self.config.refresh_interval,
                )
                self._cache.set(norm_name, channel)
            else:
                results[name] = selected.hash

        matched = sum(1 for v in results.values() if v)
        logger.info("Resolve with probe: %d/%d verified", matched, len(names))
        return results

    async def probe_and_refine(
        self,
        names: list[str],
        name_only_results: dict[str, str],
    ) -> None:
        """Background probe to verify and refine name-only resolution.

        Called as a fire-and-forget task after returning the instant
        name-only result.  Probes the selected hash for each name.
        If it fails, tries alternative candidates from source entries.
        Updates the cache if a better candidate is found.
        """
        if not self._entries or not self._prober:
            return

        logger.info("Background probe: verifying %d resolved name(s)", len(name_only_results))

        probed_hashes: set[str] = set()

        for name in names:
            norm_name = name.strip().lower()
            current_hash = name_only_results.get(name, "")
            if not current_hash:
                continue
            if current_hash in probed_hashes:
                logger.debug("  Background: %s already probed (synonym), skipping", current_hash[:8])
                continue
            probed_hashes.add(current_hash)

            # Quick probe of the current hash (8s timeout, semaphore limits concurrency)
            try:
                async with self._probe_semaphore:
                    pr = await asyncio.wait_for(
                        self._prober.probe(
                            hash=current_hash,
                            resolution_mode=self.config.resolution_mode,
                        ),
                        timeout=min(self.config.probe_timeout, 8),
                    )
                if pr is not None and pr.error is None and pr.score > 0:
                    logger.info(
                        "  ✅ Background: %s verified (res=%dx%d, score=%.0f)",
                        current_hash[:8], pr.width, pr.height, pr.score,
                    )
                    # Cache with verified score
                    self._cache.set(norm_name, ResolvedChannel(
                        channel_name=norm_name,
                        hash=current_hash,
                        resolution=f"{pr.width}x{pr.height}" if pr.width and pr.height else "unknown",
                        score=pr.score,
                        cached_at=time.time(),
                        expires_at=time.time() + self.config.refresh_interval,
                    ))
                else:
                    logger.info("  ❌ Background: %s failed probe — checking alternatives", current_hash[:8])
                    self._failed_hashes[current_hash] = time.time()
                    await self._try_alternatives(norm_name)
            except (asyncio.TimeoutError, Exception) as e:
                err_type = type(e).__name__
                err_msg = str(e) or "(no message)"
                logger.info(
                    "  ❌ Background: %s probe error [%s]: %s — checking alternatives",
                    current_hash[:8], err_type, err_msg,
                )
                self._failed_hashes[current_hash] = time.time()
                await self._try_alternatives(norm_name)

    async def _try_alternatives(self, norm_name: str) -> None:
        """Try alternative candidates when the primary hash fails probing."""
        if not self._entries or not self._prober:
            return
        candidates = self._matcher.find_matches(norm_name, self._entries)
        if not candidates:
            return
        # Score by name + source + resolution
        scored = []
        for c in candidates:
            s = self._matcher.score(norm_name, c)
            sp = self._source_priority(c.source_url)
            source_boost = max(0, (len(self.config.source_urls) - sp) * 5 - 5)
            fail_time = self._failed_hashes.get(c.hash)
            if fail_time and (time.time() - fail_time) < self.config.refresh_interval:
                s -= 100
            w, h = StreamProber._resolution_from_name(c.name)
            scored.append((s, sp, source_boost, w * h if w and h else 0, c))
        scored.sort(key=lambda x: (-x[0], -x[2], -x[3]))

        # Probe alternatives until one works
        for s, sp, boost, px, alt in scored:
            if self._cache.get(norm_name) and self._cache.get(norm_name).hash == alt.hash:
                continue  # already cached and verified
            try:
                async with self._probe_semaphore:
                    pr = await asyncio.wait_for(
                        self._prober.probe(
                            hash=alt.hash,
                            resolution_mode=self.config.resolution_mode,
                        ),
                        timeout=min(self.config.probe_timeout, 8),
                    )
                if pr is not None and pr.error is None and pr.score > 0:
                    logger.info(
                        "  ✅ Background: alternative %s works! Upgrading '%s'",
                        alt.hash[:8], norm_name,
                    )
                    self._cache.set(norm_name, ResolvedChannel(
                        channel_name=norm_name,
                        hash=alt.hash,
                        resolution=f"{pr.width}x{pr.height}" if pr.width and pr.height else "unknown",
                        score=pr.score,
                        cached_at=time.time(),
                        expires_at=time.time() + self.config.refresh_interval,
                    ))
                    return
                else:
                    self._failed_hashes[alt.hash] = time.time()
            except (asyncio.TimeoutError, Exception):
                self._failed_hashes[alt.hash] = time.time()
                continue
        logger.warning("  Background: no working alternative for '%s'", norm_name)

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
            else:
                logger.info("Resolution: no candidates for '%s'", name)

        if not all_candidate_hashes:
            logger.info(
                "Resolution: no matching candidates found for %d missing names",
                len(missing),
            )
            return {}

        # 2. Probe all unique candidate hashes in parallel
        logger.info(
            "Probing %d unique hashes for %d names",
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

            # Cache successful resolutions
            if best_hash is not None:
                resolved[name] = best_hash
                pr = probe_results.get(best_hash)
                if pr is not None:
                    resolution_str = (
                        f"{pr.width}x{pr.height}" if pr.width and pr.height else "unknown"
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
                    logger.info(
                        "Resolved '%s' → %s (%s, score=%.0f)",
                        name, best_hash[:8], resolution_str, pr.score,
                    )
                else:
                    resolved[name] = best_hash
            else:
                # All probed candidates failed — keep cached hash as fallback
                fallback = self._cache.get_fallback(name)
                if fallback:
                    resolved[name] = fallback.hash
                    logger.info(
                        "All probes failed for '%s' — keeping cached fallback %s",
                        name, fallback.hash[:8],
                    )
                else:
                    resolved[name] = ""
                    logger.warning("All probes failed for '%s' — no cached fallback", name)

        return resolved

    async def _probe_candidate(
        self,
        hash: str,
        name_to_candidates: dict[str, list[AcestreamEntry]],
    ) -> Optional[ProbeResult]:
        """Probe a single hash, deriving metadata resolution from entry name heuristics."""
        if self._prober is None:
            return None

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

        async with self._probe_semaphore:
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
                scored_probed.append((pr.score, entry.hash))
            elif pr is None:
                match_score = self._matcher.score(name, entry)
                scored_fallback.append((match_score, entry.hash))

        if scored_probed:
            scored_probed.sort(key=lambda x: -x[0])
            winner = scored_probed[0]
            if len(scored_probed) > 1:
                logger.info(
                    "  Pick for '%s': probed winner %s… (score=%.0f), runner-up %s… (score=%.0f)",
                    name,
                    winner[1][:8], winner[0],
                    scored_probed[1][1][:8], scored_probed[1][0],
                )
            return winner[1]

        if scored_fallback:
            scored_fallback.sort(key=lambda x: -x[0])
            winner = scored_fallback[0]
            logger.info(
                "  Pick for '%s': no probes available, using name-match %s… (score=%d)",
                name,
                winner[1][:8], winner[0],
            )
            return winner[1]

        logger.warning("  Pick for '%s': no viable candidate from %d entries", name, len(entries))
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
        the next request.
        """
        for norm_name, current_hash in resolved.items():
            if not current_hash:
                continue

            url = f"{self.config.acexy_base}/ace/getstream?id={current_hash}"
            is_silent, max_db = await self._prober.check_silence(url)

            swap_reason = None
            if is_silent:
                swap_reason = f"SILENT (max_volume={max_db:.1f} dB)"
            else:
                cached_entry = self._cache.get_fallback(norm_name)
                width, height = 1280, 720
                if cached_entry:
                    res_parts = cached_entry.resolution.split("x") if "x" in cached_entry.resolution else []
                    if len(res_parts) == 2:
                        try:
                            width, height = int(res_parts[0]), int(res_parts[1])
                        except ValueError:
                            pass

                has_artifacts, quality = await self._prober.assess_quality(
                    current_hash, width, height, 0.0,
                )

                if has_artifacts:
                    swap_reason = f"visual artifacts (quality={quality.get('composite', 0.0):.2f})"
                else:
                    logger.debug(
                        "Background refine: '%s' OK (hash=%s… audio=OK visual=OK)",
                        norm_name, current_hash[:8],
                    )
                    continue

            logger.warning(
                "Background refine: '%s' (hash=%s…) %s — searching alternatives",
                norm_name,
                current_hash[:8],
                swap_reason,
            )

            candidates = self._matcher.find_matches(norm_name, entries)
            alt_candidates = [c for c in candidates if c.hash != current_hash]

            if not alt_candidates:
                logger.info(
                    "Background refine: no alternative for '%s' — keeping silent stream",
                    norm_name,
                )
                continue

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

        Re-fetches sources and re-probes all cached channels at most once
        per ``refresh_interval``.  Pure periodic — no template file scanning.
        """
        time_since_refresh = 0
        while not self._stop_event.is_set():
            try:
                if time_since_refresh >= self.config.refresh_interval:
                    time_since_refresh = 0
                    await self._refresh_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Refresh loop error: %s", e)

            await asyncio.sleep(_REFRESH_INTERVAL)
            time_since_refresh += _REFRESH_INTERVAL

    async def _refresh_cache(self) -> None:
        """Re-fetch sources.  If changed, re-probe all cached channels
        and auto-upgrade if better candidates appear.
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

        # Update cached entries for name-only resolution
        self._entries = entries

        logger.info("Source changed — resolving all names from sources")

        # Collect ALL unique names from source entries (including new ones)
        all_source_names = list({
            e.name.strip().lower() for e in entries if e.name and e.name.strip()
        })

        if not all_source_names:
            logger.debug("Refresh: no names found in source entries")
            return

        # Only resolve names that are expired or not yet cached
        names_to_resolve = [n for n in all_source_names if self._cache.is_expired(n)]

        if not names_to_resolve:
            logger.debug("Refresh: all %d names already fresh", len(all_source_names))
            return

        resolved = await self._resolve_inner(names_to_resolve, entries)
        successful = sum(1 for v in resolved.values() if v)
        logger.info(
            "Cache refresh: %d/%d channels resolved (cache has %d total)",
            successful,
            len(names_to_resolve),
            len(all_source_names),
        )

        if resolved:
            asyncio.create_task(self._refine_in_background(entries, resolved))

    async def _check_sources_changed(self) -> bool:
        """Quick check: download sources with ETag/If-Modified-Since.

        Returns ``True`` if any source has new content.
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
                return content is not None
            except Exception:
                return False

        results = await asyncio.gather(
            *[check_one(u) for u in urls],
            return_exceptions=True,
        )
        return any(r is True for r in results)
