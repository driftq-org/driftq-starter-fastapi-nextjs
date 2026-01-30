# driftq-starter-fastapi-nextjs

A **FastAPI + Next.js** starter that demonstrates **durable AI/workflow execution** using **DriftQ-Core** (running via Docker).

This is intentionally **not** "hello world." The point is to show what teams actually need in production workflows:

✅ **Durable runs** (not "best effort")
✅ **Live timeline** streamed to the UI (SSE)
✅ **Retries + backoff** (demo-friendly)
✅ **DLQ after max attempts** (so failures don’t vanish)
✅ **Replay / redrive** a run on demand
✅ **Idempotency-friendly** event publishing

---

## ⚡ 2-minute demo (the money demo)

### Prereqs
- Docker Desktop (or Docker Engine)
- `make` (recommended)

### 0) Start the full stack (recommended)
From repo root:

```bash
make up
```

Open:
- UI: http://localhost:3000
- API: http://localhost:8000  (health: http://localhost:8000/healthz)
- DriftQ: http://localhost:8080

### 1) Happy path run
1. Open the UI
2. Select `Fail: none`
3. Click **Create Run**
4. Click **Connect SSE**

✅ You should see something like:
`run.created → worker.received → run.started → step.* → run.succeeded`

### 2) Deterministic failure (transform) → Retry → DLQ
1. Select `Fail: transform`
2. Click **Create Run**
3. Click **Connect SSE**

✅ You should see:
`step.started(transform) → run.attempt_failed → run.retry_scheduled` (repeats) → `run.dlq → run.failed`

This represents "**our code/data is bad**" (transform bug, invalid input, etc.).
DriftQ makes it **auditable** and **replayable**, instead of silently dying.

### 3) External-ish failure (tool_call) → Retry → DLQ (core AI pain point 💥)
1. Select `Fail: tool_call`
2. Click **Create Run**
3. Click **Connect SSE**

✅ You should see:
`step.started(tool_call) → run.attempt_failed → run.retry_scheduled` (repeats) → `run.dlq → run.failed`

This represents "**the outside world is flaky**" (LLM timeout, rate limit, tool API down).
This is where DriftQ pays for itself: failures don’t vanish — they become a durable stream you can inspect, alert on, and replay.

### 4) Replay / redrive
Take the `run_id` shown in the UI and run:

```bash
curl -X POST http://localhost:8000/runs/<RUN_ID>/replay
```

✅ You should see replay-related events and the workflow executes again with a new `replay_seq`.

---

## Create your own repo from this template

1. Click **Use this template** (top-right on GitHub)
2. Create a new repo (org or personal)
3. Clone your new repo locally and run the demo above

---

## Why DriftQ in a FastAPI + Next.js stack?

Most "AI apps" quickly become **workflow apps**:
- step-by-step execution (tools, transforms, model calls)
- retries (timeouts happen constantly)
- long-running work (minutes+)
- event streaming to the UI (progress, partial results, errors)
- redrive/replay when something fails

If you build this yourself, you end up reinventing a queue + durable state + retries + DLQ + replay + observability.

**DriftQ is the "workflow backbone"**:
- Your API **publishes commands** to DriftQ
- Workers **lease + execute** tasks (consumer group style)
- `nack` triggers **redelivery** (used here to implement retries)
- Events are written to a **per-run topic** and streamed to the UI

---

## What "Fail:" means (the 3 options)

The demo workflow runs these steps in order:

`fetch_input → transform → tool_call → finalize`

- **Fail: none** — happy path, proves end-to-end execution + SSE timeline.
- **Fail: transform** — "our code/data broke" (deterministic failure class).
- **Fail: tool_call** — "external dependency broke" (LLM/tool/API failure class).

Keeping all three makes the value obvious in under 2 minutes.

---

## What’s running (Docker Compose)

- **driftq**: DriftQ-Core broker (pulled as a Docker image)
- **api**: FastAPI backend (builds from `./api`)
- **worker**: step executor (builds from `./api`, runs `python -m app.worker`)
- **web**: Next.js UI (builds from `./web`)

---

## Repo layout

```text
.
├─ api/                  # FastAPI app + worker code
├─ web/                  # Next.js UI
├─ docker-compose.yml    # Local stack (driftq + api + worker + web)
├─ Makefile              # Common dev commands
└─ README.md
```

---

## Key endpoints

- `POST /runs` → create a run and enqueue the first command
- `GET /runs/{run_id}/events` → SSE stream for the run timeline
- `POST /runs/{run_id}/replay` → replay/redrive a run
- `POST /runs/{run_id}/emit` → emit a custom event (demo button uses this)
- `GET /healthz` → API health

---

## Dev commands

```bash
make up        # docker compose up --build
make logs      # follow logs
make down      # stop
make reset     # wipe volumes + restart (new WAL)
```

---

## Persistence (WAL) — where the data lives

In this starter, DriftQ stores its WAL inside a **Docker volume** mounted at `/data` in the container.

To see the WAL inside the running DriftQ container:

```bash
docker ps --format "table {{.Names}}	{{.Status}}	{{.Image}}"
docker exec -it <DRIFTQ_CONTAINER_NAME> ls -lah /data
```

To "start fresh" with a new WAL, use:

```bash
make reset
```

---

## How it works (high level)

1. **UI** calls the **API** to start a run
2. **API** creates a `run_id`, emits `run.created`, and publishes a command to `runs.commands`
3. **Worker** consumes the command, executes steps, and emits events to `runs.events.<run_id>`
4. **API** streams `runs.events.<run_id>` back to the UI via **SSE**
5. Failure mode:
   - Worker emits `run.attempt_failed`
   - Worker `nack`s to request redelivery (retry)
   - After `MAX_ATTEMPTS`, worker emits `run.dlq` + `run.failed` and publishes to `runs.dlq`

---

## Configuration

### DriftQ image

If your repo has a `.env`, set the DriftQ image/tag there:

```env
DRIFTQ_IMAGE=ghcr.io/driftq-org/driftq-core:1.0.0
```

Pinning a version is recommended (avoid `latest` for reproducibility).

---

## Next steps (what we’ll extend)

This starter is intentionally minimal. The goal is to expand it into a "holy crap" showcase repo over time:

- ✅ basic workflow + SSE timeline
- ✅ failure injection (`transform`, `tool_call`) + retry + DLQ
- ✅ replay/redrive
- ⏭️ make `tool_call` a *real* external call (HTTP request + timeout + retry)
- ⏭️ add a small "AI step" (LLM call) with idempotency and structured outputs
- ⏭️ add basic metrics + a `/debug` page (lag, inflight, DLQ counts)
- ⏭️ add a "Run Inspector" panel (attempts, replay history, DLQ payload)
