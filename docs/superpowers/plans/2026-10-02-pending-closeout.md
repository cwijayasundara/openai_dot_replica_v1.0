# Pending close-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every item in `docs/pending.md` §1, §2, §4 and §5. That means committing L4, moving reflection and the gate off the turn loop, finishing the learning-loop follow-ups and doing the housekeeping, so Phase 9 can start from a clean base.

**Architecture:** Each item is a small, separately testable change in the existing modules. The worker gains a second "learning" lane: a second `Worker` loop on its own thread, which claims only reflection rows, still under the per-dot advisory lock. Reflection gains two code-level checks: it skips edits a human undid, and it merges edits that create the same file. Slack gains a message shortcut that files a correction. To support that, every posted reply records its Slack `ts` against the AI message id it came from.

**Tech Stack:** Python 3.12, uv, LangGraph/deepagents 0.7.20, psycopg 3 + Postgres, FastAPI, Slack Bolt, Next.js 16 + Playwright.

**Spec:** `docs/design.md` (authority), `docs/learning-loop.md` (L1–L4), `docs/pending.md` (the list being closed), `CLAUDE.md` (rules).

## Decisions already taken (do not re-open)

- **Reflection and the gate move to a dedicated learning-lane thread.** The defaults are unchanged and there is no cap. The per-dot lock is kept, so a dot's own messages still wait for its gate, but other dots no longer do.
- **The nightly `reflection` schedule stays on** in `packs/research-analyst/pack.yaml`.
- **`design.md` §8 is updated** to say: find/replace edits drafted by the model, unified diff built by code.
- **One creating edit per night is fixed by merging,** not seeding. Reflection merges every empty-`find` edit to the same not-yet-existing file into one edit. `AGENTS.md` is not seeded.
- **Commits.** One commit per task on branch `pending-closeout`. Never push.
- **Phase 9 (K1 MCP, K2 discovery) gets its own plan** after this one lands.

## Global Constraints

- **Agents propose, code decides, humans approve.** Every new rule is enforced in code. A prompt line may only add to it, never replace it.
- **One assembly.** Any graph reader is built with `dot.assembly.build_dot_agent`. Don't fork it.
- **Scripted model in CI.** No live model in these tests.
- **No secrets in code, fixtures or prompts.**
- **Code style.** Python 3.12, type hints everywhere, pydantic v2 at boundaries, frozen dataclasses inside. Comments explain constraints, not history.
- **Checks every task must pass before its commit:**
  - `uv run ruff check . && uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run pytest -q`
  - For db-marked tests: `DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db` (after `docker compose up -d postgres`)
  - For web changes: `cd web && pnpm typecheck && pnpm test:e2e`
- **Don't commit, push or call live models** beyond what this plan says.

## Review Focus

1. **A reflection row and a user message for the same dot are queued together.** The user message must still run, after the gate, and must never be claimed by the learning lane. Pinned in Task 1.
2. **An approver rolls back an edit, and the next night the model proposes the identical edit.** It must be dropped by code with a logged reason, not just discouraged by the prompt. Pinned in Task 3.
3. **A Slack user who is neither the owner nor an approver uses the "Correct this" shortcut,** or uses it on a message posted before this change. Nothing is recorded, and they get a clear ephemeral notice. Pinned in Task 8.
4. **A correction on a message older than the scan window.** The response must say "too old", not "unknown message", while a truly unknown id still gets 404. Pinned in Task 5.
5. **Two tabs are open on the memory page and one rolls back.** The other refreshes from the streamed `memory` event, and a non-approver sees no action buttons at all. Pinned in Tasks 2 and 6.

---

### Task 0: Branch and commit L4

**Files:** none changed. This commits the working tree listed in `docs/pending.md` §1, including the untracked `tests/unit/test_versions.py`.

- [ ] **Step 1: Branch**

```bash
git switch -c pending-closeout
```

- [ ] **Step 2: Run every check**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
docker compose up -d postgres
DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db
(cd web && pnpm typecheck && pnpm test:e2e)
```

Expected: everything passes, with about 254 offline and 22 db tests. If anything fails, stop and fix it before committing; L4 must land green.

- [ ] **Step 3: Commit**

```bash
git add docs/learning-loop.md docs/pending.md src/dot/memory/reflection.py src/dot/memory/replay.py \
  src/dot/memory/versions.py src/dot/persistence/db.py src/dot/surfaces/api.py src/dot/surfaces/views.py \
  tests/contract/test_api.py tests/support/repository_contract.py tests/support/web_e2e_server.py \
  tests/unit/test_replay.py tests/unit/test_versions.py "web/app/dots/[dotId]/memory/page.tsx" \
  web/e2e/dot.spec.ts web/lib/api.ts docs/superpowers/plans/2026-10-02-pending-closeout.md
git status --short   # expect nothing left unstaged
git commit -m "Add L4: memory rollback and needs_review accept/discard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: Run reflection and the gate on a learning lane

Today the reflection row runs inside the single inbox loop (`src/dot/runtime/worker.py`, the `schedule.kind == "reflection"` branch of `run_agent_turn`). Its roughly 100 replay calls stall every other dot on that worker.

The fix is a second `Worker` loop on its own thread, which claims **only** reflection rows. The turn loop then claims everything **except** reflection rows. Both loops keep taking the per-dot advisory lock. That lock is what makes memory actions answer 409 and keeps a dot's turn off half-judged memory.

**Files:**
- Modify: `src/dot/runtime/worker.py`
- Test: `tests/contract/test_inbox_worker.py` (db marker)
- Docs: `docs/learning-loop.md` (L2, the paragraph starting "Its inbox row carries the placeholder profile")

**Interfaces:**
- Produces: `Lane = Literal["turns", "learning"]` and `Worker(pool, repos, events, runner, *, lane: Lane = "turns")`. Task 2 relies on reflection still running through `run_agent_turn`, which is unchanged.
- Consumes: `REFLECTION_PROFILE` from `dot.packs.schema`. Its value is `"reflection"`, and it is the profile on a reflection inbox row.

- [ ] **Step 1: Write the failing db test**

Add this to `tests/contract/test_inbox_worker.py`:

