import dataclasses
import json
from pathlib import Path

import pytest
from PIL import Image

from kurdiptv import cli
from kurdiptv.config import ConfigError, load_settings
from kurdiptv.playlist import PublishedChannel, PublishedStream, render_m3u

from helpers import png_bytes
from world import CANARY, build_world

REPO = Path(__file__).resolve().parents[1]


# ---- configuration --------------------------------------------------------------------------------------------------------------------

def copy_config(tmp_path, **replacements):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("settings.yaml", "catalog.yaml", "policy.yaml"):
        text = (REPO / "config" / name).read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        (cfg / name).write_text(text, encoding="utf-8")
    return cfg


def test_shipped_configuration_is_valid_and_complete():
    s = load_settings(config_dir=REPO / "config", data_dir=REPO / ".pytest_tmp" / "cfgcheck")
    assert [x.id for x in s.sources] == ["kurdtvs", "kurditv", "site123", "karwan"]
    assert s.service.refresh_interval_hours == 6 and s.service.health_interval_minutes == 15
    assert s.service.refresh_on_startup and s.publish.keep_snapshots == 3
    assert {x.id: x.trust for x in s.sources} == {"kurdtvs": "high", "kurditv": "high", "site123": "low", "karwan": "low"}
    assert s.http.respect_robots and not s.http.allow_private_hosts


def test_default_user_agent_is_descriptive_and_has_no_personal_data():
    s = load_settings(config_dir=REPO / "config", data_dir=REPO / ".pytest_tmp" / "cfgcheck")
    assert "KurdishIPTVAggregator" in s.effective_user_agent and "@" not in s.effective_user_agent


def test_contact_is_appended_only_when_configured(monkeypatch):
    monkeypatch.setenv("KURDIPTV_CONTACT", "https://example.org/about")
    s = load_settings(config_dir=REPO / "config", data_dir=REPO / ".pytest_tmp" / "cfgcheck")
    assert s.effective_user_agent.endswith("contact: https://example.org/about")


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("KURDIPTV_PUBLIC_URL", "https://tv.example.org/base/")
    monkeypatch.setenv("KURDIPTV_PORT", "9999")
    monkeypatch.setenv("KURDIPTV_HOST", "127.0.0.1")
    s = load_settings(config_dir=REPO / "config", data_dir=REPO / ".pytest_tmp" / "cfgcheck")
    assert (s.service.public_base_url, s.service.port, s.service.host) == ("https://tv.example.org/base", 9999, "127.0.0.1")


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("service:\n  host", "service:\n  prot: 1\n  host", "unknown key"),
        ("fallback_mode: group", "fallback_mode: sideways", "fallback_mode"),
        ("fail_threshold: 3", "fail_threshold: 0", "fail_threshold"),
        ("trust: high\n    min_interval: 1.0\n    max_requests: 150", "trust: maybe\n    min_interval: 1.0\n    max_requests: 150", "trust"),
        ("min_ratio_of_previous: 0.5", "min_ratio_of_previous: 1.5", "min_ratio"),
        ("- id: kurditv", "- id: kurdtvs", "duplicate source"),
    ],
)
def test_bad_configuration_fails_loudly(tmp_path, old, new, message):
    cfg = copy_config(tmp_path, **{old: new})
    with pytest.raises(ConfigError, match=message):
        load_settings(config_dir=cfg, data_dir=tmp_path / "d")


