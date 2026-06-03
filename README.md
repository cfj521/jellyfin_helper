# Jellyfin Helper

**English** | [简体中文](README.zh-CN.md)

---

## Three Core Features

### 1. Jellyfin Media Library Assistance

Fills the gaps where Jellyfin itself is missing or weak:

- **Library browsing**: Multi-library list + detail pages + poster view; optional direct
  local SQLite reads to accelerate `path → item` reverse lookups (measured 4ms vs REST
  1500ms on same-host deployments)
- **Metadata repair**: Actor photos (TMDB + Wikidata fallback), posters, NFO;
  Jellyfin actor library normalization
- **Full subtitle pipeline**: Multi-source download (OpenSubtitles / ASSRT / Shooter) →
  score fusion + tiered language sorting → on-disk naming per BCP 47 → smart fill-in for
  missing subtitles → embedded subtitle track detection via ffprobe
- **Audio track management**: Batch-set MKV default audio tracks by language preference;
  exception protection for Chinese / unrecognized tracks
- **Maintenance tools**: Web config editing (auto-backup on save), Sample cleanup, forced
  rescan, log viewing, statistics
- **Adult content (optional)**: Code recognition, multi-source scraping
  (JavBus / JavDB / AVBase / MissAV), actress profile library (javdb + Minnano-AV chain),
  health score + cooldown protection

### 2. Resource Discovery and Search (Multiple Sources)

Aggregates multiple discovery sources into a unified UI, sparing you from jumping back and
forth between a dozen sites:

- **Discovery / recommendations**: TMDB / Trakt / AniList / Douban list aggregation,
  infinite scroll + prefetch
- **Rating aggregation**: TMDB + MDBList (IMDB / RT / Metacritic / Trakt / Letterboxd)
  + Douban, stored uniformly in `media_ratings` with an independent TTL per provider;
  fused display on the frontend
- **Resource search**: Jackett cross-indexer aggregation, results categorized + sorted by
  size / seeders, one-click push to the download pipeline

### 3. Download Pipeline

Fully automated from "add torrent" to "import into library", with no manual mv / rename
required:

- **Add torrent**: Jackett search → push to qBittorrent (**requires qB 5.2+**, API Key
  authentication recommended)
- **Identification**: confidence-driven chain (regex → TMDB → LLM fallback), low-confidence
  results drop into needs_review for manual handling
- **Import**: Organized into the media library per a configurable template +
  duplicate_policy, automatically notifying Jellyfin to refresh
- **Seeding and cleanup**: state=stop + (ratio≥target OR completed N days ago) dual-condition
  soft cleanup, disk quota threshold triggers hard cleanup; protects user seeding data
- **Task system**: Background tasks + SSE real-time progress + cancellation + shutdown
  coordination; the frontend task detail page expands per-step details

---

> ## ⚠️ Prerequisite Reminder: A VPN / Proxy Is Required
>
> The project depends heavily on overseas services (TMDB / Trakt / AniList / OpenSubtitles /
> IMDB / MDBList / Wikidata, etc.). **Without a stable proxy link the user experience will be
> poor** — most sources will time out or be geo-blocked. We strongly recommend setting up
> transparent proxying at the router layer (OpenClash / mihomo, etc.) to route the entire
> jellyfin-helper host's outbound traffic by domain. This project's code layer **does not
> handle proxy logic**; it assumes by default that the network is reachable, so any
> connection failures are treated as upstream faults rather than proxy misconfiguration.