```python
from dot.packs.schema import REFLECTION_PROFILE
from dot.proactive.scheduler import trigger


def test_reflection_runs_on_the_learning_lane_and_never_delays_another_dot(
    pool: ConnectionPool, repos: Repositories
) -> None:
    _seed(repos)
    reflecting, release, answered = threading.Event(), threading.Event(), threading.Event()
    ran: list[tuple[str, str]] = []

    def runner(dot: Dot, profile: str, batch: Sequence[InboxMessage], channel: EventChannel) -> None:
        del batch, channel
        ran.append((dot.dot_id, profile))
        if profile == REFLECTION_PROFILE:
            reflecting.set()
            assert release.wait(10)
        elif dot.dot_id == "dot-b":
            answered.set()

    row = trigger(repos, repos.get_dot("dot-a"), "reflection", datetime.now(UTC))
    assert row is not None
    turns = Worker(pool, repos, InMemoryEventChannel(), runner)
    learning = Worker(pool, repos, InMemoryEventChannel(), runner, lane="learning")
    assert turns.run_once() is False  # the turn lane never claims a reflection row

    stop = threading.Event()
    threads = [threading.Thread(target=_loop, args=(w, stop), daemon=True) for w in (learning, turns)]
    threads[0].start()
    assert reflecting.wait(5)
    enqueue(repos, "dot-a", "web", {"text": "mine waits for the gate"}, "chat")
    enqueue(repos, "dot-b", "web", {"text": "hello"}, "chat")
    threads[1].start()
    try:
        assert answered.wait(5)  # dot-b answered while dot-a is still reflecting
        assert ("dot-a", "chat") not in ran  # dot-a's own turn waits for its lock
        release.set()
        deadline = time.monotonic() + 5
        while ("dot-a", "chat") not in ran and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ("dot-a", "chat") in ran
    finally:
        release.set()
        stop.set()
        for thread in threads:
            thread.join(5)
    assert all(r["done_at"] is not None and r["error"] is None for r in _inbox(pool, "dot-a"))
```

- [ ] **Step 2: Run it to verify that it fails**

Run: `DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db tests/contract/test_inbox_worker.py -k learning_lane`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'lane'`.

- [ ] **Step 3: Implement the lane**

In `src/dot/runtime/worker.py`:

```python
from typing import Any, Literal

from dot.packs.schema import REFLECTION_PROFILE

Lane = Literal["turns", "learning"]
# A reflection row: nightly reflection and its replay gate. Each runs on its own lane, so one
# dot's gate (about 100 replay calls) never delays another dot's turn.
_REFLECTION_ROW = "(inbox.source = 'schedule' AND inbox.profile = %s)"
```

Change `Worker.__init__` to take `*, lane: Lane = "turns"` and store:

```python
self._lane_sql = f"AND {'' if lane == 'learning' else 'NOT '}{_REFLECTION_ROW}"
```

In both `_peek` and `_claim`, append `{self._lane_sql}` to the `WHERE` clause, right after the paused-dot condition. Then add `REFLECTION_PROFILE` to the parameter tuple in the matching position:

- `_peek`: `(REFLECTION_PROFILE, list(skipped))`
- `_claim`: `(dot_id, REFLECTION_PROFILE)`

Build the SQL as an f-string from these constants only. No caller data goes into it.

Both queries need the filter. `_claim` takes the dot's leading run of rows, so without it the turn lane would sweep a reflection row into a user turn.

Replace the main loop in `serve()` with a shared helper, and run the learning lane on its own thread:

```python
def _serve_lane(
    settings: Settings, pool: ConnectionPool, repos: Repositories, store: JobStore, stop: threading.Event, lane: Lane
) -> None:
    """One inbox loop. The graph runtime and the lock table are per thread; neither is thread-safe."""
    runtime = build_graph_runtime(settings)
    runtime.audit_repositories = repos
    runtime.jobs = store
    try:
        worker = Worker(pool, repos, _events(pool), _agent_runner(settings, runtime), lane=lane)
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(_IDLE_WAIT_S)
    finally:
        runtime.close()
```

In `serve()`:

```python
learning = threading.Thread(
    target=_serve_lane, args=(settings, pool, repos, store, stop, "learning"), name="learning", daemon=True
)
learning.start()
try:
    _serve_lane(settings, pool, repos, store, stop, "turns")
finally:
    stop.set()
    learning.join()
    for thread in job_threads:
        thread.join()
```

Remove the old inline `runtime = build_graph_runtime(...)` and `Worker(...)` block that this replaces.

The pool stays at `max_size=10`. At most 2 lane locks plus `job_workers` (default 2) connections are pinned, which leaves 6 for queries.

- [ ] **Step 4: Run the tests**

Run: `DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db tests/contract/test_inbox_worker.py` and then `uv run pytest -q`.
Expected: PASS, including the existing parallel-dots test.

- [ ] **Step 5: Docs**

In `docs/learning-loop.md` L2, replace "The worker runs it under the dot's lock, like any schedule row, but runs no agent and touches no thread." with:

> The worker's learning lane, a second inbox loop on its own thread, runs it under the dot's lock but runs no agent and touches no thread. The turn loop never claims a reflection row, so one dot's gate does not delay another dot's turn. The dot's own messages wait for its lock, like any turn.

- [ ] **Step 6: Commit**

```bash
git add src/dot/runtime/worker.py tests/contract/test_inbox_worker.py docs/learning-loop.md
git commit -m "Run reflection and the replay gate on their own worker lane

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Stream a `memory` event when memory versions change

**Files:**
- Modify: `src/dot/runtime/turns.py` (`EventKind`)
- Modify: `src/dot/runtime/worker.py` (the reflection branch of `run_agent_turn`)
- Modify: `src/dot/surfaces/api.py` (`memory_action`)
- Modify: `web/components/dot-live.tsx` (`KINDS`)
- Test: `tests/unit/test_reflection.py`, `tests/unit/test_versions.py`

**Interfaces:**
- Produces: `EventKind` now includes `"memory"`, with detail `{"version": int, "status": str}` from an action, or `{"proposed": int, "judged": list[int]}` from the nightly run. `DeliveringEventChannel` ignores it, because `_deliverable` only forwards `message` and `approval`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_reflection.py::test_the_worker_runs_a_reflection_row_without_an_agent`, replace `assert events.events == []` with:

```python
    [event] = events.events
    assert event.kind == "memory" and event.detail["proposed"] == 1
    assert event.detail["judged"] == [v.id for v in rig.repos.list_memory_versions(rig.dot.dot_id)]
```

In `tests/unit/test_versions.py`, keep the app on the rig (`self.app = app`) and add:

```python
def test_a_memory_action_streams_a_memory_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    version = rig.held(AGENTS_PATH, "", "- Keep emails short.\n")
    assert rig.act(version, "discard").status_code == 200
    events = [e for e in rig.app.state.surface.events.events if e.kind == "memory"]
    assert [(e.dot_id, e.detail) for e in events] == [(rig.dot.dot_id, {"version": version.id, "status": "discarded"})]
    assert rig.act(version, "discard").status_code == 409
    assert len([e for e in rig.app.state.surface.events.events if e.kind == "memory"]) == 1  # no event on a refusal
