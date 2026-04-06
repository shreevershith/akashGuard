# AkashGuard

**Autonomous self-healing agent for decentralized cloud deployments on Akash Network.**

AkashGuard monitors services deployed on Akash Network, diagnoses failures using AI, and autonomously recovers them. When a deployment is killed or a provider goes down, AkashGuard detects the failure, reasons about the best recovery action via LLM, and redeploys to a new provider automatically. Zero human intervention required.

Built for the **Open Agents Hackathon** (Feb 25, 2026 — Yes SF, San Francisco). 🥈 2nd Place, Akash Track.

---

## How It Works

```
Monitor loop (every 15s by default)
  |-- Auto-discovery (optional): sync active Akash deployments into services DB
  |-- Health check all monitored services
  |-- Record metrics (status code, response time, errors)
  |-- If service unhealthy (3+ consecutive failures by default):
  |   |-- LLM diagnosis (Groq, e.g. Llama 3.3 70B)
  |   |   -> Recommends action: redeploy / wait / scale / none
  |   |-- If redeploy (confidence ≥ 70%):
  |   |   |-- Close old deployment on Akash (best effort)
  |   |   |-- Create new deployment via Akash Console API
  |   |   |-- Wait for bids (15s), accept cheapest, create lease
  |   |   |-- Poll for URIs (up to 60s), update DB
  |   |   +-- Post-recovery cooldown (60s) before re-evaluating
  |   +-- Telegram alert (optional)
  +-- Stream events to dashboard via SSE
```

**Recovery concurrency**

- **Default (sequential):** One recovery at a time. If two services are down, the second is recovered after the first finishes.
- **Optional parallel:** Set `RECOVERY_PARALLEL=true` and `RECOVERY_PARALLEL_MAX=2` to allow up to 2 recoveries at once for faster restoration when multiple services are down.

---

## Key Features

- **Autonomous recovery** — Detects failures and redeploys to new providers without human intervention
- **Killed-deployment handling** — When you kill a deployment in Akash Console, the agent marks it down, seeds failure records, and triggers redeploy (no manual re-register)
- **AI-powered diagnosis** — Groq (OpenAI-compatible API; default Llama 3.3 70B) analyzes health data and recommends redeploy / wait / scale
- **Real-time dashboard** — Live SSE updates; full sync on load/refresh and auto-refresh every 15s (services, pipeline, event log, stats)
- **Dashboard refresh** — “Refresh” button and pipeline hydration from recent events so the pipeline does not reset to “Ping” on page reload
- **Optional parallel recovery** — `RECOVERY_PARALLEL=true` allows up to 2 concurrent recoveries for faster multi-service restoration
- **Provider diversity** — Picks randomly from the top N cheapest bids (`RECOVERY_BID_TOP_N`) and deprioritizes providers that recently failed `create_lease`, so the same provider is not chosen every time
- **SDL fallback** — Auto-discovered services without SDL in DB can still be recovered using `AUTO_DISCOVER_SDL_TEMPLATE_PATH` or `/app/chatbot-sdl.yaml` in Docker
- **Telegram alerts** — Notifications on threshold hit, LLM decision, and recovery complete/failed
- **Venice voice + vision** — Spoken incident summaries and visual recovery verification (optional)
- **Post-recovery cooldown** — 60s (configurable) to avoid thrashing
- **Demo mode** — Simulate failures via API for testing

---

## Architecture

### Components

| Component | Description |
|-----------|-------------|
| **AkashGuard Agent** (`agent/`) | Monitor loop, LLM diagnosis, recovery engine (Console API) |
| **Chatbot** (`chatbot/`) | Example Flask service deployed on Akash (monitored target) |
| **Dashboard** (`agent/static/`) | Real-time UI: services, agent pipeline, event log, stats |

### Tech Stack

| Layer | Technology |
|-------|------------|
| Runtime | Python 3.11, asyncio, FastAPI |
| AI/LLM | Groq (diagnosis); AkashML optional for other stacks |
| Voice + Vision | Venice AI (optional) |
| Infrastructure | Akash Network, Docker, Akash Console API |
| Database | SQLite (WAL) |
| Alerts | Telegram Bot API |
| Tracing | Langfuse (optional) |

### Project Structure

