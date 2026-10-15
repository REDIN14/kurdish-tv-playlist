"""Minimal HLS (RFC 8216) playlist parsing - just what probing needs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("(?:[^"]*)"|[^,]*)')


def parse_attrs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _ATTR_RE.findall(text):
        out[key] = value[1:-1] if value.startswith('"') else value
    return out


@dataclass
class Variant:
    uri: str
    bandwidth: int | None = None
    average_bandwidth: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codecs: str = ""

    @property
    def has_video(self) -> bool:
        if self.height:
            return True
        codecs = self.codecs.lower()
        return any(c in codecs for c in ("avc1", "hvc1", "hev1", "vp09", "av01", "mp4v"))

    @property
    def audio_only(self) -> bool:
        return bool(self.codecs) and not self.has_video and not self.height


@dataclass
class Segment:
    uri: str
    duration: float
    byterange: tuple[int, int | None] | None = None  # (length, offset)
    program_date_time: datetime | None = None


@dataclass
class MasterPlaylist:
    variants: list[Variant] = field(default_factory=list)


@dataclass
class MediaPlaylist:
    target_duration: float | None = None
    media_sequence: int = 0
    ended: bool = False
    playlist_type: str = ""
    segments: list[Segment] = field(default_factory=list)
    map_uri: str | None = None
    map_byterange: tuple[int, int | None] | None = None
    encrypted: bool = False

    @property
    def tail_marker(self) -> str:
        last = self.segments[-1].uri if self.segments else ""
        return f"{self.media_sequence}:{last}"


def _byterange(value: str) -> tuple[int, int | None]:
    length, _, offset = value.partition("@")
    return int(length), int(offset) if offset else None


def _parse_pdt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_playlist(text: str, base_url: str) -> MasterPlaylist | MediaPlaylist | None:
    """Parse an HLS playlist. Returns None when the text is not an HLS playlist at all."""
    lines = [ln.strip() for ln in text.lstrip("﻿").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines or not lines[0].startswith("#EXTM3U"):
        return None

    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        master = MasterPlaylist()
        pending: dict[str, str] | None = None
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF:"):
                pending = parse_attrs(ln.split(":", 1)[1])
            elif pending is not None and not ln.startswith("#"):
                res = pending.get("RESOLUTION", "")
                width = height = None
                m = re.fullmatch(r"(\d+)x(\d+)", res)
                if m:
                    width, height = int(m.group(1)), int(m.group(2))
                fps = None
                try:
                    fps = float(pending["FRAME-RATE"]) if "FRAME-RATE" in pending else None
                except ValueError:
                    pass
                master.variants.append(
                    Variant(
                        uri=urljoin(base_url, ln),
                        bandwidth=int(pending["BANDWIDTH"]) if pending.get("BANDWIDTH", "").isdigit() else None,
                        average_bandwidth=int(pending["AVERAGE-BANDWIDTH"]) if pending.get("AVERAGE-BANDWIDTH", "").isdigit() else None,
                        width=width,
                        height=height,
                        fps=fps,
                        codecs=pending.get("CODECS", ""),
                    )
                )
                pending = None
        return master

    media = MediaPlaylist()
    duration: float | None = None
    byterange: tuple[int, int | None] | None = None
    pdt: datetime | None = None
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            try:
                duration = float(ln.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                media.target_duration = float(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media.media_sequence = int(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-ENDLIST"):
            media.ended = True
        elif ln.startswith("#EXT-X-PLAYLIST-TYPE:"):
            media.playlist_type = ln.split(":", 1)[1].strip().upper()
        elif ln.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            if attrs.get("URI"):
                media.map_uri = urljoin(base_url, attrs["URI"])
                if attrs.get("BYTERANGE"):
                    media.map_byterange = _byterange(attrs["BYTERANGE"])
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            try:
                byterange = _byterange(ln.split(":", 1)[1])
            except ValueError:
                byterange = None
        elif ln.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            if attrs.get("METHOD", "NONE").upper() != "NONE":
                media.encrypted = True
        elif ln.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = _parse_pdt(ln.split(":", 1)[1])
        elif not ln.startswith("#") and duration is not None:
            media.segments.append(Segment(uri=urljoin(base_url, ln), duration=duration, byterange=byterange, program_date_time=pdt))
            duration, byterange, pdt = None, None, None
    return media