```

- [ ] **Step 2: Run them to verify that they fail**

Run: `uv run pytest -q tests/unit/test_reflection.py tests/unit/test_versions.py -k "worker_runs_a_reflection or memory_event"`
Expected: FAIL, because no `memory` event is published.

- [ ] **Step 3: Implement**

In `turns.py`, add `"memory"` to the `EventKind` literal.

In `worker.py`, change the reflection branch to:

```python
                reflection = run_reflection(repos, runtime.store, dot, schedule, drafter, settings, runtime.redactor)
                judged = gate_proposed(repos, runtime, dot, settings, supervisor_model(settings, model))
                if reflection.edits or judged:
                    detail: Json = {"proposed": len(reflection.edits), "judged": [v.id for v in judged]}
                    events.publish(TurnEvent(dot.dot_id, "memory", detail))
                return
```

In `api.py` `memory_action`, add this after the `with dot_lock(...)` block, before `return`:

```python
        surface.events.publish(TurnEvent(dot_id, "memory", {"version": version.id, "status": version.status}))
```

In `web/components/dot-live.tsx`, add `"memory"` to `KINDS`. The memory page already reloads on `tick`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q && uv run mypy src && (cd web && pnpm typecheck)`
Expected: PASS.

- [ ] **Step 5: Docs and commit**

In `docs/learning-loop.md` L4, after "The web memory page shows each version's status…", add:

> Each action, and each nightly run that proposes or judges an edit, streams a `memory` event, so every open memory page reloads.

```bash
git add src/dot/runtime/turns.py src/dot/runtime/worker.py src/dot/surfaces/api.py web/components/dot-live.tsx \
  tests/unit/test_reflection.py tests/unit/test_versions.py docs/learning-loop.md
git commit -m "Stream a memory event when memory versions change

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Reflection never re-proposes an edit a human undid

**Files:**
- Modify: `src/dot/memory/reflection.py` (`run_reflection`, `reflect`, `REFLECTION_PROMPT`)
- Test: `tests/unit/test_reflection.py`
- Docs: `docs/learning-loop.md` (L2 step 4 list)

**Interfaces:**
- Produces: `reflect(model, files, episodes, objective, redactor, max_edits, undone: frozenset[tuple[str, str]] = frozenset())`. Here `undone` holds `(path, replace.strip())` for every `rolled_back` or `discarded` version of the dot. The dropped reason is `"a human undid this edit"`.

The check is code, in `reflect()`, not in `check_edit()`. That's because `check_edit` also serves `accept_reviewed`, where a human's choice wins. The undone edits are also shown to the model as data (`"undone"`), but the code check is what enforces the rule.

- [ ] **Step 1: Write the failing test**

```python
def test_an_edit_a_human_undid_is_not_proposed_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [e.id for e in rig.shortened()]
    undone = {"path": AGENTS_PATH, "find": "", "replace": "- Keep emails short.\n", "rationale": "r"}
    rig.repos.insert_memory_version(MemoryVersion(0, rig.dot.dot_id, NOW, "", ids, "rolled_back", undone))
    rig.model.structured_script = [
        {
            "edits": [
                edit(AGENTS_PATH, "", "- Keep emails short.\n", ids),
                edit(AGENTS_PATH, "", "- Sign as Ada.\n", ids),
            ]
        }
    ]

    result = rig.reflect()

    assert [d.reason for d in result.dropped] == ["a human undid this edit"]
    (proposed,) = [v for v in rig.repos.list_memory_versions(rig.dot.dot_id) if v.status == "proposed"]
    assert proposed.detail["replace"] == "- Sign as Ada.\n"
    data = json.loads(str(rig.model.structured_seen[0][1].content))
    assert data["undone"] == [{"path": AGENTS_PATH, "replace": "- Keep emails short.\n"}]
```

(Add `MemoryVersion` to the `dot.persistence.db` import.)

Task 4 later adds a merge of create-edits. The undone filter must run **before** that merge, or the undone text would be merged into the surviving edit. The order in `reflect()` is: drop undone edits, then merge (Task 4), then cap and check.

- [ ] **Step 2: Run it to verify that it fails**

Run: `uv run pytest -q tests/unit/test_reflection.py -k undid`
Expected: FAIL, with two edits proposed and no drop.

- [ ] **Step 3: Implement**

In `run_reflection`, before calling `reflect`:

```python
    undone = frozenset(
        (str(v.detail.get("path")), str(v.detail.get("replace", "")).strip())
        for v in repos.list_memory_versions(dot.dot_id)
        if v.status in {"rolled_back", "discarded"}
    )
```

Pass it as `undone=undone`. In `reflect`, add `"undone": [{"path": p, "replace": r} for p, r in sorted(undone)]` to `data`. Then, at the top of the proposal loop:

```python
        if (proposal.path, proposal.replace.strip()) in undone:
            dropped.append(Dropped(proposal.path, "a human undid this edit"))
            continue
```

Append this sentence to `REFLECTION_PROMPT`: `"Never propose an edit listed in undone: a human removed it."`

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/unit/test_reflection.py tests/unit/test_replay.py`
Expected: PASS.

- [ ] **Step 5: Docs and commit**

In `docs/learning-loop.md` L2 step 4, add this bullet:

> - it is not an edit a human undid: same path and same `replace` text as a `rolled_back` or `discarded` version. The model also sees those as `undone` data, but code enforces the rule.

