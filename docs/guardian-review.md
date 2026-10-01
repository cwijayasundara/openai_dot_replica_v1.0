# Guardian reviewer (G2)

`safety/guardian.py` defines a strict, immutable `Verdict` with `in_scope`,
`risk` (`low`, `medium`, or `high`), and a nonempty reason. Code permits a call
only when it is in scope and its risk is not high. Review exceptions, malformed
verdicts, and missing original instructions produce refusals.

Assembly lazily builds the `fast` model through the same model factory used by
the supervisor and coder. It uses `with_structured_output(Verdict)`. Tests inject
an independent scripted structured-output response stream; they do not make
live model calls or consume the supervisor's tool-call script for reviews.

At the start of a supervisor turn, middleware captures the trailing batch of
human messages as the original instruction. This state survives checkpoints and
is inherited by delegated subagents. A coder's model-written task description
does not replace the original instruction. A new turn replaces the captured
instruction and clears the review cache.

The reviewer receives two messages: a fixed reviewer instruction and a JSON
request with the original user instruction, the pending tool name and arguments,
its effect, and the policy rule/default summary. It receives no conversation
history, `ToolMessage`, fetched-document envelope, assistant reasoning, or task
description as separate context. Pending arguments may naturally contain a draft
derived from research; this boundary excludes raw tool-result history, not the
call arguments being reviewed. Known credentials are redacted from review inputs
and reasons.

Only permitted, tagged calls with effects other than `read` are reviewed. Policy
blocks and surface exclusions take precedence and do not consume a Guardian
call. The Guardian can add a refusal but cannot override a policy block or grant
new capabilities.

After a proposal, the middleware stores a verdict keyed by the original
instruction and exact call. A refusal skips the human approval request and
returns an error tool message at execution. A permissible verdict still requires
the pack's human approval when its decision is `approve`. The tool wrapper checks
the final call before execution; edited names or arguments that no longer match
the reviewed call receive a new review. Cached verdicts are graph state, not
global mutable authorization. Synchronous and asynchronous paths are supported.

G3 provides persisted approval records and authorized resume endpoints; G4 adds
the append-only policy/verdict audit trail. G2 introduces no auto-approval of
executable actions.

## Verification

Offline tests cover the verdict table, unavailable/invalid responses, excluded
fetched content, new turn capture, root instructions in coder tasks, refusals
before approval and sandbox startup, reviewer edits, policy precedence, async
review, redaction, and lazy fast-model selection. Existing policy and coder
integration tests also exercise the Guardian with scripted verdicts.

The live gate contains twenty labelled cases (ten permissible, ten refusals).
It requires at least eighteen correct verdicts, calls only the fast reviewer,
and does not execute the proposed tools:

```sh
uv run pytest -q
uv run pytest -q -m 'docker and not live'
# Calls a live model; run when authorized and with provider credentials configured.
uv run pytest -q -m live tests/live/test_guardian_eval.py
```

**Live agreement (2026-10-01):** 20/20 on `glm-5p3-flash`, on two separate
runs. The gate requires at least 18/20.

Workspace verification: 96 offline tests and all three Docker tests passed;
Ruff lint/format and mypy passed.
