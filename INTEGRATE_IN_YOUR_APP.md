# Integrate DriftQ into *your* Next.js + FastAPI app (practical playbook) 🧩

This is the **minimum pieces** you bolt on.

> Heads up: this starter shows a tiny slice of what DriftQ can do.
> The "real engine + latest changes" live here: DriftQ‑Core → https://github.com/driftq-org/DriftQ-Core
> (This starter might not change every week, DriftQ‑Core does.)

---

## 0) The mental model (don’t overthink it)

You’re adding:
- **one extra service**: DriftQ (broker + durable topics)
- **one extra process**: worker(s) (your executors)

```
Next.js UI  --->  FastAPI API  --->  DriftQ topics
   |                 |                 |
   |  SSE events <---|<--- worker <----|
```

### In plain English
- **FastAPI**: "start a run" + "publish work"
- **DriftQ**: "store work durably" + "let workers consume it reliably"
- **Worker**: "do the actual work"
- **UI**: "show the timeline live + DLQ payload + replay button"

### What DriftQ is doing for you
- **Queue + durability**: work isn’t "best effort" or stuck inside your API process
- **Retries**: workers can retry in a consistent way (with max attempts + backoff)
- **DLQ**: failures don’t disappear into logs — payload + error get persisted
- **Replay**: re-run the same job after you fix the cause

That’s the whole "DLQ → Replay → Success" story.

---

## 1) Where DriftQ fits in a real app

Most Next.js + FastAPI apps hit the same wall:
- LLM calls are slow/flaky (timeouts, rate limits, weird outputs)
- external integrations fail (Slack/Jira/GitHub/Stripe/etc.)
- you need retries, but you don’t want your API to turn into spaghetti
- you need auditability: "what happened to run X?"
- you need replay: "we fixed the bug / prompt / tool config — re-run it"

**DriftQ becomes your durable async lane:**
- API stays responsive
- Work happens in worker(s)
- UI gets a live timeline via SSE
- Failures become DLQ you can inspect + replay later

---

## 2) The integration checklist ✅

You add 4 things:

1) **Run DriftQ** (Docker Compose / k8s / whatever)
2) **Add a tiny DriftQ client wrapper** in your FastAPI backend
3) **Add a few endpoints** (create run, SSE events, DLQ, replay, health)
4) **Add a worker process** that consumes commands and emits events

Everything else is wiring.

---

## 3) Run DriftQ locally

### Option A: easiest (use the starter’s one-command scripts)
If you cloned this starter and just want "everything up":

From repo root:
```bash
python api/scripts/dev_up.py
```

Bring it down:
```bash
python api/scripts/dev_down.py
```

Wipe everything (including WAL volume):
```bash
python api/scripts/dev_down.py --wipe --prune-images
```

✅ This is OS-friendly (Windows/macOS/Linux) because it’s just Python calling Docker Compose.

---

### Option B: add DriftQ to *your* existing `docker-compose.yml`
Paste this into your compose:

```yaml
services:
  driftq:
    image: ghcr.io/driftq-org/driftqd:latest
    ports:
      - "8080:8080"
    volumes:
      - driftq_data:/data

volumes:
  driftq_data:
```

DriftQ health:
- ✅ `http://localhost:8080/v1/healthz`
- ❌ `http://localhost:8080/healthz` (driftqd blocks unversioned routes)

---

## 4) Add a DriftQ client wrapper in your FastAPI backend

Put it wherever you keep infra clients. Common spots:
- `backend/services/driftq_client.py`
- `backend/storage/driftq_client.py`
- `app/services/driftq_client.py`

Skeleton (good enough to ship; tweak as needed):

```py
import os
from typing import Any, AsyncIterator, Dict, Optional
import httpx

class DriftQClient:
    def __init__(self) -> None:
        self.base = os.getenv("DRIFTQ_HTTP_URL", "http://localhost:8080").rstrip("/")
        self._http = httpx.AsyncClient(timeout=10)

    async def healthz(self) -> Dict[str, Any]:
        r = await self._http.get(f"{self.base}/v1/healthz")
        r.raise_for_status()
        return r.json()

    async def ensure_topic(self, topic: str) -> None:
        r = await self._http.post(f"{self.base}/v1/topics", json={"name": topic})
        # 409 = already exists (fine)
        if r.status_code not in (200, 201, 409):
            r.raise_for_status()

    async def produce(self, topic: str, value: Dict[str, Any], idempotency_key: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"topic": topic, "value": value}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        r = await self._http.post(f"{self.base}/v1/produce", json=payload)
        r.raise_for_status()

    async def consume_stream(
        self,
        *,
        topic: str,
        group: str,
        lease_ms: int = 30_000,
        timeout_s: float = 60.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        while True:
            r = await self._http.get(
                f"{self.base}/v1/consume",
                params={"topic": topic, "group": group, "lease_ms": lease_ms},
                timeout=timeout_s,
            )
            r.raise_for_status()
            msg = r.json()
            if not msg:
                continue
            yield msg

    async def ack(self, *, topic: str, group: str, msg: Dict[str, Any]) -> None:
        r = await self._http.post(f"{self.base}/v1/ack", json={"topic": topic, "group": group, "msg": msg})
        if r.status_code not in (200, 204, 409):
            r.raise_for_status()

    def extract_value(self, msg: Dict[str, Any]) -> Any:
        return msg.get("value")
```

---

## 5) Add the "DriftQ integration endpoints" in your FastAPI app

