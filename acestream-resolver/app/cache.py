"""In-memory TTL cache for resolved AceStream channels.

Soft expiry: get() returns None after TTL — caller should re-resolve.
Hard expiry: entries are never deleted (get_fallback works even after TTL).
"""

import time
from typing import Optional

from models import ResolvedChannel


class ResolverCache:
    """In-memory TTL cache for resolved channels.

    Soft-expired entries are hidden from get() but still accessible via
    get_fallback() — useful when all resolution sources are unreachable.
    """

    def __init__(self, ttl: int = 1800) -> None:
        self._ttl = ttl
        self._cache: dict[str, ResolvedChannel] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(name: str) -> str:
        """Lowercase + strip for dict-key consistency."""
        return name.strip().lower()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[ResolvedChannel]:
        """Return cached entry if fresh (not expired). Returns None if expired or missing."""
        key = self._normalize(name)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            return None
        return entry

    def set(self, name: str, channel: ResolvedChannel) -> None:
        """Store entry. Normalize name as key. Overwrites cached_at/expires_at."""
        now = time.time()
        channel.cached_at = now
        channel.expires_at = now + self._ttl
        key = self._normalize(name)
        self._cache[key] = channel

    def get_all_names(self) -> list[str]:
        """Return all cached channel names (normalized keys)."""
        return list(self._cache.keys())

    def is_expired(self, name: str) -> bool:
        """Check if cached entry is soft-expired. Returns True if missing or expired."""
        key = self._normalize(name)
        entry = self._cache.get(key)
        if entry is None:
            return True
        return time.time() >= entry.expires_at

    def get_fallback(self, name: str) -> Optional[ResolvedChannel]:
        """Return entry even if expired. Never None if entry was ever cached."""
        key = self._normalize(name)
        return self._cache.get(key)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    def remove(self, name: str) -> None:
        """Remove single entry. No-op if name not cached."""
        key = self._normalize(name)
        self._cache.pop(key, None)
