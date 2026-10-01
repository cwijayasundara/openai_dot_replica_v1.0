# Slack channel (C1)

People reach their dot by DM or by mentioning it in a linked channel. Replies,
job results and approval cards come back in the same Slack thread. Approval
buttons call the same `decide()` as `POST /approvals/{id}`.

## How it works

**Inbound** (`src/dot/channels/slack.py`, `SlackInboundRouter`). Slack's request
verification vouches for the sender's id: Socket Mode locally, or a signed
request at `POST /slack/events` on GCP.

- **DM.** The sender's Slack id (`users.slack_user_id`) gives the owner, and
  the owner gives their dot. If the owner has no dot, or more than one, the
  sender gets a "not linked" notice.
- **Mention in a channel.** `channel_bindings` gives the dot for that channel.
  Only the dot's owner is accepted. Anyone else gets a private notice, and no
  inbox row is written. Nobody else can drive the owner's dot or set the
  Guardian's objective.
- **Ignored:** bot posts, edits and other subtypes. Each `(channel, ts)` is
  accepted once (`channel_events`), so a Slack redelivery is not queued twice.
  The bot's own `<@…>` mention is stripped from the text.
- **Inbox row.** Source `slack`, payload `{text, user, reply_ref: {channel,
  thread_ts}}`. A message in a thread replies in that thread; otherwise its own
  `ts` starts one.

**Outbound** (`DeliveringEventChannel`, `outbox`, `SlackDelivery`).

- **Which thread.** The worker tags each inbound message with its reply
  channel. Assistant replies, job results (through the job's origin) and
  approval cards (supervisor and job) carry that tag. The worker turns them into
  outbox rows; Slack's control is not in the agent loop.
- **Delivery.** One delivery loop per deployment (advisory lock
  `outbox:slack`) posts rows in order, so a thread never reorders. It is at
  least once: a crash between posting and marking repeats a post. A failing row
  is retried in place, holding back later rows, and is parked after 5 attempts.
  A 429 is honoured through slack_sdk's rate-limit retry handler.
- **Safe to post.** All text is escaped (`&`, `<`, `>`) and posted with
  `unfurl_links` and `unfurl_media` off. Injected `<!channel>`, disguised links
  and URL previews are inert. Long replies are split at about 3,500 characters.
- **Approval cards.** Block Kit with Approve, Edit and Reject. Only the
  `approval_id` travels in the buttons. Edit opens a modal with the arguments
  as JSON, validated before `decide()`. Arguments over the modal limit must be
  edited in the web UI.
  - Posted cards are recorded (`approval_posts`). The loop updates a card when
    it is decided, wherever that happened: Slack, the web or a job cancel.
- **Approvers.** The approver is the clicking Slack user's id. It is checked
  against the pack's `approvers` plus `DOT_PACK_APPROVERS` (a deployment
  setting, so workspace ids never go in the shipped pack).

## Known limits

- When web and Slack messages fold into one turn, the reply goes to the latest
  message's channel only.
- A failed turn posts nothing to Slack. Errors stay in the inbox row and the
  `error` event, and error text is never echoed to Slack.
- Model Markdown is posted as Slack mrkdwn without conversion.

## Set up a test workspace

1. Create the app from [`config/slack-app-manifest.yaml`](../config/slack-app-manifest.yaml):
   api.slack.com/apps → Create New App → From an app manifest. Then install it
   to the workspace.
2. In `.env`:
   ```
   DOT_SLACK_BOT_TOKEN=xoxb-…   # OAuth & Permissions → Bot User OAuth Token
   DOT_SLACK_APP_TOKEN=xapp-…   # Basic Information → App-Level Tokens, scope connections:write
   DOT_SLACK_SIGNING_SECRET=…   # Basic Information → Signing Secret
   DOT_SLACK_MODE=socket
   DOT_PACK_APPROVERS={"research-analyst":["<your Slack user id>"]}
   ```
   Your Slack user id is under Profile → ⋯ → Copy member ID.
3. Create a dot and link it:
   ```sh
   uv run dot create research-analyst --owner you
   uv run dot link-slack <dot_id> --user <your Slack user id> [--channel <channel id>]
   ```
4. Run the worker and the Slack process:
   ```sh
   uv run python -m dot.runtime.worker
   uv run python -m dot.channels.slack
   ```
   Or use compose: `docker compose --profile slack up worker slack`.

   **macOS.** If `uv run dot` or `python -m dot…` reports `No module named
   'dot.…'`, the virtualenv's `.pth` files have the macOS `hidden` flag, and
   CPython 3.12.11+ skips hidden `.pth` files. Seen with uv 0.8.13; tests are
   unaffected because pytest sets `pythonpath`. Either prefix commands with
   `PYTHONPATH=src`, or run
   `chflags nohidden .venv/lib/python3.12/site-packages/*.pth` and then use
   `uv run --no-sync …`.

## Live acceptance runbook

In a DM with the app:

1. Send: *Research open-source agent runtimes, then email a short brief to
   sam@example.com.* The dot replies in a thread, starts a `researcher` job and
   keeps answering.
2. While the job runs, send another question in the same thread. It is
   answered.
3. When the job finishes, the dot reports the brief in the thread and proposes
   `send_email`. An approval card appears in the thread.
4. Click **Edit**, change the subject, and submit. Then check:
   - the card updates to "Approved with edits by @you";
   - the dot confirms in the same thread;
   - the audit log shows the job, the Guardian verdicts and the approval.

**Expected results.** Search and email need `DOT_TAVILY_API_KEY` and the SMTP
settings ([`transports.md`](transports.md)).
- Without them, the brief says search is not configured.
- Without them, after approval `send_email` reports that email is not configured.

Either way, the C1 path is fully exercised.

## Tests

- `tests/unit/test_slack_channel.py` runs real Bolt dispatch against a fake
  WebClient. It covers:
  - routing and the owner check;
  - deduplication and bot or edit events;
  - escaping, threading, unfurl settings and chunking;
  - retry and parking;
  - cards: posting, approve, non-approver, the edit modal, and updates;
  - `link-slack`.
- `tests/contract/test_slack_db.py` (`-m db`) runs the outbox contract and the
  whole story against Postgres. The worker, the job runner and the delivery
  loop run concurrently. A DM, the job, the result, the approval card, the
  Approve click and the final reply all land in one thread, and the card is
  updated.
