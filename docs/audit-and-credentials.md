# Audit log and credentials (G4)

The one assembly shares a runtime redactor and wires audit writers into the
supervisor and every configured subagent. Production API, worker and CLI
runtimes bind `audit_repositories`; standalone graph tests may omit repositories.
The audit log is append-only in both repositories. PostgreSQL rejects updates
and deletes with the existing database trigger. In-memory reads return copied
JSON, so callers cannot mutate stored history through a returned event.

## Event trail

Each turn has a checkpointed UUID `audit_turn_id`, inherited by subagents and
preserved across approval resumes. Event detail includes `thread_id`, `profile`
and `turn_id`; tool events also include the call id and redacted arguments.

- `tool_call`: proposed, attempted, result or error phase.
- `policy`: deterministic decision at proposal and again before execution.
- `surface`: a hidden tool or unauthorized delegation was refused.
- `guardian`: verdict, reason and allow/block decision; edited calls get a new review.
- `approval`: pending and the authorized approve/edit/reject action.

An attempt is not proof of execution: guards may refuse it. Results record
status, not raw tool output. Native JSON `ok: false` is recorded as an error.
Human rejection has no execution attempt. A storage failure before a tool call
prevents execution; a failure after a side effect cannot undo that side effect.
Pending approval cards and their audit rows commit together. Decisions,
episodes, the final resume inbox row and approval audit rows also commit
together. Duplicate decisions produce no duplicate decision audit record.

`GET /dots/{dot_id}/audit?turn_id=<uuid>&after_id=0&limit=100` returns `events`
in id order and `next_after_id`. Follow the cursor until it is null to obtain
the complete trail. `limit` is 1–1000; `turn_id` is optional. A verified
`request.state.user_id` must be the dot owner or a pack approver; missing
identity gets 401, denied access gets 403. There is no client identity-header
fallback. The deployment authentication adapter is still a later task.

## Credential sources

Local configuration:

```dotenv
DOT_CREDENTIAL_BACKEND=env
DOT_CREDENTIAL_BINDINGS={"cred:smtp":"DOT_SMTP_CREDENTIAL","cred:slack-bot":"DOT_SLACK_BOT_TOKEN"}
DOT_SMTP_CREDENTIAL=
DOT_SLACK_BOT_TOKEN=
```

Fill values only in the ignored local `.env`. These two known settings support
`.env` loading. Custom handle bindings read named process environment variables.
The broker never derives environment names or resource paths from a model
argument: only administrator-configured handles are accepted. Native email
code resolves the fixed `cred:smtp` handle inside the tool. Email and Slack
transports remain injected adapters; this task does not select an SMTP/Gmail
provider or configure an external messaging service.

For GCP set `DOT_CREDENTIAL_BACKEND=secret_manager` and bind each handle to a
version resource, for example
`projects/example-project/secrets/smtp/versions/latest`. The SDK client is
created only when the tool resolves a handle, uses Application Default
Credentials, validates the payload CRC32C and decodes UTF-8. Calls have a
10-second timeout with provider retries disabled. This follows Google's
[official access-secret-version pattern](https://docs.cloud.google.com/secret-manager/docs/samples/secretmanager-access-secret-version).
No keys are passed to the sandbox. Runtime permissions and live GCP validation
belong to deployment; the G4 tests use a fake Secret Manager client.

Every resolved value is registered before reaching a transport. Supervisor,
subagent and Guardian requests share this dynamic redactor, including after
graphs are rebuilt for resumes. Redaction covers message content, nested
tool arguments, system messages, reasoning/response metadata and tool
artifacts, plus JSON-escaped values. Tool transport exceptions become generic
error messages rather than exposing provider exception text. Offloaded tool
responses are redacted before their preview/storage step. Audit arguments and
verdicts are scrubbed too. This is defense in depth, not a promise to recognize
arbitrary transformed or previously unknown secrets in untrusted content.

Verification: `tests/contract/test_audit.py`, `tests/unit/test_credentials.py`,
the shared repository contract, and approval contracts. No live model, cloud
credential access or external message send is used.

## Live Secret Manager check

```sh
DOT_GCP_TEST_PROJECT=<project> uv run pytest -q -m gcp tests/live/test_secret_manager_live.py
```

This needs application-default credentials (`gcloud auth application-default login`).
It creates a throwaway secret and deletes it afterwards. It passed against
`biz2bricks-dev-v1` on 2026-10-01.
