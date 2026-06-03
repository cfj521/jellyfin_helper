# Jellyfin Helper

**English** | [简体中文](README.zh-CN.md)

---

## Three Core Features

### 1. Jellyfin Media Library Assistant

Fills in the gaps where Jellyfin itself is missing or weak:

- **Library browsing**: Multi-library list + detail pages + poster view; optional direct
  local SQLite reads to accelerate `path → item` reverse lookups (measured at 4ms vs.
  1500ms for REST when deployed on the same machine)
- **Metadata repair**: Actor photos (TMDB + Wikidata fallback), posters, NFO;
  normalization of the Jellyfin actor library
- **Full subtitle pipeline**: Multi-source downloading (OpenSubtitles / ASSRT / Shooter) →
  score fusion + tiered language sorting → filenames written to disk per BCP 47 →
  smart filling of missing subtitles → ffprobe detection of embedded subtitle tracks
- **Audio track management**: Batch-set MKV default audio tracks by language preference;
  exception protection for Chinese / unrecognized tracks
- **Maintenance tools**: Web-based config editing (auto-backup on save), Sample cleanup, forced rescan, log viewing, statistics
- **Adult content (optional)**: Code recognition, multi-source scraping from JavBus / JavDB / AVBase / MissAV,
  actress profile library (javdb + Minnano-AV chain), health checks + cooldown protection

### 2. Resource Discovery and Search (Multiple Types)

Aggregates multiple discovery sources into a unified UI, so you don't have to bounce between a dozen sites:

- **Discovery / recommendations**: Aggregated lists from TMDB / Trakt / AniList / Douban, infinite scroll + prefetch
- **Rating aggregation**: TMDB + MDBList (IMDB / RT / Metacritic / Trakt / Letterboxd)
  + Douban, stored uniformly in `media_ratings` with an independent TTL per provider; fused display on the frontend
- **Resource search**: Jackett cross-indexer aggregation, results categorized + sorted by size / Seeders,
  one-click push to the download pipeline

### 3. Download Pipeline

Fully automated from "add torrent" to "import into library" — no manual mv / renaming required:

- **Add torrent**: Jackett search → push to qBittorrent (**requires qB 5.2+**, enabling
  API Key authentication recommended)
- **Recognition**: confidence-driven chain (regex → TMDB → LLM fallback); low confidence drops into
  needs_review for manual handling
- **Import**: Organized into the library per a configurable template + duplicate_policy, with automatic Jellyfin refresh notification
- **Seeding and cleanup**: Soft cleanup with dual conditions state=stop + (ratio≥target OR completed N days ago),
  hard cleanup triggered by disk quota thresholds; protects user seeding data
- **Task system**: Background tasks + SSE real-time progress + cancellation + shutdown coordination;
  the frontend task detail page can expand the details of each step

---

> ## ⚠️ Prerequisite Note: A "Magic Internet Connection" Is Required
>
> The project depends heavily on overseas services (TMDB / Trakt / AniList / OpenSubtitles / IMDB / MDBList /
> Wikidata, etc.), and **without a stable circumvention link the user experience will be poor** — most sources will
> time out or be geo-blocked. We strongly recommend setting up a transparent proxy at the router level
> (OpenClash / mihomo, etc.) to split the entire jellyfin-helper host's outbound traffic by domain. The
> project's code layer **does not handle proxy logic**; it assumes by default that the network is reachable, so
> connection-failure errors are treated as upstream failures rather than proxy configuration issues.

