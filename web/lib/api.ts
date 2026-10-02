// Typed calls to the dot API. Every path goes through the /api proxy (next.config.ts).

export type Dot = {
  dot_id: string;
  owner_user_id: string;
  pack_name: string;
  pack_version: string;
  thread_id: string;
  status: string;
  created_at: string;
};

export type Pack = { name: string; profiles: string[]; subagents: string[] };

export type ThreadMessage = {
  role: "human" | "ai" | "tool" | "system" | string;
  // Set on AI messages; a correction names the message it corrects.
  id?: string;
  content: string;
  source?: string;
  name?: string;
  tool_calls?: string[];
};

export type Job = {
  job_id: string;
  subagent: string;
  status: string;
  profile: string;
  instructions: string;
  updates: number;
  result_ref: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Approval = {
  approval_id: string;
  tool: string;
  args: Record<string, unknown>;
  status: string;
  job_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  edit: { type: string; edited_args?: Record<string, unknown>; message?: string } | null;
  allowed_decisions: string[];
};

export type Finding = {
  id: number;
  schedule: string;
  title: string;
  evidence: Record<string, unknown>;
  score: number;
  status: string;
  created_at: string;
};

export type AuditEvent = {
  id: number;
  at: string;
  actor: string;
  kind: string;
  tool: string | null;
  effect: string | null;
  decision: string | null;
  verdict: Record<string, unknown> | null;
  detail: Record<string, unknown> | null;
};

export type MemoryVersion = {
  id: number;
  at: string;
  diff: string;
  episodes: number[];
  status: string;
  // The file, the rationale, and later the replay results.
  detail: {
    path?: string;
    rationale?: string;
    gate?: { reason: string; results: { episode: number; arm: string; match: boolean; stop: string }[] };
  } & Record<string, unknown>;
};

export type Decision =
  | { type: "approve" }
  | { type: "reject"; message?: string }
  | { type: "edit"; edited_args: Record<string, unknown> };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not JSON: keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  me: () => call<{ user_id: string }>("/me"),
  packs: () => call<{ packs: Pack[] }>("/packs").then((r) => r.packs),
  dots: () => call<{ dots: Dot[] }>("/dots").then((r) => r.dots),
  dot: (id: string) => call<Dot>(`/dots/${id}`),
  createDot: (pack: string, owner: string) => call<Dot>("/dots", post({ pack, owner_user_id: owner })),
  thread: (id: string) => call<{ messages: ThreadMessage[] }>(`/dots/${id}/thread`).then((r) => r.messages),
  send: (id: string, text: string) => call<{ id: number }>(`/dots/${id}/messages`, post({ text })),
  jobs: (id: string) => call<{ jobs: Job[] }>(`/dots/${id}/jobs`).then((r) => r.jobs),
  approvals: (id: string) => call<{ approvals: Approval[] }>(`/dots/${id}/approvals`).then((r) => r.approvals),
  findings: (id: string) => call<{ findings: Finding[] }>(`/dots/${id}/findings`).then((r) => r.findings),
  memory: (id: string) => call<{ versions: MemoryVersion[] }>(`/dots/${id}/memory`).then((r) => r.versions),
  audit: (id: string, afterId = 0) =>
    call<{ events: AuditEvent[]; next_after_id: number | null }>(`/dots/${id}/audit?after_id=${afterId}&limit=200`),
  sandbox: (id: string, afterId = 0) =>
    call<{ events: AuditEvent[]; actors: string[]; next_after_id: number | null }>(
      `/dots/${id}/sandbox?after_id=${afterId}&limit=200`,
    ),
  correct: (id: string, messageId: string, text: string) =>
    call<{ episode_id: number }>(`/dots/${id}/corrections`, post({ message_id: messageId, text })),
  memoryAction: (id: string, versionId: number, action: "rollback" | "accept" | "discard") =>
    call<MemoryVersion>(`/dots/${id}/memory/${versionId}/${action}`, post({})),
  decide: (approvalId: string, decision: Decision) =>
    call<{ status: string }>(`/approvals/${approvalId}`, post(decision)),
};

export function eventsUrl(dotId: string): string {
  return `/api/dots/${dotId}/events`;
}
