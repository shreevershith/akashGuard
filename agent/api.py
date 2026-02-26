from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import StreamingResponse

import agent.database as db
import agent.event_bus as bus
from agent.config import settings

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Agent lifecycle — runs the monitoring loop inside the FastAPI process so the
# SSE event bus (in-memory queues) is shared with the API endpoints.
# ---------------------------------------------------------------------------

_agent_instance = None
_agent_task: asyncio.Task | None = None
_start_time = time.time()


async def _start_agent() -> None:
    global _agent_instance, _agent_task

    db.init_db()

    if not settings.agent_auto_monitor:
        return

    from agent.main import AkashGuardAgent

    _agent_instance = AkashGuardAgent()
    _agent_instance.running = True

    async def _loop() -> None:
        while _agent_instance.running:
            try:
                await _agent_instance.monitor_cycle()
            except Exception:
                pass
            await asyncio.sleep(settings.health_check_interval)

    _agent_task = asyncio.create_task(_loop())


async def _stop_agent() -> None:
    if _agent_instance:
        _agent_instance.running = False
    if _agent_task:
        _agent_task.cancel()
        try:
            await _agent_task
        except (asyncio.CancelledError, Exception):
            pass
    if _agent_instance:
        await _agent_instance._cleanup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _start_agent()
    yield
    await _stop_agent()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="AkashGuard", docs_url="/docs", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@app.get("/api/services")
async def get_services() -> JSONResponse:
    services = db.get_all_services()
    return JSONResponse({"services": services})


@app.get("/api/health-checks")
async def get_health_checks(service_id: int | None = None, limit: int = 50) -> JSONResponse:
    if service_id:
        checks = db.get_recent_health_checks(service_id, limit)
        return JSONResponse({"health_checks": {"_": checks}})
    services = db.get_all_services()
    result = {}
    for svc in services:
        result[svc["name"]] = db.get_recent_health_checks(svc["id"], limit)
    return JSONResponse({"health_checks": result})


@app.get("/api/decisions")
async def get_decisions(limit: int = 20) -> JSONResponse:
    decisions = db.get_recent_decisions(limit)
    return JSONResponse({"decisions": decisions})


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status() -> JSONResponse:
    services = db.get_all_services()
    enriched = []
    for svc in services:
        recent = db.get_recent_health_checks(svc["id"], limit=1)
        last_check = recent[0] if recent else None
        enriched.append({
            "name": svc["name"],
            "status": svc.get("status", "unknown"),
            "health_url": svc.get("health_url", ""),
            "dseq": svc.get("current_dseq"),
            "provider": svc.get("current_provider"),
            "last_check": last_check.get("checked_at") if last_check else None,
            "last_response_time_ms": last_check.get("response_time_ms") if last_check else None,
            "consecutive_failures": svc.get("consecutive_failures", 0),
        })

    return JSONResponse({
        "services": enriched,
        "recent_events": bus.get_recent_events(50),
        "agent_status": "running" if _agent_instance and _agent_instance.running else "stopped",
        "agent_uptime": round(time.time() - _start_time),
    })


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

@app.get("/api/events/stream")
async def event_stream():
    queue = bus.subscribe()

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {json.dumps({'type': 'heartbeat', 'data': {}, 'timestamp': time.time()})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Kill service (close Akash deployment)
# ---------------------------------------------------------------------------

@app.post("/api/services/{service_name}/kill")
async def kill_service(service_name: str) -> JSONResponse:
    services = db.get_all_services()
    svc = next((s for s in services if s["name"] == service_name), None)
    if not svc:
        return JSONResponse({"error": f"Service '{service_name}' not found"}, status_code=404)

    dseq = svc.get("current_dseq")
    if not dseq:
        return JSONResponse({"error": f"Service '{service_name}' has no deployment to kill"}, status_code=400)

    bus.emit("service_killed", {
        "service": service_name,
        "dseq": dseq,
        "detail": f"User killed deployment DSEQ {dseq}",
    })

    from agent.recovery_engine import RecoveryEngine
    engine = RecoveryEngine()
    try:
        success = await engine.close_deployment(dseq)
    finally:
        await engine.close()

    if success:
        return JSONResponse({"status": "killed", "dseq": dseq, "service": service_name})
    else:
        return JSONResponse({"status": "kill_failed", "dseq": dseq, "error": "Failed to close deployment"}, status_code=500)


# ---------------------------------------------------------------------------
# Simulate failure
# ---------------------------------------------------------------------------

@app.post("/api/services/{service_name}/simulate-failure")
async def simulate_failure(service_name: str) -> JSONResponse:
    services = db.get_all_services()
    svc = next((s for s in services if s["name"] == service_name), None)
    if not svc:
        return JSONResponse({"error": f"Service '{service_name}' not found"}, status_code=404)

    from agent.main import simulate_failures
    simulate_failures[service_name] = 5

    bus.emit("health_check", {
        "service": service_name,
        "status": "simulated_failure",
        "status_code": None,
        "response_time_ms": None,
        "error": "Simulated failure activated - next 5 health checks will fail",
    })

    return JSONResponse({
        "status": "simulating",
        "service": service_name,
        "fake_failures_remaining": 5,
    })


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def get_stats() -> JSONResponse:
    services = db.get_all_services()

    total_checks = 0
    total_failures = 0
    for svc in services:
        checks = db.get_recent_health_checks(svc["id"], limit=1000)
        total_checks += len(checks)
        total_failures += sum(1 for c in checks if not c["is_healthy"])

    decisions = db.get_recent_decisions(limit=100)
    total_recoveries = sum(1 for d in decisions if d["action_taken"] == "redeploy")
    successful_recoveries = sum(1 for d in decisions if d["action_taken"] == "redeploy" and d.get("outcome") == "success")
    recovery_times = [
        d["downtime_seconds"] for d in decisions
        if d["action_taken"] == "redeploy" and d.get("outcome") == "success" and d.get("downtime_seconds")
    ]
    avg_recovery_time = round(sum(recovery_times) / len(recovery_times), 1) if recovery_times else None

    return JSONResponse({
        "total_health_checks": total_checks,
        "total_failures_detected": total_failures,
        "total_recoveries_attempted": total_recoveries,
        "total_recoveries_succeeded": successful_recoveries,
        "avg_recovery_time_seconds": avg_recovery_time,
        "agent_uptime_seconds": round(time.time() - _start_time),
        "monitoring_interval_seconds": settings.health_check_interval,
        "failure_threshold": settings.failure_threshold,
        "llm_model": settings.akashml_model,
        "services_count": len(services),
    })