> The project is still under intensive iteration. On schema changes it usually **drops the tables and rescans**
> rather than writing migration scripts. If deploying to production, assess this yourself and back up your data.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 / FastAPI / SQLAlchemy / uvicorn |
| Frontend | Vue 3 (Composition API) / Element Plus / Vite |
| Database | PostgreSQL 12+ (business database) |
| Task push | SSE (no WebSocket) |
| Anti-scraping | curl-cffi (TLS impersonation to bypass Cloudflare's weak anti-bot) |
| LLM | Any OpenAI-compatible endpoint (DeepSeek / Alibaba Tongyi / OpenAI itself) |

---

## Project Structure

```
jellyfin-helper/
├── config.yaml.example            # Config template (copy to config.yaml and fill in real values; the latter is not committed to git)
├── requirements.txt               # Python dependencies
├── VERSION                        # Version number (single source shared by frontend and backend)
│
├── Dockerfile                     # Multi-stage build: node compiles the frontend + python:3.12-slim runtime
├── docker-compose.yml             # 5-service orchestration (helper + Jellyfin + Jackett + qB + Postgres)
├── .env.example                   # compose environment variable template
├── scripts/
│   ├── bootstrap_stack.py         #   One-stop initialization: qb/Jackett passwords + API Key + indexer + RSS + Jellyfin wizard
│   └── docker-entrypoint.sh       #   Container entrypoint: chown then drop privileges with gosu
│
├── backend/                       # FastAPI backend
│   ├── main.py                    #   Application entry + lifespan + SPA static hosting
│   ├── run.py                     #   uvicorn launcher
│   ├── config.py / config_models.py  #   pydantic settings
│   ├── database.py                #   SQLAlchemy models + one-time migration
│   ├── auth_middleware.py         #   JWT authentication middleware
│   ├── diagnostics.py             #   Performance / DB pool monitoring / access log filtering
│   ├── api/                       #   Routers sliced by feature (see table below)
│   └── services/                  #   Background services (Jellyfin event listener, etc.)
│
├── common/                        # Third-party service clients
│   ├── jellyfin_client.py         #   jellyfin REST
│   ├── jellyfin_db.py             #   jellyfin direct SQLite reads (path→item reverse-lookup acceleration, optional)
│   ├── tmdb_client.py / trakt_client.py / anilist_client.py
│   ├── mdblist_client.py / douban_client.py / wikidata_client.py
│   ├── jackett_client.py / qbittorrent_client.py / llm_client.py
│   ├── lang_utils.py              #   Subtitle language code normalization (zh / chs / cht / BCP 47)
│   └── label_cleaner.py / rate_limiter.py
│
├── tools/                         # Business modules (imported by the backend)
│   ├── subtitle_manager/          #   Scan / rename / embedded detection
│   ├── subtitle_downloader/       #   Multi-source download + score fusion
│   ├── audio_manager/             #   MKV audio track adjustment
│   ├── actor_fix/                 #   Actor photos
│   ├── adult_manager/             #   Code scraping + actress profiles
│   └── dispatch/                  #   Download-to-import automation pipeline
│       ├── pipeline_worker.py     #     State machine advancement
│       ├── analyzer.py            #     identify confidence-driven
│       ├── organizer.py           #     Template-based copy + duplicate_policy
│       └── ...
│
└── frontend/                      # Vue 3 SPA
    ├── package.json / vite.config.js
    └── src/
        ├── views/                 #   Pages (medialibraries / downloadpipeline / settings, etc.)
        ├── components/            #   Shared components
        ├── composables/ stores/   #   Composables / Pinia store
        └── api/ router/ utils/ styles/
```

> `docs/` (development docs) and `data/` `logs/` (runtime data) are `.gitignore`d by default and not committed.

### Backend API Routes

| Prefix | File | Main responsibility |
|---|---|---|
| `/api/auth` | `auth.py` | Login / JWT issuance / user list |
| `/api/medialibraries` | `medialibraries.py` | Library list / detail / item reverse lookup (direct DB read + REST fallback) |
| `/api/media` | `media.py` | File browsing, duplicate detection, storage analysis |
| `/api/subtitle` | `subtitle.py` | Scan / rename / auto-download / subtitle language detection |
| `/api/metadata` | `metadata.py` | Actor photos, posters, NFO repair |
| `/api/audio` | `audio.py` | MKV default audio track |
| `/api/adult` | `adult.py` | Code scraping, actress profiles |
| `/api/ratings` | `ratings.py` | MDBList + Douban rating aggregation |
| `/api/discover` | `discover.py` | TMDB / Trakt / AniList / Douban lists |
| `/api/resourcesearch` | `resourcesearch.py` | Jackett aggregated search |
| `/api/downloadpipeline` | `downloadpipeline.py` | qB status monitoring + torrent push entry |
| `/api/dispatch` | `dispatch.py` | Pipeline needs_review / quota / dispatch_map |
| `/api/maintenance` | `maintenance.py` | Sample cleanup, forced rescan, auto-repair orchestration |
| `/api/tasks` | `tasks.py` | Task list / detail / cancel / SSE push |
| `/api/stats` | `stats.py` | Overview statistics |
| `/api/config` | `config_api.py` | Read/write config.yaml + auto-backup |
| `/api/logs` | `logs.py` | Backend log viewing |
| `/api/img_proxy` | `img_proxy.py` | Third-party image proxy (bypass cross-origin + CDN) |
| `/api/diagnostics` | `diagnostics.py` | Availability checks (local tools + various network services) / performance monitoring |

---

## Quick Start (Docker One-Stop · Recommended)

The entire stack is packaged into 5 services: jellyfin-helper + Jellyfin + Jackett + qBittorrent
5.2+ + PostgreSQL 16. The `bootstrap` one-time script automatically sets up the qBittorrent /
Jackett passwords, API Keys, indexers, and RSS switches, and writes them back into `config.yaml`.

```bash
git clone <this-repo> && cd jellyfin-helper

# 1) Required .env: MEDIA_DIR / DOWNLOADS_DIR (POSTGRES_PASSWORD defaults to jellyfin_helper, no need to change)
cp .env.example .env && $EDITOR .env

# 2) Required config.yaml: JWT secret_key + the third-party API Keys you applied for yourself
#    (TMDB required; OpenSubtitles / ASSRT / MDBList, etc. as needed).
#    See the "External Services → Credential Acquisition Cheat Sheet" below for the full credential list and application URLs.
#    The database / jellyfin / jackett / qbittorrent / auth.users[0] sections
#    will be written automatically by bootstrap in the next step (including the helper default password jellyfin_helper).
cp config.yaml.example config.yaml && $EDITOR config.yaml

# 3) bootstrap phase prep: pre-fill qb/jackett config + write back config.yaml
#    The bootstrap / helper containers start as root; the entrypoint automatically chowns
#    ./data/* and ./logs to PUID:PGID, so **no manual mkdir / chown is needed**
docker compose --profile bootstrap run --rm bootstrap-prep

# 4) Start the 5 services
docker compose up -d

# 5) bootstrap phase connect: connect to Jackett and add 7 indexers + run the Jellyfin
#    Setup Wizard + request an API Key and write it back to config.yaml
#    (52BT / dmhy / OneJAV / ThePirateBay / TheRARBG / TorrentKitty / YTS)
docker compose --profile bootstrap run --rm bootstrap-connect

# 6) Make helper re-read config.yaml
docker compose restart helper
```

Done. Open `http://<host-IP>:8099` in your browser and log in with the account from `config.yaml`.

### Web UI Login Credentials

Bootstrap uses the "pre-filled conf file" approach to write qb / Jackett passwords and API Keys
before the containers start (without going through the WebUI login interaction), so helper does not need
to log in first to obtain the API Keys. However, **the qb WebUI still requires a password login** (when browsing
torrents or changing settings) — use the set below:

| Service | URL | Account | Password | Notes |
|---|---|---|---|---|
| **jellyfin-helper** | `http://<host-IP>:8099` | `admin` | `jellyfin_helper` | Main entry; change password: edit `config.yaml.auth.users` then restart helper |
| **qBittorrent** | `http://<host-IP>:8080` | `admin` | `jellyfin_helper` | Change password: WebUI → Options → Web UI → Authentication |
| **Jackett** | `http://<host-IP>:9117` | `admin` | `jellyfin_helper` | Change password: WebUI → Configuration → Admin password |
| **Jellyfin** | `http://<host-IP>:8096` | `admin` | `jellyfin_helper` | bootstrap runs the Wizard automatically; the API Key is already written back to config.yaml; the first thing to do in the UI is add a media library pointing to `/media` |
| **PostgreSQL** | `postgres:5432` (in-stack only) | `jellyfin_helper` | `jellyfin_helper` | 5432 is not exposed to the host, in-stack access only |

> **The qBittorrent API Key must be generated manually once** (the qB 5.2 key is an internally generated `qbt_`
> string that bootstrap cannot pre-fill): go to the qb WebUI (`admin`/`jellyfin_helper`) → Options → WebUI →
> "API key" → Generate, write the key starting with `qbt_` into `config.yaml.qbittorrent.api_key`,
> then `docker compose restart helper`. Before it's filled in, qb-related features are unavailable but helper still starts normally.

### First-Time Verification

Open **Frontend → Config → Availability Check** (the first item in the left navigation). See everything at a glance:

- **Local environment**: FFmpeg · FFprobe · MKVPropEdit · bsdtar (all pre-installed in the container)
- **Network services**: Jellyfin · qBittorrent · Jackett · TMDB · Douban · MDBList ·
  Trakt · AniList · Wikidata · LLM · subtitle sources · adult scraping sites

Each item shows `status / info / elapsed time`. **When you hit a problem, check here first** — it can save half the troubleshooting time.

---

## Bare Metal / Development Mode

If you don't want to use Docker and want to run directly on the host (development or customization scenarios), you'll need to provide:

- Python 3.12 (a conda isolated environment is recommended) + Node 20+
- PostgreSQL 12+ (create the database and user first)
- A Jellyfin instance (10.9+ recommended, requires an admin API Key)
- qBittorrent **5.2+** (using API Key authentication, see "External Services" below)
- Jackett
- System tools: `ffmpeg / mkvtoolnix / bsdtar (libarchive ≥ 3.6 required for RAR5 support)`
- A stable proxy / transparent circumvention link (explained in the prerequisite note)

Startup:

```bash
pip install -r requirements.txt
python -m backend.run            # Backend (default 8000, override with BACKEND_PORT)

cd frontend && npm install && npm run dev   # Frontend (default 5173)
```

---

## Database

Tables are created automatically on first startup; no manual table creation is required.

| Table | Description |
|---|---|
| `users` | User accounts (JWT authentication) |
| `tasks` | Background task records |
| `scan_reports` | Scan report archive |
| `actors` | Actor information cache |
| `media_items` | Media file metadata |
| `media_metadata` | Extended media metadata (posters, synopses, etc.) |
| `media_ratings` | Rating aggregation (TMDB / IMDB / RT / Metacritic / Trakt / Letterboxd / Douban), unique by `(tmdb_id, media_type)`; independent `*_fetched_at` per provider |
| `video_annotations` | Video annotations (hardsub markers, etc.) |
| `adult_items` | Adult content metadata (optional) |
| `adult_actresses` | Actress profile library (adult content, optional) |
| `download_dispatch_map` | Download-to-import mapping (torrent → target path) |
| `kv_cache` | General-purpose KV cache |
| `llm_classify_cache` | LLM classification result cache |

---

## External Services

### Credential Acquisition Cheat Sheet

The Keys / Tokens below need to be applied for from the corresponding sites and filled into `config.yaml`.
Under **Docker deployment**, the three Keys for qBittorrent / Jackett / Postgres are automatically generated +
written back by bootstrap, so **no manual application is needed**.

| Service | Requirement | Application URL | Field |
|---|---|---|---|
| **Jellyfin API Key** | Required | Docker: bootstrap runs the Wizard + requests it automatically; bare metal: Jellyfin Web → Dashboard → API Keys → New | `jellyfin.api_key` |
| **TMDB API Key** | Required | https://www.themoviedb.org/settings/api (free registration and application) | `tmdb.api_key` |
| **PostgreSQL password** | Auto for Docker | Docker default `jellyfin_helper` (used in-stack, not exposed); bare metal: set your own | `database.password` |
| **qBittorrent API Key** | Required (mandatory for 5.2+, must be generated manually once) | qB WebUI (`admin`/`jellyfin_helper`) → Options → WebUI → API key → Generate | `qbittorrent.api_key` (**no** username/password needed) |
| **Jackett API Key** | Required | Docker: automatic via bootstrap; bare metal: shown directly in the top-right of the Jackett UI | `jackett.api_key` |
| **MDBList API Key** | Recommended (ratings) | https://mdblist.com/api generate after logging in (free, 1000 req/day) | `mdblist.api_key` |
| **OpenSubtitles trio** | Recommended (subtitles) | API Consumer → https://www.opensubtitles.com/consumers + register an account | `subtitle.opensubtitles_api_key` + `opensubtitles_username` + `opensubtitles_password` |
| **ASSRT API Token** | Recommended (primary for Chinese subtitles) | after registration → https://secure.assrt.net/usercp.php | `subtitle.assrt_api_token` |
| **Trakt Client ID** | Optional (recommendation source) | https://trakt.tv/oauth/applications create an app | `trakt.client_id` |
| **LLM API Key** | Optional (recognition fallback) | Any OpenAI-compatible service (DeepSeek / Alibaba Tongyi / OpenAI) | `llm.api_key` + `llm.base_url` |
| **JWT secret_key** | Required | Generate it yourself: `python -c "import secrets; print(secrets.token_urlsafe(32))"` | `auth.secret_key` |
| **Admin account password** | Auto for Docker | Docker default `admin` / `jellyfin_helper` (written by bootstrap); bare metal: set your own | `auth.users[].password` |

**Sources that require no Key** (work out of the box, no application needed):

- Shooter subtitles (hash matching)
- AniList (public GraphQL endpoint, anime metadata)
- Douban (web scraping, with a 5-failure circuit breaker)
- Wikidata (actor image fallback, but you must fill your contact info into `wikidata.user_agent`, as required by Wikimedia)

**Adult content** (enable as needed): JavBus / JavDB / AVBase / MissAV, no API Key but with geo-blocking and anti-scraping.

Each field in `config.yaml.example` also has the corresponding application URL pasted next to it; just fill in section by section.

### Why qBittorrent 5.2+ Is Required

Compatibility with older versions (< 5.0) was removed in 2026-06, **for security reasons**:

- 4.x / 5.0 / 5.1 all lack API Key authentication and only support username/password
- qB's default admin/adminadmin weak credentials are mass-scanned across the public internet by automated scripts, leading to **direct RCE that installs miners**
- Real incidents: backdoors planted via `Preferences → Downloads → Run external program on torrent added / completed`
  with `wget ... | sh`, a common technique for XMR mining trojans

Only 5.2.0+ introduced the stateless API Key mechanism of `Authorization: Bearer qbt_xxx`;
when this project is deployed via Docker, the bootstrap script automatically generates the API Key and writes it into `config.yaml`,
and for bare-metal deployment you must also manually Generate one in the qB UI and fill it in.

### ⚠️ qB Security Checklist

Do this regardless of Docker or bare metal:

1. **Don't expose the WebUI to the public internet** (Docker maps to the host only by default, combine with a host firewall;
   for bare metal, change the listen address to `127.0.0.1` or use a reverse proxy with an IP allowlist)
