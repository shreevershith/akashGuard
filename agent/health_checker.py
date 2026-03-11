import asyncio
import logging
import time
from typing import Any

import httpx

from agent.config import settings
from agent.database import (
    get_monitored_services,
    get_recent_health_checks,
    record_health_check,
    update_service_status,
)

logger = logging.getLogger("akashguard.health")


class HealthChecker:

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            verify=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def check_service(self, service: dict[str, Any]) -> dict[str, Any]:
        sid = service["id"]
        name = service["name"]
        url = service["health_url"]

        status_code: int | None = None
        response_time_ms: float | None = None
        is_healthy = False
        error_message: str | None = None

        try:
            start = time.monotonic()
            resp = await self._client.get(url)
            response_time_ms = round((time.monotonic() - start) * 1000, 2)
            status_code = resp.status_code

            is_healthy = (
                status_code == 200
                and response_time_ms < settings.response_time_threshold_ms
            )

            logger.info(
                "health_check service=%s status=%s time_ms=%.1f healthy=%s",
                name, status_code, response_time_ms, is_healthy,
            )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "health_check service=%s error=%s", name, error_message,
            )

        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            logger.error(
                "health_check service=%s unexpected_error=%s", name, error_message,
            )

        try:
            record_health_check(
                service_id=sid,
                status_code=status_code,
                response_time_ms=response_time_ms,
                is_healthy=is_healthy,
                error_message=error_message,
            )
        except Exception as exc:
            logger.error("failed to record health check for service=%s: %s", name, exc)

        return {
            "service_id": sid,
            "service_name": name,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
            "is_healthy": is_healthy,
            "error_message": error_message,
        }

    async def check_all_services(self) -> list[dict[str, Any]]:
        try:
            services = get_monitored_services()
        except Exception as exc:
            logger.error("failed to load services: %s", exc)
            return []

        if not services:
            logger.info("no services registered, skipping health checks")
            return []

        tasks = [self.check_service(svc) for svc in services]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        checked: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, Exception):
                logger.error("gather exception: %s", r)
            else:
                checked.append(r)
        return checked

    def evaluate_service_health(self, service_id: int) -> tuple[str, list[dict[str, Any]]]:
        try:
            recent = get_recent_health_checks(service_id, limit=settings.failure_threshold)

            if not recent:
                return "unknown", []

            failed = sum(1 for c in recent if not c["is_healthy"])

            if failed == len(recent) and len(recent) >= settings.failure_threshold:
                status = "down"
            elif failed > 0:
                status = "degraded"
            else:
                status = "healthy"

            try:
                update_service_status(service_id, status)
            except Exception as exc:
                logger.error("failed to update service %s status: %s", service_id, exc)

            logger.info(
                "evaluate service_id=%s status=%s (failed=%d/%d)",
                service_id, status, failed, len(recent),
            )
            return status, recent

        except Exception as exc:
            logger.error("evaluate_service_health failed for service_id=%s: %s", service_id, exc)
            return "unknown", []


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    from agent.database import add_service, get_all_services, get_service, init_db

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def main() -> None:
        init_db()

        # Register chatbot (ignore duplicate)
        try:
            add_service("chatbot", "http://localhost:5000/health")
        except Exception:
            pass

        services = get_all_services()
        if not services:
            print("No services registered")
            sys.exit(1)

        checker = HealthChecker()
        try:
            print("\n--- Checking all services ---")
            results = await checker.check_all_services()
            for r in results:
                print(f"  {r['service_name']}: healthy={r['is_healthy']} "
                      f"status={r['status_code']} time={r['response_time_ms']}ms")

            print("\n--- Evaluating health ---")
            for svc in services:
                status, checks = checker.evaluate_service_health(svc["id"])
                print(f"  {svc['name']}: {status} ({len(checks)} recent checks)")

            svc = get_service(services[0]["id"])
            print(f"\n  DB status for {svc['name']}: {svc['status']}")
        finally:
            await checker.close()

    asyncio.run(main())


