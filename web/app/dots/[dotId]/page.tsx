"use client";

import { useEffect, useRef, useState } from "react";

import { ApprovalCard } from "@/components/approval-card";
import { useLive } from "@/components/dot-live";
import { ErrorNote } from "@/components/error-note";
import { Time } from "@/components/time";
import { api, type Job, type ThreadMessage } from "@/lib/api";

const SOURCES: Record<string, { label: string; className: string }> = {
  slack: { label: "Slack", className: "bg-slack-soft text-slack" },
  web: { label: "Web", className: "bg-dot-soft text-dot" },
  job_result: { label: "Job result", className: "bg-fog text-muted" },
  schedule: { label: "Schedule", className: "bg-fog text-muted" },
};

// The worker prefixes each inbound message with its channel: "[slack] hello".
function inbound(message: ThreadMessage): { source: string; text: string } {
  const match = /^\[([a-z_]+)\] ([\s\S]*)$/.exec(message.content);
  return match ? { source: match[1], text: match[2] } : { source: message.source ?? "web", text: message.content };
}

function Message({ message }: { message: ThreadMessage }) {
  if (message.role === "human") {
    const { source, text } = inbound(message);
    if (source === "job_result") {
      return (
        <li data-testid="thread-message" data-role="human" data-source={source} className="text-sm text-muted">
          <span className="font-medium text-ink">Job result.</span> {text}
        </li>
      );
    }
    const badge = SOURCES[source] ?? { label: source, className: "bg-fog text-muted" };
    return (
      <li data-testid="thread-message" data-role="human" data-source={source} className="flex flex-col items-end">
        <span className={`mb-1 rounded px-1.5 text-xs ${badge.className}`}>{badge.label}</span>
        <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-white px-3 py-2 shadow-[0_0_0_1px_var(--color-rule)]">
          {text}
        </p>
      </li>
    );
  }
  if (message.role === "ai" && message.tool_calls?.length && !message.content.trim()) {
    return (
      <li className="text-sm text-muted">
        Called <span className="font-mono">{message.tool_calls.join(", ")}</span>
      </li>
    );
  }
  if (message.role === "tool") {
    return (
      <li className="text-sm text-muted">
        <details>
          <summary className="cursor-pointer">
            <span className="font-mono">{message.name ?? "tool"}</span> returned
          </summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-paper p-2 font-mono text-xs">
            {message.content}
          </pre>
        </details>
      </li>
    );
  }
  if (message.role === "ai") {
    return (
      <li data-testid="thread-message" data-role="ai" className="flex gap-3">
        <span aria-hidden className="dot-mark mt-1.5 size-3 shrink-0" />
        <p className="whitespace-pre-wrap">{message.content}</p>
      </li>
    );
  }
  return null;
}

function Composer() {
  const { dotId, refresh } = useLive();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function send(event: { preventDefault(): void }) {
    event.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.send(dotId, text.trim());
      setText("");
      setError(null);
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={send} className="sticky bottom-0 border-t border-rule bg-fog pb-4 pt-3">
      <ErrorNote error={error} />
      <div className="flex gap-2">
        <label htmlFor="composer" className="sr-only">
          Message your dot
        </label>
        <textarea
          id="composer"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) void send(e);
          }}
          rows={2}
          placeholder="Ask your dot to research, draft or build something"
          className="flex-1 resize-none rounded-md border border-rule bg-white px-3 py-2"
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          className="self-end rounded-md bg-dot px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          Send
        </button>
      </div>
    </form>
  );
}

const JOB_STATUS: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  paused: "Waiting on approval",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

function JobRow({ job }: { job: Job }) {
  const open = job.status === "queued" || job.status === "running";
  return (
    <li data-testid="job" className="py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium">{job.subagent}</span>
        <span className={`text-sm ${open ? "text-dot" : job.status === "failed" ? "text-bad" : "text-muted"}`}>
          {JOB_STATUS[job.status] ?? job.status}
        </span>
      </div>
      <p className="line-clamp-2 text-sm text-muted">{job.instructions}</p>
      {job.error && <p className="text-sm text-bad">{job.error}</p>}
      <p className="text-xs text-muted">
        <Time at={job.finished_at ?? job.created_at} />
      </p>
    </li>
  );
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="mt-6 first:mt-0">
      <h2 className="flex items-baseline gap-2 font-semibold">
        {title}
        {count !== undefined && count > 0 && <span className="text-sm font-normal text-muted">{count}</span>}
      </h2>
      {children}
    </section>
  );
}

export default function DotHome() {
  const { thread, jobs, approvals, findings, error, refresh } = useLive();
  const end = useRef<HTMLLIElement>(null);
  const pending = approvals.filter((a) => a.status === "pending");
  const decided = approvals.filter((a) => a.status !== "pending").slice(0, 5);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [thread.length]);

  return (
    <main className="mx-auto grid max-w-6xl gap-8 px-4 py-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <aside className="lg:order-2">
        <Section title="Waiting on you" count={pending.length}>
          {pending.length === 0 ? (
            <p className="mt-1 text-sm text-muted">Nothing needs your decision.</p>
          ) : (
            <div className="mt-2 space-y-3">
              {pending.map((card) => (
                <ApprovalCard key={card.approval_id} card={card} onDecided={() => void refresh()} />
              ))}
            </div>
          )}
        </Section>

        <Section title="Background jobs" count={jobs.length}>
          {jobs.length === 0 ? (
            <p className="mt-1 text-sm text-muted">No jobs yet. Long research and coding run here.</p>
          ) : (
            <ul className="divide-y divide-rule">
              {jobs.slice(0, 8).map((job) => (
                <JobRow key={job.job_id} job={job} />
              ))}
            </ul>
          )}
        </Section>

        <Section title="Findings" count={findings.filter((f) => f.status === "open").length}>
          {findings.length === 0 ? (
            <p className="mt-1 text-sm text-muted">Sweeps record what they notice here.</p>
          ) : (
            <ul className="divide-y divide-rule">
              {findings.slice(0, 8).map((finding) => (
                <li key={finding.id} className="py-2">
                  <p className="font-medium">{finding.title}</p>
                  <p className="text-xs text-muted">
                    {finding.schedule}, score {finding.score.toFixed(2)}, {finding.status}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {decided.length > 0 && (
          <Section title="Recent decisions">
            <div className="mt-2 space-y-3">
              {decided.map((card) => (
                <ApprovalCard key={card.approval_id} card={card} onDecided={() => void refresh()} />
              ))}
            </div>
          </Section>
        )}
      </aside>

      <div className="flex min-h-[60vh] flex-col lg:order-1">
        <ErrorNote error={error} />
        {thread.length === 0 ? (
          <p className="flex-1 py-10 text-muted">
            This is the start of the thread. Messages from here and from Slack land in the same place.
          </p>
        ) : (
          <ol aria-label="Thread" className="flex-1 space-y-4 py-2">
            {thread.map((message, index) => (
              <Message key={index} message={message} />
            ))}
            <li ref={end} aria-hidden />
          </ol>
        )}
        <Composer />
      </div>
    </main>
  );
}