2. **Before first taking over an existing qB**, check whether `Preferences → Downloads → Run external program
   on torrent added / completed` has suspicious commands planted in it — this is a common trace of historical weak-credential compromise

---

## FAQ

> **Go to the Config page → Availability Check first** (the first item in the left navigation). The local environment runs automatically and network services can be tested with one click; most problems' root causes are visible there.

### Backend fails to start

Look directly at the startup logs (if the DB can't connect, you can't even reach the config page):

```bash
docker compose logs -f helper           # Docker deployment
# or bare metal: the output of python -m backend.run
```

Common root causes:

1. The `database` section of `config.yaml` is wrong — for Docker deployment `host: postgres`,
   for bare-metal deployment `host: 127.0.0.1`
2. Docker: bootstrap wasn't run, or `POSTGRES_PASSWORD` doesn't match `config.yaml`
3. Bare metal: `requirements.txt` not fully installed / system-level dependencies (ffmpeg / mkvtoolnix /
   bsdtar) are not on PATH

### Frontend can't connect to the backend (bare-metal mode only)

1. Confirm the backend is running on the port configured in `config.yaml`
2. Check the proxy configuration in `vite.config.js`
3. Check `cors_origins` (default `["*"]`)

Under Docker deployment the frontend is static assets hosted directly by FastAPI, so there's no cross-process issue.

