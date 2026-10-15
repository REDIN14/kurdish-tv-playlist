"""Persistence helpers for discovered candidates and source state."""

from __future__ import annotations

import json
from typing import Iterable

from .config import SourceConfig
from .db import Database
from .models import RawCandidate, SourceResult
from .util import url_key

DEFAULT_GRACE_HOURS = 48.0


def upsert_candidate(db: Database, c: RawCandidate, ts: float) -> tuple[int, bool]:
    """Insert or refresh one discovered stream. Returns (candidate id, was_new)."""
    key = url_key(c.url)
    row = db.one("SELECT id FROM candidates WHERE source_id = ? AND url_key = ?", (c.source_id, key))
    fields = (
        c.url, c.name, c.logo_url, json.dumps(list(c.tags), ensure_ascii=False), json.dumps(list(c.aliases), ensure_ascii=False),
        c.language, c.country, c.page_url, c.name_confidence, c.station_id, ts,
    )
    if row is None:
        cur = db.execute(
            "INSERT INTO candidates (source_id, url_key, url, raw_name, logo_url, tags_json, aliases_json, language, country, "
            "page_url, name_confidence, station_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.source_id, key, *fields, ts),
        )
        return int(cur.lastrowid), True
    db.execute(
        "UPDATE candidates SET url = ?, raw_name = ?, logo_url = ?, tags_json = ?, aliases_json = ?, language = ?, country = ?, "
        "page_url = ?, name_confidence = ?, station_id = ?, last_seen = ? WHERE id = ?",
        (*fields, row["id"]),
    )
    return int(row["id"]), False


def record_source_run(db: Database, result: SourceResult, ts: float) -> None:
    """Remember how a source fared. last_success_at moves only on a fully successful read."""
    row = db.one("SELECT consecutive_failures, last_success_at FROM source_state WHERE source_id = ?", (result.source_id,))
    failures = 0 if result.status == "ok" else (row["consecutive_failures"] if row else 0) + 1
    success_at = ts if result.status == "ok" else (row["last_success_at"] if row else None)
    db.execute(
        "INSERT INTO source_state (source_id, last_run_at, last_status, last_http, last_error, last_success_at, last_found, "
        "consecutive_failures, detail_json) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (source_id) DO UPDATE SET last_run_at = excluded.last_run_at, last_status = excluded.last_status, "
        "last_http = excluded.last_http, last_error = excluded.last_error, last_success_at = excluded.last_success_at, "
        "last_found = excluded.last_found, consecutive_failures = excluded.consecutive_failures, detail_json = excluded.detail_json",
        (result.source_id, ts, result.status, result.http_status, result.error, success_at, len(result.candidates), failures,
         json.dumps(result.summary(), ensure_ascii=False)),
    )


def load_active_candidates(
    db: Database, sources: Iterable[SourceConfig], ts: float, grace_hours: float = DEFAULT_GRACE_HOURS
) -> list[tuple[int, RawCandidate, SourceConfig]]:
    """Candidates believed to still exist.

    A candidate a source stops listing stays for `grace_hours` after the last time that source was read
    successfully - and indefinitely while the source itself is down, so an outage never empties the playlist.
    """
    by_id = {s.id: s for s in sources if s.enabled}
    state = {r["source_id"]: r["last_success_at"] for r in db.query("SELECT source_id, last_success_at FROM source_state")}
    out = []
    for r in db.query("SELECT * FROM candidates ORDER BY id"):
        src = by_id.get(r["source_id"])
        if src is None:
            continue
        success_at = state.get(r["source_id"])
        if success_at is not None and success_at > r["last_seen"] + 1 and success_at - r["last_seen"] > grace_hours * 3600:
            continue  # the source was read fine since and no longer lists it
        out.append((r["id"], _to_raw(r), src))
    return out


def _to_raw(r) -> RawCandidate:
    return RawCandidate(
        source_id=r["source_id"],
        url=r["url"],
        name=r["raw_name"] or "",
        aliases=tuple(json.loads(r["aliases_json"] or "[]")),
        logo_url=r["logo_url"],
        tags=tuple(json.loads(r["tags_json"] or "[]")),
        language=r["language"],
        country=r["country"],
        page_url=r["page_url"],
        name_confidence=r["name_confidence"] or "medium",
        station_id=r["station_id"],
    )


def save_assignment(db: Database, cid: int, channel_id: str | None, scope: str, reason: str) -> None:
    db.execute("UPDATE candidates SET channel_id = ?, scope = ?, scope_reason = ? WHERE id = ?", (channel_id, scope, reason[:300], cid))
