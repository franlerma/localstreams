"""Acestream Hash Resolver — isolated package for resolving AceStream hashes by channel name.

This package has zero imports from the rest of LocalStreams. It communicates
with the app only through StreamResolver.start()/stop() lifecycle and the
resolve_batch() -> dict[str, str] interface.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AcestreamEntry:
    """A single entry parsed from an external M3U source."""
    hash: str                                   # 40-char hex
    name: str                                   # display name from EXTINF (after comma)
    tvg_name: Optional[str] = None              # tvg-name attribute
    tvg_id: Optional[str] = None                # tvg-id attribute
    tvg_resolution: Optional[str] = None        # e.g. "1080p", "FHD"
    source_url: Optional[str] = None            # which M3U source this came from


@dataclass
class ResolvedChannel:
    """A cached resolution result for one channel name."""
    channel_name: str                           # original lookup name (normalized)
    hash: str                                   # resolved 40-char hex
    resolution: str                             # human-readable: "720p", "1080p", "4K"
    score: float                                # composite score (pixels * stability)
    cached_at: float                            # time.time() when cached
    expires_at: float                           # cached_at + refresh_interval


@dataclass
class ProbeResult:
    """Result of probing a single AceStream candidate."""
    hash: str
    width: int                                  # 0 if resolution unknown
    height: int                                 # 0 if resolution unknown
    stable: bool
    score: float                                # (width * height) * stability_ratio
    avg_bitrate_bps: float = 0.0                # average bitrate from stability sample
    has_audio: bool = True                      # False if no audio stream detected
    error: Optional[str] = None


from .config import ResolverConfig
from .source import M3USourceManager
from .matcher import ChannelMatcher
from .prober import StreamProber
from .cache import ResolverCache
from .resolver import StreamResolver

__all__ = [
    "AcestreamEntry",
    "ResolvedChannel",
    "ProbeResult",
    "ResolverConfig",
    "M3USourceManager",
    "ChannelMatcher",
    "StreamProber",
    "ResolverCache",
    "StreamResolver",
]
