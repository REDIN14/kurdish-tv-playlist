# Kurdish TV playlist

One standard M3U link for Kurdish TV channels. The service collects streams from public aggregator
pages, **checks that each one really plays**, merges duplicates into a single channel with ordered
backups, caches logos, and keeps the list fresh on its own. Nothing is relayed through this program:
the playlist only contains links to streams that other people operate.

```
http://<host>:8787/playlist.m3u        <- the address you give to a player
```

## Use it

| Player | How |
|---|---|
| VLC | Media → Open Network Stream → paste the address → View → Playlist |
| TiviMate | Add playlist → M3U playlist → paste the address |
| Kodi | PVR IPTV Simple Client → *M3U play list URL* → paste the address |
| OTT Navigator | Add provider → Playlist → paste the address |

Channels are grouped (News, General, Music, Kids…). A group called **Backup streams** holds
alternative links for the same channels; if a main stream stops, try the same name there. The list
refreshes itself — there is nothing to update in the player.

## What happens on every refresh

```
discover → normalise → match / de-duplicate → probe → rank → logos → validate → publish
```

1. **Discover** – each source has a small adapter (`src/kurdiptv/adapters/`); a layout change breaks only that source.
2. **Normalise + match** – names are folded (case, Kurdish/Turkish letters, Arabic-script variants, `HD`/`Live`/`ZINDÎ`, `(Alt 2)`).
   A known station's own stream servers (`catalog.yaml › url_patterns`) outrank page titles, which are often wrong.
   Unsure matches are **not** merged; they are listed as review warnings.
3. **Probe** – each stream is fetched like a player would: playlist → best variant → a real media segment. Resolution,
   frame rate and codecs are *measured* with `ffprobe` when it is installed, never taken from the URL or the playlist's claims.
   Dead, frozen (playlist stops advancing), encrypted, audio-only and non-HLS URLs are rejected.
4. **Rank** – verified resolution → bitrate/fps → latency/failures → source stability. A failing primary is replaced by a healthy
   fallback after 3 consecutive failures; fallbacks must live on a *different server*.
5. **Validate + publish** – the new playlist replaces the old one atomically, and only if it passes checks (never empty,
   never less than half of the previous one, no credentials in any URL). The last 3 snapshots are kept (`kurdiptv rollback`).

A light check every 15 minutes re-probes only published streams and promotes fallbacks; a full refresh runs on
start-up and every 6 hours. If a source site is down, its channels stay (remembered) and the other sources carry on.
If this machine loses its internet connection, a run is abandoned and **nothing** is changed.

## Sources and the rules it follows

| Source | How it is read | Status |
|---|---|---|
| kurdtvs.net | its published M3U (`/iptv.php?format=m3u`) + each channel page's player data | works |
| kurditv.com | the site's WordPress REST API + the `vembed_data` call its own player makes | works |
| semir.site123.me | tile grid → player pages on `wapsite.me`; titles on that page are unreliable, so names come from link targets and must be corroborated | works (low trust) |
| karwan.tv | **behind a Cloudflare bot challenge** | reported as `blocked`; never circumvented. It starts working by itself if the site owner lets this aggregator in. |

Built in: `robots.txt` is honoured for every page fetched; one request per second per source, a request budget per run,
page caching with conditional requests; a descriptive User-Agent; private/loopback addresses are refused (SSRF guard, also
for redirects); stream URLs with embedded credentials or tokens (e.g. Xtream-Codes panel URLs) are never published.

**Scope.** Only Kurdish channels. Commercial pay-TV, sports, films and international networks (MBC, Eurosport, beIN, Setanta,
Al Jazeera, ZDF…) are excluded by `config/policy.yaml`; a stream that announces itself (MPEG-TS service name) as a
foreign network is rejected. The tool cannot judge whether a small channel's stream is *licensed* for redistribution — that
remains the operator's responsibility. To remove a channel at a rights-holder's request, add it to `blocklist` in
`policy.yaml` (id, name, host or URL fragment); it disappears at the next refresh. The scope rules are about rights
(pay-TV, sports, films, foreign networks), not editorial stance: nothing is filtered because of what a Kurdish channel
says. Some channels are politically affiliated and their legal status differs between countries - use the blocklist
for any you do not want to carry.