### Third-party sources can't fetch data

Open **Availability Check** → find the corresponding source (TMDB / Douban / Jellyfin / qB / Jackett ...) and click "Test". The result will show the specific HTTP status code or exception type:

- `HTTP 401 / 403`: api_key / credential error
- `HTTP 429`: rate-limited; rate_limiter pauses automatically (check the remaining quota in the QuotaStatusPanel on the task detail page)
- `Connection*` exception: network / proxy issue
- `not_configured`: key not yet filled in or enabled=false

### Database connection error

Docker deployment:

```bash
docker compose exec postgres psql -U jellyfin_helper -d jellyfin_helper -c "\dt"
```

Bare metal:

```bash
psql -h <host> -p 5432 -U jellyfin_helper -d jellyfin_helper
```

When it can't connect, check in order: container name/network reachability (Docker), firewall (bare metal), user password, whether the database exists.

### Torrent add fails

The frontend ElMessage now gives specific reasons, distinguished by status code:

- **HTTP 409**: the torrent is already in the qB queue (the pre-check intercepts it first and shows the torrent name / status)
- **HTTP 415**: the torrent file is invalid (not bencode format)
- **HTTP 502 + "qBittorrent refused to add torrent"**: qB's default download directory doesn't exist / has no permission, or the category hasn't been created in qB

---

## License

MIT
