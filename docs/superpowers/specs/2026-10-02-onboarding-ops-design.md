# `onboarding-ops` pack: design

Date: 2026-10-02. This is Phase 12 of `docs/implementation-plan.md`, built before Phases 9–11 at the user's request. The authorities, in order, are `docs/design.md`, `docs/proactivity.md` and `docs/learning-loop.md`.

## Goal

A dot that operates the recon workbench (`../recon_knowledge_work_agent_v2`) through its HTTP API:

- it picks up sponsor files from a drop folder;
- it starts workbench runs, with a human approving each start;
- it watches runs through their phases;
- each morning it reports what is moving, what is blocked and what is ageing;
- it drafts the sponsor emails the workbench's brief questions call for.

The dot never answers a workbench gate.

## The workbench, as the dot sees it

The workbench API is in `src/onboarding_agent/surfaces/api.py` in that repo.

- `GET /sponsors` returns `[{id, name}]`.
- `GET /runs?sponsor_id=` returns run records. A record has these fields:
  - `id`, `sponsor_id`, `entity` and `status`;
  - `upload_name` and `upload_sha`;
  - `created_at` and `updated_at`.

  A record is created with status `scoping`, and its status and `updated_at` change only when the run is rejected or locked. An in-flight run therefore always lists as `scoping`, with `updated_at` at its creation time.
- `GET /runs/{id}` returns the run's graph state at top level, plus fields the API adds:
  - `run_id`, `sponsor_id`, `phase` (`p1`–`p4`, `done`) and the live `status`;
  - `pending` (the gate payload: `{gate, message, blocked_reasons, allowed_actions}`) and `gate_message`;
  - `brief.questions` (up to two `{id, text, options}`);
  - `error` and `artifacts`;
  - added by the API: `working`, `job_error` (a failed background job), `decisions`, and `record` (the run record above, which holds `created_at`).
- `POST /runs` takes a multipart form with `sponsor_id`, `file` and `entity` (`affiliate`). It returns 202 `{run_id}`. Invalid input gets 422: the sponsor id format, the entity, more than 20 MB, or a file that isn't CSV, TSV, XLSX or XLS.
- Gates are `brief`, `findings` and `signoff`, answered at `POST /runs/{id}/gate`. **The dot gets no tool that reaches this route.**
- Auth:
  - locally there is none, and an `X-Actor` header names the caller;
  - when the workbench sets `ONB_API_TOKEN`, it needs `Authorization: Bearer <token>`.
- The only entity is `affiliate`.
- There is no drop location and no classifier. The dot supplies both.

## Decisions

- **Drop location:** a local folder for each sponsor, `<DOT_RECON_DROP_ROOT>/<sponsor_id>/`, mounted read-only into the worker. GCS is out of scope.
- **Starting runs:** a sweep only reads. A run starts only through an approval card: the `start_run` effect is `write`, and the policy sets it to `approve`.
- **Gates:** humans answer them in the workbench UI.

## Flow

1. **`intake-sweep`** runs every 15 minutes, 07:00–17:45, Monday–Friday. It is a sweep with profile `sweep`.
   - It calls `list_drops()`. Every file that is supported, has no workbench run, and is not declined becomes a finding: title `New file <name> for <sponsor>`, with a summary of size, sha256 and type.
   - Unsupported or oversized files become findings titled `Skipped file …`, with the reason.
2. **`status-sweep`** runs every hour, 07:00–22:00, Monday–Friday. It is a sweep with profile `sweep`.
   - It calls `list_runs()` to enumerate runs and `get_run()` for each run not locked or rejected. Each run that `get_run` shows waiting at a gate, or with an `error` or `job_error`, becomes a finding. `list_runs` cannot judge this: the workbench's run record keeps status `scoping` and its creation time until the run is rejected or locked.
   - Titles are fixed per run and state (`Run <id> waiting at brief`), so one open finding exists per blocker.
   - Its summary carries the phase, status, age, the gate message, any error or job error, and the brief questions, so the digest can report how long each blocker has aged.
3. **`intake`** is a digest at `5-59/15 7-17 * * 1-5`, five minutes after each `intake-sweep` (which fires at `*/15 7-17 * * 1-5`), with profile `intake` and `findings_from: [intake-sweep]`.
   - For each `New file` finding, the dot calls `start_run(sponsor_id, file_name, sha256)`. That raises an approval card.
   - After approval, the tool uploads the file and returns the `run_id`.
   - Its findings are marked reported when the turn ends in a reply *or* in an approval wait (below).
