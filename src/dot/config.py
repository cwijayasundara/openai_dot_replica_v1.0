"""Runtime settings. Every variable is read with the ``DOT_`` prefix."""

from __future__ import annotations

import os
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # A schedule's ceilings when its pack sets none. Calls include its subagents'.
    schedule_max_model_calls: int = Field(default=20, ge=1)
    schedule_max_tokens: int = Field(default=200_000, ge=1)
    # IANA zone the pack crons are read in, so working hours mean local hours.
    schedule_timezone: str = "UTC"
    # Cloud Scheduler's OIDC token: the audience it is minted for and the service account it names.
    scheduler_audience: str | None = None
    scheduler_invoker: str | None = None
    # One reflection reads at most this many episodes (oldest first) and keeps at most this many edits.
    reflection_max_episodes: int = Field(default=200, ge=1)
    reflection_max_edits: int = Field(default=5, ge=1)
    # The replay gate: cited and random episodes per edit, and model calls per replay.
    replay_cited: int = Field(default=5, ge=1)
    replay_random: int = Field(default=5, ge=0)
    replay_max_model_calls: int = Field(default=4, ge=1)
    # Threads per worker process that run background jobs beside the inbox loop.
    job_workers: int = Field(default=2, ge=0)

    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_app_token: str | None = Field(default=None, repr=False)
    slack_signing_secret: str | None = Field(default=None, repr=False)
    smtp_credential: str | None = Field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_sender: str | None = None
    smtp_starttls: bool = True
    tavily_api_key: str | None = Field(default=None, repr=False)
    slack_mode: Literal["socket", "http"] = "socket"
    # Approvers added by this deployment, per pack: {"research-analyst": ["U123ABC"]}.
    pack_approvers: dict[str, list[str]] = Field(default_factory=dict)
    credential_backend: Literal["env", "secret_manager"] = "env"
    # Where this process runs. A fixed dev principal is refused outside ``local``.
    env: Literal["local", "cloud"] = "local"
    # How the API learns the web caller: nobody, a fixed dev user, or an IAP-signed header.
    web_auth: Literal["off", "dev", "iap"] = "off"
    web_dev_user: str | None = None
    iap_audience: str | None = None
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
        if self.web_auth == "dev" and (self.env != "local" or not self.web_dev_user):
            raise ValueError("DOT_WEB_AUTH=dev needs DOT_ENV=local and DOT_WEB_DEV_USER")
        # Cloud Run sets K_SERVICE: a fixed dev user must never serve there, whatever DOT_ENV says.
        if self.web_auth == "dev" and os.environ.get("K_SERVICE"):
            raise ValueError("DOT_WEB_AUTH=dev is refused on Cloud Run")
        if bool(self.scheduler_audience) != bool(self.scheduler_invoker):
            raise ValueError("DOT_SCHEDULER_AUDIENCE and DOT_SCHEDULER_INVOKER are set together")
        try:
            ZoneInfo(self.schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown DOT_SCHEDULE_TIMEZONE {self.schedule_timezone!r}") from exc
        if self.web_auth == "iap" and not self.iap_audience:
            raise ValueError("DOT_IAP_AUDIENCE is required for DOT_WEB_AUTH=iap")
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