## Run it

Windows, from this folder (everything is already set up here):

```
scripts\start.cmd                      # run the service in a window
powershell -File scripts\install-autostart.ps1     # start automatically at every login (hidden)
scripts\refresh-now.cmd                # refresh once, print the report
```

Anywhere else: `python -m venv .venv`, `pip install .`, `kurdiptv serve`. Install `ffmpeg` too (it provides `ffprobe`).

```
kurdiptv serve                 # HTTP service + scheduler (refresh on start, every 6 h; health check every 15 min)
kurdiptv refresh [--force]     # one refresh, prints the report   (exit code 0 ok, 2 held, 3 offline, 1 failed)
kurdiptv health-check          # light re-probe + failover
kurdiptv status | report       # what is published / the last report
kurdiptv rollback [--snapshot] # re-publish an earlier snapshot
kurdiptv build-static OUT --base-url https://…   # export playlist + logos as plain files
kurdiptv admin-token           # token for POST /refresh
```

HTTP endpoints: `/playlist.m3u`, `/logos/<id>.png`, `/health` (service + source status), `POST /refresh` and `/report`
(admin token), `/epg.xml` (only exists when real EPG data is present — none of the sources provides any).

## Configuration (all in `config/`, no code changes needed)

* `settings.yaml` – schedule, timeouts, probe limits, sources. Adding another site that offers an M3U is one entry with
  `adapter: m3u`.
* `catalog.yaml` – known channels: stable id, spelling variants, category, language, stream-server patterns, logo.
  Channels *not* listed still appear automatically (flagged "not in catalog" in the report); this file only improves names and grouping.
* `policy.yaml` – scope, blocklist, brand deny-list, overrides (force an alias, prefer a source, pin a URL, fix a logo).

Environment: `KURDIPTV_PUBLIC_URL`, `KURDIPTV_PORT`, `KURDIPTV_HOST`, `KURDIPTV_DATA_DIR`, `KURDIPTV_CONFIG_DIR`,
`KURDIPTV_CONTACT` (appended to the User-Agent so site owners can reach you), `KURDIPTV_ADMIN_TOKEN`.

## Hosting

The playlist is just an HTTP(S) address; where it lives decides who can reach it.

* **This PC** – `http://localhost:8787/playlist.m3u` (by default the service listens on this PC only). For phones/TVs on the
  same network run `powershell -File scripts\install-autostart.ps1 -AllowLan` as administrator: it listens on the network
  and opens the port for *private* networks only; then use `http://<PC address>:8787/playlist.m3u`. Works while the PC is on.
* **Docker** – `docker compose up -d` (image includes `ffprobe`). Put it behind HTTPS (Caddy, nginx, a tunnel) on any small VPS;
  free tiers that sleep or forbid scheduled jobs are not suitable.
* **claude.ai artifacts** – tried, does not work for a playlist: `.m3u`/`.m3u8` are not file types artifacts serve, the files
  of a page only exist inside its sandboxed viewer (a direct file URL returns claude.ai's app shell or a 404), pages are
  private and login-gated by default, and a page cannot run the scheduled refresh. A player cannot fetch anything from there.
* **GitHub Pages** – `.github/workflows/refresh.yml` refreshes every 6 hours on GitHub's runners and publishes
  `playlist.m3u` + logos as a static site: a stable HTTPS URL, no server. Caveats: a static site cannot run the 15-minute
  failover check; streams that block data-centre IP addresses will look dead from there; and GitHub pauses scheduled
  workflows in a repository with no activity for 60 days (re-enable it in the Actions tab).

## Diagnosing

`kurdiptv report` (or `data/published/report.txt`) lists, per refresh: every source's status, new / removed / changed
channels, which URLs were merged, what was excluded and why, probe failures by cause, and review warnings.
`data/snapshots/` holds the last three playlists with their reports. `/health` shows source status at a glance.

## Development

```
pip install -e ".[dev]"
pytest            # ~280 tests: offline, deterministic; real-ffprobe tests run when ffmpeg is installed
```
