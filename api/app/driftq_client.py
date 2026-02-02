import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import httpx


def _normalize_base_url(raw: str) -> str:
    u = (raw or "").strip().rstrip("/")
    if not u:
        return "http://127.0.0.1:8080/v1"
    if u.endswith("/v1"):
        return u

    return f"{u}/v1"


class DriftQClient:
    """
    DriftQ-Core HTTP client for driftqd v1 routes.

    IMPORTANT (from DriftQ-Core server types):
    - POST /v1/topics expects {"name": "...", "partitions": N}
    - POST /v1/produce expects {"topic": "...", "value": "<string>", "envelope": {...}}
      where value MUST be a string (often JSON-encoded string)
    - GET /v1/consume requires owner (non-empty)
    - POST /v1/ack and /v1/nack expect {"topic","group","owner","partition","offset"} only
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        env_url = (
            base_url
            or os.getenv("DRIFTQ_HTTP_URL")  # docker-compose in your repo
            or os.getenv("DRIFTQ_URL")       # older name
            or os.getenv("DRIFTQ_BASE_URL")  # optional
        )
        self.base_url = _normalize_base_url(env_url)

    async def healthz(self) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{self.base_url}/healthz")
            r.raise_for_status()
            return r.json()

    async def ensure_topic(self, topic: str, partitions: int = 1) -> None:
        # DriftQ-Core expects "name" (NOT "topic")
        body = {"name": topic, "partitions": partitions}

        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{self.base_url}/topics", json=body)
            if r.status_code in (200, 201, 204, 409):
                return
            r.raise_for_status()

    async def produce(
        self,
        topic: str,
        value: Any,
        *,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        key: Optional[str] = None,
    ) -> None:
        """
        DriftQ-Core ProduceRequest.value is a STRING. If you pass a dict/list/etc, we JSON-encode it into a string
        """
        if isinstance(value, str):
            value_str = value
        else:
            value_str = json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        payload: Dict[str, Any] = {
            "topic": topic,
            "value": value_str,
        }

        envelope: Dict[str, Any] = {}
        if tenant_id is not None:
            envelope["tenant_id"] = tenant_id

        if idempotency_key is not None:
            envelope["idempotency_key"] = idempotency_key

        if envelope:
            payload["envelope"] = envelope

        if key is not None:
            payload["key"] = key

        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{self.base_url}/produce", json=payload)
            if r.status_code in (200, 201, 202, 204):
                return
            # Surface the actual DriftQ-Core error message (it’s usually very specific)
            raise RuntimeError(f"driftq.produce failed: {r.status_code} {r.text}")

    async def consume_stream(
        self,
        *,
        topic: str,
        group: str,
        owner: Optional[str] = None,
        lease_ms: int = 30000,
        timeout_s: float = 60.0
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Yields NDJSON objects from DriftQ /consume.

        DriftQ-Core REQUIRES owner, so we default it if missing.
        We also inject 'owner' into each yielded message so ack/nack can work.
        """
        eff_owner = owner or group or f"owner-{uuid.uuid4().hex[:8]}"

        params = {
            "topic": topic,
            "group": group,
            "owner": eff_owner,
            "lease_ms": str(lease_ms),
        }

        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{self.base_url}/consume", params=params) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if isinstance(msg, dict):
                            # inject owner so ack/nack can use it later
                            msg["owner"] = eff_owner
                            yield msg
                    except Exception:
                        continue

    def extract_value(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        DriftQ-Core consume returns:
          { "topic":..., "partition":..., "offset":..., "envelope": {...}, "value": "<string>" }

        Our app publishes JSON objects encoded into that string, so here we parse msg["value"] if it looks like JSON
        """
        v = msg.get("value")
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    decoded = json.loads(s)
                    return decoded if isinstance(decoded, dict) else {"value": decoded}
                except Exception:
                    return {"value": v}
            return {"value": v}
        return None

    async def ack(self, *, topic: str, group: str, msg: Dict[str, Any]) -> None:
        body = {
            "topic": topic,
            "group": group,
            "owner": msg.get("owner"),
            "partition": msg.get("partition"),
            "offset": msg.get("offset"),
        }

        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{self.base_url}/ack", json=body)
            if r.status_code in (200, 204):
                return

            raise RuntimeError(f"ack failed: {r.status_code} {r.text}")

    async def nack(self, *, topic: str, group: str, msg: Dict[str, Any]) -> None:
        body = {
            "topic": topic,
            "group": group,
            "owner": msg.get("owner"),
            "partition": msg.get("partition"),
            "offset": msg.get("offset"),
        }

        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{self.base_url}/nack", json=body)
            if r.status_code in (200, 204):
                return
            raise RuntimeError(f"nack failed: {r.status_code} {r.text}")
