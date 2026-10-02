"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { api, eventsUrl, type Approval, type Dot, type Finding, type Job, type ThreadMessage } from "@/lib/api";

// Kinds the API streams (dot.runtime.turns.EventKind). Each one means "something changed".
const KINDS = ["message", "tool_call", "interrupt", "approval", "job_started", "job_finished", "memory", "error"];

export type DotState = "idle" | "working" | "waiting";

type Live = {
  dotId: string;
  dot: Dot | null;
  thread: ThreadMessage[];
  jobs: Job[];
  approvals: Approval[];
  findings: Finding[];
  connected: boolean;
  // Bumps on every streamed event, for pages that load their own data.
  tick: number;
  error: unknown;
  state: DotState;
  refresh: () => Promise<void>;
};

const LiveContext = createContext<Live | null>(null);

export function useLive(): Live {
  const live = useContext(LiveContext);
  if (!live) throw new Error("useLive must be used inside DotLive");
  return live;
}

function stateOf(thread: ThreadMessage[], jobs: Job[], approvals: Approval[]): DotState {
  if (approvals.some((a) => a.status === "pending")) return "waiting";
  if (jobs.some((j) => j.status === "queued" || j.status === "running")) return "working";
  const last = thread.at(-1);
  if (last && (last.role === "human" || last.role === "tool" || (last.tool_calls?.length ?? 0) > 0)) return "working";
  return "idle";
}

export function DotLive({ dotId, children }: { dotId: string; children: React.ReactNode }) {
  const [dot, setDot] = useState<Dot | null>(null);
  const [thread, setThread] = useState<ThreadMessage[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [connected, setConnected] = useState(false);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, t, j, a, f] = await Promise.all([
        api.dot(dotId),
        api.thread(dotId),
        api.jobs(dotId),
        api.approvals(dotId),
        api.findings(dotId),
      ]);
      setDot(d);
      setThread(t);
      setJobs(j);
      setApprovals(a);
      setFindings(f);
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [dotId]);

  useEffect(() => {
    void refresh();
    const source = new EventSource(eventsUrl(dotId));
    // Events arrive in bursts during a turn; reload once the burst settles.
    const changed = () => {
      setTick((n) => n + 1);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void refresh(), 150);
    };
    source.onopen = () => {
      setConnected(true);
      // Events published while disconnected are not replayed; reload what may have changed.
      changed();
    };
    source.onerror = () => setConnected(false);
    for (const kind of KINDS) source.addEventListener(kind, changed);
    return () => {
      source.close();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [dotId, refresh]);

  const value: Live = {
    dotId,
    dot,
    thread,
    jobs,
    approvals,
    findings,
    connected,
    tick,
    error,
    state: stateOf(thread, jobs, approvals),
    refresh,
  };
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