```bash
git add src/dot/memory/reflection.py tests/unit/test_reflection.py docs/learning-loop.md
git commit -m "Drop reflection edits identical to ones a human undid

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Merge edits that create the same file

When `AGENTS.md` does not exist, each preference arrives as an edit with an empty `find` that creates it. Only the first of these can land; the rest go stale. Reflection now merges every create-edit to the same path into one.

**Files:**
- Modify: `src/dot/memory/reflection.py` (`reflect`, plus a new `_merge_creates`)
- Test: `tests/unit/test_reflection.py`
- Docs: `docs/learning-loop.md` (L2 step 3 and the `AGENTS.md is not seeded` sentence) and `docs/pending.md` (later, in Task 13)

**Interfaces:**
- Produces: `_merge_creates(proposals: list[DraftEdit], current: dict[str, str]) -> list[DraftEdit]`. It is order-preserving: the merged edit sits where the first create-edit for that path was. `replace` texts are joined with exactly one newline between them, `rationale` with `"; "`, and `episode_ids` are the sorted union.

- [ ] **Step 1: Write the failing test**

```python
def test_edits_creating_the_same_file_are_merged_into_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [])
    ids = [e.id for e in rig.shortened(3)]
    rig.model.structured_script = [
        {
            "edits": [
                edit(AGENTS_PATH, "", "- Keep emails short.", [ids[0]], "Short."),
                edit(SKILL, "- Keep it short.", "- Keep it under 60 words.", [ids[1]]),
                edit(AGENTS_PATH, "", "- Sign as Ada.\n", [ids[2], ids[0]], "Signature."),
            ]
        }
    ]

    result = rig.reflect()

    assert [e.path for e in result.edits] == [AGENTS_PATH, SKILL] and result.dropped == ()
    merged = result.edits[0]
    assert merged.find == "" and merged.after == "- Keep emails short.\n- Sign as Ada.\n"
    assert merged.episode_ids == (ids[0], ids[2]) and merged.rationale == "Short.; Signature."
```

- [ ] **Step 2: Run it to verify that it fails**

Run: `uv run pytest -q tests/unit/test_reflection.py -k merged`
Expected: FAIL, because the second `AGENTS.md` edit is still a separate edit.

- [ ] **Step 3: Implement**

```python
def _merge_creates(proposals: list[DraftEdit], current: dict[str, str]) -> list[DraftEdit]:
    """Fold every edit that creates the same new file into one.

    Only one edit can create a file; once it lands, the others no longer
    apply and the gate rejects them as stale. Merged, they are judged together.
    """
    merged: list[DraftEdit] = []
    creating: dict[str, int] = {}
    for proposal in proposals:
        if proposal.find or proposal.path in current:
            merged.append(proposal)
            continue
        at = creating.get(proposal.path)
        if at is None:
            creating[proposal.path] = len(merged)
            merged.append(proposal)
            continue
        first = merged[at]
        text = first.replace if first.replace.endswith("\n") else first.replace + "\n"
        # model_copy skips validation: the merged text may pass DraftEdit's per-field limit.
        # check_edit still enforces the file's size cap.
        merged[at] = first.model_copy(
            update={
                "replace": text + proposal.replace,
                "rationale": f"{first.rationale}; {proposal.rationale}",
                "episode_ids": sorted(set(first.episode_ids) | set(proposal.episode_ids)),
            }
        )
    return merged
```

In `reflect`, collect the proposals that survive Task 3's undone filter into a list. Run `_merge_creates(kept, current)` on it. Then apply the existing cap and `check_edit` loop over the result, so the cap counts merged edits.

The test's first `replace` has no trailing newline, which is why the join adds one. A last line without a newline is left as the model wrote it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/unit/test_reflection.py tests/unit/test_replay.py`
Expected: PASS.

- [ ] **Step 5: Docs and commit**

In `docs/learning-loop.md` L2 step 3, append:

> Edits that create the same new file are merged into one edit: texts joined, rationales joined, episodes unioned. Only one edit can create a file.

Delete the sentence in step 4 that reads "which is how the first preference lands, since `AGENTS.md` is not seeded". Replace it with "which is how the first preference lands".

```bash
git add src/dot/memory/reflection.py tests/unit/test_reflection.py docs/learning-loop.md
git commit -m "Merge reflection edits that create the same file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: A clear error for messages too old to correct

**Files:**
- Modify: `src/dot/memory/episodes.py` (`_find_message`, `record_correction`, plus a new `CorrectionTooOld`)
- Modify: `src/dot/surfaces/api.py` (`correction` route)
- Test: `tests/unit/test_episodes.py`
- Docs: `docs/learning-loop.md` (L1 corrections bullet)

**Interfaces:**
- Produces: `class CorrectionTooOld(Exception)` in `dot.memory.episodes`. The API maps it to **409** with the detail `"this message is too old to correct"`. Task 8's Slack handler catches the same exception.

The rule is: when the scan finds nothing, look at the thread's current state. If an `AIMessage` with that id is there, the message is real but past the scan window, so raise `CorrectionTooOld`. Otherwise answer `NotFound` (404), as today.

- [ ] **Step 1: Write the failing test**

Add this to `tests/unit/test_episodes.py`, reusing `_rig`:

```python
def test_a_message_past_the_scan_window_is_too_old_not_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path, monkeypatch, [say("First."), say("Second."), say("Third.")])
    for text in ("one", "two", "three"):
        rig.turn(text)
    monkeypatch.setattr("dot.memory.episodes.CORRECTION_SCAN_LIMIT", 3)
    first, *_ = rig.ai_messages()
    app = create_app(rig.settings, repos=rig.repos, runtime=rig.runtime, events=rig.events, model=rig.model)

    @app.middleware("http")
    async def trusted_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_id = "owner"
        return await call_next(request)

    endpoint = f"/dots/{rig.dot.dot_id}/corrections"
    with TestClient(app) as client:
        old = client.post(endpoint, json={"message_id": first["id"], "text": "Shorter"})
        assert old.status_code == 409 and "too old" in old.json()["detail"]
        assert client.post(endpoint, json={"message_id": "nope", "text": "x"}).status_code == 404
```

- [ ] **Step 2: Run it to verify that it fails**

Run: `uv run pytest -q tests/unit/test_episodes.py -k too_old`
Expected: FAIL with 404 instead of 409.

- [ ] **Step 3: Implement**

In `episodes.py`:

```python
class CorrectionTooOld(Exception):
    """The message is on the thread but older than the checkpoints a correction scans."""
```

In `record_correction`, change the `found is None` branch to:

```python
    if found is None:
        state = graph.get_state({"configurable": {"thread_id": dot.thread_id}})
        messages = state.values.get("messages", []) if isinstance(state.values, dict) else []
        if any(isinstance(m, AIMessage) and m.id == body.message_id for m in messages):
            raise CorrectionTooOld(body.message_id)
        raise NotFound("messages", body.message_id)
```

In `api.py`'s `correction` route:

```python
        try:
            episode = record_correction(surface.repos, graph, dot, str(principal(request)), body)
        except CorrectionTooOld as exc:
            raise HTTPException(409, "this message is too old to correct") from exc
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/unit/test_episodes.py`
Expected: PASS.

- [ ] **Step 5: Docs and commit**

In `docs/learning-loop.md` L1, replace "so a message older than that also gets 404" with "so a message older than that gets 409 'too old to correct'. An id that is not on the thread at all gets 404."

```bash
git add src/dot/memory/episodes.py src/dot/surfaces/api.py tests/unit/test_episodes.py docs/learning-loop.md
git commit -m "Answer 'too old to correct' for messages past the scan window

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Hide memory actions from people who can't take them

