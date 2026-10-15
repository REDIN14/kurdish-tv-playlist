"""FastAPI service: /playlist.m3u, /logos, /health, /refresh (admin), /epg.xml (only when real EPG data exists)."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import __version__
from .app import App
from .config import Settings
from .pipeline import RefreshBusy, health_check, refresh
from .playlist import BASE_TOKEN
from .report import iso, load_report, render_report_text, unavailable_html
from .util import now

log = logging.getLogger(__name__)

_LOGO_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{0,80}$")
_HOST_OK = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")
RETRY_AFTER_FAILURE = 15 * 60


def render_playlist(text: str, base_url: str) -> str:
    return text.replace(BASE_TOKEN, base_url.rstrip("/"))


class RateLimiter:
    """Sliding-window limit per client address - keeps a public instance from being hammered or used as a relay."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def allow(self, client: str) -> bool:
        if self.per_minute <= 0:
            return True
        t = time.monotonic()
        window = self._hits[client]
        while window and t - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(t)
        if t - self._last_sweep > 300:
            self._last_sweep = t
            for key in [k for k, v in self._hits.items() if not v or t - v[-1] > 60]:
                self._hits.pop(key, None)
        return True


async def _scheduler(app: App) -> None:
    svc = app.settings.service
    refresh_every = svc.refresh_interval_hours * 3600
    health_every = svc.health_interval_minutes * 60

    async def run_refresh(trigger: str) -> float:
        try:
            report = await refresh(app, trigger)
            ok = report["status"] in {"published", "unchanged", "held"}
        except RefreshBusy:
            ok = True
        except Exception:
            log.exception("scheduled refresh crashed")
            ok = False
        # offline / crashed: look again soon instead of waiting a full interval
        return refresh_every if ok else min(refresh_every, RETRY_AFTER_FAILURE)

    delay = await run_refresh("startup") if svc.refresh_on_startup else refresh_every
    app.state.next_refresh_at = now() + delay
    app.state.next_health_at = now() + health_every
    while True:
        due = min(app.state.next_refresh_at, app.state.next_health_at)
        await asyncio.sleep(max(1.0, due - now()))
        if now() >= app.state.next_refresh_at:
            delay = await run_refresh("schedule")
            app.state.next_refresh_at = now() + delay
        if now() >= app.state.next_health_at:
            try:
                await health_check(app)
            except Exception:
                log.exception("health check crashed")
            app.state.next_health_at = now() + health_every


