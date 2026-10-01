# Web UI (C2)

The web UI is a Next.js app in `web/`. Its pages are:

- **Dot list** (`/`): your dots, and a form to create one from a pack.
- **Dot home** (`/dots/{id}`): the thread, with live updates, and a side rail.
  The rail shows approvals waiting on you, background jobs, findings and
  recent decisions.
- **Audit trail**, **Memory** and **Sandbox** tabs for the same dot.

The thread is the dot's single checkpointed thread, so a message sent from
Slack appears here too. Each inbound message is labelled with where it came
from: Web, Slack, a job result or a schedule.

## How it reaches the API

The browser only talks to the web origin. `next.config.ts` rewrites `/api/*`
to `DOT_API_URL` (default `http://localhost:8000`), so the API needs no CORS.
On GCP, one identity proxy covers both. Live updates use an `EventSource` on
`/api/dots/{id}/events`. Response compression is off so the proxied SSE stream
is not buffered. The API sends a keepalive comment every 15 s so proxies keep
an idle stream open. Each streamed event makes the page reload the dot's data
once the burst settles (150 ms). After a reconnect the page reloads anyway,
because events published while it was disconnected are not replayed.

## Identity

Routes that need a user read `request.state.user_id`. Only
`src/dot/surfaces/identity.py` sets it, and never from a request body or a
header the client can choose.

| `DOT_WEB_AUTH` | Who the caller is |
|---|---|
| `off` (default) | Nobody. Routes that need a user return 401. |
| `dev` | `DOT_WEB_DEV_USER`, a fixed local user. Refused unless `DOT_ENV=local`. |
| `iap` | The `sub` of a verified `x-goog-iap-jwt-assertion`, mapped through `users.web_subject`. |

In `iap` mode the assertion must carry a valid Google IAP signature (keys
cached for an hour), the issuer `https://cloud.google.com/iap` and the
audience `DOT_IAP_AUDIENCE`. A subject with no linked user gets 401. Linking
web subjects to users, and the IAP audience when the web proxy forwards the
header to the API, are Phase 11 deployment tasks.

These routes need the caller to be the dot's owner or a pack approver (401
without a user, 403 otherwise):

| Route | Returns |
|---|---|
| `GET /dots/{id}/jobs` | Jobs, newest first |
| `GET /dots/{id}/approvals?status=` | Cards, pending first. `args` is the proposal; `edit.edited_args` holds the human's edit |
| `GET /dots/{id}/findings?status=` | Findings, newest first |
| `GET /dots/{id}/memory` | Memory versions with their diffs, newest first |
| `GET /dots/{id}/audit` | The audit trail (unchanged) |
| `GET /dots/{id}/sandbox` | Audit rows from the pack's sandboxed subagents |

`GET /me` and `GET /dots` need a user. `GET /packs` does not. Every free-text
field is redacted. Deciding a card still needs a pack approver
(`DOT_PACK_APPROVERS`); owning the dot is not enough.

`POST /dots`, `GET /dots/{id}`, `POST /dots/{id}/messages`,
`GET /dots/{id}/thread` and `GET /dots/{id}/events` are still open, as they
were before C2.

## Run it locally

```bash
docker compose up -d postgres
# .env: DOT_WEB_AUTH=dev, DOT_WEB_DEV_USER=local, DOT_PACK_APPROVERS={"research-analyst":["local"]}
uv run uvicorn dot.surfaces.api:app --port 8000
uv run python -m dot.runtime.worker
cd web && pnpm dev            # http://localhost:3000
```

`docker compose up` sets `DOT_WEB_AUTH=dev` and `DOT_WEB_DEV_USER=local` on the
`api` service, and builds `web` with `DOT_API_URL=http://api:8000`.

## Tests

```bash
uv run pytest -q tests/unit/test_identity.py tests/unit/test_web_routes.py
cd web && pnpm typecheck && pnpm test:e2e
```

`pnpm test:e2e` is the C2 acceptance test. Playwright starts
`tests/support/web_e2e_server.py`, which runs the real API, graph, policy,
Guardian and approvals with a scripted model and no network. One thread drains
the inbox and job queue in order. The Next.js dev server runs on port 3100 with
its own build directory, so it can run beside `pnpm dev`.

The test goes through one story:

1. Create a dot in the UI.
2. Send a DM through the Slack adapter's inbound router, and see it labelled
   Slack in the thread, with the reply.
3. Ask for research from the web. A `researcher` job runs and finishes.
4. The job result leads to a `send_email` card. Edit its body and approve.
5. Check that the email went to the fake transport.
6. Check that the audit trail has the Guardian verdicts and both approval rows.

Run `pnpm exec playwright install chromium` once before the first run.
