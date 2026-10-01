"use client";

import { useCallback } from "react";

import { AuditLog } from "@/components/audit-log";
import { useLive } from "@/components/dot-live";
import { api } from "@/lib/api";

export default function SandboxPage() {
  const { dotId } = useLive();
  const load = useCallback((after: number) => api.sandbox(dotId, after), [dotId]);
  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <p className="mb-4 max-w-prose text-muted">
        What the dot&apos;s coding subagents did in its sandboxed computer: commands run and files written.
      </p>
      <AuditLog load={load} empty="The sandbox has not been used yet. Ask the dot to write and run some code." />
    </main>
  );
}