def create_app(settings: Settings, *, app_obj: App | None = None, start_scheduler: bool = True) -> FastAPI:
    holder: dict[str, App] = {}
    limiter = RateLimiter(settings.service.rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app = app_obj or App(settings)
        holder["app"] = app
        task = asyncio.create_task(_scheduler(app)) if start_scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if app_obj is None:
                await app.aclose()

    api = FastAPI(title="Kurdish IPTV aggregator", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def core() -> App:
        return holder["app"]

    @api.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return PlainTextResponse("Too many requests\n", status_code=429, headers={"Retry-After": "60"})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path in {"/playlist.m3u", "/epg.xml"} or request.url.path.startswith("/logos/"):
            response.headers.setdefault("Access-Control-Allow-Origin", "*")
        return response

    def base_url(request: Request) -> str:
        if settings.service.public_base_url:
            return settings.service.public_base_url
        proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip()
        if proto not in {"http", "https"} or not _HOST_OK.match(host or ""):
            host, proto = request.url.netloc, request.url.scheme
        return f"{proto}://{host}"

    def require_admin(authorization: str | None, x_admin_token: str | None) -> None:
        supplied = x_admin_token or ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, settings.admin_token()):
            raise HTTPException(status_code=401, detail="admin token required", headers={"WWW-Authenticate": "Bearer"})

    # ---- public ------------------------------------------------------------------------------------------------------
    @api.api_route("/playlist.m3u", methods=["GET", "HEAD"])
    async def playlist(request: Request):
        published = core().publisher.current()
        if published is None:
            return PlainTextResponse(
                "The first playlist is still being built. Try again in a few minutes.\n",
                status_code=503, headers={"Retry-After": "60"},
            )
        body = render_playlist(published.playlist, base_url(request))
        etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest()[:20] + '"'
        headers = {"ETag": etag, "Cache-Control": "public, max-age=300", "Content-Disposition": 'inline; filename="kurdish-tv.m3u"'}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="audio/x-mpegurl; charset=utf-8", headers=headers)

    @api.api_route("/logos/{name}.png", methods=["GET", "HEAD"])
    async def logo(name: str):
        if not _LOGO_NAME.match(name):
            raise HTTPException(status_code=404)
        path = settings.logos_dir / f"{name}.png"
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @api.api_route("/epg.xml", methods=["GET", "HEAD"])
    async def epg():
        path = settings.data_dir / "epg.xml"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no reliable EPG data is available for these channels")
        return FileResponse(path, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})

    @api.get("/health")
    async def health():
        app = core()
        pub = app.publisher.current()
        meta = pub.meta if pub else {}
        created = meta.get("checked_at") or meta.get("created_at")  # last time the playlist was (re)verified
        st = app.state
        sources = [
            {
                "id": r["source_id"], "status": r["last_status"], "last_run": iso(r["last_run_at"]),
                "last_success": iso(r["last_success_at"]), "candidates": r["last_found"], "http": r["last_http"],
                "error": r["last_error"] or "", "consecutive_failures": r["consecutive_failures"],
            }
            for r in app.db.query("SELECT * FROM source_state ORDER BY source_id")
        ]
        age = None
        if pub and created:
            age = max(0, int(now() - calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))))
        stale_after = settings.service.refresh_interval_hours * 3600 * 2 + 3600
        last = st.last_refresh or {}
        if pub is None:
            status = "starting"
        elif (age is not None and age > stale_after) or last.get("status") in {"failed", "held"}:
            status = "degraded"
        else:
            status = "ok"
        body = {
            "status": status,
            "version": __version__,
            "uptime_s": int(now() - st.started_at),
            "playlist": {
                "available": pub is not None, "snapshot": meta.get("snapshot_id"), "created_at": meta.get("created_at"),
                "verified_at": created, "age_s": age, "channels": meta.get("channels"), "streams": meta.get("streams"),
            },
            "refresh": {"running": st.refreshing, "last": st.last_refresh, "next_at": iso(st.next_refresh_at)},
            "health_check": {"running": st.checking, "last": st.last_health, "next_at": iso(st.next_health_at)},
            "sources": sources,
            "ffprobe": bool(app.ffprobe),
        }
        return JSONResponse(body, status_code=200 if pub else 503)

    @api.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        app = core()
        pub = app.publisher.current()
        url = base_url(request) + "/playlist.m3u"
        meta = pub.meta if pub else {}
        status = (
            f"{meta.get('channels', '?')} channels, updated {html.escape(str(meta.get('created_at', '?')))}"
            if pub else "The first playlist is still being built - reload in a few minutes."
        )
        unavailable = unavailable_html(load_report(settings.published_dir))
        return HTMLResponse(_INDEX.format(url=html.escape(url), status=status, version=__version__, unavailable=unavailable))

    # ---- admin -------------------------------------------------------------------------------------------------------
    @api.post("/refresh", status_code=202)
    async def trigger_refresh(authorization: str | None = Header(default=None), x_admin_token: str | None = Header(default=None)):
        require_admin(authorization, x_admin_token)
        app = core()
        if app.lock.locked():
            raise HTTPException(status_code=409, detail="a refresh or health check is already running")

        async def run():
            try:
                await refresh(app, "manual")
            except RefreshBusy:
                pass
            except Exception:
                log.exception("manual refresh crashed")

        asyncio.create_task(run())
        return {"status": "started"}

    @api.get("/report")
    async def report(format: str = "text", authorization: str | None = Header(default=None), x_admin_token: str | None = Header(default=None)):
        require_admin(authorization, x_admin_token)
        app = core()
        path: Path = settings.published_dir / "report.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no report yet")
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if format == "json":
            return JSONResponse(data)
        return PlainTextResponse(render_report_text(data) if "sources" in data else json.dumps(data, indent=2))

    return api


_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurdish TV playlist</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--muted:#5b6670;--card:#f3f5f7;--accent:#0b6bcb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12161b;--fg:#e8ecef;--muted:#9aa6b1;--card:#1c232b;--accent:#6cb2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:44rem;margin:0 auto;padding:2rem 1rem}}
h1{{font-size:1.5rem;margin:0 0 .25rem}} p.muted{{color:var(--muted);margin:.25rem 0 1.5rem}}
.url{{display:flex;gap:.5rem;flex-wrap:wrap}} code{{flex:1;min-width:14rem;background:var(--card);padding:.7rem .8rem;border-radius:.5rem;overflow-wrap:anywhere}}
button{{background:var(--accent);color:#fff;border:0;border-radius:.5rem;padding:.7rem 1rem;font:inherit;cursor:pointer}}
ul{{padding-left:1.2rem}} li{{margin:.35rem 0}} small{{color:var(--muted)}}
</style></head><body><main>
<h1>Kurdish TV playlist</h1>
<p class="muted">{status}</p>
<div class="url"><code id="u">{url}</code><button onclick="navigator.clipboard.writeText(document.getElementById('u').textContent);this.textContent='Copied'">Copy</button></div>
<h2 style="font-size:1.1rem;margin-top:2rem">Add it to your player</h2>
<ul>
<li><b>VLC</b>: Media &rarr; Open Network Stream &rarr; paste the address, then View &rarr; Playlist.</li>
<li><b>TiviMate</b>: Add playlist &rarr; M3U playlist &rarr; paste the address.</li>
<li><b>Kodi</b>: PVR IPTV Simple Client &rarr; M3U play list URL &rarr; paste the address.</li>
<li><b>OTT Navigator</b>: Add provider &rarr; Playlist &rarr; paste the address.</li>
</ul>
{unavailable}
<p><small>Channels in the group "Backup streams" are alternative links for the same channel - use them if the main one stops.
The list refreshes itself; there is nothing to update. v{version}</small></p>
</main></body></html>"""
