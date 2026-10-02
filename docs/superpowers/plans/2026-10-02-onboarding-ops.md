# onboarding-ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `onboarding-ops` pack. A dot from this pack picks up sponsor files from a drop folder, starts recon workbench runs behind an approval card, watches those runs, and posts a morning digest with drafted sponsor questions. It never answers a workbench gate.

**Architecture:**
- **Engine change 1:** a digest can take only the findings recorded by named sweeps (`Schedule.findings_from`).
- **Engine change 2:** a digest that ends waiting at an approval marks its findings reported.
- **Workbench client and tools:** a small `ReconClient`, with no gate method, plus five native tools in `tools/native/recon.py`.
- **New pack:** `packs/onboarding-ops/` holds the pack definition, policy, persona, skills, wiki and schedules.

**Tech Stack:** Python 3.12, `httpx` (already a dependency), LangGraph/deepagents, pydantic v2, pytest with the scripted model.

**Spec:** `docs/superpowers/specs/2026-10-02-onboarding-ops-design.md`. The authorities above it are `docs/design.md`, `docs/proactivity.md` and `CLAUDE.md`.

## Global Constraints

- **Agents propose, code decides, humans approve.** Every rule is enforced in code, and a prompt line may only add to it. The dot has **no tool and no client method** that reaches `POST /runs/{id}/gate`.
- **One assembly.** Agents are built only by `dot.assembly`.
- **Tests run offline on the scripted model.** The fake workbench is an `httpx.MockTransport`. Live calls run only under `-m live`.
- **No secrets.** None in code, fixtures or prompts. `.env.example` holds names only. The `cred:recon` token is resolved by `CredentialBroker` at call time and never appears in tool arguments, results or logs.
- **Tools return compact JSON**, never file contents.
- **Code style.** Python 3.12, type hints everywhere, pydantic v2 at boundaries, frozen dataclasses inside. Comments explain constraints, not history. Keep diffs small.
- **Checks before every commit:**
  - `uv run ruff check . && uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run pytest -q`
  - db-marked tests: `DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db`, never run alongside another suite
  - web: untouched
- **Commits.** Commit on `main`, one commit per task. Each message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.
- **`research-analyst` behaviour is unchanged.**

## Ruling recorded at planning

The spec says the recon tools are "not registered" without `DOT_RECON_URL`. This plan follows the existing native-tool pattern instead: every native tool is always registered, and it returns `{"ok": false, "error": "the recon workbench is not configured"}` when its dependency is missing. Only packs that list the tools offer them, so `research-analyst` is unaffected either way. Cost if wrong: none observable.

## Review Focus

1. **A file changed between approval and upload.** `start_run` must re-hash it and refuse. The approved sha256 is the only file that may be uploaded.
2. **A crafted file name or symlink.** `start_run` and `list_drops` must never read outside `<root>/<sponsor_id>/`.
3. **A rejected start, then the next intake.** The file must be reported `declined` and not proposed again until its content changes.
4. **Intake and daily share the dot.** Neither may consume the other's findings. `research-analyst`'s digest still sees every open finding.
5. **The workbench is down or slow.** Every tool returns `ok: false` within the timeout. Sweeps record nothing new, and no turn crashes.

---

### Task 1: Engine: `findings_from`, and an approval wait reports intake's findings

**Files:**
- Modify: `src/dot/packs/schema.py`: add `Schedule.findings_from`.
- Modify: `src/dot/packs/loader.py`, `_check_schedules`.
- Modify: `src/dot/proactive/findings.py`, `open_for_digest`.
- Modify: `src/dot/proactive/runs.py`, `prepare` and `finish`.
- Modify: `docs/proactivity.md`.
- Test: `tests/unit/test_proactive.py`, `tests/unit/test_pack_loader.py`.

**Interfaces:**
- Produces:
  - `Schedule.findings_from: list[str] | None = None`, valid only when `kind == "digest"`.
  - `open_for_digest(repos, dot_id, schedules: Sequence[str] | None = None) -> list[Finding]`.
  - `finish` marks a digest's snapshot reported when the turn replied **or** paused at an approval.

