# Deterministic policy enforcement (G1)

`safety/policy.py` resolves rules for both pack validation and compiled agents.
For a tagged tool, the first matching `tools` rule in YAML declaration order
wins. Rules use case-sensitive shell globs (`delete_*`, for example). If no rule
matches, the tool's effect default applies. Unknown and untagged tools are always
blocked, even when a name rule would otherwise grant them.

Assembly snapshots the pack policy and registry effects so subsequent mutations
cannot change an already compiled agent's decisions. The registry supplies
native effects; the built-in file tools are explicitly tagged as reads or writes.
`task` is a read/control operation; each delegated agent enforces the policy on
its own tools. A task cannot grant its subagent new capabilities.

Policy middleware returns an error `ToolMessage` for `block` and never calls the
underlying tool. It supports synchronous and asynchronous invocation. Surface
guards still restrict which tools each agent can see or call.

For `approve`, assembly builds an `interrupt_on` map using only concrete tool
names offered to that agent and installs LangChain's HITL middleware. Review
permits approve, edit, or reject. Parent and subagent maps are separate, so a
hidden tool cannot create an approval interrupt solely because another agent
can use it. The default pack pauses sends, publishing, sandbox file writes,
and execution; reads and drafts continue normally.

The installed HITL middleware substitutes reviewer edits during tool execution.
Its wrapper therefore sits outside the surface/policy wrappers, which check
the final edited call. Review cannot redirect an allowed call into a blocked
tool and bypass enforcement. Read-only/denied sandbox paths remain enforced by
the backend after any approved file operation.

G1 implements graph-level interrupts. Persisted approval cards, approver
authorization, and the surface endpoint for resuming a run are G3; append-only
tool/policy audit logging is G4. There is no production auto-approval path.

Verification covers all effect defaults, exact/glob rules, declaration order,
unknown tools, immutable snapshots, concrete approval maps, sync/async blocks,
external approve/edit/reject, edits redirected to blocked tools, and coder
interrupts before lazy sandbox startup. The Docker CSV integration resumes
fixture reviews and verifies the computed output. No live models are needed.

Workspace verification: 77 offline tests and all three Docker tests passed.
Ruff lint/format and mypy passed. Tests also confirm that reviewer edits cannot
redirect a supervisor call into its hidden `execute` tool.

```sh
uv run pytest -q
uv run pytest -q -m 'docker and not live'
```