**Files:**
- Modify: `src/dot/surfaces/api.py` (`GET /dots/{dot_id}/memory`)
- Modify: `web/lib/api.ts` (the `memory` call)
- Modify: `web/app/dots/[dotId]/memory/page.tsx`
- Modify: `tests/support/web_e2e_server.py`, if it serves its own memory list. Check with `grep -n '"/dots/{dot_id}/memory"' tests/support/web_e2e_server.py`; if it delegates to `create_app`, nothing changes there.
- Test: `tests/unit/test_versions.py`, `web/e2e/dot.spec.ts`

**Interfaces:**
- Produces: `GET /dots/{id}/memory` returns `{"dot_id", "versions", "can_act": bool}`. In the web client, `api.memory(id)` returns `{versions: MemoryVersion[]; can_act: boolean}` instead of the bare array. Update every caller (`grep -rn "api.memory(" web`).

The 403 on the action route stays. `can_act` only decides what is shown.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_versions.py`:

```python
def test_the_memory_list_says_whether_the_viewer_can_act(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, monkeypatch)
    assert rig.client.get(f"/dots/{rig.dot.dot_id}/memory").json()["can_act"] is True
    rig.user[0] = "owner"  # the owner may view but is not an approver
    assert rig.client.get(f"/dots/{rig.dot.dot_id}/memory").json()["can_act"] is False
```

In `web/e2e/dot.spec.ts`, add a case that sets the e2e identity to the owner, opens the memory page with a held edit, and expects `getByRole("button", { name: "Accept" })` to have count 0. Use whatever identity switch the spec already uses. If it has none, assert instead that the approver path still shows the buttons, and leave the non-approver path to the Python test.

- [ ] **Step 2: Run it to verify that it fails**

Run: `uv run pytest -q tests/unit/test_versions.py -k can_act`
Expected: FAIL with `KeyError: 'can_act'`.

- [ ] **Step 3: Implement**

In `api.py`:

```python
    @app.get("/dots/{dot_id}/memory")
    def memory_versions(dot_id: str, request: Request) -> dict[str, Any]:
        dot = viewer(request, dot_id)
        rows = surface.repos.list_memory_versions(dot_id)
        return {
            "dot_id": dot_id,
            "versions": [memory_version_view(row, surface.runtime.redactor) for row in rows],
            # What the page shows; the action route still refuses non-approvers.
            "can_act": principal(request) in approvers(dot.pack_name),
        }
```

In `web/lib/api.ts`:

```ts
  memory: (id: string) => call<{ versions: MemoryVersion[]; can_act: boolean }>(`/dots/${id}/memory`),
```

In `page.tsx`, keep `canAct` in state from the response, and render `<Actions …/>` only when `canAct`. Keep the existing 403 message as a fallback.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q && (cd web && pnpm typecheck && pnpm test:e2e)`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dot/surfaces/api.py web/lib/api.ts "web/app/dots/[dotId]/memory/page.tsx" web/e2e/dot.spec.ts \
  tests/unit/test_versions.py tests/support/web_e2e_server.py
git commit -m "Hide memory actions from viewers who are not approvers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Record which AI message each Slack reply came from

Today there is no mapping from a Slack message to the AI message id. `SlackDelivery._post` drops the `chat_postMessage` result, and the `message` event has no id. This task adds that mapping. Task 8 uses it.

**Files:**
- Modify: `src/dot/runtime/turns.py` (`_publish_message`: add `message_id`)
- Create: `src/dot/persistence/migrations/007_slack_message_posts.sql`
- Modify: `src/dot/channels/outbox.py` (the `Outbox` protocol, `MemoryOutbox`, `PostgresOutbox`: `record_post`, `find_post`)
- Modify: `src/dot/channels/slack.py` (`SlackDelivery._post`)
- Test: `tests/unit/test_slack_channel.py`. If an outbox contract test exists, also add the Postgres case there; check with `grep -rln "PostgresOutbox" tests`.

**Interfaces:**
- Produces:
  - The `message` event detail gains `"message_id": str`, only when the AI message has an id.
  - `Outbox.record_post(channel: str, conversation: str, ts: str, dot_id: str, message_id: str) -> None`
  - `Outbox.find_post(channel: str, conversation: str, ts: str) -> tuple[str, str] | None`, which returns `(dot_id, message_id)`.
  - Every chunk of a split reply is recorded against the same `message_id`.

- [ ] **Step 1: Write the failing test**

```python
def test_each_posted_chunk_records_the_ai_message_it_came_from(rig: Rig) -> None:
    channel = DeliveringEventChannel(InMemoryEventChannel(), rig.outbox, ["slack"])
    text = "x" * 5_000  # two chunks
    channel.publish(
        TurnEvent(
            "dot-1",
            "message",
            {
                "role": "assistant",
                "text": text,
                "message_id": "ai-1",
                "channel": {"source": "slack", "reply_ref": {"channel": "D1", "thread_ts": "1.0"}},
            },
        )
    )
    assert rig.delivery.deliver_once()
    posts = rig.slack.made("chat.postMessage")
    assert len(posts) == 2
    for posted in rig.slack.posted_ts():
        assert rig.outbox.find_post("slack", "D1", posted) == ("dot-1", "ai-1")
    assert rig.outbox.find_post("slack", "D1", "9.9") is None
```

`FakeSlack` must return a distinct `ts` per `chat_postMessage`. Read `tests/support/fake_slack.py`. If it doesn't already return one, make it return `{"ok": True, "channel": channel, "ts": f"{n}.0"}` with a counter, and add `posted_ts()` returning those values in order.

Also add this to `tests/unit/test_transports.py`, or wherever `publish_graph_update` is tested (find it with `grep -rln publish_graph_update tests`): an AI reply's event detail has `message_id == message.id`.

- [ ] **Step 2: Run it to verify that it fails**

Run: `uv run pytest -q tests/unit/test_slack_channel.py -k records_the_ai_message`
Expected: FAIL with `AttributeError: 'MemoryOutbox' object has no attribute 'find_post'`.

- [ ] **Step 3: Implement**

`007_slack_message_posts.sql`:

