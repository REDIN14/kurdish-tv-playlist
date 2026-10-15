"""Source adapters. Register new ones here (or just use the generic ``m3u`` adapter via config)."""

from __future__ import annotations

from .base import SourceAdapter, run_adapter
from .htmlscan import HtmlScanAdapter
from .kurditv import KurdiTvAdapter
from .kurdtvs import KurdTvsAdapter
from .m3u import M3UAdapter
from .official import OfficialAdapter
from .site123 import Site123Adapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "m3u": M3UAdapter,
    "official": OfficialAdapter,
    "kurdtvs": KurdTvsAdapter,
    "kurditv": KurdiTvAdapter,
    "site123": Site123Adapter,
    "html_scan": HtmlScanAdapter,
}

__all__ = ["ADAPTERS", "SourceAdapter", "run_adapter"]
