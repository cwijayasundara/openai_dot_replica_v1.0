import { useCallback, useEffect, useRef, useState } from "react";

import { useLive } from "@/components/dot-live";
import { ErrorNote } from "@/components/error-note";
import { Time } from "@/components/time";
import type { AuditEvent } from "@/lib/api";

type Page = { events: AuditEvent[]; next_after_id: number | null };

const KIND: Record<string, string> = {
  tool_call: "Tool call",
  policy: "Policy",
  guardian: "Guardian",
  approval: "Approval",
};

const DECISION_STYLE: Record<string, string> = {
  allow: "text-good",
  approve: "text-wait",
  block: "text-bad",
  reject: "text-bad",
  pending: "text-wait",
};

// A tool call is recorded as it is attempted and again with its result.
function what(event: AuditEvent): string {
  const label = KIND[event.kind] ?? event.kind;
  const phase = event.detail?.phase;
  if (typeof phase !== "string") return label;
  const status = event.detail?.status;
  return `${label}, ${phase}${typeof status === "string" ? `: ${status}` : ""}`;
}

function summary(event: AuditEvent): string | null {
  const reason = event.verdict?.reason ?? event.detail?.reason;
  return typeof reason === "string" ? reason : null;
}

// The full audit trail, followed page by page; it reloads when the dot streams an event.
export function AuditLog({ load, empty }: { load: (afterId: number) => Promise<Page>; empty: string }) {
  const { tick } = useLive();
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  const seen = useRef(0);
  const reading = useRef(false);
  const again = useRef(false);

  // The log is append-only: follow it from the last row already shown.
  const readNew = useCallback(async () => {
    if (reading.current) {
      again.current = true;
      return;
    }
    reading.current = true;
    try {
      const fresh: AuditEvent[] = [];
      let after = seen.current;
      for (;;) {
        const page = await load(after);
        fresh.push(...page.events);
        if (page.events.length) after = page.events[page.events.length - 1].id;
        if (page.next_after_id === null) break;
      }
      seen.current = after;
      setEvents((current) => [...(current ?? []), ...fresh]);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      reading.current = false;
    }
    if (again.current) {
      again.current = false;
      await readNew();
    }
  }, [load]);

  useEffect(() => {
    seen.current = 0;
    setEvents(null);
  }, [load]);

  useEffect(() => {
    const timer = setTimeout(() => void readNew(), 150);
    return () => clearTimeout(timer);
  }, [readNew, tick]);

  if (error) return <ErrorNote error={error} />;
  if (events === null) return <p className="text-muted">Loading…</p>;
  if (events.length === 0) return <p className="text-muted">{empty}</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-rule text-muted">
          <tr>
            <th className="py-2 pr-4 font-normal">When</th>
            <th className="py-2 pr-4 font-normal">Who</th>
            <th className="py-2 pr-4 font-normal">What</th>
            <th className="py-2 pr-4 font-normal">Tool</th>
            <th className="py-2 pr-4 font-normal">Outcome</th>
            <th className="py-2 font-normal">Details</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule">
          {[...events].reverse().map((event) => (
            <tr key={event.id} data-testid="audit-row" data-kind={event.kind} className="align-top">
              <td className="whitespace-nowrap py-2 pr-4 text-muted">
                <Time at={event.at} />
              </td>
              <td className="py-2 pr-4">{event.actor}</td>
              <td className="py-2 pr-4">{what(event)}</td>
              <td className="py-2 pr-4 font-mono text-[13px]">{event.tool ?? ""}</td>
              <td className={`py-2 pr-4 ${DECISION_STYLE[event.decision ?? ""] ?? ""}`}>
                {event.decision ?? ""}
                {event.effect && <span className="text-muted"> ({event.effect})</span>}
              </td>
              <td className="py-2">
                {summary(event) && <p>{summary(event)}</p>}
                <details>
                  <summary className="cursor-pointer text-muted">Record</summary>
                  <pre className="mt-1 max-w-xl overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">
                    {JSON.stringify({ verdict: event.verdict, detail: event.detail }, null, 2)}
                  </pre>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
