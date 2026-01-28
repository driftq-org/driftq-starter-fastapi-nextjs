import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .driftq_client import DriftQClient
from .store import RUNS, Run, get_queue, publish

app = FastAPI(title="driftq-fastapi-nextjs-starter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

driftq = DriftQClient()
StepName = Literal["fetch_input", "transform", "tool_call", "finalize"]


class RunCreateRequest(BaseModel):
    workflow: str = "demo"
    input: Dict[str, Any] = Field(default_factory=dict)
    fail_at: Optional[StepName] = None


class RunCreateResponse(BaseModel):
    run_id: str


@app.get("/healthz")
async def healthz():
    # If DriftQ is down, we are NOT healthy :)
    try:
        driftq_health = await driftq.healthz()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"driftq unhealthy: {e}")

    return {"ok": True, "driftq": driftq_health}


async def run_workflow(run_id: str) -> None:
    """
    Simple demo workflow runner.
    Emits events the UI can stream via SSE.
    """
    run = RUNS.get(run_id)
    if not run:
        return

    now_ms = lambda: int(time.time() * 1000)

    await publish(run_id, {"ts": now_ms(), "type": "run.started", "run_id": run_id, "workflow": run.workflow})

    steps: list[StepName] = ["fetch_input", "transform", "tool_call", "finalize"]

    for step in steps:
        await publish(run_id, {"ts": now_ms(), "type": "step.started", "run_id": run_id, "step": step})
        await asyncio.sleep(0.6)

        if run.fail_at == step:
            await publish(
                run_id,
                {
                    "ts": now_ms(),
                    "type": "step.failed",
                    "run_id": run_id,
                    "step": step,
                    "error": f"forced failure at {step}",
                },
            )
            await publish(run_id, {"ts": now_ms(), "type": "run.failed", "run_id": run_id})
            return

        await publish(run_id, {"ts": now_ms(), "type": "step.completed", "run_id": run_id, "step": step})

    await publish(run_id, {"ts": now_ms(), "type": "run.succeeded", "run_id": run_id})


@app.post("/runs", response_model=RunCreateResponse)
async def create_run(req: RunCreateRequest):
    run_id = uuid.uuid4().hex

    RUNS[run_id] = Run(
        run_id=run_id,
        workflow=req.workflow,
        input=req.input,
        fail_at=req.fail_at,
    )

    now_ms = int(time.time() * 1000)
    await publish(run_id, {"ts": now_ms, "type": "run.created", "run_id": run_id, "workflow": req.workflow})

    # kick off the demo workflow runner
    asyncio.create_task(run_workflow(run_id))

    return RunCreateResponse(run_id=run_id)


@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    q = get_queue(run_id)

    async def event_gen():
        try:
            yield f"data: {json.dumps({'type': 'sse.connected', 'run_id': run_id})}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # keep-alive comment for proxies/browsers
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(evt)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            return

    resp = StreamingResponse(event_gen(), media_type="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


class EmitRequest(BaseModel):
    event: Dict[str, Any] = Field(default_factory=dict)


@app.post("/runs/{run_id}/emit")
async def emit_event(run_id: str, req: EmitRequest):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")
    await publish(run_id, req.event)
    return {"ok": True}
