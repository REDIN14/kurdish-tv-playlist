"""Extended-M3U reading and writing (the IPTV playlist dialect: #EXTINF with key="value" attributes)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ATTR_RE = re.compile(r"""([A-Za-z0-9_.:\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DURATION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


@dataclass
class M3UEntry:
    name: str
    url: str
    duration: str = "-1"
    attrs: dict[str, str] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)  # #EXTVLCOPT / #KODIPROP lines that preceded the URL


def parse_m3u(text: str) -> list[M3UEntry]:
    """Parse an M3U/M3U8 channel list. Lenient: unknown directives are skipped, bad entries ignored."""
    entries: list[M3UEntry] = []
    pending: M3UEntry | None = None
    options: list[str] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending = _parse_extinf(line)
            pending.options = options  # keeps collecting #EXTVLCOPT/#KODIPROP lines until the URL arrives
        elif line.startswith("#"):
            if line.startswith(("#EXTVLCOPT", "#KODIPROP", "#EXTGRP")):
                options.append(line)
                if line.startswith("#EXTGRP:") and pending is not None and not pending.attrs.get("group-title"):
                    pending.attrs["group-title"] = line.split(":", 1)[1].strip()
        else:
            if pending is not None:
                pending.url = line
                entries.append(pending)
                pending = None
            options = []
    return entries


def _parse_extinf(line: str) -> M3UEntry:
    body = line[len("#EXTINF:"):] if line.startswith("#EXTINF:") else line[len("#EXTINF"):]
    m = _DURATION_RE.match(body)
    duration = m.group(1) if m else "-1"
    rest = body[m.end():] if m else body
    # The title starts after the first comma that is not inside a quoted attribute value.
    in_quote: str | None = None
    split_at = -1
    for i, ch in enumerate(rest):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in "\"'" and i > 0 and rest[i - 1] == "=":
            in_quote = ch
        elif ch == ",":
            split_at = i
            break
    attr_part, name = (rest[:split_at], rest[split_at + 1:]) if split_at >= 0 else (rest, "")
    attrs: dict[str, str] = {}
    for am in _ATTR_RE.finditer(attr_part):
        attrs[am.group(1).lower()] = am.group(2) if am.group(2) is not None else (am.group(3) or "")
    return M3UEntry(name=name.strip(), url="", duration=duration, attrs=attrs)


def attr_value(value: str) -> str:
    """Make a value safe inside a double-quoted M3U attribute."""
    return " ".join(value.replace('"', "'").replace("\r", " ").replace("\n", " ").split())


def render_extinf(name: str, attrs: dict[str, str]) -> str:
    parts = ["#EXTINF:-1"]
    for key, value in attrs.items():
        if value is None or value == "":
            continue
        parts.append(f'{key}="{attr_value(value)}"')
    title = attr_value(name).replace(",", " ")
    return " ".join(parts) + "," + title
