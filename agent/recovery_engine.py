import asyncio
import logging
import time
from typing import Any

import httpx

import agent.event_bus as bus
from agent.config import settings
from agent.database import update_decision_outcome, update_service_deployment

logger = logging.getLogger("akashguard.recovery")


def _fail(error: str, old_dseq: str | None = None) -> dict[str, Any]:
    return {
        "success": False,
        "old_dseq": old_dseq,
        "new_dseq": None,
        "uris": [],
        "provider": None,
        "error": error,
    }


class RecoveryEngine:

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.akash_console_api_base,
            headers={
                "x-api-key": settings.akash_console_api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(120.0),
        )
        self._service_name: str = ""

    async def close(self) -> None:
        await self._client.aclose()

    def _emit_api(self, method: str, endpoint: str, purpose: str) -> None:
        bus.emit("akash_api_call", {
            "method": method,
            "endpoint": endpoint,
            "purpose": purpose,
            "service": self._service_name,
        })

    def _emit_api_resp(self, method: str, endpoint: str, status_code: int, summary: str) -> None:
        bus.emit("akash_api_response", {
            "method": method,
            "endpoint": endpoint,
            "status_code": status_code,
            "summary": summary,
            "service": self._service_name,
        })

    # ------------------------------------------------------------------
    # Low-level API wrappers
    # ------------------------------------------------------------------

    async def get_deployments(self) -> list[dict[str, Any]]:
        try:
            resp = await self._client.get("/deployments")
            logger.debug("GET /deployments status=%s", resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("get_deployments failed: %s", exc)
            return []

    async def get_deployment(self, dseq: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(f"/deployments/{dseq}")
            logger.debug("GET /deployments/%s status=%s", dseq, resp.status_code)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("get_deployment dseq=%s failed: %s", dseq, exc)
            return {}

    async def close_deployment(self, dseq: str) -> bool:
        self._emit_api("DELETE", f"/v1/deployments/{dseq}", f"Closing deployment DSEQ {dseq}")
        try:
            resp = await self._client.delete(f"/deployments/{dseq}")
            logger.debug("DELETE /deployments/%s status=%s body=%s", dseq, resp.status_code, resp.text[:300])
            if resp.status_code >= 400:
                body_text = resp.text[:300]
                # Treat "Deployment closed" as success — it's already closed
                if resp.status_code == 400 and "closed" in body_text.lower():
                    logger.info("close_deployment dseq=%s already closed (killed by user), skipping", dseq)
                    return True
                logger.warning("close_deployment dseq=%s got %s: %s", dseq, resp.status_code, body_text)
                self._emit_api_resp("DELETE", f"/v1/deployments/{dseq}", resp.status_code, f"Failed: {resp.text[:200]}")
                bus.emit("akash_close_old", {"service": self._service_name, "old_dseq": dseq, "status": f"failed: HTTP {resp.status_code}"})
                return False
            self._emit_api_resp("DELETE", f"/v1/deployments/{dseq}", resp.status_code, f"Deployment {dseq} closed")
            bus.emit("akash_close_old", {"service": self._service_name, "old_dseq": dseq, "status": "closed"})
            logger.info("closed deployment dseq=%s", dseq)
            return True
        except Exception as exc:
            self._emit_api_resp("DELETE", f"/v1/deployments/{dseq}", 0, f"Failed: {exc}")
            bus.emit("akash_close_old", {"service": self._service_name, "old_dseq": dseq, "status": f"failed: {exc}"})
            logger.error("close_deployment dseq=%s failed: %s", dseq, exc)
            return False

    async def create_deployment(
        self, sdl: str, deposit: float = 5.0,
    ) -> dict[str, Any] | None:
        self._emit_api("POST", "/v1/deployments", "Creating new deployment on Akash Network")
        try:
            body = {"data": {"sdl": sdl, "deposit": deposit}}
            resp = await self._client.post("/deployments", json=body)
            logger.debug("POST /deployments status=%s", resp.status_code)
            resp.raise_for_status()
            raw = resp.json()
            logger.debug("create_deployment raw response: %s", raw)

            data = raw.get("data", raw)
            dseq = data.get("dseq")
            manifest = data.get("manifest")
            tx_hash = (
                data.get("signTx", {}).get("transactionHash")
                if isinstance(data.get("signTx"), dict)
                else data.get("transactionHash")
            )

            self._emit_api_resp("POST", "/v1/deployments", resp.status_code, f"Created DSEQ: {dseq}")
            bus.emit("akash_create_deploy", {
                "service": self._service_name,
                "new_dseq": str(dseq),
                "transaction_hash": tx_hash,
            })

            logger.info("created deployment dseq=%s tx=%s manifest_len=%s",
                        dseq, tx_hash, len(manifest) if manifest else 0)
            return {"dseq": dseq, "transactionHash": tx_hash, "manifest": manifest}
        except Exception as exc:
            self._emit_api_resp("POST", "/v1/deployments", 0, f"Failed: {exc}")
            logger.error("create_deployment failed: %s", exc)
            return None

    async def get_bids(self, dseq: str) -> list[dict[str, Any]]:
        self._emit_api("GET", f"/v1/bids?dseq={dseq}", "Fetching provider bids")
        try:
            resp = await self._client.get("/bids", params={"dseq": dseq})
            logger.debug("GET /bids?dseq=%s status=%s", dseq, resp.status_code)
            resp.raise_for_status()
            raw = resp.json()
            logger.debug("get_bids raw response: %s", raw)
            bids = raw.get("data", raw)
            if isinstance(bids, dict):
                bids = bids.get("bids", [])
            result = bids if isinstance(bids, list) else []
            self._emit_api_resp("GET", f"/v1/bids?dseq={dseq}", resp.status_code, f"{len(result)} bids received")
            return result
        except Exception as exc:
            self._emit_api_resp("GET", f"/v1/bids?dseq={dseq}", 0, f"Failed: {exc}")
            logger.error("get_bids dseq=%s failed: %s", dseq, exc)
            return []

    async def create_lease(
        self, manifest: str, dseq: str, gseq: int, oseq: int, provider: str,
        certificate: dict[str, str] | None = None,
    ) -> bool:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            self._emit_api("POST", "/v1/leases", f"Creating lease with provider {provider[:20]}... (attempt {attempt}/{max_attempts})")
            try:
                body: dict[str, Any] = {
                    "manifest": manifest,
                    "leases": [{"dseq": dseq, "gseq": gseq, "oseq": oseq, "provider": provider}],
                }
                if certificate:
                    body["certificate"] = certificate
                logger.info("create_lease attempt %d/%d: dseq=%s provider=%s cert=%s",
                            attempt, max_attempts, dseq, provider, bool(certificate))
                resp = await self._client.post("/leases", json=body)
                logger.info("POST /leases status=%s response=%s", resp.status_code, resp.text[:1000])
                if resp.status_code >= 400:
                    logger.error("create_lease attempt %d FAILED: status=%s response=%s",
                                 attempt, resp.status_code, resp.text)
                    if attempt < max_attempts:
                        logger.info("retrying create_lease in 10s...")
                        await asyncio.sleep(10)
                        continue
                    self._emit_api_resp("POST", "/v1/leases", resp.status_code, f"Failed ({resp.status_code}): {resp.text[:300]}")
                    return False
                self._emit_api_resp("POST", "/v1/leases", resp.status_code, f"Lease created DSEQ {dseq}")
                bus.emit("akash_lease_created", {
                    "service": self._service_name,
                    "dseq": dseq,
                    "provider": provider,
                    "gseq": gseq,
                    "oseq": oseq,
                })
                logger.info("lease created dseq=%s provider=%s gseq=%d oseq=%d",
                            dseq, provider, gseq, oseq)
                return True
            except Exception as exc:
                logger.error("create_lease attempt %d exception: %s", attempt, exc)
                if attempt < max_attempts:
                    await asyncio.sleep(10)
                    continue
                self._emit_api_resp("POST", "/v1/leases", 0, f"Failed: {exc}")
                return False
        return False

    async def get_certificate(self) -> dict[str, str] | None:
        try:
            resp = await self._client.get("/certificates")
            logger.debug("GET /certificates status=%s", resp.status_code)
            if resp.status_code >= 400:
                logger.info("no existing certificate, creating one...")
                return await self.create_certificate()
            data = resp.json()
            certs = data if isinstance(data, list) else data.get("data", [])
            if certs and isinstance(certs, list) and len(certs) > 0:
                cert = certs[0]
                cert_pem = cert.get("certPem") or cert.get("cert") or cert.get("certificate", {}).get("cert")
                key_pem = cert.get("keyPem") or cert.get("key") or cert.get("certificate", {}).get("pubkey")
                if cert_pem and key_pem:
                    logger.info("found existing certificate")
                    return {"certPem": cert_pem, "keyPem": key_pem}
            logger.info("no usable certificate found, creating one...")
            return await self.create_certificate()
        except Exception as exc:
            logger.error("get_certificate failed: %s", exc)
            return None

    async def create_certificate(self) -> dict[str, str] | None:
        try:
            resp = await self._client.post("/certificates")
            logger.info("POST /certificates status=%s response=%s", resp.status_code, resp.text[:500])
            if resp.status_code >= 400:
                logger.error("create_certificate failed: status=%s body=%s", resp.status_code, resp.text)
                return None
            data = resp.json()
            cert_data = data.get("data", data) if isinstance(data, dict) else data
            cert_pem = cert_data.get("certPem") or cert_data.get("cert")
            key_pem = (
                cert_data.get("encryptedKey")
                or cert_data.get("keyPem")
                or cert_data.get("key")
            )
            pub_pem = cert_data.get("pubkeyPem") or cert_data.get("pubkey")
            if cert_pem and key_pem:
                logger.info("certificate created successfully")
                result = {"certPem": cert_pem, "keyPem": key_pem}
                if pub_pem:
                    result["pubkeyPem"] = pub_pem
                return result
            logger.warning("create_certificate response missing cert/key. keys found: %s", list(cert_data.keys()))
            return None
        except Exception as exc:
            logger.error("create_certificate failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # High-level recovery
    # ------------------------------------------------------------------

    async def recover_service(
        self,
        service_id: int,
        sdl: str,
        old_dseq: str | None = None,
        decision_id: int | None = None,
        service_name: str = "",
    ) -> dict[str, Any]:
        self._service_name = service_name
        self._t0 = time.monotonic()
        try:
            result = await self._do_recover(service_id, sdl, old_dseq, decision_id)
            result["total_time_seconds"] = round(time.monotonic() - self._t0, 1)
            return result
        except Exception as exc:
            error = f"recovery failed unexpectedly: {exc}"
            logger.error("recover_service service_id=%s: %s", service_id, error)
            r = _fail(error, old_dseq)
            r["total_time_seconds"] = round(time.monotonic() - self._t0, 1)
            return r

    async def _do_recover(
        self,
        service_id: int,
        sdl: str,
        old_dseq: str | None,
        decision_id: int | None,
    ) -> dict[str, Any]:
        name = self._service_name

        # Step 1: close old deployment (best effort)
        if old_dseq:
            bus.emit("recovery_progress", {"service": name, "step": "close_old", "detail": f"Closing old deployment DSEQ {old_dseq}..."})
            logger.info("closing old deployment dseq=%s", old_dseq)
            await self.close_deployment(old_dseq)

        # Step 2: create new deployment
        bus.emit("recovery_progress", {"service": name, "step": "create_deploy", "detail": "Creating new deployment on Akash Network..."})
        logger.info("creating new deployment for service_id=%s", service_id)
        deploy_result = await self.create_deployment(sdl)
        if not deploy_result or not deploy_result.get("dseq"):
            error = "create_deployment returned no dseq"
            logger.error(error)
            return _fail(error, old_dseq)

        new_dseq = str(deploy_result["dseq"])
        manifest = deploy_result.get("manifest")
        logger.info("new deployment created dseq=%s", new_dseq)

        if not manifest:
            error = f"create_deployment returned no manifest for dseq={new_dseq}"
            logger.error(error)
            return _fail(error, old_dseq)

        # Step 3: wait for bids
        bus.emit("recovery_progress", {"service": name, "step": "waiting_bids", "detail": f"Waiting 30s for provider bids on DSEQ {new_dseq}..."})
        logger.info("waiting 30s for bids on dseq=%s", new_dseq)
        await asyncio.sleep(30)

        bids = await self.get_bids(new_dseq)
        open_bids = [b for b in bids if self._bid_is_open(b)]

        if not open_bids:
            error = f"no open bids received for dseq={new_dseq} (total bids: {len(bids)})"
            logger.error(error)
            return _fail(error, old_dseq)

        logger.info("received %d open bids for dseq=%s", len(open_bids), new_dseq)

        # Emit bids summary
        bids_summary = []
        for b in open_bids[:10]:
            bid_data = b.get("bid", b)
            bid_id = bid_data.get("id", {})
            price = bid_data.get("price", {})
            bids_summary.append({
                "provider": bid_id.get("provider", "unknown"),
                "price": price.get("amount", "?"),
                "denom": price.get("denom", "uakt"),
            })

        bus.emit("akash_bids_received", {
            "service": name,
            "dseq": new_dseq,
            "num_bids": len(open_bids),
            "bids_summary": bids_summary,
        })

        # Step 4: accept cheapest open bid (sort by price ascending)
        def _bid_price_float(b: dict) -> float:
            try:
                return float(b.get("bid", b).get("price", {}).get("amount", "999999"))
            except (ValueError, TypeError):
                return 999999.0

        open_bids.sort(key=_bid_price_float)
        bid = open_bids[0]
        bid_id = bid.get("bid", {}).get("id", bid.get("id", {}))
        provider = bid_id.get("provider", "")
        gseq = int(bid_id.get("gseq", 1))
        oseq = int(bid_id.get("oseq", 1))

        bid_price_data = bid.get("bid", bid).get("price", {})
        bid_price_amount = bid_price_data.get("amount", "?")
        bid_price_denom = bid_price_data.get("denom", "uakt")
        bus.emit("akash_bid_selected", {
            "service": name,
            "dseq": new_dseq,
            "provider": provider,
            "price": bid_price_amount,
            "denom": bid_price_denom,
        })

        # Fetch certificate for lease creation
        bus.emit("recovery_progress", {"service": name, "step": "get_cert", "detail": "Fetching deployment certificate..."})
        certificate = await self.get_certificate()
        if certificate:
            logger.info("certificate ready for lease creation")
        else:
            logger.warning("no certificate available, proceeding without it")

        bus.emit("recovery_progress", {"service": name, "step": "accept_bid", "detail": f"Accepting bid from {provider[:20]}..."})
        logger.info("accepting bid from provider=%s gseq=%d oseq=%d", provider, gseq, oseq)
        lease_ok = await self.create_lease(manifest, new_dseq, gseq, oseq, provider, certificate=certificate)
        if not lease_ok:
            # Clean up orphaned deployment to avoid wasting funds
            logger.info("cleaning up orphaned deployment dseq=%s after lease failure", new_dseq)
            await self.close_deployment(new_dseq)
            error = f"create_lease failed for dseq={new_dseq} provider={provider}"
            logger.error(error)
            return _fail(error, old_dseq)

        # Step 5: poll for URIs
        bus.emit("recovery_progress", {"service": name, "step": "poll_uris", "detail": "Polling for service URIs..."})
        uris: list[str] = []
        max_wait = 90
        poll_interval = 10
        elapsed = 0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            self._emit_api("GET", f"/v1/deployments/{new_dseq}", f"Polling for URIs ({elapsed}s)")
            detail = await self.get_deployment(new_dseq)
            if not detail:
                self._emit_api_resp("GET", f"/v1/deployments/{new_dseq}", 0, "Empty response")
                bus.emit("recovery_progress", {"service": name, "step": "poll_uris", "detail": f"No URIs yet... ({elapsed}s elapsed)"})
                continue

            self._emit_api_resp("GET", f"/v1/deployments/{new_dseq}", 200, f"Checking URIs ({elapsed}s)")

            uris, found_provider = self._extract_uris(detail)
            if found_provider:
                provider = found_provider
            if uris:
                bus.emit("akash_uri_ready", {
                    "service": name,
                    "dseq": new_dseq,
                    "uris": uris,
                })
                logger.info(
                    "service live dseq=%s provider=%s uris=%s (elapsed=%ds)",
                    new_dseq, provider, uris, elapsed,
                )
                break

            bus.emit("recovery_progress", {"service": name, "step": "poll_uris", "detail": f"No URIs yet... ({elapsed}s elapsed)"})
            logger.debug("poll dseq=%s: no URIs yet (elapsed=%ds)", new_dseq, elapsed)

        if not uris:
            error = f"service not live after {max_wait}s for dseq={new_dseq}"
            logger.error(error)
            return _fail(error, old_dseq)

        # Step 6: update DB
        bus.emit("recovery_progress", {"service": name, "step": "update_db", "detail": "Updating database with new deployment info..."})
        try:
            new_health_url = f"http://{uris[0]}/health" if uris else None
            update_service_deployment(service_id, new_dseq, provider, new_health_url)
            if decision_id:
                update_decision_outcome(
                    decision_id,
                    outcome="success",
                    new_dseq=new_dseq,
                    new_provider=provider,
                    downtime_seconds=round(time.monotonic() - self._t0, 1),
                )
        except Exception as exc:
            logger.error("failed to update DB after recovery: %s", exc)

        result = {
            "success": True,
            "old_dseq": old_dseq,
            "new_dseq": new_dseq,
            "uris": uris,
            "provider": provider,
            "bid_price": bid_price_amount,
            "bid_denom": bid_price_denom,
            "gseq": gseq,
            "oseq": oseq,
            "error": None,
        }
        logger.info("recovery complete: %s", result)
        return result

    @staticmethod
    def _bid_is_open(bid: dict[str, Any]) -> bool:
        state = (
            bid.get("bid", {}).get("state")
            or bid.get("state")
            or ""
        )
        return str(state).lower() == "open"

    @staticmethod
    def _extract_uris(detail: dict[str, Any]) -> tuple[list[str], str | None]:
        try:
            data = detail.get("data", detail)
            leases = data.get("leases", [])
            if not leases:
                return [], None

            lease = leases[0]
            provider = (
                lease.get("provider")
                or lease.get("providerAddress")
                or lease.get("status", {}).get("provider")
            )

            services = lease.get("status", {}).get("services", {})
            for svc_info in services.values():
                uris = svc_info.get("uris", [])
                if uris:
                    return uris, provider

            uris = lease.get("uris", [])
            if uris:
                return uris, provider

            return [], provider
        except Exception:
            return [], None
