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

        Parameters
        ----------
        name:
            Channel name to look up (e.g. ``"Movistar LaLiga"``).
        entries:
            Pool of known entries to search against.
        threshold:
            Minimum ``token_sort_ratio`` score (0‑100) for entries whose
            extra tokens are all quality suffixes.
        strict_threshold:
            Higher threshold for entries whose extra tokens include
            non‑quality words (channel numbers, different names).

        Returns
        -------
        list[AcestreamEntry]
            Matching entries sorted by score descending.  When two entries
            have the same score, the one whose ``tvg_name`` matches *name*
            exactly is preferred.
        """
        normalized_query = self._normalize(name)
        scored: list[tuple[int, int, AcestreamEntry]] = []

        for entry in entries:
            score = self.score(name, entry)

            # If the entry has extra tokens that aren't quality suffixes,
            # require a higher threshold to avoid accidental matches
            # (e.g. "DAZN LaLiga" → "DAZN LaLiga 2 1080p").
            effective = (
                strict_threshold
                if self._has_non_quality_extras(normalized_query, entry)
                else threshold
            )

            if score < effective:
                logger.debug(
                    "Unmatched entry: %s (score=%d, threshold=%d)",
                    entry.name,
                    score,
                    effective,
                )
                continue

            # Tiebreaker flag — exact tvg_name match wins ties.
            exact_tvg = 1 if self._tvg_matches_exactly(normalized_query, entry) else 0
            scored.append((score, exact_tvg, entry))

        # Descending by score, then by exact-tvg-name tiebreaker.
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [entry for _, _, entry in scored]

    def score(self, name: str, entry: AcestreamEntry) -> int:
        """Compute a match score (0‑100) between *name* and *entry*.

        Evaluates both ``entry.name`` and ``entry.tvg_name`` and returns
        the highest score of the two.
        """
        normalized_query = self._normalize(name)

        name_score = fuzz.token_sort_ratio(
            normalized_query,
            self._normalize(entry.name),
        )

        tvg_score = 0
        if entry.tvg_name:
            tvg_score = fuzz.token_sort_ratio(
                normalized_query,
                self._normalize(entry.tvg_name),
            )

        return max(name_score, tvg_score)

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
        """Return ``True`` when *entry*'s name or tvg_name contains extra
        tokens (beyond those in *query*) that are NOT quality suffixes.

        This prevents "DAZN LaLiga" from matching "DAZN LaLiga 2 1080p"
        while still allowing "La 1" → "La 1 HD".
        """
        query_tokens = set(normalized_query.split())

        for target_text in (entry.name, entry.tvg_name or ""):
            if not target_text:
                continue
            target_tokens = set(self._normalize(target_text).split())
            extra = target_tokens - query_tokens
            if extra and not extra.issubset(_QUALITY_TOKENS):
                return True

        return False

    def _tvg_matches_exactly(
        self, normalized_query: str, entry: AcestreamEntry
    ) -> bool:
        """Return ``True`` when *entry*'s ``tvg_name`` matches *normalized_query* exactly."""
        if not entry.tvg_name:
            return False
        return self._normalize(entry.tvg_name) == normalized_query
