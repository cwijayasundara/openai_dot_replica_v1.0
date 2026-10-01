# Background jobs (J1, J2)

Design 4.3. A job is a granted subagent run as its own Deep Agent on its own
thread, beside the dot's inbox loop, so the dot keeps answering while it works.

## Store (`src/dot/jobs/store.py`)

`JobStore` has an in-memory and a Postgres implementation, and both pass
`tests/support/job_store_contract.py`. Each state change is one conditional
write:

| Operation | Rule |
|---|---|
| `create` | `queued`. Thread `"{dot thread}:{job_id}"`. Records `profile` and `origin` |
| `get`, `list_jobs` | Scoped to the dot. Another dot's job is `NotFound` |
| `append_update` | Atomic jsonb append. Only on `queued`/`running`, otherwise `JobClosed` |
| `cancel` | Only `queued`/`running` become `cancelled`. A finished job is returned unchanged |
| `claim` | Oldest open job no live runner holds, set to `running` |
| `finish` | Only from `running`. Inserts the `job_result` inbox row in the same transaction |

In Postgres a claim is an advisory lock (`job:{job_id}`) held on its own
connection for the whole run. A `running` job whose lock is free belonged to a
worker that died. The next claim takes it and resumes from its checkpoint.
Migration `003_jobs.sql` adds `profile`, `origin`, `error` and `started_at`.

`origin` holds the source channel, the inbox ids, and the user's `instruction`
for that turn. J2's `start_job` must fill it in code, with the instruction
taken from the turn's captured Guardian instruction in graph state. Never take
it from the tool's arguments, because then the model would choose the objective
the Guardian checks against.

## Runner (`src/dot/jobs/runner.py`)

`serve()` starts `DOT_JOB_WORKERS` (default 2) runner threads. Each thread has
its own `GraphRuntime`, because the runtime's sandboxes and connections are not
thread-safe. `build_job_agent` in `assembly.py` builds the job:

- The subagent must be granted to the profile the job was started under.
- It gets the subagent's model and tools, and the same surface guard, policy,
  Guardian, audit (actor = subagent name) and HITL chain as the supervisor.
- A sandbox subagent gets its own sandbox key (`{dot_id}-{job_id}`). This keeps
  it from retiring the supervisor's sandbox. The sandbox is closed when the job
  ends.
- `JobControlMiddleware` is the outermost layer. Before each model call it
  reads the job row. A cancelled job jumps to the end. New `update_job`
  messages are appended as `[update from the dot] …`, and the count already
  applied is kept in graph state. Before each tool call, a cancelled job
  returns an error `ToolMessage` and the tool does not run.

How a run ends:

- **succeeded:** the last assistant text is redacted and stored as an
  artifact. The job gets `result_ref`, a `job_result` row is posted, and a
  `job_finished` event is published.
- **failed:** an exception, an empty result, or an approval interrupt. The
  job's chain raises on exhausted model retries and on the model-call limit,
  which the supervisor turns into a message instead. The error is
  redacted and stored on the job, and a `job_result` row is posted.
- **cancelled:** nothing is posted. `finish` refuses a job that is no longer
  `running`.

## Supervisor tools (`src/dot/jobs/tools.py`, J2)

`build_dot_agent` offers `start_job`, `check_job`, `update_job`, `cancel_job`
and `list_jobs` only when two things hold: the profile grants subagents, and
the runtime has a job store (`GraphRuntime.jobs`, which the worker sets). The
tools are tagged `read`, like `task`, because each job's own tools are gated
when they run. They act only on the dot they were built for. Another dot's
job id is "no such job".

| Tool | Returns |
|---|---|
| `start_job(subagent, instructions)` | `job_id` at once. Only subagents granted to the turn's profile |
| `check_job(job_id)` | Status, last update, `result_ref`. Once done, the result as an untrusted excerpt (4,000 chars) |
| `update_job(job_id, message)` | Update count. Fails on a finished job |
| `cancel_job(job_id)` | The new status. A finished job is unchanged |
| `list_jobs(status?)` | The latest 20 job summaries |

`start_job` builds `origin` in code from graph state, never from its
arguments:
- `instruction` is the Guardian's captured instruction for the turn.
- `turn_id` is the turn's audit id.
- `channel` is the `{source, inbox_id}` that the worker tags on each inbound
  message.

