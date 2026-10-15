"""Render and validate the published M3U playlist."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import LANGUAGE_NAMES
from .config import PublishSettings
from .m3u import parse_m3u, render_extinf
from .policy import credential_reason

BASE_TOKEN = "{{BASE_URL}}"
_GROUP_ORDER = ["News", "General", "Entertainment", "Music", "Kids", "Education", "Culture", "Religion"]


@dataclass
class PublishedStream:
    cid: int
    url: str
    source_id: str
    height: int | None = None
    width: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    verified: bool = False
    latency_ms: int | None = None
    vcodec: str = ""
    acodec: str = ""


@dataclass
class PublishedChannel:
    id: str
    name: str
    group: str
    language: str = ""
    country: str = ""
    logo_version: str = ""
    streams: list[PublishedStream] = field(default_factory=list)  # primary first
    status: str = "healthy"  # healthy | degraded | stale
    in_catalog: bool = False
    order: int = 10_000  # catalogue position; unknown channels sort after the catalogue

    def sort_key(self):
        try:
            g = _GROUP_ORDER.index(self.group)
        except ValueError:
            g = len(_GROUP_ORDER)
        return (g, self.order, self.name.casefold(), self.id)


def _extinf(ch: PublishedChannel, tvg_id: str, title: str, group: str) -> str:
    logo = f"{BASE_TOKEN}/logos/{ch.id}.png" + (f"?v={ch.logo_version}" if ch.logo_version else "")
    attrs = {
        "tvg-id": tvg_id,
        "tvg-name": title,
        "tvg-logo": logo,
        "tvg-language": LANGUAGE_NAMES.get(ch.language, ch.language),
        "tvg-country": ch.country,
        "group-title": group,
    }
    return render_extinf(title, attrs)


def render_m3u(channels: list[PublishedChannel], cfg: PublishSettings, *, epg: bool = False) -> str:
    """The playlist text. Logo URLs carry BASE_TOKEN, replaced with the real base URL when served/exported."""
    lines = ["#EXTM3U" + (f' url-tvg="{BASE_TOKEN}/epg.xml" x-tvg-url="{BASE_TOKEN}/epg.xml"' if epg else ""), f"#PLAYLIST:{cfg.playlist_title}"]
    ordered = sorted(channels, key=PublishedChannel.sort_key)
    backups: list[tuple[PublishedChannel, int, PublishedStream]] = []
    for ch in ordered:
        if not ch.streams:
            continue
        lines.append(_extinf(ch, ch.id, ch.name, ch.group))
        lines.append(ch.streams[0].url)
        for n, stream in enumerate(ch.streams[1:], start=2):
            if cfg.fallback_mode == "inline":
                lines.append(_extinf(ch, f"{ch.id}-alt{n}", f"{ch.name} (Alt {n})", ch.group))
                lines.append(stream.url)
            elif cfg.fallback_mode == "group":
                backups.append((ch, n, stream))
    for ch, n, stream in backups:
        lines.append(_extinf(ch, f"{ch.id}-alt{n}", f"{ch.name} (Alt {n})", cfg.backup_group))
        lines.append(stream.url)
    return "\n".join(lines) + "\n"


def validate_m3u(text: str, cfg: PublishSettings, previous_primary_count: int = 0, *, min_channels: int | None = None) -> list[str]:
    """Problems that must stop publication (empty list = playlist is fine)."""
    problems: list[str] = []
    if not text.startswith("#EXTM3U"):
        problems.append("first line is not #EXTM3U")
    entries = parse_m3u(text)
    primaries = [e for e in entries if e.attrs.get("group-title") != cfg.backup_group and "-alt" not in e.attrs.get("tvg-id", "")]
    need = cfg.min_channels if min_channels is None else min_channels
    if len(primaries) < need:
        problems.append(f"only {len(primaries)} channels (minimum {need})")
    if previous_primary_count >= 10 and len(primaries) < previous_primary_count * cfg.min_ratio_of_previous:
        problems.append(
            f"channel count dropped from {previous_primary_count} to {len(primaries)} "
            f"(below {int(cfg.min_ratio_of_previous * 100)} % of the previous playlist)"
        )
    seen: set[str] = set()
    for i, e in enumerate(entries, start=1):
        tvg_id = e.attrs.get("tvg-id", "")
        if not e.name:
            problems.append(f"entry {i}: empty channel name")
        if not tvg_id:
            problems.append(f"entry {i} ({e.name}): missing tvg-id")
        elif tvg_id in seen:
            problems.append(f"entry {i} ({e.name}): duplicate tvg-id {tvg_id}")
        seen.add(tvg_id)
        for attr in ("tvg-name", "tvg-logo", "group-title"):
            if not e.attrs.get(attr):
                problems.append(f"entry {i} ({e.name}): missing {attr}")
        if not e.url.lower().startswith(("http://", "https://")):
            problems.append(f"entry {i} ({e.name}): stream URL is not http(s)")
        else:
            reason = credential_reason(e.url)
            if reason:
                problems.append(f"entry {i} ({e.name}): {reason}")
    return problems[:50]
