import asyncio
import time
from typing import Any, Dict, Optional
from .driftq_client import DriftQClient

driftq = DriftQClient()

COMMANDS_TOPIC = "runs.commands"
DLQ_TOPIC = "runs.dlq"
EVENTS_PREFIX = "runs.events."

# demo retry state (in-memory). Good enough to showcase retries + DLQ in this starter
ATTEMPTS: Dict[str, int] = {}
MAX_ATTEMPTS = 3


def now_ms() -> int:
    return int(time.time() * 1000)


async def emit(events_topic: str, evt: Dict[str, Any], *, idem: Optional[str] = None) -> None:
    # events topic is per-run; ensure it exists (safe if already exists)
    await driftq.ensure_topic(events_topic)
    await driftq.produce(events_topic, evt, idempotency_key=idem)


async def publish_dlq(record: Dict[str, Any], *, idem: Optional[str] = None) -> None:
    # global DLQ topic
    await driftq.ensure_topic(DLQ_TOPIC)
    await driftq.produce(DLQ_TOPIC, record, idempotency_key=idem)


async def run_demo_workflow(run_id: str, events_topic: str, fail_at: Optional[str]) -> None:
    await emit(events_topic, {"ts": now_ms(), "type": "run.started", "run_id": run_id})

    steps = ["fetch_input", "transform", "tool_call", "finalize"]
    for step in steps:
        await emit(events_topic, {"ts": now_ms(), "type": "step.started", "run_id": run_id, "step": step})

        # if step == "tool_call":
        #     print(f"[worker] executing step=tool_call fail_at={fail_at!r}")

        # failure injection
        if fail_at == step:
            raise RuntimeError(f"forced failure at {step}")

        # pretend work
        await asyncio.sleep(0.2)

        await emit(events_topic, {"ts": now_ms(), "type": "step.completed", "run_id": run_id, "step": step})

    await emit(events_topic, {"ts": now_ms(), "type": "run.succeeded", "run_id": run_id})


async def safe_ack(*, topic: str, group: str, msg: Dict[str, Any]) -> None:
    try:
        await driftq.ack(topic=topic, group=group, msg=msg)
    except Exception:
        pass


async def safe_nack(*, topic: str, group: str, msg: Dict[str, Any]) -> bool:
    try:
        await driftq.nack(topic=topic, group=group, msg=msg)
        return True
    except Exception:
        return False


async def main() -> None:
    await driftq.ensure_topic(COMMANDS_TOPIC)
    await driftq.ensure_topic(DLQ_TOPIC)

    group = "demo-worker"
    print(f"[worker] consuming topic={COMMANDS_TOPIC} group={group}")

    async for msg in driftq.consume_stream(topic=COMMANDS_TOPIC, group=group, lease_ms=30000, timeout_s=60.0):
        payload = driftq.extract_value(msg) or {}
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            # ack junk so we don't spin
            await safe_ack(topic=COMMANDS_TOPIC, group=group, msg=msg)
            continue

        events_topic = f"{EVENTS_PREFIX}{run_id}"
        fail_at = payload.get("fail_at")
        # print(f"[worker] cmd fail_at={fail_at!r} payload_keys={list(payload.keys())}")

        # attempts should reset for replay
        replay_seq = int(payload.get("replay_seq") or 0)
        attempt_key = f"{run_id}:{replay_seq}"

        ATTEMPTS[attempt_key] = ATTEMPTS.get(attempt_key, 0) + 1
        attempt = ATTEMPTS[attempt_key]

        try:
            await emit(
                events_topic,
                {"ts": now_ms(), "type": "worker.received", "run_id": run_id, "attempt": attempt, "replay_seq": replay_seq},
            )

            await run_demo_workflow(run_id, events_topic, fail_at)

            # success => ack command + cleanup attempts
            await safe_ack(topic=COMMANDS_TOPIC, group=group, msg=msg)
            ATTEMPTS.pop(attempt_key, None)

        except Exception as e:
            err = str(e)

            await emit(
                events_topic,
                {
                    "ts": now_ms(),
                    "type": "run.attempt_failed",
                    "run_id": run_id,
                    "attempt": attempt,
                    "replay_seq": replay_seq,
                    "error": err,
                },
            )

            if attempt < MAX_ATTEMPTS:
                await emit(
                    events_topic,
                    {
                        "ts": now_ms(),
                        "type": "run.retry_scheduled",
                        "run_id": run_id,
                        "attempt": attempt + 1,
                        "replay_seq": replay_seq,
                        "max_attempts": MAX_ATTEMPTS,
                    },
                )

                # small backoff so retries don't hammer instantly (for demo purpose/demo-friendly)
                await asyncio.sleep(0.3 * attempt)

                # tell DriftQ to redeliver
                ok = await safe_nack(topic=COMMANDS_TOPIC, group=group, msg=msg)
                if not ok:
                    # fallback: ack to avoid infinite loop if nack breaks
                    await safe_ack(topic=COMMANDS_TOPIC, group=group, msg=msg)

            else:
                # REAL DLQ: publish original command + error
                dlq_record = {
                    "ts": now_ms(),
                    "type": "runs.dlq",
                    "run_id": run_id,
                    "workflow": payload.get("workflow", "demo"),
                    "attempts": attempt,
                    "max_attempts": MAX_ATTEMPTS,
                    "replay_seq": replay_seq,
                    "reason": "max_attempts",
                    "error": err,
                    "command": payload,  # keep the original command payload for replay
                }

                dlq_idem = f"dlq:{run_id}:{replay_seq}:{attempt}"
                await publish_dlq(dlq_record, idem=dlq_idem)
                await emit(
                    events_topic,
                    {
                        "ts": now_ms(),
                        "type": "run.dlq",
                        "run_id": run_id,
                        "replay_seq": replay_seq,
                        "reason": "max_attempts",
                        "dlq_topic": DLQ_TOPIC,
                        "dlq_idem": dlq_idem,
                    },
                )
                await emit(events_topic, {"ts": now_ms(), "type": "run.failed", "run_id": run_id, "replay_seq": replay_seq})

                # ack so it's NOT re-delivered forever + cleanup attempts
                await safe_ack(topic=COMMANDS_TOPIC, group=group, msg=msg)
                ATTEMPTS.pop(attempt_key, None)


if __name__ == "__main__":
    asyncio.run(main())