- [ ] **Step 1: Write the failing tests**

  In `tests/unit/test_pack_loader.py`, using that file's existing pack-writing helpers:
  - A digest with `findings_from: [sweep]` loads.
  - A digest with `findings_from: [digest]` fails with `"findings_from 'digest' is not a sweep of this pack"`, because it names a non-sweep.
  - A digest with `findings_from: [nope]` fails, because the name is unknown.
  - A `sweep` with `findings_from` set fails with `"only a digest takes findings_from"`.

  In `tests/unit/test_proactive.py`, using the existing `Rig`:

```python
def test_a_digest_with_findings_from_sees_only_those_sweeps(rig: Rig) -> None:
    rig.finding("sweep", "From the sweep")
    rig.finding("other", "From elsewhere")
    titles = [f.title for f in open_for_digest(rig.repos, rig.dot.dot_id, ["sweep"])]
    assert titles == ["From the sweep"]
    assert len(open_for_digest(rig.repos, rig.dot.dot_id)) == 2  # no filter: every open finding


def test_a_digest_that_ends_at_an_approval_marks_its_snapshot_reported(rig: Rig) -> None: ...
```

  `rig.finding(schedule, title)` inserts an open finding directly through `rig.repos.insert_finding`. Add the helper if the rig doesn't have one.

  The second test needs a pack schedule whose digest profile can reach `send_email`. Build it with the rig's existing digest path and a scripted `tools(call("send_email", ...))` turn that ends in an interrupt. Then assert that the digest's findings are `reported`. If the `research-analyst` digest profile can't reach an approve tool, monkeypatch the loaded pack's `digest` profile to `tools: [send_email]` for this test only.

- [ ] **Step 2: Run them to see them fail**

  Run: `uv run pytest -q tests/unit/test_proactive.py tests/unit/test_pack_loader.py -k "findings_from or ends_at_an_approval"`

  Expected: FAIL. Either `open_for_digest()` takes no third argument, or the pydantic error for the unknown field `findings_from`.

- [ ] **Step 3: Implement**

  `schema.py`, inside `Schedule`:

```python
    findings_from: list[str] | None = None
```

  Extend `check_profile`:

```python
        if self.findings_from is not None and self.kind != "digest":
            raise ValueError("only a digest takes findings_from")
```

  `loader.py`, in `_check_schedules`, after the name loop:

```python
    sweeps = {s.name for s in pack.schedules if s.kind == "sweep"}
    for schedule in pack.schedules:
        for name in schedule.findings_from or []:
            if name not in sweeps:
                errors.append(f"schedule {schedule.name!r}: findings_from {name!r} is not a sweep of this pack")
```

  `findings.py`:

```python
def open_for_digest(repos: Repositories, dot_id: str, schedules: Sequence[str] | None = None) -> list[Finding]:
    """The open findings a digest covers, highest score first; only those sweeps' when ``schedules`` is set."""
    rows = repos.list_findings(dot_id, OPEN)
    if schedules is not None:
        wanted = set(schedules)
        rows = [row for row in rows if row.schedule in wanted]
    return sorted(rows, key=lambda row: (-row.score, row.id))[:DIGEST_LIMIT]
```

  `runs.py`:
  - In `prepare`, pass `schedule.findings_from` to `open_for_digest`.
  - In `finish`, change the digest condition to this:

```python
    if run.schedule.kind == "digest" and (_replied(messages) or _awaiting_approval(messages)):
        # A turn paused at an approval acted on its findings; reporting them again would raise a second card.
        mark_reported(repos, run.findings)
```

  Use this predicate:

```python
def _awaiting_approval(messages: Sequence[Any]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and bool(last.tool_calls)
```

  `finish` is called with the snapshot's messages after the stream. A turn paused at `interrupt_on` ends with the AI message that holds the pending tool calls. Confirm this against `run_agent_turn` in `src/dot/runtime/worker.py`. If the snapshot shape differs, pass `snapshot.interrupts` into `finish` and use that as the predicate instead. Report which one you used.

- [ ] **Step 4: Run the tests**

  Run `uv run pytest -q`. Expected: PASS, with the `research-analyst` digest tests unchanged.

