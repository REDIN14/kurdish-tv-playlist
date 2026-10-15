"""Channel logos: source-supplied -> verified catalogue (iptv-org) -> generated text placeholder.

Every image is downloaded through the SSRF-safe client, validated (type, size, dimensions, decodability) and
re-encoded as a local PNG. The playlist only ever points at our own copy - nothing is hot-linked.
"""

from __future__ import annotations

import asyncio
import colorsys
import io
import json
import logging
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import LogoSettings, Settings
from .db import Database
from .fetcher import RobotsCache
from .net import FetchError, HostLimiter, SafeHttp, UnsafeURL
from .normalize import name_key
from .util import atomic_write_bytes, now, sha1_hex, url_host

log = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 30_000_000

_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpeg", b"GIF87a": "gif", b"GIF89a": "gif"}
_FONT_CANDIDATES = [
    "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
PLACEHOLDER_RETRY_SECONDS = 24 * 3600


@dataclass
class LogoInfo:
    channel_id: str
    kind: str  # source | catalog | placeholder
    sha1: str
    origin: str = ""

    @property
    def version(self) -> str:
        return self.sha1[:8]


def _sniff(data: bytes) -> str:
    for magic, fmt in _MAGIC.items():
        if data.startswith(magic):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def normalise_logo(data: bytes, cfg: LogoSettings) -> tuple[bytes, int, int]:
    """Validate and re-encode an image as PNG. Raises ValueError with the reason when it is unusable."""
    if not data:
        raise ValueError("empty response")
    fmt = _sniff(data)
    if not fmt:
        raise ValueError("not a PNG/JPEG/GIF/WEBP image")
    try:
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        if max(width, height) < cfg.min_px:
            raise ValueError(f"image too small ({width}x{height})")
        if max(width, height) > cfg.max_px:
            raise ValueError(f"image too large ({width}x{height})")
        img.load()
    except ValueError:
        raise
    except Exception as exc:  # Pillow raises many exception types for corrupt files
        raise ValueError(f"undecodable image ({type(exc).__name__})") from exc
    img = img.convert("RGBA")
    if img.getchannel("A").getextrema() == (0, 0):
        raise ValueError("image is fully transparent")
    img.thumbnail((cfg.output_px, cfg.output_px), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), img.width, img.height


def _font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def make_placeholder(name: str, channel_id: str, size: int = 512) -> bytes:
    """A deterministic coloured tile with the channel name, readable in any IPTV player."""
    digest = int(sha1_hex(channel_id)[:8], 16)
    hue = (digest % 360) / 360.0
    bg = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.30, 0.55))
    img = Image.new("RGBA", (size, size), bg + (255,))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((size * 0.04, size * 0.04, size * 0.96, size * 0.96), radius=size * 0.08, outline=(255, 255, 255, 90), width=max(2, size // 128))
    ascii_name = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))
    ascii_name = "".join(ch for ch in ascii_name if ch.isascii() and (ch.isalnum() or ch in " -&+.")).strip() or channel_id
    lines = textwrap.wrap(ascii_name.upper(), width=10, break_long_words=True)[:3] or [channel_id.upper()[:10]]
    fsize = int(size * (0.22 if len(lines) == 1 else 0.19 if len(lines) == 2 else 0.15))
    font = _font(fsize)
    while fsize > 12 and max(draw.textlength(line, font=font) for line in lines) > size * 0.84:
        fsize -= 4
        font = _font(fsize)
    line_h = fsize * 1.15
    y = (size - line_h * len(lines)) / 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((size - w) / 2, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


class LogoCatalog:
    """Name -> logo URLs from the iptv-org channel/logo catalogue (https://github.com/iptv-org/api)."""

    def __init__(self, keys: dict[str, list[str]] | None = None):
        self.keys = keys or {}

    def find(self, names: list[str]) -> list[str]:
        urls: list[str] = []
        for name in names:
            key = name_key(name)
            for url in self.keys.get(key, []):
                if url not in urls:
                    urls.append(url)
        return urls

    @classmethod
    async def load(cls, http: SafeHttp, db: Database, cfg: LogoSettings, cache_dir: Path, ttl_days: float = 7.0) -> "LogoCatalog":
        index_path = cache_dir / "iptv_org_index.json"
        built_at = float(db.get_meta("iptv_org_built_at", "0") or 0)
        if index_path.exists() and now() - built_at < ttl_days * 86400:
            return cls(_read_index(index_path))
        try:
            ch_res = await http.get(cfg.catalog_channels_url, max_bytes=40_000_000, total_timeout=120, timeout=60)
            lg_res = await http.get(cfg.catalog_logos_url, max_bytes=40_000_000, total_timeout=120, timeout=60)
            if ch_res.status != 200 or lg_res.status != 200:
                raise FetchError("http", f"{ch_res.status}/{lg_res.status}")
            keys = await asyncio.to_thread(_build_index, json.loads(ch_res.body), json.loads(lg_res.body))
        except (FetchError, UnsafeURL, ValueError) as exc:
            log.warning("logo catalogue unavailable (%s)%s", exc, "; using the previous copy" if index_path.exists() else "")
            return cls(_read_index(index_path) if index_path.exists() else {})
        atomic_write_bytes(index_path, json.dumps(keys, ensure_ascii=False).encode("utf-8"))
        db.set_meta("iptv_org_built_at", str(now()))
        return cls(keys)


def _read_index(path: Path) -> dict[str, list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _build_index(channels: list[dict], logos: list[dict]) -> dict[str, list[str]]:
    by_channel: dict[str, list[tuple[tuple, str]]] = {}
    format_rank = {"PNG": 0, "WEBP": 1, "JPEG": 2, "JPG": 2, "GIF": 3}
    for logo in logos:
        fmt = (logo.get("format") or "").upper()
        if fmt not in format_rank or not logo.get("url") or not logo.get("channel"):
            continue  # SVG/AVIF are not usable in IPTV players
        width = logo.get("width") or 0
        score = (format_rank[fmt], abs(width - 512) if width else 9999)
        by_channel.setdefault(logo["channel"], []).append((score, logo["url"]))
    keys: dict[str, list[str]] = {}
    for ch in channels:
        entries = by_channel.get(ch.get("id"))
        if not entries or ch.get("is_nsfw"):
            continue
        urls = [u for _, u in sorted(entries)][:3]
        for label in [ch.get("name", ""), *(ch.get("alt_names") or [])]:
            key = name_key(label or "")
            if key and len(key) >= 3:
                bucket = keys.setdefault(key, [])
                for u in urls:
                    if u not in bucket and len(bucket) < 6:
                        bucket.append(u)
    return keys


class LogoManager:
    def __init__(self, settings: Settings, http: SafeHttp, db: Database, robots: RobotsCache | None = None):
        self.settings = settings
        self.cfg = settings.logos
        self.http = http
        self.db = db
        self.robots = robots
        self.limiter = HostLimiter(max_concurrent=2, min_interval=0.4)
        self.catalog = LogoCatalog()
        self.dir = settings.logos_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    async def prepare(self) -> None:
        if self.cfg.use_catalog:
            try:
                self.catalog = await LogoCatalog.load(self.http, self.db, self.cfg, self.settings.logo_cache_dir)
            except Exception as exc:  # the catalogue is a nicety: never fail a refresh over it
                log.warning("logo catalogue disabled: %s", exc)

    def path_for(self, channel_id: str) -> Path:
        return self.dir / f"{channel_id}.png"

    def current(self, channel_id: str) -> LogoInfo | None:
        row = self.db.one("SELECT kind, origin_url, sha1 FROM logo_state WHERE channel_id = ?", (channel_id,))
        if row is None or not self.path_for(channel_id).exists():
            return None
        return LogoInfo(channel_id, row["kind"], row["sha1"] or "", row["origin_url"] or "")

    async def resolve(
        self, channel_id: str, name: str, names: list[str], source_urls: list[str], catalog_url: str = "", override: str = ""
    ) -> LogoInfo:
        """Return the channel's local logo, fetching/validating a real one when needed.

        ``override`` (from policy.yaml) is either a direct image URL that replaces the source logos, or the word
        "placeholder" to force the generated tile.
        """
        ts = now()
        if override.strip().lower() == "placeholder":
            existing = self.current(channel_id)
            if existing and existing.kind == "placeholder":
                return existing
            png = await asyncio.to_thread(make_placeholder, name, channel_id, self.cfg.output_px)
            return self._store(channel_id, "placeholder", "", png, self.cfg.output_px, self.cfg.output_px, ts)
        if override:
            source_urls, catalog_url = [override], ""
        row = self.db.one("SELECT kind, fetched_at FROM logo_state WHERE channel_id = ?", (channel_id,))
        existing = self.current(channel_id)
        if existing and row:
            age = ts - row["fetched_at"]
            if existing.kind != "placeholder" and age < self.cfg.ttl_days * 86400:
                return existing
            if existing.kind == "placeholder" and age < PLACEHOLDER_RETRY_SECONDS:
                return existing

        tried: list[tuple[str, str]] = [("source", u) for u in source_urls[:3]]
        if catalog_url:
            tried.append(("catalog", catalog_url))
        tried += [("catalog", u) for u in self.catalog.find(names or [name])[:3]]
        seen: set[str] = set()
        for kind, url in tried:
            if url in seen:
                continue
            seen.add(url)
            data = await self._download(url)
            if data is None:
                continue
            png, w, h = data
            return self._store(channel_id, kind, url, png, w, h, ts)

        if existing and existing.kind != "placeholder":  # keep the old real logo; try again tomorrow
            self.db.execute("UPDATE logo_state SET fetched_at = ? WHERE channel_id = ?", (ts - self.cfg.ttl_days * 86400 + PLACEHOLDER_RETRY_SECONDS, channel_id))
            return existing
        png = await asyncio.to_thread(make_placeholder, name, channel_id, self.cfg.output_px)
        return self._store(channel_id, "placeholder", "", png, self.cfg.output_px, self.cfg.output_px, ts)

    # ---- internals ---------------------------------------------------------------------------------------------------
    def _store(self, channel_id: str, kind: str, origin: str, png: bytes, w: int, h: int, ts: float) -> LogoInfo:
        atomic_write_bytes(self.path_for(channel_id), png)
        sha = sha1_hex(png)
        self.db.execute(
            "INSERT INTO logo_state (channel_id, kind, origin_url, fetched_at, sha1, width, height) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (channel_id) DO UPDATE SET kind = excluded.kind, origin_url = excluded.origin_url, "
            "fetched_at = excluded.fetched_at, sha1 = excluded.sha1, width = excluded.width, height = excluded.height",
            (channel_id, kind, origin, ts, sha, w, h),
        )
        return LogoInfo(channel_id, kind, sha, origin)

    async def _download(self, url: str) -> tuple[bytes, int, int] | None:
        if not url.lower().startswith(("http://", "https://")):
            return None
        try:
            if self.robots is not None:
                ok, _ = await self.robots.allowed(url)
                if not ok:
                    return None
            async with self.limiter.slot(url_host(url)):
                res = await self.http.get(url, headers={"Accept": "image/png,image/jpeg,image/webp,image/*;q=0.8"}, max_bytes=self.cfg.max_bytes, timeout=15, total_timeout=30)
        except (FetchError, UnsafeURL) as exc:
            log.debug("logo %s: %s", url, exc)
            return None
        if res.status != 200 or res.truncated:
            return None
        ctype = res.content_type
        if ctype and not ctype.startswith("image/") and ctype not in {"application/octet-stream", "binary/octet-stream"}:
            return None
        if ctype == "image/svg+xml":
            return None
        try:
            return await asyncio.to_thread(normalise_logo, res.body, self.cfg)
        except ValueError as exc:
            log.debug("logo %s rejected: %s", url, exc)
            return None
