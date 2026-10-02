"use client";

import { useEffect, useState } from "react";

import { useLive } from "@/components/dot-live";
import { ErrorNote } from "@/components/error-note";
import { Time } from "@/components/time";
import { ApiError, api, type MemoryVersion } from "@/lib/api";

const STATUS: Record<string, string> = {
  proposed: "Proposed, waiting for replay",
  accepted: "Accepted",
  rejected: "Rejected by replay",
  needs_review: "Held for your review",
  discarded: "Discarded",
  rolled_back: "Rolled back",
};

type Action = "rollback" | "accept" | "discard";

function Actions({ version, onDone }: { version: MemoryVersion; onDone: (updated: MemoryVersion) => void }) {
  const { dotId } = useLive();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(action: Action) {
    setBusy(true);
    setError(null);
    try {
      onDone(await api.memoryAction(dotId, version.id, action));
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setError("Only a pack approver can change the dot's memory.");
      else setError(err instanceof Error ? err.message : "The change was not saved.");
    } finally {
      setBusy(false);
    }
  }

  const buttons: { action: Action; label: string; primary?: boolean }[] =
    version.status === "accepted"
      ? [{ action: "rollback", label: "Roll back" }]
      : version.status === "needs_review"
        ? [
            { action: "accept", label: "Accept", primary: true },
            { action: "discard", label: "Discard" },
          ]
        : [];
  if (buttons.length === 0) return null;
  return (
    <div className="mt-3">
      {error && (
        <p role="alert" className="mb-2 text-sm text-bad">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        {buttons.map(({ action, label, primary }) => (
          <button
            key={action}
            type="button"
            disabled={busy}
            onClick={() => void act(action)}
            className={
              primary
                ? "rounded-md bg-dot px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
                : "rounded-md border border-rule bg-white px-3 py-1 text-sm disabled:opacity-50"
            }
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-muted";
  if (line.startsWith("@@")) return "text-dot";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "";
}

function Diff({ diff }: { diff: string }) {
  return (
    <pre className="mt-3 overflow-x-auto rounded-md border border-rule bg-white py-2 font-mono text-[13px] leading-relaxed">
      {diff.split("\n").map((line, index) => (
        <span key={index} className={`block px-3 ${lineClass(line)}`}>
          {line || " "}
        </span>
      ))}
    </pre>
  );
}

export default function MemoryPage() {
  const { dotId, tick } = useLive();
  const [versions, setVersions] = useState<MemoryVersion[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.memory(dotId).then(setVersions, setError);
  }, [dotId, tick]);

  return (
    <main className="mx-auto max-w-4xl px-4 py-6">
      <p className="mb-6 max-w-prose text-muted">
        Changes the dot made to its own preferences, wiki and skills after reflecting on your approvals and edits.
        Each change cites the episodes behind it.
      </p>
      <ErrorNote error={error} />
      {versions === null ? (
        <p className="text-muted">Loading…</p>
      ) : versions.length === 0 ? (
        <p className="text-muted">
          No memory changes yet. They appear after the nightly reflection has a day of decisions to learn from.
        </p>
      ) : (
        <ol className="space-y-8">
          {versions.map((version) => (
            <li key={version.id} data-testid="memory-version">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <h2 className="font-semibold">Version {version.id}</h2>
                <span className="text-sm">{STATUS[version.status] ?? version.status}</span>
                <span className="text-sm text-muted">
                  <Time at={version.at} />
                </span>
              </div>
              {version.detail.rationale && <p className="mt-1 max-w-prose">{version.detail.rationale}</p>}
              {version.detail.gate && (
                <p className="text-sm text-muted">Replay: {version.detail.gate.reason}</p>
              )}
              {version.episodes.length > 0 && (
                <p className="text-sm text-muted">From episodes {version.episodes.join(", ")}</p>
              )}
              <Diff diff={version.diff} />
              <Actions
                version={version}
                onDone={(updated) => setVersions((all) => all?.map((v) => (v.id === updated.id ? updated : v)) ?? null)}
              />
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}
