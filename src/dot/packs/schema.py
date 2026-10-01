"""Pydantic models for pack.yaml and policy.yaml. See design sections 4.1 and 5.2."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dot.tools.effects import Effect


class Decision(StrEnum):
    allow = "allow"
    approve = "approve"
    block = "block"


class EffectDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: Decision
    draft: Decision
    write: Decision
    external: Decision
    financial: Decision
    credential: Decision

    def for_effect(self, effect: Effect) -> Decision:
        return Decision(getattr(self, effect.value))


class Policy(BaseModel):
    """Explicit tool rules win over effect defaults. Tool keys may be globs."""

    model_config = ConfigDict(extra="forbid")

    defaults: EffectDefaults
    tools: dict[str, Decision] = Field(default_factory=dict)
    approvers: list[str] = Field(default_factory=list)


class ModelRoles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supervisor: str
    heavy: str
    fast: str


class ToolSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native: list[str]
    mcp: list[str] = Field(default_factory=list)


class SubagentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model: Literal["supervisor", "heavy", "fast"]
    tools: list[str]
    description: str
    system_prompt: str = ""
    sandbox: bool = False


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: Literal["*"] | list[str] | None = None
    effects: list[Effect] | None = None
    subagents: Literal["*"] | list[str] | None = None

    @model_validator(mode="after")
    def _has_capability(self) -> Profile:
        if self.tools is None and not self.effects:
            raise ValueError("profile needs tools or effects")
        return self


class Schedule(BaseModel):
    """A scheduled entry point. See design section 7.

    A ``sweep`` runs on its own thread, may only read, and records findings.
    A ``digest`` runs on the dot's thread, summarises open findings and posts
    to the dot's default channel. Budgets left unset take the deployment's.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    cron: str
    profile: str
    prompt: str
    kind: Literal["sweep", "digest"] = "sweep"
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    persona: str
    models: ModelRoles
    skills: str
    wiki: str
    tools: ToolSelection
    subagents: list[SubagentSpec] = Field(default_factory=list)
    profiles: dict[str, Profile]
    policy: str
    schedules: list[Schedule] = Field(default_factory=list)

    def schedule(self, name: str) -> Schedule:
        for schedule in self.schedules:
            if schedule.name == name:
                return schedule
        raise KeyError(name)


class McpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: dict[str, Effect | None] = Field(default_factory=dict)


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: dict[str, McpServer] = Field(default_factory=dict)


class LoadedPack(BaseModel):
    """A pack that passed validation, plus the files it will seed."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    pack: Pack
    policy: Policy
    persona_text: str
    skill_files: tuple[Path, ...]
    wiki_files: tuple[Path, ...]
