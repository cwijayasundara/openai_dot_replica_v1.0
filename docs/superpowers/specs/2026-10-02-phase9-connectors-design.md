# Phase 9: Connectors and tool discovery, design

Date: 2026-10-02. This spec covers Phase 9 of `docs/implementation-plan.md`:

- **K1:** MCP connectors.
- **K2:** discovery for large tool sets.

`docs/design.md` §2, §4.1 and §5 remain the authority. This document fills in what they leave open.

## Goal

A dot can use tools from MCP servers, starting with GitHub. Each MCP tool goes through the same effect, policy, Guardian, approval and audit chain as a native tool. A profile with many tools stays usable through search instead of shipping every schema in the prompt.

## Decisions

- **Transport:** GitHub's hosted MCP endpoint over streamable HTTP, authenticated with a fine-grained personal access token behind the credential handle `cred:github`. Stdio transport is out of scope.
- **GitHub tool set:** read tools, plus the pull-request flow, each as `write`, so each needs approval under the default policy. The PR flow is `create_branch`, `create_or_update_file`, `push_files` and `create_pull_request`.
- **Loading:** each process loads and caches MCP tools once, in an `McpToolbox`. Agents are never built with a network listing per build. Schemas are not checked into the repo.
- **Discovery:** the schemas of deferred tools are hidden from the model. A `run_tool` call is rewritten into the real tool call before the safety chain, so every layer judges the real tool. `run_tool` is never a dispatcher that calls tools itself.
- **Search:** keyword scoring over each tool's name and description. Embeddings are not used.

## Components

### 1. `config/mcp.yaml` and `McpServer`

Each server gains `url` and `auth`. `auth` is a credential handle, never a secret. Tools map to effects:

```yaml
servers:
  github:
    url: https://api.githubcopilot.com/mcp/
    auth: cred:github
    tools:
      get_me: read
      get_file_contents: read
      search_repositories: read
      search_code: read
      list_issues: read
      get_issue: read
      list_pull_requests: read
      get_pull_request: read
      get_pull_request_files: read
      list_commits: read
      get_commit: read
      list_branches: read
      create_branch: write
      create_or_update_file: write
      push_files: write
      create_pull_request: write
```

`McpServer` in `src/dot/packs/schema.py` gains these two fields:

- `url: HttpUrl`
- `auth: str | None`, which must match `cred:[a-z][a-z0-9_-]*` when set.

The loader keeps its existing checks: an unknown server, a tool-name collision and an untagged tool each fail the load.

`cred:github` must be bound in the credential settings, following the pattern of the existing `cred:` handles. The token's name goes in `.env.example`, and its value never does.

### 2. `src/dot/tools/mcp.py`: `McpToolbox`

There is one toolbox per process. It is held on `GraphRuntime`, as `jobs` is, and with no toolbox there are no MCP tools.

- `tools(server: str) -> list[BaseTool]`:
  - **Listing:** lists the server's tools through `langchain-mcp-adapters` over streamable HTTP. A successful listing is cached for 10 minutes and a failed one for 60 seconds.
  - **Filtering:** keeps only the tools mapped in `mcp.yaml`. Every other tool is dropped with one warning per refresh, naming the tool.
  - **Sync wrapping:** wraps each kept tool as a synchronous `BaseTool` with the same name, description and argument schema. A call runs the MCP coroutine on the toolbox's own background event loop thread and waits for it, with a timeout of 30 seconds by default.
  - **Auth:** resolves the bearer token with `CredentialBroker.resolve(auth)` at listing time and again at call time. The token never enters agent state, prompts, tool arguments or logs. Error text passes through the redactor.
- **Results:** compact JSON. Text content is joined; large outputs go through the existing offload middleware. A failure returns `{"ok": false, "error": "<redacted message>"}`, and nothing is retried automatically.

### 3. Assembly

In `build_dot_agent`, after `native_registry(deps)`, the MCP tools for each server in `pack.tools.mcp` are registered with their configured effects from `runtime.mcp`. Subagents whose `tools` name MCP tools get them in the same way through `_subagent_spec`.

From that point on, MCP tools are ordinary tools. `for_profile`, `PolicyResolver`, `SurfaceGuard`, the Guardian, `interrupt_on` and audit all apply unchanged. One assembly builds agents for the API, worker, CLI and tests.