> The project is still under heavy iteration. When the schema changes, we usually **just drop
> the tables and rescan** instead of writing migration scripts. For production deployment,
> evaluate this yourself and make proper data backups.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 / FastAPI / SQLAlchemy / uvicorn |
| Frontend | Vue 3 (Composition API) / Element Plus / Vite |
| Database | PostgreSQL 12+ (business database) |
| Task push | SSE (not WebSocket) |
| Anti-scraping | curl-cffi (TLS impersonation to get past Cloudflare's lighter defenses) |
| LLM | Any OpenAI-compatible endpoint (DeepSeek / Alibaba Tongyi / OpenAI itself) |

---

## Project Structure

```
jellyfin-helper/
├── config.yaml.example            # Config template (copy to config.yaml and fill in real values; the latter is gitignored)
├── requirements.txt               # Python dependencies
├── VERSION                        # Version number (single source shared by frontend and backend)
│
├── backend/                       # FastAPI backend
│   ├── main.py                    #   App entry + lifespan + SPA static hosting
│   ├── run.py                     #   uvicorn launcher
│   ├── config.py / config_models.py  #   pydantic settings
│   ├── database.py                #   SQLAlchemy models + one-off migrations
│   ├── auth_middleware.py         #   JWT auth middleware
│   ├── diagnostics.py             #   Performance / DB pool monitoring / access log filtering
│   ├── api/                       #   Feature-sliced routers (see table below)
│   └── services/                  #   Background services (jellyfin event listener, etc.)
│
├── common/                        # Third-party service clients
│   ├── jellyfin_client.py         #   jellyfin REST
│   ├── jellyfin_db.py             #   jellyfin SQLite direct read (path→item reverse-lookup acceleration, optional)
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
│   └── dispatch/                  #   Download-to-library automation pipeline
│       ├── pipeline_worker.py     #     State machine advancement
│       ├── analyzer.py            #     identify confidence-driven
│       ├── organizer.py           #     Template-based copy + duplicate_policy
│       └── ...
│
└── frontend/                      # Vue 3 SPA
    ├── package.json / vite.config.js
    └── src/
        ├── views/                 #   Pages (medialibraries / downloadpipeline / settings, etc.)
        ├── components/            #   Common components
        ├── composables/ stores/   #   Composables / Pinia store
        └── api/ router/ utils/ styles/
```

> `docs/` (development docs) and `data/` `logs/` (runtime data) are `.gitignore`d by default
> and not committed.

### Backend API Routes

| Prefix | File | Main Responsibility |
|---|---|---|
| `/api/auth` | `auth.py` | Login / JWT issuance / user list |
| `/api/medialibraries` | `medialibraries.py` | Library list / details / item reverse lookup (direct DB read + REST fallback) |
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
| `/api/tasks` | `tasks.py` | Task list / details / cancellation / SSE push |
| `/api/stats` | `stats.py` | Overview statistics |
| `/api/config` | `config_api.py` | Read/write config.yaml + auto-backup |
| `/api/logs` | `logs.py` | Backend log viewing |
| `/api/img_proxy` | `img_proxy.py` | Third-party image proxy (bypass CORS + CDN) |
| `/api/diagnostics` | `diagnostics.py` | Availability checks (local tools + network services) / performance monitoring |

---

## Quick Start

### Requirements

- Python 3.12 (recommended to use an isolated conda environment)
- Node.js 20+
- PostgreSQL 12+ (create the database and user first)
- A running Jellyfin (10.9+ recommended) + admin API Key
- qBittorrent **5.2+ required** (use API Key authentication instead of the weak admin/admin
  password; older versions have no API Key support and carry a real risk of being injected
  with mining malware — see "External Services → qBittorrent" below)
- System-level tools (see the "System-Level Dependencies" section below)
- **A stable proxy / transparent VPN link** (explained in the prerequisite reminder at the top)

### 1. Configuration

```bash
cp config.yaml.example config.yaml
```

At minimum, fill in these fields (everything else is optional):

```yaml
database:
  host: "127.0.0.1"
  name: "jellyfin_helper"
  user: "jellyfin_helper"
  password: "your_password"

jellyfin:
  host: "http://your-jellyfin:8096"
  api_key: "your_jellyfin_admin_api_key"
  # Optional: direct Jellyfin DB read to accelerate path→item reverse lookups (fill in when on the same host or SMB-mounted)
  # db_path: "/var/lib/jellyfin/data/jellyfin.db"

tmdb:
  api_key: "your_tmdb_api_key"
```

See [config.yaml.example](config.yaml.example) for the full set of fields; you can also run
it empty first and then edit in the frontend at `/settings` (auto-backup on save).

### 2. Backend

```bash
pip install -r requirements.txt
python -m backend.run
```

Port priority: environment variable `BACKEND_PORT` > `config.yaml: server.backend_port` >
default 8000.

Development hot reload:

```bash
# bash
BACKEND_RELOAD=1 python -m backend.run

# PowerShell
$env:BACKEND_RELOAD='1'; python -m backend.run
```

### 3. Frontend

Open a new terminal:

```bash
cd frontend
npm install
npm run dev
```

vite automatically reads `server.frontend_port` from `config.yaml`.

### 4. Access

- Frontend: http://localhost:5173
- API docs (Swagger): http://localhost:8000/docs

### 5. First-Launch Verification ★

Open **Frontend → Settings → Availability Check** (the first item in the left navigation,
right there when you enter the page). See everything at a glance on one screen:

- **Local environment** (runs on entry, zero network cost): FFmpeg · FFprobe · MKVPropEdit · bsdtar
- **Network services** (manual button): Jellyfin · qBittorrent · Jackett · TMDB · Douban ·
  MDBList · Trakt · AniList · Wikidata · LLM · subtitle sources · adult scraping sites

Each item shows `status / info / elapsed time`. Disabled sources are greyed out with their
buttons disabled. **When you hit a problem, check here first — it can save you half the
troubleshooting time.**

---

## System-Level Dependencies

Beyond Python packages (`requirements.txt`), some features depend on the following system
tools. **They are not required** — when missing, the corresponding feature automatically
degrades (scan/suggest only, no writes) without crashing.

| Tool | Purpose | Impact When Missing |
|---|---|---|
| **ffmpeg / ffprobe** | Audio track scanning, embedded subtitle track detection | Cannot detect embedded subtitles or audio track info |
| **mkvtoolnix (mkvpropedit)** | Modify the default audio track flag of MKV files | Audio management only returns suggestions, does not actually write |
| **bsdtar (libarchive ≥ 3.6)** | Extract rar / 7z subtitle packs | rar subtitle packs cannot be extracted; zip is unaffected |

> **bsdtar's libarchive version must be ≥ 3.6 to support RAR5** (modern subtitle packs are
> basically all RAR5). Reference: Ubuntu 22.04+ / Debian 12+ / macOS Homebrew / conda-forge
> all satisfy this. Ubuntu 20.04 / Debian 11 ship libarchive 3.4, which fails to extract
> RAR5 — upgrade your distribution, or install from conda-forge.

