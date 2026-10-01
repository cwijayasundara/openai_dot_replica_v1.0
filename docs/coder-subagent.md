# Coder subagent (X3)

The research-analyst pack declares `coder` on the `heavy` model. Assembly exposes
only `execute` and the built-in file tools to it; native tools, planning tools,
and further delegation are excluded. A coder configured with another model,
without a sandbox, or with native tools is rejected at assembly.

The coder reads `/in` and `/skills`, writes scripts and results into `/work`,
executes its script, verifies the output, and reports paths and a compact summary.
Its file middleware uses the lazy sandbox directly, so it cannot bypass sandbox
filesystem restrictions by accessing the supervisor's memory/wiki store routes.
The supervisor's routes still provide its memory and skills. The sweep profile
cannot delegate to coder.

With G1 in place, the pack's `write: approve` policy pauses `write_file`,
`edit_file`, `delete`, and `execute` before they run. The coder test fixtures
simulate a human review and resume each sandbox action; production assembly
never automatically approves them. The approval endpoint and approver identity
checks are implemented in G3.

X3 is the short, synchronous `task` delegation in design section 4.2. The durable
background job queue, separate job threads, and job tools are Phase 5 (J1/J2).

## Acceptance

The shared CSV fixture contains three rows. Its computed output must be:

```json
{"rows": 3, "total_quantity": 6, "total_value": "33.00"}
```

The scripted integration test runs the supervisor/coder graph with real sandbox
file tools, executes the delivered script, and downloads `/work/result.json` to
check its bytes. Run it with:

```sh
uv run pytest -q -m docker tests/contract/test_coder_csv.py
uv run pytest -q -m openshell tests/contract/test_coder_csv.py
```

The live test delegates through the actual supervisor and heavy models, checks
the generated script and output, then reruns the script to prove the result is
reproducible. It calls live models and requires the provider's API key. Run only
when live-model calls are authorized:

```sh
uv run pytest -q -m 'live and docker' tests/live/test_coder_csv_live.py
uv run pytest -q -m 'live and openshell' tests/live/test_coder_csv_live.py
```

Docker requires `docker compose build sandbox`. OpenShell additionally needs the
registered gateway and image prerequisites in `docs/openshell-backend.md`.
Explicit backend acceptance runs fail when prerequisites are missing.

Offline tests prove role selection, the offered tool surface, rejected native
and delegation calls, memory route isolation, lazy startup, and sweep denial.
They simulate the command result; real computation is checked in the backend
integration tests. **Live acceptance (2026-10-01):** passes with Kimi K3 under
Docker (about 130 s) and under a local OpenShell 0.1.2 gateway (about 65 s).

Workspace verification: 56 offline tests and all three Docker tests passed,
including the scripted coder CSV run. Ruff lint/format and mypy passed. Both
live acceptance variants collect successfully; neither was executed.
