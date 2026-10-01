"""Credential handles, provider failures and dynamic value redaction (no cloud calls)."""

import json
from types import SimpleNamespace
from uuid import uuid4

import google_crc32c
import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from dot.config import Settings
from dot.middleware.redaction import MASK, RedactionMiddleware, Redactor
from dot.safety.credentials import CredentialBroker, EnvironmentSecrets, SecretManagerSecrets, build_credential_broker
from dot.tools.native.deps import CredentialError


def test_environment_handles_are_allowlisted_and_registered() -> None:
    value = uuid4().hex
    redactor = Redactor()
    broker = CredentialBroker(
        {"cred:smtp": "DOT_SMTP_CREDENTIAL"}, EnvironmentSecrets({"DOT_SMTP_CREDENTIAL": value}), redactor
    )
    assert broker.resolve("cred:smtp") == value
    assert redactor.text(value) == MASK
    with pytest.raises(CredentialError, match="unknown"):
        broker.resolve("DOT_SMTP_CREDENTIAL")
    with pytest.raises(CredentialError, match="unknown"):
        broker.resolve("cred:unconfigured")


@pytest.mark.parametrize("failure", ["empty", "missing", "provider"])
def test_broker_failure_never_exposes_provider_details(failure: str) -> None:
    value = uuid4().hex

    class Broken:
        def read(self, reference: str) -> str:
            if failure == "provider":
                raise RuntimeError(value)
            if failure == "missing":
                raise KeyError(value)
            return ""

    broker = CredentialBroker({"cred:smtp": "REF"}, Broken(), Redactor())
    with pytest.raises(CredentialError) as error:
        broker.resolve("cred:smtp")
    assert value not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("corrupt", [False, True])
def test_secret_manager_is_lazy_allowlisted_and_checks_integrity(corrupt: bool) -> None:
    value = uuid4().hex
    opened = []
    requested = []

    class Client:
        def __enter__(self):
            opened.append("open")
            return self

        def __exit__(self, *args):
            opened.append("closed")

        def access_secret_version(self, *, request, timeout, retry):
            requested.append(request)
            assert timeout == 10.0
            assert retry is None
            data = value.encode()
            return SimpleNamespace(
                payload=SimpleNamespace(data=data, data_crc32c=google_crc32c.value(data) + int(corrupt))
            )

    redactor = Redactor()
    reference = "projects/test-project/secrets/smtp/versions/latest"
    broker = CredentialBroker({"cred:smtp": reference}, SecretManagerSecrets(Client), redactor)
    assert not opened
    with pytest.raises(CredentialError):
        broker.resolve("cred:other")
    assert not opened
    if corrupt:
        with pytest.raises(CredentialError):
            broker.resolve("cred:smtp")
    else:
        assert broker.resolve("cred:smtp") == value
        assert redactor.text(value) == MASK
    assert requested == [{"name": reference}]
    assert opened == ["open", "closed"]


def test_default_broker_uses_dotenv_loaded_settings() -> None:
    value = uuid4().hex
    settings = Settings(_env_file=None, smtp_credential=value)
    redactor = Redactor()
    assert build_credential_broker(settings, redactor).resolve("cred:smtp") == value
    assert value not in repr(settings)


def test_redaction_covers_nested_metadata_args_system_and_short_values() -> None:
    value = uuid4().hex + '"\n'
    redactor = Redactor([value, "xy"])
    assert redactor.text("xy") == MASK
    original = AIMessage(
        content=[{"type": "text", "text": value, "nested": {"value": value}}],
        tool_calls=[{"name": "tool", "args": {"value": value}, "id": "call-1", "type": "tool_call"}],
        additional_kwargs={"reasoning_content": value},
        response_metadata={"nested": [value]},
    )
    cleaned = redactor.message(original)
    assert value not in json.dumps(cleaned.model_dump())
    assert cleaned.tool_calls[0]["args"]["value"] == MASK
    assert original.tool_calls[0]["args"]["value"] == value
    assert redactor.message(SystemMessage(value)).content == MASK
    assert redactor.text(json.dumps({"value": value})) == json.dumps({"value": MASK})
    result = RedactionMiddleware(redactor=redactor)._tool(
        ToolMessage(content=json.dumps({"value": value}), tool_call_id="call-1", artifact={"value": value})
    )
    assert result.content == json.dumps({"value": MASK})
    assert result.artifact == {"value": MASK}
