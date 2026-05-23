"""Resolver configuration — reads ACESTREAM_RESOLVER_* env vars directly.

Isolated from app.config: this module uses os.getenv so the resolver package
remains fully independent of the rest of LocalStreams.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResolverConfig:
    """Configuration for the Acestream Hash Resolver.

    All values come from environment variables with sensible defaults.
    """
    sources: str = field(default_factory=lambda: os.getenv("ACESTREAM_RESOLVER_SOURCES", ""))
    """Comma-separated URLs of external M3U lists."""

    refresh_interval: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_RESOLVER_REFRESH_INTERVAL", "1800"))
    )
    """Seconds between periodic source re-fetches."""

    download_timeout: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_RESOLVER_DOWNLOAD_TIMEOUT", "10"))
    )
    """Timeout in seconds for downloading a single external M3U list."""

    probe_timeout: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_RESOLVER_PROBE_TIMEOUT", "20"))
    )
    """Timeout in seconds for probing a single stream candidate (includes engine cold‑start time)."""

    total_timeout: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_RESOLVER_TOTAL_TIMEOUT", "30"))
    )
    """Total timeout in seconds for resolving a full batch of channels."""

    stability_sample: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_RESOLVER_STABILITY_SAMPLE", "5"))
    )
    """Seconds of bitrate sampling for stability measurement."""

    resolution_mode: str = field(
        default_factory=lambda: os.getenv("ACESTREAM_RESOLVER_RESOLUTION_MODE", "probe")
    )
    """Resolution detection mode: 'probe' (ffprobe) or 'metadata' (tvg-attributes / name heuristic)."""

    acexy_host: str = field(
        default_factory=lambda: os.getenv("ACESTREAM_PROXY_HOST", "127.0.0.1")
    )
    """AceXY host for stream probing."""

    acexy_port: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_PROXY_PORT", "8080"))
    )
    """AceXY port for stream probing."""

    app_port: int = field(
        default_factory=lambda: int(os.getenv("ACESTREAM_APP_PORT", "15123"))
    )
    """LocalStreams app port. Used to probe streams through our own proxy
    instead of directly against AceXY, so the built-in retry/patience logic
    gives cold engines time to find peers."""

    m3u_dir: str = field(
        default_factory=lambda: os.getenv("APP_M3U_DIR", "/data/m3u")
    )
    """Directory containing M3U template files."""

    cache_file: str = field(
        default_factory=lambda: os.getenv(
            "ACESTREAM_RESOLVER_CACHE_FILE",
            "/data/m3u/.resolver_cache.yaml",
        )
    )
    """Path to persistent cache YAML file (survives container restarts, human‑editable)."""

    @property
    def source_urls(self) -> list[str]:
        """Returns the list of source URLs, stripped and filtered."""
        return [u.strip() for u in self.sources.split(",") if u.strip()]

    @property
    def acexy_base(self) -> str:
        """Returns the AceXY base URL for stream probing."""
        return f"http://{self.acexy_host}:{self.acexy_port}"
