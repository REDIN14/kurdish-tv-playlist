"""Settings: YAML files in ``config/`` plus ``KURDIPTV_*`` environment overrides."""

from __future__ import annotations

import dataclasses
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .util import atomic_write_text

DEFAULT_USER_AGENT = "KurdishIPTVAggregator/1.0 (personal playlist aggregator; honours robots.txt)"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceSettings:
    host: str = "127.0.0.1"
    port: int = 8787
    public_base_url: str = ""
    refresh_on_startup: bool = True
    refresh_interval_hours: float = 6.0
    health_interval_minutes: float = 15.0
    rate_limit_per_minute: int = 120


@dataclass(frozen=True)
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    contact: str = ""
    connect_timeout: float = 8.0
    read_timeout: float = 20.0
    respect_robots: bool = True
    allow_private_hosts: bool = False
    verify_tls: bool = True


@dataclass(frozen=True)
class ProbeSettings:
    concurrency: int = 10
    per_host_concurrency: int = 2
    per_host_min_interval: float = 0.15
    playlist_timeout: float = 12.0
    segment_timeout: float = 20.0
    max_playlist_bytes: int = 1_000_000
    max_segment_bytes: int = 6_000_000
    verify_with_ffprobe: bool = True
    ffprobe_path: str = ""
    ok_ttl_seconds: int = 900
    backoff_base_seconds: int = 900
    backoff_max_seconds: int = 86_400
    fail_threshold: int = 3
    max_variants_tried: int = 2
    max_fallbacks: int = 3
    same_host_fallbacks: bool = False
    keep_stale_hours: float = 72.0
    stall_seconds: int = 600


@dataclass(frozen=True)
class PublishSettings:
    keep_snapshots: int = 3
    min_channels: int = 5
    min_ratio_of_previous: float = 0.5
    fallback_mode: str = "group"  # group | inline | none
    backup_group: str = "Backup streams"
    playlist_title: str = "Kurdish TV"


@dataclass(frozen=True)
class LogoSettings:
    max_bytes: int = 1_000_000
    min_px: int = 48
    max_px: int = 4096
    output_px: int = 512
    ttl_days: float = 7.0
    use_catalog: bool = True
    catalog_channels_url: str = "https://iptv-org.github.io/api/channels.json"
    catalog_logos_url: str = "https://iptv-org.github.io/api/logos.json"


@dataclass(frozen=True)
class SourceConfig:
    id: str
    adapter: str
    url: str
    enabled: bool = True
    trust: str = "high"  # high: unknown channels are published; low: only recognisably Kurdish ones
    # A community-maintained list (somebody's M3U on GitHub) mixes Kurdish channels with world sports, pay-TV and cartoons and
    # its labels are not reliable. With catalog_only the list can add streams to channels that are in catalog.yaml (or that
    # another source already lists), but it cannot introduce a channel by itself and never counts as corroboration.
    catalog_only: bool = False
    # The source reads the broadcasters' own web pages: a stream it finds there is the channel's original source and is
    # preferred over the same channel's copies on aggregator sites and community lists.
    official: bool = False
    min_interval: float = 1.0
    max_requests: int = 200
    page_ttl_hours: float = 12.0
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    root: Path
    config_dir: Path
    data_dir: Path
    service: ServiceSettings
    http: HttpSettings
    probe: ProbeSettings
    publish: PublishSettings
    logos: LogoSettings
    sources: tuple[SourceConfig, ...]

    # --- paths -------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def published_dir(self) -> Path:
        return self.data_dir / "published"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def logos_dir(self) -> Path:
        return self.data_dir / "logos"

    @property
    def logo_cache_dir(self) -> Path:
        return self.data_dir / "logo_cache"

    @property
    def effective_user_agent(self) -> str:
        ua = self.http.user_agent
        if self.http.contact:
            ua = f"{ua} contact: {self.http.contact}"
        return ua

    def admin_token(self) -> str:
        """Token for the /refresh endpoint. Env wins; otherwise a random one is kept in the data dir."""
        env = os.environ.get("KURDIPTV_ADMIN_TOKEN", "").strip()
        if env:
            return env
        path = self.data_dir / "admin_token.txt"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(24)
        atomic_write_text(path, token + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return token


def _build(cls, data: Mapping[str, Any] | None, where: str):
    data = dict(data or {})
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)} (allowed: {', '.join(sorted(names))})")
    try:
        return cls(**data)
    except TypeError as exc:  # missing required keys
        raise ConfigError(f"invalid {where}: {exc}") from exc


