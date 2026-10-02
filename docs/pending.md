# Pending work

Where things stand after L1–L3 of Phase 8 (2026-10-02), and what to pick up
next. The learning loop's design is in [`learning-loop.md`](learning-loop.md).

## Done in Phase 8 so far

- **L1 Episodes.** Approval decisions already wrote episodes. Corrections are
  new: `POST /dots/{dot_id}/corrections` and a "Correct this" button in the
  web UI. `memory/episodes.py` decides which episodes can be replayed.
- **L2 Reflection.** The nightly `reflection` schedule kind (02:00 in the
  research-analyst pack) drafts find/replace edits with the `fast` model.
  Code checks each one and writes it as a `proposed` row. Migration 006 adds
  `memory_versions.detail`.
- **L3 Replay gate.** Right after reflection, each `proposed` edit is replayed
  against past episodes with and without it. Accepted edits are written to
  memory.
- **Memory reloads every turn** (`middleware/memory.py`). Before this, a dot
  never saw an `AGENTS.md` or skills change after its first turn.

## Next: L4 (versions and rollback)

1. **Rollback.** An approver action that restores a file to its state before
   an accepted edit and marks the row `rolled_back`. It must be audited.
   Refuse with a conflict if a later accepted edit touched the same text. The
   simplest version reverse-applies the stored find/replace (`detail.find`,
   `detail.replace`) to the current file. This is the third part of the
   Phase 8 acceptance test.
2. **`needs_review` actions.** These edits had nothing to replay against, and
   there is no way to act on them yet. Add accept and discard routes for
   approvers. Accept applies the edit through `versions.record_verdict`'s
   path, after the same stale-base check.
3. **Memory page.** Add a label for `needs_review`, show `detail.gate` (the
   reason and each replay result), and add the rollback, accept and discard
   buttons. Extend the e2e test.
4. Add a rollback step to `tests/unit/test_replay.py`'s acceptance test,
   which today covers "accepted" and "rejected".

## Decisions waiting on you

- **Reflection and the gate block the worker.** With the defaults, the gate
  makes about 100 supervisor-model calls per dot per night (worst case 400:
  5 edits × 10 episodes × 2 arms × up to 4 calls). It runs in the inbox
  loop, so every other dot on that worker waits meanwhile. The options are
  lower defaults (`DOT_REPLAY_CITED`, `DOT_REPLAY_RANDOM`,
  `DOT_REFLECTION_MAX_EDITS`), a nightly replay cap that leaves extra rows
  `proposed`, or moving reflection and the gate to a job-runner thread. The
  recommendation is the job thread, before anything goes live.
- **The nightly schedule is on.** The research-analyst pack now reflects at
  02:00. Remove the `reflection` schedule from `packs/research-analyst/pack.yaml`
  to stop the model calls until you want them.
- **`design.md` wording.** §8 says the model drafts unified diffs. It now
  drafts find/replace edits and code builds the diff. Update `design.md` to
  match?

## Smaller follow-ups

- **Slack correction shortcut.** The design promises a Slack message shortcut
  that calls the corrections route. Only the web button exists.
- **One creating edit per night.** `AGENTS.md` is not seeded, so only one
  edit that creates it can land per night; the others are rejected as stale.
  Seeding an empty `AGENTS.md` per dot would remove this.
- **Old messages can't be corrected.** The lookup reads only the newest 2000
  checkpoints (about 200 turns), and older messages get a 404 that looks like
  "unknown message". Consider a clearer error.
- **Live-model nondeterminism.** Each arm is replayed once. If E2 shows the
  gate flapping, replay each arm k times and compare majorities.
- **Checkpoint retention.** Replay needs the checkpoints episodes point at.
  Any future pruning must keep those.
- **Private deepagents import.** `memory/reflection.py` uses
  `_parse_skill_metadata`, which may break on a deepagents upgrade.
- **An existing intermittent test.**
  `tests/unit/test_job_tools.py::test_check_update_cancel_and_list` fails
  about 1 run in 6 with a `KeyError`. It did so before Phase 8 too.
- **`import dot` outside pytest.** `uv run python -c "import dot"` finds a
  stray namespace package in `.venv/site-packages/dot` (probably from the
  hatch `force-include` of `sandbox/policies`). Scripts needed
  `PYTHONPATH=.:src`. Check that `python -m dot.proactive.scheduler` and the
  worker entry point still start.
