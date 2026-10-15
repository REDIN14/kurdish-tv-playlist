"""The curated channel catalogue (config/catalog.yaml) and its lookup indexes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, load_yaml
from .normalize import name_key

LANGUAGE_NAMES = {
    "ku": "Kurdish",
    "ckb": "Kurdish (Sorani)",
    "kmr": "Kurdish (Kurmanji)",
    "zza": "Kurdish (Zazaki)",
    "ar": "Arabic",
    "tr": "Turkish",
    "en": "English",
    "fa": "Persian",
}

_FIELDS = {"id", "name", "aliases", "category", "language", "country", "url_patterns", "sites", "own_servers", "logo"}


@dataclass(frozen=True)
class CatalogChannel:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    category: str = "General"
    language: str = ""
    country: str = ""
    url_patterns: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()  # the broadcaster's own web domains: a stream served from one of them is the original source
    own_servers: tuple[str, ...] = ()  # "host/path" fragments of the broadcaster's own account on a CDN or hosting provider
    logo: str = ""
    keys: frozenset[str] = frozenset()

    def is_original(self, url: str) -> bool:
        """The stream comes from the broadcaster itself: its own domain, or its own account on a hosting provider."""
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if any(host == d or host.endswith("." + d) for d in self.sites):
            return True
        target = url_target(url)
        return any(fragment in target for fragment in self.own_servers)


def url_target(url: str) -> str:
    """The part of a stream URL that url_patterns are matched against: host[:port] + path, lower-case.
    A default port (":443" on https, ":80" on http) is dropped, so the same stream matches with or without it."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    default = {"http": ":80", "https": ":443"}.get(parts.scheme)
    if default and host.endswith(default):
        host = host[: -len(default)]
    return (host + parts.path).lower()


class Catalog:
    def __init__(self, channels: list[CatalogChannel]):
        self.channels = channels
        self.by_id: dict[str, CatalogChannel] = {}
        self.by_key: dict[str, str] = {}
        self._patterns: list[tuple[str, str]] = []
        for ch in channels:
            if ch.id in self.by_id:
                raise ConfigError(f"catalog: duplicate channel id {ch.id!r}")
            self.by_id[ch.id] = ch
            for key in ch.keys:
                other = self.by_key.get(key)
                if other and other != ch.id:
                    raise ConfigError(f"catalog: name {key!r} is claimed by both {other!r} and {ch.id!r}")
                self.by_key[key] = ch.id
            for pat in ch.url_patterns:
                self._patterns.append((pat.lower(), ch.id))
        self._patterns.sort(key=lambda item: len(item[0]), reverse=True)

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        data = load_yaml(path)
        channels = []
        for i, raw in enumerate(data.get("channels") or []):
            unknown = sorted(set(raw) - _FIELDS)
            if unknown:
                raise ConfigError(f"catalog.yaml channel #{i + 1}: unknown field(s) {', '.join(unknown)}")
            if not raw.get("id") or not raw.get("name"):
                raise ConfigError(f"catalog.yaml channel #{i + 1}: id and name are required")
            aliases = tuple(str(a) for a in raw.get("aliases") or [])
            keys = {name_key(n) for n in (raw["name"], *aliases)}
            keys.discard("")
            channels.append(
                CatalogChannel(
                    id=str(raw["id"]),
                    name=str(raw["name"]),
                    aliases=aliases,
                    category=str(raw.get("category") or "General"),
                    language=str(raw.get("language") or ""),
                    country=str(raw.get("country") or ""),
                    url_patterns=tuple(str(p) for p in raw.get("url_patterns") or []),
                    sites=tuple(str(s).lower().strip().removeprefix("www.") for s in raw.get("sites") or []),
                    own_servers=tuple(str(s).lower().strip() for s in raw.get("own_servers") or []),
                    logo=str(raw.get("logo") or ""),
                    keys=frozenset(keys),
                )
            )
        return cls(channels)

    def by_name(self, name: str) -> CatalogChannel | None:
        key = name_key(name)
        cid = self.by_key.get(key) if key else None
        return self.by_id[cid] if cid else None

    def by_url(self, url: str) -> CatalogChannel | None:
        target = url_target(url)
        hits = [(pat, cid) for pat, cid in self._patterns if pat in target]
        if not hits:
            return None
        best_len = len(hits[0][0])
        ids = {cid for pat, cid in hits if len(pat) == best_len}
        if len(ids) > 1:
            return None  # two stations claim the same pattern length: do not guess
        return self.by_id[next(iter(ids))]
