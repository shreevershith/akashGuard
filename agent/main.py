"""
AkashGuard agent: monitor cycle, health checks, LLM diagnosis, and recovery.
Recovery is sequential by default; optional RECOVERY_PARALLEL allows up to N concurrent recoveries.
"""
import asyncio
import base64
import io
import logging
import time
from pathlib import Path
from typing import Any

from agent.config import settings
from agent.database import (
    add_service,
    get_all_services,
    get_monitored_services,
    get_recent_health_checks,
    init_db,
    mark_stale_discovered_services,
    record_health_check,
    update_placeholder_to_discovered,
    update_service_deployment,
    update_service_status,
    upsert_discovered_service,
)
import agent.event_bus as bus
from agent.health_checker import HealthChecker
from agent.llm_engine import DiagnosisEngine
from agent.notifier import TelegramNotifier
from agent.recovery_engine import RecoveryEngine

logger = logging.getLogger("akashguard.agent")

# Minimum LLM confidence to trigger redeploy (0–1)
REDEPLOY_CONFIDENCE_THRESHOLD = 0.7

# Demo mode: service_name -> number of fake failures left to inject
simulate_failures: dict[str, int] = {}

# Post-recovery cooldown: service_name -> timestamp when cooldown expires
recovery_cooldowns: dict[str, float] = {}

# Recovery concurrency: semaphore(1) = sequential, semaphore(N) = up to N in parallel when RECOVERY_PARALLEL=true
_recovery_limit = settings.recovery_parallel_max if settings.recovery_parallel else 1
recovery_semaphore = asyncio.Semaphore(_recovery_limit)


