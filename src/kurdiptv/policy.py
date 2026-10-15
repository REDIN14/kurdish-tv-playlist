"""Publication policy: blocklist, brands that are not Kurdish TV, rights-sensitive content, unsafe URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .config import ConfigError, load_yaml
from .normalize import fold_ascii, name_key
from .util import url_host

# ---- URLs that must never be published ----------------------------------------------------------------------------

_AUTH_QUERY_KEYS = {
    "token", "access_token", "auth", "authorization", "key", "apikey", "api_key", "password", "pass", "pwd", "passwd",
    "user", "username", "login", "sig", "signature", "expires", "expire", "exp", "hdnts", "hdnea", "wmsauthsign",
    "hmac", "secret", "session", "sessionid", "sid", "e", "st", "policy", "key-pair-id", "x-amz-signature",
}
# Query parameters that only carry a session id and can safely be dropped.
EPHEMERAL_QUERY_KEYS = {"nimblesessionid", "nimblesession"}
# Xtream-Codes style panel URLs embed the subscriber's username and password in the path.
_XTREAM_PATH = re.compile(r"^/(?:live|movie|series|streaming)/[^/]+/[^/]+/\d+\.(?:m3u8|ts)$", re.IGNORECASE)
_GET_PHP = re.compile(r"/(?:get|player_api|panel_api)\.php", re.IGNORECASE)


def strip_ephemeral_query(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in EPHEMERAL_QUERY_KEYS]
    if len(kept) == len(parse_qsl(parts.query, keep_blank_values=True)):
        return url
    query = "&".join(f"{k}={v}" if v != "" else k for k, v in kept)
    return parts._replace(query=query).geturl()


def redact_url(url: str) -> str:
    """Report-safe form of a URL: one that carries credentials or tokens keeps only scheme://host, so a rejected
    stream is never republished together with somebody else's login."""
    if credential_reason(url) is None:
        return url
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname or ''}{port}/(credentials removed)"


