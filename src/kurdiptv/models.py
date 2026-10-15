"""Plain data objects passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field

# Tag an adapter puts on a stream whose source lists it without any language / country / category information
# (a bare channel page on a site that also carries Turkish, Arabic and international TV).
UNCLASSIFIED = "unclassified"


@dataclass
class RawCandidate:
    """One stream URL found on a source page, before normalisation and matching."""

    source_id: str
    url: str
    name: str
    aliases: tuple[str, ...] = ()  # other labels the source gave this entry (often unreliable)
    logo_url: str | None = None
    tags: tuple[str, ...] = ()  # source categories / group titles
    language: str | None = None
    country: str | None = None
    page_url: str | None = None
    name_confidence: str = "medium"  # high | medium | low
    station_id: str | None = None  # tvg-id supplied by the source, if any
    note: str = ""


@dataclass
class SourceResult:
    source_id: str
    status: str  # ok | partial | failed | blocked | skipped
    candidates: list[RawCandidate] = field(default_factory=list)
    http_status: int | None = None
    requests: int = 0
    cache_hits: int = 0
    duration_ms: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)
    robots_disallowed: list[str] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count (unusable entries seen)

    def summary(self) -> dict:
        return {
            "source": self.source_id,
            "status": self.status,
            "http_status": self.http_status,
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "duration_ms": self.duration_ms,
            "candidates": len(self.candidates),
            "error": self.error,
            "notes": self.notes[:20],
            "skipped": self.skipped,
            "robots_disallowed": self.robots_disallowed[:10],
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
        }
