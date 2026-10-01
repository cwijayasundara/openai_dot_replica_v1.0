import { useState } from "react";

import { ApiError, api, type Approval, type Decision } from "@/lib/api";

const OUTCOME: Record<string, string> = {
  approve: "Approved",
  edit: "Approved with edits",
  reject: "Rejected",
  cancelled: "Cancelled",
};

function show(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function parse(original: unknown, text: string): unknown {
  // Strings stay strings; anything else is edited as JSON.
  return typeof original === "string" ? text : JSON.parse(text);
}

function Args({ args }: { args: Record<string, unknown> }) {
  return (
    <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
      {Object.entries(args).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted">{key}</dt>
          <dd className="min-w-0 whitespace-pre-wrap break-words font-mono text-[13px]">{show(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Changes({ before, after }: { before: Record<string, unknown>; after: Record<string, unknown> }) {
  const keys = Object.keys({ ...before, ...after }).filter((k) => show(before[k]) !== show(after[k]));
  if (keys.length === 0) return <p className="mt-2 text-sm text-muted">No arguments changed.</p>;
  return (
    <dl className="mt-3 space-y-2 text-sm">
      {keys.map((key) => (
        <div key={key}>
          <dt className="text-muted">{key}</dt>
          <dd className="diff-del whitespace-pre-wrap break-words px-2 font-mono text-[13px] line-through">
            {show(before[key])}
          </dd>
          <dd className="diff-add whitespace-pre-wrap break-words px-2 font-mono text-[13px]">{show(after[key])}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ApprovalCard({ card, onDecided }: { card: Approval; onDecided: () => void }) {
  const [mode, setMode] = useState<"view" | "edit" | "reject">("view");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = card.status === "pending";

  function startEdit() {
    setDraft(Object.fromEntries(Object.entries(card.args).map(([k, v]) => [k, show(v)])));
    setMode("edit");
  }

  async function decide(decision: Decision) {
    setBusy(true);
    setError(null);
    try {
      await api.decide(card.approval_id, decision);
      onDecided();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setError("You are not an approver for this dot's pack.");
      else if (err instanceof ApiError && err.status === 409) setError("This action was already decided elsewhere.");
      else setError(err instanceof Error ? err.message : "The decision was not saved.");
      setBusy(false);
    }
  }

  function submitEdit() {
    let edited: Record<string, unknown>;
    try {
      edited = Object.fromEntries(Object.entries(draft).map(([k, text]) => [k, parse(card.args[k], text)]));
    } catch {
      setError("One of the edited values is not valid JSON.");
      return;
    }
    void decide({ type: "edit", edited_args: edited });
  }

  return (
    <article
      aria-label={`Approval for ${card.tool}`}
      data-testid="approval-card"
      className={`rounded-lg border p-4 ${pending ? "border-wait bg-wait-soft" : "border-rule bg-paper"}`}
    >
      <header className="flex items-baseline justify-between gap-3">
        <h3 className="font-medium">
          <span className="font-mono">{card.tool}</span>
          {card.job_id && <span className="text-sm font-normal text-muted"> in a background job</span>}
        </h3>
        <span className={`text-sm ${pending ? "font-medium text-wait" : "text-muted"}`}>
          {pending ? "Needs your decision" : (OUTCOME[card.status] ?? card.status)}
        </span>
      </header>

      {mode === "edit" ? (
        <div className="mt-3 space-y-3">
          {Object.entries(card.args).map(([key, value]) => (
            <label key={key} className="block text-sm">
              <span className="text-muted">{key}</span>
              <span className="mt-1 block whitespace-pre-wrap break-words font-mono text-[13px] text-muted">
                Current: {show(value)}
              </span>
              <textarea
                value={draft[key] ?? ""}
                onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                rows={Math.min(8, Math.max(1, (draft[key] ?? "").split("\n").length))}
                className="mt-1 w-full rounded-md border border-rule bg-white px-2 py-1 font-mono text-[13px]"
              />
            </label>
          ))}
        </div>
      ) : card.edit?.edited_args ? (
        <Changes before={card.args} after={card.edit.edited_args} />
      ) : (
        <Args args={card.args} />
      )}

      {mode === "reject" && (
        <label className="mt-3 block text-sm">
          <span className="text-muted">Tell the dot why (optional)</span>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="mt-1 w-full rounded-md border border-rule bg-white px-2 py-1"
          />
        </label>
      )}

      {!pending && card.decided_by && (
        <p className="mt-3 text-sm text-muted">
          Decided by {card.decided_by}
          {card.edit?.message ? `: “${card.edit.message}”` : ""}
        </p>
      )}

      {error && (
        <p role="alert" className="mt-3 text-sm text-bad">
          {error}
        </p>
      )}

      {pending && (
        <div className="mt-4 flex flex-wrap gap-2">
          {mode === "view" && (
            <>
              <button
                disabled={busy}
                onClick={() => void decide({ type: "approve" })}
                className="rounded-md bg-ink px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                Approve
              </button>
              <button
                disabled={busy}
                onClick={startEdit}
                className="rounded-md border border-ink px-3 py-1.5 text-sm disabled:opacity-50"
              >
                Edit
              </button>
              <button
                disabled={busy}
                onClick={() => setMode("reject")}
                className="rounded-md px-3 py-1.5 text-sm text-bad disabled:opacity-50"
              >
                Reject
              </button>
            </>
          )}
          {mode === "edit" && (
            <>
              <button
                disabled={busy}
                onClick={submitEdit}
                className="rounded-md bg-ink px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                Approve with edits
              </button>
              <button onClick={() => setMode("view")} className="rounded-md px-3 py-1.5 text-sm">
                Cancel
              </button>
            </>
          )}
          {mode === "reject" && (
            <>
              <button
                disabled={busy}
                onClick={() => void decide(reason ? { type: "reject", message: reason } : { type: "reject" })}
                className="rounded-md bg-bad px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                Reject
              </button>
              <button onClick={() => setMode("view")} className="rounded-md px-3 py-1.5 text-sm">
                Cancel
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}
