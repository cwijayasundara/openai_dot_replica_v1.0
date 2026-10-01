"""One assembly for the API, the worker, the CLI and the tests.

LangGraph's checkpointer and store are Postgres when ``DATABASE_URL`` is set,
and in memory otherwise. ``build_dot_agent`` is the only place a dot's model,
tools, middleware and sandbox are wired together.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import SubAgent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from .config import Role, Settings, get_settings
from .jobs.store import JobStore
from .jobs.tools import JOB_EFFECTS, build_job_tools
from .middleware.audit import AuditMiddleware
from .middleware.guard import FS_TOOLS, SurfaceGuard, ToolSurfacePolicy
from .middleware.guardian import GuardianMiddleware
from .middleware.job_control import JobControlMiddleware
from .middleware.offload import OffloadMiddleware
from .middleware.policy import PolicyMiddleware
from .middleware.redaction import RedactionMiddleware, Redactor
from .models import chat_model
from .packs.loader import REPO_ROOT, load_pack, memories_namespace, wiki_namespace
from .packs.schema import LoadedPack, Profile, SubagentSpec
from .persistence.db import AuditEvent, Dot, Job, Repositories
from .safety.audit import AuditWriter
from .safety.credentials import RedactingBroker, build_credential_broker
from .safety.guardian import Guardian
from .safety.policy import FILESYSTEM_EFFECTS, PolicyResolver
from .sandbox.base import LazySandbox, RunSandbox
from .sandbox.docker_backend import docker_sandbox_for
from .sandbox.openshell_backend import OpenShellSandbox, openshell_sandbox_for, policy_for_profile
from .tools.artifacts import ArtifactStore
from .tools.effects import Effect
from .tools.native import native_registry
from .tools.native.deps import ToolDeps
from .tools.registry import ToolRegistry

# The supervisor reaches the sandbox through the coder subagent, not these tools.
_SHELL_TOOLS = FS_TOOLS


@dataclass
class GraphRuntime:
    checkpointer: BaseCheckpointSaver[Any]
    store: BaseStore
    # Connections the Postgres saver and store borrow. Held so they stay open.
    owned: list[Any] = field(default_factory=list)
    sandboxes: dict[tuple[str, str], RunSandbox] = field(default_factory=dict)
    audit_repositories: Repositories | None = None
    redactor: Redactor = field(default_factory=Redactor)
    # Background jobs. Without a store the supervisor is not offered job tools.
    jobs: JobStore | None = None

    def close(self) -> None:
        for sandbox in self.sandboxes.values():
            sandbox.close()
        self.sandboxes.clear()
        for resource in self.owned:
            resource.close()
        self.owned.clear()

    def close_sandbox(self, key: str, *, keep_work: bool = False) -> None:
        """Retire ``key``'s sandbox. With ``keep_work``, /work is snapshotted for the next start."""
        for sandbox_key in [k for k in self.sandboxes if k[0] == key]:
            sandbox = self.sandboxes.pop(sandbox_key)
            suspend = getattr(sandbox, "suspend", None)
            if keep_work and callable(suspend):
                suspend()
            else:
                sandbox.close()

    def sandbox(self, dot_id: str, profile: str, settings: Settings, pack: LoadedPack | None = None) -> RunSandbox:
        policy = policy_for_profile(profile) if settings.sandbox_backend == "openshell" else "docker"
        key = (dot_id, policy)
        if key not in self.sandboxes:
            # A new policy needs a new sandbox; carry work over and retire the old one.
            for old_key, old in list(self.sandboxes.items()):
                if old_key[0] == dot_id:
                    if isinstance(old, OpenShellSandbox):
                        old.suspend()
                    else:
                        old.close()
                    del self.sandboxes[old_key]
            if pack is not None:
                skills = Path(settings.object_root) / dot_id / "sandbox" / "skills"
                for source in pack.skill_files:
                    target = skills / source.relative_to(pack.root / pack.pack.skills)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            if settings.sandbox_backend == "docker":
                sandbox: RunSandbox = docker_sandbox_for(dot_id, settings)
            else:
                if self.audit_repositories is None:
                    raise RuntimeError("OpenShell requires the runtime's audit_repositories to be configured")

                def record(event: AuditEvent) -> AuditEvent:
                    repos = self.audit_repositories
                    if repos is None:
                        raise RuntimeError("audit repositories are not configured")
                    return repos.append_audit(
                        replace(
                            event,
                            verdict=self.redactor.content(event.verdict),
                            detail=self.redactor.content(event.detail),
                        )
                    )

                sandbox = openshell_sandbox_for(
                    dot_id,
                    settings,
                    profile=profile,
                    audit=record,
                )
            self.sandboxes[key] = sandbox
        sandbox = self.sandboxes[key]
        sandbox.start()
        return sandbox