- [ ] **Step 5: Docs and commit**

  In `docs/proactivity.md`'s digest rule:
  - add one sentence on `findings_from`;
  - change "After a turn that ends in a reply" to "After a turn that ends in a reply, or waits at an approval".

```bash
git add src/dot/packs/schema.py src/dot/packs/loader.py src/dot/proactive/findings.py src/dot/proactive/runs.py \
  docs/proactivity.md tests/unit/test_proactive.py tests/unit/test_pack_loader.py
git commit -m "Let a digest take named sweeps' findings and report them at an approval wait

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `ReconClient`, settings and tool deps

**Files:**
- Create: `src/dot/tools/native/recon_client.py`
- Modify: `src/dot/config.py`
  - `recon_url: str | None = None`
  - `recon_drop_root: str | None = None`
  - `recon_timeout_s: float = 30.0`
- Modify: `src/dot/tools/native/deps.py`: `ToolDeps` gains `recon`, `drop_root` and `recon_declined`.
- Modify: `src/dot/assembly.py`, `default_tool_deps`.
- Modify: `.env.example`
  - `DOT_RECON_URL=`
  - `DOT_RECON_DROP_ROOT=`
  - `DOT_RECON_TIMEOUT_S=30`
  - a comment that `cred:recon` goes in `DOT_CREDENTIAL_BINDINGS` when the workbench requires a token
- Test: `tests/unit/test_recon_client.py`

**Interfaces:**
- Produces, in `recon_client.py`:

```python
RECON_CREDENTIAL = "cred:recon"


class ReconError(Exception):
    """The workbench refused or could not be reached. The message never contains a credential."""


