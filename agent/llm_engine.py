import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI

import agent.event_bus as bus
from agent.config import settings
from agent.database import record_decision

logger = logging.getLogger("akashguard.llm")

SAFE_FALLBACK: dict[str, Any] = {
    "diagnosis": "Unable to parse LLM response",
    "confidence": 0.0,
    "recommended_action": "wait",
    "reasoning": "LLM response was not valid JSON",
}

SYSTEM_PROMPT = (
    "You are an infrastructure diagnosis agent for Akash Network deployments. "
    "Analyze the health data and respond with ONLY valid JSON, no markdown."
)

VALID_ACTIONS = {"redeploy", "wait", "scale", "none"}


class DiagnosisEngine:

    def __init__(self) -> None:
        # Diagnosis uses Groq (OpenAI-compatible). Set GROQ_API_KEY in .env.
        # Previous AkashML setup (commented):
        # self._client = AsyncOpenAI(
        #     api_key=settings.akashml_api_key,
        #     base_url=settings.akashml_base_url,
        # )
        # self._diagnosis_model = settings.akashml_model
        self._client = AsyncOpenAI(
            # api_key=settings.akashml_api_key,
            # base_url=settings.akashml_base_url,
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
        )
        self._diagnosis_model = settings.groq_model

    def _build_prompt(
        self,
        service_name: str,
        health_status: str,
        recent_checks: list[dict[str, Any]],
    ) -> str:
        total = len(recent_checks)
        failed = sum(1 for c in recent_checks if not c["is_healthy"])
        slow = sum(
            1 for c in recent_checks
            if c["is_healthy"] and c.get("response_time_ms") is not None
            and c["response_time_ms"] > settings.response_time_threshold_ms * 0.8
        )

        parts = [f"failure_count={failed}/{total}"]
        if slow:
            parts.append(f"slow_responses={slow}")
        pattern = ", ".join(parts)

        checks_summary = []
        for c in recent_checks[:10]:
            checks_summary.append({
                "timestamp": c.get("checked_at", ""),
                "status_code": c.get("status_code"),
                "response_time_ms": c.get("response_time_ms"),
                "is_healthy": bool(c.get("is_healthy")),
                "error": c.get("error_message"),
            })

        return (
            f"Service: {service_name}\n"
            f"Current status: {health_status}\n"
            f"Failure pattern: {pattern}\n"
            f"\nRecent health checks (newest first):\n"
            f"{json.dumps(checks_summary, indent=2)}\n"
            f"\nAnalyze this data and respond with ONLY a JSON object:\n"
            f'{{\n'
            f'  "diagnosis": "brief description of what is wrong",\n'
            f'  "confidence": 0.0 to 1.0,\n'
            f'  "recommended_action": "redeploy" | "wait" | "scale" | "none",\n'
            f'  "reasoning": "why this action"\n'
            f'}}'
        )

    async def diagnose(
        self,
        service_id: int,
        service_name: str,
        health_status: str,
        recent_checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            prompt = self._build_prompt(service_name, health_status, recent_checks)
            logger.debug("llm prompt:\n%s", prompt)

            bus.emit("llm_request", {
                "service": service_name,
                "prompt_summary": prompt[:200],
                # "model": settings.akashml_model,
                "model": self._diagnosis_model,
            })

            t0 = time.monotonic()

            completion = await self._client.chat.completions.create(
                # model=settings.akashml_model
                model=self._diagnosis_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=500,
            )

            elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

            raw = completion.choices[0].message.content or ""
            tokens_used = getattr(completion.usage, "total_tokens", None) if completion.usage else None
            logger.debug("llm raw response: %s", raw)

            result = self._parse_response(raw)

            bus.emit("llm_response", {
                "service": service_name,
                "diagnosis": result["diagnosis"],
                "confidence": result["confidence"],
                "recommended_action": result["recommended_action"],
                "reasoning": result["reasoning"],
                # "model": settings.akashml_model,
                "model": self._diagnosis_model,
                "tokens_used": tokens_used,
                "response_time_ms": elapsed_ms,
            })

            logger.info(
                "diagnosis service=%s action=%s confidence=%.2f diagnosis=%s",
                service_name,
                result["recommended_action"],
                result["confidence"],
                result["diagnosis"],
            )

            try:
                decision_id = record_decision(
                    service_id=service_id,
                    trigger=f"health_status:{health_status}",
                    diagnosis=result["diagnosis"],
                    confidence=result["confidence"],
                    action_taken=result["recommended_action"],
                    reasoning=result["reasoning"],
                )
                result["decision_id"] = decision_id
            except Exception as exc:
                logger.error("failed to record decision: %s", exc)

            return result

        except Exception as exc:
            logger.error("diagnose failed for service=%s: %s", service_name, exc)

            bus.emit("llm_response", {
                "service": service_name,
                "diagnosis": SAFE_FALLBACK["diagnosis"],
                "confidence": 0.0,
                "recommended_action": "wait",
                "reasoning": f"LLM call failed: {exc}",
                # "model": settings.akashml_model,
                "model": self._diagnosis_model,
                "tokens_used": None,
                "response_time_ms": None,
                "error": str(exc),
            })

            try:
                record_decision(
                    service_id=service_id,
                    trigger=f"health_status:{health_status}",
                    diagnosis=SAFE_FALLBACK["diagnosis"],
                    confidence=SAFE_FALLBACK["confidence"],
                    action_taken=SAFE_FALLBACK["recommended_action"],
                    reasoning=f"LLM call failed: {exc}",
                )
            except Exception as db_exc:
                logger.error("failed to record fallback decision: %s", db_exc)

            return {**SAFE_FALLBACK}

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {**SAFE_FALLBACK}

        diagnosis = str(data.get("diagnosis", SAFE_FALLBACK["diagnosis"]))

        try:
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        action = str(data.get("recommended_action", "wait")).lower()
        if action not in VALID_ACTIONS:
            action = "wait"

        reasoning = str(data.get("reasoning", ""))

        return {
            "diagnosis": diagnosis,
            "confidence": confidence,
            "recommended_action": action,
            "reasoning": reasoning,
        }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from agent.database import init_db

    async def main() -> None:
        init_db()
        engine = DiagnosisEngine()

        fake_checks = [
            {"checked_at": "2026-02-24T00:01:00Z", "status_code": None, "response_time_ms": None, "is_healthy": 0, "error_message": "ConnectError: Connection refused"},
            {"checked_at": "2026-02-24T00:00:30Z", "status_code": None, "response_time_ms": None, "is_healthy": 0, "error_message": "ConnectError: Connection refused"},
            {"checked_at": "2026-02-24T00:00:00Z", "status_code": 200, "response_time_ms": 150.0, "is_healthy": 1, "error_message": None},
        ]

        print("\n--- Testing LLM diagnosis (service down) ---")
        result = await engine.diagnose(
            service_id=1,
            service_name="test-chatbot",
            health_status="down",
            recent_checks=fake_checks,
        )
        print(json.dumps(result, indent=2))

        print("\n--- Testing _parse_response with bad input ---")
        bad = DiagnosisEngine._parse_response("this is not json")
        print(json.dumps(bad, indent=2))

        print("\n--- Testing _parse_response with markdown fences ---")
        fenced = DiagnosisEngine._parse_response(
            '```json\n{"diagnosis":"dead","confidence":0.95,"recommended_action":"redeploy","reasoning":"gone"}\n```'
        )
        print(json.dumps(fenced, indent=2))

    asyncio.run(main())
