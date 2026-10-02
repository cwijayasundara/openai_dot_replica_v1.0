"""L3: the replay gate. A proposed memory edit is kept only if past episodes say it does no harm.

Each sampled episode is replayed twice from the state just before its
proposal: once with the current memory (baseline) and once with the edit
applied (candidate). Replays run on a scratch runtime, so they never touch the
dot's thread, store or tables, and ``ReplayStop`` ends each one before any tool
with an effect runs. The edit is kept when the candidate matches the human at
least as often as the baseline and at least one cited episode goes from
mismatch to match. An edit with nothing to replay is held for a human.
"""

from __future__ import annotations

import hashlib
import logging
import random
import tempfile
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from dot.assembly import GraphRuntime, build_dot_agent, dot_artifacts
from dot.config import Settings
from dot.memory.compare import Expectation, expectation
from dot.memory.episodes import ReplayPoint, Unreplayable, replay_point
from dot.memory.reflection import MemoryEdit, MemoryFiles
from dot.memory.versions import record_verdict, recorded_edit
from dot.middleware.replay import ReplayStop
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Dot, Episode, Json, MemoryVersion, Repositories
from dot.sandbox.base import RunSandbox
from dot.tools.native.deps import ToolDeps

log = logging.getLogger(__name__)

# The random sample is drawn from at most this many of the dot's oldest episodes.
_SAMPLE_POOL = 1_000


@dataclass(frozen=True)
class ReplayResult:
    episode_id: int
    arm: str
    match: bool
    stop: str
    calls: int

    def view(self) -> Json:
        return {
            "episode": self.episode_id,
            "arm": self.arm,
            "match": self.match,
            "stop": self.stop,
            "calls": self.calls,
        }


@dataclass(frozen=True)
class GateResult:
    status: str
    reason: str
    edit: MemoryEdit | None = None
    results: tuple[ReplayResult, ...] = ()
    unreplayable: tuple[Unreplayable, ...] = ()


def gate_proposed(
    repos: Repositories, runtime: GraphRuntime, dot: Dot, settings: Settings, model: BaseChatModel
) -> list[MemoryVersion]:
    """Judge every ``proposed`` edit of this dot, oldest first, and apply the accepted ones.

    Runs under the dot's lock. Each edit is judged against memory as the
    previous one left it.
    """
    pending = sorted(
        (v for v in repos.list_memory_versions(dot.dot_id) if v.status == "proposed"), key=lambda v: (v.at, v.id)
    )
    if not pending:
        return []
    files = MemoryFiles(runtime.store, dot.dot_id)
    judged: list[MemoryVersion] = []
    with tempfile.TemporaryDirectory(prefix="replay-") as scratch_root:
        gate = ReplayGate(repos, runtime, dot, settings, model, scratch_root)
        for version in pending:
            try:
                verdict = gate.judge(version)
            except Exception:
                # A failed replay (a model error, say) is no verdict: the row stays proposed for the next run.
                log.exception("replay gate failed for %s memory version %s", dot.dot_id, version.id)
                continue
            judged.append(record_verdict(repos, files, version, verdict))
    return judged


