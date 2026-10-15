import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from kurdiptv.m3u import parse_m3u
from kurdiptv.playlist import BASE_TOKEN, PublishedChannel, PublishedStream, render_m3u
from kurdiptv.server import RateLimiter, create_app

from helpers import png_bytes


def channels(n=6):
    return [
        PublishedChannel(id=f"ch{i}", name=f"Channel {i}", group="News", language="ckb", country="IQ", logo_version="v1",
                         streams=[PublishedStream(cid=i, url=f"https://cdn{i}.example/live.m3u8", source_id="s", height=720)])
        for i in range(n)
    ]


@pytest.fixture
def served(make_app, settings):
    """An App with one published snapshot, and a TestClient on top of it (no scheduler, no network)."""
    app = make_app()

    def publish(n=6, **meta):
        pub = app.publisher
        text = render_m3u(channels(n), settings.publish)
        pub.publish(pub.new_snapshot_id(), text, [], {"sources": []}, {"created_at": "2026-10-15T12:00:00Z", "checked_at": "2026-10-15T12:00:00Z", "channels": n, "streams": n, **meta})
        for i in range(n):
            (settings.logos_dir / f"ch{i}.png").parent.mkdir(parents=True, exist_ok=True)
            (settings.logos_dir / f"ch{i}.png").write_bytes(png_bytes())

    with TestClient(create_app(settings, app_obj=app, start_scheduler=False)) as client:
        client.publish = publish
        client.core = app
        yield client


def admin(served):
    return {"Authorization": f"Bearer {served.core.settings.admin_token()}"}


# ---- playlist ----------------------------------------------------------------------------------------------------------------------

def test_no_playlist_yet_is_a_503_never_an_empty_playlist(served):
    r = served.get("/playlist.m3u")
    assert r.status_code == 503 and r.headers["retry-after"] == "60" and "#EXTM3U" not in r.text


def test_playlist_is_served_as_a_standard_m3u(served):
    served.publish()
    r = served.get("/playlist.m3u")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/x-mpegurl")
    assert r.text.startswith("#EXTM3U\n") and BASE_TOKEN not in r.text
    assert len(parse_m3u(r.text)) == 6
    assert "tvg-logo=\"http://testserver/logos/ch0.png?v=v1\"" in r.text  # absolute logo URLs for any player
    assert r.headers["cache-control"] == "public, max-age=300" and r.headers["access-control-allow-origin"] == "*"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-length" in r.headers and r.content.decode("utf-8") == r.text  # plain UTF-8, no BOM
    assert not r.content.startswith(b"\xef\xbb\xbf")


def test_etag_and_conditional_requests_and_head(served):
    served.publish()
    r = served.get("/playlist.m3u")
    etag = r.headers["etag"]
    assert served.get("/playlist.m3u", headers={"If-None-Match": etag}).status_code == 304
    served.publish(7)  # a new playlist has a new ETag
    assert served.get("/playlist.m3u", headers={"If-None-Match": etag}).status_code == 200
    h = served.head("/playlist.m3u")
    assert h.status_code == 200 and h.content == b""


def test_logo_base_url_follows_the_request_host_but_is_validated(served):
    served.publish()
    assert "https://tv.example.org/logos/ch0.png" in served.get(
        "/playlist.m3u", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "tv.example.org"}).text
    evil = served.get("/playlist.m3u", headers={"Host": 'evil.com/"><script>'}).text
    assert "<script>" not in evil and "evil.com" not in evil
    spoof = served.get("/playlist.m3u", headers={"X-Forwarded-Proto": "javascript", "X-Forwarded-Host": "x"}).text
    assert "javascript:" not in spoof


def test_configured_public_url_always_wins(make_app, settings):
    cfg = dataclasses.replace(settings, service=dataclasses.replace(settings.service, public_base_url="https://pages.example.io/kurdish"))
    app = make_app(settings_override=cfg)
    text = render_m3u(channels(), cfg.publish)
    app.publisher.publish(app.publisher.new_snapshot_id(), text, [], {}, {"created_at": "2026-01-01T00:00:00Z", "channels": 6})
    with TestClient(create_app(cfg, app_obj=app, start_scheduler=False)) as client:
        body = client.get("/playlist.m3u", headers={"X-Forwarded-Host": "other.example"}).text
        assert 'tvg-logo="https://pages.example.io/kurdish/logos/ch0.png?v=v1"' in body


# ---- logos ---------------------------------------------------------------------------------------------------------------------------

