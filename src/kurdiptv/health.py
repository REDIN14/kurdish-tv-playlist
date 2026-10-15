"""Probe bookkeeping: history, consecutive failures, exponential backoff, frozen-playlist detection."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

from .config import ProbeSettings
from .db import Database
from .probe import ProbeResult

HISTORY_PER_CANDIDATE = 30


@dataclass
class CandState:
    """What ranking needs to know about one candidate stream."""

    cid: int
    url: str
    source_id: str
    trust: str
    channel_id: str | None
    consecutive_failures: int = 0
    probes_total: int = 0
    probes_ok: int = 0
    last_ok: bool = False
    last_ok_at: float | None = None
    last_probe_at: float | None = None
    kind: str = ""
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    bandwidth: int | None = None
    verified: bool = False
    latency_ms: int | None = None
    last_error: str = ""
    vcodec: str = ""
    acodec: str = ""
    service: str = ""
    official: bool = False  # the broadcaster's own stream (found on its own web page, or served from its own domain)

    @property
    def success_ratio(self) -> float:
        return self.probes_ok / self.probes_total if self.probes_total else 0.0


def backoff_seconds(failures: int, cfg: ProbeSettings, rand=random.random) -> float:
    """15 min, 30 min, 1 h, 2 h ... capped; +-10 % jitter so retries do not synchronise."""
    if failures <= 0:
        return 0.0
    base = min(cfg.backoff_max_seconds, cfg.backoff_base_seconds * (2 ** (failures - 1)))
    return base * (0.9 + 0.2 * rand())


def record_probe(db: Database, cid: int, result: ProbeResult, cfg: ProbeSettings, ts: float, rand=random.random) -> bool:
    """Store a probe and update the candidate's health. Returns the effective ok flag (stall detection can veto)."""
    row = db.one("SELECT * FROM health WHERE candidate_id = ?", (cid,))
    ok, error = result.ok, result.error
    marker = row["tail_marker"] if row else None
    changed_at = row["tail_changed_at"] if row else None
    if ok and result.tail_marker:
        if marker == result.tail_marker and changed_at is not None and ts - changed_at >= cfg.stall_seconds:
            ok, error = False, f"stalled: playlist has not advanced for {int((ts - changed_at) // 60)} min"
        elif marker != result.tail_marker:
            changed_at = ts
    if changed_at is None:
        changed_at = ts

    failures = 0 if ok else (row["consecutive_failures"] if row else 0) + 1
    next_probe = ts + (cfg.ok_ttl_seconds if ok else backoff_seconds(failures, cfg, rand))
    full = result.mode == "full"

    with db.tx():
        db.execute(
            "INSERT INTO probes (candidate_id, ts, mode, ok, kind, http_status, latency_ms, width, height, fps, vcodec, acodec, "
            "bandwidth, bitrate, verified, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, result.mode, int(ok), result.kind, result.http_status, result.latency_ms, result.width, result.height,
             result.fps, result.vcodec, result.acodec, result.bandwidth, result.bitrate, int(result.verified), error[:200]),
        )
        db.execute(
            "DELETE FROM probes WHERE candidate_id = ? AND id NOT IN "
            "(SELECT id FROM probes WHERE candidate_id = ? ORDER BY ts DESC, id DESC LIMIT ?)",
            (cid, cid, HISTORY_PER_CANDIDATE),
        )
        if row is None:
            db.execute("INSERT INTO health (candidate_id) VALUES (?)", (cid,))
        # Quality fields are only replaced by a successful FULL probe; light checks and failures keep the last measurement.
        quality_ok = ok and full
        db.execute(
            "UPDATE health SET consecutive_failures = ?, probes_total = probes_total + 1, probes_ok = probes_ok + ?, "
            "last_probe_at = ?, last_full_at = CASE WHEN ? THEN ? ELSE last_full_at END, "
            "last_ok_at = CASE WHEN ? THEN ? ELSE last_ok_at END, last_fail_at = CASE WHEN ? THEN last_fail_at ELSE ? END, "
            "next_probe_at = ?, last_ok = ?, last_error = ?, kind = ?, latency_ms = ?, final_url = ?, "
            "tail_marker = ?, tail_changed_at = ?, "
            "width = CASE WHEN ? THEN ? ELSE width END, height = CASE WHEN ? THEN ? ELSE height END, "
            "fps = CASE WHEN ? THEN ? ELSE fps END, vcodec = CASE WHEN ? THEN ? ELSE vcodec END, "
            "acodec = CASE WHEN ? THEN ? ELSE acodec END, bandwidth = CASE WHEN ? THEN ? ELSE bandwidth END, "
            "bitrate = CASE WHEN ? THEN ? ELSE bitrate END, verified = CASE WHEN ? THEN ? ELSE verified END, "
            "service = CASE WHEN ? THEN ? ELSE service END "
            "WHERE candidate_id = ?",
            (
                failures, int(ok), ts, int(full), ts, int(ok), ts, int(ok), ts, next_probe, int(ok), error[:200],
                result.kind or None, result.latency_ms, result.final_url or None,
                result.tail_marker or marker, changed_at,
                int(quality_ok), result.width, int(quality_ok), result.height, int(quality_ok), result.fps,
                int(quality_ok), result.vcodec, int(quality_ok), result.acodec, int(quality_ok), result.bandwidth,
                int(quality_ok), result.bitrate, int(quality_ok), int(result.verified),
                int(quality_ok), result.service,
                cid,
            ),
        )
    return ok


def due_for_full_probe(db: Database, cids: Iterable[int], cfg: ProbeSettings, ts: float, force: bool = False) -> list[int]:
    """Candidates that need a full probe now: never probed, or past the cache lifetime and past their backoff."""
    out = []
    for cid in cids:
        row = db.one("SELECT last_full_at, next_probe_at, consecutive_failures FROM health WHERE candidate_id = ?", (cid,))
        if row is None or force:
            out.append(cid)
            continue
        fresh = row["last_full_at"] is not None and ts - row["last_full_at"] < cfg.ok_ttl_seconds
        gated = row["consecutive_failures"] > 0 and row["next_probe_at"] is not None and row["next_probe_at"] > ts
        if not fresh and not gated:
            out.append(cid)
    return out


def load_states(db: Database, cids: Iterable[int]) -> dict[int, CandState]:
    cids = list(cids)
    if not cids:
        return {}
    marks = ",".join("?" * len(cids))
    rows = db.query(
        f"SELECT c.id, c.url, c.source_id, c.channel_id, h.* FROM candidates c LEFT JOIN health h ON h.candidate_id = c.id "
        f"WHERE c.id IN ({marks})",
        cids,
    )
    return {r["id"]: _state(r) for r in rows}


def _state(r) -> CandState:
    def get(name, default=None):
        try:
            value = r[name]
        except (IndexError, KeyError):
            return default
        return default if value is None else value

    return CandState(
        cid=r["id"], url=r["url"], source_id=r["source_id"], trust="high", channel_id=r["channel_id"],
        consecutive_failures=get("consecutive_failures", 0), probes_total=get("probes_total", 0), probes_ok=get("probes_ok", 0),
        last_ok=bool(get("last_ok", 0)), last_ok_at=get("last_ok_at"), last_probe_at=get("last_probe_at"),
        kind=get("kind", ""), width=get("width"), height=get("height"), fps=get("fps"), bitrate=get("bitrate"),
        bandwidth=get("bandwidth"), verified=bool(get("verified", 0)), latency_ms=get("latency_ms"),
        last_error=get("last_error", ""), vcodec=get("vcodec", ""), acodec=get("acodec", ""), service=get("service", ""),
    )
