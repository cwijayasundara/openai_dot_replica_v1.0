# Learning loop (L1–L4)

A dot gets better by editing its own memory, not its weights. Episodes record
what the human did with the dot's proposals. Each night reflection drafts
edits to memory. Replay re-runs past episodes with each edit applied and keeps
only edits that do not make the dot worse. Accepted edits are versioned and can
be rolled back.

| Step | Module | Model | Writes |
|---|---|---|---|
| L1 Episodes | `safety/approvals.py`, `memory/episodes.py` | none | `episodes` |
| L2 Reflection | `memory/reflection.py` | `fast` | `memory_versions` rows marked `proposed` |
| L3 Replay gate | `memory/replay.py`, `memory/compare.py` | `supervisor` | The verdict; accepted edits to the store |
| L4 Versions | `memory/versions.py` | none | `memory_versions`, the store, `audit_log` |

The model proposes edits. Code decides which ones are kept: replay and a fixed
rule. A human can roll any edit back.

## L1. Episodes

An episode is one human judgement about one dot proposal:
`episodes(id, dot_id, at, task, proposal jsonb, human_action, outcome jsonb)`.
No migration is needed.

**Approvals (built in G3).** Each decision (`approve`, `edit` or `reject`)
writes an episode in the same transaction as the decision. `proposal` holds
`approval_id`, `tool` and `args`. The card's `run_ref` holds `thread_id`,
`profile`, `checkpoint_id` and `task`, so every approval episode already points
at the exact graph state that produced the proposal. `outcome.decision` holds
`edited_args` or the rejection message.

**Corrections (new).** "Don't do X" is captured only through an explicit
action. Code never guesses which chat messages are corrections.

```
POST /dots/{dot_id}/corrections
{"message_id": "<id of the dot's AI message>", "text": "Don't cc my manager"}
```

- The web UI shows "Correct this" on the dot's messages; the thread view
  returns each AI message's `id`. Only the dot's owner or an approver may file
  a correction. A Slack message shortcut calling the same route is not built
  yet.
- Code resolves `message_id` to the earliest checkpoint whose last message is
  that AI message (the step right after the model made it), then stores
  `proposal = {thread_id, profile, checkpoint_id, message_id, tool_calls}` and
  `human_action = "correction"`. `outcome` holds `{text, by}`. A message id
  with no such checkpoint gets 404. The search reads at most the newest 2000
  checkpoints (`CORRECTION_SCAN_LIMIT`), about 200 turns, so a message older
  than that also gets 404.
- The worker puts `dot_profile` in every run's config metadata, and LangGraph
  copies it onto each checkpoint. That is how a correction knows which
  profile made the message. Runs from before this change have no profile, so
  their corrections cannot be replayed.
- Messages are stored as deltas in checkpoints, so state is read through the
  compiled graph (`get_state`, `get_state_history`), never the raw saver.
- `text` is untrusted data. Reflection reads it as quoted input. It never
  reaches a system prompt.

**Which episodes can be replayed.** Code decides this when replay loads an
episode. An episode can be replayed when:

1. It is an approval or a correction on the dot's own thread. Job agents and
   subagents do not load `/memories/AGENTS.md` or skills (see
   `assembly._subagent_spec` and `build_job_agent`), so a memory edit cannot
   change what they propose.
2. Its checkpoint still exists.
3. The last AI message in that checkpoint's state carries the call the
   episode is about: same tool, same call id. For a correction, the message
   must have at least one tool call.

Anything else is reflection input only. It can be cited, but replay skips it
and reports it as `unreplayable` with the reason. It never counts as a match.

## L2. Reflection

A pack schedules reflection with `kind: reflection`. It takes no profile and
no budget, and its `prompt` says what to learn:

```yaml
- {name: reflection, kind: reflection, cron: "0 2 * * *",
   prompt: "Learn how this user wants research, briefs and emails done."}
```

Its inbox row carries the placeholder profile `reflection`. The worker's
learning lane, a second inbox loop on its own thread, runs it under the dot's
lock but runs no agent and touches no thread. The turn loop never claims a
reflection row, so one dot's gate does not delay another dot's turn. The dot's
own messages wait for its lock, like any turn. A paused dot's reflection waits until the dot is unpaused. One run
(`memory/reflection.py`):