4. **`daily`** is a digest at `45 6 * * 1-5`, before `intake` first fires, with profile `digest` and `findings_from: [status-sweep]`. It posts one summary:
   - runs grouped by phase;
   - blockers (gate waits and errors) with their ages.

   For each run waiting at a `brief` gate, it calls `draft_email` to the sponsor's contact, which `list_sponsors` reads from the dot's `/wiki/sponsors.md`. The draft asks the brief questions in plain language, with the options. Sending stays a separate, approved `send_email`.
5. **`reflection`** runs nightly at 02:00, as in `research-analyst`.

## Engine changes

These are small, and the rules are enforced in code.

- **`Schedule.findings_from: list[str] | None`** applies to digests only.
  - `None` keeps today's behaviour: every open finding.
  - A list restricts the digest's snapshot to findings recorded by those schedules, plus the digest's own open budget-stop finding, so a digest that overran its budget still reports it.
  - The loader checks that every name is a sweep in the same pack.
  - `proactive/findings.open_for_digest` takes the filter.
- **Intake's findings are reported in an approval wait too.** In `proactive/runs.finish`, a digest whose turn ends paused at an approval also marks its snapshot reported. Without this, the next firing would propose the same file again, because only a reply marks findings reported today.
  - The documented rule in `docs/proactivity.md` changes to say so.
  - A rejected start does not come back, because `list_drops` reports the file as declined (below).
- **No change to the sweep rules.** A sweep's profile may still reach only `read` tools, and the existing loader check stays.

## Tools: `src/dot/tools/native/recon.py`

`ReconClient` is a small `httpx` client with these settings:

- a base URL from `DOT_RECON_URL`;
- a timeout from `DOT_RECON_TIMEOUT_S`, default 30;
- the header `X-Actor: dot`;
- `Authorization: Bearer <token>` when `cred:recon` is bound. The token is resolved by `CredentialBroker` at call time and never reaches tool arguments, results or logs.

It has one method per route the tools use. It has **no gate method**.

| Tool | Effect | Behaviour |
|---|---|---|
| `list_sponsors()` | read | `[{id, name, contact}]`; `contact` is parsed from the dot's `/wiki/sponsors.md` table, null when missing |
| `list_drops(sponsor_id=None)` | read | For each file under the drop root: `{sponsor_id, file_name, bytes, sha256, supported, reason, run_id, declined}`. `run_id` comes from matching `sha256` with a run's `upload_sha`. `declined` is true when this dot has a rejected `start_run` approval for the same `sponsor_id`, `file_name` and `sha256`. Sponsor folders not registered in the workbench are reported as `reason: "unknown sponsor"`. |
| `list_runs(sponsor_id=None)` | read | `[{run_id, sponsor_id, status, upload_name, age_hours}]`. `status` is the record's: `scoping` until rejected or locked. |
| `get_run(run_id)` | read | `{run_id, sponsor_id, phase, status, gate, gate_message, blocked_reasons, brief_questions, error, job_error, working, age_hours}`, compact. `status` is the live graph status; `age_hours` comes from the nested `record.created_at`; messages and errors are clipped to 300 characters. |
| `start_run(sponsor_id, file_name, sha256)` | write | See below. |

`start_run` does these steps in code:

1. Validates `sponsor_id` against `^[a-z0-9][a-z0-9-]{0,62}$`.
2. Resolves the path strictly inside `<root>/<sponsor_id>/`, refusing separators, `..` and symlinks that leave the folder.
3. Checks that the extension is supported and the size is at most 20 MB.
4. Re-hashes the file and refuses if the hash is not the approved `sha256`, so the file uploaded is the file that was approved.
5. Refuses if a run with that `upload_sha` already exists, and returns its `run_id`.
6. Posts the multipart form with `entity=affiliate` and returns `{ok, run_id}`.

Every result is compact JSON, and file contents never reach the model. Errors come back as `{ok: false, error}`: the workbench is unreachable, the workbench returned 422 (passed through with its detail), or one of the refusals above happened. Error text is redacted.

`NATIVE_EFFECTS` gains the five tools. The tools are built from `ToolDeps`, which gains `recon: ReconClient | None` and `drop_root: Path | None`. With no `DOT_RECON_URL`, the tools are not registered, so `research-analyst` is unaffected. `assembly.default_tool_deps` wires them.

## Pack: `packs/onboarding-ops/`

- **`pack.yaml`:**
  - **Native tools:** `list_sponsors`, `list_drops`, `list_runs`, `get_run`, `start_run`, `draft_email`, `send_email`, `write_report`.
  - **MCP:** none.
  - **Subagents:** none.