```
akashguard/
├── agent/
│   ├── main.py           # Monitor loop, evaluation, recovery orchestration
│   ├── api.py            # FastAPI + SSE
│   ├── health_checker.py # HTTP health checks
│   ├── llm_engine.py     # LLM diagnosis
│   ├── recovery_engine.py# Akash Console API (deploy, bids, lease, URIs)
│   ├── database.py       # SQLite, discovery helpers
│   ├── event_bus.py      # In-process event bus for SSE
│   ├── notifier.py       # Telegram + Venice
│   ├── config.py         # Pydantic settings
│   └── static/
│       └── dashboard.html# Real-time dashboard
├── chatbot/              # Example monitored service
├── deploy/               # SDL for agent and chatbot
├── data/                 # SQLite DB (gitignored)
├── .env.example
└── README.md
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (for container workflow)
- API keys: **Akash Console**, **Groq** (diagnosis LLM); optional: AkashML (chatbot), Telegram, Venice

### Setup

```bash
git clone https://github.com/shreevershith/akashGuard.git
cd akashGuard

pip install -r agent/requirements.txt
pip install -r chatbot/requirements.txt

cp .env.example .env
# Edit .env: AKASH_CONSOLE_API_KEY, GROQ_API_KEY, etc.
```

---

## Running the Agent

### With monitoring (recommended)

```powershell
$env:AGENT_AUTO_MONITOR = "true"
uvicorn agent.api:app --host 0.0.0.0 --port 8000
```

### API only (no monitor loop)

```powershell
$env:AGENT_AUTO_MONITOR = "false"
uvicorn agent.api:app --host 0.0.0.0 --port 8000
```

---

## Docker

### Agent

```powershell
docker rm -f akashguard-agent
docker build -t DOCKER_HUB_USERNAME/akashguard-agent:latest -f agent/Dockerfile agent
docker run --rm -d --name akashguard-agent -p 8000:8000 --env-file .env DOCKER_HUB_USERNAME/akashguard-agent:latest
```

Optional: persist DB across restarts

```powershell
docker run --rm -d --name akashguard-agent -p 8000:8000 --env-file .env -v "${PWD}/data:/app/agent/data" DOCKER_HUB_USERNAME/akashguard-agent:latest
```

### Chatbot

```powershell
docker build -t DOCKER_HUB_USERNAME/akashguard-chatbot:latest chatbot/
docker push DOCKER_HUB_USERNAME/akashguard-chatbot:latest
```

---

## Register a Service

The agent can run with **auto-discovery** (syncs deployments from Akash Console) or with **manually registered** services.

### Check current services

```powershell
Invoke-RestMethod http://localhost:8000/api/services
```

### Register or update (idempotent)

Use `add_or_update_service` so the same command is safe to run multiple times.

**Local:**

```bash
python -c "from pathlib import Path; from agent.database import init_db, add_or_update_service; init_db(); sdl=Path('deploy/chatbot-sdl.yaml').read_text(); print(add_or_update_service('chatbot','http://localhost:5000/health',sdl))"
```

**Docker (copy SDL then register):**

```powershell
docker cp deploy\chatbot-sdl.yaml akashguard-agent:/app/chatbot-sdl.yaml
docker exec akashguard-agent python -c "from pathlib import Path; from agent.database import init_db, add_or_update_service; init_db(); sdl=Path('/app/chatbot-sdl.yaml').read_text(); print(add_or_update_service('chatbot','http://host.docker.internal:5000/health',sdl))"
```

For services on Akash (not host), use the deployment’s health URL instead of `host.docker.internal`.

### Auto-discovery

Set in `.env`:

- `AUTO_DISCOVER_DEPLOYMENTS=true`
- `AUTO_DISCOVER_INTERVAL_SECONDS=30`
- `AUTO_DISCOVER_SDL_TEMPLATE_PATH=/app/chatbot-sdl.yaml` (or a path the agent can read)

Then the agent syncs active deployments from the Console API into the services table. When a deployment is **killed**, it is marked **down** (not removed); failure records are seeded so the next cycle triggers LLM diagnosis and redeploy. Placeholder services (`akash-{dseq}`) are created when a deployment has no URIs yet and upgraded when URIs appear.

---

## Dashboard

- **URL:** `http://localhost:8000/`
- **Live updates:** SSE for health checks, diagnosis, recovery progress.
- **On load/refresh:** Full sync from API (services, pipeline, event log, stats) so the pipeline does not reset to “Ping.”
- **Refresh button:** Resyncs services, pipeline, event log, and stats without a full page reload.
- **Auto-refresh:** Every 15s the dashboard refetches and updates everything.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Dashboard UI |
| GET | `/docs` | Swagger UI |
| GET | `/api/services` | List monitored services |
| POST | `/api/services` | Register a service |
| GET | `/api/status` | Status snapshot (services, recent_events, failure_threshold, agent status) |
| GET | `/api/health-checks` | Recent health checks |
| GET | `/api/decisions` | Recent decisions |
| GET | `/api/stats` | Aggregated stats (checks, recoveries, thresholds) |
| GET | `/api/events/stream` | SSE event stream |
| POST | `/api/services/{name}/kill` | Kill deployment on Akash |
| POST | `/api/services/{name}/simulate-failure` | Simulate failures (demo) |

