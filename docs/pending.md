# Pending work

Updated 2026-10-02, after the Phase 8 close-out. The learning loop's design
is in [`learning-loop.md`](learning-loop.md); the order of all work is in
[`implementation-plan.md`](implementation-plan.md).

## 1. Next phases (from the implementation plan)

Phase 12 (`onboarding-ops`) was built ahead of order on 2026-10-02. Phase 9 is next, and its spec is approved; it gets its own plan.

| Phase | Work | Size |
|---|---|---|
| 9 Connectors and tool discovery | K1 MCP; K2 discovery for large tool sets | 3 days |
| 10 Evaluation and hardening | E1 scripted suites, E2 live task eval, E3 prompt-injection red team, E4 budgets and failure paths | 4 days |
| 11 GCP deployment | I1 Terraform, I2 OpenShell gateway on GCE, I3 smoke and rollback | 4 days |
| 12 Second pack | `onboarding-ops` | Done 2026-10-02 |

E2 data also settles some open items: Flash vs `glm-5p3` for sweeps, and
whether the replay gate needs repeated runs.

## 2. Learning-loop follow-ups

- **Live-model nondeterminism.** Each replay arm runs once. Waiting on E2: if
  the gate's verdicts flip-flop, replay each arm k times and compare
  majorities.
- **Checkpoint retention.** Replay needs the checkpoints that episodes point
  at. There is no pruning today; any future pruning must keep those.
- **Older accepted rows.** Edits accepted before rollback support existed
  carry no saved prior file, so their rollback swaps text back rather than
  restoring the file exactly. Documented, not fixable: nothing can recover a
  file that was never saved.
- **A dot's own messages wait for its nightly gate.** By design: the per-dot
  lock keeps turns off half-judged memory. Other dots are not affected,
  because reflection and the gate run on the separate learning lane.
- The correction lookup fetches up to 2000 full checkpoints, so cost grows
  with thread history; slow on long threads for both the web route and Slack.
- Slack: "Correct this" on a non-dot message in a channel the bot isn't in
  gets no notice; replies posted before message ids were recorded can't be
  corrected from Slack.
- Existing Slack installs must be reinstalled for the new `commands` scope.

## 3. Housekeeping

- **Schedules for a paused dot:** a pending approval pauses all of a dot's
  schedules (sweeps included); consider letting sweeps run and digests catch
  up after the decision. Before letting sweeps run, note that
  `persist_interrupts` sets the dot `active` after any turn that ends without
  an interrupt, a sweep's included, which would unpause the dot while its card
  still waits.
- **`import dot` outside pytest.** On this machine it still needs either the
  `chflags` workaround or the opt-in venv relocation outside `~/Documents`
  (`UV_PROJECT_ENVIRONMENT`); see CLAUDE.md.