```sql
-- Which AI message each posted reply came from, so a Slack shortcut on it can file a correction.
CREATE TABLE message_posts (
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    ts TEXT NOT NULL,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    PRIMARY KEY (channel, conversation, ts)
);
```

In `turns._publish_message`, after building `said`:

```python
        if isinstance(message.id, str):
            said["message_id"] = message.id
```

In `PostgresOutbox`:

```python
def record_post(self, channel: str, conversation: str, ts: str, dot_id: str, message_id: str) -> None:
    with self._pool.connection() as conn:
        conn.execute(
            "INSERT INTO message_posts (channel, conversation, ts, dot_id, message_id)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (channel, conversation, ts, dot_id, message_id),
        )


def find_post(self, channel: str, conversation: str, ts: str) -> tuple[str, str] | None:
    with self._pool.connection() as conn:
        row = conn.execute(
            "SELECT dot_id, message_id FROM message_posts WHERE channel = %s AND conversation = %s AND ts = %s",
            (channel, conversation, ts),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None
```

In `MemoryOutbox`, keep a `self._posts: dict[tuple[str, str, str], tuple[str, str]]` guarded by `self._lock`, with the same two methods. Add both signatures to the `Outbox` protocol. Add `message_posts` to the `TRUNCATE` lists in the db test fixtures that truncate `channel_bindings`.

In `SlackDelivery._post`, inside the `item.kind == "message"` loop:

```python
            for part in chunks(str(item.body.get("text", ""))):
                posted = self._client.chat_postMessage(**target, text=escape(part), unfurl_links=False, unfurl_media=False)
                message_id = item.body.get("message_id")
                if isinstance(message_id, str):
                    self._outbox.record_post(CHANNEL, str(posted["channel"]), str(posted["ts"]), item.dot_id, message_id)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q` then `DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db`
Expected: PASS. The migration applies cleanly.

- [ ] **Step 5: Commit**

```bash
git add src/dot/runtime/turns.py src/dot/persistence/migrations/007_slack_message_posts.sql src/dot/channels/outbox.py \
  src/dot/channels/slack.py tests/
git commit -m "Record which AI message each Slack reply was posted from

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: "Correct this" Slack message shortcut

**Files:**
- Modify: `src/dot/channels/slack.py` (`build_app` gains `correct`, plus the `dot_correct` shortcut and view handlers, and `_correct_view`)
- Modify: `src/dot/surfaces/api.py` (`_mount_slack` passes `correct`)
- Modify: `src/dot/channels/slack.py` (`serve()` builds a graph runtime and passes `correct`)
- Modify: `config/slack-app-manifest.yaml` (a message shortcut with `callback_id: dot_correct`)
- Modify: `docs/slack.md`, `docs/learning-loop.md` (L1: delete "A Slack message shortcut calling the same route is not built yet.")
- Modify: `tests/support/fake_slack.py` (`shortcut(...)` and `submit_correction(...)` body builders, mirroring `click` and `submit_edit`)
- Test: `tests/unit/test_slack_channel.py`

**Interfaces:**
- Consumes: `Outbox.find_post` (Task 7), `CorrectionTooOld` (Task 5), `record_correction` and `CorrectionBody` (`dot.memory.episodes`), `approvers` (`dot.safety.approvals`).
- Produces: `Corrector = Callable[[Dot, str, CorrectionBody], Episode]`. `build_app(..., correct: Corrector | None = None)` registers the shortcut only when `correct` is set.

Who may correct: the dot owner's Slack user, or a Slack id in `approvers(dot.pack_name)`. That is the same rule as `viewer()` and the same identity `decide()` uses in Slack. The recorded `by` is the Slack user id.

The handler acks first and opens the modal at once, because `trigger_id` expires after 3 seconds. The lookup is in-process and fast. On submit, it validates the text and checks access, then calls `correct`.

- [ ] **Step 1: Write the failing tests**

```python
def _posted_reply(rig: Rig) -> str:
    rig.outbox.record_post("slack", "D1", "5.0", "dot-1", "ai-1")
    return "5.0"


def test_the_correct_shortcut_files_a_correction_for_the_owner(rig: Rig) -> None:
    filed: list[tuple[str, str, str]] = []
    rig.app = build_app(
        rig.repos,
        rig.outbox,
        client=rig.slack,
        slack_signatures_checked=False,
        process_before_response=True,
        authorize=authorize,
        correct=lambda dot, by, body: filed.append((dot.dot_id, by, body.message_id)) or None,
    )
    ts = _posted_reply(rig)
    rig.send(shortcut("dot_correct", "D1", ts, OWNER))
    [opened] = rig.slack.made("views.open")
    assert opened["view"]["callback_id"] == "dot_correct"
    assert json.loads(opened["view"]["private_metadata"]) == {"dot_id": "dot-1", "message_id": "ai-1"}

    rig.send(submit_correction(opened["view"]["private_metadata"], OWNER, "Don't cc my manager"))
    assert filed == [("dot-1", OWNER, "ai-1")]


def test_the_correct_shortcut_refuses_unknown_messages_and_outsiders(rig: Rig) -> None:
    filed: list[object] = []
    rig.app = build_app(
        rig.repos,
        rig.outbox,
        client=rig.slack,
        slack_signatures_checked=False,
        process_before_response=True,
        authorize=authorize,
        correct=lambda *a: filed.append(a) or None,
    )
    rig.send(shortcut("dot_correct", "D1", "9.9", OWNER))  # posted before mapping existed
    assert rig.slack.made("views.open") == []
    assert "web UI" in rig.slack.made("chat.postEphemeral")[-1]["text"]

    metadata = json.dumps({"dot_id": "dot-1", "message_id": "ai-1"})
    refused = rig.send(submit_correction(metadata, OTHER, "No"))
    assert json.loads(refused.body)["response_action"] == "errors" and filed == []