---

## Environment Variables

See [.env.example](.env.example). Key variables:

| Variable | Description | Default |
|----------|-------------|--------|
| `AKASH_CONSOLE_API_KEY` | Akash Console API key | required |
| `AKASH_CONSOLE_API_BASE` | Console API base URL | `https://console-api.akash.network/v1` |
| `GROQ_API_KEY` | Groq API key for diagnosis LLM | required for diagnosis |
| `GROQ_BASE_URL` | Groq OpenAI-compatible base URL | `https://api.groq.com/openai/v1` |
| `GROQ_MODEL` | Diagnosis model id | `llama-3.3-70b-versatile` |
| `AKASHML_API_KEY` | AkashML (optional; not used for agent diagnosis) | optional |
| `AKASHML_BASE_URL` | AkashML base URL | `https://api.akashml.com/v1` |
| `AKASHML_MODEL` | AkashML model | `meta-llama/Llama-3.3-70B-Instruct` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | optional |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | optional |
| `HEALTH_CHECK_INTERVAL` | Seconds between monitor cycles | `15` |
| `FAILURE_THRESHOLD` | Consecutive failures before recovery | `3` |
| `RECOVERY_COOLDOWN_SECONDS` | Cooldown after successful recovery | `60` |
| `RECOVERY_BID_WAIT_SECONDS` | Wait for provider bids | `15` |
| `RECOVERY_URI_POLL_SECONDS` | Max wait for URIs after lease | `60` |
| `RECOVERY_URI_POLL_INTERVAL_SECONDS` | Poll interval for URIs | `5` |
| `RECOVERY_LEASE_RETRY_DELAY_SECONDS` | Delay between create_lease retries | `5` |
| `RECOVERY_PARALLEL` | Allow concurrent recoveries | `false` |
| `RECOVERY_PARALLEL_MAX` | Max concurrent recoveries when parallel | `2` |
| `RECOVERY_BID_TOP_N` | Pick randomly from top N cheapest bids (provider diversity) | `3` |
| `RECOVERY_FAILED_PROVIDER_AVOID_SECONDS` | After a provider fails create_lease, avoid them for this many seconds (then they can be chosen again) | `1800` (30 min) |
| `AGENT_AUTO_MONITOR` | Run monitor loop when API starts | `false` |
| `AUTO_DISCOVER_DEPLOYMENTS` | Sync deployments from Console API | `false` |
| `AUTO_DISCOVER_INTERVAL_SECONDS` | Min seconds between discovery syncs | `30` |
| `AUTO_DISCOVER_SDL_TEMPLATE_PATH` | SDL path for discovered services (redeploy) | `` |
| `DB_PATH` | SQLite database path | `./data/akashguard.db` |
| Venice / Langfuse | See .env.example | optional |

---

## Venice AI Integration (Optional)

- **Voice:** After recovery, a short spoken summary is generated and sent as a Telegram voice message.
- **Vision:** After redeploy, a screenshot of the service is sent to Venice Vision to verify the page looks healthy.

If `VENICE_API_KEY` is not set, these features are skipped and the agent runs normally.

---

## Demo Flow

1. Open the dashboard — services and pipeline show current state.
2. Kill a deployment (Akash Console or “Kill service” in the dashboard).
3. Agent marks it down, runs LLM diagnosis, and redeploys (if SDL is available).
4. Dashboard and Telegram show recovery progress and result.
5. Optional: use “Simulate failure” to inject fake failures without killing a real deployment.

---

## Docker Purge (project only)

```powershell
$containers = docker ps -a --format "{{.ID}} {{.Names}} {{.Image}}" | Select-String -Pattern "akashguard|chatbot" | ForEach-Object { ($_ -split " ")[0] }
if ($containers) { docker stop $containers; docker rm $containers }

$images = docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | Select-String -Pattern "akashguard|chatbot" | ForEach-Object { ($_ -split " ")[1] }
if ($images) { docker rmi -f $images }

docker builder prune -f
```

---

## License

MIT
