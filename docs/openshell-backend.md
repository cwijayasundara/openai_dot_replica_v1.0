# OpenShell backend (X2)

The backend implements `RunSandbox`, using the installed OpenShell 0.1.2 SDK
and the execution/file-transfer pattern from
[OpenShellBackend](https://github.com/langchain-ai/openshell-deepagent/blob/main/src/backend.py).
Assembly creates it lazily when `DOT_SANDBOX_BACKEND=openshell`.

## Local setup

Install the OpenShell CLI and register/start a gateway following the
[OpenShell quickstart](https://docs.nvidia.com/openshell/get-started/quickstart).
Set `DOT_OPENSHELL_GATEWAY` to its registered **name**, not its URL.
The SDK uses that gateway's host-side authentication; the sandbox receives no
providers or application credentials.

Build the base image with `docker compose build sandbox`. The default image
builder uses the Docker daemon selected by the Docker SDK. The gateway's Docker
driver must be able to see images built on that daemon. This is a local Docker
gateway workflow; a Kubernetes gateway does not automatically import local
Docker images.

Set:

```sh
export DOT_SANDBOX_BACKEND=openshell
export DOT_OPENSHELL_GATEWAY=local
export DOT_SANDBOX_IMAGE=dot-sandbox
```

Inputs live at `DOT_OBJECT_ROOT/<dot_id>/sandbox/in`; assembly copies the pack's
skills into the sibling `skills` directory on first sandbox use. Both trees are
copied into a deterministic image layer before sandbox creation, owned by root,
and stripped of write permission. Input symlinks and special files are refused.
OpenShell starts with its final filesystem policy: `/in` and `/skills` read-only,
`/work` writable, and Landlock required. No temporary writable policy is used.
This image step is necessary because OpenShell's
[filesystem policy is static](https://docs.nvidia.com/openshell/latest/reference/policy-schema).

The `default` policy denies egress. The explicit `research` profile selects
`research.yaml`, which permits only `/usr/bin/curl` to make read-only HTTPS REST
requests to `api.github.com` and `en.wikipedia.org`. Unknown profiles select
`default`. Existing chat, sweep, and digest profiles therefore deny sandbox
egress. Update the checked-in allowlist deliberately when adding research
sources; native web tools run separately from the sandbox.

OpenShell's proxy and CA environment settings are preserved, while application
environment values are cleared. Policy-denied responses and failed execution
are appended to the dot's existing audit repository, including the diagnostic,
sandbox ID, and policy. Descriptive output is also returned to the caller.
The audit trail records returned diagnostics; it is not an ingestion of every
OpenShell gateway network log, and a command that hides a denial's output and
returns success may hide it from this adapter.

Sandboxes are reused per dot. Changing to another policy snapshots `/work`,
retires the previous sandbox, and restores the work in the replacement. The
backend exposes `suspend_if_idle` for an idle lifecycle caller; no periodic
idle timer is introduced by X2.

For a remote gateway, supply `OpenShellSandbox(image_builder=...)` with a builder
that returns an image reference already accessible to that gateway, including
the sealed inputs and skills. The default builder builds locally and never
pushes to a registry. Automatic remote image publication is not implemented.

## Local gateway on macOS (verified 2026-10-01)

The installer at `https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh`,
with `OPENSHELL_VERSION=v0.1.2`:
- installs OpenShell through Homebrew;
- runs the gateway as a launchd service (`brew services`), which starts at
  login;
- registers the gateway as `openshell` at `https://localhost:17670`.

Set `DOT_OPENSHELL_GATEWAY=openshell`.

**Docker Desktop.** The gateway uses its Docker driver. By default, sandbox
supervisors call back on `https://127.0.0.1:17670`, which inside a container is
the container itself. Add this to `/opt/homebrew/var/openshell/gateway.toml`,
then run `brew services restart openshell`:

```toml
[openshell.drivers.docker]
grpc_endpoint = "https://host.docker.internal:17670"
```

**Gateway rules our policies and names must follow** (fixed after the first
gateway run):
- Sandbox names are at most 19 characters.
- `run_as_user`/`run_as_group` are `sandbox` or a numeric id; ours is `"1000"`.
- REST endpoints must not set `tls`; the proxy terminates TLS itself.

**Egress denials in v0.1.2:**
- **A host outside the policy** is refused at connect time. The command sees
  only "could not connect". The descriptive reason
  (`NET:REFUSE … DENIED <host>`) is in `openshell logs <sandbox>`, pushed
  asynchronously.
- **A disallowed method or path on an allowed host** gets the proxy's
  descriptive `policy_denied` body, which is audited as a `sandbox_denial`.

**Docker credential helper.** The image builder uses the Docker SDK, which
loads every credential helper in `~/.docker/config.json`. If one of those
helpers is logged out (for example `docker-credential-gcloud`), builds fail;
`gcloud auth login` fixes it. Alternatively, point `DOCKER_CONFIG` at an empty
config and set `DOCKER_HOST` to the active context's socket.

## Verification

```sh
uv run pytest -q
uv run pytest -q -m docker tests/contract/test_sandbox_contract.py tests/contract/test_docker_sandbox_restore.py
uv run pytest -q -m openshell tests/contract/test_sandbox_contract.py
```

The shared contract checks working directory, binary file transfer, inherited
read/write/edit calls, filesystem restrictions, and command timeout. The
OpenShell-specific test checks an allowed GET, a denied destination, a denied
POST, descriptive errors, and append-only audit events. Gateway tests require
a working gateway and the base image; they fail when explicitly requested
without those prerequisites. They do not call a model.

Offline tests cover SDK request construction, policy conversion, image contents
and sealing, path traversal, idempotent lifecycle/cleanup, diagnostic propagation,
audit persistence, snapshot/restore, and assembly policy selection. Offline
success does not establish that a gateway enforces these controls.

**X2 verification (2026-10-01).** Both OpenShell contract tests pass against
the local 0.1.2 gateway. The coder CSV tests pass under OpenShell, both the
scripted run and the live run with Kimi K3.

**Remaining gap for E3:** host-level denials are recorded in our audit as
`sandbox_error`, not as a denial, because OpenShell's OCSF events are not
ingested yet.
