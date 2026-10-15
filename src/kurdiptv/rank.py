"""Order a channel's candidate streams: best first, with failover candidates behind it.

Order of preference (README / plan):
  1. reachable HLS with the highest *verified* resolution
  2. higher verified bitrate, then frame rate, when resolution is equal
  3. lower measured latency, fewer recent failures
  4. stable source with a recent successful probe
Quality values are bucketed so measurement noise does not make the primary flip back and forth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import ProbeSettings
from .health import CandState
from .util import url_host, url_key


@dataclass
class Choice:
    primary: CandState | None = None
    fallbacks: list[CandState] = field(default_factory=list)
    status: str = "none"  # healthy | degraded | stale | none
    notes: list[str] = field(default_factory=list)

    @property
    def ordered(self) -> list[CandState]:
        return ([self.primary] if self.primary else []) + self.fallbacks


def bitrate_bucket(bps: int | None) -> int:
    if not bps or bps <= 0:
        return 0
    return int(math.floor(math.log(max(bps, 1) / 100_000, 1.5))) + 20


def fps_bucket(fps: float | None) -> int:
    if not fps:
        return 0
    if fps >= 45:
        return 2
    return 1 if fps >= 20 else 0


def latency_bucket(ms: int | None) -> int:
    return 9 if ms is None else min(9, int(ms // 400))


def is_eligible(s: CandState, cfg: ProbeSettings, now: float) -> bool:
    """Usable as a published stream: probed OK at least once, and not failing repeatedly."""
    if s.last_ok_at is None:
        return False
    if now - s.last_ok_at > cfg.keep_stale_hours * 3600:
        return False
    return s.last_ok or s.consecutive_failures < cfg.fail_threshold


def sort_key(s: CandState, preferred: list[str], pinned: str | None):
    pinned_first = 0 if pinned and url_key(s.url) == pinned else 1
    pref = preferred.index(s.source_id) if s.source_id in preferred else len(preferred)
    return (
        pinned_first,
        -(s.height or 0) if s.verified else 0,  # verified resolution first
        -(s.height or 0),  # then claimed resolution among unverified
        -bitrate_bucket(s.bitrate or s.bandwidth),
        -fps_bucket(s.fps),
        0 if s.kind == "hls-master" else 1,  # a master playlist lets the player adapt its quality
        pref,
        latency_bucket(s.latency_ms),
        s.consecutive_failures,
        0 if s.trust == "high" else 1,
        -round(s.success_ratio * 4),
        -int((s.last_ok_at or 0) // 3600),
        s.url,
    )


def choose(
    states: list[CandState],
    cfg: ProbeSettings,
    now: float,
    *,
    preferred_sources: list[str] | None = None,
    pin_url: str | None = None,
) -> Choice:
    preferred = preferred_sources or []
    pinned = url_key(pin_url) if pin_url else None
    eligible = [s for s in states if is_eligible(s, cfg, now)]
    choice = Choice()
    if eligible:
        ordered = sorted(eligible, key=lambda s: sort_key(s, preferred, pinned))
        choice.status = "healthy" if ordered[0].last_ok else "degraded"
    else:
        stale = [s for s in states if s.last_ok_at is not None and now - s.last_ok_at <= cfg.keep_stale_hours * 3600]
        if not stale:
            return choice
        ordered = sorted(stale, key=lambda s: (-(s.last_ok_at or 0), s.url))
        choice.status = "stale"
        choice.notes.append("all streams failing; keeping the most recent known-good one")

    # A fallback on the same server as the primary is no protection - two URLs on one host die together.
    primary = ordered[0]
    chosen = [primary]
    hosts = {url_host(primary.url)}
    for s in ordered[1:]:
        if len(chosen) > cfg.max_fallbacks:
            break
        host = url_host(s.url)
        if host in hosts and not cfg.same_host_fallbacks:
            continue
        chosen.append(s)
        hosts.add(host)
    choice.primary, choice.fallbacks = primary, chosen[1:]
    return choice