1. Loads the oldest episodes after the dot's cursor, at most
   `DOT_REFLECTION_MAX_EPISODES`, leaving out any from the last minute so an
   episode that commits late is not skipped. A backlog drains over several
   nights. With no episodes there is no model call.
2. Sends the `fast` model one structured request. The system prompt is fixed.
   The human message is JSON: the schedule's prompt, every memory file
   (`/memories/AGENTS.md`, the skills and the wiki), and the episodes. Fields
   that only locate state (approval, thread and checkpoint ids, who decided)
   are removed, long strings are clipped, and everything is redacted. Episode
   text is untrusted, so it is only ever data.
3. The model returns find/replace edits: `{path, find, replace, rationale,
   episode_ids}`. Code builds the unified diff with `difflib`, because models
   get unified-diff line numbers wrong. The stored record is still a unified
   diff.
4. Code checks each edit and drops it, with a logged reason, unless all of
   these hold:
   - the path is `/memories/AGENTS.md`, an existing skill's `SKILL.md`, or a
     wiki page (`/wiki/<name>.md`);
   - `find` occurs exactly once in the file. An empty `find` creates a file
     that does not exist yet, which is how the first preference lands, since
     `AGENTS.md` is not seeded;
   - the edit changes the file;
   - it cites only episodes given to this run;
   - the file stays under its cap: 8000 characters for `AGENTS.md`, 20000
     for a skill or wiki page;
   - the new text contains no known secret;
   - an edited `SKILL.md` still parses with deepagents' own skill parser.
     That parser only warns on a name mismatch, but drops a skill whose
     frontmatter is broken.

   Edits past `DOT_REFLECTION_MAX_EDITS` are dropped.
5. Writes each surviving edit as a `memory_versions` row with status
   `proposed`. Its `detail` holds the path, the rationale, the SHA-256 of the
   file it was made against, and the schedule. Then it moves the cursor to the
   last episode read. A model failure raises: the worker records the error on
   the inbox row, and no row is written and the cursor stays put.

The cursor lives in the store at `(dot_id, "reflection")`, key `/cursor`.
The rows and the cursor are written over separate connections. A crash
between the two makes the next run read the same episodes again and may
propose the same edit twice; the gate's stale-base check (L4) rejects the
second copy once the first is applied.
Reflection never writes memory. Until the gate exists (L3), `proposed` is
where an edit stops.

**Memory reloads every turn.** deepagents loads `AGENTS.md` and the skills
list once and keeps them in thread state. A dot's thread never ends, so no
edit would ever reach it. `middleware/memory.py` replaces both middlewares, by
name, with versions that reload at the start of every turn. That also makes
hand edits to memory take effect.

## L3. Replay gate

The nightly reflection row runs the gate right after reflection, under the
same dot lock (`memory/replay.py`, `gate_proposed`). It judges every
`proposed` row of the dot, oldest first, against memory as the previous
verdict left it. Replays use the `supervisor` model, the one that made the
original proposals. `memory/versions.py` records each verdict and applies
accepted edits.

Replay agents fail hard: a model error raises instead of becoming a reply,
which a reject or correction episode would otherwise score as a match. A row
whose replay raises gets no verdict and stays `proposed` for the next night;
the rows after it are still judged. An episode whose profile is no longer in
the pack cannot be replayed. Replay agents offload large tool results to a
temporary folder, not the dot's object folder.

### Replaying one episode

Replay never touches the dot's live thread, store or tables.

1. Read the state at the episode's checkpoint. Drop the trailing AI message:
   that is the proposal being re-made.
2. Build a scratch `GraphRuntime` with a `MemorySaver` and an
   `InMemoryStore`. Copy the dot's memory and wiki files into the store. For
   the candidate arm, use the edited file. The scratch runtime has no
   repositories and no job store.
3. Build the agent with `build_dot_agent(dot, profile, runtime=scratch,
   model=..., deps=ToolDeps(artifacts), sandbox_factory=<raises>,
   replay=ReplayStop(...))`. One assembly.
4. Seed a fresh thread with the messages only. deepagents caches
   `AGENTS.md` in `state["memory_contents"]`. Copying the whole state would
   carry the old memory into the candidate arm.
5. Run until `ReplayStop` fires, then read the AI message that triggered it.

`ReplayStop` (`middleware/replay.py`) is an `after_model` middleware that
jumps to `end`, so no tool node runs, when the model's message:

- calls the episode's tool;
- calls any tool that is not a plain read: its effect is not `read`, policy
  does not simply allow it, or it delegates (`task`, the job tools, the
  finding tools);
