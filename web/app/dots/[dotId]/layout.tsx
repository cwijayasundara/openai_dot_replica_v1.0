"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

import { DotLive, useLive, type DotState } from "@/components/dot-live";

const TABS = [
  { href: "", label: "Thread" },
  { href: "/audit", label: "Audit trail" },
  { href: "/memory", label: "Memory" },
  { href: "/sandbox", label: "Sandbox" },
];

const STATE_TEXT: Record<DotState, string> = {
  idle: "Idle",
  working: "Working",
  waiting: "Waiting on you",
};

function Header() {
  const { dotId, dot, state, connected } = useLive();
  const pathname = usePathname();
  const base = `/dots/${dotId}`;
  return (
    <div className="border-b border-rule bg-paper">
      <div className="mx-auto max-w-6xl px-4 pt-6">
        <div className="flex items-center gap-4">
          <span aria-hidden data-state={state} className="dot-mark size-10 shrink-0" />
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold tracking-tight">{dot?.pack_name ?? "Dot"}</h1>
            <p className="text-sm text-muted" aria-live="polite">
              <span data-testid="dot-state">{STATE_TEXT[state]}</span>
              {!connected && <span> (reconnecting to live updates)</span>}
            </p>
          </div>
        </div>
        <nav aria-label="Dot sections" className="-mb-px mt-5 flex gap-1 overflow-x-auto">
          {TABS.map((tab) => {
            const href = base + tab.href;
            const active = pathname === href;
            return (
              <Link
                key={tab.label}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm ${
                  active ? "border-dot font-medium text-ink" : "border-transparent text-muted hover:text-ink"
                }`}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </div>
  );
}

export default function DotLayout({ children }: { children: React.ReactNode }) {
  const { dotId } = useParams<{ dotId: string }>();
  return (
    <DotLive dotId={dotId}>
      <Header />
      {children}
    </DotLive>
  );
}