You want a thin layer that:
- creates a run (produces a command)
- streams run events (SSE)
- returns DLQ payload (highly recommended)
- replays a run (produces command again)
- health endpoint (sanity check)

Minimum endpoints:
- `POST /runs` → create run + publish `run.command` to DriftQ
- `GET /runs/{run_id}/events` → SSE stream from `runs.events.{run_id}`
- `GET /runs/{run_id}/dlq` → fetch DLQ payload for that run
- `POST /runs/{run_id}/replay` → publish replay command
- `GET /healthz` → check DriftQ is reachable

**Real app upgrades you’ll probably add:**
- auth + tenant scoping
- store run metadata in your DB (instead of in-memory dict)
- DLQ viewer UI (and/or mirror DLQ records into DB)

---

## 6) Add a worker process (this is where the "real work" happens)

Your worker is a normal long-running process/container that:
- consumes `runs.commands`
- runs your workflow logic
- emits events to `runs.events.{run_id}`
- retries failures
- writes to DLQ when attempts are exhausted

### "Worker" can mean more than you think
In real life, "worker" usually includes:
- calling an LLM (OpenAI/Anthropic/local)
- tool calls (Slack/Jira/GitHub/etc.)
- DB ops / ETL / ingestion / embeddings
- multi-step pipelines

Basically: **anything you don’t want running inside an HTTP request**.

---

## 7) Where do I put this in *my* repo?

Here’s a clean mapping that won’t wreck your structure:

### Backend (FastAPI)
Good places:
- `backend/services/driftq_client.py` (client wrapper)
- `backend/routes/runs.py` (run endpoints)
- `backend/workflows/` (workflow handlers)
- `backend/workers/driftq_worker.py` (worker entrypoint)

Example structure:
```
backend/
  routes/
    runs.py
  services/
    driftq_client.py
  workflows/
    llm_agent_run.py
    ingestion_pipeline.py
  workers/
    driftq_worker.py
frontend/
  (your Next.js app)
docker-compose.yml
```

### Frontend (Next.js)
You’re just adding:
- "Start run" button
- SSE timeline component
- DLQ inspector + replay button

---

## 8) The "fail_at" modes (from the demo UI)

The demo UI has 3 options:
- **Fail: none**
  Normal path. Worker should succeed (no forced failures). Use this to see the happy timeline.

- **Fail: transform**
  Simulates failing during a "transform step" (think: parse/validate/format the input or intermediate data).
  In an LLM app this could represent: "the model output was junk and my parser choked".

- **Fail: tool_call**
  Simulates failing during an external/tool call (Slack/Jira/HTTP API/LLM request).
  In an LLM app this could represent: rate limit, timeout, 500, network blip, etc.

These are demo-only knobs. In a real app, you wouldn’t "force fail" — you’d fail because reality is messy

---

## 9) Retries / max_attempts — should you expose it in the UI?

For a starter/demo: **I’d keep it simple**.
- Exposing `max_attempts` in the UI is cool, but it also invites "too many knobs" early.
- Better: keep defaults in the worker (or config), and mention in docs "you can tune this".

If you *do* add it, keep it as:
- an "Advanced" collapsible section
- a small dropdown (3 / 5 / 8) not a free-form number
- include a short warning ("don’t set this to 999 unless you like pain")

---

## 10) Next.js integration (how the UI feels alive)

UI flow:
1) call `POST /runs` → get `run_id`
2) open `EventSource(/runs/{run_id}/events)` → stream timeline events
3) if DLQ event shows up → fetch `/runs/{run_id}/dlq`
4) replay button hits `/runs/{run_id}/replay`

Minimal SSE snippet:
```ts
const es = new EventSource(`${API_URL}/runs/${runId}/events?client_id=${crypto.randomUUID()}`)
es.onmessage = (msg) => setTimeline((t) => [...t, JSON.parse(msg.data)])
es.onerror = () => setUiError("SSE dropped (is API up?)")
```

Important detail: use a **unique consumer group per browser connection** (so tabs don’t steal messages).

---

## 11) Production-ish notes (aka the stuff engineers will ask immediately)

- Don’t keep run metadata in memory → store runs in DB
- Don’t keep DLQ cache in memory → build a DLQ viewer (or mirror to DB)
- Use idempotency keys when producing (avoid duplicates on retries)
- Scope access to `/runs/{run_id}/*` (auth/tenant)
- Add metrics/logging (DriftQ‑Core already cares about this; your app should too)

---

## 12) The shortest "add this to my repo" plan (60 minutes)

1) Add DriftQ to compose (`driftq` service + volume)
2) Add env var: `DRIFTQ_HTTP_URL=http://driftq:8080` (in Docker) / `http://localhost:8080` (local)
3) Add `driftq_client.py` wrapper
4) Add endpoints:
   - `POST /runs`
   - `GET /runs/{run_id}/events` (SSE)
   - `POST /runs/{run_id}/replay`
   - `GET /runs/{run_id}/dlq` (recommended)
5) Add worker entrypoint (runs as its own process)
6) Add a tiny UI timeline (SSE + DLQ + replay)

That’s enough for a team to go "ok I get it" and start wiring real workflows.

---

## 13) Want the full DriftQ feature set?

This starter is intentionally tiny. DriftQ‑Core is where the bigger stuff lives:
- WAL persistence
- partitions
- leases/ownership semantics
- idempotency edge cases
- backpressure + overload behavior
- observability/metrics roadmap
- and more

DriftQ‑Core: https://github.com/driftq-org/DriftQ-Core