Installation:

```bash
# Debian / Ubuntu 22.04+ / Debian 12+
sudo apt install ffmpeg mkvtoolnix libarchive-tools

# macOS (bsdtar is built in, no extra install needed)
brew install ffmpeg mkvtoolnix

# Windows (Chocolatey)
choco install ffmpeg mkvtoolnix
# Install bsdtar via conda: see below

# Conda (cross-platform one-liner, libarchive 3.7+ ships bsdtar with RAR5 support)
conda install -c conda-forge ffmpeg mkvtoolnix libarchive
```

Verification: running `ffprobe -version` / `mkvpropedit --version` / `bsdtar --version` on
the command line and seeing a version number printed is enough. You can also open
**Frontend → Settings → Availability Check**, where the local environment column shows at a
glance which tools are on PATH.

---

## Filesystem Permissions

The user the backend process runs as (systemd `User=` / Docker `user:` / the shell user when
running bare) must have the following access. **This is the most common root cause of "it
looks like it's running but nothing happens"**:

| Path | Permission Needed | Purpose | Symptom When Misconfigured |
|---|---|---|---|
| Jellyfin SQLite (e.g. `/var/lib/jellyfin/data/jellyfin.db`) | **read** | `path → item` reverse lookup direct-read acceleration (optional, 4ms vs REST 1500ms) | `PermissionError` in startup log; runtime falls back to REST, 100× slower |
| qB download directory (e.g. `/download`) | **read + write** | mv/cp source files on import; soft/hard cleanup `rm` of completed torrents; disk quota monitoring | Pipeline cannot move files; log shows `配额: 后端 stat 不到 '/download'，禁用配额监视/清理` |
| Jellyfin media library directory (e.g. `/library/videos`) | **read + write** | dispatch writes files on import + lays down NFO/poster; adult_scanner detects local attachments | Import fails; NFO / poster not written; scan result "sees the file but can't identify it" |

**Typical systemd deployment** (recommended): add the backend user to the `jellyfin` group to
inherit read access via Jellyfin DB's default 640 permissions; use group ownership + 775 for
library directories:

```ini
# /etc/systemd/system/jellyfin-helper.service.d/user.conf
[Service]
User=jellyfin_helper
Group=jellyfin
UMask=0002
```

```bash
# Add the backend user to the jellyfin group (if library directories are owned by the jellyfin user)
sudo usermod -aG jellyfin jellyfin_helper

# Verify: read the jellyfin DB / write to the library directory as the backend user
sudo -u jellyfin_helper sqlite3 /var/lib/jellyfin/data/jellyfin.db ".tables" | head
sudo -u jellyfin_helper touch /library/videos/_perm_test && rm /library/videos/_perm_test
```

**Docker deployment**: align `user: "1000:1000"` in `docker-compose.yml` with the owner/group
of the mounted host directories; or `chown` in the entrypoint so the data volume ownership
follows along.

**Bare run (during development)**: for convenience you can use
`sudo -u jellyfin python -m backend.run` to skip permission coordination; sharing a shell
user is not recommended in production.

---

## Database

Tables are created automatically on first launch; no manual table creation needed.

| Table | Description |
|---|---|
| `users` | User accounts (JWT authentication) |
| `tasks` | Background task records |
| `scan_reports` | Scan report archive |
| `actors` | Actor info cache |
| `media_items` | Media file metadata |
| `media_metadata` | Media extended metadata (posters, synopses, etc.) |
| `media_ratings` | Rating aggregation (TMDB / IMDB / RT / Metacritic / Trakt / Letterboxd / Douban), unique by `(tmdb_id, media_type)`; independent `*_fetched_at` per provider |
| `video_annotations` | Video annotations (hard-subtitle markers, etc.) |
| `adult_items` | Adult content metadata (optional) |
| `adult_actresses` | Actress profile library (adult content, optional) |
| `download_dispatch_map` | Download-to-library mapping (torrent → target path) |
| `kv_cache` | General KV cache |
| `llm_classify_cache` | LLM classification result cache |