def credential_reason(url: str) -> str | None:
    """Why this URL carries credentials / private tokens (so it cannot be published), or None."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        return "credentials in URL"
    if _XTREAM_PATH.match(parts.path):
        return "panel URL with user name and password in the path"
    if _GET_PHP.search(parts.path) and parts.query:
        return "panel API URL"
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _AUTH_QUERY_KEYS:
            return f"auth parameter in query string ({key})"
    return None


# ---- policy -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Brand:
    name: str
    tokens: tuple[str, ...] = ()  # name words; also URL words when 4+ characters long
    phrases: tuple[str, ...] = ()  # multi-word names
    url_tokens: tuple[str, ...] = ()  # URL words of any length (distinctive short ones like "zdf")


def _url_tokens(url: str) -> set[str]:
    parts = urlsplit(url)
    text = ((parts.hostname or "") + parts.path).lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if t}


def _name_tokens(name: str) -> set[str]:
    """Words of a channel name plus run-together neighbours, so "Sky News" also yields "skynews".

    Joined forms need 5+ characters: "Kanal D" -> "kanald" is a brand, "Channel A TV" -> "atv" is not.
    """
    words = fold_ascii(name).split()
    out = set(words)
    for i in range(len(words) - 1):
        joined = words[i] + words[i + 1]
        if len(joined) >= 5:
            out.add(joined)
    whole = "".join(words)
    if len(words) > 1 and len(whole) >= 5:
        out.add(whole)
    return out


def _token_hit(spec: str, tokens: set[str]) -> bool:
    if spec.endswith("*"):
        prefix = spec[:-1]
        return any(t.startswith(prefix) for t in tokens)
    return spec in tokens


@dataclass
class Policy:
    unknown_from_trusted: str = "include"
    require_corroboration: bool = True
    foreign_tags: frozenset[str] = frozenset()
    kurdish_markers: frozenset[str] = frozenset()
    rights_words: tuple[str, ...] = ()
    block_ids: frozenset[str] = frozenset()
    block_name_keys: frozenset[str] = frozenset()
    block_hosts: frozenset[str] = frozenset()
    block_url_parts: tuple[str, ...] = ()
    brands: tuple[Brand, ...] = ()
    force_alias: dict[str, str] = field(default_factory=dict)
    preferred_source: dict[str, list[str]] = field(default_factory=dict)
    pin_url: dict[str, str] = field(default_factory=dict)
    logo: dict[str, str] = field(default_factory=dict)
    never_merge: tuple[frozenset[str], ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Policy":
        data = load_yaml(path)
        scope = data.get("scope") or {}
        block = data.get("blocklist") or {}
        over = data.get("overrides") or {}
        allowed = {"scope", "blocklist", "deny_brands", "overrides"}
        extra = sorted(set(data) - allowed)
        if extra:
            raise ConfigError(f"policy.yaml: unknown section(s) {', '.join(extra)}")
        unknown = scope.get("unknown_from_trusted", "include")
        if unknown not in {"include", "review"}:
            raise ConfigError("policy.yaml: scope.unknown_from_trusted must be include or review")
        brands = []
        for i, b in enumerate(data.get("deny_brands") or []):
            if not isinstance(b, dict) or not b.get("name"):
                raise ConfigError(f"policy.yaml: deny_brands[{i}] needs a name")
            brands.append(
                Brand(
                    name=str(b["name"]),
                    tokens=tuple(str(t).lower() for t in b.get("tokens") or []),
                    phrases=tuple(fold_ascii(str(p)) for p in b.get("phrases") or []),
                    url_tokens=tuple(str(t).lower() for t in b.get("url_tokens") or []),
                )
            )
        return cls(
            unknown_from_trusted=unknown,
            require_corroboration=bool(scope.get("require_corroboration_from_low_trust", True)),
            foreign_tags=frozenset(str(t).lower() for t in scope.get("foreign_tags") or []),
            kurdish_markers=frozenset(str(t).lower() for t in scope.get("kurdish_markers") or []),
            rights_words=tuple(str(w).lower() for w in scope.get("rights_sensitive_words") or []),
            block_ids=frozenset(str(x).lower() for x in block.get("channel_ids") or []),
            block_name_keys=frozenset(k for k in (name_key(str(x)) for x in block.get("names") or []) if k),
            block_hosts=frozenset(str(x).lower() for x in block.get("hosts") or []),
            block_url_parts=tuple(str(x).lower() for x in block.get("url_contains") or []),
            brands=tuple(brands),
            force_alias={name_key(k): str(v) for k, v in (over.get("force_alias") or {}).items() if name_key(k)},
            preferred_source={str(k): [str(s) for s in v] for k, v in (over.get("preferred_source") or {}).items()},
            pin_url={str(k): str(v) for k, v in (over.get("pin_url") or {}).items()},
            logo={str(k): str(v) for k, v in (over.get("logo") or {}).items()},
            never_merge=tuple(frozenset(str(x).lower() for x in pair) for pair in over.get("never_merge") or []),
        )

    # ---- checks (each returns a human-readable reason, or None) ----------------------------------------------------
    def blocked(self, *, channel_id: str | None, name: str, url: str) -> str | None:
        if channel_id and channel_id.lower() in self.block_ids:
            return f"channel {channel_id} is on the blocklist"
        if self.block_name_keys and name_key(name) in self.block_name_keys:
            return f"name {name!r} is on the blocklist"
        host = url_host(url)
        for blocked in self.block_hosts:
            if host == blocked or host.endswith("." + blocked):
                return f"host {host} is on the blocklist"
        low = url.lower()
        for part in self.block_url_parts:
            if part and part in low:
                return f"URL matches blocklist entry {part!r}"
        return None

    def denied_by_url(self, url: str) -> str | None:
        tokens = _url_tokens(url)
        for brand in self.brands:
            for spec in brand.tokens:
                if len(spec.rstrip("*")) >= 4 and _token_hit(spec, tokens):
                    return f"stream URL belongs to {brand.name} ({spec.rstrip('*')})"
            for spec in brand.url_tokens:
                if _token_hit(spec, tokens):
                    return f"stream URL belongs to {brand.name} ({spec.rstrip('*')})"
        return None

    def denied_by_name(self, name: str) -> str | None:
        tokens = _name_tokens(name)
        text = " " + fold_ascii(name) + " "
        for brand in self.brands:
            for spec in brand.tokens:
                if _token_hit(spec, tokens):
                    return f"{brand.name}: channel is not Kurdish TV ({spec.rstrip('*')})"
            for phrase in brand.phrases:
                if phrase and f" {phrase} " in text:
                    return f"{brand.name}: channel is not Kurdish TV ({phrase})"
        return None

    def rights_reason(self, name: str, url: str) -> str | None:
        hay_name = fold_ascii(name)
        parts = urlsplit(url)
        hay_url = ((parts.hostname or "") + parts.path).lower()
        for word in self.rights_words:
            if word in hay_name or word in hay_url:
                return f"rights-sensitive content ({word}): sports and films are not published automatically"
        return None

    def has_kurdish_marker(self, *texts: str) -> bool:
        for text in texts:
            for token in fold_ascii(text).split():
                for marker in self.kurdish_markers:
                    if token == marker or (marker.startswith("kurd") and token.startswith("kurd")):
                        return True
                if token.startswith("کورد"):
                    return True
        return False

    def foreign_only(self, tags: tuple[str, ...]) -> str | None:
        """The foreign-language tag if the source tags the channel as foreign and not at all as Kurdish."""
        lowered = [t.lower() for t in tags]
        foreign = [t for t in lowered if t in self.foreign_tags]
        if foreign and not any(t in self.kurdish_markers for t in lowered):
            return foreign[0]
        return None

    def may_merge(self, id_a: str, id_b: str, name_a: str = "", name_b: str = "") -> bool:
        pair_ids = {id_a.lower(), id_b.lower()}
        pair_names = {name_a.lower(), name_b.lower()}
        for pair in self.never_merge:
            if pair <= pair_ids or pair <= pair_names:
                return False
        return True
