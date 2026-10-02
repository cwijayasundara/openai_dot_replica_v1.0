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
