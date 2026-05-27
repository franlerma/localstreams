"""Fuzzy channel-name matcher — tolerant matching against M3U entries.

Uses rapidfuzz to match channel names with tolerance for typos, acronyms,
word order, and extra suffixes like "HD" / "FHD".  Raises the threshold
when the target contains extra tokens that are NOT quality suffixes (e.g.
a channel number like "2"), so "DAZN LaLiga" won't accidentally match
"DAZN LaLiga 2 1080p".
"""

from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz, utils as fuzz_utils

from . import AcestreamEntry

logger = logging.getLogger("LocalStreams.resolver.matcher")

# Quality/resolution suffixes that are safe to ignore when matching —
# they indicate stream quality, not a different channel.
_QUALITY_TOKENS: set[str] = {
    "hd", "fhd", "uhd", "sd", "4k", "8k",
    "1080p", "720p", "480p", "2160p",
    "hevc", "h264", "h265", "hdr",
    "50fps", "60fps",
}


class ChannelMatcher:
    """Fuzzy-matches channel names against AcestreamEntry names."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_matches(
        self,
        name: str,
        entries: list[AcestreamEntry],
        threshold: int = 70,
        strict_threshold: int = 85,
    ) -> list[AcestreamEntry]:
        """Find entries matching *name*, sorted by descending score.

        **Two‑phase matching**: first scores against ``entry.name`` and
        ``entry.tvg_name``.  If nothing passes, falls back to ``entry.tvg_id``
        to catch entries whose display name is cluttered with batch labels.
        """
        normalized_query = self._normalize(name)

        # --- Phase 1: name + tvg_name ---
        candidates = self._score_entries(
            normalized_query, entries, threshold, strict_threshold, use_tvg_id=False
        )
        if candidates:
            return candidates

        # --- Phase 2: fallback to tvg_id ---
        logger.info(
            "No name/tvg_name match for '%s' — trying tvg_id",
            normalized_query,
        )
        candidates = self._score_entries(
            normalized_query, entries, threshold, strict_threshold, use_tvg_id=True
        )
        return candidates

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _score_entries(
        self,
        normalized_query: str,
        entries: list[AcestreamEntry],
        threshold: int,
        strict_threshold: int,
        *,
        use_tvg_id: bool,
    ) -> list[AcestreamEntry]:
        """Score and filter entries, returning sorted list of matches."""
        scored: list[tuple[int, int, AcestreamEntry]] = []

        for entry in entries:
            score = self._score_entry(normalized_query, entry, use_tvg_id=use_tvg_id)

            # If the entry has extra tokens with digits (channel numbers
            # like "2", not batch labels like "new era"), require higher
            # threshold to avoid accidental matches.
            effective = (
                strict_threshold
                if self._has_non_quality_extras(normalized_query, entry)
                else threshold
            )

            if score < effective:
                if score > 40:
                    logger.warning(
                        "Unmatched: query='%s' vs entry='%s' tvg='%s' tvg_id='%s' — score=%d threshold=%d",
                        normalized_query,
                        self._normalize(entry.name),
                        self._normalize(entry.tvg_name) if entry.tvg_name else "",
                        self._normalize_id(entry.tvg_id) if entry.tvg_id else "",
                        score,
                        effective,
                    )
                continue

            # Tiebreaker — exact tvg_name match wins ties.
            exact_tvg = 1 if self._tvg_matches_exactly(normalized_query, entry) else 0
            scored.append((score, exact_tvg, entry))

        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [entry for _, _, entry in scored]

    def _score_entry(
        self,
        normalized_query: str,
        entry: AcestreamEntry,
        *,
        use_tvg_id: bool,
    ) -> int:
        """Score a single entry, optionally including tvg_id."""
        scores = [
            fuzz.token_sort_ratio(normalized_query, self._normalize(entry.name)),
        ]
        if entry.tvg_name:
            scores.append(
                fuzz.token_sort_ratio(normalized_query, self._normalize(entry.tvg_name)),
            )
        if use_tvg_id and entry.tvg_id:
            scores.append(
                fuzz.token_sort_ratio(normalized_query, self._normalize_id(entry.tvg_id)),
            )
        return max(scores)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize(self, text: str) -> str:
        """Normalize *text* for fuzzy matching.

        Steps
        -----
        1. ``fuzz_utils.default_process`` — lowercase + strip.
        2. Remove any remaining non‑alphanumeric characters (keep spaces).
        3. Collapse whitespace runs into a single space; strip edges.
        """
        normalized = fuzz_utils.default_process(text)
        normalized = re.sub(r"[^a-z0-9\s]", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _has_non_quality_extras(
        self, normalized_query: str, entry: AcestreamEntry
    ) -> bool:
        """Return ``True`` when extra tokens include digits — indicating
        a potentially different channel ("DAZN LaLiga 2" vs "DAZN LaLiga").

        Batch labels like "new era", "elcano", "new loop iii" are safe
        because they don't change the channel identity.
        """
        query_tokens = set(normalized_query.split())

        for target_text in (entry.name, entry.tvg_name or ""):
            if not target_text:
                continue
            target_tokens = set(self._normalize(target_text).split())
            extra = target_tokens - query_tokens
            # Only worry about non-quality tokens that contain digits
            dangerous = {t for t in extra if t not in _QUALITY_TOKENS and bool(re.search(r"\d", t))}
            if dangerous:
                return True

        return False

    # keep public wrapper for external callers (resolver._pick_best_hash)

    def score(self, name: str, entry: AcestreamEntry) -> int:
        """Public wrapper — scores with tvg_id included (fallback mode)."""
        return self._score_entry(self._normalize(name), entry, use_tvg_id=True)

    def _normalize_id(self, text: str) -> str:
        """Normalize a tvg-id: lowercase, strip country TLD and dots."""
        _id = text.lower().strip()
        _id = re.sub(r"\.(es|sp|uk|us|com|net|org|tv|fr|de|it|pt|mx|ar|cl|pe|co|ve|ec|bo|py|uy)$", "", _id)
        _id = re.sub(r"[^a-z0-9\s]", "", _id)
        return _id

    def _tvg_matches_exactly(
        self, normalized_query: str, entry: AcestreamEntry
    ) -> bool:
        """Return ``True`` when *entry*'s ``tvg_name`` matches *normalized_query* exactly."""
        if not entry.tvg_name:
            return False
        return self._normalize(entry.tvg_name) == normalized_query
