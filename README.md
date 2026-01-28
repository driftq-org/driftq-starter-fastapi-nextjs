# driftq-starter-fastapi-nextjs

Starter template: **FastAPI API + Next.js UI + Worker** wired to **DriftQ-Core** via **Docker Compose**.

This is **not** “hello world.” The point is to show a real-ish, durable workflow pattern:

- UI triggers a run
- API creates the run + streams events (SSE)
- Worker executes steps
- Includes a failure path + replay/redrive later

---

## Create your own repo from this template

1. Click **Use this template** (top-right on GitHub).
2. Create a new repo (your org or personal account).
3. Clone your new repo locally and run the quick start below.

---

## Quick start

### Prereqs
- Docker Desktop (or Docker Engine)
- `make` (recommended)

### Run it

```bash
# from repo root
# (optional) if you have an env template:
# cp .env.example .env

make up
```

Open:
- UI: http://localhost:3000
- API: http://localhost:8000  (health: http://localhost:8000/healthz)
- DriftQ: http://localhost:8080

---

## What’s running (Docker Compose)

- **driftq**: DriftQ-Core broker (pulled as a Docker image)
- **api**: FastAPI backend (builds from `./api`)
- **worker**: step executor (see `docker-compose.yml` for the exact build/command)
- **web**: Next.js UI (builds from `./web`)

---

## Repo layout

```
.
├─ api/                  # FastAPI app + worker code
├─ web/                  # Next.js UI
├─ docker-compose.yml    # Local stack (driftq + api + worker + web)
├─ Makefile              # Common dev commands
└─ README.md
```

---

## Configuration

### DriftQ image

If your repo has a `.env`, set the DriftQ image/tag there:

```env
DRIFTQ_IMAGE=ghcr.io/driftq-org/driftqd:latest
```

If you have a pinned tag (recommended), swap `latest` for the version you want.

---

## Dev commands

```bash
make up        # docker compose up --build
make logs      # follow logs
make down      # stop
make reset     # wipe volumes + restart
```

---

## How it works (high level)

1. **UI** calls the **API** to start a run
2. **API** writes the run state + publishes the first step to **DriftQ**
3. **Worker** consumes steps, executes work, and publishes step events back
4. **API** streams events to the UI via **SSE** so the timeline updates live
5. A forced failure path is included to prove replay/redrive behavior

---

## Next steps (what we’ll build / extend)

1. FastAPI skeleton + `/healthz` ✅
2. “Run” endpoint (create run + enqueue first step)
3. SSE endpoint to stream run events
4. Worker that consumes steps and publishes step events
5. Next.js page to trigger run + show timeline
6. Forced failure + replay/redrive path
