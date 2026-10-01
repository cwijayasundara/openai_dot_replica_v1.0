"use client";

import { useCallback } from "react";

import { AuditLog } from "@/components/audit-log";
import { useLive } from "@/components/dot-live";
import { api } from "@/lib/api";

export default function AuditPage() {
  const { dotId } = useLive();
  const load = useCallback((after: number) => api.audit(dotId, after), [dotId]);
  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <p className="mb-4 max-w-prose text-muted">
        Every tool call, policy decision, Guardian verdict and approval, newest first. The log is append-only.
      </p>
      <AuditLog load={load} empty="Nothing has happened yet." />
    </main>
  );
}