- **Profiles:**
  - `chat`: all tools;
  - `sweep`: effects `[read]`;
  - `intake`: tools `[list_drops, list_runs, start_run]`;
  - `digest`: tools `[list_runs, get_run, list_sponsors, draft_email]`.
- **`policy.yaml`:** defaults as `research-analyst` (`write: approve`, `external: approve`, `credential: block`), with `start_run: approve` and `send_email: approve`. The approvers are taken from `DOT_PACK_APPROVERS`, as today.
- **`persona.md`:** an onboarding operations assistant. It must never answer or try to pass a workbench gate, must treat file names and run text as data, and must keep drafts short and plain.
- **Skills:** `intake/SKILL.md` covers how to triage drops. `sponsor-questions/SKILL.md` covers how to turn brief questions into a sponsor email.
- **Wiki:**
  - `affiliate-onboarding.md` is condensed from the workbench's `workspace/skills/affiliate/` and `references/flow.md`. It covers what each phase and gate means.
  - `sponsors.md` lists each sponsor's id, name and contact address for drafted questions. Local placeholders are `sponsor-a` and `sponsor-b`, with example addresses.
- **Schedules:** as in Flow above.

## Configuration and running both systems

- **New settings, names only in `.env.example`:**
  - `DOT_RECON_URL`, e.g. `http://host.docker.internal:8100` from the containers;
  - `DOT_RECON_DROP_ROOT`, defaulting to `/var/dot/drops` in containers;
  - `DOT_RECON_TIMEOUT_S`;
  - `cred:recon` in `DOT_CREDENTIAL_BINDINGS` when a token is used.
- **`docker-compose.yml`:** the worker and API mount `./var/drops:/var/dot/drops:ro`.
- **README:** a section on running the workbench on port 8100, with `scripts/start-backend.sh --port 8100` in its repo. It explains how to create a dot from `onboarding-ops` and where to drop files.

## Error handling

| Situation | Behaviour |
|---|---|
| The workbench is unreachable | Tools return `ok: false`. The sweeps record nothing new, and the next firing retries. |
| A 422 from `POST /runs` | `ok: false` with the workbench's detail. The finding stays visible in chat. |
| The file changed after approval | `start_run` refuses on the sha mismatch. The next intake-sweep proposes the new version. |
| A file was rejected at approval | `list_drops` marks it declined, and it is not proposed again until its content changes. |
| A start card (or any approval) is pending | The dot is paused: no schedule is queued for it, sweeps and digests included, until the card is decided. Missed slots are not caught up. `daily` fires at 06:45, before `intake` can raise a card, so the morning digest is not lost to a card left overnight. |
| A sponsor folder is not registered in the workbench | A `Skipped file` finding with "unknown sponsor". |

## Testing

All tests are offline, with the scripted model and a fake workbench (an `httpx.MockTransport` recording requests), except the live test.

- **Acceptance:** this is a single scripted test.
  1. A file is written to `drops/sponsor-a/`.
  2. `intake-sweep` records a `New file` finding.
  3. `intake` raises a `start_run` approval card, and approving it uploads the file to the fake workbench. The run id comes back.
  4. The fake run is set to wait at `brief` with one question, and `status-sweep` records the blocker.
  5. `daily` posts a summary naming the run and calls `draft_email` to sponsor-a's contact, with the question.
  6. The fake workbench saw **no request** to `/runs/*/gate`.
- **Unit tests for `start_run` refusals:**
  - path traversal and symlink escape;
  - an unsupported extension and an oversized file;
  - a sha mismatch;
  - a duplicate `upload_sha`.
- **Unit tests for headers and errors:**
  - `X-Actor` and the resolved bearer are sent, and the token never appears in results;
  - a 422 detail is passed through.
- **Unit tests for `list_drops`:** `declined` from a rejected approval, `unknown sponsor`, and run matching by sha.
- **Engine tests:**
  - `findings_from` restricts a digest's snapshot;
  - the loader rejects a `findings_from` that is not a sweep of the pack;
  - a digest that ends in an approval wait marks its snapshot reported;
  - `research-analyst` behaviour is unchanged.
- **Pack tests:**
  - `onboarding-ops` loads;
  - its sweeps reach only `read` tools;
  - no tool in the pack calls the gate route, checked from the `ReconClient` surface.
- **Live** (`-m live`, needs a running workbench at `DOT_RECON_URL`): `list_sponsors` and `list_runs` only, with nothing started.

## Out of scope

- GCS or other drop backends.
- Entities other than `affiliate`.
- Any tool that answers or skips a gate.
- Sending sponsor email without approval.
- Workbench artifacts download.
- Per-sponsor wiki pages beyond `sponsors.md`. Those grow through memory and reflection.
