import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .driftq_client import DriftQClient

app = FastAPI(title="driftq-fastapi-nextjs-starter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

driftq = DriftQClient()

COMMANDS_TOPIC = "runs.commands"
EVENTS_PREFIX = "runs.events."

# tiny in-memory run registry (just for replay counters / metadata)
RUNS: Dict[str, Dict[str, Any]] = {}


class RunCreateRequest(BaseModel):
    workflow: str = "demo"
    input: Dict[str, Any] = Field(default_factory=dict)
    fail_at: Optional[str] = None


class RunCreateResponse(BaseModel):
    run_id: str
    events_topic: str


class EmitRequest(BaseModel):
    event: Dict[str, Any]


@app.get("/healthz")
async def healthz():
    try:
        driftq_health = await driftq.healthz()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"driftq unhealthy: {e}")
    return {"ok": True, "driftq": driftq_health}


async def _ensure_topic(topic: str) -> None:
    try:
        await driftq.ensure_topic(topic)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"driftq.ensure_topic({topic}) failed: {e}")


async def _produce(topic: str, value: Dict[str, Any], *, idem_key: Optional[str] = None) -> None:
    try:
        if idem_key is None:
            await driftq.produce(topic, value)
        else:
            await driftq.produce(topic, value, idempotency_key=idem_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"driftq.produce(topic={topic}) failed: {e}")


@app.post("/runs", response_model=RunCreateResponse)
async def create_run(req: RunCreateRequest):
    # print("CREATE_RUN req.workflow=", req.workflow, "req.fail_at=", req.fail_at)
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

    await _produce(
        events_topic,
        {"ts": now_ms, "type": "run.created", "run_id": run_id, "workflow": req.workflow},
        idem_key=f"evt:{run_id}:created",
    )

    # publish the command that the worker will execute
    await _produce(
        COMMANDS_TOPIC,
        {
            "ts": now_ms,
            "type": "run.command",
            "run_id": run_id,
            "workflow": req.workflow,
            "input": req.input,
            "fail_at": req.fail_at,
        },
        idem_key=f"cmd:{run_id}:0",
    )

    return RunCreateResponse(run_id=run_id, events_topic=events_topic)


@app.post("/runs/{run_id}/replay")
async def replay_run(run_id: str):
    meta = RUNS.get(run_id)
    if not meta:
        raise HTTPException(status_code=404, detail="run not found")

    meta["replay_seq"] += 1
    seq = meta["replay_seq"]
    events_topic = f"{EVENTS_PREFIX}{run_id}"

    await _ensure_topic(COMMANDS_TOPIC)
    await _ensure_topic(events_topic)

    now_ms = int(time.time() * 1000)

    await _produce(
        events_topic,
        {"ts": now_ms, "type": "run.replay_requested", "run_id": run_id, "seq": seq},
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
            "fail_at": meta.get("fail_at"),
            "replay_seq": seq,
        },
        idem_key=f"cmd:{run_id}:{seq}",
    )

    return {"ok": True, "run_id": run_id, "seq": seq}


@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    events_topic = f"{EVENTS_PREFIX}{run_id}"
    client_id = request.query_params.get("client_id") or "default"
    client_id = client_id[:32]  # keep it short/safe
    group = f"web-{run_id}-{client_id}"

    async def event_gen():
        try:
            yield f"data: {json.dumps({'type': 'sse.connected', 'run_id': run_id})}\n\n"

            async for msg in driftq.consume_stream(
                topic=events_topic, group=group, lease_ms=30000, timeout_s=60.0
            ):
                if await request.is_disconnected():
                    break

                evt = driftq.extract_value(msg)
                if isinstance(evt, dict):
                    yield f"data: {json.dumps(evt)}\n\n"

                try:
                    await driftq.ack(topic=events_topic, group=group, msg=msg)
                except Exception:
                    pass

        except (asyncio.CancelledError, GeneratorExit):
            return

    resp = StreamingResponse(event_gen(), media_type="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.post("/runs/{run_id}/emit")
async def emit_event(run_id: str, req: EmitRequest):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    events_topic = f"{EVENTS_PREFIX}{run_id}"
    await _ensure_topic(events_topic)

    await _produce(events_topic, req.event)
    return {"ok": True}

@app.get("/debug/dlq/peek")
async def peek_dlq(limit: int = 5):
    group = f"debug-dlq-{uuid.uuid4().hex[:8]}"
    items = []
    async for msg in driftq.consume_stream(topic="runs.dlq", group=group, lease_ms=30000, timeout_s=3.0):
        items.append(msg)
        if len(items) >= limit:
            break
    return {"count": len(items), "items": items}