- or has no tool calls (a final reply).

Assembly binds the policy check and puts `ReplayStop` last, so its
`after_model` runs before the Guardian's and the approval review's.

Reads under `/memories/` and `/wiki/` run against the scratch store, so a
skill or wiki edit that only works once it is read gets the chance to work.
Replay's tool deps carry no search, fetch, email, Slack or credentials, so a
read that needs the network fails instead of reaching it, and the sandbox
factory raises. After `DOT_REPLAY_MAX_MODEL_CALLS` (default 4) model calls
without a stop, the replay ends with stop `budget`, which is a mismatch.

Because the stop fires before any consequential tool, replay reaches no
Guardian review, no HITL interrupt, no approval card and no inbox row. With
`audit_repositories=None` it writes no audit rows either. A test asserts that a
replay leaves every repository table unchanged.

### Comparing a replay with the human action

The comparator is code, one function per human action. Tools may register a
field comparator. The default compares fields as follows:

- strings match when the normalised `difflib` ratio is at least 0.8;
- everything else must be equal.

| Human action | Target | `match` when the replayed message… |
|---|---|---|
| `approve` | the approved `args` | calls the same tool with matching args |
| `edit` | `edited_args` | calls the same tool with matching args |
| `reject` | the rejected call | does not call that tool with matching args |
| `correction` | the corrected call | does not repeat any of the corrected tool calls |

The comparators live in `memory/compare.py`. Both sides must have the same
argument names. `send_email` and `draft_email` register their own rules: `to`
must be the same address (ignoring case and spaces), and `body` matches when its word count is within 25% of the
target's. `subject` uses the default string rule. An edit that shortens emails
is therefore judged on length, not wording, which is what the human changed.

### The gate

Before replaying, the gate re-applies the edit's recorded `find`/`replace`
to the file as it is now and runs reflection's checks again. If that fails
(another edit tonight changed the text, or created the file), the edit is
`rejected` with `stale_base: <reason>` and nothing is replayed.

For each edit, take:

- the cited episodes that can be replayed (at most `DOT_REPLAY_CITED`,
  default 5);
- plus up to `DOT_REPLAY_RANDOM` (default 5) other replayable episodes of the
  same dot, drawn from its oldest 1000 with a seed derived from the edit's
  path and diff, so the result can be reproduced.

Replay each one twice:

- **baseline:** current memory;
- **candidate:** current memory with the edit applied.

Both arms use the same model and settings. The stored proposal is never the
baseline, because model drift would then count against every edit.

**Keep the edit only if** both of these hold:

- matches(candidate) ≥ matches(baseline) over the sample;
- at least one cited episode goes from mismatch to match.

An edit with no replayable cited episode is not applied. It is stored as
`needs_review` for a human to accept or discard.

The verdict goes in the row's `detail.gate`: the reason (`matches 0 -> 3`,
`match rate fell from 2 to 0`, `no cited episode improved`, …), every replay
result, and the episodes that could not be replayed, with why:

```json
{"episode": 12, "arm": "candidate", "match": true, "stop": "tool:send_email", "calls": 2}
```

An accepted edit is written to the store first, then the row is updated with
the diff as judged. If the row update fails, the edit is in memory but the
row still says `proposed`. The next gate then rejects it as a stale base,
because its `find` text is gone or its `replace` text is already in the file
(an edit that keeps its `find`, such as appending a line), and does not apply
it twice.

## L4. Versions and rollback

`memory_versions(id, dot_id, at, diff, episodes, status, detail)` gets one
row per edit that passed reflection's checks (migration 006 adds `detail`):

- `proposed`: drafted by reflection, not yet replayed;
- `accepted`: applied, by the gate or by an approver;
- `rejected`: failed the gate, or no longer applied to the file (`stale_base`);
- `needs_review`: the gate had nothing to replay, so it waits for an approver;
- `discarded`: an approver turned down a `needs_review` edit;
- `rolled_back`: an accepted edit an approver has undone.

`memory/versions.py` owns every change of status after reflection. When an
edit is applied, its row keeps what rollback needs: the whole prior file
(`detail.before`, absent if the edit created the file) and the SHA-256 of the
file it wrote (`detail.after_sha256`). The API view leaves `before` out.

**Approver actions.** `POST /dots/{dot_id}/memory/{version_id}/{action}`, for
pack approvers only (401 without an identity, 403 otherwise):

