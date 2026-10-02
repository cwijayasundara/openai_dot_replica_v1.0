# Proactivity (P1, P2)

A pack's `schedules` make a dot act without being asked. Two kinds run an
agent; a third, `reflection`, runs no agent and is described in
[`learning-loop.md`](learning-loop.md).

| Kind | Thread | Can do | Leaves | Says to the user |
|---|---|---|---|---|
| `sweep` (default) | Its own, one per run | `read` tools, `record_finding`, `list_findings`, granted subagents through `task` | Findings | Nothing |
| `digest` | The dot's thread | Its profile's tools | Findings marked `reported` | One summary, in the default channel |

```yaml
schedules:
  - {name: sweep, cron: "*/30 7-22 * * 1-5", profile: sweep, prompt: "Run your sweep.",
     max_model_calls: 15, max_tokens: 150000}
  - {name: digest, kind: digest, cron: "45 8 * * 1-5", profile: digest, prompt: "Send the morning digest.",
     max_model_calls: 6, max_tokens: 60000}
```

## Rules enforced in code

- **Sweeps only read.** The pack fails to load if a sweep's profile, or a
  subagent it grants, reaches a tool whose effect is not `read`. `SurfaceGuard`
  refuses anything the model was not offered. A sweep gets no job tools,
  because a background job would escape the budget. Its replies are delivered
  nowhere and are not streamed as messages. Its thread is deleted when the
  run ends; its findings and audit rows are the record.
- **Findings.** `record_finding(title, summary, score, sources)` writes a row
  with status `open`. The schedule is bound from code. Text sizes are capped,
  the score must be in 0..1, and an open finding with the same title is
  returned instead of being duplicated. `record_finding` writes only to the
  dot's own inbox, so it is a `read` effect.
- **The digest.** It is skipped when nothing is open. Its request carries the
  open findings (up to 30, highest score first) as untrusted data. A digest
  with `findings_from: [sweep, ...]` is shown only those sweeps' findings, so
  two digests on one dot do not consume each other's. The
  Guardian's objective is the schedule's prompt, never the findings' text. The
  reply goes to the bound Slack channel (`dot link-slack --channel`) as a new
  message. With no channel bound, it stays in the web thread. After a turn
  that ends in a reply, or waits at an approval, exactly the findings it was shown are marked
  `reported`, so the next digest does not repeat them. While the dot's
  thread waits for an approval, the digest fails with "thread is paused for
  human review". Its findings stay open and the next firing tries again.
- **Budgets.** Each run has one model-call and token ceiling, shared with its
  subagents. It comes from the schedule, else from `DOT_SCHEDULE_MAX_MODEL_CALLS`
  and `DOT_SCHEDULE_MAX_TOKENS`. The next call past the ceiling ends the run
  with no message and records a `budget` finding. A digest stopped this way
  posts nothing and marks nothing.
- **One run per firing.** A schedule row runs as a turn of its own. A firing
  is dropped while that schedule's last row for the dot is pending, and a slot
  (the fire time to the minute) never runs twice.

## Running it

Crons are five-field crontab, read in `DOT_SCHEDULE_TIMEZONE` (an IANA zone).
Day-of-week `0` and `7` are Sunday. Steps such as `*/2` in the day-of-week
field are refused.

**Locally**, run exactly one scheduler:

```bash
uv run python -m dot.proactive.scheduler   # or: docker compose up -d scheduler
```

It adds one cron job per pack schedule. Each firing queues a row for every
active dot of that pack, so dots created later are included.

**On GCP**, Cloud Scheduler calls the API instead, with one job per dot and
schedule:

```
POST /schedules/{dot_id}/{schedule}
Authorization: Bearer <Google OIDC token>
```

Set `DOT_SCHEDULER_AUDIENCE` to the job's OIDC audience and
`DOT_SCHEDULER_INVOKER` to its service account email. The token must be
Google-signed, for that audience, with that `email` verified. Without both
settings the route answers 503. The request body is ignored: profile and
prompt come from the pack. The `X-CloudScheduler-ScheduleTime` header sets
the slot, so a retried attempt is not queued twice. Answers are 202 with
`queued: true|false`, 401 for a bad token (checked before the dot is looked
up), and 404 for an unknown dot or schedule. A paused dot is not queued. Cloud
Scheduler must reach this route without passing IAP; that is Phase 11 infra.

## Tests

`tests/unit/test_proactive.py` holds the acceptance tests. A scripted sweep
records a finding with email and Slack transports that fail if called. The
sweep surface excludes `send_email`, `slack_post` and `start_job`. The digest
posts once to the bound channel and marks its findings. Budgets count
subagent calls and tokens. `tests/unit/test_scheduler.py` covers cron
weekdays, coalescing and the webhook's token checks. The repository contract
covers `list_active_dots` and `insert_schedule_run` in memory and Postgres.