def _validate(settings: Settings) -> None:
    if settings.publish.fallback_mode not in {"group", "inline", "none"}:
        raise ConfigError("publish.fallback_mode must be group, inline or none")
    if settings.probe.fail_threshold < 1:
        raise ConfigError("probe.fail_threshold must be >= 1")
    if settings.probe.concurrency < 1 or settings.probe.per_host_concurrency < 1:
        raise ConfigError("probe concurrency values must be >= 1")
    if not 0 < settings.publish.min_ratio_of_previous <= 1:
        raise ConfigError("publish.min_ratio_of_previous must be in (0, 1]")
    seen: set[str] = set()
    for src in settings.sources:
        if src.id in seen:
            raise ConfigError(f"duplicate source id: {src.id}")
        seen.add(src.id)
        if src.trust not in {"high", "low"}:
            raise ConfigError(f"source {src.id}: trust must be high or low")
        for flag in ("catalog_only", "official"):
            if not isinstance(getattr(src, flag), bool):
                raise ConfigError(f"source {src.id}: {flag} must be true or false")
        if not src.url:
            raise ConfigError(f"source {src.id}: url is required")


def find_config_dir(explicit: str | os.PathLike | None = None) -> Path:
    """An explicitly requested directory (argument or KURDIPTV_CONFIG_DIR) is used as given or the run fails -
    never silently replaced by a different configuration."""
    requested = explicit or os.environ.get("KURDIPTV_CONFIG_DIR")
    if requested:
        path = Path(requested)
        if not (path / "settings.yaml").is_file():
            raise ConfigError(f"settings.yaml not found in {path}")
        return path.resolve()
    for cand in (Path.cwd() / "config", Path(__file__).resolve().parents[2] / "config"):
        if (cand / "settings.yaml").is_file():
            return cand.resolve()
    raise ConfigError("config/settings.yaml not found (run from the project root or set KURDIPTV_CONFIG_DIR)")


def load_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")
    return data


def load_settings(
    config_dir: str | os.PathLike | None = None,
    data_dir: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if env is None else env
    cfg_dir = find_config_dir(config_dir)
    raw = load_yaml(cfg_dir / "settings.yaml")

    known = {"service", "http", "probe", "publish", "logos", "sources"}
    extra = sorted(set(raw) - known)
    if extra:
        raise ConfigError(f"unknown top-level key(s) in settings.yaml: {', '.join(extra)}")

    service = _build(ServiceSettings, raw.get("service"), "service")
    http = _build(HttpSettings, raw.get("http"), "http")
    probe = _build(ProbeSettings, raw.get("probe"), "probe")
    publish = _build(PublishSettings, raw.get("publish"), "publish")
    logos = _build(LogoSettings, raw.get("logos"), "logos")

    overrides: dict[str, dict[str, Any]] = {"service": {}, "http": {}}
    if env.get("KURDIPTV_HOST"):
        overrides["service"]["host"] = env["KURDIPTV_HOST"]
    if env.get("KURDIPTV_PORT"):
        overrides["service"]["port"] = int(env["KURDIPTV_PORT"])
    if env.get("KURDIPTV_PUBLIC_URL"):
        overrides["service"]["public_base_url"] = env["KURDIPTV_PUBLIC_URL"].rstrip("/")
    if env.get("KURDIPTV_CONTACT"):
        overrides["http"]["contact"] = env["KURDIPTV_CONTACT"]
    service = dataclasses.replace(service, **overrides["service"])
    http = dataclasses.replace(http, **overrides["http"])
    if service.public_base_url:
        service = dataclasses.replace(service, public_base_url=service.public_base_url.rstrip("/"))

    sources = tuple(_build(SourceConfig, item, f"sources[{i}]") for i, item in enumerate(raw.get("sources") or []))

    root = cfg_dir.parent
    data = Path(data_dir or env.get("KURDIPTV_DATA_DIR") or (root / "data"))
    settings = Settings(
        root=root,
        config_dir=cfg_dir,
        data_dir=data.resolve(),
        service=service,
        http=http,
        probe=probe,
        publish=publish,
        logos=logos,
        sources=sources,
    )
    _validate(settings)
    return settings
