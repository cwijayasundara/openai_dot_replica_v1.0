from __future__ import annotations

import pytest
from pydantic import ValidationError

from dot.config import FAST_MODEL, HEAVY_MODEL, SUPERVISOR_MODEL, Settings


def test_defaults_are_fireworks(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DOT_MODEL_PROVIDER",
        "DOT_FIREWORKS_API_KEY",
        "DOT_OPENAI_BASE_URL",
        "DOT_DATABASE_URL",
        "DOT_SANDBOX_BACKEND",
        "DOT_SUPERVISOR_MODEL",
        "DOT_HEAVY_MODEL",
        "DOT_FAST_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_provider == "fireworks"
    assert settings.supervisor_model == SUPERVISOR_MODEL
    assert settings.heavy_model == HEAVY_MODEL
    assert settings.fast_model == FAST_MODEL
    assert settings.database_url is None
    assert settings.sandbox_backend == "docker"
    assert settings.fireworks_api_key is None


def test_env_prefix_parses_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOT_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.setenv("DOT_OPENAI_BASE_URL", "https://api.fireworks.ai/inference/v1")
    monkeypatch.setenv("DOT_SUPERVISOR_MODEL", "accounts/fireworks/models/glm-5p3")
    monkeypatch.setenv("DOT_MAX_MODEL_CALLS", "12")
    monkeypatch.setenv("DOT_MODEL_TIMEOUT_S", "30")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_provider == "openai_compatible"
    assert settings.openai_base_url == "https://api.fireworks.ai/inference/v1"
    assert settings.max_model_calls == 12
    assert settings.model_timeout_s == 30


def test_openai_compatible_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOT_MODEL_PROVIDER", "openai_compatible")
    monkeypatch.delenv("DOT_OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_openshell_requires_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOT_SANDBOX_BACKEND", "openshell")
    monkeypatch.delenv("DOT_OPENSHELL_GATEWAY", raising=False)
    with pytest.raises(ValidationError, match="OPENSHELL_GATEWAY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_unknown_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOT_MODEL_PROVIDER", "anthropic")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