class AkashGuardAgent:

    def __init__(self) -> None:
        self.health_checker = HealthChecker()
        self.diagnosis_engine = DiagnosisEngine()
        self.recovery_engine = RecoveryEngine()
        self.notifier = TelegramNotifier()
        self.running = False
        self._cycles_completed = 0
        self._last_discovery_sync_ts = 0.0

    async def start(self) -> None:
        init_db()
        self.running = True
        logger.info(
            "AkashGuard agent started, interval=%ds, grace_period=%d cycles",
            settings.health_check_interval, settings.failure_threshold,
        )
        try:
            await self.run_loop()
        finally:
            await self._cleanup()

    async def run_loop(self) -> None:
        while self.running:
            try:
                await self.monitor_cycle()
            except Exception as exc:
                logger.error("monitor cycle failed: %s", exc)
            await asyncio.sleep(settings.health_check_interval)

    async def monitor_cycle(self) -> None:
        """One cycle: sync deployments, health-check all, then evaluate and recover (one at a time)."""
        await self._maybe_auto_discover_deployments()
        results = await self.health_checker.check_all_services()

        # Demo mode: override with simulated failures and re-record
        for r in results:
            name = r["service_name"]
            if name in simulate_failures and simulate_failures[name] > 0:
                r["is_healthy"] = False
                r["status_code"] = 503
                r["error_message"] = "Simulated failure (demo mode)"
                r["response_time_ms"] = None
                simulate_failures[name] -= 1
                if simulate_failures[name] <= 0:
                    del simulate_failures[name]
                try:
                    record_health_check(
                        service_id=r["service_id"],
                        status_code=503,
                        response_time_ms=None,
                        is_healthy=False,
                        error_message="Simulated failure (demo mode)",
                    )
                except Exception:
                    pass

        # Emit health_check events, suppress during cooldown
        for r in results:
            name = r["service_name"]
            if name in recovery_cooldowns:
                remaining = recovery_cooldowns[name] - time.time()
                if remaining > 0:
                    continue
                del recovery_cooldowns[name]
            bus.emit("health_check", {
                "service": name,
                "status": "healthy" if r["is_healthy"] else "unhealthy",
                "status_code": r.get("status_code"),
                "response_time_ms": r.get("response_time_ms"),
                "error": r.get("error_message"),
            })

        self._cycles_completed += 1

        # Grace period: avoid acting on stale DB state right after agent restart
        if self._cycles_completed < settings.failure_threshold:
            logger.info(
                "startup grace period: cycle %d/%d, collecting baseline data",
                self._cycles_completed, settings.failure_threshold,
            )
            return

        # Evaluate each service; recovery concurrency limited by recovery_semaphore (1 or N in parallel)
        services = get_monitored_services()
        for svc in services:
            try:
                await self._evaluate_and_act(svc)
            except Exception as exc:
                logger.error("evaluate_and_act failed for %s: %s", svc["name"], exc)


    async def _maybe_auto_discover_deployments(self) -> None:
        """Discover active Akash deployments and sync them into local services DB."""
        if not settings.auto_discover_deployments:
            return

        now = time.time()
        interval = max(10, settings.auto_discover_interval_seconds)
        if now - self._last_discovery_sync_ts < interval:
            return
        self._last_discovery_sync_ts = now

        try:
            sdl_template: str | None = None
            if settings.auto_discover_sdl_template_path:
                try:
                    sdl_template = Path(settings.auto_discover_sdl_template_path).read_text()
                except Exception as exc:
                    logger.warning(
                        "auto-discovery fallback SDL unreadable at %s: %s",
                        settings.auto_discover_sdl_template_path,
                        exc,
                    )

            existing = get_all_services()
            existing_by_name = {s.get("name", ""): s for s in existing}
            existing_by_dseq = {
                str(s.get("current_dseq")): s
                for s in existing
                if s.get("current_dseq")
            }
            seen_dseqs: set[str] = set()

            deployments = await self.recovery_engine.get_deployments()
            created = 0
            updated = 0
            seen = 0

            for dep in deployments:
                if not isinstance(dep, dict):
                    logger.debug("auto-discovery: skipping non-dict deployment item: %r", dep)
                    continue
                dep_data = dep.get("deployment", dep)
                if not isinstance(dep_data, dict):
                    logger.debug("auto-discovery: skipping non-dict deployment payload: %r", dep_data)
                    continue
                dep_id = dep_data.get("id", {}) if isinstance(dep_data, dict) else {}
                dseq = str(
                    dep_data.get("dseq")
                    or dep_id.get("dseq")
                    or ""
                ).strip()
                if not dseq:
                    continue
                seen_dseqs.add(dseq)

                detail = await self.recovery_engine.get_deployment(dseq)
                discovered = self._extract_discovered_services(detail, dseq)

                # If deployment has no leases/URIs yet (e.g. just created), add placeholder so it shows in dashboard
                if not discovered:
                    placeholder_name = f"akash-{dseq}"
                    upsert_discovered_service(
                        name=placeholder_name,
                        health_url="http://0.0.0.0/health",
                        dseq=dseq,
                        provider=None,
                        sdl_template=sdl_template,
                    )
                    seen += 1
                    if placeholder_name not in existing_by_name:
                        created += 1
                        existing_by_name[placeholder_name] = {"name": placeholder_name, "current_dseq": dseq}
                    existing_by_dseq[dseq] = {"name": placeholder_name, "current_dseq": dseq}
                    logger.info("auto-discovery: no URIs for dseq=%s, added placeholder %s", dseq, placeholder_name)
                    continue

                for item in discovered:
                    seen += 1
                    base_name = item["name"]
                    # Keep existing service name when dseq already exists in DB.
                    if dseq in existing_by_dseq:
                        item["name"] = existing_by_dseq[dseq]["name"]
                    else:
                        # If another deployment already uses this base name, keep each
                        # deployment as a separate service row by suffixing with dseq.
                        existing_same_name = existing_by_name.get(base_name)
                        if existing_same_name and str(existing_same_name.get("current_dseq", "")) != dseq:
                            item["name"] = f"{base_name}-{dseq}"
                    before = existing_by_name.get(item["name"])
                    discovered_sdl = sdl_template
                    if not discovered_sdl and before and before.get("sdl_template"):
                        discovered_sdl = before.get("sdl_template")
                    if not discovered_sdl:
                        base_existing = existing_by_name.get(base_name)
                        if base_existing and base_existing.get("sdl_template"):
                            discovered_sdl = base_existing.get("sdl_template")
                    # If we had a placeholder for this dseq, upgrade it to real name/url instead of creating duplicate
                    placeholder_updated = update_placeholder_to_discovered(
                        dseq=item["dseq"],
                        new_name=item["name"],
                        health_url=item["health_url"],
                        provider=item.get("provider"),
                        sdl_template=discovered_sdl,
                    )
                    if placeholder_updated:
                        updated += 1
                        existing_by_name.pop(f"akash-{dseq}", None)
                        existing_by_name[item["name"]] = {"name": item["name"], "current_dseq": item["dseq"]}
                        existing_by_dseq[item["dseq"]] = {"name": item["name"], "current_dseq": item["dseq"]}
                    else:
                        upsert_discovered_service(
                            name=item["name"],
                            health_url=item["health_url"],
                            dseq=item["dseq"],
                            provider=item.get("provider"),
                            sdl_template=discovered_sdl,
                        )
                        if before:
                            updated += 1
                        else:
                            created += 1
                            existing_by_name[item["name"]] = {
                                "name": item["name"],
                                "current_dseq": item["dseq"],
                            }
                        existing_by_dseq[item["dseq"]] = {
                            "name": item["name"],
                            "current_dseq": item["dseq"],
                        }

            stale_count, stale_service_ids = mark_stale_discovered_services(seen_dseqs)
            # Seed failed health checks for newly stale services so evaluation sees N consecutive
            # failures and triggers redeploy (they stay "down", not "inactive", so remain monitored)
            for sid in stale_service_ids:
                for _ in range(settings.failure_threshold):
                    try:
                        record_health_check(
                            service_id=sid,
                            status_code=None,
                            response_time_ms=None,
                            is_healthy=False,
                            error_message="Deployment no longer on Akash (e.g. killed)",
                        )
                    except Exception as exc:
                        logger.warning("seed failure record for stale service id=%s: %s", sid, exc)

            if seen > 0:
                bus.emit("auto_discovery_sync", {
                    "created": created,
                    "updated": updated,
                    "seen": seen,
                    "inactive_marked": stale_count,
                })
            logger.info(
                "auto-discovery sync complete: deployments=%d discovered=%d created=%d updated=%d stale_marked=%d",
                len(deployments), seen, created, updated, stale_count,
            )
        except Exception as exc:
            logger.warning("auto-discovery sync failed: %s", exc)

    @staticmethod
    def _extract_discovered_services(detail: dict[str, Any], fallback_dseq: str) -> list[dict[str, str]]:
        data = detail.get("data", detail) if isinstance(detail, dict) else {}
        dseq = str(data.get("dseq") or fallback_dseq)
        leases = data.get("leases", []) if isinstance(data, dict) else []

        discovered: list[dict[str, str]] = []
        seen_names: set[str] = set()

        for lease in leases:
            provider = (
                lease.get("provider")
                or lease.get("providerAddress")
                or lease.get("status", {}).get("provider")
                or ""
            )
            services = lease.get("status", {}).get("services", {})

            if isinstance(services, dict) and services:
                for raw_name, svc_info in services.items():
                    uris = svc_info.get("uris", []) if isinstance(svc_info, dict) else []
                    if not uris:
                        continue
                    name = str(raw_name or f"akash-{dseq}")
                    if name in seen_names:
                        continue
                    seen_names.add(name)
                    discovered.append({
                        "name": name,
                        "health_url": f"http://{uris[0]}/health",
                        "dseq": dseq,
                        "provider": str(provider),
                    })
                continue

            # Fallback for providers that return URIs directly on lease
            uris = lease.get("uris", [])
            if uris:
                name = f"akash-{dseq}"
                if name not in seen_names:
                    seen_names.add(name)
                    discovered.append({
                        "name": name,
                        "health_url": f"http://{uris[0]}/health",
                        "dseq": dseq,
                        "provider": str(provider),
                    })

        return discovered
    async def _evaluate_and_act(self, svc: dict[str, Any]) -> None:
        """Evaluate health, run LLM diagnosis if unhealthy; trigger recovery (concurrency limited by recovery_semaphore)."""
        sid = svc["id"]
        name = svc["name"]
        prev_status = svc.get("status", "unknown")

        # Skip if still in post-recovery cooldown
        if name in recovery_cooldowns:
            remaining = recovery_cooldowns[name] - time.time()
            if remaining > 0:
                logger.info("service=%s in cooldown (%.0fs remaining)", name, remaining)
                bus.emit("health_check", {
                    "service": name,
                    "status": "cooldown",
                    "remaining": round(remaining),
                })
                return
            del recovery_cooldowns[name]

        status, recent = self.health_checker.evaluate_service_health(sid)

        if status == "healthy":
            # No action; optionally emit service_healthy if recovering from down
            logger.info("service=%s status=healthy", name)
            if prev_status in ("down", "degraded", "recovering"):
                bus.emit("service_healthy", {"service": name})
            return

        if status == "unknown":
            logger.info("service=%s status=unknown (no checks yet)", name)
            return

        # Unhealthy: emit events and run LLM diagnosis
        failures = sum(1 for c in recent if not c["is_healthy"])
        bus.emit("service_down", {
            "service": name,
            "status": status,
            "consecutive_failures": failures,
        })

        # Telegram: notify on first failure
        if failures == 1:
            await self.notifier.notify_first_failure(name, "Health check failed")

        # Telegram: notify when threshold hit
        if failures == settings.failure_threshold:
            await self.notifier.notify_threshold_hit(name, failures, settings.failure_threshold)

        # Emit health_streak when nearing or hitting threshold
        bus.emit("health_streak", {
            "service": name,
            "consecutive_failures": failures,
            "threshold": settings.failure_threshold,
        })

        logger.warning("service=%s status=%s, requesting LLM diagnosis", name, status)

        bus.emit("diagnosis_start", {"service": name})

        checks = get_recent_health_checks(sid, limit=10)
        diagnosis = await self.diagnosis_engine.diagnose(
            service_id=sid,
            service_name=name,
            health_status=status,
            recent_checks=checks,
        )

        action = diagnosis["recommended_action"]
        confidence = diagnosis["confidence"]
        decision_id = diagnosis.get("decision_id")

        bus.emit("diagnosis", {
            "service": name,
            "diagnosis": diagnosis["diagnosis"],
            "confidence": confidence,
            "recommended_action": action,
            "reasoning": diagnosis.get("reasoning", ""),
        })

        # Emit the decision summary
        bus.emit("llm_decision", {
            "service": name,
            "action": action,
            "confidence": confidence,
            "reasoning_summary": diagnosis.get("reasoning", "")[:200],
        })

        logger.info(
            "service=%s llm_action=%s confidence=%.2f diagnosis=%s",
            name, action, confidence, diagnosis["diagnosis"],
        )

        # Telegram: notify LLM decision
        await self.notifier.notify_llm_decision(name, diagnosis)

        if action != "redeploy":
            logger.info("service=%s action=%s, no recovery needed", name, action)
            return
        if confidence < REDEPLOY_CONFIDENCE_THRESHOLD:
            logger.info(
                "service=%s confidence=%.2f < threshold=%.2f, skipping redeploy",
                name, confidence, REDEPLOY_CONFIDENCE_THRESHOLD,
            )
            return

        sdl = self._load_sdl(svc)
        # Fallback: auto-discovered services may have no SDL in DB; try config path and /app (Docker)
        if not sdl and settings.auto_discover_sdl_template_path:
            sdl = self._load_sdl_from_path(settings.auto_discover_sdl_template_path)
        if not sdl:
            for fallback in ("/app/chatbot-sdl.yaml", "/app/deploy/chatbot-sdl.yaml"):
                sdl = self._load_sdl_from_path(fallback)
                if sdl:
                    break
        if not sdl:
            logger.error("service=%s has no SDL, cannot redeploy. Set sdl_template in DB or AUTO_DISCOVER_SDL_TEMPLATE_PATH.", name)
            bus.emit("recovery_skipped", {"service": name, "reason": "no_sdl", "message": "No SDL template; set AUTO_DISCOVER_SDL_TEMPLATE_PATH or register service with SDL"})
            return

        detection_time = time.time()
        old_dseq = svc.get("current_dseq")

        # Concurrency limit: 1 by default (sequential), or up to recovery_parallel_max when RECOVERY_PARALLEL=true
        async with recovery_semaphore:
            bus.emit("recovery_start", {
                "service": name,
                "reason": diagnosis["diagnosis"],
                "old_dseq": old_dseq,
            })
            logger.info("service=%s initiating recovery (confidence=%.2f)", name, confidence)
            t0 = time.monotonic()
            result = await self.recovery_engine.recover_service(
                service_id=sid,
                sdl=sdl,
                old_dseq=old_dseq,
                decision_id=decision_id,
                service_name=name,
            )
            await self.notifier.notify_recovery_complete(
                name, result, diagnosis=diagnosis, detection_time=detection_time,
            )

        if result["success"]:
            cooldown = settings.recovery_cooldown_seconds
            recovery_cooldowns[name] = time.time() + cooldown
            logger.info("service=%s cooldown set for %ds", name, cooldown)

            # Mark service healthy and seed successful health checks so the next evaluation
            # sees "healthy" (not still the old failures) and doesn't immediately re-trigger redeploy
            update_service_status(sid, "healthy")
            for _ in range(settings.failure_threshold):
                try:
                    record_health_check(
                        service_id=sid,
                        status_code=200,
                        response_time_ms=0.0,
                        is_healthy=True,
                        error_message=None,
                    )
                except Exception as exc:
                    logger.warning("seed healthy check after recovery: %s", exc)
            bus.emit("service_healthy", {"service": name})

            total_time = result.get("total_time_seconds", round(time.monotonic() - t0, 1))
            new_uri = (result.get("uris") or [""])[0]
            bus.emit("recovery_complete", {
                "service": name,
                "new_dseq": result.get("new_dseq"),
                "new_uri": new_uri,
                "provider": result.get("provider"),
                "total_time_seconds": total_time,
            })
            logger.info(
                "service=%s recovery succeeded new_dseq=%s provider=%s uris=%s",
                name, result["new_dseq"], result["provider"], result["uris"],
            )

            # Vision-based health verification (background task)
            if new_uri:
                asyncio.create_task(
                    self._vision_verify(name, new_uri),
                    name=f"vision-verify-{name}",
                )
        else:
            bus.emit("recovery_failed", {
                "service": name,
                "error": result.get("error", "Unknown error"),
                "step": "recovery",
            })
            logger.error("service=%s recovery failed: %s", name, result["error"])

    async def _vision_verify(self, service_name: str, uri: str) -> None:
        """After recovery, wait briefly then screenshot and send to Venice vision for Telegram."""
        logger.info("service=%s vision verification scheduled, waiting 10s for boot", service_name)
        await asyncio.sleep(10)

        url = uri if uri.startswith("http") else f"http://{uri}"
        screenshot_b64 = await self._capture_screenshot(url)

        if not screenshot_b64:
            logger.warning("service=%s vision verification skipped: screenshot capture failed", service_name)
            return

        assessment = await self.notifier.venice.vision(
            screenshot_b64,
            "Is this web service functioning correctly? Check if the page loaded properly, shows expected content, and has no error messages.",
        )

        if not assessment:
            logger.warning("service=%s vision verification skipped: vision API unavailable", service_name)
            return
        logger.info("service=%s vision verification complete: %s", service_name, assessment)

    @staticmethod
    async def _capture_screenshot(url: str) -> str | None:
        """Capture a screenshot. Try playwright first, fall back to httpx HTML fetch."""
        # Try playwright
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                screenshot_bytes = await page.screenshot(type="png")
                await browser.close()
                return base64.b64encode(screenshot_bytes).decode("ascii")
        except Exception as exc:
            logger.warning("Playwright screenshot failed (%s), trying httpx fallback", exc)

        # Fallback: fetch HTML, render as simple image for vision model
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=_httpx.Timeout(10.0)) as client:
                resp = await client.get(url)
                html = resp.text[:2000]
                from PIL import Image, ImageDraw, ImageFont
                img = Image.new("RGB", (800, 400), (255, 255, 255))
                draw = ImageDraw.Draw(img)
                font = ImageFont.load_default()
                draw.text((20, 20), f"URL: {url}", fill=(0, 0, 0), font=font)
                draw.text((20, 40), f"Status: {resp.status_code}", fill=(0, 0, 0), font=font)
                lines = html.split("\n")[:15]
                y_pos = 70
                for line in lines:
                    draw.text((20, y_pos), line[:100], fill=(0, 0, 0), font=font)
                    y_pos += 18
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                return base64.b64encode(buf.read()).decode("ascii")
        except Exception as exc2:
            logger.error("httpx fallback screenshot also failed: %s", exc2)
            return None

    @staticmethod
    def _load_sdl(svc: dict[str, Any]) -> str | None:
        """Load SDL from svc: inline content or file path. Returns None if missing/unreadable."""
        sdl = svc.get("sdl_template")
        if not sdl:
            return None
        if isinstance(sdl, str) and sdl.rstrip().endswith((".yaml", ".yml")):
            return AkashGuardAgent._load_sdl_from_path(sdl)
        return sdl

    @staticmethod
    def _load_sdl_from_path(path: str) -> str | None:
        """Read SDL from a file path; try path as-is then /app/<basename> for Docker."""
        try:
            p = Path(path)
            if p.exists():
                return p.read_text()
            # Docker: file is often copied to /app/
            alt = Path("/app") / p.name
            if alt.exists():
                return alt.read_text()
            return None
        except Exception as exc:
            logger.debug("SDL path %s not readable: %s", path, exc)
            return None

    def register_service(
        self,
        name: str,
        health_url: str,
        sdl_path: str | None = None,
        current_dseq: str | None = None,
        current_provider: str | None = None,
    ) -> int:
        sdl_content: str | None = None
        if sdl_path:
            path = Path(sdl_path)
            if path.exists():
                sdl_content = path.read_text()
                logger.info("loaded SDL from %s (%d bytes)", sdl_path, len(sdl_content))
            else:
                logger.warning("SDL file not found: %s", sdl_path)

        sid = add_service(name, health_url, sdl_content)
        logger.info("registered service=%s id=%d health_url=%s", name, sid, health_url)

        if current_dseq and current_provider:
            update_service_deployment(sid, current_dseq, current_provider)

        return sid

    async def stop(self) -> None:
        self.running = False
        logger.info("AkashGuard agent stopping")
        await self._cleanup()

    async def _cleanup(self) -> None:
        await self.health_checker.close()
        await self.recovery_engine.close()
        await self.notifier.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    agent = AkashGuardAgent()
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()



