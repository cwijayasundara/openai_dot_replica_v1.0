# Pending work

Updated 2026-10-02, after Phase 8 (the learning loop) was finished. The
learning loop's design is in [`learning-loop.md`](learning-loop.md); the
order of all work is in [`implementation-plan.md`](implementation-plan.md).

## 1. Not committed

L4 (rollback and `needs_review` actions) is done and passes every check, but
it is not committed or pushed. L1–L3 are on `main` in `d3adeea`.

- Changed: `src/dot/memory/{versions,replay,reflection}.py`,
  `src/dot/persistence/db.py`, `src/dot/surfaces/{api,views}.py`, the web
  memory page, `web/lib/api.ts`, the e2e spec and server, the repository
  contract, `docs/learning-loop.md` and this file.
- New: `tests/unit/test_versions.py`.
- Checks at the time of writing: ruff, mypy, 254 offline tests, 22 Postgres
  tests (`DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run
  pytest -q -m db`), web typecheck and e2e.

## 2. Decisions waiting on you

1. **Reflection and the gate block the worker.** With the defaults, the
   nightly gate makes about 100 supervisor-model calls per dot (worst case
   400: 5 edits × 10 episodes × 2 runs × up to 4 calls). It runs in the
   worker's inbox loop, so every other dot on that worker waits meanwhile,
   including user messages. The options:
   - lower the defaults (`DOT_REPLAY_CITED`, `DOT_REPLAY_RANDOM`,
     `DOT_REFLECTION_MAX_EDITS`);
   - add a nightly replay cap that leaves extra rows `proposed`;
   - move reflection and the gate to a job-runner thread (recommended, before
     anything goes live).
2. **The nightly schedule is on.** The research-analyst pack reflects at
   02:00 in `DOT_SCHEDULE_TIMEZONE`. Remove the `reflection` schedule from
   `packs/research-analyst/pack.yaml` to stop those model calls until you
   want them.
3. **`design.md` wording.** §8 says the model drafts unified diffs. It now
   drafts find/replace edits and code builds the diff. Should `design.md` be
   updated to match?

## 3. Next phases (from the implementation plan)

| Phase | Work | Size |
|---|---|---|
| 9 Connectors and tool discovery | K1 MCP; K2 discovery for large tool sets | 3 days |
| 10 Evaluation and hardening | E1 scripted suites, E2 live task eval, E3 prompt-injection red team, E4 budgets and failure paths | 4 days |
| 11 GCP deployment | I1 Terraform, I2 OpenShell gateway on GCE, I3 smoke and rollback | 4 days |
| 12 Second pack | `onboarding-ops` | 4 days |

E2 data also settles some open items: Flash vs `glm-5p3` for sweeps, and
whether the replay gate needs repeated runs (see 4).

## 4. Learning-loop follow-ups

Roughly in order of value:

- **Rolled-back edits can come back.** Reflection doesn't see rollbacks or
  discards, so it may propose the same edit the next night, and the gate may
  accept it again. Feed rolled-back and discarded versions to reflection as
  "do not propose" input, or have the gate reject an edit identical to one a
  human undid.
- **Slack correction shortcut.** The design promises a Slack message shortcut
  that calls the corrections route. Only the web button exists.
- **One creating edit per night.** `AGENTS.md` is not seeded, so only one
  edit that creates it can land per night; the others are rejected as stale.
  Seeding an empty `AGENTS.md` per dot would remove this.
- **Live-model nondeterminism.** Each replay arm runs once. If E2 shows the
  gate's verdicts flip-flopping, replay each arm k times and compare
  majorities.
- **No event on memory changes.** The memory page updates itself after an
  action, but other open tabs only refresh on their next streamed event.
- **Old messages can't be corrected.** The lookup reads only the newest 2000
  checkpoints (about 200 turns). Older messages get a 404 that looks like
  "unknown message"; give them a clearer error.
- **Checkpoint retention.** Replay needs the checkpoints that episodes point
  at. Any future checkpoint pruning must keep those.
- **Older accepted rows.** Edits accepted before rollback support existed
  carry no saved prior file, so their rollback swaps text back rather than
  restoring the file exactly.
- **Owners who aren't approvers** see the memory buttons but get "Only a pack
  approver can change the dot's memory." Hide the buttons for them if that
  matters.

## 5. Housekeeping

- **Private deepagents import.** `memory/reflection.py` uses
  `_parse_skill_metadata`, which may break on a deepagents upgrade.
- **Intermittent test.**
  `tests/unit/test_job_tools.py::test_check_update_cancel_and_list` fails
  about 1 run in 6 with a `KeyError`. It did so before Phase 8 too.
- **`import dot` outside pytest.** `uv run python -c "import dot"` finds a
  stray namespace package in `.venv/site-packages/dot` (probably from the
  hatch `force-include` of `sandbox/policies`), so scripts needed
  `PYTHONPATH=.:src`. Check that `python -m dot.proactive.scheduler` and the
  worker entry point still start.
- **Prettier isn't installed** in `web/`, so web formatting isn't checked.
