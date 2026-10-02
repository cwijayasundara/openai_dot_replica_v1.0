"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorNote } from "@/components/error-note";
import { ApiError, api, type Dot, type Pack } from "@/lib/api";

export default function DotsPage() {
  const router = useRouter();
  const [user, setUser] = useState<string | null>(null);
  const [dots, setDots] = useState<Dot[] | null>(null);
  const [packs, setPacks] = useState<Pack[]>([]);
  const [pack, setPack] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    Promise.all([api.me(), api.dots(), api.packs()])
      .then(([me, mine, available]) => {
        setUser(me.user_id);
        setDots(mine);
        setPacks(available);
        setPack(available[0]?.name ?? "");
      })
      .catch(setError);
  }, []);

  async function create(event: { preventDefault(): void }) {
    event.preventDefault();
    if (!user || !pack) return;
    setCreating(true);
    try {
      const dot = await api.createDot(pack, user);
      router.push(`/dots/${dot.dot_id}`);
    } catch (err) {
      setError(err);
      setCreating(false);
    }
  }

  if (error instanceof ApiError && error.status === 401) {
    return (
      <main className="mx-auto max-w-2xl px-4 py-12">
        <h1 className="text-2xl font-semibold">Sign-in required</h1>
        <p className="mt-2 text-muted">
          The API did not receive a verified user. Locally, start it with{" "}
          <code className="font-mono">DOT_WEB_AUTH=dev</code> and <code className="font-mono">DOT_WEB_DEV_USER</code>{" "}
          set to your user id.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="text-3xl font-semibold tracking-tight">Your dots</h1>
      <p className="mt-1 text-muted">Each dot is one agent with one continuous thread, here and in Slack.</p>
      <ErrorNote error={error} />

      {dots === null ? (
        <p className="mt-8 text-muted">Loading…</p>
      ) : dots.length === 0 ? (
        <p className="mt-8">You have no dots yet. Create one from a pack below.</p>
      ) : (
        <ul className="mt-8 divide-y divide-rule border-y border-rule">
          {dots.map((dot) => (
            <li key={dot.dot_id}>
              <Link
                href={`/dots/${dot.dot_id}`}
                className="flex items-center gap-4 py-4 hover:bg-paper focus-visible:bg-paper"
              >
                <span aria-hidden className="dot-mark size-5 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{dot.pack_name}</span>
                  <span className="block truncate font-mono text-xs text-muted">{dot.dot_id}</span>
                </span>
                <span className="text-sm text-muted">{dot.status}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={create} className="mt-10 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Pack
          <select
            value={pack}
            onChange={(e) => setPack(e.target.value)}
            className="min-w-56 rounded-md border border-rule bg-paper px-3 py-2 text-base"
          >
            {packs.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          disabled={!pack || !user || creating}
          className="rounded-md bg-dot px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          {creating ? "Creating…" : "Create dot"}
        </button>
      </form>
    </main>
  );
}
