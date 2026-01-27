import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class Run:
    run_id: str
    workflow: str
    input: Dict[str, Any]
    fail_at: Optional[str] = None

RUNS: Dict[str, Run] = {}
EVENT_QUEUES: Dict[str, "asyncio.Queue[dict]"] = {}

def get_queue(run_id: str) -> "asyncio.Queue[dict]":
    q = EVENT_QUEUES.get(run_id)
    if q is None:
        q = asyncio.Queue()
        EVENT_QUEUES[run_id] = q
    return q

async def publish(run_id: str, event: dict) -> None:
    q = get_queue(run_id)
    await q.put(event)
