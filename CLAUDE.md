# CLAUDE.md: working in this repository

This repository builds an open, self-hostable equivalent of always-on agents ("dots"): one pack, one continuous thread, one sandboxed computer. Read `docs/design.md` first, then `docs/implementation-plan.md`. The design is the authority; the plan is the order of work. Do not re-decide the design while implementing.

## Non-negotiable rules

- **Agents propose, code decides, humans approve.** Policy, gates, approvals, budgets and credential resolution are code. No prompt is ever the only thing enforcing a rule.
- **One assembly.** `src/dot/assembly.py` builds models, tools, middleware and agents for the API, worker, CLI and tests. Don't fork it per surface.
- **Scripted model in CI.** Graph and agent behaviour is tested with a scripted chat model (`tests/support/scripted_model.py`, ported from recon v2). Live models only under the `live` marker.
- **Tools return compact JSON plus artifact ids,** never raw documents or rows.
- **No secrets in code, fixtures or prompts.** `.env.example` holds names only.
- **Reuse by copy from `../recon_knowledge_work_agent_v2`.** Copy, then adapt. Do not import across repos. The pieces named in the plan: `sandbox/base.py`, `sandbox/docker_backend.py`, `middleware/guard.py`, `middleware/offload.py`, `middleware/redaction.py`, `tests/support/scripted_model.py`, the assembly pattern, `sandbox/Dockerfile`, and `tests/sandbox/test_sandbox_contract.py`.

## Commands

```bash
uv sync --dev
# macOS re-applies the `hidden` flag to .venv's .pth files under ~/Documents (likely iCloud sync), and Python 3.12 skips
# hidden .pth files, so `import dot` fails outside pytest. Quick, temporary fix (the flag can return within seconds):
chflags nohidden .venv/lib/python3.12/site-packages/*.pth
# Durable option: keep the venv outside ~/Documents (opt in; not set by the repo):
# export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/open-dot" && uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q                 # offline; skips live, docker, db, openshell
uv run pytest -q -m db           # needs docker compose postgres
uv run pytest -q -m live         # needs DOT_FIREWORKS_API_KEY
docker compose up -d postgres
docker compose build sandbox     # build-only image, not started by compose
cd web && pnpm format:check && pnpm typecheck && pnpm test:e2e   # UI against the scripted API (tests/support/web_e2e_server.py)
```

## Conventions

- Python 3.12, `uv`, type hints everywhere, pydantic v2 at boundaries, frozen dataclasses inside.
- Small diffs. Comments explain constraints, not history.
- Don't commit, push, deploy or call live models unless asked.
