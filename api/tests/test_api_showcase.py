import asyncio
import json
import time
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.main as main


@pytest.fixture(autouse=True)
def _reset_globals():
    # keep tests isolated
    main.RUNS.clear()
    main.DLQ_CACHE.clear()
    yield
    main.RUNS.clear()
    main.DLQ_CACHE.clear()


class FakeDriftQ:
    def __init__(self, *, dlq_messages=None, events_messages=None):
        self.topics = set()
        self.records = []  # (topic, value, idem_key)
        self.acked = []    # (topic, group, msg)
        self._dlq_queue = list(dlq_messages or [])
        self._events_queue = list(events_messages or [])

    async def ensure_topic(self, topic: str) -> None:
        self.topics.add(topic)

    async def produce(self, topic: str, value: dict, idempotency_key=None) -> None:
        self.records.append((topic, value, idempotency_key))

    async def ack(self, *, topic: str, group: str, msg: dict) -> None:
        self.acked.append((topic, group, msg))

    def extract_value(self, msg: dict):
        return msg.get("value")

    async def consume_stream(self, *, topic: str, group: str, lease_ms: int, timeout_s: float):
        # Route based on topic
        if topic == main.DLQ_TOPIC:
            while self._dlq_queue:
                yield self._dlq_queue.pop(0)
            return

        # events topic: yield provided messages then end
        while self._events_queue:
            yield self._events_queue.pop(0)


def _types_for(fake: FakeDriftQ, topic: str) -> list[str]:
    return [v.get("type") for (t, v, _idem) in fake.records if t == topic]


@pytest.mark.anyio
async def test_create_run_publishes_created_event_and_command(monkeypatch):
    fake = FakeDriftQ()
    monkeypatch.setattr(main, "driftq", fake)

    req = main.RunCreateRequest(workflow="demo", input={"hello": "world"}, fail_at="tool_call")
    resp = await main.create_run(req)

    assert resp.run_id
    events_topic = f"{main.EVENTS_PREFIX}{resp.run_id}"

    # ensure topics created
    assert main.COMMANDS_TOPIC in fake.topics
    assert events_topic in fake.topics

    # should publish run.created + run.command
    ev_types = _types_for(fake, events_topic)
    assert "run.created" in ev_types

    cmd_types = _types_for(fake, main.COMMANDS_TOPIC)
    assert "run.command" in cmd_types

    # command should contain fail_at
    cmd_payloads = [v for (t, v, _idem) in fake.records if t == main.COMMANDS_TOPIC]
    assert cmd_payloads[0]["fail_at"] == "tool_call"


@pytest.mark.anyio
async def test_replay_increments_seq_and_republishes(monkeypatch):
    fake = FakeDriftQ()
    monkeypatch.setattr(main, "driftq", fake)

    run_id = "r1"
    main.RUNS[run_id] = {
        "run_id": run_id,
        "workflow": "demo",
        "fail_at": "transform",
        "replay_seq": 0,
        "created_ms": int(time.time() * 1000)
    }

    out = await main.replay_run(run_id)
    assert out["ok"] is True
    assert out["seq"] == 1
    assert main.RUNS[run_id]["replay_seq"] == 1

    events_topic = f"{main.EVENTS_PREFIX}{run_id}"
    ev_types = _types_for(fake, events_topic)
    assert "run.replay_requested" in ev_types

    cmd_payloads = [v for (t, v, _idem) in fake.records if t == main.COMMANDS_TOPIC]
    assert cmd_payloads[0]["replay_seq"] == 1
    assert cmd_payloads[0]["fail_at"] == "transform"


@pytest.mark.anyio
async def test_get_run_dlq_404_when_missing():
    with pytest.raises(HTTPException) as e:
        await main.get_run_dlq("missing")
    assert e.value.status_code == 404


@pytest.mark.anyio
async def test_get_run_dlq_returns_payload():
    main.DLQ_CACHE["r_ok"] = {"type": "runs.dlq", "run_id": "r_ok"}
    got = await main.get_run_dlq("r_ok")
    assert got["run_id"] == "r_ok"


@pytest.mark.anyio
async def test_sse_emits_dlq_available_if_cached(monkeypatch):
    run_id = "r_dlq"
    main.RUNS[run_id] = {"run_id": run_id, "workflow": "demo", "fail_at": "tool_call", "replay_seq": 0, "created_ms": 0}
    main.DLQ_CACHE[run_id] = {"type": "runs.dlq", "run_id": run_id}

    # consume_stream yields nothing -> response should only contain initial markers
    fake = FakeDriftQ(events_messages=[])
    monkeypatch.setattr(main, "driftq", fake)

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/runs/{run_id}/events",
        "query_string": b"client_id=test",
        "headers": [],
    }
    request = Request(scope)

    resp = await main.stream_run_events(run_id, request)

    chunks: list[bytes] = []
    async for c in resp.body_iterator:
        chunks.append(c if isinstance(c, (bytes, bytearray)) else str(c).encode("utf-8"))

    text = b"".join(chunks).decode("utf-8")
    assert '"type": "sse.connected"' in text
    assert '"type": "dlq.available"' in text


@pytest.mark.anyio
async def test_dlq_indexer_caches_and_acks(monkeypatch):
    run_id = "r_cache"
    payload = {"type": "runs.dlq", "run_id": run_id, "reason": "max_attempts"}

    # dlq_indexer reads msg envelopes; extract_value pulls out .value
    fake = FakeDriftQ(dlq_messages=[{"value": payload}])
    monkeypatch.setattr(main, "driftq", fake)

    await main.dlq_indexer()

    assert run_id in main.DLQ_CACHE
    assert main.DLQ_CACHE[run_id]["reason"] == "max_attempts"
    assert len(fake.acked) == 1