**Reply routing.** Assistant `message` events carry a `channel`: the tag on
the turn's latest inbound message. That is the asking channel for ordinary
turns, the job's origin channel for `job_result` turns, and the paused request's
channel for approval resumes. C1 posts replies by it. `job_started` is
published from a successful `start_job` result and includes `job_id`, so it
pairs with `job_finished`.

`job_result` is not an `enqueue` source. Only `JobStore.finish` writes those
rows, because their origin sets the Guardian's objective for the turn.

## Guardian scoping

- **`job_result` payload.** The text is a fixed template: job id, subagent,
  status, artifact id. Job output never reaches the supervisor as an inbound
  message. It arrives only through tools (J2 `check_job`).
- **`job_result` turns.** The worker tags the message with the origin
  instruction. The Guardian reviews what the dot does next against that
  instruction, not against the notice text.
- **The job agent.** Its Guardian reviews against `origin.instruction`, never
  against the instructions the supervisor wrote. With no recorded instruction,
  nothing consequential is in scope.

## Approvals in jobs

A job that reaches an approval-gated call pauses; it is not failed.

- **Pausing.** The runner persists the review cards. In the same transaction
  the job moves from `running` to `paused`, and its `run_ref` is stored on the
  job row (`pending_run_ref`, migration 004).
  - The `run_ref` carries the `job_id` and the job's thread.
  - Card ids are derived from the job thread, so a replayed pause adds no new
    cards.
  - The approval events include `job_id` and the job's origin channel, so C1
    can route the card back to where the request was made.
- **The dot is not affected.** Its status is untouched and it keeps answering.
  A dot that is paused on its own review stays paused.
- **Sandbox.** A paused job's sandbox is suspended, not closed, so `/work` is
  restored when the job resumes.
- **Deciding.** `POST /approvals/{id}` and the approver check are the same as
  for G3. For a job card, the decision locks the job row and requires the job
  to be `paused` on that `run_ref`. The last decision moves the job back to
  `queued`; no inbox row is written. Episodes and audit rows use the job thread.
- **Resuming.** A runner claims the job, rebuilds the `Command(resume=…)` from
  the decided cards, and checks it against the current checkpoint and
  interrupts. A review that is stale or undecided pauses the job again. A
  resumed job can pause again on its next gated call.
- **Cancelling.** Cancel works while a job is paused. It closes the job's
  pending cards in the same transaction, and a later decision on one of them
  is a conflict.
- **Status sets.** `OPEN` (`queued`, `running`, `paused`) takes updates and
  cancels. Only `CLAIMABLE` (`queued`, `running`) is claimed.

## Known limits

- Job progress (tool calls) is not streamed to the dot's event channel; only
  `job_finished` is.
- `job_result` text is a fixed notice, so reporting a result costs the
  supervisor one `check_job` call.

## Tests

- `tests/contract/test_jobs_db.py` (`-m db`) is the J2 acceptance test. It
  runs the real inbox `Worker` and a job-runner thread against Postgres. The
  dot starts a job and answers an unrelated message while the job runs. It then
  reports the result from the `job_result` turn, and that row carries the Slack
  origin. A cancel turn stops a running job within one step: the job's pending
  tool never runs, nothing is posted, and no further model call is made.
- `tests/unit/test_job_approvals.py` covers:
  - approve, edit and reject, each resuming the job, with a non-approver
    refused and the job staying paused;
  - a second pause after a resume;
  - cancel while paused;
  - the dot answering while its job is paused;
  - a supervisor-paused dot staying paused.
- `tests/contract/test_job_store_db.py` covers concurrent last decisions
  (the job is queued once) and a cancel racing a decision.
- `tests/unit/test_job_tools.py` covers:
  - when the tools are offered;
  - origin taken from state rather than arguments;
  - profile grants;
  - dot scoping;
  - the offline round trip, where the result arrives only as tool output.

- `tests/unit/test_job_store.py` and `tests/contract/test_job_store_db.py`
  (`-m db`) run the shared store contract. The DB test also checks that
  concurrent runners claim distinct jobs.
- `tests/unit/test_job_runner.py` uses the scripted model. It covers:
  - the job's own thread, the artifact and the inbox row;
  - an update applied once;
  - cancel within one step, with no tool run;
  - the crash path;
  - a profile grant check;
  - the Guardian instruction for jobs and for `job_result` turns;
  - the approval failure.
