import pytest
import app.worker as worker


class FakeDriftQ:
    def __init__(self):
        self.topics = set()
        self.records = []  # (topic, value, idem)

    async def ensure_topic(self, topic: str) -> None:
        self.topics.add(topic)

    async def produce(self, topic: str, value: dict, idempotency_key=None) -> None:
        self.records.append((topic, value, idempotency_key))


def event_types_for_topic(fake: FakeDriftQ, topic: str) -> list[str]:
    return [v["type"] for (t, v, _idem) in fake.records if t == topic]


@pytest.mark.anyio
async def test_fail_none_succeeds(monkeypatch):
    fake = FakeDriftQ()
    monkeypatch.setattr(worker, "driftq", fake)

    run_id = "r1"
    events_topic = f"{worker.EVENTS_PREFIX}{run_id}"

    await worker.run_demo_workflow(run_id, events_topic, fail_at=None)

    assert event_types_for_topic(fake, events_topic) == [
        "run.started",
        "step.started", "step.completed",      # fetch_input
        "step.started", "step.completed",      # transform
        "step.started", "step.completed",      # tool_call
        "step.started", "step.completed",      # finalize
        "run.succeeded"
    ]


@pytest.mark.anyio
async def test_fail_transform_raises_at_transform(monkeypatch):
    fake = FakeDriftQ()
    monkeypatch.setattr(worker, "driftq", fake)

    run_id = "r2"
    events_topic = f"{worker.EVENTS_PREFIX}{run_id}"

    with pytest.raises(RuntimeError, match="forced failure at transform"):
        await worker.run_demo_workflow(run_id, events_topic, fail_at="transform")

    assert event_types_for_topic(fake, events_topic) == [
        "run.started",
        "step.started", "step.completed",      # fetch_input
        "step.started",                        # transform (no completed)
    ]


@pytest.mark.anyio
async def test_fail_tool_call_raises_at_tool_call(monkeypatch):
    fake = FakeDriftQ()
    monkeypatch.setattr(worker, "driftq", fake)

    run_id = "r3"
    events_topic = f"{worker.EVENTS_PREFIX}{run_id}"

    with pytest.raises(RuntimeError, match="forced failure at tool_call"):
        await worker.run_demo_workflow(run_id, events_topic, fail_at="tool_call")

    assert event_types_for_topic(fake, events_topic) == [
        "run.started",
        "step.started", "step.completed",      # fetch_input
        "step.started", "step.completed",      # transform
        "step.started",                        # tool_call (no completed)
    ]
