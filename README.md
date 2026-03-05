# AkashGuard

**Autonomous self-healing agent for decentralized cloud deployments on Akash Network.**

AkashGuard monitors services deployed on Akash Network, diagnoses failures using AI, and autonomously recovers them. When a provider goes down, AkashGuard detects the failure, reasons about the best recovery action via LLM, and redeploys to a new provider automatically. Zero human intervention required.

Built for the **Open Agents Hackathon** (Feb 25, 2026 — Yes SF, San Francisco). 🥈 2nd Place, Akash Track.

---

## How It Works

```
Monitor Loop (every 30s)
  |-- Health check all registered services
  |-- Record metrics (status code, response time, errors)
  |-- If service unhealthy (3+ consecutive failures):
  |   |-- LLM Diagnosis (Llama 3.3 70B via AkashML)
  |   |   -> Analyzes health history, recommends action
  |   |-- Decision: redeploy / wait / scale
  |   |-- If redeploy (confidence > 70%):
  |   |   |-- Close current lease on Akash
  |   |   |-- Redeploy via Akash Console API
  |   |   |-- Wait for new provider + lease acceptance
  |   |   +-- Verify recovery with health check
  |   +-- Send Telegram alert
  +-- Stream events to dashboard via SSE
```

---

## Key Features

- **Autonomous Recovery** — Detects failures and redeploys to new providers without human intervention
- **AI-Powered Diagnosis** — Uses Llama 3.3 70B to analyze health data and make intelligent recovery decisions
- **Real-Time Dashboard** — Live monitoring via Server-Sent Events with service status, decision history, and provider tracking
- **Telegram Alerts** — Instant notifications on service down/recovery events
- **Venice Voice + Vision** — Spoken incident summaries and visual recovery verification via Venice AI
- **Post-Recovery Cooldown** — 120s stabilization period prevents recovery thrashing
- **Provider Scoring** — Tracks provider reliability over time
- **Graceful Degradation** — Never crashes on partial failures; degrades intelligently
- **Demo Mode** — Simulated failure injection for live demonstrations

---

## Architecture

### Components

| Component | Description |
|-----------|-------------|
| **AkashGuard Agent** (`agent/`) | Core monitoring loop, LLM diagnosis, autonomous recovery engine |
| **Chatbot** (`chatbot/`) | Simple Flask chatbot deployed on Akash — the monitored service |
| **Dashboard** (`agent/static/`) | Real-time web dashboard showing service health and agent decisions |

### Tech Stack

| Layer | Technology |
|-------|------------|
| Runtime | Python 3.11, asyncio, FastAPI |
| AI/LLM | AkashML (Llama 3.3 70B, DeepSeek V3.2) |
| Voice + Vision | Venice AI (TTS, Vision) |
| Infrastructure | Akash Network, Docker, Akash Console API |
| Database | SQLite (WAL mode) |
| Alerts | Telegram Bot API |
| Tracing | Langfuse |

### Project Structure

```
akashguard/
├── agent/                      # AkashGuard autonomous agent
│   ├── main.py                 # Agent loop orchestrator
│   ├── api.py                  # FastAPI server + SSE dashboard
│   ├── health_checker.py       # HTTP health monitoring
│   ├── llm_engine.py           # LLM-powered diagnosis engine
│   ├── recovery_engine.py      # Akash Console API recovery
│   ├── database.py             # SQLite persistence layer
│   ├── event_bus.py            # Asyncio event queue (SSE)
│   ├── notifier.py             # Telegram alert integration
│   ├── config.py               # Pydantic settings
│   └── static/
│       └── dashboard.html      # Real-time monitoring UI
│
├── chatbot/                    # Monitored service (Flask chatbot)
│   ├── app.py                  # Flask + AkashML Llama chat
│   ├── Dockerfile
│   └── requirements.txt
│
├── deploy/                     # Akash deployment configs
│   ├── agent-sdl.yaml          # AkashGuard SDL
│   └── chatbot-sdl.yaml        # Chatbot SDL
│
├── misc/                       # Testing & debugging scripts
├── data/                       # SQLite database (gitignored)
├── .env.example                # Environment variable template
└── README.md
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (for container workflow)
- API keys: AkashML, Akash Console, Telegram (optional), Venice (optional)

### Setup

```bash
git clone https://github.com/xploit007/akashGuard.git
cd akashGuard

python -m pip install -r agent/requirements.txt
python -m pip install -r chatbot/requirements.txt

cp .env.example .env
# Edit .env with your API keys
```

---

## Running the Agent

### Agent + API (monitoring enabled)

```powershell
$env:AGENT_AUTO_MONITOR = "true"
uvicorn agent.api:app --host 0.0.0.0 --port 8000
```

### API only (no monitoring loop)

```powershell
$env:AGENT_AUTO_MONITOR = "false"
uvicorn agent.api:app --host 0.0.0.0 --port 8000
```

---

## Docker

### Agent

```powershell
# Build and run
docker build -t DOCKER_HUB_USERNAME/akashguard-agent:latest -f agent/Dockerfile agent
docker run --rm -d --name akashguard-agent -p 8000:8000 --env-file .env DOCKER_HUB_USERNAME/akashguard-agent:latest

