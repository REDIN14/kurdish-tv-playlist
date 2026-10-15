"""Command line: serve, refresh, health-check, status, report, rollback, build-static, admin-token."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .app import App
from .config import ConfigError, load_settings
from .pipeline import RefreshBusy, health_check, refresh
from .playlist import BASE_TOKEN, validate_m3u
from .publish import Publisher
from .report import iso, load_report, render_report_text, unavailable_html
from .server import render_playlist
from .util import atomic_write_text

EXIT_CODES = {"published": 0, "unchanged": 0, "offline": 3, "held": 2, "failed": 1}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(args):
    try:
        return load_settings(args.config, args.data)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2)


async def _with_app(args, work):
    app = App(_settings(args))
    try:
        return await work(app)
    finally:
        await app.aclose()


# ---- commands -----------------------------------------------------------------------------------------------------

def cmd_refresh(args) -> int:
    async def work(app: App):
        try:
            return await refresh(app, "cli", discover=not args.no_discover, force=args.force)
        except RefreshBusy as exc:
            print(str(exc), file=sys.stderr)
            return None

    report = asyncio.run(_with_app(args, work))
    if report is None:
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_report_text(report))
    return EXIT_CODES.get(report["status"], 1)


def cmd_health_check(args) -> int:
    out = asyncio.run(_with_app(args, health_check))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("status") in {"ok", "skipped"} else 2


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app

    settings = _settings(args)
    if args.no_startup_refresh:
        import dataclasses

        settings = dataclasses.replace(settings, service=dataclasses.replace(settings.service, refresh_on_startup=False))
    host = args.host or settings.service.host
    port = args.port or settings.service.port
    print(f"Kurdish IPTV playlist: http://localhost:{port}/playlist.m3u  (listening on {host}:{port})", flush=True)
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info", access_log=False)
    return 0


def cmd_status(args) -> int:
    settings = _settings(args)
    pub = Publisher(settings)
    cur = pub.current()
    if cur is None:
        print("No playlist published yet. Run: kurdiptv refresh")
        return 1
    print(f"snapshot  {cur.snapshot_id}")
    print(f"created   {cur.meta.get('created_at')}  ({cur.meta.get('channels')} channels, {cur.meta.get('streams')} streams)")
    print(f"snapshots {', '.join(pub.list_snapshots())}")
    problems = validate_m3u(cur.playlist, settings.publish, 0)
    print("validation", "OK" if not problems else "; ".join(problems))
    return 0


def cmd_report(args) -> int:
    settings = _settings(args)
    path = settings.published_dir / ("report.json" if args.json else "report.txt")
    if not path.exists():
        print("no report yet")
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_rollback(args) -> int:
    settings = _settings(args)
    try:
        target = Publisher(settings).rollback(args.snapshot)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"playlist rolled back to snapshot {target}")
    return 0


def cmd_admin_token(args) -> int:
    print(_settings(args).admin_token())
    return 0


def cmd_build_static(args) -> int:
    """Export the published playlist as plain files for a static host (GitHub Pages, S3, any web server)."""
    settings = _settings(args)
    base = (args.base_url or settings.service.public_base_url or "").rstrip("/")
    if not base:
        print("--base-url (or service.public_base_url) is required: logo links in the playlist must be absolute", file=sys.stderr)
        return 2
    pub = Publisher(settings)
    cur = pub.current()
    if cur is None:
        print("nothing published yet; run `kurdiptv refresh` first", file=sys.stderr)
        return 1
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    text = render_playlist(cur.playlist, base)
    if BASE_TOKEN in text:
        print("internal error: base token left in playlist", file=sys.stderr)
        return 1
    atomic_write_text(out / "playlist.m3u", text)
    logos_out = out / "logos"
    logos_out.mkdir(exist_ok=True)
    wanted = {f"{c['id']}.png" for c in pub.channels()}
    for name in wanted:
        src = settings.logos_dir / name
        if src.is_file():
            shutil.copy2(src, logos_out / name)
    for stale in logos_out.glob("*.png"):
        if stale.name not in wanted:
            stale.unlink()
    for name in ("report.txt", "report.json"):
        src = settings.published_dir / name
        if src.is_file() and not args.no_report:
            shutil.copy2(src, out / name)
    health = {
        "status": "ok", "generated_at": cur.meta.get("checked_at") or cur.meta.get("created_at"), "snapshot": cur.snapshot_id,
        "channels": cur.meta.get("channels"), "streams": cur.meta.get("streams"), "version": __version__,
    }
    atomic_write_text(out / "health.json", json.dumps(health, indent=2))
    atomic_write_text(out / ".nojekyll", "")
    atomic_write_text(out / "index.html", _static_index(base + "/playlist.m3u", cur.meta, unavailable_html(load_report(settings.published_dir))))
    print(f"static site written to {out} ({len(wanted)} channels) - playlist URL: {base}/playlist.m3u")
    return 0


def _static_index(url: str, meta: dict, unavailable: str = "") -> str:
    import html

    from .server import _INDEX

    status = f"{meta.get('channels', '?')} channels, updated {html.escape(str(meta.get('created_at', '?')))}"
    return _INDEX.format(url=html.escape(url), status=status, version=__version__, unavailable=unavailable)


# ---- entry point -----------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kurdiptv", description="Kurdish IPTV playlist aggregator")
    p.add_argument("--version", action="version", version=f"kurdiptv {__version__}")
    p.add_argument("--config", help="config directory (default: ./config)")
    p.add_argument("--data", help="data directory (default: ./data)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the HTTP service (refreshes on start and on a schedule)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-startup-refresh", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("refresh", help="discover, probe, rank and publish once")
    s.add_argument("--force", action="store_true", help="ignore cached pages and re-probe everything")
    s.add_argument("--no-discover", action="store_true", help="skip the source sites, re-evaluate known streams only")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("health-check", help="light re-probe of published streams, promote fallbacks if needed")
    s.set_defaults(func=cmd_health_check)

    s = sub.add_parser("status", help="show what is currently published")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("report", help="print the latest refresh report")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("rollback", help="re-publish an earlier snapshot")
    s.add_argument("--snapshot")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("build-static", help="export playlist + logos as static files")
    s.add_argument("outdir")
    s.add_argument("--base-url", help="public address the files will be served from, e.g. https://user.github.io/repo")
    s.add_argument("--no-report", action="store_true", help="do not include report files")
    s.set_defaults(func=cmd_build_static)

    s = sub.add_parser("admin-token", help="print the token that authorises POST /refresh")
    s.set_defaults(func=cmd_admin_token)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    raise SystemExit(args.func(args))