---

## External Services

Summary:

- **Required**: Jellyfin, TMDB, PostgreSQL
- **Strongly recommended**: Jackett + qBittorrent (enables the download-to-library pipeline)
- **Subtitles**: OpenSubtitles + ASSRT — either one will do, both is best
- **Ratings**: MDBList (optional) + Douban (no key needed)
- **LLM**: Any OpenAI-compatible service (identification fallback, optional)
- **Adult content**: JavBus / JavDB / AVBase / MissAV (with geo-blocking and proxy advice)

### qBittorrent Version Requirement: **5.2+ Required**

Compatibility with older versions (< 5.0) was removed in 2026-06, **for security reasons**:

- 4.x / 5.0 / 5.1 have no API Key authentication, only username/password
- qB's default weak admin/adminadmin password gets scanned across the public internet by
  automated scripts, leading **directly to RCE and miner installation**
- A real incident: a `wget ... | sh` backdoor was injected via
  `Preferences → Downloads → Run external program on torrent added / completed`, a common
  technique for XMR mining malware

It wasn't until 5.2.0+ that the stateless API Key mechanism
(`Authorization: Bearer qbt_xxx`) was introduced; this project requires that minimum version
to guarantee authentication strength.

### Configuring the API Key

Go to qB **Preferences → WebUI → API Key section**, click Generate, and copy the `qbt_xxx...`
into `config.yaml`:

```yaml
qbittorrent:
  host: "http://127.0.0.1:8080"
  api_key: "qbt_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # Strongly recommended: stateless, no plaintext password sent
  # Fallback (still requires qB 5.2+ for the rest of the API compatibility):
  username: ""
  password: ""
```

Or paste it in the frontend at **Settings → qBittorrent Download Management**. Once api_key is
set, you **don't need to fill in username/password**.

### ⚠️ qB Security Checklist

Whether or not you enable the API Key, we recommend:

1. **Change the listen address to `127.0.0.1`** (local only), or add an IP allowlist behind a
   reverse proxy
2. **Before taking over an existing qB for the first time**, check whether
   `Preferences → Downloads → Run external program on torrent added / completed` has been
   injected with suspicious commands — this is a common trace left behind when a weak password
   has historically been compromised
3. Do not expose the qB WebUI directly to the public internet

---

## FAQ

> **Go to Settings → Availability Check first** (the first item in the left navigation). The
> local environment runs automatically and network services can be tested with one click; for
> most issues you'll see the root cause right there.

### Backend Fails to Start

1. Check whether PostgreSQL is reachable and whether the database and user have been created
   (if the DB is unreachable the backend process won't even start, and you **can't reach the
   settings page** — look directly at the startup log)
2. Confirm the `database` section in `config.yaml` is filled in correctly
3. Confirm everything in `requirements.txt` is installed
4. Confirm system-level dependencies are installed (the Availability Check "local
   environment" lists each one)

### Frontend Can't Connect to Backend

1. Confirm the backend is started on the port configured in `config.yaml`
2. Check the proxy configuration in `vite.config.js`
3. Check `cors_origins` (default `["*"]`)

### Third-Party Sources Return No Data

Open the **Availability Check** → find the corresponding source
(TMDB / Douban / Jellyfin / qB / Jackett ...) and click "Test". The result shows the specific
HTTP status code or exception type:

- `HTTP 401 / 403`: wrong api_key / credentials
- `HTTP 429`: rate-limited; rate_limiter will automatically pause (see the remaining quota in
  the QuotaStatusPanel on the task detail page)
- `Connection*` exception: network / proxy issue
- `not_configured`: key not yet filled in, or enabled=false

### Database Connection Error

```bash
psql -h <host> -p 5432 -U jellyfin_helper -d jellyfin_helper
```

If you can't connect, check in order: network, firewall, whether `pg_hba.conf` allows the IP,
the user password, and whether the database exists.

### Torrent Addition Fails

The frontend ElMessage now gives a specific reason, distinguished by status code:

- **HTTP 409**: the torrent is already in the qB queue (the pre-check intercepts it first and
  shows the torrent name / status)
- **HTTP 415**: the torrent file is invalid (not bencode format)
- **HTTP 502 + "qBittorrent rejected the torrent"**: qB's default download directory does not
  exist / has no permissions, or the category has not been created in qB

---

## License

MIT
