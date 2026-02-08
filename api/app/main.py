import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .driftq_client import DriftQClient

# ------------------------------------------------------------
# NOTE:
# This repo is intentionally a *tiny* demo to show DriftQ in ~2 minutes.
# DriftQ itself has way more going on (WAL, partitions, leases, metrics,backpressure, idempotency edge cases, observability, etc).
# If you want the "real" implementation details, check out DriftQ-Core. (https://github.com/driftq-org/DriftQ-Core) 🙂
# ------------------------------------------------------------

DLQ_TOPIC = "runs.dlq"

# Demo-only: keep latest DLQ record per run_id in memory
# (This is NOT how you'd build it for real prod usage — it's just to keep the demo snappy.)
DLQ_CACHE: dict[str, dict] = {}
DLQ_CACHE_MAX = 200  # prevent unbounded growth in long dev sessions

_dlq_task: asyncio.Task | None = None


def _is_field_provided(model: BaseModel, field_name: str) -> bool:
    """
    Works for both Pydantic v2 (model_fields_set) and v1 (__fields_set__)
    """
    s = getattr(model, "model_fields_set", None)
    if s is not None:
        return field_name in s

    s = getattr(model, "__fields_set__", None)
    if s is not None:
        return field_name in s

    # If we can't tell, assume provided.
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start/stop background tasks (FastAPI lifespan, replaces deprecated on_event).

    In this demo, we run a tiny DLQ indexer in the background so the UI can instantly
    show "DLQ available" without making you hunt around.

    In DriftQ-Core, this kind of thing is handled in a way more robust way. 🙂
    """
    global _dlq_task
    _dlq_task = asyncio.create_task(dlq_indexer())
    try:
        yield
    finally:
        if _dlq_task:
            _dlq_task.cancel()
            try:
                await _dlq_task
            except asyncio.CancelledError:
                pass
            _dlq_task = None


app = FastAPI(title="driftq-fastapi-nextjs-starter API", lifespan=lifespan)

# Demo CORS: keep it simple for local dev. For real apps you'd lock this down more
cors_origins_env = os.getenv("CORS_ALLOW_ORIGINS", "")
cors_allow_origins = ["http://localhost:3000"]
if cors_origins_env.strip():
    cors_allow_origins.extend([o.strip() for o in cors_origins_env.split(",") if o.strip()])

cors_origin_regex = os.getenv("CORS_ALLOW_ORIGIN_REGEX", r"^https://.*\.(app\.github\.dev|githubpreview\.dev)$")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# This client is the only thing this demo needs: talk to DriftQ over HTTP. DriftQ-Core is doing the heavy lifting behind the scenes
driftq = DriftQClient()

COMMANDS_TOPIC = "runs.commands"
EVENTS_PREFIX = "runs.events."

# tiny in-memory run registry (just for replay counters / metadata)
# again: demo vibes only 😄
RUNS: Dict[str, Dict[str, Any]] = {}


class RunCreateRequest(BaseModel):
    workflow: str = "demo"
    input: Dict[str, Any] = Field(default_factory=dict)
    fail_at: Optional[str] = None  # used only to force the "failure -> DLQ -> replay -> success" story


class RunCreateResponse(BaseModel):
    run_id: str
    events_topic: str


class EmitRequest(BaseModel):
    event: Dict[str, Any]


class ReplayRequest(BaseModel):
    # If omitted => inherit stored fail_at
    # If provided as null => "fix applied" (no fail_at)
    # If provided as a string => override
    #
    # This is the whole point of the demo: replay the same run but "fix applied"
    # by clearing fail_at. DriftQ makes replay workflows easy
    fail_at: Optional[str] = Field(default=None)


@app.get("/healthz")
async def healthz():
    # Quick sanity check that DriftQ is reachable
    try:
        driftq_health = await driftq.healthz()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"driftq unhealthy: {e}")
    return {"ok": True, "driftq": driftq_health}


async def _ensure_topic(topic: str) -> None:
    # For the demo we create topics on-demand
    # In more serious setups you'd probably create + manage topics explicitly
    try:
        await driftq.ensure_topic(topic)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"driftq.ensure_topic({topic}) failed: {e}")


async def _produce(topic: str, value: Dict[str, Any], *, idem_key: Optional[str] = None) -> None:
    # Produce events/commands into DriftQ
    # DriftQ-Core supports idempotency properly — this demo just uses it lightly
    try:
        if idem_key is None:
            await driftq.produce(topic, value)
        else:
            await driftq.produce(topic, value, idempotency_key=idem_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"driftq.produce(topic={topic}) failed: {e}")


@app.post("/runs", response_model=RunCreateResponse)
async def create_run(req: RunCreateRequest):
    # Create a run + its private events topic
    # This pattern is just for demo clarity (each run has its own timeline)
    run_id = uuid.uuid4().hex
    events_topic = f"{EVENTS_PREFIX}{run_id}"

    RUNS[run_id] = {
        "run_id": run_id,
        "workflow": req.workflow,
        "fail_at": req.fail_at,
        "replay_seq": 0,
        "created_ms": int(time.time() * 1000),
    }

    await _ensure_topic(COMMANDS_TOPIC)
    await _ensure_topic(events_topic)

    now_ms = int(time.time() * 1000)

    # Emit run.created on the run's events topic (UI consumes this via SSE)
    await _produce(
        events_topic,
        {"ts": now_ms, "type": "run.created", "run_id": run_id, "workflow": req.workflow},
        idem_key=f"evt:{run_id}:created",
    )

    # Publish the command the worker consumes.
    # Worker does the "workflow", and publishes status/events back to the run topic
    await _produce(
        COMMANDS_TOPIC,
        {
            "ts": now_ms,
            "type": "run.command",
            "run_id": run_id,
            "workflow": req.workflow,
            "input": req.input,
            "fail_at": req.fail_at
        },
        idem_key=f"cmd:{run_id}:0"
    )

    return RunCreateResponse(run_id=run_id, events_topic=events_topic)


@app.post("/runs/{run_id}/replay")
async def replay_run(run_id: str, req: ReplayRequest | None = None):
    """
    Replay is the "money shot" of the demo:
      - First run fails and hits DLQ
      - Then we replay with fail_at cleared (fix applied)
      - Same run_id, new replay_seq, new outcome
    """
    meta = RUNS.get(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="run not found")

    meta["replay_seq"] += 1
    seq = meta["replay_seq"]
    events_topic = f"{EVENTS_PREFIX}{run_id}"

    await _ensure_topic(COMMANDS_TOPIC)
    await _ensure_topic(events_topic)

    # Decide fail_at for replay:
    # - No body => inherit original meta["fail_at"]
    # - Body with fail_at provided (even null) => override
    #
    # That null override is what lets the UI do "Replay (fix applied)" in one click
    fail_at_to_use = meta.get("fail_at")
    if req is not None and _is_field_provided(req, "fail_at"):
        fail_at_to_use = req.fail_at  # may be None (fix applied)

    now_ms = int(time.time() * 1000)

    await _produce(
        events_topic,
        {
            "ts": now_ms,
            "type": "run.replay_requested",
            "run_id": run_id,
            "seq": seq,
            "fail_at": fail_at_to_use
        },
        idem_key=f"evt:{run_id}:replay:{seq}"
    )

    await _produce(
        COMMANDS_TOPIC,
        {
            "ts": now_ms,
            "type": "run.command",
            "run_id": run_id,
            "workflow": meta.get("workflow", "demo"),
            "input": {"replay": True},
            "fail_at": fail_at_to_use,
            "replay_seq": seq
        },
        idem_key=f"cmd:{run_id}:{seq}"
    )

    return {"ok": True, "run_id": run_id, "seq": seq, "fail_at": fail_at_to_use}


@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request):
    """
    SSE stream: UI subscribes to a run's event topic
    DriftQ is doing the streaming + leases + acking — this just forwards events to the browser
    """
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    events_topic = f"{EVENTS_PREFIX}{run_id}"
    client_id = request.query_params.get("client_id") or "default"
    client_id = client_id[:32]
    group = f"web-{run_id}-{client_id}"

    async def event_gen():
        try:
            # Always send a "connected" marker so the UI knows it's live
            yield f"data: {json.dumps({'type': 'sse.connected', 'run_id': run_id})}\n\n"

            # UX trick: if our in-memory DLQ cache already has this run, tell UI right away
            # In DriftQ-Core you'd do richer indexing/queries — this is just fast for demo
            if run_id in DLQ_CACHE:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "ts": int(time.time() * 1000),
                            "type": "dlq.available",
                            "run_id": run_id,
                            "hint": "DLQ record exists for this run. Use /runs/{run_id}/dlq",
                        }
                    )
                    + "\n\n"
                )

            async for msg in driftq.consume_stream(
                topic=events_topic, group=group, lease_ms=30000, timeout_s=60.0
            ):
                if await request.is_disconnected():
                    break

                evt = driftq.extract_value(msg)
                if isinstance(evt, dict):
                    yield f"data: {json.dumps(evt)}\n\n"

                # Ack so the web group doesn't keep re-reading the same messages forever
                # (DriftQ handles the lease ownership rules under the hood.)
                try:
                    await driftq.ack(topic=events_topic, group=group, msg=msg)
                except Exception:
                    pass

        except (asyncio.CancelledError, GeneratorExit):
            return

    resp = StreamingResponse(event_gen(), media_type="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.post("/runs/{run_id}/emit")
async def emit_event(run_id: str, req: EmitRequest):
    # Debug / demo helper: push an event into the run timeline
    # Not part of the "main story", just handy
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    events_topic = f"{EVENTS_PREFIX}{run_id}"
    await _ensure_topic(events_topic)

    await _produce(events_topic, req.event)
    return {"ok": True}


@app.get("/debug/dlq/peek")
async def peek_dlq(limit: int = 5):
    """
    Debug helper: read a few DLQ payloads (not raw message envelopes)
    Real DriftQ usage has more knobs + tooling; this is just for quick local sanity checks
    """
    group = f"debug-dlq-{uuid.uuid4().hex[:8]}"
    items: list[dict] = []
    async for msg in driftq.consume_stream(topic=DLQ_TOPIC, group=group, lease_ms=30000, timeout_s=3.0):
        payload = driftq.extract_value(msg)
        if isinstance(payload, dict):
            items.append(payload)
        if len(items) >= limit:
            break
    return {"count": len(items), "items": items}


@app.get("/runs/{run_id}/dlq")
async def get_run_dlq(run_id: str):
    # UI uses this to fetch the DLQ payload instantly once DLQ is available
    rec = DLQ_CACHE.get(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="No DLQ record for this run_id")
    return rec


def _dlq_cache_put(run_id: str, payload: dict) -> None:
    # Demo cache: latest DLQ per run_id, bounded
    DLQ_CACHE[str(run_id)] = payload
    if len(DLQ_CACHE) > DLQ_CACHE_MAX:
        oldest_key = next(iter(DLQ_CACHE.keys()))
        DLQ_CACHE.pop(oldest_key, None)


async def dlq_indexer() -> None:
    """
    Demo-only: keep the latest DLQ record per run_id in memory so the UI can fetch it instantly.

    DriftQ-Core has a lot more power/features than what we're doing here —
    this is literally just to make the demo feel "product-y" in 2 minutes.
    """
    await driftq.ensure_topic(DLQ_TOPIC)
    group = "demo-api-dlq-indexer"

    try:
        async for msg in driftq.consume_stream(topic=DLQ_TOPIC, group=group, lease_ms=30000, timeout_s=60.0):
            try:
                payload = driftq.extract_value(msg) or {}
                run_id = payload.get("run_id")
                if run_id:
                    _dlq_cache_put(str(run_id), payload)

                await driftq.ack(topic=DLQ_TOPIC, group=group, msg=msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                # We don't want the demo API to crash because of one weird message.
                # In real systems you'd log/metric this properly
                pass
    except asyncio.CancelledError:
        raise