MCP output is tool output. It is wrapped and marked as untrusted data, as `fetch_url` output is, and never reaches the Guardian.

### 4. `src/dot/tools/discovery.py`: discovery

This applies when the tools offered to a profile, after `for_profile` and the job tools, number more than `DISCOVERY_THRESHOLD = 30`.

- **Split, decided in code:**
  - Native tools, the job tools and the deepagents filesystem tools stay visible: the core.
  - MCP tools and any further registered tools are deferred.
  - If the core alone exceeds the threshold, all non-core tools are still deferred, and the core stays whole.
- **Tools:** assembly adds three `read` tools.
  - `search_tools(query: str, limit: int = 8)` returns the top matches as `[{name, effect, summary}]`. The summary is the first sentence of the description, clipped to 160 characters.
  - `describe_tool(name: str)` returns `{name, effect, description, args_schema}`, or `{"ok": false}` for an unknown or ungranted name.
  - `run_tool(name: str, args: dict)` is never executed itself: the middleware always rewrites it. If one reaches the tool node, it returns an error.
- **`ToolDiscovery` middleware:**
  - **Before each model call** (`wrap_model_call`), it removes deferred tools from the request and adds one system line: "N more tools are available through search_tools."
  - **After each model call** (`after_model`), it rewrites every `run_tool` call whose `name` is granted into a call to that tool with `args`, keeping the call id. A `run_tool` naming a tool the profile doesn't grant is left as-is. `SurfaceGuard` then refuses it, just as it refuses any tool the model wasn't shown.
  - It is placed so its `after_model` runs before `SurfaceGuard`, policy, the Guardian, the approval review and audit. All of them see the real tool name and arguments.
  - Deferred tools stay registered in the tool node and in the `SurfaceGuard` allow-list, so a rewritten call can run. The allow-list contains exactly the granted tools.
- **Scoring:** case-insensitive token overlap between the query and each tool's name and description. A name match is weighted three times a description match. Ties are broken by name. Results are deterministic.
- **Prompt budget:** with 200 stub tools in one profile, the serialized tool schemas sent to the model stay under 4,000 tokens, counted as characters ÷ 4.

## Error handling

| Situation | Behaviour |
|---|---|
| Listing fails (timeout, 5xx, bad token) | That server's tools are not offered this turn. One warning per cache window, and the failure is cached for 60 s. The turn runs. |
| `cred:github` not configured | Same as a listing failure, logged as "credential is not configured". No crash at startup. |
| Call fails or times out | The tool returns `{"ok": false, "error": ...}`, redacted, with no automatic retry. |
| Tool removed from the server after listing | The call returns "tool not available", and the next refresh drops it. |
| `run_tool` with a tool not granted | Refused by `SurfaceGuard`, the same as any unshown tool. |
| `run_tool` arguments don't match the schema | The tool's validation error is returned as the tool result. |

## Testing

All tests are offline with the scripted model, except the live test.

- **Stub server:** an in-process FastMCP server from the `mcp` SDK, a dependency of `langchain-mcp-adapters`, on a local port over streamable HTTP. It exposes:
  - mapped read and write tools;
  - an unmapped tool;
  - a failing tool;
  - a tool that echoes the `Authorization` header it received.
- **Acceptance K1:** an unmapped MCP tool is never offered. A listing failure or a missing credential removes only that server's tools.
- **Chain:** a mapped `write` MCP tool produces an approval card through the real chain. After approval it runs and the stub receives the resolved bearer token. A `read` tool runs without approval. Audit rows name the MCP tool.
- **Acceptance K2:**
  - With 200 stub tools, the schemas sent to the model stay under 4,000 tokens, and the native tools remain directly visible.
  - A scripted model calls `search_tools`, finds the target tool in the top results, and calls `run_tool`.
  - Audit and the approval card show the real tool.
  - A `run_tool` call for an ungranted tool is refused.
- **Loader:** the new fields validate, a bad `auth` handle is rejected, and the existing collision, unknown-server and untagged checks still hold.
- **Live** (`-m live`, needs the GitHub token): list GitHub's tools, check that every mapped name exists on the server, and call `get_me`. The test is read-only.

## Out of scope

- Stdio transport, other MCP servers.
- Embeddings-based search.
- Automatic retries on MCP calls.
- A web UI for MCP configuration.
