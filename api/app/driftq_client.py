import os
import httpx

class DriftQClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("DRIFTQ_HTTP_URL", "http://driftq:8080").rstrip("/")

    async def healthz(self) -> dict:
        url = f"{self.base_url}/v1/healthz"
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
