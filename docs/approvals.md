# Approval workflow

The worker persists LangGraph HITL interrupts as approval cards, marks the dot
`paused`, and emits `approval` events. Card ids are deterministic per thread,
interrupt and action, so repeating persistence does not duplicate cards. All
cards in a paused checkpoint share a versioned-by-checkpoint `run_ref` containing
the thread, profile, interrupt ids, card ordering and original instruction.
Nested subagent interrupts use the same workflow.

`POST /approvals/{approval_id}` accepts one of:

```json
{"type": "approve"}
```

```json
{"type": "edit", "edited_args": {"to": "colleague@example.com", "subject": "Brief", "body": "Reviewed text"}}
```

```json
{"type": "reject", "message": "Do not send this"}
```

Edits change arguments, not tool identity. The resumed call still goes through
the surface guard, policy and Guardian; human approval cannot bypass them.

The API returns 202 after recording the decision, not after executing the tool.
Each decision writes an episode with the original proposal, instruction,
reviewer and human action (including edited arguments). Its outcome explicitly
states that execution has not yet resumed; approval is not proof of success.
The decision and episode are atomic. The last decision in a checkpoint group
also atomically inserts one internal `source=approval` inbox row. Concurrent or
duplicate decisions get 409 and do not create extra episodes or resume rows.

Only the worker invokes `Command(resume={interrupt_id: {decisions: [...]}})`.
It verifies the thread, profile, checkpoint and complete interrupt set first,
and takes the existing per-dot lock. A paused dot's ordinary messages remain
queued, including schedules with a different profile. The worker handles one
resume row alone and unpauses the dot only if the resumed graph has no further
interrupts. Stale/replayed commands fail closed. Worker execution failures use
the existing inbox error mechanism; they are not automatically retried because
external side effects are not guaranteed idempotent.

## Authentication integration

Trusted authentication middleware must verify identity and populate
`request.state.user_id`. The endpoint does not trust identity in the request
body or headers. Without a verified principal it returns 401. A principal not
listed in the dot pack's `policy.yaml` `approvers` gets 403, without changing
the card, thread or episodes. The shipped pack has `approvers: []`, so no one
can approve until explicitly configured. Dot ownership is not approval access.

The IAP/Identity Platform deployment adapter is a later deployment task; this
endpoint is deliberately inaccessible by default. Tests install a trusted
fixture middleware rather than weakening the production identity boundary.

## Verification

`tests/contract/test_approvals.py` covers the HTTP decisions against the assembled
scripted graph, multi-action cards, denied identities, replay and nested coder
write/execute approvals. `tests/contract/test_approvals_db.py` covers PostgreSQL
decision races and the worker's paused queue behavior. No live model is called.
