"""SQLite state: sources, candidates, probe history, health, channel state, HTTP cache."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL,           -- running | published | held | failed | offline
    snapshot_id TEXT,
    report_json TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source_id           TEXT PRIMARY KEY,
    last_run_at         REAL,
    last_status         TEXT,            -- ok | partial | failed | blocked | skipped
    last_http           INTEGER,
    last_error          TEXT,
    last_success_at     REAL,
    last_found          INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    detail_json         TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    url_key         TEXT NOT NULL,
    raw_name        TEXT,
    logo_url        TEXT,
    tags_json       TEXT,
    aliases_json    TEXT,
    language        TEXT,
    country         TEXT,
    page_url        TEXT,
    name_confidence TEXT,
    station_id      TEXT,
    channel_id      TEXT,
    scope           TEXT,                -- in | out | review
    scope_reason    TEXT,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    UNIQUE (source_id, url_key)
);
CREATE INDEX IF NOT EXISTS idx_candidates_channel ON candidates (channel_id);

CREATE TABLE IF NOT EXISTS probes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    mode       TEXT NOT NULL,            -- full | light
    ok         INTEGER NOT NULL,
    kind       TEXT,
    http_status INTEGER,
    latency_ms INTEGER,
    width      INTEGER,
    height     INTEGER,
    fps        REAL,
    vcodec     TEXT,
    acodec     TEXT,
    bandwidth  INTEGER,
    bitrate    INTEGER,
    verified   INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_candidate ON probes (candidate_id, ts);

CREATE TABLE IF NOT EXISTS health (
    candidate_id         INTEGER PRIMARY KEY REFERENCES candidates (id) ON DELETE CASCADE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    probes_total         INTEGER NOT NULL DEFAULT 0,
    probes_ok            INTEGER NOT NULL DEFAULT 0,
    last_probe_at        REAL,
    last_full_at         REAL,
    last_ok_at           REAL,
    last_fail_at         REAL,
    next_probe_at        REAL,
    last_ok              INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    kind                 TEXT,
    width                INTEGER,
    height               INTEGER,
    fps                  REAL,
    vcodec               TEXT,
    acodec               TEXT,
    service              TEXT,
    bandwidth            INTEGER,
    bitrate              INTEGER,
    verified             INTEGER NOT NULL DEFAULT 0,
    latency_ms           INTEGER,
    final_url            TEXT,
    tail_marker          TEXT,
    tail_changed_at      REAL
);

CREATE TABLE IF NOT EXISTS channel_state (
    channel_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    group_title       TEXT,
    first_seen_at     REAL NOT NULL,
    last_published_at REAL,
    last_verified_at  REAL,
    primary_candidate_id INTEGER,
    candidate_ids_json TEXT,
    status            TEXT NOT NULL      -- published | stale | dropped
);

CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    fetched_at    REAL NOT NULL,
    status        INTEGER NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    content_type  TEXT,
    body          BLOB
);

CREATE TABLE IF NOT EXISTS logo_state (
    channel_id  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,           -- source | catalog | placeholder
    origin_url  TEXT,
    fetched_at  REAL NOT NULL,
    sha1        TEXT,
    width       INTEGER,
    height      INTEGER
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- query helpers -----------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Transaction; nested use joins the outer one."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    # --- key/value ---------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
