from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import FakeWeb  # noqa: E402

from kurdiptv import util  # noqa: E402
from kurdiptv.app import App  # noqa: E402
from kurdiptv.config import load_settings  # noqa: E402
from kurdiptv.net import SafeHttp  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture(autouse=True)
def real_clock():
    util.set_clock(__import__("time").time)
    yield
    util.set_clock(__import__("time").time)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path):
    base = load_settings(config_dir=REPO / "config", data_dir=tmp_path / "data")
    return dataclasses.replace(
        base,
        probe=dataclasses.replace(base.probe, verify_with_ffprobe=False, per_host_min_interval=0.0, concurrency=8),
        logos=dataclasses.replace(base.logos, use_catalog=False),
        http=dataclasses.replace(base.http, respect_robots=True),
        sources=tuple(dataclasses.replace(s, min_interval=0.0) for s in base.sources),
    )


@pytest.fixture
def make_app(settings, web):
    """Factory: an App wired to the fake internet (public IPs for every host, no real canary)."""
    apps: list[App] = []

    def factory(*, settings_override=None, canary_urls=()) -> App:
        s = settings_override or settings
        http = SafeHttp(user_agent=s.effective_user_agent, resolver=web.resolver, transport=web.transport())
        app = App(s, http=http, canary_urls=canary_urls)
        apps.append(app)
        return app

    yield factory
    import asyncio

    for app in apps:
        try:
            asyncio.run(app.aclose())
        except Exception:
            pass