def build_graph_runtime(settings: Settings | None = None) -> GraphRuntime:
    settings = settings or get_settings()
    if not settings.database_url:
        return GraphRuntime(MemorySaver(), InMemoryStore(), redactor=Redactor(_secret_values(settings)))

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore
    from psycopg import Connection
    from psycopg.rows import dict_row

    def connect() -> Connection[dict[str, Any]]:
        return Connection.connect(
            settings.database_url,  # type: ignore[arg-type]
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )

    saver_conn = connect()
    store_conn = connect()
    checkpointer = PostgresSaver(saver_conn)
    checkpointer.setup()
    store = PostgresStore(store_conn)
    store.setup()
    return GraphRuntime(checkpointer, store, [saver_conn, store_conn], redactor=Redactor(_secret_values(settings)))


def build_dot_agent(
    dot: Dot,
    profile: str,
    *,
    settings: Settings | None = None,
    runtime: GraphRuntime | None = None,
    deps: ToolDeps | None = None,
    model: BaseChatModel | None = None,
    guardian: Guardian | None = None,
    sandbox_factory: Callable[[str], RunSandbox] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build the dot's supervisor for one capability profile.

    Policy, Guardian, audit and approval interrupts are enforced here.
    The sandbox factory is not called here.
    """
    settings = settings or get_settings()
    runtime = runtime or build_graph_runtime(settings)
    deps = _tool_deps(dot, settings, runtime, deps)
    loaded = load_pack(REPO_ROOT / "packs" / dot.pack_name)
    selected = loaded.pack.profiles.get(profile)
    if selected is None:
        known = ", ".join(sorted(loaded.pack.profiles))
        raise ValueError(f"pack {loaded.pack.name!r} has no profile {profile!r} (known: {known})")

    supervisor = _role_model("supervisor", settings, model)
    # deepagents 0.7 adds a general-purpose subagent that inherits execute and skips this guard.
    _disable_general_purpose(supervisor)
    registry = native_registry(deps)
    # task and the job tools only delegate; the subagent gates each consequential tool separately.
    policy = PolicyResolver(
        loaded.policy, registry.effects() | FILESYSTEM_EFFECTS | {"task": Effect.read} | JOB_EFFECTS
    )
    guardian = guardian or Guardian(lambda: _role_model("fast", settings, model), redactor=runtime.redactor)
    audit = AuditWriter(
        runtime.audit_repositories, dot.dot_id, dot.thread_id, profile, "supervisor", runtime.redactor, policy
    )
    granted = _granted_subagents(loaded, selected)
    tools = registry.for_profile(selected)
    if granted and runtime.jobs is not None:
        tools += build_job_tools(runtime.jobs, deps.artifacts, dot.dot_id, profile, granted)
    surface = ToolSurfacePolicy(
        allowed=frozenset(tool.name for tool in tools) | ({"task"} if granted else frozenset()),
        subagents=frozenset(spec.name for spec in granted),
    )
    factory = sandbox_factory or (lambda dot_id: runtime.sandbox(dot_id, profile, settings, loaded))
    sandbox_backend = LazySandbox(lambda: factory(dot.dot_id), dot.dot_id)
    backend = CompositeBackend(
        default=sandbox_backend,
        routes={
            "/memories/": StoreBackend(store=runtime.store, namespace=lambda _rt: memories_namespace(dot.dot_id)),
            "/wiki/": StoreBackend(store=runtime.store, namespace=lambda _rt: wiki_namespace(dot.dot_id)),
        },
    )
    subagents = [
        _subagent_spec(
            spec,
            registry,
            settings,
            dot.dot_id,
            _role_model(spec.model, settings, model),
            sandbox_backend,
            policy,
            guardian,
            runtime,
            dot.thread_id,
            profile,
        )
        for spec in granted
    ]
    return create_deep_agent(
        model=supervisor,
        system_prompt=loaded.persona_text,
        tools=tools,
        subagents=subagents,
        memory=["/memories/AGENTS.md"],
        skills=["/memories/skills/"],
        backend=backend,
        middleware=_chain(
            surface, settings, dot.dot_id, policy, guardian, audit, runtime.redactor, capture_instruction=True
        ),
        checkpointer=runtime.checkpointer,
        store=runtime.store,
    )


def dot_artifacts(settings: Settings, dot_id: str) -> ArtifactStore:
    """The dot's artifacts. Tools and job results share it, so ``check_job`` can read results."""
    return ArtifactStore(Path(settings.object_root) / dot_id)


def _tool_deps(dot: Dot, settings: Settings, runtime: GraphRuntime, deps: ToolDeps | None) -> ToolDeps:
    deps = deps or ToolDeps(dot_artifacts(settings, dot.dot_id))
    for secret in _secret_values(settings):
        runtime.redactor.add(secret)
    broker = (
        RedactingBroker(deps.credentials, runtime.redactor)
        if deps.credentials is not None
        else build_credential_broker(settings, runtime.redactor)
    )
    return replace(deps, credentials=broker)


def job_sandbox_key(job: Job) -> str:
    """A job's own sandbox, so it never retires or shares the supervisor's."""
    return f"{job.dot_id}-{job.job_id}"


def build_job_agent(
    dot: Dot,
    job: Job,
    current: Callable[[], Job],
    *,
    settings: Settings | None = None,
    runtime: GraphRuntime | None = None,
    deps: ToolDeps | None = None,
    model: BaseChatModel | None = None,
    guardian: Guardian | None = None,
    sandbox_factory: Callable[[str], RunSandbox] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Build a background job as its own Deep Agent on ``job.thread_id``.

    The subagent must be granted to the profile the job was started under.
    It gets the subagent's model, tools and sandbox, the same safety chain as
    the supervisor, and ``current`` for cancellation and updates.
    """
    settings = settings or get_settings()
    runtime = runtime or build_graph_runtime(settings)
    deps = _tool_deps(dot, settings, runtime, deps)
    loaded = load_pack(REPO_ROOT / "packs" / dot.pack_name)
    selected = loaded.pack.profiles.get(job.profile)
    if selected is None:
        raise ValueError(f"pack {loaded.pack.name!r} has no profile {job.profile!r}")
    spec = next((s for s in _granted_subagents(loaded, selected) if s.name == job.subagent), None)
    if spec is None:
        raise ValueError(f"subagent {job.subagent!r} is not granted to profile {job.profile!r}")

    job_model = _role_model(spec.model, settings, model)
    _disable_general_purpose(job_model)
    registry = native_registry(deps)
    policy = PolicyResolver(loaded.policy, registry.effects() | FILESYSTEM_EFFECTS)
    guardian = guardian or Guardian(lambda: _role_model("fast", settings, model), redactor=runtime.redactor)
    audit = AuditWriter(
        runtime.audit_repositories, dot.dot_id, job.thread_id, job.profile, spec.name, runtime.redactor, policy
    )
    tools = registry.for_profile(Profile(tools=spec.tools))
    allowed = {tool.name for tool in tools} | (_SHELL_TOOLS if spec.sandbox else set())
    surface = ToolSurfacePolicy(allowed=frozenset(allowed), subagents=frozenset())
    backend = None
    if spec.sandbox:
        key = job_sandbox_key(job)
        factory = sandbox_factory or (lambda k: runtime.sandbox(k, job.profile, settings, loaded))
        backend = LazySandbox(lambda: factory(key), key)
    instruction = job.origin.get("instruction")
    middleware = _chain(
        surface,
        settings,
        dot.dot_id,
        policy,
        guardian,
        audit,
        runtime.redactor,
        capture_instruction=True,
        # No recorded user objective means nothing consequential is in scope.
        instruction=instruction if isinstance(instruction, str) else "",
        fail_hard=True,
    )
    return create_deep_agent(
        model=job_model,
        system_prompt=spec.system_prompt or spec.description,
        tools=tools,
        subagents=[],
        backend=backend,
        # First in the dot's chain, so a cancelled job stops before its other hooks run.
        middleware=[JobControlMiddleware(current), *middleware],
        checkpointer=runtime.checkpointer,
        store=runtime.store,
    )


def _granted_subagents(loaded: LoadedPack, profile: Profile) -> list[SubagentSpec]:
    if profile.subagents is None:
        return []
    if profile.subagents == "*":
        return list(loaded.pack.subagents)
    by_name = {spec.name: spec for spec in loaded.pack.subagents}
    return [by_name[name] for name in profile.subagents]


def _subagent_spec(
    spec: SubagentSpec,
    registry: ToolRegistry,
    settings: Settings,
    dot_id: str,
    model: BaseChatModel,
    sandbox_backend: LazySandbox,
    policy: PolicyResolver,
    guardian: Guardian,
    runtime: GraphRuntime,
    thread_id: str,
    profile: str,
) -> SubAgent:
    if spec.name == "coder" and (spec.model != "heavy" or not spec.sandbox or set(spec.tools) - FS_TOOLS):
        raise ValueError("coder must use the heavy model and sandbox, with execute and file tools only")
    tools = registry.for_profile(Profile(tools=spec.tools))
    allowed = {tool.name for tool in tools}
    if spec.sandbox:
        allowed |= _SHELL_TOOLS
    surface = ToolSurfacePolicy(allowed=frozenset(allowed), subagents=frozenset())
    audit = AuditWriter(runtime.audit_repositories, dot_id, thread_id, profile, spec.name, runtime.redactor, policy)
    middleware = _chain(surface, settings, dot_id, policy, guardian, audit, runtime.redactor, capture_instruction=False)
    if spec.sandbox:
        # Override the inherited CompositeBackend so file tools cannot edit memory/wiki.
        middleware.insert(0, FilesystemMiddleware(backend=sandbox_backend))
    built: SubAgent = {
        "name": spec.name,
        "description": spec.description,
        "model": model,
        "system_prompt": spec.system_prompt,
        "tools": tools,
        "middleware": middleware,
        # This chain owns HITL so reviewer edits are checked by the guard/policy.
        "interrupt_on": {},
    }
    return built


def _chain(
    surface: ToolSurfacePolicy,
    settings: Settings,
    dot_id: str,
    policy: PolicyResolver,
    guardian: Guardian,
    audit: AuditWriter,
    redactor: Redactor,
    *,
    capture_instruction: bool,
    instruction: str | None = None,
    fail_hard: bool = False,
) -> list[AgentMiddleware[Any, Any, Any]]:
    reviewer = GuardianMiddleware(
        guardian, policy, surface, capture_instruction=capture_instruction, audit=audit, instruction=instruction
    )
    approval_map = policy.approval_map(surface.allowed or ())
    for config in approval_map.values():
        if isinstance(config, dict):
            config["when"] = reviewer.needs_approval
    review: list[AgentMiddleware[Any, Any, Any]] = (
        [HumanInTheLoopMiddleware(interrupt_on=approval_map)] if approval_map else []
    )
    return [
        # HITL substitutes edited calls in its wrapper. Guards must see those
        # substitutions, so review wraps them at execution time.
        *review,
        AuditMiddleware(audit),
        SurfaceGuard(surface, audit),
        OffloadMiddleware(Path(settings.object_root) / dot_id),
        RedactionMiddleware(redactor=redactor),
        PolicyMiddleware(policy, audit),
        reviewer,
        # A job raises instead, so a model failure is never reported as its result.
        ModelRetryMiddleware(
            max_retries=settings.model_max_retries,
            initial_delay=0.0,
            jitter=False,
            on_failure="error" if fail_hard else "continue",
        ),
        ModelCallLimitMiddleware(run_limit=settings.max_model_calls, exit_behavior="error" if fail_hard else "end"),
    ]


def settings_redactor(settings: Settings) -> Redactor:
    """A redactor for this deployment's configured secrets, for processes without a graph runtime."""
    return Redactor(_secret_values(settings))


def _secret_values(settings: Settings) -> list[str]:
    values = (
        settings.fireworks_api_key,
        settings.slack_bot_token,
        settings.slack_app_token,
        settings.slack_signing_secret,
        settings.smtp_credential,
    )
    return [value for value in values if value]


def _disable_general_purpose(model: BaseChatModel) -> None:
    """Register a harness profile that turns the auto-added subagent off for this model."""
    provider = _provider_name(model)
    if provider is None:
        return
    profile = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
    register_harness_profile(provider, profile)
    identifier = _model_identifier(model)
    if identifier and ":" in identifier:
        register_harness_profile(f"{provider}:{identifier}", profile)


def _provider_name(model: BaseChatModel) -> str | None:
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError):
        return None
    if not isinstance(params, Mapping):
        return None
    provider = params.get("ls_provider")
    if isinstance(provider, str) and provider:
        return provider
    return None


def _model_identifier(model: BaseChatModel) -> str | None:
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _role_model(role: Role, settings: Settings, override: BaseChatModel | None) -> BaseChatModel:
    if override is not None:
        return override
    return chat_model(role, settings)
