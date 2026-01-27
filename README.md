# driftq-fastapi-nextjs-starter

Starter template: **FastAPI + Next.js + Worker** wired to **DriftQ-Core** via Docker Compose.

This is **not** “hello world.” The point is to show a real-ish workflow pattern:
- UI triggers a run
- API creates the run + streams events (SSE)
- Worker executes steps
- Includes a failure path + replay/redrive later

---

## Quick start

```bash
# from repo root (DriftQ-Starters)
cd driftq-fastapi-nextjs-starter
cp .env.example .env
make up
```

Open:
- UI: http://localhost:3000
- API: http://localhost:8000
- DriftQ: http://localhost:8080

---

## What’s running

- **driftq**: DriftQ-Core broker (pulled as a Docker image)
- **api**: FastAPI backend (builds from `./api`)
- **worker**: step executor (builds from `./worker`)
- **web**: Next.js UI (builds from `./web`)

---

## Configuration

Set the DriftQ image in `.env`:

```env
DRIFTQ_IMAGE=ghcr.io/driftq-org/driftqd:latest
```

If you have a different tag (recommended), just change it.

---

## Dev commands

```bash
make up        # docker compose up --build
make logs      # follow logs
make down      # stop
make reset     # wipe volumes + restart
```

---

## Next steps (what we’ll build)

1. FastAPI skeleton + `/healthz`
2. “Run” endpoint (create run + enqueue first step)
3. SSE endpoint to stream run events
4. Worker that consumes steps and publishes step events
5. Next.js page to trigger run + show timeline
6. Forced failure + replay/redrive path
