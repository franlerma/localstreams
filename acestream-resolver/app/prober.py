"""Probes AceStream candidates for resolution and stability.

Resolution is detected via ffprobe subprocess (mode ``"probe"``) or falls
back to caller-provided metadata heuristics.

Stability is measured by sampling chunked HTTP reads over
:attr:`ResolverConfig.stability_sample` seconds.

Score is ``(width * height) * stability_ratio``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import aiohttp

from models import ProbeResult
from config import ResolverConfig

logger = logging.getLogger("LocalStreams.resolver.prober")

# ---- helpers -----------------------------------------------------------

def _bitrate_ok(bitrate_bps: float, pixels: int) -> tuple[bool, float]:
    """Check whether *bitrate_bps* is adequate for *pixels*.

    Returns ``(ok, score)`` where *score* is a 0‑1 quality estimate.
    """
    if bitrate_bps <= 0 or pixels <= 0:
        return False, 0.5

    # Reasonable minimums:  2 Mbps for 1080p, 1 Mbps for 720p, etc.
    min_bps = (pixels / (1920 * 1080)) * 2_000_000
    ratio = bitrate_bps / min_bps

    if ratio >= 1.0:
        return True, 1.0
    if ratio >= 0.6:
        return True, 0.7
    if ratio >= 0.3:
        return True, 0.4
    return False, 0.1

# ---- StreamProber -----------------------------------------------------
class StreamProber:
    """Probes AceStream candidates for resolution and stability.

    Parameters
    ----------
    config:
        Resolver configuration — provides AceXY base URL, timeouts, and
        stability sample duration.
    http_session:
        Shared ``aiohttp.ClientSession`` used for all HTTP requests
        (bitrate sampling / stability checks).
    """

    def __init__(
        self, config: ResolverConfig, http_session: aiohttp.ClientSession
    ) -> None:
        self.config = config
        self.http = http_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def probe(
        self,
        hash: str,
        resolution_mode: str = "probe",
        metadata_resolution: tuple[int, int] = (0, 0),
    ) -> ProbeResult:
        """Probe a single stream candidate.

        Runs resolution detection and stability sampling concurrently.
        When *resolution_mode* is ``"metadata"``, or when ffprobe returns
        ``(0, 0)``, the *metadata_resolution* is used instead (falling
        back to ``1280×720`` if not provided).

        Parameters
        ----------
        hash:
            40‑character AceStream content hash.
        resolution_mode:
            ``"probe"`` (default) — use ffprobe subprocess.
            ``"metadata"`` — skip ffprobe and rely on *metadata_resolution*.
        metadata_resolution:
            Resolution guessed from channel metadata (e.g. ``(1920, 1080)``).
            Ignored when *resolution_mode* is ``"probe"`` **and** ffprobe
            succeeds.

        Returns
        -------
        ProbeResult
            Resolution, stability, and composite score.  ``error`` is set
            to ``"connection failed"`` when the stream was unreachable and
            no resolution could be determined.
        """
        url = f"{self.config.localstreams_base_url}/acestream/video?id={hash}"

        # Run resolution+audio and stability checks concurrently.
        # Probing through localstreams' own proxy (not acexy directly) so
        # the built-in retry/patience logic gives cold engines time to start.
        (width, height, has_audio), (stability_ratio, stable, avg_bitrate_bps) = await asyncio.gather(
            self._probe_resolution(url),
            self._measure_stability(url),
        )

        # Apply metadata / default resolution fallback (stream is alive
        # but cold — engine may not have data yet, so probes returned 0).
        if resolution_mode == "metadata" or (width == 0 and height == 0):
            if metadata_resolution != (0, 0):
                width, height = metadata_resolution
            else:
                width, height = 1280, 720

        # For cold streams, if stability came back 0 but the server is
        # alive, give a small tentative score so the candidate isn't killed.
        if stability_ratio == 0.0:
            stability_ratio = 0.1  # tentative — may improve when engine warms up

        # Score: resolution * stability * audio_penalty
        audio_factor = 1.0 if has_audio else 0.5
        score = (width * height) * stability_ratio * audio_factor

        return ProbeResult(
            hash=hash,
            width=width,
            height=height,
            stable=stable,
            has_audio=has_audio,
            score=score,
            avg_bitrate_bps=avg_bitrate_bps,
        )

    # ------------------------------------------------------------------
    # Resolution detection
    # ------------------------------------------------------------------

    async def _probe_resolution(self, url: str) -> tuple[int, int, bool]:
        """Detect stream resolution and audio presence via ``ffprobe`` subprocess.

        Runs::

            ffprobe -v quiet -print_format json -show_streams \\
                    -read_intervals '%+5' <url>

        and extracts the first video stream's width/height and checks
        for at least one audio stream.

        Returns
        -------
        tuple[int, int, bool]
            ``(width, height, has_audio)`` or ``(0, 0, False)`` on failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-read_intervals",
                "%+5",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, asyncio.SubprocessError) as exc:
            logger.warning("Failed to start ffprobe for %s: %s", url, exc)
            return 0, 0, False

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.probe_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("ffprobe timed out for %s", url)
            return 0, 0, False

        try:
            data = json.loads(stdout)
            streams: list[dict] = data.get("streams", [])
            width, height = 0, 0
            has_audio = False
            for stream in streams:
                codec_type = stream.get("codec_type")
                if codec_type == "video":
                    width = stream.get("width", 0) or 0
                    height = stream.get("height", 0) or 0
                elif codec_type == "audio":
                    has_audio = True

            if width == 0 and height == 0:
                logger.debug("No video stream found in ffprobe output for %s", url)

            return width, height, has_audio

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse ffprobe output for %s: %s", url, exc)
            return 0, 0, False

    # ------------------------------------------------------------------
    # Stability measurement
    # ------------------------------------------------------------------

    async def _measure_stability(self, url: str) -> tuple[float, bool, float]:
        """Measure stream stability via aiohttp chunked reads.

        Connects to *url* and reads chunks in 1‑second intervals for
        :attr:`ResolverConfig.stability_sample` seconds.  An interval is
        counted as "active" when at least one byte was received during it.

        Returns
        -------
        tuple[float, bool, float]
            ``(stability_ratio, is_stable, avg_bitrate_bps)``
        """
        total_intervals = self.config.stability_sample
        timeout = aiohttp.ClientTimeout(total=self.config.probe_timeout)

        try:
            async with self.http.get(url, timeout=timeout) as response:
                if response.status != 200:
                    logger.warning(
                        "Stability check: HTTP %d for %s", response.status, url
                    )
                    return 0.0, False, 0.0

                intervals_with_data = 0
                start = time.monotonic()
                bytes_in_interval = 0
                total_bytes = 0
                current_interval = 0

                async for chunk in response.content.iter_chunked(8192):
                    bytes_in_interval += len(chunk)
                    total_bytes += len(chunk)
                    elapsed = time.monotonic() - start
                    interval = int(elapsed)

                    # Crossed a 1‑second boundary — tally the previous interval.
                    if interval > current_interval:
                        if bytes_in_interval > 0:
                            intervals_with_data += 1
                        bytes_in_interval = 0
                        current_interval = interval

                    # Sampled long enough — stop reading.
                    if interval >= total_intervals:
                        break

                # Account for any data received in the final partial interval.
                if current_interval < total_intervals and bytes_in_interval > 0:
                    intervals_with_data += 1

                stability_ratio = intervals_with_data / total_intervals
                is_stable = stability_ratio >= 0.5
                elapsed_total = time.monotonic() - start
                avg_bitrate_bps = (total_bytes * 8) / max(elapsed_total, 0.1)
                return stability_ratio, is_stable, avg_bitrate_bps

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Stability check failed for %s: %s", url, exc)
            return 0.0, False, 0.0

    # ------------------------------------------------------------------
    # Quality assessment (background refinement)
    # ------------------------------------------------------------------

    async def assess_quality(self, hash: str, width: int, height: int, avg_bitrate_bps: float) -> tuple[bool, dict[str, float]]:
        """Run all three quality checks and return a verdict.

        Returns ``(has_artifacts, details)`` where *details* contains each
        check's score (0 = worst, 1 = best).  A stream is flagged for
        artifacts when the weighted composite falls below 0.5.
        """
        url = f"{self.config.localstreams_base_url}/acestream/video?id={hash}"
        pixels = width * height

        # 1. Bitrate vs. resolution (40% weight)
        br_ok, br_score = _bitrate_ok(avg_bitrate_bps, pixels)

        # 2. SSIM structural similarity (40% weight)
        ssim_score = await self._check_ssim(url)

        # 3. Grid / blocking detection (20% weight)
        grid_score = await self._check_blocking(url)

        composite = 0.4 * br_score + 0.4 * ssim_score + 0.2 * grid_score
        has_artifacts = composite < 0.5

        details = {
            "bitrate_score": br_score,
            "bitrate_bps": avg_bitrate_bps,
            "ssim_score": ssim_score,
            "grid_score": grid_score,
            "composite": composite,
        }
        return has_artifacts, details

    async def _check_ssim(self, url: str) -> float:
        """Run ffmpeg SSIM filter on 3 seconds of video.

        Returns a 0‑1 score (higher = better).  Falls back to 0.5 on failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-t", "3", "-i", url,
                "-vf", "ssim", "-f", "null", "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, asyncio.SubprocessError) as exc:
            logger.warning("Failed to start ffmpeg SSIM for %s: %s", url, exc)
            return 0.5

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.probe_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.debug("SSIM check timed out for %s", url)
            return 0.5

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        # Parse: "SSIM All:0.987654 (18.765432)"
        for line in stderr_text.splitlines():
            if "All:" in line:
                try:
                    ssim_str = line.split("All:")[1].strip().split()[0]
                    return max(0.0, min(1.0, float(ssim_str)))
                except (ValueError, IndexError):
                    continue
        return 0.5

    async def _check_blocking(self, url: str) -> float:
        """Extract one frame and run FFT‑based macroblock detection.

        Returns a 0‑1 score (1 = no blocking, 0 = severe blocking).
        Falls back to 0.5 on any error.
        """
        try:
            from io import BytesIO
            import numpy as np
            from PIL import Image

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", url, "-vframes", "1",
                "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, asyncio.SubprocessError) as exc:
            logger.warning("Failed to start ffmpeg frame-grab for %s: %s", url, exc)
            return 0.5

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.probe_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.debug("Frame grab timed out for %s", url)
            return 0.5

        if not stdout:
            return 0.5

        try:
            img = Image.open(BytesIO(stdout)).convert("L")
            arr = np.array(img, dtype=np.float64)
            h, w = arr.shape

            # 2‑D FFT
            fft = np.abs(np.fft.fft2(arr))
            fft_shifted = np.fft.fftshift(fft)
            total_energy = np.sum(fft_shifted ** 2)
            if total_energy == 0:
                return 0.5

            # Energy at macroblock frequencies (multiples of 8 pixels)
            cy, cx = h // 2, w // 2
            grid_energy = 0.0
            step = 8
            for dy in range(-h // 2, h // 2, step):
                for dx in range(-w // 2, w // 2, step):
                    gy, gx = cy + dy, cx + dx
                    if 0 <= gy < h and 0 <= gx < w:
                        grid_energy += fft_shifted[gy, gx] ** 2

            grid_ratio = grid_energy / total_energy
            # Invert: 0% ratio → score 1.0, 30%+ ratio → score 0.0
            score = max(0.0, 1.0 - grid_ratio / 0.3)
            return min(1.0, score)

        except Exception as e:
            logger.warning("Blocking check failed for %s: %s", url, e)
            return 0.5

    # ------------------------------------------------------------------
    # Silence detection  (deep / background only)
    # ------------------------------------------------------------------

    async def check_silence(self, url: str) -> tuple[bool, float]:
        """Check if stream audio is silent via ffmpeg ``volumedetect``.

        Downloads a 5‑second sample and runs::

            ffmpeg -t 5 -i <url> -af volumedetect -f null -

        Parses the ``max_volume`` line from stderr.
        Returns ``(is_silent, max_volume_db)`` — silent if max_volume < -50 dB.

        Takes ~5 seconds (the sample duration).
        Returns ``(False, 0.0)`` on failure (assume audio is OK rather than
        penalizing a working stream because ffmpeg couldn't decode).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-t", "5",
                "-i", url,
                "-af", "volumedetect",
                "-f", "null",
                "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, asyncio.SubprocessError) as exc:
            logger.warning("Failed to start ffmpeg for silence check %s: %s", url, exc)
            return False, 0.0

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.probe_timeout + 3,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.debug("Silence check timed out for %s", url)
            return False, 0.0

        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        # Parse max_volume from stderr: "max_volume: -12.5 dB"
        for line in stderr_text.splitlines():
            line = line.strip()
            if "max_volume:" in line:
                try:
                    db_str = line.split("max_volume:")[1].strip().split()[0]
                    max_db = float(db_str)
                    is_silent = max_db < -50.0
                    if is_silent:
                        logger.warning("Silent stream detected: %s (max_volume=%.1f dB)", url, max_db)
                    return is_silent, max_db
                except (ValueError, IndexError):
                    continue

        logger.debug("Could not parse volumedetect output for %s", url)
        return False, 0.0

    # ------------------------------------------------------------------
    # Name-based heuristic  (resolution_mode="metadata")
    # ------------------------------------------------------------------

    @staticmethod
    def _resolution_from_name(name: str) -> tuple[int, int]:
        """Heuristic: extract resolution from channel *name*.

        Patterns are matched case‑insensitively with word boundaries:

        ==========  ============
        Pattern     Resolution
        ==========  ============
        ``4K``      3840×2160
        ``FHD`` /   1920×1080
        ``1080``
        ``HD``  /   1280×720
        ``720``
        ``SD``  /   854×480
        ``480``
        *(default)* 1280×720
        ==========  ============
        """
        if re.search(r"(?i)\b4K\b", name):
            return 3840, 2160
        if re.search(r"(?i)\bFHD\b", name) or re.search(r"(?i)\b1080\b", name):
            return 1920, 1080
        if re.search(r"(?i)\bHD\b", name) or re.search(r"(?i)\b720\b", name):
            return 1280, 720
        if re.search(r"(?i)\bSD\b", name) or re.search(r"(?i)\b480\b", name):
            return 854, 480
        return 1280, 720
