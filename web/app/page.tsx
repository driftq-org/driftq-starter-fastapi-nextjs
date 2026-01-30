"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type JSONPrimitive = string | number | boolean | null;
type JSONValue = JSONPrimitive | JSONValue[] | { [key: string]: JSONValue };
type EventItem = Record<string, JSONValue>;

function eventType(e: EventItem): string {
  return typeof e.type === "string" ? e.type : "event";
}

function eventTs(e: EventItem): number | null {
  return typeof e.ts === "number" ? e.ts : null;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  // Modern API first
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall back
    }
  }

  // Fallback that works in more weird contexts haha
  const ta = document.createElement("textarea");
  ta.value = text;

  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "-9999px";
  ta.style.opacity = "0";
  ta.style.pointerEvents = "none";

  document.body.appendChild(ta);

  const selection = document.getSelection();
  const range =
    selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  ta.focus();
  ta.select();
  ta.setSelectionRange(0, ta.value.length);

  let ok = false;
  try {
    // execCommand is deprecated but still the most compatible fallback
    ok = document.execCommand("copy");
  } finally {
    document.body.removeChild(ta);
    if (range && selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }

  return ok;
}

export default function Home() {
  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const [runId, setRunId] = useState<string>("");
  const [events, setEvents] = useState<EventItem[]>([]);
  const [status, setStatus] = useState<string>("idle");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [query, setQuery] = useState<string>("");
  const [copyStatus, setCopyStatus] = useState<string>("");
  const [failAt, setFailAt] = useState<"none" | "transform" | "tool_call">("none");
  const esRef = useRef<EventSource | null>(null);

  async function replayRun() {
    if (!runId) return;

    try {
      const res = await fetch(`${API_URL}/runs/${runId}/replay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });

      if (!res.ok) {
        const txt = await res.text();
        setStatus(`replay failed: ${res.status} ${txt}`);
        return;
      }

      setStatus("replay requested");
    } catch (err) {
      setStatus(`replay failed: ${(err as Error)?.message ?? "unknown error"}`);
    }
  }

  useEffect(() => {
    return () => {
      esRef.current?.close();
      esRef.current = null;
    };
  }, []);

  async function createRun() {
    esRef.current?.close();
    esRef.current = null;
    setStatus("creating run...");
    setEvents([]);
    setRunId("");
    setTypeFilter("all");
    setQuery("");

    try {
      const res = await fetch(`${API_URL}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workflow: "demo",
          input: { hello: "world" },
          fail_at: failAt === "none" ? null : failAt
        })
      });

      if (!res.ok) {
        const txt = await res.text();
        setStatus(`create failed: ${res.status} ${txt}`);
        return;
      }

      const data = (await res.json()) as { run_id: string };
      setRunId(data.run_id);
      setStatus("run created");
    } catch (err) {
      setStatus(`create failed: ${(err as Error)?.message ?? "unknown error"}`);
    }
  }

  function getClientId() {
    if (typeof window === "undefined") {
      return "ssr";
    }

    const k = "dq_client_id";
    let v = sessionStorage.getItem(k);

    if (!v) {
      v = crypto.randomUUID();
      sessionStorage.setItem(k, v);
    }

    return v;
  }

  function connectSSE(id: string) {
    esRef.current?.close();

    setStatus("connecting SSE...");
    const es = new EventSource(`${API_URL}/runs/${id}/events?client_id=${encodeURIComponent(getClientId())}`);
    esRef.current = es;

    es.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(msg.data) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          setEvents((prev) => [...prev, parsed as EventItem]);
          setStatus("streaming");
        }
      } catch {
        // ignore bad events for now
      }
    };

    es.onerror = () => {
      setStatus("SSE error (is API running?)");
      // keeping it open for now
    };
  }

  async function emitPing() {
    if (!runId) return;

    try {
      await fetch(`${API_URL}/runs/${runId}/emit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event: { type: "demo.ping", msg: "hello from web", ts: Date.now() },
        }),
      });
    } catch {
      // do nothing
    }
  }

  const types = useMemo(() => {
    const set = new Set<string>();
    for (const e of events) set.add(eventType(e));
    return Array.from(set).sort();
  }, [events]);

  const filteredEvents = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter((e) => {
      const t = eventType(e);
      if (typeFilter !== "all" && t !== typeFilter) {
        return false;
      }

      if (!q) {
        return true;
      }

      try {
        return JSON.stringify(e).toLowerCase().includes(q);
      } catch {
        return false;
      }
    });
  }, [events, typeFilter, query]);

  async function onCopyEvent(e: EventItem) {
    try {
      await copyToClipboard(JSON.stringify(e, null, 2));
      setCopyStatus("✅ Copied JSON");
      window.clearTimeout((onCopyEvent as unknown as { _t?: number })._t);

      (onCopyEvent as unknown as { _t?: number })._t = window.setTimeout(() => {
        setCopyStatus("");
      }, 1200);
    } catch {
      setCopyStatus("❌ Copy failed");
      window.setTimeout(() => setCopyStatus(""), 1200);
    }
  }

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100 p-8">
      <div className="mx-auto max-w-3xl space-y-6">
        <header className="space-y-2">
          <h1 className="text-3xl font-bold">DriftQ FastAPI + Next.js Starter</h1>
          <p className="text-sm text-white/70">
            API: <span className="font-mono text-white/90">{API_URL}</span>
          </p>
          <p className="text-sm text-white/70">
            Status: <span className="font-mono text-white/90">{status}</span>
          </p>
        </header>

        <section className="rounded-xl border border-white/10 bg-white/[0.03] p-4 space-y-3">
          <div className="flex flex-wrap gap-2">
            <button
              className="rounded-lg bg-white text-black px-4 py-2 font-medium hover:bg-white/90"
              onClick={createRun}
            >
              Create Run
            </button>

            <select
              value={failAt}
              onChange={(e) => setFailAt(e.target.value as typeof failAt)}
              className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-white outline-none focus:border-white/25"
            >
              <option value="none">Fail: none</option>
              <option value="transform">Fail: transform</option>
              <option value="tool_call">Fail: tool_call</option>
            </select>

            <button
              className="rounded-lg border border-white/15 px-4 py-2 disabled:opacity-50 hover:bg-white/[0.05]"
              disabled={!runId}
              onClick={() => connectSSE(runId)}
            >
              Connect SSE
            </button>

            <button
              className="rounded-lg border border-white/15 px-4 py-2 disabled:opacity-50 hover:bg-white/[0.05]"
              disabled={!runId}
              onClick={emitPing}
            >
              Emit Ping
            </button>

            <button
              className="rounded-lg border border-white/15 px-4 py-2 disabled:opacity-50 hover:bg-white/[0.05]"
              disabled={!runId}
              onClick={replayRun}
            >
              Replay Run
            </button>
          </div>

          <div className="text-sm text-white/70">
            Run ID:{" "}
            <span className="font-mono text-white/90">
              {runId ? runId : "(none yet)"}
            </span>
          </div>
        </section>

        <section className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div>
              <h2 className="font-semibold">Timeline</h2>
              <p className="text-xs text-white/60">
                Showing{" "}
                <span className="font-mono text-white/80">{filteredEvents.length}</span>{" "}
                of <span className="font-mono text-white/80">{events.length}</span>
              </p>
            </div>

            <div className="text-xs text-white/70 font-mono">{copyStatus}</div>
          </div>

          <div className="mb-4 flex flex-col sm:flex-row gap-2">
            <div className="flex-1">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder='Search JSON (e.g. "demo.ping" or "hello")'
                className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-white placeholder:text-white/40 outline-none focus:border-white/25"
              />
            </div>

            <div className="flex gap-2">
              <select
                value={typeFilter}
                onChange={(e) => setTypeFilter(e.target.value)}
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-white outline-none focus:border-white/25"
              >
                <option value="all">All types</option>
                {
                  types.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))
                }
              </select>

              <button
                onClick={() => {
                  // stop streaming so the list does NOT immediately refill
                  esRef.current?.close();
                  esRef.current = null;

                  setEvents([]);
                  setQuery("");
                  setTypeFilter("all");
                  setStatus("idle");
                }}
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-white/80 hover:bg-white/[0.06]"
              >
                Clear
              </button>
            </div>
          </div>

          {
            filteredEvents.length === 0
            ? <p className="text-sm text-white/60">No events match your filters.</p>
            : <ul className="space-y-3">
                {filteredEvents
                  .slice()
                  // newest last by default
                  .map((e, idx) => {
                    const type = eventType(e);
                    const ts = eventTs(e);
                    const tsStr = ts ? new Date(ts).toLocaleTimeString() : null;

                    return (
                      <li
                        key={`${type}-${idx}`}
                        className="rounded-lg border border-white/10 bg-neutral-900/60 p-3"
                      >
                        <div className="mb-2 flex items-center justify-between gap-3">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-xs text-white/90">
                              {type}
                            </span>
                            {
                              tsStr ? (
                                <span className="font-mono text-xs text-white/50">
                                  {tsStr}
                                </span>
                              ) : null
                            }
                          </div>

                          <button
                            onClick={() => onCopyEvent(e)}
                            className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-xs text-white/80 hover:bg-white/[0.06]"
                          >
                            Copy JSON
                          </button>
                        </div>

                        <pre className="font-mono text-xs sm:text-sm text-white/90 whitespace-pre-wrap break-words max-h-64 overflow-auto rounded-md bg-black/30 p-3 border border-white/5">
                          {JSON.stringify(e, null, 2)}
                        </pre>
                      </li>
                    );
                  })}
              </ul>
          }
        </section>
      </div>
    </main>
  );
}