| Action | From | Does |
|---|---|---|
| `accept` | `needs_review` | Re-applies the edit to the file as it is now, with all of reflection's checks, and writes it. If it no longer applies: 409, nothing changes |
| `discard` | `needs_review` | Marks it `discarded`; memory is untouched |
| `rollback` | `accepted` | Undoes the edit (below) |

**Rollback.** If the file still has the hash the edit wrote, nothing has
changed it since, so the prior file is restored exactly, or removed if the
edit created it. Otherwise the edit's own `replace` text is swapped back for
its `find` text, which needs that text to still be in the file exactly once.
Other edits since then are kept. It is a 409 when the text is gone (a later
edit rewrote it), or when a newer accepted edit to the same file has this
edit's text inside its `find`: that edit is built on this one, and swapping
this one back would leave it impossible to roll back. Roll back newest first.

**Rules enforced in code.**

- An action runs under the worker's per-dot advisory lock. If a turn or the
  gate holds it, the action answers 409 "the dot is busy" and changes
  nothing.
- The row's new status and an audit event (`kind=memory`, `decision` =
  `accept`, `discard` or `rollback`, the actor, the version and path) are
  written in one transaction, and only if the row is still in the status
  the action expects. A repeated or concurrent action gets 409 and writes no
  second audit event.
- The store is written before the row. If the row write then fails, the
  file has changed but the row has not. The next action or gate finds the
  edit already in the file, or its `find` text gone, and refuses, rather
  than applying anything twice.
- Rows the gate accepted before rollback support existed have no `before`
  or `after_sha256`; their rollback always uses the text swap.
- Gate verdicts are code, not human actions, so they are not audited. Their
  reasons and replay results are in `detail.gate`.

The web memory page shows each version's status, rationale, replay reason
and diff, with **Accept** and **Discard** on held edits and **Roll back** on
accepted ones.

## Settings

| Setting | Default |
|---|---|
| `DOT_REFLECTION_MAX_EPISODES` | 200 |
| `DOT_REFLECTION_MAX_EDITS` | 5 |
| `DOT_REPLAY_CITED` | 5 |
| `DOT_REPLAY_RANDOM` | 5 |
| `DOT_REPLAY_MAX_MODEL_CALLS` | 4 |

## Tests

These are all offline, with the scripted model.

- **Shorter emails accepted** (`tests/unit/test_replay.py`). Seed three `edit` episodes through real approval turns, in which the human
  cut a `send_email` body from about 200 words to about 60. The scripted
  supervisor is a callable step: it returns a 60-word body when the system
  prompt contains the preference, and a 200-word body when it doesn't. The
  scripted reflection proposes the preference for `AGENTS.md`. Replay accepts
  it: the baseline matches 0 of 3, the candidate 3 of 3. The next live turn
  drafts the short body.
- **Harmful edit rejected.** Seed approved episodes that match under the
  current memory, plus a cited edit episode. Propose an edit that changes
  `to` behaviour. The candidate loses approved matches, so the edit is
  `rejected`, and the store is unchanged.
- **Rollback** (the acceptance test, and `tests/unit/test_versions.py`).
  After the accepted preference, rollback removes `AGENTS.md` and the next
  draft is long again. A skill edit rolls back to the prior file byte for
  byte. A rollback after an unrelated later edit swaps back only its own
  text; one whose text a later edit rewrote is refused with 409 until the
  later edit is rolled back. Held edits can be accepted or discarded, a stale
  accept is refused, every action is audited once, and only approvers may
  act. On Postgres, an action waits for the dot's lock (409 while a turn
  holds it).
- **Replay writes nothing.** Repository tables and the dot's checkpoints are
  identical before and after a replay. Transports raise if called.
- **Unreplayable episodes.** A job episode, a missing checkpoint and a
  text-only correction are reported as `unreplayable` and never counted as
  matches. An edit citing only those is stored as `needs_review`.
- **Corrections.** The route resolves a message to its checkpoint, refuses
  unknown messages and non-approvers, and writes one episode.

## Open items

- Live models are not deterministic. Each arm is replayed once at the model
  factory's temperature. If E2 shows the gate flapping, replay each arm k
  times and compare majorities.
- Replay depends on checkpoints being retained. If checkpoint pruning is
  added, it must keep checkpoints that episodes reference for as long as
  those episodes can be sampled.