# Push
docker push DOCKER_HUB_USERNAME/akashguard-agent:latest
```

### Chatbot

```powershell
docker build -t DOCKER_HUB_USERNAME/akashguard-chatbot:latest chatbot/
docker push DOCKER_HUB_USERNAME/akashguard-chatbot:latest
```

---

## Register a Service (Required)

The agent starts with no services registered. You need to register at least one service before monitoring begins.

### 1. Check current services

```powershell
Invoke-RestMethod http://localhost:8000/api/services
```

### 2. Register when running the agent locally

```bash
python -c "from pathlib import Path; from agent.database import init_db, add_service; init_db(); sdl=Path('deploy/chatbot-sdl.yaml').read_text(); print(add_service('chatbot','http://localhost:5000/health',sdl))"
```

### 3. Register when running the agent in Docker

Use `host.docker.internal` so the containerized agent can reach host services.

```powershell
docker cp deploy\chatbot-sdl.yaml akashguard-agent:/app/chatbot-sdl.yaml
docker exec akashguard-agent python -c "from pathlib import Path; from agent.database import init_db, add_service; init_db(); sdl=Path('/app/chatbot-sdl.yaml').read_text(); print(add_service('chatbot','http://host.docker.internal:5000/health',sdl))"
```

### 4. Verify registration

```powershell
Invoke-RestMethod http://localhost:8000/api/services
```

---

## Local Test Flow

1. Start the chatbot (or any target service with a health endpoint)
2. Start the agent API on port 8000
3. Register the service (see above)
4. Open `http://localhost:8000/` to view the dashboard
5. Trigger a failure simulation:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/services/chatbot/simulate-failure
```

---

## Deploy to Akash

1. Build and push images to Docker Hub (see Docker section above)
2. Deploy using SDL files in `deploy/` via Akash Console or CLI
3. Monitor from the AkashGuard dashboard and Telegram alerts

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Dashboard UI |
| GET | `/docs` | Swagger UI |
| GET | `/api/services` | List monitored services |
| POST | `/api/services` | Register a new service |
| GET | `/api/status` | Agent status snapshot |
| GET | `/api/health-checks` | Recent health checks |
| GET | `/api/decisions` | Recent decisions |
| GET | `/api/stats` | Aggregated monitoring/recovery stats |
| GET | `/api/events/stream` | SSE event stream |
| POST | `/api/services/{service_name}/kill` | Kill service deployment |
| POST | `/api/services/{service_name}/simulate-failure` | Simulate failure |

> Note: there is no `/health` endpoint on the agent API in the current version.

---

## Venice AI Integration

Venice is used in two places in AkashGuard:

**1. Voice incident summaries (TTS + LLM narration)**
- On recovery completion, the agent generates a short spoken summary
- Venice chat completions produce the narration text, then Venice TTS synthesizes the audio
- The audio is sent as a Telegram voice message

**2. Post-recovery visual verification (Vision)**
- After redeploy, the agent captures a screenshot of the recovered service
- The image is sent to Venice Vision to assess whether the service appears healthy
- The result is emitted to the event stream and reported via Telegram

If `VENICE_API_KEY` is not set, Venice features are skipped gracefully and the agent continues to run normally.

---

## Environment Variables

See [.env.example](.env.example) for the full list. Key variables:

| Variable | Description |
|----------|-------------|
| `AKASHML_API_KEY` | AkashML API key for LLM diagnosis |
| `AKASH_CONSOLE_API_KEY` | Akash Console API key for deployments |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications |
| `VENICE_API_KEY` | Venice API key for voice + vision features |
| `VENICE_API_BASE` | Venice API base URL (default: `https://api.venice.ai/api/v1`) |
| `VENICE_TTS_MODEL` | Venice TTS model (default: `tts-kokoro`) |
| `VENICE_TTS_VOICE` | Venice TTS voice (default: `af_sky`) |
| `VENICE_VISION_MODEL` | Venice vision model for screenshot verification |
| `VENICE_CHAT_MODEL` | Venice chat model for voice narration generation |
| `HEALTH_CHECK_INTERVAL` | Seconds between health checks (default: 30) |
| `FAILURE_THRESHOLD` | Consecutive failures before recovery (default: 3) |

---

## Demo Flow

1. **Show dashboard** — Services green, agent monitoring
2. **Kill a deployment** — Simulate provider failure
3. **Watch AkashGuard** — Agent detects failure, LLM diagnoses, auto-redeploys
4. **Service returns** — New provider, new lease, service back online
5. **Telegram alert** — Real-time notification with voice summary and visual verification

---

## Docker Purge (project only)

Removes AkashGuard/chatbot containers, images, and build cache.

```powershell
# Stop and remove containers
$containers = docker ps -a --format "{{.ID}} {{.Names}} {{.Image}}" | Select-String -Pattern "akashguard|chatbot" | ForEach-Object { ($_ -split " ")[0] }
if ($containers) { docker stop $containers; docker rm $containers }

# Remove images
$images = docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | Select-String -Pattern "akashguard|chatbot" | ForEach-Object { ($_ -split " ")[1] }
if ($images) { docker rmi -f $images }

# Remove build cache
docker builder prune -f
```

> Note: `docker builder prune -f` clears the entire build cache, not just AkashGuard-related layers. Skip this line if you want to preserve cache for other projects.

---

## Notes

- Certificate creation via deprecated API endpoint is not required in this repo's current deployment flow.
- If the Telegram image does not arrive but text/audio does, `sendPhoto` failed and the notifier used fallback text.

---

## License

MIT