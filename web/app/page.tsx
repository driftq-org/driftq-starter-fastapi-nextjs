"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type JSONPrimitive = string | number | boolean | null;
type JSONValue = JSONPrimitive | JSONValue[] | { [key: string]: JSONValue };
type EventItem = Record<string, JSONValue>;
type DemoMode = "idle" | "running" | "done" | "error";
type DemoSteps = {
  fail: boolean;
  dlq: boolean;
  replay: boolean;
  success: boolean;
};


function eventType(e: EventItem): string {
  return typeof e.type === "string" ? e.type : "event";
}

function eventTs(e: EventItem): number | null {
  return typeof e.ts === "number" ? e.ts : null;
}

function isDlqSignal(e: EventItem): boolean {
  const t = eventType(e);
  return t === "dlq.available" || t === "run.dlq" || t === "runs.dlq";
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * waitFor() with optional fail-fast predicate:
 * - cond(): return true when done
 * - failFast(): return string (error message) to abort early, or ""/null/false to keep waiting
 */
async function waitFor(cond: () => boolean | Promise<boolean>, timeoutMs: number, tickMs = 250, failFast?: () => string | null | false) {
  const started = Date.now();
  for (;;) {
    const ff = failFast?.();
    if (ff) {
      throw new Error(ff);
    }

    const ok = await cond();
    if (ok) {
      return;
    }

    if (Date.now() - started > timeoutMs) {
      throw new Error("timeout");
    }

    await sleep(tickMs);
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall back
    }
  }

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
  const range = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  ta.focus();
  ta.select();
  ta.setSelectionRange(0, ta.value.length);

  let ok = false;
  try {
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
  const API_URL = useMemo(() => {
    const envUrl = process.env.NEXT_PUBLIC_API_URL;
    if (typeof window === "undefined") {
      return envUrl || "http://localhost:8000";
    }

    const { protocol, hostname } = window.location;
    const isCodespacesHost = hostname.endsWith(".app.github.dev");

    if (envUrl && !envUrl.includes("localhost")) {
      return envUrl;
    }

    if (isCodespacesHost) {
      const codespacesApiHost = hostname.replace(/-3000\./, "-8000.");
      if (codespacesApiHost !== hostname) {
        return `${protocol}//${codespacesApiHost}`;
      }
    }

    return `${protocol}//${hostname}:8000`;
  }, []);

  const [runId, setRunId] = useState<string>("");
  const [events, setEvents] = useState<EventItem[]>([]);
  const [status, setStatus] = useState<string>("idle");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [query, setQuery] = useState<string>("");
  const [copyStatus, setCopyStatus] = useState<string>("");
  const [failAt, setFailAt] = useState<"none" | "transform" | "tool_call">("none");

  // DLQ inspector state
  const [dlqRecord, setDlqRecord] = useState<EventItem | null>(null);
  const [dlqStatus, setDlqStatus] = useState<string>("");
  const [dlqAvailable, setDlqAvailable] = useState<boolean>(false);

  // Product-y UX: collapse DLQ after success (demo only)
  const [dlqResolved, setDlqResolved] = useState<boolean>(false);
  const [showDlqDetails, setShowDlqDetails] = useState<boolean>(true);

  // Demo script state
  const [demoMode, setDemoMode] = useState<DemoMode>("idle");
  const [demoSteps, setDemoSteps] = useState<DemoSteps>({
    fail: false,
    dlq: false,
    replay: false,
    success: false
  });
  const [demoRunId, setDemoRunId] = useState<string>("");
  const [demoError, setDemoError] = useState<string>("");

  const esRef = useRef<EventSource | null>(null);
  const demoModeRef = useRef<DemoMode>("idle");
  const eventsRef = useRef<EventItem[]>([]);
  const dlqAvailableRef = useRef<boolean>(false);
  const dlqRecordRef = useRef<EventItem | null>(null);
  const dlqAutoFetchedRef = useRef<boolean>(false);

  useEffect(() => {
    demoModeRef.current = demoMode;
  }, [demoMode]);

  function stopSSE() {
    esRef.current?.close();
    esRef.current = null;
  }

  function resetAll() {
    stopSSE();

    setEvents([]);
    eventsRef.current = [];

    setRunId("");
    setStatus("idle");
    setTypeFilter("all");
    setQuery("");

    setDlqRecord(null);
    dlqRecordRef.current = null;

    setDlqStatus("");
    setDlqAvailable(false);
    dlqAvailableRef.current = false;

    dlqAutoFetchedRef.current = false;

    setDlqResolved(false);
    setShowDlqDetails(true);

    setDemoMode("idle");
    setDemoError("");
    setDemoSteps({ fail: false, dlq: false, replay: false, success: false });
    setDemoRunId("");
  }

  function markDemoStep(step: keyof DemoSteps) {
    setDemoSteps((s) => (s[step] ? s : { ...s, [step]: true }));
  }

  function hasEvent(t: string): boolean {
    return eventsRef.current.some((e) => eventType(e) === t);
  }

  function hasAnyEvent(types: string[]): boolean {
    return eventsRef.current.some((e) => types.includes(eventType(e)));
  }

  function hasDlqForReplaySeq(seq: number): boolean {
    return eventsRef.current.some((e) => {
      if (!isDlqSignal(e)) {
        return false;
      }

      const rs = e.replay_seq;
      return typeof rs === "number" && rs === seq;
    });
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

  async function fetchDlq(targetRunId?: string) {
    const rid = targetRunId ?? runId;
    if (!rid) {
      return false;
    }

    setDlqStatus("loading...");
    try {
      const res = await fetch(`${API_URL}/runs/${rid}/dlq`, { credentials: "include" });
      if (!res.ok) {
        const txt = (await res.text()).trim();
        setDlqRecord(null);
        dlqRecordRef.current = null;

        setDlqStatus(txt ? `No DLQ (${res.status}): ${txt}` : `No DLQ (${res.status})`);
        return false;
      }

      const data = (await res.json()) as EventItem;
      setDlqRecord(data);
      dlqRecordRef.current = data;

      setDlqStatus("✅ loaded");
      setDlqAvailable(true);
      dlqAvailableRef.current = true;

      // if user explicitly loads, show details
      setShowDlqDetails(true);

      return true;
    } catch {
      setDlqRecord(null);
      dlqRecordRef.current = null;

      setDlqStatus("❌ failed to fetch");
      return false;
    }
  }

  function connectSSE(id: string) {
    stopSSE();

    setStatus("connecting SSE...");
    dlqAutoFetchedRef.current = false;

    const es = new EventSource(`${API_URL}/runs/${id}/events?client_id=${encodeURIComponent(getClientId())}`, { withCredentials: true });
    esRef.current = es;

    es.onmessage = (msg) => {
      try {
        const parsed = JSON.parse(msg.data) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          const evt = parsed as EventItem;

          setEvents((prev) => {
            const next = [...prev, evt];
            eventsRef.current = next;
            return next;
          });

          setStatus("streaming");
          const t = eventType(evt);

          // Step 1 (fail)
          if (t === "run.attempt_failed" || t === "run.failed" || isDlqSignal(evt)) {
            markDemoStep("fail");
          }

          // Step 2 (dlq)
          if (isDlqSignal(evt)) {
            setDlqAvailable(true);
            dlqAvailableRef.current = true;

            setDlqStatus((s) => s || "⚠️ DLQ available");
            markDemoStep("dlq");

            if (!dlqAutoFetchedRef.current) {
              dlqAutoFetchedRef.current = true;
              void fetchDlq(id);
            }
          }

          // Step 3 (replay)
          if (t === "run.replay_requested" || t === "ui.replay.fix_applied") {
            markDemoStep("replay");
          }

          // Step 4 (success)
          if (t === "run.succeeded") {
            markDemoStep("success");

            // Demo-only: collapse DLQ after success to make the "recovered" moment pop
            if (demoModeRef.current !== "idle") {
              setDlqResolved(true);
              setShowDlqDetails(false);
            }

            if (demoModeRef.current === "running") {
              setDemoMode("done");
              setStatus("✅ demo complete");
            }
          }
        }
      } catch {
        // ignore bad events
      }
    };

    es.onerror = () => {
      setStatus("SSE error (is API running?)");
    };
  }

  async function createRunWith(fail: "none" | "transform" | "tool_call") {
    stopSSE();

    setStatus("creating run...");
    setEvents([]);
    eventsRef.current = [];

    setRunId("");
    setTypeFilter("all");
    setQuery("");

    setFailAt(fail);

    // reset DLQ panel
    setDlqRecord(null);
    dlqRecordRef.current = null;

    setDlqStatus("");
    setDlqAvailable(false);
    dlqAvailableRef.current = false;

    dlqAutoFetchedRef.current = false;

    setDlqResolved(false);
    setShowDlqDetails(true);

    const res = await fetch(`${API_URL}/runs`, {
      credentials: "include",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workflow: "demo",
        input: { hello: "world" },
        fail_at: fail === "none" ? null : fail
      })
    });

    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`create failed: ${res.status} ${txt}`);
    }

    const data = (await res.json()) as { run_id: string };
    setRunId(data.run_id);
    setStatus("run created");
    return data.run_id;
  }

  async function createRun() {
    try {
      const id = await createRunWith(failAt);
      return id;
    } catch (err) {
      setStatus((err as Error)?.message ?? "create failed");
      return "";
    }
  }

  async function replayRunFix(targetRunId?: string) {
    const rid = targetRunId ?? runId;
    if (!rid) {
      return;
    }

    try {
      const res = await fetch(`${API_URL}/runs/${rid}/replay`, {
        credentials: "include",
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fail_at: null })
      });

      if (!res.ok) {
        const txt = await res.text();
        setStatus(`replay failed: ${res.status} ${txt}`);
        return;
      }

      const now = Date.now();
      const uiEvt: EventItem = {
        ts: now,
        type: "ui.replay.fix_applied",
        run_id: rid,
        fail_at: null,
        note: "Replay requested (fix applied ✅)"
      };

      setEvents((prev) => {
        const next = [...prev, uiEvt];
        eventsRef.current = next;
        return next;
      });

      setStatus("replay requested (fix applied)");
      markDemoStep("replay");
    } catch (err) {
      setStatus(`replay failed: ${(err as Error)?.message ?? "unknown error"}`);
    }
  }

  async function emitPing() {
    if (!runId) {
      return;
    }

    try {
      await fetch(`${API_URL}/runs/${runId}/emit`, {
        credentials: "include",
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: { type: "demo.ping", msg: "hello from web", ts: Date.now() }})
      });
    } catch {
      // ignore
    }
  }

  async function onCopyEvent(e: EventItem) {
    try {
      await copyToClipboard(JSON.stringify(e, null, 2));
      setCopyStatus("✅ Copied JSON");
      window.clearTimeout((onCopyEvent as unknown as { _t?: number })._t);
      (onCopyEvent as unknown as { _t?: number })._t = window.setTimeout(() => {setCopyStatus("");}, 1200);
    } catch {
      setCopyStatus("❌ Copy failed");
      window.setTimeout(() => setCopyStatus(""), 1200);
    }
  }

  async function onCopyDlq() {
    if (!dlqRecord) {
      return;
    }

    await onCopyEvent(dlqRecord);
  }

  async function runTwoMinuteDemo() {
    if (demoModeRef.current === "running") {
      return;
    }

    setDemoMode("running");
    setDemoError("");
    setDemoSteps({ fail: false, dlq: false, replay: false, success: false });

    setDlqResolved(false);
    setShowDlqDetails(true);

    try {
      // 1) Create forced-failing run
      const id = await createRunWith("tool_call");
      setDemoRunId(id);

      // 2) Connect SSE immediately
      connectSSE(id);

      // 3) Wait until we see "fail" signals
      await waitFor(
        () => hasAnyEvent(["run.attempt_failed", "run.failed", "run.dlq", "runs.dlq"]),
        45_000,
        400
      );
      markDemoStep("fail");

      // 4) Wait until DLQ is available (event OR /dlq endpoint)
      await waitFor(
        async () => {
          if (dlqAvailableRef.current) {
            return true;
          }

          if (dlqRecordRef.current) {
            return true;
          }

          try {
            const res = await fetch(`${API_URL}/runs/${id}/dlq`, { credentials: "include" });
            if (res.ok) {
              const data = (await res.json()) as EventItem;
              setDlqRecord(data);
              dlqRecordRef.current = data;

              setDlqStatus("✅ loaded");
              setDlqAvailable(true);
              dlqAvailableRef.current = true;

              return true;
            }
          } catch {
            // ignore transient network issues
          }

          return false;
        },
        90_000,
        800
      );
      markDemoStep("dlq");

      // 5) Replay with fix applied (fail_at=null)
      await replayRunFix(id);

      // 6) Wait for success — fail-fast if replay DLQs again
      const expectedReplaySeq = 1;
      await waitFor(
        () => hasEvent("run.succeeded"),
        90_000,
        500,
        () => {
          if (hasDlqForReplaySeq(expectedReplaySeq)) {
            return "Replay still DLQ'd (fix not applied). Make sure backend replay accepts fail_at override.";
          }

          return false;
        }
      );

      markDemoStep("success");
      setDemoMode("done");
      setStatus("✅ demo complete");
    } catch (e) {
      const msg =
        (e as Error)?.message === "timeout"
          ? "Timed out waiting for DLQ/success (is worker + API running?)"
          : (e as Error)?.message ?? "demo failed";

      setDemoMode("error");
      setDemoError(msg);
      setStatus(`❌ demo failed: ${msg}`);
    }
  }

  useEffect(() => {
    return () => {
      stopSSE();
    };
  }, []);

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

  const demoHeaderStatus =
    demoMode === "running"
      ? "running"
      : demoMode === "done"
        ? "done ✅"
        : demoMode === "error"
          ? "error ❌"
          : "idle";

  // Make dots "current step aware
  const stepOrder: (keyof DemoSteps)[] = ["fail", "dlq", "replay", "success"];
  const currentIdx = (() => {
    for (let i = 0; i < stepOrder.length; i++) {
      if (!demoSteps[stepOrder[i]]) {
        return i;
      }
    }

    return stepOrder.length - 1;
  })();

  function stepDot(stepKey: keyof DemoSteps, idx: number) {
    if (demoSteps[stepKey]) {
      return "🟢";
    }

    if (demoMode === "running" && idx === currentIdx) {
      return "🟡";
    }

    return "⚪";
  }

  const showResolvedBanner = dlqResolved && (dlqAvailable || !!dlqRecord) && demoMode !== "idle";

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

        {/* Demo Script panel */}
        <section className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
          <div className="flex items-center justify-between">
            <div className="font-semibold">Demo Script</div>
            <div className="text-xs font-mono text-white/60">{demoHeaderStatus}</div>
          </div>
          <div className="text-xs text-white/60 mt-1">
            Fail → DLQ → Replay (fix) → Success (the 2-minute DriftQ story) 🧠
          </div>

          <div className="mt-3 space-y-1 text-sm">
            <div>{stepDot("fail", 0)} 1) Fail (forced)</div>
            <div>{stepDot("dlq", 1)} 2) DLQ persisted</div>
            <div>{stepDot("replay", 2)} 3) Replay with fix applied</div>
            <div>{stepDot("success", 3)} 4) Success</div>
          </div>

          {
            demoRunId
              ? (
                <div className="mt-3 text-xs text-white/70">
                  Demo run: <span className="font-mono text-white/90">{demoRunId}</span>
                </div>
              )
              : null
          }

          {
            demoMode === "error" && demoError
              ? <div className="mt-3 text-xs text-red-300">{demoError}</div>
              : null
          }
        </section>

        {/* Controls */}
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
              onClick={() => replayRunFix()}
            >
              Replay Run (fix)
            </button>

            <button
              className={[
                "rounded-lg border px-4 py-2 disabled:opacity-50 hover:bg-white/[0.05]",
                dlqAvailable
                  ? "border-amber-400/50 bg-amber-400/10"
                  : "border-white/15",
              ].join(" ")}
              disabled={!runId}
              onClick={() => {
                setShowDlqDetails(true);
                void fetchDlq();
              }}
              title={dlqAvailable ? "DLQ available for this run" : ""}
            >
              View DLQ Payload
            </button>

            <button
              className={[
                "rounded-lg px-4 py-2 font-semibold disabled:opacity-50",
                demoMode === "running"
                  ? "bg-amber-400/30 text-amber-100"
                  : "bg-amber-300 text-black hover:bg-amber-200",
              ].join(" ")}
              onClick={runTwoMinuteDemo}
              disabled={demoMode === "running"}
              title="Runs the full story: fail → DLQ → replay fix → success"
            >
              {demoMode === "running" ? "🗂️ Running demo..." : "🗂️ 2-Minute Demo"}
            </button>

            <button
              className="rounded-lg border border-white/10 bg-black/30 px-4 py-2 text-sm text-white/80 hover:bg-white/[0.06]"
              onClick={resetAll}
              title="Stop SSE + clear everything (demo reset)"
            >
              Reset Demo
            </button>
          </div>

          <div className="text-sm text-white/70">
            Run ID:{" "}
            <span className="font-mono text-white/90">
              {runId ? runId : "(none yet)"}
            </span>
          </div>

          {/* Resolved banner after success (demo only) */}
          {showResolvedBanner ? (
            <div className="rounded-lg border border-emerald-400/25 bg-emerald-400/10 p-3 text-sm">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-medium">✅ Resolved after replay</div>
                  <div className="text-xs text-white/70 mt-1">
                    DriftQ recovered the run. DLQ stays available as the original audit trail.
                  </div>
                </div>
                <button
                  onClick={() => setShowDlqDetails((v) => !v)}
                  className="rounded-md border border-white/10 bg-black/30 px-3 py-1 text-xs text-white/80 hover:bg-white/[0.06]"
                >
                  {showDlqDetails ? "Hide DLQ" : "View original DLQ"}
                </button>
              </div>
            </div>
          ) : null}

          {/* DLQ available banner (only if not resolved) */}
          {
            runId && dlqAvailable && !dlqResolved ? (
              <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-medium">⚠️ DLQ available for this run</div>
                    <div className="text-xs text-white/70 mt-1">
                      DriftQ persisted the failure + payload. Inspect it, then replay when ready.
                    </div>
                  </div>
                  <button
                    onClick={() => {
                      setShowDlqDetails(true);
                      void fetchDlq();
                    }}
                    className="rounded-md bg-amber-300 text-black px-3 py-1 text-xs font-semibold hover:bg-amber-200"
                  >
                    Load DLQ
                  </button>
                </div>
              </div>
            ) : null
          }

          {
            runId ? (
              <div className="text-sm text-white/70">
                DLQ:{" "}
                <span className="font-mono text-white/90">
                  {dlqResolved && demoMode !== "idle"
                    ? "✅ resolved after replay"
                    : dlqStatus || "(not checked)"}
                </span>

                {/* Show payload only when expanded */}
                {showDlqDetails && dlqRecord ? (
                  <div className="mt-2">
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <div className="font-mono text-xs text-white/60">
                        Latest DLQ record for this run
                      </div>
                      <button
                        onClick={onCopyDlq}
                        className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-xs text-white/80 hover:bg-white/[0.06]"
                      >
                        Copy JSON
                      </button>
                    </div>

                    <pre className="font-mono text-xs sm:text-sm text-white/90 whitespace-pre-wrap break-words max-h-64 overflow-auto rounded-md bg-black/30 p-3 border border-white/5">
                      {JSON.stringify(dlqRecord, null, 2)}
                    </pre>
                  </div>
                ) : null}
              </div>
            ) : null
          }
        </section>

        {/* Timeline */}
        <section className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div>
              <h2 className="font-semibold">Timeline</h2>
              <p className="text-xs text-white/60">
                Showing{" "}
                <span className="font-mono text-white/80">
                  {filteredEvents.length}
                </span>{" "}
                of{" "}
                <span className="font-mono text-white/80">{events.length}</span>
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
                onClick={resetAll}
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-white/80 hover:bg-white/[0.06]"
              >
                Clear
              </button>
            </div>
          </div>

          {
            filteredEvents.length === 0
              ? <p className="text-sm text-white/60">No events match your filters.</p>
              : (
                <ul className="space-y-3">
                  {
                    filteredEvents.map((e, idx) => {
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
                    })
                  }
              </ul>
            )
          }
        </section>
      </div>
    </main>
  );
}
