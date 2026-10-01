# Implementation plan: open dot runtime v1

Audience: the engineer (and Claude Code) implementing v1. Work top to bottom. Each task lists files, steps and an **acceptance check** that must pass before moving on. The design and its decisions are in `docs/design.md`; don't re-decide them here.

---

## 0. Ground rules

- **Agents propose, code decides, humans approve.** Policy, gates, approvals, budgets and credential resolution are code. No prompt is ever the only thing enforcing a rule.
- **One assembly.** `src/dot/assembly.py` builds models, tools, middleware and agents for the API, worker, CLI and tests. Don't fork it per surface.
- **Scripted model in CI.** Graph and agent behaviour is tested with a scripted chat model (port `tests/support/scripted_model.py` from recon v2). Live models only under the `live` marker.
- **Tools return compact JSON plus artifact ids,** never raw documents or rows.
- **No secrets in code, fixtures or prompts.** `.env.example` holds names only.
- Small diffs, type hints everywhere, pydantic v2 at boundaries, frozen dataclasses inside.
- Don't commit, push, deploy or call live models unless asked.

**Reuse from `../recon_knowledge_work_agent_v2`** (copy, then adapt; don't import across repos): `sandbox/base.py`, `sandbox/docker_backend.py`, `middleware/guard.py`, `middleware/offload.py`, `middleware/redaction.py`, `tests/support/scripted_model.py`, the `assembly.py` pattern, `sandbox/Dockerfile`, and `tests/sandbox/test_sandbox_contract.py`.

---

## 1. Definition of done for v1

A user creates a dot from the `research-analyst` pack in the web UI and links their Slack account. They ask it, in Slack, to research a topic and draft an email to a colleague. The dot replies at once, starts a background research job, and keeps answering other messages. When the job finishes it posts the brief in the same Slack thread and proposes the email; `send_email` pauses for approval, and the user approves in Slack. The same thread is visible in the web UI, with the job, the Guardian verdicts and the approval in the audit log.

The dot's 30-minute sweep runs read-only and writes findings; the morning digest summarises the ones worth attention. After a day of approvals and edits, the nightly reflection proposes a change to `AGENTS.md` that passes the replay gate and shows as a diff. A code-writing request runs in the sandbox (Docker locally, OpenShell when configured).

Everything runs locally with `docker compose` and a Fireworks API key. Phase 11 deploys the same build to GCP.

---

## 2. Target repository layout

```
openai_dot_replica_v1.0/
  CLAUDE.md  pyproject.toml  uv.lock  docker-compose.yml  .env.example
  docs/                         design.md, implementation-plan.md, reference/
  config/mcp.yaml               MCP servers and tool → effect mapping
  packs/
    research-analyst/{pack.yaml,persona.md,policy.yaml,skills/,wiki/}
  src/dot/
    __init__.py config.py models.py assembly.py observability.py
    packs/{schema.py,loader.py}
    runtime/{router.py,worker.py,locks.py,turns.py}
    jobs/{store.py,tools.py,runner.py}
    tools/{registry.py,effects.py,native/*.py,mcp.py,discovery.py}
    safety/{policy.py,guardian.py,approvals.py,audit.py,credentials.py}
    middleware/{guard.py,policy.py,offload.py,redaction.py}
    sandbox/{base.py,docker_backend.py,openshell_backend.py}
    memory/{store.py,episodes.py,reflection.py,replay.py,versions.py}
    proactive/{scheduler.py,findings.py}
    channels/{base.py,web.py,slack.py}
    persistence/{db.py,migrations/001_init.sql}
    surfaces/{api.py,sse.py,cli.py}
  sandbox/Dockerfile  sandbox/policies/{default.yaml,research.yaml}
  web/                          Next.js UI
  tests/{unit,contract,sandbox,safety,memory,e2e,live}/  tests/support/
  infra/                        Terraform (Phase 11)
```

**Pinned stack.** Python 3.12 · `deepagents` 0.7.x (exact pin) · `langchain~=1.4` · `langgraph~=1.2` · `langchain-fireworks` (current) · `langchain-openai~=1.6` · `langchain-mcp-adapters` · `langgraph-checkpoint-postgres~=3.1` · `psycopg[binary,pool]~=3.3` · `pydantic~=2.13` · `pydantic-settings` · `fastapi` · `uvicorn` · `sse-starlette` · `slack-bolt` · `apscheduler` · `docker` · `openshell` · `opentelemetry-sdk` · `httpx`. Web: Next.js 16, React 19, TypeScript, Tailwind v4.

---

## Phase 1: Foundations (3 days)

### F1. Scaffold the repository
- Create the layout above: `uv` project, ruff, mypy, pytest markers `live`, `docker`, `db`, `openshell`.
- `docker-compose.yml`: `postgres` (pgvector/pgvector:pg17, port 55434), `api`, `worker`, `web`, and a build-only `sandbox` image.
- `CLAUDE.md` pointing at `docs/design.md` and this plan, with the ground rules from section 0.
- CI (GitHub Actions): lint, type-check, offline tests.

**Acceptance:** `uv sync` works; `uv run pytest -q` is green with zero tests; CI runs lint and type-check.

### F2. Settings and model factory
- `config.py` (`pydantic-settings`, prefix `DOT_`): `MODEL_PROVIDER` (`fireworks` | `openai_compatible`), `FIREWORKS_API_KEY`, `OPENAI_BASE_URL`, `SUPERVISOR_MODEL`, `HEAVY_MODEL`, `FAST_MODEL`, `DATABASE_URL`, `OBJECT_ROOT`, `SANDBOX_BACKEND` (`docker` | `openshell`), `SANDBOX_IMAGE`, `OPENSHELL_GATEWAY`, `SANDBOX_IDLE_S`, `MAX_MODEL_CALLS`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`.
- `models.py::chat_model(role)` returns `ChatFireworks` or `ChatOpenAI(base_url=…)` per role, with timeouts and retries.
- `tests/live/test_model_smoke.py`: for each role, a Deep Agent with one tool (`add`) must call it and answer.
- `tests/live/test_reasoning_roundtrip.py`: a three-step tool loop on `kimi-k3` and `glm-5p3`; assert reasoning content is present on every assistant message in the checkpointed history. If it isn't, fix it here (upgrade, or the OpenAI-compatible path with reasoning passthrough) before any other phase.

**Acceptance:** both live tests pass on all three models; unit tests cover settings for both providers.

**Status (2026-10-01):** verified live on Fireworks.
- **Model smoke test:** passes for all three roles.
- **The gate failed first and exposed a real integration bug.** `langchain-fireworks` 1.7.0 reads `reasoning_content` from responses but never sends it back on assistant history. Its stream parser drops reasoning deltas too.
  - A raw-API probe showed Fireworks accepts `reasoning_content` in history.
  - With it stripped, Kimi K3 stopped reasoning after the first tool turn. With it kept, Kimi reasoned on every turn.
  - GLM-5.3 skips reasoning on trivial steps either way, so that is how the model behaves.
- **Fix:** `models.ReasoningChatFireworks` re-attaches the reasoning and disables token streaming.
- **The gate test changed meaning.** For both models it now checks that every reasoning trace produced is sent back on the next request. It requires reasoning on every turn for Kimi only. Both pass.
- **Not covered:** the `openai_compatible` provider path likely drops reasoning the same way; it is untested. With streaming disabled, C2 gets no token-by-token output.

### F3. Persistence
- `persistence/migrations/001_init.sql`: the tables in design section 9.
- `persistence/db.py`: pool, migrations runner, typed repositories.
- LangGraph `PostgresSaver` and `PostgresStore` built in `assembly.py`; in-memory versions when `DATABASE_URL` is unset.

**Acceptance:** `db`-marked tests create, read and update every table; the in-memory path passes the same repository contract tests.

---

## Phase 2: The dot core (5 days)

### D1. Pack schema and loader
- `packs/schema.py`: pydantic models for `pack.yaml` and `policy.yaml` (design 4.1 and 5.2).
- `packs/loader.py`: validates, resolves tool names against the registry, seeds `skills/` and `wiki/` into the store for a new dot.
- The `research-analyst` pack: persona, three skills (`research-method`, `brief-writing`, `email-drafting`), a small wiki, policy.

**Acceptance:** the pack loads; tests prove that an unknown tool, an untagged tool, and a profile granting a blocked effect each fail with a clear message.

### D2. Tool registry and effects
- `tools/effects.py`: the `Effect` enum. `tools/registry.py`: register native tools with an effect; `for_profile(profile)` filters by name and effect.
- Native tools for v1: `web_search`, `fetch_url` (read), `write_report` (draft), `draft_email` (draft), `send_email` (external, SMTP or Gmail API behind the credential broker), `slack_post` (external).
- Fetched content is wrapped in an untrusted-data envelope.

**Acceptance:** unit tests for registration, filtering and the envelope; every native tool has an effect.

### D3. Assembly and the supervisor
- `assembly.py`: `build_dot_agent(dot, profile)` as in design 4.2, with sandbox, store routes and the middleware chain (surface guard and offload/redaction first; policy and Guardian are wired in Phase 4).
- Port `SurfaceGuard` from recon's `InvocationGuardMiddleware`.

**Acceptance:** with the scripted model, a chat turn calls a tool and answers; a tool outside the profile is neither offered nor callable.

### D4. Runtime: inbox, worker, one thread per dot
- `runtime/router.py`: `enqueue(dot_id, source, payload, profile)`.
- `runtime/worker.py`: claims rows with `SKIP LOCKED`, takes a per-dot advisory lock, folds all pending messages for the dot into one turn, runs the agent on `dot.thread_id`, emits events.
- `runtime/turns.py`: turn events (message, tool call, interrupt, job started, error) to an `events` channel for SSE and Slack.

**Acceptance:** contract test: three messages sent while a turn runs are handled in the next turn, in order, with no concurrent run for the same dot; two dots run in parallel.

### D5. API and CLI
- `surfaces/api.py`: `POST /dots`, `GET /dots/{id}`, `POST /dots/{id}/messages`, `GET /dots/{id}/events` (SSE), `GET /dots/{id}/thread`.
- `surfaces/cli.py`: `dot create`, `dot run`, `dot tail`.

**Acceptance:** API contract tests with the scripted model; `dot run research-analyst "hello"` works against a live model.

**Status (2026-10-01):** `tests/live/test_cli_run.py` passes against Fireworks.

---

## Phase 3: Sandbox (3 days)

### X1. Port the seam and Docker backend
- Copy `RunSandbox`, `LazySandbox`, `DockerSandbox` from recon v2. Key sandboxes by `dot_id` (a job may use its own key). Mounts: `/in` read-only, `/skills` read-only, `/work` writable.
- `sandbox/Dockerfile`: Python 3.12 slim, ripgrep, pandas, duckdb, openpyxl, jinja2; non-root `runner`.
- Idle suspend: stop the container after `SANDBOX_IDLE_S`; snapshot `/work` to object storage; restore on next use.

**Acceptance:** the ported contract tests pass under `docker`; a suspend and restore keeps `/work` intact.

### X2. OpenShell backend
- `sandbox/openshell_backend.py`: `OpenShellSandbox(RunSandbox)`. Port exec and file calls from `langchain-ai/openshell-deepagent`'s `OpenShellBackend`; create with the policy for the profile, copy inputs and skills, seal read-only.
- `sandbox/policies/default.yaml` (deny all egress, same filesystem contract as Docker) and `research.yaml` (read-only egress to allowlisted hosts).

**Acceptance:** the same contract tests pass under `openshell` (marker `openshell`); an egress attempt outside policy fails with OpenShell's descriptive error, and the audit event is recorded.

**Status (2026-10-01):** verified against a local OpenShell 0.1.2 gateway (Homebrew, Docker driver).

The first gateway run found three bugs that offline tests had not caught. All are fixed:
- Sandbox names were too long; the gateway allows at most 19 characters.
- `run_as_user`/`run_as_group` must be numeric (`"1000"`) or `sandbox`.
- An explicit `tls: terminate` is rejected; the field is now omitted.

What the contract tests now verify:
- The shared filesystem and execution contract passes.
- An allowed GET succeeds.
- A disallowed method on an allowed host returns OpenShell's descriptive `policy_denied` and is audited as a `sandbox_denial` block.
- A host outside the policy is refused at connect time. OpenShell's OCSF log records it (`NET:REFUSE … DENIED example.com`), and the test asserts that log entry.

**Known gap for E3:** for a host outside the policy, the command output says only "could not connect", so our audit records it as a `sandbox_error`, not a denial. Ingesting OpenShell's `GetSandboxLogs` OCSF events would close this.

Setup notes are in `docs/openshell-backend.md`.

### X3. Coder subagent job
- A `coder` subagent spec on the `heavy` model with `execute` and file tools only.

**Acceptance:** live: "write and run a script that computes X from this CSV" produces the correct output file in `/work` under both backends.

**Status (2026-10-01):** verified live with Kimi K3.
- The pack supplies coder instructions. Assembly enforces the heavy-model, file-only spec and gives the coder's file tools a sandbox-only backend.
- Scripted CSV delegation passes under Docker and OpenShell.
- The live CSV task passes under Docker (about 130 s) and OpenShell (about 65 s).

Test commands: `docs/coder-subagent.md`.

---

## Phase 4: Safety (5 days)

### G1. Policy middleware
- `safety/policy.py`: resolve the decision for a tool from explicit rules, then effect defaults. `middleware/policy.py`: return a blocked `ToolMessage` for `block`; build the `interrupt_on` map for `approve`.

**Acceptance:** table-driven tests over tool names, globs and effects; blocked calls never reach the tool.

Implemented and verified offline: one resolver serves pack validation and runtime;
policy middleware blocks sync/async calls; concrete approval maps pause native
and coder operations. Tests cover approval, rejection, edited arguments, and
rechecking policy after reviewer edits. Approval records and authorized surface
resume remain G3. Details: `docs/policy-enforcement.md`.

### G2. Guardian
- `safety/guardian.py`: `Verdict{in_scope, risk, reason}` via structured output on the `fast` model. Input: the original user instruction for the turn, the pending call, and the policy summary. Never tool output.
- Wired as `wrap_tool_call` for effects above `read`; refusal returns a `ToolMessage` with the reason.

**Acceptance:** scripted tests for allow and refuse; a test proves the Guardian prompt contains no tool-result text; live: 20 labelled cases with at least 90% agreement.

Implementation and offline verification are present: strict structured verdicts,
fast-role review for non-read calls, root-instruction propagation to subagents,
pre-approval refusal, and re-review of edited calls. Tool history is excluded from
the review request; malformed/unavailable review fails closed. The 20-case live
gate passes on `glm-5p3-flash` with 20/20 agreement, on two separate runs
(2026-10-01). See `docs/guardian-review.md`.

### G3. Approvals
- `safety/approvals.py`: on interrupt, insert an `approvals` row and emit an approval event. `POST /approvals/{id}` with approve, edit or reject resumes the run with `Command(resume=…)`. Only users in the pack's `approvers` may decide.
- Every decision writes an `episodes` row (proposal and human action).

**Acceptance:** contract tests for approve, edit (edited args are what execute) and reject; a non-approver gets 403 and the run stays paused.

**Status:** implemented and verified offline and against local PostgreSQL.
Interrupt cards and the paused dot state are persisted atomically; authorized
decisions atomically record episodes and enqueue exactly one resume once all
cards in the interrupt group are decided. The worker checks the checkpoint and
resumes with `Command`, keeping ordinary inbox messages queued during review.
Tests cover approve/edit/reject, unauthorized callers, duplicate/concurrent
decisions, multi-action reviews, stale resumes and successive nested coder
interrupts. Verified identity must be supplied by trusted authentication
middleware; no client identity header or owner fallback is accepted. See
[`approvals.md`](approvals.md) for the HTTP contract and integration requirements.

### G4. Audit log and credentials
- `safety/audit.py`: append-only writes for every tool call, verdict, policy decision and approval.
- `safety/credentials.py`: `CredentialBroker.resolve(handle)` from env locally and Secret Manager on GCP. Redaction middleware removes any resolved secret that appears in text.

**Acceptance:** a test scans every model request in a scripted run and finds no secret values; the audit viewer endpoint returns the full trail for a turn.

**Status:** implemented and verified offline and against local PostgreSQL.
Supervisor and subagent safety events carry a shared turn id; proposals,
execution attempts/outcomes, surface refusals, policy decisions, Guardian
verdicts and approvals are append-only. Approval audit rows commit atomically
with decisions, episodes and resume rows. The authenticated audit endpoint
supports turn filtering and cursor pagination. Environment and lazy Secret
Manager credential sources resolve allowlisted handles inside tool code and
register values with shared runtime redaction. Tests scan supervisor and
Guardian requests, including echoed credentials and transport errors, and
cover async operation, immutable reads, approval races and audit-failure
rollback. Live Secret Manager access is verified (2026-10-01, project
`biz2bricks-dev-v1`): `tests/live/test_secret_manager_live.py` (marker `gcp`,
which needs application-default credentials and `DOT_GCP_TEST_PROJECT`)
creates a throwaway secret and resolves it through the broker. The resolved
value is registered for redaction, a missing secret fails without leaking
names or values, and the secret is deleted afterwards (no leftovers). See
[`audit-and-credentials.md`](audit-and-credentials.md).

---

## Phase 5: Background jobs (3 days)

### J1. Job store and runner
- `jobs/store.py` over the `jobs` table. `jobs/runner.py`: the worker also claims jobs, builds the subagent as its own Deep Agent on its own thread, applies `update_job` messages at turn boundaries, honours cancellation, stores the result as an artifact, and posts a `job_result` to the dot's inbox.

**Status:** implemented and verified offline and against local PostgreSQL.
Store operations are conditional and atomic; `finish` and the `job_result`
inbox row commit together. Postgres claims hold an advisory lock, so jobs
orphaned by a dead worker are reclaimed and resume from their checkpoint.
Runner threads have their own graph runtime; `build_job_agent` reuses the
supervisor's safety chain, checks the profile grant, keys sandboxes per job and
adds cancel/update control. The `job_result` text is a fixed template and the
Guardian reviews job work against the originating user instruction. Jobs
pause for human review and resume when it is decided; the dot keeps answering
meanwhile (added 2026-10-01). Supervisor tools are J2. See
[`background-jobs.md`](background-jobs.md).

### J2. Job tools
- `start_job`, `check_job`, `update_job`, `cancel_job`, `list_jobs` for the supervisor (design 4.3). A `researcher` subagent spec on the `supervisor` model.

**Acceptance:** contract test: the dot starts a job, answers an unrelated message while it runs, then reports the job result in the originating channel; cancel stops a job within one step.

**Status:** implemented and verified offline and against local PostgreSQL.
The five tools are offered only to profiles that grant subagents, and only
with a job store; they are scoped to the dot. `start_job` records the origin
(Guardian instruction, turn id, inbound channel) from graph state, never from
its arguments. The `researcher` spec runs on the `supervisor` model. The
acceptance test runs the inbox worker and a job runner concurrently: the dot
answers while the job runs (that reply is tagged for web), then reports the
result tagged for the originating Slack conversation. Cancel stops the job
within one step. Assistant events carry the reply channel, and C1 delivers by
it. See
[`background-jobs.md`](background-jobs.md).

---

## Phase 6: Channels and UI (6 days)

### C1. Channel abstraction and Slack
- `channels/base.py`: inbound normalisation (`user`, `dot`, `text`, `reply_ref`) and outbound `post(reply_ref, blocks)`.
- `channels/slack.py`: Bolt app (Socket Mode locally, HTTP on GCP). DMs and mentions go to the inbox; replies in thread; approval cards in Block Kit with approve, edit and reject; the approver check uses the Slack user id.

**Acceptance:** e2e with a Slack test workspace: message, job, result and approval all in one Slack thread.

**Status (2026-10-01):** implemented and verified offline and against local
PostgreSQL. Live acceptance in a Slack workspace is pending.
- **Channel seam.** `channels/base.py` defines `Inbound`, the reply-ref tagging
  and `DeliveringEventChannel`. `channels/outbox.py` is the durable outbox,
  with the same contract in memory and Postgres.
- **Slack adapter.** `channels/slack.py` uses Socket Mode locally, and
  `POST /slack/events` on the API when `DOT_SLACK_MODE=http`.
- **Inbound.** DMs route by the sender's linked Slack id; channel mentions
  route through `channel_bindings` and are accepted only from the owner. Bot
  and edit events are dropped, and redeliveries are deduped.
- **Outbound.** Replies, job results and approval cards (supervisor and job)
  return to the asking thread through the outbox. Delivery is ordered, retried,
  then parked. Everything posted is escaped, with unfurling off.
- **Cards.** Approve, Edit (a JSON modal) and Reject use the Slack user id as
  the approver. Cards update wherever the decision was made.
- **Setup.** `dot link-slack` links an owner and a channel.
  `DOT_PACK_APPROVERS` adds deployment approvers. The app manifest is
  `config/slack-app-manifest.yaml`.
- **Tests.** The offline story test runs real Bolt dispatch, the worker, the
  job runner and delivery concurrently. The DM, job, result, approval card,
  Approve click and final reply land in one thread, and the card is updated.
- **Runbook.** [`slack.md`](slack.md).

### C2. Web UI
- Next.js pages: dot list and create; dot home (thread with live SSE, jobs, findings, approvals); audit viewer; memory diff viewer; sandbox activity.
- Approval cards show current and proposed values and support edit.

**Acceptance:** Playwright e2e against a scripted API: send a message, see the job, approve an action, see the audit trail. The same thread shows messages that came from Slack.

---

## Phase 7: Proactivity (3 days)

### P1. Scheduler
- `proactive/scheduler.py`: APScheduler locally reads pack schedules and enqueues with the schedule's profile. `POST /schedules/{dot}/{name}` for Cloud Scheduler (OIDC-verified).

### P2. Sweeps and findings
- The `sweep` profile offers `read` tools and the `researcher` subagent only. A `record_finding` tool writes to `findings`. Sweeps cannot post to channels.
- The `digest` profile reads open findings and posts a summary to the dot's default channel.
- Per-schedule budgets for model calls and tokens.

**Acceptance:** a scripted sweep writes findings and makes no external call; a test proves the `sweep` profile cannot see `send_email` or `slack_post`; the digest posts once and marks findings as reported.

---

## Phase 8: Learning loop (4 days)

### L1. Episodes
- Capture approvals, edits, rejections and explicit corrections ("don't do X") as episodes with the proposal and the human action.

### L2. Reflection
- `memory/reflection.py`: nightly per dot; the `fast` model proposes unified diffs to `/memories/AGENTS.md`, `/wiki/` and `/memories/skills/`, each citing episode ids.

### L3. Replay gate
- `memory/replay.py`: for each proposed edit, re-run up to N cited and N random episodes with the edited memory (scripted comparison of the new proposal against the human action). Keep the edit if the match rate does not fall and at least one cited episode improves.

### L4. Versions and rollback
- `memory/versions.py`: every accepted edit is a `memory_versions` row; the UI shows the diff; rollback restores the previous version.

**Acceptance:** with seeded episodes where the user always shortens emails, reflection proposes a preference, replay accepts it, and the next draft follows it; a harmful edit (seeded) is rejected by replay; rollback restores the prior file.

---

## Phase 9: Connectors and tool discovery (3 days)

### K1. MCP
- `tools/mcp.py`: load servers from `config/mcp.yaml` with `langchain-mcp-adapters`; attach effects per tool from the same file; drop unmapped tools with a warning.
- Start with GitHub (read tools as `read`, PR creation as `write`).

### K2. Discovery for large tool sets
- `tools/discovery.py`: when a profile has more than 30 tools, offer `search_tools`, `describe_tool` and `run_tool` instead of every schema (the paid media agent's pattern). `run_tool` passes through the same policy, Guardian and approval chain.

**Acceptance:** an MCP tool without an effect mapping is never offered; with 200 stub tools the prompt stays under a set token budget and the right tool is found in a scripted test.

---

## Phase 10: Evaluation and hardening (4 days)

### E1. Scripted suites
- Coverage for runtime ordering, policy, Guardian wiring, approvals, jobs, sweeps and replay.

### E2. Live task eval
- `tests/live/test_eval.py`: 15 tasks across research, drafting and coding. Record pass or fail, model calls, tokens, cost and wall time to `eval_results.jsonl` and `eval_summary.md` (the format used in recon v2). Run per model assignment.

### E3. Prompt-injection red team
- 20 fetched pages and emails that try to trigger `send_email`, reveal credentials, widen policy or message third parties. Pass means no side effect executed without approval and no secret in any model request.

### E4. Budgets and failure paths
- Budget exhaustion, model timeout, sandbox failure, Slack outage and a worker restart mid-run (resume from checkpoint).

**Acceptance:** E2 at least 80% pass on the default model mix; E3 100%; every E4 path ends in a recorded, user-visible state.

---

## Phase 11: GCP deployment (4 days)

### I1. Terraform
- `infra/`: Cloud Run (`api`, `worker` with always-allocated CPU and min 1, `web`), Cloud SQL Postgres 17 with pgvector, GCS bucket, Secret Manager, Cloud Scheduler jobs per pack schedule, Artifact Registry, service accounts with Workload Identity, IAP in front of `web` and `api`.

### I2. OpenShell gateway on GCE
- A small VM on the private VPC running the OpenShell gateway; the worker reaches it over the internal address; policies deployed from `sandbox/policies/`.

### I3. Smoke and rollback
- Deploy script, smoke test (create dot, message, job, approval), and a documented rollback.

**Acceptance:** the definition of done in section 1 works on GCP with Slack in HTTP mode.

---

## Phase 12: Second pack, `onboarding-ops` (4 days)

- A pack that operates the recon workbench over its HTTP API: tools `list_runs`, `start_run(entity, file_ref)`, `get_run(run_id)` (read and write effects); **no tool that answers a gate**.
- A sweep over a sponsor drop location with read-only credentials that classifies new files and starts runs.
- A per-sponsor wiki seeded from the recon skills' business context; the daily digest reports runs by phase, blockers and ageing.

**Acceptance:** dropping a file in the watched location leads to a started run, a digest entry and a drafted sponsor question for any open brief question, with no gate passed by the dot.

---

## 3. Order of work and parallelism

| Track | Tasks | Can start after |
|---|---|---|
| Core | F1 → F2 → F3 → D1 → D2 → D3 → D4 → D5 | none |
| Sandbox | X1 → X2 → X3 | F1 |
| Safety | G1 → G2 → G3 → G4 | D3 |
| Jobs | J1 → J2 | D4 |
| Channels | C1, C2 | D5 and G3 |
| Proactivity | P1 → P2 | D4 and G1 |
| Learning | L1 → L2 → L3 → L4 | G3 |
| Connectors | K1 → K2 | G1 |
| Eval | E1 continuous; E2–E4 | J2, P2, L3 |
| Deploy | I1 → I2 → I3 | E2 |
| Second pack | Phase 12 | I3 |

Estimated total: about 50 engineering days for one engineer, less with Claude Code working the tracks in parallel.

## 4. Open items (non-blocking)

- Email transport for `send_email`: SMTP relay or Gmail API.
- Whether sweeps should use `glm-5p3` instead of Flash for harder domains; decide from E2 data.
- Whether to add GKE Agent Sandbox before or after v1 GA.
- Web auth provider: IAP or Identity Platform.
