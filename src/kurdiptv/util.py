"""Small shared helpers: clock, slugs, URL identity, atomic file writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Indirection so tests can freeze time.
_clock = time.time


def now() -> float:
    return _clock()


def set_clock(fn) -> None:
    global _clock
    _clock = fn


def sha1_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def slugify(text: str, fallback: str = "channel") -> str:
    """ASCII slug used for stable channel ids ("Rudaw TV" -> "rudaw-tv")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


_DEFAULT_PORTS = {"http": 80, "https": 443}


def url_key(url: str) -> str:
    """Identity of a stream URL: lower-case scheme/host, default port and fragment removed."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` so readers see the old or the new file, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def clip(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