class ReconClient:
    def __init__(
        self,
        base_url: str,
        *,
        credentials: CredentialBroker | None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None: ...
    def sponsors(self) -> list[dict[str, Any]]: ...  # GET /sponsors
    def runs(self, sponsor_id: str | None = None) -> list[dict[str, Any]]: ...  # GET /runs
    def run(self, run_id: str) -> dict[str, Any]: ...  # GET /runs/{id}
    def start(self, sponsor_id: str, file_name: str, data: bytes) -> str: ...  # POST /runs -> run_id
```

  There is **no other public method**. In particular there is no gate method.

  Each request sends `X-Actor: dot`. When `credentials` is set and `cred:recon` resolves, it also sends `Authorization: Bearer <token>`. When it doesn't resolve (`CredentialError`), no auth header is sent, which suits a local workbench with no token.

  Errors:
  - any non-2xx response raises `ReconError(f"workbench returned {status}: {detail}")`, where `detail` is the JSON `detail` when present, clipped to 300 characters;
  - an `httpx` transport error raises `ReconError("workbench unreachable")`.

  `start` posts multipart with `data={"sponsor_id": ..., "entity": "affiliate"}` and `files={"file": (file_name, data)}`, and returns `json["run_id"]`.

- In `deps.py`, `ToolDeps` gains:

```python
    recon: ReconClient | None = None
    drop_root: Path | None = None
    # Files this dot was refused permission to start: (sponsor_id, file_name, sha256).
    recon_declined: Callable[[], frozenset[tuple[str, str, str]]] | None = None
```

  Use a `TYPE_CHECKING` import for `ReconClient` if importing it would cycle.

- In `default_tool_deps`:
  - when `settings.recon_url` is set, build `ReconClient(settings.recon_url, credentials=None, timeout_s=settings.recon_timeout_s)`;
  - set `drop_root=Path(settings.recon_drop_root)` when that is set.

  The broker is attached later by `_tool_deps`, which `replace`s `credentials`. So `ReconClient` must take its broker **lazily**: pass a callable, or let `_tool_deps` rebuild the client with the redacting broker. Choose the smallest correct option and state it in your report. The token must come from the same `RedactingBroker` the other tools use.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_recon_client.py`, with an `httpx.MockTransport` that records requests.
  - `sponsors()`, `runs("sponsor-a")` (query param), `run("run-1")` and `start(...)` hit the right method and path. `start` sends multipart containing `sponsor_id`, `entity=affiliate` and the file bytes, and returns the `run_id`.
  - `X-Actor: dot` is always sent. A broker that resolves `cred:recon` to `"tok"` adds `Authorization: Bearer tok`. A broker raising `CredentialError` adds no auth header.
  - A 422 with `{"detail": "unsupported file type"}` raises `ReconError` containing that detail. A transport error raises `ReconError("workbench unreachable")`.
  - The client exposes no attribute containing `gate`: `assert not [n for n in dir(ReconClient) if "gate" in n]`.
- [ ] **Step 2: Run them to see them fail**, with a `ModuleNotFoundError`.
- [ ] **Step 3: Implement** the client, settings, deps and `default_tool_deps` wiring.
- [ ] **Step 4: Run the tests**, then the full checks.
- [ ] **Step 5: Commit** with the message "Add the recon workbench client and settings".

---

### Task 3: The five recon tools

**Files:**
- Create: `src/dot/tools/native/recon.py`
- Modify: `src/dot/tools/native/__init__.py`: add the names to `NATIVE_TOOL_NAMES` and the builders.
- Modify: `src/dot/tools/registry.py`: add to `NATIVE_EFFECTS`.
  - `list_sponsors`, `list_drops`, `list_runs`, `get_run` are `read`.
  - `start_run` is `write`.
- Modify: `src/dot/assembly.py`. In `_tool_deps`, or wherever `runtime.audit_repositories` is available, set `recon_declined` to read this dot's rejected `start_run` cards:

```python
def _declined(repos: Repositories, dot_id: str) -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (str(c.args.get("sponsor_id")), str(c.args.get("file_name")), str(c.args.get("sha256")))
        for c in repos.list_dot_approvals(dot_id, "rejected")
        if c.tool == "start_run"
    )
```

  Check the real rejected status value used in `src/dot/safety/approvals.py`. Use that constant.
- Test: `tests/unit/test_recon_tools.py`

**Interfaces:**
- Consumes: `ReconClient` and `ReconError` from Task 2, and `ToolDeps.recon`, `drop_root` and `recon_declined`.
- Produces tools named exactly `list_sponsors`, `list_drops`, `list_runs`, `get_run` and `start_run`. All return `json.dumps(...)` strings, following `envelope.py` or `results.py` if those hold the existing compact-result helper.

**Behaviour** (constants at module top):

```python
SUPPORTED = (".csv", ".tsv", ".xlsx", ".xls")
MAX_BYTES = 20 * 1024 * 1024
SPONSOR_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
```

- Any tool whose dependency is missing returns `{"ok": false, "error": "the recon workbench is not configured"}`.
- **`list_sponsors()`** returns `{"ok": true, "sponsors": [{"id", "name"}]}`.
- **`list_drops(sponsor_id: str | None = None)`:**
  - Walk only `drop_root/<id>/` for directories whose name matches `SPONSOR_ID`, or just the one given. Look at regular files only; never follow symlinks (`is_symlink()` means skip, with reason `"symlink"`).
  - For each file:
    - `sha256` is streamed, and `bytes` is the size;
    - `supported` is true when the extension is in `SUPPORTED` and the size is at most `MAX_BYTES`;
    - `reason` is `None`, `"unsupported type"`, `"too large"`, `"unknown sponsor"` (the sponsor isn't in `client.sponsors()`) or `"symlink"`;
    - `run_id` comes from the run whose `upload_sha` equals `sha256` (from `client.runs()`);
    - `declined` is true when `(sponsor_id, file_name, sha256)` is in `recon_declined()`.
  - Return `{"ok": true, "files": [...]}`, sorted by sponsor, then file name.
- **`list_runs(sponsor_id=None)`** returns `[{run_id, sponsor_id, status, upload_name, age_hours, updated_hours}]`, with hours rounded to one decimal from `created_at` and `updated_at`.
- **`get_run(run_id)`** returns:
  - `{run_id, sponsor_id, phase, status, error, working, age_hours}`;
  - `gate`, `gate_message` and `blocked_reasons`, taken from `pending` or `None`;
  - `brief_questions` as `[{id, text, options}]`, from `brief.questions` or `[]`.
- **`start_run(sponsor_id: str, file_name: str, sha256: str)`:**
  1. Check `sponsor_id` against `SPONSOR_ID`. `file_name` must equal `Path(file_name).name` and must not start with `.`.
  2. Resolve `path = (drop_root / sponsor_id / file_name)`. Refuse if `path.is_symlink()`, or if `path.resolve().parent != (drop_root / sponsor_id).resolve()`.
  3. Check the extension and size.
  4. Read the bytes. Refuse if `hashlib.sha256(data).hexdigest() != sha256`, with the error `"the file changed since it was approved"`.
  5. Refuse if any run has `upload_sha == sha256`. Return `{"ok": false, "error": "a run already exists for this file", "run_id": ...}`.
  6. Call `client.start(...)`, which returns `{"ok": true, "run_id": ...}`.
  7. On `ReconError`, return `{"ok": false, "error": str(exc)}`, passed through the redactor if the tool has one. Follow how `send_email` redacts.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_recon_tools.py`, using `tmp_path` as the drop root and the `MockTransport` fake from Task 2. Move the fake into `tests/support/fake_recon.py` so Task 5 can reuse it. The fake keeps an in-memory list of sponsors and runs, records every request path, and serves `POST /runs` by appending a run with `upload_sha` set to the uploaded bytes' sha256 and status `awaiting_brief`. Tests:
  - `list_drops` covers a supported file, `"unsupported type"` (`.pdf`), `"too large"` (monkeypatch `MAX_BYTES` small), `"unknown sponsor"`, `"symlink"`, `run_id` matched by sha, and `declined` from a stub `recon_declined`.
  - `start_run` refuses `../x.csv`, `a/b.csv`, `.hidden.csv`, a symlink out of the folder, a sha mismatch after the file is rewritten, a duplicate sha with the existing `run_id`, and a bad `sponsor_id`. The happy path uploads exactly the file bytes and returns a `run_id`.
  - No tool request in any test hits a path containing `/gate`. Assert this on the fake's recorded paths, in a fixture teardown or a final test.
  - Every tool returns `ok: false` "not configured" with empty deps.
  - The effects are `read` ×4 and `write` for `start_run`, via `builtin_registry().effect(...)`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and the full checks.** `research-analyst` tests must pass unchanged.
- [ ] **Step 5: Commit** with the message "Add the recon workbench tools".

---

### Task 4: The `onboarding-ops` pack

**Files:**
- Create in `packs/onboarding-ops/`:
  - `pack.yaml` and `policy.yaml`
  - `persona.md`
  - `skills/intake/SKILL.md` and `skills/sponsor-questions/SKILL.md`
  - `wiki/affiliate-onboarding.md` and `wiki/sponsors.md`
- Test: `tests/unit/test_onboarding_pack.py`

- [ ] **Step 1: Write the failing test**, following `tests/unit/test_pack_loader.py`'s style.
  - `load_pack(REPO_ROOT / "packs/onboarding-ops")` succeeds.
  - Every sweep profile reaches only `read` tools. This is already a loader error, so loading is the test. Also assert it directly.
  - `intake` has `findings_from == ["intake-sweep"]`, and `daily` has `findings_from == ["status-sweep"]`.
  - The policy resolves `start_run` and `send_email` to approve, and `draft_email` to allow.
  - No pack tool name contains `gate`.
- [ ] **Step 2: Run it to see it fail.**
- [ ] **Step 3: Write the pack.**

`pack.yaml`. Keep `models` as in `research-analyst`, and adjust keys to the real schema, using `packs/research-analyst/pack.yaml` as the reference.

```yaml
name: onboarding-ops
persona: persona.md
models: {supervisor: glm-5p3, heavy: kimi-k3, fast: glm-5p3-flash}
skills: skills/
wiki: wiki/
tools:
  native: [list_sponsors, list_drops, list_runs, get_run, start_run, draft_email, send_email, write_report]
  mcp: []
subagents: []
profiles:
  chat:
    tools: "*"
  sweep:
    effects: [read]
  intake:
    tools: [list_drops, list_runs, start_run]
  digest:
    tools: [list_runs, get_run, list_sponsors, draft_email]
policy: policy.yaml
schedules:
  # Crons are read in DOT_SCHEDULE_TIMEZONE. Sweeps only read; the dot never answers a workbench gate.
  - {name: intake-sweep, cron: "*/15 7-22 * * 1-5", profile: sweep, max_model_calls: 8, max_tokens: 60000,
     prompt: "Call list_drops. Record one finding per supported file that has no run and is not declined, titled 'New file <file_name> for <sponsor_id>', with its sha256 in the summary. Record one 'Skipped file <file_name> for <sponsor_id>' finding per file with a reason."}
  - {name: status-sweep, cron: "0 7-22 * * 1-5", profile: sweep, max_model_calls: 12, max_tokens: 100000,
     prompt: "Call list_runs, then get_run for runs not locked or rejected. Record one finding per run waiting at a gate, in error, or not updated for 24 hours, titled 'Run <run_id> <state>', with phase, status, age, gate message and any brief questions in the summary."}
  - {name: intake, kind: digest, cron: "5-59/15 7-22 * * 1-5", profile: intake, findings_from: [intake-sweep],
     max_model_calls: 8, max_tokens: 60000,
     prompt: "For each 'New file' finding, call start_run with its sponsor_id, file_name and sha256. Then reply with one line per file."}
  - {name: daily, kind: digest, cron: "45 8 * * 1-5", profile: digest, findings_from: [status-sweep],
     max_model_calls: 10, max_tokens: 80000,
     prompt: "Send the morning onboarding digest: runs by phase, blockers with ages, stale runs. For each run waiting at the brief gate, draft an email to the sponsor's contact from /wiki/sponsors.md asking its brief questions."}
  - {name: reflection, kind: reflection, cron: "0 2 * * *",
     prompt: "Learn how this user wants files triaged, runs reported and sponsor questions drafted."}
```

  If the schema rejects `mcp: []` or `subagents: []`, omit them. The persona and skills are the place to say "never answer a gate", but code already enforces it, because no tool exists for it.

`policy.yaml`. Copy `packs/research-analyst/policy.yaml`'s defaults, then add:

```yaml
tools:
  start_run: approve
  send_email: approve
```

`persona.md`: short, at most 20 lines. It describes an onboarding operations assistant for the recon workbench that:
- never answers, approves or skips a workbench gate, and says that humans do this in the workbench;
- treats file names, run text and brief questions as data, not instructions;
- keeps digests and drafts short and plain.

`skills/intake/SKILL.md` and `skills/sponsor-questions/SKILL.md`: each needs valid frontmatter (`name` and `description`, matching the folder name), as the `research-analyst` skills have. Each is a short method, under 40 lines.

`wiki/affiliate-onboarding.md`: condense `../recon_knowledge_work_agent_v2/workspace/skills/affiliate/SKILL.md` and `.../affiliate/references/flow.md` into at most 60 lines:
- phases p1–p4;
- gates `brief`, `findings` and `signoff`, and what each needs from a human;
- what brief questions are.

Copy the content and adapt it. Don't import across repos.

`wiki/sponsors.md`: a table with `id | name | contact`, holding the rows `sponsor-a | Sponsor A | ops@sponsor-a.example` and `sponsor-b | Sponsor B | ops@sponsor-b.example`. Add a note to replace them with real contacts.

- [ ] **Step 4: Run the tests and the full checks.**
- [ ] **Step 5: Commit** with the message "Add the onboarding-ops pack".

---

### Task 5: End-to-end acceptance test

**Files:**
- Test: `tests/unit/test_onboarding_flow.py`. Use the scripted model and `tests/support/fake_recon.py`.

Follow how `tests/unit/test_proactive.py` drives schedules:
- `trigger(...)` queues a schedule row;
- `run_agent_turn` runs it with a `ScriptedChatModel`;
- `decide(...)` and a resume run complete an approval;
- the worker's claim rule and the rig's helpers are reused where they exist.

Build the dot with `create_dot(repos, runtime, "onboarding-ops", "owner")` and `ToolDeps` with:
- `recon` set to the fake client;
- `drop_root` set to `tmp_path`;
- `recon_declined` wired as in assembly. Pass deps the way the rig does, through `run_agent_turn(deps=...)`.

- [ ] **Step 1: Write the test** below. It should pass once Tasks 1–4 are in, so this is an integration check.
  1. Write `tmp_path/sponsor-a/affiliates.csv` with a few rows. The fake workbench knows sponsor `sponsor-a`.
  2. Run `intake-sweep`. The scripted model calls `list_drops`, then `record_finding("New file affiliates.csv for sponsor-a", "<sha>", 0.9)`. Assert an open finding with schedule `intake-sweep`.
  3. Run `intake`. The scripted model calls `start_run(sponsor-a, affiliates.csv, <sha>)`. Assert a pending approval card for `start_run`, and assert the intake finding is now `reported`.
  4. Decide `approve` and resume. Assert the fake workbench received `POST /runs` with the file bytes, and that the tool result has `run_id`.
  5. The fake run is now at `awaiting_brief` with `pending = {gate: "brief", ...}` and one brief question. Run `status-sweep`. The scripted model calls `list_runs`, then `get_run`, then `record_finding("Run <id> waiting at brief", ...)`.
  6. Run `daily`. The scripted model calls `get_run`, then `draft_email(to="ops@sponsor-a.example", ...)`, then replies with a summary naming the run. Assert:
     - `draft_email` ran without an approval card, because `draft` is allowed;
     - the reply is published;
     - the status finding is `reported`;
     - the intake finding was never in `daily`'s snapshot.
  7. Assert that none of the fake workbench's recorded paths contains `/gate`.
- [ ] **Step 2: Run it.** If it fails, the failure points at a real gap in Tasks 1–4. Fix the cause in the owning module, not the test, and say so in the report.
- [ ] **Step 3: Run the full checks.**
- [ ] **Step 4: Commit** with the message "Add the onboarding-ops acceptance test".

---

### Task 6: Run it locally: compose mount, README and docs

**Files:**
- Modify: `docker-compose.yml`. The `api` and `worker` services each gain a volume `./var/drops:/var/dot/drops:ro` and the environment entry `DOT_RECON_DROP_ROOT: /var/dot/drops`.
- Modify: `.gitignore`. Add `var/drops/` if `var/` isn't already ignored. Check first.
- Modify: `README.md`. Add a section "The onboarding-ops dot", covering:
  - starting the workbench on port 8100 from its repo: `scripts/start-backend.sh --port 8100`;
  - setting `DOT_RECON_URL=http://host.docker.internal:8100` in `.env`;
  - rebuilding the backend with step 2;
  - creating a dot from the `onboarding-ops` pack in the web UI;
  - dropping a file into `var/drops/sponsor-a/`;
  - what happens next: within 15 minutes, a finding and then an approval card, during weekday working hours in `DOT_SCHEDULE_TIMEZONE`;
  - triggering a schedule by hand for a quick try: the scheduler webhook needs OIDC, so instead say to wait or set the schedule crons. Check whether a dev route or CLI command exists to fire a schedule (`grep -rn "def trigger\|schedule" src/dot/surfaces/cli.py`). If one exists, document it. Otherwise say to wait for the next firing.
- Modify: `docs/pending.md`. Phase 12 is done ahead of order. Keep Phases 9–11 listed, with Phase 9's spec approved.
- Modify: `docs/implementation-plan.md`. Phase 12 gets one line: "Built 2026-10-02 ahead of order; see `docs/superpowers/specs/2026-10-02-onboarding-ops-design.md`." Note that the drop location is a local folder and runs start behind approval.

- [ ] **Step 1: Make the changes.**
- [ ] **Step 2: Verify the compose file.** `docker compose config >/dev/null` must exit 0. Don't start containers.
- [ ] **Step 3: Run the full checks**, including `uv run ruff format --check .`, which also checks markdown code blocks.
- [ ] **Step 4: Commit** with the message "Document running the onboarding-ops dot locally".
