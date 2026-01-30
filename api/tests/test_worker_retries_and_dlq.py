import asyncio
import pytest
import app.worker as worker


@pytest.fixture(autouse=True)
def _clear_worker_attempts():
    worker.ATTEMPTS.clear()

class FakeDriftQ:
    def __init__(self, messages):
        self._queue = list(messages)
        self.records = []
        self.acked = []
        self.nacked = []
        self.topics = set()
        self.consume_calls = []

    async def ensure_topic(self, topic: str) -> None:
        self.topics.add(topic)

    async def produce(self, topic: str, value: dict, idempotency_key=None) -> None:
        self.records.append((topic, value, idempotency_key))

    async def ack(self, *, topic: str, group: str, msg: dict) -> None:
        self.acked.append((topic, group, msg))

    async def nack(self, *, topic: str, group: str, msg: dict) -> None:
        self.nacked.append((topic, group, msg))
        self._queue.append(msg)  # redeliver

    def extract_value(self, msg: dict):
        return msg.get("value")

    async def consume_stream(self, *, topic: str, group: str, lease_ms: int, timeout_s: float):
        # record + validate what worker asked for
        self.consume_calls.append(
            {"topic": topic, "group": group, "lease_ms": lease_ms, "timeout_s": timeout_s}
        )

        assert topic == worker.COMMANDS_TOPIC
        assert group == "demo-worker"

        while self._queue:
            yield self._queue.pop(0)


def types_for(fake: FakeDriftQ, topic: str) -> list[str]:
    return [v["type"] for (t, v, _idem) in fake.records if t == topic]


@pytest.mark.anyio
async def test_worker_retries_then_dlq(monkeypatch):
    run_id = "r_dlq"
    events_topic = f"{worker.EVENTS_PREFIX}{run_id}"

    # one command that will fail at tool_call
    msg = {
        "value": {
            "run_id": run_id,
            "workflow": "demo",
            "fail_at": "tool_call",
            "replay_seq": 0
        }
    }

    fake = FakeDriftQ([msg])
    monkeypatch.setattr(worker, "driftq", fake)

    async def no_sleep(_):
        return

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    # Run one main loop pass (it will exit when consume_stream ends)
    await worker.main()

    assert len(fake.nacked) == worker.MAX_ATTEMPTS - 1
    assert len(fake.acked) == 1

    dlq_types = types_for(fake, worker.DLQ_TOPIC)
    assert "runs.dlq" in dlq_types

    ev_types = types_for(fake, events_topic)
    assert "worker.received" in ev_types
    assert ev_types.count("run.attempt_failed") == worker.MAX_ATTEMPTS
    assert ev_types.count("run.retry_scheduled") == worker.MAX_ATTEMPTS - 1
    assert "run.dlq" in ev_types
    assert "run.failed" in ev_types


@pytest.mark.anyio
async def test_worker_success_ack(monkeypatch):
    run_id = "r_ok"
    events_topic = f"{worker.EVENTS_PREFIX}{run_id}"

    msg = {
        "value": {
            "run_id": run_id,
            "workflow": "demo",
            "fail_at": None,
            "replay_seq": 0,
        }
    }

    fake = FakeDriftQ([msg])
    monkeypatch.setattr(worker, "driftq", fake)

    async def no_sleep(_):
        return
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await worker.main()

    # success should ack, no nacks, no dlq publish
    assert len(fake.acked) == 1
    assert len(fake.nacked) == 0
    assert types_for(fake, worker.DLQ_TOPIC) == []

    ev_types = types_for(fake, events_topic)
    assert "run.succeeded" in ev_types