class ReplayGate:
    def __init__(
        self,
        repos: Repositories,
        runtime: GraphRuntime,
        dot: Dot,
        settings: Settings,
        model: BaseChatModel,
        scratch_root: str,
    ) -> None:
        self.repos = repos
        self.runtime = runtime
        self.dot = dot
        self.settings = settings
        self.model = model
        # Replay agents offload large tool results under scratch_root, not the dot's object folder.
        self.replay_settings = settings.model_copy(update={"object_root": scratch_root})
        self.profiles = set(load_pack(REPO_ROOT / "packs" / dot.pack_name).pack.profiles)
        # Reads the dot's checkpoints; building it does not invoke it.
        self.reader = build_dot_agent(dot, "chat", settings=settings, runtime=runtime, model=model)

    def judge(self, version: MemoryVersion) -> GateResult:
        files = MemoryFiles(self.runtime.store, self.dot.dot_id).listing()
        edit = recorded_edit(version, files, self.runtime.redactor)
        if isinstance(edit, str):
            return GateResult("rejected", f"stale_base: {edit}")
        cited, skipped = self._points([self.repos.get_episode(n) for n in version.episodes], self.settings.replay_cited)
        if not cited:
            return GateResult("needs_review", "no cited episode can be replayed", edit, (), tuple(skipped))
        cited_ids = {episode.id for episode, _ in cited}
        pool = [
            e
            for e in self.repos.list_episodes(self.dot.dot_id, limit=_SAMPLE_POOL)
            if e.id not in set(version.episodes)
        ]
        random.Random(_seed(version)).shuffle(pool)
        others, more = self._points(pool, self.settings.replay_random)
        candidate = {**files, edit.path: edit.after}
        results: list[ReplayResult] = []
        improved = False
        for episode, point in [*cited, *others]:
            expected = expectation(episode.human_action, point.calls, episode.outcome)
            base = self._replay(point, expected, files, "baseline")
            cand = self._replay(point, expected, candidate, "candidate")
            results += [base, cand]
            improved = improved or (episode.id in cited_ids and cand.match and not base.match)
        baseline = sum(r.match for r in results if r.arm == "baseline")
        after = sum(r.match for r in results if r.arm == "candidate")
        unreplayable = (*skipped, *more)
        if after < baseline:
            return GateResult(
                "rejected", f"match rate fell from {baseline} to {after}", edit, tuple(results), unreplayable
            )
        if not improved:
            return GateResult("rejected", "no cited episode improved", edit, tuple(results), unreplayable)
        return GateResult("accepted", f"matches {baseline} -> {after}", edit, tuple(results), unreplayable)

    def _points(
        self, episodes: list[Episode], limit: int
    ) -> tuple[list[tuple[Episode, ReplayPoint]], list[Unreplayable]]:
        found: list[tuple[Episode, ReplayPoint]] = []
        skipped: list[Unreplayable] = []
        for episode in episodes:
            if len(found) == limit:
                break
            point = replay_point(self.repos, self.reader, self.dot, episode)
            if isinstance(point, ReplayPoint) and point.profile not in self.profiles:
                point = Unreplayable(episode.id, f"profile {point.profile!r} is no longer in the pack")
            if isinstance(point, ReplayPoint):
                found.append((episode, point))
            else:
                skipped.append(point)
        return found, skipped

    def _replay(self, point: ReplayPoint, expected: Expectation, files: dict[str, str], arm: str) -> ReplayResult:
        scratch = GraphRuntime(MemorySaver(), InMemoryStore(), redactor=self.runtime.redactor)
        memory = MemoryFiles(scratch.store, self.dot.dot_id)
        for path, text in files.items():
            memory.write(path, text)
        stop = ReplayStop(frozenset(call["name"] for call in point.calls), self.settings.replay_max_model_calls)
        agent = build_dot_agent(
            self.dot,
            point.profile,
            settings=self.replay_settings,
            runtime=scratch,
            # No transports: a read that needs the network fails instead of reaching it.
            deps=ToolDeps(dot_artifacts(self.settings, self.dot.dot_id)),
            model=self.model,
            sandbox_factory=_no_sandbox,
            replay=stop,
        )
        agent.invoke({"messages": list(point.messages)}, {"configurable": {"thread_id": f"replay-{uuid4().hex}"}})
        message = stop.message
        proposed = [{"name": c["name"], "args": c["args"]} for c in (message.tool_calls if message else [])]
        reason = stop.stop or "budget"
        return ReplayResult(point.episode_id, arm, expected.met_by(proposed, reason), reason, stop.calls)


def _seed(version: MemoryVersion) -> int:
    """The same edit always draws the same sample, so a verdict can be reproduced."""
    return int(hashlib.sha256(f"{version.detail.get('path')}\n{version.diff}".encode()).hexdigest()[:16], 16)


def _no_sandbox(dot_id: str) -> RunSandbox:
    raise RuntimeError(f"replay for {dot_id} has no sandbox")
