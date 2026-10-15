"""Snapshots and atomic publication of the playlist (last known good is never replaced by something worse)."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .util import atomic_write_text, dumps, now


@dataclass
class Published:
    playlist: str
    meta: dict

    @property
    def snapshot_id(self) -> str:
        return self.meta.get("snapshot_id", "")


class Publisher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.published = settings.published_dir
        self.snapshots = settings.snapshots_dir
        self.published.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)

    # ---- reading -----------------------------------------------------------------------------------------------------
    def current(self) -> Published | None:
        path = self.published / "playlist.m3u"
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            meta = json.loads((self.published / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Published(text, meta) if text.startswith("#EXTM3U") else None

    def channels(self) -> list[dict]:
        try:
            return json.loads((self.published / "channels.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    # ---- writing -----------------------------------------------------------------------------------------------------
    def new_snapshot_id(self) -> str:
        """Fixed-width, strictly increasing ids (UTC time to the millisecond), so name order is time order."""
        ts = now()
        base = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts)) + f"-{int(ts * 1000) % 1000:03d}"
        sid, n = base, 0
        while (self.snapshots / sid).exists() or any(p.name > sid for p in self.snapshots.iterdir() if p.is_dir()):
            n += 1
            sid = f"{base}-{n:02d}"
            if n > 99:
                break
        return sid

    def publish(self, snapshot_id: str, playlist: str, channels: list[dict], report: dict, meta: dict) -> Path:
        """Write the snapshot, then switch the live files over. Call only with a playlist that passed validation."""
        meta = {**meta, "snapshot_id": snapshot_id}
        snap = self.snapshots / snapshot_id
        snap.mkdir(parents=True, exist_ok=True)
        atomic_write_text(snap / "playlist.m3u", playlist)
        atomic_write_text(snap / "channels.json", dumps(channels))
        atomic_write_text(snap / "report.json", dumps(report))
        atomic_write_text(snap / "meta.json", dumps(meta))
        self._activate(snap)
        self.rotate()
        return snap

    def touch(self, snapshot_id: str, checked_at: str, channels: list[dict] | None = None) -> None:
        """A refresh found nothing new: keep the snapshot, record that the playlist was re-verified just now."""
        for target in (self.snapshots / snapshot_id, self.published):
            meta_path = target / "meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if target is self.published and meta.get("snapshot_id") != snapshot_id:
                continue
            meta["checked_at"] = checked_at
            atomic_write_text(meta_path, dumps(meta))
            if channels is not None:
                atomic_write_text(target / "channels.json", dumps(channels))

    def update_report(self, snapshot_id: str, report: dict, text: str) -> None:
        """Store the finished report (with timings) next to the snapshot and as the live report."""
        snap = self.snapshots / snapshot_id
        for target in (snap, self.published):
            if target is self.published and (self.current() is None or self.current().snapshot_id != snapshot_id):
                continue
            atomic_write_text(target / "report.json", dumps(report))
            atomic_write_text(target / "report.txt", text)

    def _activate(self, snap: Path) -> None:
        # Supporting files first, the playlist last: readers never see a new playlist with an old meta/report.
        for name in ("channels.json", "report.json", "meta.json"):
            atomic_write_text(self.published / name, (snap / name).read_text(encoding="utf-8"))
        atomic_write_text(self.published / "playlist.m3u", (snap / "playlist.m3u").read_text(encoding="utf-8"))

    def rotate(self) -> None:
        keep = max(1, self.settings.publish.keep_snapshots)
        snaps = sorted((p for p in self.snapshots.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
        for old in snaps[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    def list_snapshots(self) -> list[str]:
        return sorted((p.name for p in self.snapshots.iterdir() if p.is_dir()), reverse=True)

    def rollback(self, snapshot_id: str | None = None) -> str:
        """Re-activate an earlier snapshot (the newest one if none is named after the live one)."""
        names = self.list_snapshots()
        if not names:
            raise RuntimeError("no snapshots to roll back to")
        live = (self.current().snapshot_id if self.current() else "")
        target = snapshot_id or next((n for n in names if n != live), None)
        if target is None or target not in names:
            raise RuntimeError(f"snapshot {snapshot_id!r} not found (have: {', '.join(names)})")
        self._activate(self.snapshots / target)
        return target