```

Add one more test with a `correct` that raises `CorrectionTooOld`. It expects `response_action == "errors"`, with "too old" in the error text.

The `rig` fixture sets `approvers = [OWNER]` on the loaded pack, and `OTHER` is not the owner. Make sure `dot.channels.slack.approvers` resolves through the same monkeypatched `load_pack`, or monkeypatch `dot.channels.slack.approvers` in these tests.

- [ ] **Step 2: Run them to verify that they fail**

Run: `uv run pytest -q tests/unit/test_slack_channel.py -k correct_shortcut`
Expected: FAIL, because `build_app` has no `correct` parameter.

- [ ] **Step 3: Implement**

In `slack.py`, add the imports (`Episode` from `dot.persistence.db`, `CorrectionBody`, `CorrectionTooOld` from `dot.memory.episodes`, `approvers` from `dot.safety.approvals`), then:

```python
Corrector = Callable[[Dot, str, CorrectionBody], Episode]
NOT_CORRECTABLE = 'This message can\'t be corrected here. Use "Correct this" in the web UI.'
```

Inside `build_app`, after the edit handlers:

```python
    if correct is not None:

        @app.shortcut("dot_correct")
        def on_correct(ack: Ack, body: Json) -> None:
            ack()
            conversation = str((body.get("channel") or {}).get("id", ""))
            found = outbox.find_post(CHANNEL, conversation, str((body.get("message") or {}).get("ts", "")))
            if found is None:
                _ephemeral(client, body, NOT_CORRECTABLE)
                return
            dot_id, message_id = found
            # trigger_id is valid for three seconds: open the modal before anything slow.
            client.views_open(trigger_id=body["trigger_id"], view=_correct_view(dot_id, message_id))

        @app.view("dot_correct")
        def on_correct_submit(ack: Ack, body: Json, view: Json) -> None:
            target = json.loads(str(view["private_metadata"]))
            text = (view["state"]["values"]["text"]["text"]["value"] or "").strip()
            user = str(body["user"]["id"])
            try:
                dot = repos.get_dot(str(target["dot_id"]))
                owner = repos.get_user(dot.owner_user_id)
            except NotFound:
                ack(response_action="errors", errors={"text": "This dot no longer exists."})
                return
            if user != owner.slack_user_id and user not in approvers(dot.pack_name):
                ack(response_action="errors", errors={"text": "Only the dot's owner or an approver can correct it."})
                return
            try:
                correct(dot, user, CorrectionBody(message_id=str(target["message_id"]), text=text))
            except ValidationError:
                ack(response_action="errors", errors={"text": "Say what the dot should do differently."})
                return
            except CorrectionTooOld:
                ack(response_action="errors", errors={"text": "This message is too old to correct."})
                return
            except NotFound:
                ack(response_action="errors", errors={"text": NOT_CORRECTABLE})
                return
            ack()
```

Add `_correct_view(dot_id, message_id)`, modelled on `_edit_view`:

- `callback_id: "dot_correct"`
- `private_metadata: json.dumps({"dot_id": …, "message_id": …})`
- title "Correct this"
- submit "Save"
- one multiline `plain_text_input`, `block_id`/`action_id` `"text"`, `max_length: 2000`, with the label "What should the dot do differently?"

The `except ValidationError` clause uses pydantic's `ValidationError`.

Wiring:

- **`api._mount_slack`:** pass `correct=lambda dot, by, body: record_correction(surface.repos, build_dot_agent(dot, "chat", settings=settings, runtime=surface.runtime, model=surface.model), dot, by, body)`.
- **`slack.serve()`:**
  - Build `runtime = build_graph_runtime(settings)` and pass the same lambda with `model=None`. That is what the API does in production; building the agent does not call the model.
  - Close the runtime in the `finally`.
  - Check that `chat_model("supervisor", settings)` can be constructed without network access. Read `dot/models.py` or wherever `chat_model` lives. If it requires an API key at construction, say so in `docs/slack.md` ("the Slack process needs the model settings to read the thread").

Manifest: under `features.shortcuts`, add:

```yaml
    - name: Correct this
      type: message
      callback_id: dot_correct
      description: Tell the dot what to do differently next time
```

Add `commands`/`interactivity` only if they aren't already enabled. Interactivity already exists for the approval buttons.

`docs/slack.md`: add a short "Correcting the dot" section. It should say who may use it, that it files the same episode as the web button, and that replies posted before this change can't be corrected from Slack.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dot/channels/slack.py src/dot/surfaces/api.py config/slack-app-manifest.yaml docs/slack.md \
  docs/learning-loop.md tests/support/fake_slack.py tests/unit/test_slack_channel.py
git commit -m "Add the Slack 'Correct this' message shortcut

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Fix the intermittent job-tools test

`test_check_update_cancel_and_list` puts `update_job` and `check_job` in **one** scripted model step. The tool node can run one message's calls concurrently, so `check_job` sometimes runs first. `mine["last_update"]` is then missing, which is the `KeyError`.

**Files:**
- Modify: `tests/unit/test_job_tools.py`

- [ ] **Step 1: Confirm that the cause is real**

Read how deepagents/LangGraph's `ToolNode` runs several calls from one `AIMessage`. Look at `.venv/lib/python3.12/site-packages/langgraph/prebuilt/tool_node.py` and search for `executor.map` or `get_executor_for_config`. If it uses a thread pool, the race is confirmed. Record the line you found in the commit message. If it runs the calls one after another, stop and use superpowers:systematic-debugging instead: run the test in a loop (`for i in $(seq 1 60); do uv run pytest -q -x tests/unit/test_job_tools.py::test_check_update_cancel_and_list || break; done`) and read the traceback.

- [ ] **Step 2: Fix the test's ordering**

Move `update_job` into its own step before the rest:

```python
    model = ScriptedChatModel(
        script=[
            tools(call("update_job", job_id=job.job_id, message="Only 2026.")),
            tools(
                call("check_job", job_id=job.job_id),
                call("check_job", job_id=foreign.job_id),
                call("cancel_job", job_id=foreign.job_id),
                call("list_jobs", status="queued"),
                call("list_jobs", status="bogus"),
            ),
            tools(call("cancel_job", job_id=job.job_id)),
            tools(call("update_job", job_id=job.job_id, message="too late")),
            say("done"),
        ]
    )
```

Shift the `model.seen` indexes up by one: the update result is in `seen[1]`, the checks and lists in `seen[2]`, the cancel in `seen[3]`, and "too late" in `seen[4]`.

`_results(first, "check_job")` relies on two `check_job` results arriving in call order. ToolMessages are appended in call order even when the calls run concurrently, so that stays. Confirm it while reading `tool_node.py` in Step 1.

- [ ] **Step 3: Run it many times**

Run: `for i in $(seq 1 40); do uv run pytest -q -x tests/unit/test_job_tools.py::test_check_update_cancel_and_list -p no:cacheprovider | tail -1; done | sort | uniq -c`
Expected: 40 passes.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_job_tools.py
git commit -m "Order update_job before check_job in the job tools test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Pin deepagents' skill parser with a contract test

The private import `_parse_skill_metadata` stays: the point is to agree with deepagents' own parser. deepagents is pinned at `==0.7.20`. A test makes a version bump fail in CI if the parser's name or behaviour changes.

**Files:**
- Create: `tests/unit/test_deepagents_contract.py`

- [ ] **Step 1: Write the test**

```python
"""Reflection checks SKILL.md edits with deepagents' private parser. A deepagents upgrade must keep its behaviour."""

