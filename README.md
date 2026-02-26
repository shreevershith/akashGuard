# AkashGuard

**Autonomous self-healing agent for decentralized cloud deployments on Akash Network.**

AkashGuard monitors services deployed on Akash Network, diagnoses failures using AI, and autonomously recovers them — zero human intervention required. When a provider goes down, AkashGuard detects the failure, reasons about the best recovery action via LLM, and redeploys to a new provider automatically.

Built for the **Open Agents Hackathon** (Feb 25, 2026 — Yes SF, San Francisco).

---

## How It Works

```
Monitor Loop (every 30s)
  ├─ Health check all registered services
  ├─ Record metrics (status code, response time, errors)
  ├─ If service unhealthy (3+ consecutive failures):
  │   ├─ LLM Diagnosis (Llama 3.3 70B via AkashML)
  │   │   → Analyzes health history, recommends action
  │   ├─ Decision: redeploy / wait / scale
  │   ├─ If redeploy (confidence > 70%):
  │   │   ├─ Close current lease on Akash
  │   │   ├─ Redeploy via Akash Console API
  │   │   ├─ Wait for new provider + lease acceptance
  │   │   └─ Verify recovery with health check
  │   └─ Send Telegram alert
  └─ Stream events to dashboard via SSE
```

## Key Features

- **Autonomous Recovery** — Detects failures and redeploys to new providers without human intervention
- **AI-Powered Diagnosis** — Uses Llama 3.3 70B to analyze health data and make intelligent recovery decisions
- **Real-Time Dashboard** — Live monitoring via Server-Sent Events with service status, decision history, and provider tracking
- **Telegram Alerts** — Instant notifications on service down/recovery events
- **Post-Recovery Cooldown** — 120s stabilization period prevents recovery thrashing
- **Provider Scoring** — Tracks provider reliability over time
- **Graceful Degradation** — Never crashes on partial failures; degrades intelligently
- **Demo Mode** — Simulated failure injection for live demonstrations

## Architecture

### Components

| Component | Description |
|-----------|-------------|
| **AkashGuard Agent** (`agent/`) | Core monitoring loop, LLM diagnosis, autonomous recovery engine |
| **Chatbot** (`chatbot/`) | Simple Flask chatbot deployed on Akash — the monitored service |
| **Dashboard** (`agent/static/`) | Real-time web dashboard showing service health and agent decisions |

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11, asyncio, FastAPI |
| AI/LLM | AkashML (Llama 3.3 70B, DeepSeek V3.2) |
| Infrastructure | Akash Network, Docker, Akash Console API |
| Database | SQLite (WAL mode) |
| Alerts | Telegram Bot API |
| Tracing | Langfuse |

## Project Structure

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

## Quick Start

### Prerequisites

- Python 3.11+
- API keys: AkashML, Akash Console, Telegram (optional)

### Setup

```bash
# Clone
git clone https://github.com/xploit007/akashGuard.git
cd akashGuard

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys
```

### Run Locally

```bash
# Agent + API (monitoring + dashboard)
$env:AGENT_AUTO_MONITOR = "true"
uvicorn agent.api:app --host 0.0.0.0 --port 8000

# API only (no active monitoring)
$env:AGENT_AUTO_MONITOR = "true"
uvicorn agent.api:app --host 0.0.0.0 --port 8000
```

### Deploy to Akash

```bash
# Build and push Docker images
docker build -t DOCKER_HUB_USERNAME/akashguard-agent:latest agent/
docker push DOCKER_HUB_USERNAME/akashguard-agent:latest

docker build -t DOCKER_HUB_USERNAME/akashguard-chatbot:latest chatbot/
docker push DOCKER_HUB_USERNAME/akashguard-chatbot:latest

# Deploy via Akash Console or CLI using SDL files in deploy/
```

### Register a service (after Docker / first run)

The API starts with no services registered. To have the agent monitor your chatbot (or another service), register it once:

1. **Check services** (optional — will show 0 at first):

   ```powershell
   Invoke-RestMethod http://localhost:8000/api/services
   ```

2. **Register the chatbot** (initializes DB and adds the service with its SDL):

   ```bash
   python -c "from pathlib import Path; from agent.database import init_db, add_service; init_db(); sdl=Path('deploy/chatbot-sdl.yaml').read_text(); print(add_service('chatbot','http://localhost:5000/health',sdl))"
   ```

3. **Verify** — list services again; you should see the registered service:

   ```powershell
   Invoke-RestMethod http://localhost:8000/api/services
   ```

After this, the agent will monitor the registered service and the project is ready to use.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Agent health status |
| `GET` | `/` | Dashboard UI |
| `GET` | `/api/services` | List monitored services |
| `POST` | `/api/services` | Register a new service |
| `GET` | `/api/events` | SSE event stream |
| `POST` | `/api/simulate-failure` | Trigger simulated failure (demo) |

## Environment Variables

See [.env.example](.env.example) for the full list. Key variables:

| Variable | Description |
|----------|-------------|
| `AKASHML_API_KEY` | AkashML API key for LLM diagnosis |
| `AKASH_CONSOLE_API_KEY` | Akash Console API key for deployments |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for notifications |
| `HEALTH_CHECK_INTERVAL` | Seconds between health checks (default: 30) |
| `FAILURE_THRESHOLD` | Consecutive failures before recovery (default: 3) |

## Demo Flow

1. **Show dashboard** — Services green, agent monitoring
2. **Kill a deployment** — Simulate provider failure
3. **Watch AkashGuard** — Agent detects failure, LLM diagnoses, auto-redeploys
4. **Service returns** — New provider, new lease, service back online
5. **Telegram alert** — Real-time notification of recovery

## License

MIT