def test_broken_yaml_and_missing_config(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text("service: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_settings(config_dir=cfg, data_dir=tmp_path / "d")
    with pytest.raises(ConfigError, match="not found"):
        load_settings(config_dir=tmp_path / "nowhere", data_dir=tmp_path / "d")


# ---- CLI --------------------------------------------------------------------------------------------------------------------------------

def run_cli(capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def publish_sample(settings, n=6):
    from kurdiptv.app import App
    from kurdiptv.net import SafeHttp

    app = App(settings, canary_urls=())
    chans = [PublishedChannel(id=f"ch{i}", name=f"Channel {i}", group="News", logo_version="v1",
                              streams=[PublishedStream(cid=i, url=f"https://cdn{i}.example/l.m3u8", source_id="s")]) for i in range(n)]
    app.publisher.publish(app.publisher.new_snapshot_id(), render_m3u(chans, settings.publish),
                          [{"id": c.id} for c in chans], {}, {"created_at": "2026-10-15T12:00:00Z", "channels": n, "streams": n})
    settings.logos_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n + 1):  # one logo of a channel that is no longer published
        (settings.logos_dir / f"ch{i}.png").write_bytes(png_bytes())
    app.db.close()


def args(settings):
    return ["--config", str(REPO / "config"), "--data", str(settings.data_dir)]


def test_status_without_a_playlist(settings, capsys):
    code, out, _ = run_cli(capsys, *args(settings), "status")
    assert code == 1 and "No playlist published yet" in out


def test_status_and_report_after_publishing(settings, capsys):
    publish_sample(settings)
    code, out, _ = run_cli(capsys, *args(settings), "status")
    assert code == 0 and "6 channels" in out and "validation OK" in out


def test_admin_token_command(settings, capsys):
    code, out, _ = run_cli(capsys, *args(settings), "admin-token")
    assert code == 0 and out.strip() == settings.admin_token()


def test_rollback_command(settings, capsys):
    publish_sample(settings, 6)
    publish_sample(settings, 8)
    code, out, _ = run_cli(capsys, *args(settings), "rollback")
    assert code == 0 and "rolled back" in out
    code, out, err = run_cli(capsys, *args(settings), "rollback", "--snapshot", "does-not-exist")
    assert code == 1 and "not found" in err


def test_build_static_writes_a_ready_to_host_site(settings, tmp_path, capsys):
    publish_sample(settings)
    out_dir = tmp_path / "site"
    code, out, _ = run_cli(capsys, *args(settings), "build-static", str(out_dir), "--base-url", "https://user.github.io/kurdish-tv")
    assert code == 0 and "https://user.github.io/kurdish-tv/playlist.m3u" in out
    playlist = (out_dir / "playlist.m3u").read_text(encoding="utf-8")
    assert "{{BASE_URL}}" not in playlist and 'tvg-logo="https://user.github.io/kurdish-tv/logos/ch0.png?v=v1"' in playlist
    assert sorted(p.name for p in (out_dir / "logos").glob("*.png")) == [f"ch{i}.png" for i in range(6)]  # only published channels
    assert Image.open(out_dir / "logos" / "ch0.png").size
    assert (out_dir / ".nojekyll").exists() and json.loads((out_dir / "health.json").read_text())["channels"] == 6
    assert "https://user.github.io/kurdish-tv/playlist.m3u" in (out_dir / "index.html").read_text(encoding="utf-8")


def test_build_static_needs_an_absolute_base_url_and_a_playlist(settings, tmp_path, capsys):
    code, _, err = run_cli(capsys, *args(settings), "build-static", str(tmp_path / "x"))
    assert code == 2 and "--base-url" in err
    code, _, err = run_cli(capsys, *args(settings), "build-static", str(tmp_path / "x"), "--base-url", "https://a.example")
    assert code == 1 and "nothing published" in err


def test_refresh_command_end_to_end(settings, web, capsys, monkeypatch):
    from kurdiptv.app import App
    from kurdiptv.net import SafeHttp

    build_world(web)
    small = dataclasses.replace(settings, publish=dataclasses.replace(settings.publish, min_channels=3))

    def make(_settings):
        http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
        return App(small, http=http, canary_urls=(CANARY,))

    monkeypatch.setattr(cli, "App", make)
    code, out, _ = run_cli(capsys, *args(settings), "refresh")
    assert code == 0 and "PUBLISHED" in out and "karwan" in out and "blocked" in out and "Rudaw TV" in out
    code, out, _ = run_cli(capsys, *args(settings), "refresh")
    assert code == 0 and "UNCHANGED" in out  # a second run changes nothing
    code, out, _ = run_cli(capsys, *args(settings), "report")
    assert code == 0 and "Sources" in out
    code, out, _ = run_cli(capsys, *args(settings), "refresh", "--json")
    assert json.loads(out)["status"] == "unchanged"
    code, out, _ = run_cli(capsys, *args(settings), "health-check")
    assert code == 0 and json.loads(out)["status"] == "ok"


def test_refresh_command_exit_codes(settings, web, capsys, monkeypatch):
    from kurdiptv.app import App
    from kurdiptv.net import SafeHttp

    web.offline = True

    def make(_settings):
        http = SafeHttp(user_agent="t", resolver=web.resolver, transport=web.transport())
        return App(settings, http=http, canary_urls=(CANARY,))

    monkeypatch.setattr(cli, "App", make)
    code, out, _ = run_cli(capsys, *args(settings), "refresh")
    assert code == 3 and "OFFLINE" in out


def test_bad_config_directory_is_a_clean_error(tmp_path, capsys):
    code, _, err = run_cli(capsys, "--config", str(tmp_path / "missing"), "--data", str(tmp_path), "status")
    assert code == 2 and "configuration error" in err