def test_logos(served):
    served.publish()
    r = served.get("/logos/ch0.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert "max-age" in r.headers["cache-control"]
    assert served.head("/logos/ch0.png").status_code == 200
    for bad in ("/logos/nope.png", "/logos/CH0.png", "/logos/..%2F..%2Fstate.db.png", "/logos/%2e%2e%2fstate.db.png", "/logos/a%00b.png",
                "/logos/http:%2F%2Fevil.example%2Fx.png"):
        assert served.get(bad).status_code == 404, bad


# ---- health ---------------------------------------------------------------------------------------------------------------------------

def test_health_before_and_after_the_first_playlist(served):
    r = served.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "starting" and not r.json()["playlist"]["available"]
    served.publish()
    body = served.get("/health").json()
    assert served.get("/health").status_code == 200
    assert body["playlist"]["channels"] == 6 and body["playlist"]["snapshot"] and body["refresh"]["running"] is False
    assert {"status", "version", "uptime_s", "sources", "ffprobe", "health_check"} <= set(body)


def test_health_reports_sources_and_degradation(served):
    served.publish(created_at="2026-10-01T12:00:00Z", checked_at="2026-10-01T12:00:00Z")  # two weeks old
    app = served.core
    app.db.execute("INSERT INTO source_state (source_id, last_run_at, last_status, last_http, last_error, last_success_at, last_found, consecutive_failures) "
                   "VALUES ('kurdtvs', 1, 'failed', 503, 'HTTP 503', 1, 0, 2)")
    body = served.get("/health").json()
    assert body["sources"][0]["id"] == "kurdtvs" and body["sources"][0]["status"] == "failed" and body["sources"][0]["consecutive_failures"] == 2
    assert body["status"] == "degraded"  # the playlist has not been re-verified for far longer than two refresh intervals
    app.state.last_refresh = {"status": "held"}
    assert served.get("/health").json()["status"] == "degraded"
    from kurdiptv.report import iso
    from kurdiptv import util

    served.publish(checked_at=iso(util.now()), created_at=iso(util.now()))
    app.state.last_refresh = {"status": "held"}  # a recent playlist, but the last refresh was held back
    assert served.get("/health").json()["status"] == "degraded"
    app.state.last_refresh = {"status": "unchanged"}
    assert served.get("/health").json()["status"] == "ok"


def test_health_is_ok_for_a_recently_verified_playlist(served):
    from kurdiptv.report import iso
    from kurdiptv import util

    served.publish(checked_at=iso(util.now() - 60), created_at=iso(util.now() - 86400))
    body = served.get("/health").json()
    assert body["status"] == "ok" and 50 <= body["playlist"]["age_s"] <= 120


# ---- admin -------------------------------------------------------------------------------------------------------------------------------

def test_refresh_needs_the_admin_token(served, monkeypatch):
    started = []

    async def fake_refresh(app, trigger, **kw):
        started.append(trigger)
        return {"status": "published"}

    monkeypatch.setattr("kurdiptv.server.refresh", fake_refresh)
    assert served.post("/refresh").status_code == 401
    assert served.post("/refresh", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert served.post("/refresh", headers={"X-Admin-Token": ""}).status_code == 401
    assert served.get("/refresh").status_code == 405  # state-changing: POST only
    assert served.post("/refresh", headers=admin(served)).status_code == 202
    assert served.post("/refresh", headers={"X-Admin-Token": served.core.settings.admin_token()}).status_code == 202
    assert started == ["manual", "manual"]


def test_refresh_while_busy_is_a_409(served):
    served.core.lock = type("Busy", (), {"locked": lambda self: True})()  # a refresh/health check holds the lock
    assert served.post("/refresh", headers=admin(served)).status_code == 409


def test_report_is_admin_only(served):
    served.publish()
    assert served.get("/report").status_code == 401
    r = served.get("/report", headers=admin(served))
    assert r.status_code == 200
    assert served.get("/report?format=json", headers=admin(served)).json() == {"sources": []}


def test_admin_token_is_persistent_and_overridable(settings, monkeypatch):
    t1 = settings.admin_token()
    assert t1 == settings.admin_token() and len(t1) >= 24  # stored in the data dir, not regenerated
    monkeypatch.setenv("KURDIPTV_ADMIN_TOKEN", "from-env")
    assert settings.admin_token() == "from-env"


# ---- epg, landing page, hardening --------------------------------------------------------------------------------------------------------

def test_epg_only_exists_when_there_is_real_data(served, settings):
    assert served.get("/epg.xml").status_code == 404
    (settings.data_dir / "epg.xml").write_text('<?xml version="1.0"?><tv></tv>', encoding="utf-8")
    r = served.get("/epg.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")


def test_landing_page_shows_the_playlist_address(served):
    served.publish()
    html = served.get("/").text
    assert "http://testserver/playlist.m3u" in html and "6 channels" in html and "VLC" in html and "TiviMate" in html


def test_api_docs_and_unknown_paths_are_not_exposed(served):
    for path in ("/docs", "/redoc", "/openapi.json", "/admin", "/channels.json", "/state.db", "/../config/settings.yaml"):
        assert served.get(path).status_code == 404, path


def test_rate_limiting(make_app, settings):
    cfg = dataclasses.replace(settings, service=dataclasses.replace(settings.service, rate_limit_per_minute=3))
    app = make_app(settings_override=cfg)
    with TestClient(create_app(cfg, app_obj=app, start_scheduler=False)) as client:
        codes = [client.get("/health").status_code for _ in range(5)]
        assert codes[:3] == [503, 503, 503] and codes[3:] == [429, 429]
        assert client.get("/health").headers["retry-after"] == "60"


def test_rate_limiter_window_and_cleanup():
    rl = RateLimiter(2)
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")  # clients are independent
    assert RateLimiter(0).allow("x")  # 0 disables the limit
