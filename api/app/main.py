import asyncio
from fastapi import Request
import json
import time
import uuid
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .driftq_client import DriftQClient
from .store import RUNS, Run, get_queue, publish

app = FastAPI(title="driftq-fastapi-nextjs-starter API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

driftq = DriftQClient()

class RunCreateRequest(BaseModel):
    workflow: str = "demo"
    input: Dict[str, Any] = {}
    fail_at: Optional[str] = None

class RunCreateResponse(BaseModel):
    run_id: str

@app.get("/healthz")
async def healthz():
    # If DriftQ is down, we are NOT healthy :)
    try:
        driftq_health = await driftq.healthz()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"driftq unhealthy: {e}")

    return {
        "ok": True,
        "driftq": driftq_health,
    }

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

    return RunCreateResponse(run_id=run_id)

@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    q = get_queue(run_id)

    async def event_gen():
        try:
            # initial connect event
            yield f"data: {json.dumps({'type': 'sse.connected', 'run_id': run_id})}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(evt)}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            return

    resp = StreamingResponse(event_gen(), media_type="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


class EmitRequest(BaseModel):
    event: Dict[str, Any]

@app.post("/runs/{run_id}/emit")
async def emit_event(run_id: str, req: EmitRequest):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")
    await publish(run_id, req.event)
    return {"ok": True}
