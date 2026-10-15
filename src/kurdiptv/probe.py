"""Stream probing: is it HLS, is it live, does a real segment arrive, what quality does it really have?

Never trusts the URL or claimed values: resolution/fps/codec come from ffprobe run on a downloaded segment
(when ffprobe is installed); without ffprobe the playlist's declared RESOLUTION is used and marked unverified.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import ProbeSettings
from .hls import MasterPlaylist, MediaPlaylist, Variant, parse_playlist
from .net import FetchError, Fetched, HostLimiter, SafeHttp, UnsafeURL
from .util import url_host


@dataclass
class ProbeResult:
    ok: bool
    mode: str = "full"
    kind: str = ""  # hls-master | hls-media | not-hls | audio-only | error
    http_status: int | None = None
    latency_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str = ""
    acodec: str = ""
    bandwidth: int | None = None
    bitrate: int | None = None
    verified: bool = False
    error: str = ""
    final_url: str = ""
    tail_marker: str = ""
    variants: int = 0
    service: str = ""  # what the MPEG-TS stream calls itself (SDT service_name / provider), if it says
    notes: list[str] = field(default_factory=list)


def find_ffprobe(configured: str = "") -> str | None:
    if configured:
        return configured if shutil.which(configured) or _exists(configured) else None
    return shutil.which("ffprobe")


def _exists(path: str) -> bool:
    import os

    return os.path.isfile(path)


def looks_like_media(data: bytes, fragmented_mp4: bool = False) -> str:
    """Container sniffing: "ts" | "fmp4" | "aac" | "" (unknown)."""
    if len(data) >= 189 and data[0] == 0x47 and data[188] == 0x47:
        return "ts"
    if len(data) >= 1 and data[0] == 0x47 and len(data) < 189:
        return "ts"
    if len(data) >= 12 and data[4:8] in {b"ftyp", b"moof", b"styp", b"sidx", b"moov"}:
        return "fmp4"
    if fragmented_mp4 and len(data) >= 8 and data[4:8] in {b"moof", b"styp"}:
        return "fmp4"
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xF0 == 0xF0):
        return "aac"
    return ""


def _fps(value: str | None) -> float | None:
    if not value or value in {"0/0", "0"}:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return round(float(num) / float(den), 2) if float(den) else None
        return round(float(value), 2)
    except (ValueError, ZeroDivisionError):
        return None


_GENERIC_SERVICE = {"", "service01", "service", "unknown", "ffmpeg", "mpegts", "digital tv", "wowza", "nimble", "streamer", "hls"}


def _service_label(info: dict | None) -> str:
    """The channel name an MPEG-TS stream announces about itself ("ZDF", "KURDSAT"), ignoring generic muxer defaults."""
    for program in (info or {}).get("programs") or []:
        tags = program.get("tags") or {}
        parts = []
        for key in ("service_provider", "service_name"):
            value = str(tags.get(key, "")).strip()
            if value.lower() not in _GENERIC_SERVICE and value not in parts:
                parts.append(value)
        if parts:
            return " / ".join(parts)[:80]
    return ""


async def run_ffprobe(ffprobe: str, data: bytes, timeout: float = 20.0) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_programs", "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotImplementedError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    try:
        return json.loads(out.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return None


class Prober:
    def __init__(
        self,
        http: SafeHttp,
        cfg: ProbeSettings,
        *,
        ffprobe: str | None = None,
        limiter: HostLimiter | None = None,
    ):
        self.http = http
        self.cfg = cfg
        self.ffprobe = ffprobe
        self.limiter = limiter or HostLimiter(cfg.per_host_concurrency, cfg.per_host_min_interval)
        self._sem = asyncio.Semaphore(cfg.concurrency)

    # ---- public --------------------------------------------------------------------------------------------------
    async def probe(self, url: str, mode: str = "full") -> ProbeResult:
        async with self._sem:
            try:
                return await self._probe(url, mode)
            except UnsafeURL as exc:
                return ProbeResult(False, mode, "error", error=f"refused: {exc}")
            except FetchError as exc:
                return ProbeResult(False, mode, "error", error=exc.kind + (f" ({exc.message[:60]})" if exc.message else ""))
            except Exception as exc:  # a probe must never take the refresh down
                return ProbeResult(False, mode, "error", error=f"{type(exc).__name__}: {str(exc)[:80]}")

    # ---- internals --------------------------------------------------------------------------------------------------
    async def _get(self, url: str, *, max_bytes: int, timeout: float, byte_range: tuple[int, int] | None = None) -> Fetched:
        async with self.limiter.slot(url_host(url)):
            return await self.http.get(url, max_bytes=max_bytes, timeout=timeout, total_timeout=timeout * 2, byte_range=byte_range)

    @staticmethod
    def _http_error(status: int) -> str:
        if status in {401, 403}:
            return f"http-{status} (refused: login, geo or IP block)"
        if status == 429:
            return "http-429 (rate limited)"
        return f"http-{status}"

    async def _probe(self, url: str, mode: str) -> ProbeResult:
        cfg = self.cfg
        res = await self._get(url, max_bytes=cfg.max_playlist_bytes, timeout=cfg.playlist_timeout)
        result = ProbeResult(False, mode, http_status=res.status, latency_ms=res.elapsed_ms, final_url=res.url)
        if res.status != 200:
            result.kind, result.error = "error", self._http_error(res.status)
            return result
        text = res.text()
        head = text.lstrip()[:20]
        parsed = parse_playlist(text, res.url) if not res.truncated else None
        if parsed is None:
            result.kind = "not-hls"
            if res.truncated and res.body[:1] == b"\x47":
                result.error = "not an HLS playlist (endless MPEG-TS stream)"
            elif head.startswith("<"):
                result.error = "not an HLS playlist (web page)"
            else:
                result.error = "not an HLS playlist"
            return result

        if isinstance(parsed, MasterPlaylist):
            result.kind, result.variants = "hls-master", len(parsed.variants)
            if not parsed.variants:
                result.error = "master playlist without variants"
                return result
            video = [v for v in parsed.variants if not v.audio_only]
            if not video:
                result.kind, result.error = "audio-only", "audio-only stream (radio)"
                return result
            ordered = sorted(video, key=lambda v: (-(v.height or 0), -(v.bandwidth or 0)))
            errors: list[str] = []
            for variant in ordered[: cfg.max_variants_tried]:
                ok = await self._check_variant(result, variant, mode)
                if ok:
                    result.ok = True
                    return result
                errors.append(result.error)
                result.error = ""
            result.error = errors[0] if errors else "no playable variant"
            if len(errors) > 1:
                result.notes.append(f"{len(errors)} variants failed")
            return result

        result.kind = "hls-media"
        result.ok = await self._check_media(result, parsed, res.url, None, mode)
        return result

    async def _check_variant(self, result: ProbeResult, variant: Variant, mode: str) -> bool:
        res = await self._get(variant.uri, max_bytes=self.cfg.max_playlist_bytes, timeout=self.cfg.playlist_timeout)
        if res.status != 200:
            result.error = "variant " + self._http_error(res.status)
            return False
        media = parse_playlist(res.text(), res.url) if not res.truncated else None
        if not isinstance(media, MediaPlaylist):
            result.error = "variant is not a media playlist"
            return False
        return await self._check_media(result, media, res.url, variant, mode)

    async def _check_media(self, result: ProbeResult, media: MediaPlaylist, media_url: str, variant: Variant | None, mode: str) -> bool:
        cfg = self.cfg
        # A failed earlier variant must not leak its claimed quality into this attempt.
        result.width = result.height = result.fps = result.bitrate = result.bandwidth = None
        result.vcodec = result.acodec = result.tail_marker = ""
        result.verified = False
        if not media.segments:
            result.error = "playlist has no segments"
            return False
        if media.ended:
            result.error = "not live (playlist has EXT-X-ENDLIST)"
            return False
        if media.encrypted:
            result.error = "encrypted segments (cannot be verified)"
            return False
        last = media.segments[-1]
        if last.program_date_time is not None:
            age = (datetime.now(timezone.utc) - last.program_date_time).total_seconds()
            if age > 1800:
                result.error = f"playlist is stale (newest segment {int(age // 60)} min old)"
                return False
        result.tail_marker = media.tail_marker
        result.bandwidth = variant.bandwidth if variant else None
        if variant and variant.height:
            result.width, result.height, result.fps = variant.width, variant.height, variant.fps

        seg = media.segments[-2] if len(media.segments) >= 3 else media.segments[-1]
        light = mode == "light"
        byte_range = None
        if seg.byterange:
            length, offset = seg.byterange
            start = offset if offset is not None else 0
            byte_range = (start, start + length - 1)
        elif light:
            byte_range = (0, 4095)
        seg_res = await self._get(
            seg.uri,
            max_bytes=4096 if light else cfg.max_segment_bytes,
            timeout=cfg.segment_timeout,
            byte_range=byte_range,
        )
        if seg_res.status not in {200, 206}:
            result.error = "segment " + self._http_error(seg_res.status)
            return False
        data = seg_res.body
        if not data:
            result.error = "segment is empty"
            return False
        container = looks_like_media(data, fragmented_mp4=bool(media.map_uri))
        if not container:
            result.error = "segment is not MPEG-TS/fMP4/AAC data"
            return False
        if container == "aac" and not (variant and variant.has_video):
            result.kind, result.error = "audio-only", "audio-only stream (radio)"
            return False
        if light:
            return True

        if not seg_res.truncated and seg.duration > 0:
            result.bitrate = int(len(data) * 8 / seg.duration)

        if self.ffprobe:
            init = b""
            if media.map_uri:
                init_res = await self._get(media.map_uri, max_bytes=500_000, timeout=cfg.segment_timeout)
                init = init_res.body if init_res.status in {200, 206} else b""
            info = await run_ffprobe(self.ffprobe, init + data, timeout=cfg.segment_timeout)
            streams = (info or {}).get("streams") or []
            vstreams = [s for s in streams if s.get("codec_type") == "video" and s.get("width")]
            if not streams:
                result.error = "segment could not be decoded"
                return False
            if not vstreams:
                result.kind, result.error = "audio-only", "audio-only stream (radio)"
                return False
            v = vstreams[0]
            result.width, result.height = int(v["width"]), int(v["height"])
            result.fps = _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")) or result.fps
            result.vcodec = v.get("codec_name", "")
            a = next((s for s in streams if s.get("codec_type") == "audio"), None)
            result.acodec = (a or {}).get("codec_name", "")
            result.service = _service_label(info)
            result.verified = True
        else:
            result.notes.append("ffprobe unavailable: resolution is the playlist's claim, not measured")
        return True