from __future__ import annotations

from deepagents.middleware.skills import _parse_skill_metadata

from dot.packs.loader import REPO_ROOT

PATH = "/memories/skills/email-drafting/SKILL.md"


def test_a_valid_skill_parses_and_broken_frontmatter_does_not() -> None:
    valid = (REPO_ROOT / "packs/research-analyst/skills/email-drafting/SKILL.md").read_text(encoding="utf-8")
    assert _parse_skill_metadata(valid, PATH, "email-drafting") is not None
    assert _parse_skill_metadata("---\nname: [unclosed\n---\nbody\n", PATH, "email-drafting") is None
    assert _parse_skill_metadata("no frontmatter at all\n", PATH, "email-drafting") is None
```

First check the real skill path with `ls packs/research-analyst/skills`. If that skill's directory name differs, use one that exists.

- [ ] **Step 2: Run it**

Run: `uv run pytest -q tests/unit/test_deepagents_contract.py`
Expected: PASS. If the "no frontmatter" case does not return `None`, read the parser and assert what it actually does. The test pins today's behaviour; it doesn't argue with it.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_deepagents_contract.py
git commit -m "Pin deepagents' skill parser behaviour that reflection relies on

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: `import dot` outside pytest

Root cause, verified on 2026-10-02: `.venv/lib/python3.12/site-packages/_editable_impl_open_dot.pth` has the macOS `hidden` file flag, and Python 3.12's `site` skips hidden `.pth` files. So `src/` is never put on `sys.path`, and `import dot` finds only the namespace portion `site-packages/dot/sandbox/policies` left by the hatch `force-include`. With `src/` on the path, the regular package would win, so the force-include is not the cause.

**Files:**
- Modify: `CLAUDE.md` (Commands) and `README.md` if it has setup steps (check with `grep -n "uv sync" README.md`)

- [ ] **Step 1: Fix the environment**

```bash
chflags nohidden .venv/lib/python3.12/site-packages/*.pth
uv run python -c "import dot; print(dot.__file__)"
```

Expected: `…/src/dot/__init__.py`.

- [ ] **Step 2: Check that the entry points start without `PYTHONPATH`**

```bash
uv run python -c "import dot.runtime.worker, dot.proactive.scheduler, dot.channels.slack; print('ok')"
uv run python -m dot.proactive.scheduler --help 2>&1 | head -5
grep -n "\[project.scripts\]" -A8 pyproject.toml
```

Expected: imports print `ok`. If `--help` isn't supported, the module still has to get past import: an error about missing `DOT_DATABASE_URL` is fine, but `ModuleNotFoundError` is not. Repeat for each script in `[project.scripts]`.

- [ ] **Step 3: Document**

Under Commands in `CLAUDE.md`, after `uv sync --dev`, add:

```bash
chflags nohidden .venv/lib/python3.12/site-packages/*.pth   # macOS: Python skips hidden .pth files, so `import dot` fails
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "Document the macOS hidden .pth fix for importing dot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Prettier for `web/`, format-only

**Files:**
- Modify: `web/package.json` (devDependency `prettier`, and scripts `format` and `format:check`)
- Create: `web/.prettierrc.json`, `web/.prettierignore`
- Modify: every web file Prettier reformats, in **this commit only**

- [ ] **Step 1: Add Prettier**

```bash
cd web && pnpm add -D prettier
```

`web/.prettierrc.json` matches the existing style, with 120-column lines (the files already use long lines) and double quotes:

```json
{ "printWidth": 120, "semi": true, "singleQuote": false, "trailingComma": "all" }
```

`web/.prettierignore`:

```
.next
node_modules
playwright-report
test-results
```

Scripts: `"format": "prettier --write ."` and `"format:check": "prettier --check ."`.

- [ ] **Step 2: Format and check that nothing else changed**

```bash
cd web && pnpm format && pnpm format:check && pnpm typecheck && pnpm test:e2e
```

Expected: all pass. Read `git diff --stat web` and confirm it is whitespace and quotes only. If the diff is large, change `.prettierrc.json` to match the existing style more closely before committing.

- [ ] **Step 3: Add to CLAUDE.md commands**

Change the web line to `cd web && pnpm format:check && pnpm typecheck && pnpm test:e2e`.

- [ ] **Step 4: Commit**

```bash
git add web CLAUDE.md
git commit -m "Add Prettier to web and format once

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: design.md §8, learning-loop open items, pending.md

**Files:**
- Modify: `docs/design.md` §8
- Modify: `docs/learning-loop.md` ("Open items" and L4)
- Modify: `docs/pending.md` (rewrite)

- [ ] **Step 1: design.md §8**

Run `grep -n "diff" docs/design.md` to find the sentence that says the model drafts unified diffs. Replace it with:

> The model drafts find/replace edits, and code builds the unified diff that is stored and shown. Models get diff line numbers wrong.

Change nothing else in §8.

- [ ] **Step 2: learning-loop.md**

Keep "Live models are not deterministic" as an open item, blocked on E2, and keep "Replay depends on checkpoints being retained". There is no pruning today, so this stays a rule for whoever adds it.

In L4, keep the sentence about rows accepted before rollback support existed. It is the documented closure, because nothing can recover a file that was never saved.

- [ ] **Step 3: Rewrite pending.md**

Remove §1, §2, §4 and §5 items that are now done. Keep:

- **§3** (next phases), with Phase 9 next and its own plan to come.
- **Live-model nondeterminism**, waiting on E2.
- **Checkpoint retention**, a rule for future pruning.
- **Older accepted rows**, documented and not fixable.
- **A dot's own messages wait for its nightly gate**, by design: the lock keeps turns off half-judged memory.

Date it 2026-10-02.

- [ ] **Step 4: Final full check**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
DOT_DATABASE_URL=postgresql://dot:dot@localhost:55434/dot uv run pytest -q -m db
(cd web && pnpm format:check && pnpm typecheck && pnpm test:e2e)
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add docs/design.md docs/learning-loop.md docs/pending.md
git commit -m "Update design §8 and close out pending items

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## After this plan

Start the Phase 9 plan (K1 MCP connectors, K2 tool discovery) from `docs/implementation-plan.md`, using superpowers:brainstorming and then superpowers:writing-plans.
