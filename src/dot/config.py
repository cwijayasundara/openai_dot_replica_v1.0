"""Runtime settings. Every variable is read with the ``DOT_`` prefix."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelProvider = Literal["fireworks", "openai_compatible"]
SandboxBackend = Literal["docker", "openshell"]
Role = Literal["supervisor", "heavy", "fast"]

SUPERVISOR_MODEL = "accounts/fireworks/models/glm-5p3"
HEAVY_MODEL = "accounts/fireworks/models/kimi-k3"
FAST_MODEL = "accounts/fireworks/models/glm-5p3-flash"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOT_", env_file=".env", extra="ignore")

    model_provider: ModelProvider = "fireworks"
    fireworks_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str | None = None
    supervisor_model: str = SUPERVISOR_MODEL
    heavy_model: str = HEAVY_MODEL
    fast_model: str = FAST_MODEL
    model_timeout_s: float = 120.0
    model_max_retries: int = 2

    database_url: str | None = None
    object_root: str = "var/objects"

    sandbox_backend: SandboxBackend = "docker"
    sandbox_image: str = "dot-sandbox"
    openshell_gateway: str | None = None
    sandbox_idle_s: int = 600
    max_model_calls: int = 40
    # Threads per worker process that run background jobs beside the inbox loop.
    job_workers: int = Field(default=2, ge=0)

    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_app_token: str | None = Field(default=None, repr=False)
    slack_signing_secret: str | None = Field(default=None, repr=False)
    smtp_credential: str | None = Field(default=None, repr=False)
    slack_mode: Literal["socket", "http"] = "socket"
    # Approvers added by this deployment, per pack: {"research-analyst": ["U123ABC"]}.
    pack_approvers: dict[str, list[str]] = Field(default_factory=dict)
    credential_backend: Literal["env", "secret_manager"] = "env"
    credential_bindings: dict[str, str] = Field(
        default_factory=lambda: {
            "cred:smtp": "DOT_SMTP_CREDENTIAL",
            "cred:slack-bot": "DOT_SLACK_BOT_TOKEN",
        }
    )

    @model_validator(mode="after")
    def _required_pairs(self) -> Settings:
        if self.model_provider == "openai_compatible" and not self.openai_base_url:
            raise ValueError("DOT_OPENAI_BASE_URL is required for openai_compatible")
        if self.sandbox_backend == "openshell" and not self.openshell_gateway:
            raise ValueError("DOT_OPENSHELL_GATEWAY is required for the openshell sandbox")
        return self

    def model_name(self, role: Role) -> str:
        if role == "supervisor":
            return self.supervisor_model
        if role == "heavy":
            return self.heavy_model
        if role == "fast":
            return self.fast_model
        raise ValueError(f"unknown model role {role!r}")


def get_settings() -> Settings:
    return Settings()
