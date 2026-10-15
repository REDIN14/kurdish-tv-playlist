"""Application container: wires settings, database, HTTP client, catalogue, policy, prober, logos, publisher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .catalog import Catalog
from .config import Settings
from .db import Database
from .fetcher import RobotsCache
from .logos import LogoManager
from .net import FetchError, SafeHttp, UnsafeURL
from .policy import Policy
from .probe import Prober, find_ffprobe
from .publish import Publisher
from .util import now

log = logging.getLogger(__name__)

CANARY_URLS = (
    "https://www.gstatic.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://www.msftconnecttest.com/connecttest.txt",
)


@dataclass
class AppState:
    started_at: float = field(default_factory=now)
    refreshing: bool = False
    checking: bool = False
    last_refresh: dict | None = None
    last_health: dict | None = None
    next_refresh_at: float | None = None
    next_health_at: float | None = None


class App:
    def __init__(
        self,
        settings: Settings,
        *,
        http: SafeHttp | None = None,
        canary_urls: tuple[str, ...] | None = CANARY_URLS,
        catalog: Catalog | None = None,
        policy: Policy | None = None,
    ):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.db_path)
        self.http = http or SafeHttp(
            user_agent=settings.effective_user_agent,
            connect_timeout=settings.http.connect_timeout,
            read_timeout=settings.http.read_timeout,
            allow_private=settings.http.allow_private_hosts,
            verify_tls=settings.http.verify_tls,
        )
        self.catalog = catalog or Catalog.load(settings.config_dir / "catalog.yaml")
        self.policy = policy or Policy.load(settings.config_dir / "policy.yaml")
        self.robots = RobotsCache(self.http, self.db)
        ffprobe = find_ffprobe(settings.probe.ffprobe_path) if settings.probe.verify_with_ffprobe else None
        self.ffprobe = ffprobe
        self.prober = Prober(self.http, settings.probe, ffprobe=ffprobe)
        self.logos = LogoManager(settings, self.http, self.db, self.robots)
        self.publisher = Publisher(settings)
        self.lock = asyncio.Lock()
        self.state = AppState()
        self.canary_urls = canary_urls
        if ffprobe is None and settings.probe.verify_with_ffprobe:
            log.warning("ffprobe not found: stream resolution will be the playlist's claim, not a measurement")

    async def online(self) -> bool:
        """True when the public internet is reachable (so a wave of failed probes means the streams, not us)."""
        if not self.canary_urls:
            return True
        for url in self.canary_urls:
            try:
                res = await self.http.get(url, max_bytes=4096, timeout=6, total_timeout=10)
                if res.status < 500:
                    return True
            except (FetchError, UnsafeURL):
                continue
        return False

    async def aclose(self) -> None:
        try:
            await self.http.aclose()
        finally:
            self.db.close()
