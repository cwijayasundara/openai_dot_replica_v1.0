"use client";

import { useEffect, useState } from "react";

import { useLive } from "@/components/dot-live";
import { ErrorNote } from "@/components/error-note";
import { Time } from "@/components/time";
import { api, type MemoryVersion } from "@/lib/api";

const STATUS: Record<string, string> = {
  proposed: "Proposed",
  accepted: "Accepted",
  rejected: "Rejected by replay",
  rolled_back: "Rolled back",
};

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
              {version.episodes.length > 0 && (
                <p className="text-sm text-muted">From episodes {version.episodes.join(", ")}</p>
              )}
              <Diff diff={version.diff} />
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}
