# Multi-user mode

Everyone signs in with **Google, GitHub or Microsoft**. Each person gets their own progress, their own notebook
environment (a locked-down Docker container) and their own sandbox for running tests. The normal single-user mode
(`./setup.sh`, `./launch.sh`) is unchanged and needs none of this.

```
Browser ──HTTPS──> Caddy ──> app (FastAPI) ──┬─ SQLite: users + each user's progress
                                             ├─ /auth/*     OAuth sign-in, signed session cookie
                                             ├─ /api/*      per-user tutor logic, LLM calls (rate-limited)
                                             └─ /jupyter/*  authenticated reverse proxy ──> THAT user's container
                                                   per-user JupyterLab container   (internal network, no internet)
                                                   throwaway test sandbox          (docker run --rm --network none)
```

## Security model (read this before putting it on the internet)

Every user can run arbitrary Python. What stands between users, and between users and your server:

| Threat | Defence |
|---|---|
| One user reads another's notebooks | Separate container + separate Docker volume per user. The proxy picks the container **only from the signed session**, never from the URL. |
| Learner code steals API keys / OAuth secrets | Containers get no environment secrets (only their own Jupyter token). Test sandboxes run with no network and no env. |
| Learner code attacks the host | Containers: non-root user, read-only root fs, all capabilities dropped, `no-new-privileges`, 768 MB RAM, 1 CPU, 256 processes. Test sandbox: no network, 256 MB, 128 processes, killed after 50 s. |
| Learner code uses your network / sends spam / mines | In Docker Compose mode user containers sit on an `internal` network with no route to the internet. |
| Account takeover through email spoofing | Users are keyed by `(provider, provider's user id)`, never email; accounts on different providers are never merged. Allow-lists only trust **verified** emails. |
| CSRF / cross-site WebSocket hijacking | `SameSite=Lax` cookie + Origin / Sec-Fetch-Site checks on every state-changing request and on WebSocket handshakes. |
| Login CSRF / token substitution | OAuth `state`, OIDC `nonce`, PKCE (S256), ID-token signature + issuer + audience checks. |
| Someone burns your LLM budget | Per-user hourly call cap (`TUTOR_LLM_CALLS_PER_HOUR`), input size limits. |
| Session theft | HttpOnly, Secure (https) cookie, 7-day lifetime, signed with `TUTOR_SECRET_KEY`. |

**Not covered / you should know:**
- The app container needs the Docker socket to start user containers. Anyone who compromises the *app* controls
  the host's Docker. Keep the host dedicated to this service and patched. (For stronger setups use a socket proxy
  that only allows container create/start/stop, or Kubernetes with a spawner.)
- User containers share one internal network (they can reach each other's port 8888, but each Jupyter requires a
  256-bit token). Per-user networks would be stricter.
- No disk quota per user volume (Docker volumes have none by default). Add an XFS project quota or a periodic check.
- Learners can use their allotted CPU/RAM; there's no global fair-share scheduler beyond `TUTOR_MAX_CONTAINERS`.
- State (rate limits, process registry) is in memory: run **one** app replica. SQLite is the database.
- No admin UI. Deleting a user = delete their row/state and `docker volume rm tutor-ws-<id>`.

## 1. Create the configuration

```
python3 setup_multiuser.py
```
It asks for your public address, walks you through registering the app with each provider (showing the exact
redirect URI), asks who may sign in, which AI key to use, and writes `.env.multiuser` (mode 600, secrets inside,
never commit it). Redirect URIs are always `<public address>/auth/callback/<google|github|microsoft>`.

Provider notes:
- **Google:** while the OAuth consent screen is in *Testing*, only listed test users can sign in. Publish it for the public.
- **GitHub:** an OAuth App allows exactly one callback URL; use a separate app for localhost testing.
- **Microsoft:** for company use register a **single-tenant** app and set `MICROSOFT_TENANT` to the tenant id; everyone in
  that tenant can sign in. Multi-tenant (`common`) works for open sign-up but cannot be combined with allow-lists, because
  Microsoft's `email` claim is not verified.

## 2a. Production: Docker Compose with automatic HTTPS

Prerequisites: a Linux server with Docker + Compose, a DNS name pointing at it, ports 80/443 open.

```
docker compose --profile build build jupyter-image          # per-user image (once, and after upgrades)
export DOCKER_GID=$(stat -c %g /var/run/docker.sock)        # lets the app use the Docker socket
docker compose up -d --build
docker compose logs -f app
```
Caddy gets a Let's Encrypt certificate for `TUTOR_DOMAIN` automatically. Open `https://<your domain>`.

## 2b. Try it on one machine (no HTTPS proxy)

```
./setup.sh                      # once (creates .venv)
python3 setup_multiuser.py      # use http://localhost:8765 as the public address
./launch_multiuser.sh           # builds the per-user image on first run, then starts the app
```
Add the localhost redirect URIs in the provider consoles (Google allows http://localhost URIs for development).
`TUTOR_ISOLATION=none` runs users as plain processes with **no isolation**. It exists for development and fully trusted
groups only and prints a warning at startup.

## Settings (environment variables, normally in `.env.multiuser`)

| Variable | Meaning | Default |
|---|---|---|
| `TUTOR_MODE` | `multiuser` to enable all of this | `local` |
| `TUTOR_BASE_URL` | public address, e.g. `https://tutor.example.com` (https required except localhost) | required |
| `TUTOR_SECRET_KEY` | session signing key, ≥ 32 random chars | required |
| `GOOGLE_CLIENT_ID/SECRET`, `GITHUB_CLIENT_ID/SECRET`, `MICROSOFT_CLIENT_ID/SECRET` | enable that provider | |
| `MICROSOFT_TENANT` | tenant id, or `common`/`organizations`/`consumers` | `common` |
| `TUTOR_ALLOWED_DOMAINS`, `TUTOR_ALLOWED_EMAILS` | comma lists; only verified emails count | open to all |
| `TUTOR_LLM` + key | `llmgateway` (default, needs `LLMGATEWAY_API_KEY`) / `anthropic` / `openai`; not `claude-cli` | `llmgateway` |
| `TUTOR_MODEL` | model id (LLM Gateway default: `gpt-4o`; list yours with `python llmgateway_models.py`) | |
| `TUTOR_LLM_CALLS_PER_HOUR` | AI calls per user per hour | 120 |
| `TUTOR_ISOLATION` | `docker` or `none` (unsafe) | `docker` |
| `TUTOR_DOCKER_IMAGE` | per-user image name | `coding-tutor-jupyter` |
| `TUTOR_DOCKER_NETWORK` | set (by compose) when the app itself runs in Docker | |
| `TUTOR_MAX_CONTAINERS` | environments running at once | 50 |
| `TUTOR_IDLE_MINUTES` | stop idle environments after | 30 |
| `TUTOR_DATA_DIR` | database (and, with `none`, user files) | `./data` |
| `TUTOR_HOST`, `TUTOR_PORT` | listen address | `127.0.0.1`, `8765` |

## Operating it

- **Backups:** the `tutor-data` volume (SQLite: users and progress) and the `tutor-ws-<id>` volumes (notebooks).
- **Upgrade:** `git pull && docker compose --profile build build jupyter-image && docker compose up -d --build`.
  Running user containers keep the old image until the idle reaper stops them.
- **See who's active:** `docker ps --filter label=tutor.user`.
- **Free capacity now:** `docker stop $(docker ps -q --filter label=tutor.user)` (they restart on next use; work is kept).
- **Restart behaviour:** user containers survive app restarts; the app re-adopts them and reaps idle ones.
