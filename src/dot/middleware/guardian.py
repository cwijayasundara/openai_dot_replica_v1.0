"""Review consequential calls before approval, then recheck reviewer edits."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from dot.middleware.guard import ToolSurfacePolicy
from dot.packs.schema import Decision
from dot.safety.audit import AuditWriter
from dot.safety.guardian import Guardian, Verdict
from dot.safety.policy import PolicyResolver
from dot.tools.effects import Effect

# Set by the worker on inbound messages that carry no user objective of their
# own (job results): the Guardian reviews against this instead of the text.
GUARDIAN_INSTRUCTION_KEY = "dot_guardian_instruction"


def _objective(message: HumanMessage) -> str:
    stored = message.additional_kwargs.get(GUARDIAN_INSTRUCTION_KEY)
    return stored if isinstance(stored, str) else message.text


class GuardianState(AgentState[Any]):
    guardian_instruction: str
    guardian_reviews: dict[str, dict[str, Any]]
    audit_turn_id: str


class GuardianMiddleware(AgentMiddleware[GuardianState, Any, Any]):
    state_schema = GuardianState

    def __init__(
        self,
        guardian: Guardian,
        policy: PolicyResolver,
        surface: ToolSurfacePolicy,
        *,
        capture_instruction: bool,
        audit: AuditWriter | None = None,
        instruction: str | None = None,
    ) -> None:
        super().__init__()
        self.guardian, self.policy, self.surface = guardian, policy, surface
        self.capture_instruction = capture_instruction
        self.audit = audit
        # A background job reviews against its originating user instruction,
        # never against the instructions the supervisor wrote for it.
        self.instruction = instruction

    def before_agent(self, state: GuardianState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        if not self.capture_instruction:
            return None
        if self.instruction is not None:
            objective = self.instruction
        else:
            # A turn's queued user messages form the trailing human-message run.
            # Never derive the objective from a subagent's synthetic task description.
            incoming = []
            for message in reversed(state["messages"]):
                if not isinstance(message, HumanMessage):
                    break
                incoming.append(_objective(message))
            objective = "\n\n".join(part for part in reversed(incoming) if part)
        return {
            "guardian_instruction": objective,
            "guardian_reviews": {},
            "audit_turn_id": str(uuid4()),
        }

    def _proposal(self, state: Any, call: ToolCall) -> None:
        if self.audit is not None:
            detail = {"phase": "proposed", "tool_call_id": call.get("id"), "args": call["args"]}
            self.audit.record("tool_call", call["name"], state=state, detail=detail)
            self.audit.record(
                "policy",
                call["name"],
                state=state,
                decision=self.policy.decision(call["name"]).value,
                detail={**detail, "surface_permitted": self.surface.permits_call(call["name"], call["args"])},
            )

    def _verdict_event(self, state: Any, call: ToolCall, verdict: Verdict) -> None:
        if self.audit is not None:
            self.audit.record(
                "guardian",
                call["name"],
                state=state,
                decision="allow" if verdict.permitted else "block",
                verdict=verdict.model_dump(),
                detail={"tool_call_id": call.get("id"), "args": call["args"]},
            )

    @staticmethod
    def _key(state: Any, name: str, args: dict[str, Any]) -> str:
        value = json.dumps([state.get("guardian_instruction", ""), name, args], sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()

    def _effect(self, name: str, args: dict[str, Any]) -> Effect | None:
        if not self.surface.permits_call(name, args) or self.policy.decision(name) is Decision.block:
            return None
        effect = dict(self.policy.effects).get(name)
        return effect if effect is not Effect.read else None

    def _calls(self, state: GuardianState) -> list[ToolCall]:
        last = state["messages"][-1] if state["messages"] else None
        return list(last.tool_calls) if isinstance(last, AIMessage) else []

    def after_model(self, state: GuardianState, runtime: Runtime[Any]) -> dict[str, Any]:
        reviews = {}
        for call in self._calls(state):
            self._proposal(state, call)
            name, args = call["name"], call["args"]
            if (effect := self._effect(name, args)) is not None:
                verdict = self.guardian.review(state.get("guardian_instruction", ""), name, args, effect, self.policy)
                self._verdict_event(state, call, verdict)
                reviews[self._key(state, name, args)] = verdict.model_dump()
        return {"guardian_reviews": reviews}

    async def aafter_model(self, state: GuardianState, runtime: Runtime[Any]) -> dict[str, Any]:
        reviews = {}
        for call in self._calls(state):
            self._proposal(state, call)
            name, args = call["name"], call["args"]
            if (effect := self._effect(name, args)) is not None:
                verdict = await self.guardian.areview(
                    state.get("guardian_instruction", ""),
                    name,
                    args,
                    effect,
                    self.policy,
                )
                self._verdict_event(state, call, verdict)
                reviews[self._key(state, name, args)] = verdict.model_dump()
        return {"guardian_reviews": reviews}

    def _cached(self, request: ToolCallRequest) -> Verdict | None:
        name, args = request.tool_call["name"], request.tool_call["args"]
        value = request.state.get("guardian_reviews", {}).get(self._key(request.state, name, args))
        return Verdict.model_validate(value) if value is not None else None

    def needs_approval(self, request: ToolCallRequest) -> bool:
        # Skipping human review for a refusal cannot grant execution: the tool
        # wrapper below returns that refusal instead of invoking the handler.
        cached = self._cached(request)
        return cached is None or cached.permitted

    @staticmethod
    def _denial(request: ToolCallRequest, verdict: Verdict) -> ToolMessage:
        return ToolMessage(
            content=f"Guardian refused: {verdict.reason}",
            status="error",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call.get("id") or "",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        name, args = request.tool_call["name"], request.tool_call["args"]
        if (effect := self._effect(name, args)) is None:
            return handler(request)
        cached = self._cached(request)
        verdict = cached or self.guardian.review(
            request.state.get("guardian_instruction", ""),
            name,
            args,
            effect,
            self.policy,
        )
        if cached is None:
            self._verdict_event(request.state, request.tool_call, verdict)
        return handler(request) if verdict.permitted else self._denial(request, verdict)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        name, args = request.tool_call["name"], request.tool_call["args"]
        if (effect := self._effect(name, args)) is None:
            return await handler(request)
        cached = self._cached(request)
        verdict = cached or await self.guardian.areview(
            request.state.get("guardian_instruction", ""),
            name,
            args,
            effect,
            self.policy,
        )
        if cached is None:
            self._verdict_event(request.state, request.tool_call, verdict)
        return await handler(request) if verdict.permitted else self._denial(request, verdict)
