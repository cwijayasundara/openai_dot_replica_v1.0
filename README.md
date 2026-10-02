# Open dot

An open, self-hostable version of always-on agents ("dots"). Each dot has one pack, one continuous thread and one sandboxed computer. The design is in [`docs/design.md`](docs/design.md), and the order of work is in [`docs/implementation-plan.md`](docs/implementation-plan.md).

## Before you start

- **Docker**, with Docker Compose v2.
- **A `.env` file.** Copy the template, then fill in the values you need:

  ```bash
  cp .env.example .env
  ```

  - **Required:** `DOT_FIREWORKS_API_KEY`, the model key. Every message, sweep and nightly reflection calls live models, so running the stack costs money.
  - **Optional:** `DOT_TAVILY_API_KEY` for web search, the Slack tokens, and the SMTP settings.

  `.env` is git-ignored. Never commit it.

## Start it: three commands

### 1. Set up the Docker resources (once, and after changes to `sandbox/`)

```bash
docker compose up -d postgres && docker compose build sandbox
```

This starts Postgres on `localhost:55434` and builds the `dot-sandbox` image that each dot's sandboxed computer runs in. Compose only builds that image; it never starts it.

### 2. Start the backend

```bash
docker compose up -d --build api worker scheduler
```

- **`api`** runs on `localhost:8000`. It queues messages and streams events; check it with `curl localhost:8000/health`.
- **`worker`** runs each dot's turns, background jobs, and nightly reflection with its replay gate.
- **`scheduler`** fires each pack's schedules: sweeps, digests and reflection.

To connect Slack as well, add `--profile slack` to that command. It needs valid `DOT_SLACK_BOT_TOKEN` and `DOT_SLACK_APP_TOKEN` values in `.env`.

### 3. Start the frontend

```bash
docker compose up -d --build web
```

Open [http://localhost:3000](http://localhost:3000). Locally, the web UI signs you in as one fixed user (`DOT_WEB_DEV_USER`, `local` by default).

To work on the UI with hot reload instead, run `cd web && pnpm install && pnpm dev`. That also serves on port 3000 and talks to the API on `localhost:8000`.

## The onboarding-ops dot

The `onboarding-ops` pack watches a local drop folder and operates the recon workbench ([`../recon_knowledge_work_agent_v2`](../recon_knowledge_work_agent_v2)) over its HTTP API. It never answers a gate: runs start only after you approve them.

1. **Start the workbench** from its repo, on port 8100:

   ```bash
   scripts/start-backend.sh --port 8100
   ```

   Sponsors `sponsor-a` and `sponsor-b` are seeded by default.
   The script needs `OPENAI_API_KEY` in the workbench's own `.env`. Without `--db`, its runs are kept in memory and lost when it stops.
2. **Point the dot at it.** In `.env`, set `DOT_RECON_URL=http://host.docker.internal:8100`. If the workbench needs a token, add `"cred:recon"` to `DOT_CREDENTIAL_BINDINGS`. `DOT_RECON_DROP_ROOT` is set by compose; leave it empty in `.env`. `host.docker.internal` resolves on Docker Desktop (macOS and Windows). On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `api` and `worker` services, or use the host's IP.
3. **Rebuild the backend** so the containers pick up the settings: `docker compose up -d --build api worker scheduler` (step 2 above).
4. **Create the dot.** In the web UI, choose `onboarding-ops` in the Pack select and press Create dot.
5. **Drop a file** into a sponsor folder, creating it if needed. Compose mounts `./var/drops` read-only into `api` and `worker`:

   ```bash
   mkdir -p var/drops/sponsor-a && cp some-file.xlsx var/drops/sponsor-a/
   ```

What happens next: on weekdays during working hours in `DOT_SCHEDULE_TIMEZONE`, the intake sweep runs every 15 minutes (`*/15 7-17`), so within 15 minutes the dot records a finding for the new file. The intake pass follows (at 5, 20, 35 and 50 past the hour) and puts an approval card in front of you. Approving it starts the recon run. The status sweep runs hourly, and the daily digest at 06:45, before the first intake. Outside those hours or at weekends, the file waits for the next firing.

Decide start cards promptly — while one is pending, the dot's scheduled runs are paused. Sweeps and digests are not queued until you approve or reject it, and the slots they miss are not caught up.

There is no command or dev route to fire a schedule by hand: the API webhook (`POST /schedules/{dot_id}/{name}`) needs a Cloud Scheduler OIDC token. To try it quickly, wait for the next firing, or temporarily change the crons in `packs/onboarding-ops/pack.yaml` and rebuild with `docker compose up -d --build scheduler`. Packs are copied into the image, so a restart alone picks up nothing. Only the scheduler reads crons; if you change prompts or profiles, rebuild `worker` too. Reverting the edit needs the same rebuild.

## Day to day

```bash
docker compose ps                                    # what is running
docker compose logs -f worker                        # follow one service's logs
docker compose --profile slack down                  # stop everything (data is kept)
docker compose --profile slack down -v               # stop and delete the database and object volumes
```

## Troubleshooting

- **Port 3000 is in use.** Another app is on that port. Stop it, or change the web service's port mapping in `docker-compose.yml`.
- **Slack keeps restarting with `invalid_auth`.** The bot token in `.env` was revoked or has expired. Reinstall the Slack app from `config/slack-app-manifest.yaml` and copy the new tokens into `.env`.
- **Your dots disappeared after running tests.** `uv run pytest -m db` uses the same local Postgres database and empties its tables. Don't run the database tests against data you want to keep.
- **`import dot` fails outside pytest on macOS.** See the note in [`CLAUDE.md`](CLAUDE.md) about the hidden `.pth` flag.

## Development

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q                                   # offline: no live models, Docker or database
cd web && pnpm format:check && pnpm typecheck && pnpm test:e2e
```

[`CLAUDE.md`](CLAUDE.md) has the project rules and the full command list.
