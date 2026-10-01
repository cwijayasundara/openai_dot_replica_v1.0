"""G4 live check: the broker resolves a real Secret Manager secret and registers it for redaction.

Creates a throwaway secret in ``DOT_GCP_TEST_PROJECT`` and deletes it afterwards.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest
from google.cloud import secretmanager

from dot.config import Settings
from dot.middleware.redaction import MASK, Redactor
from dot.safety.credentials import build_credential_broker
from dot.tools.native.deps import CredentialError

pytestmark = pytest.mark.gcp


def test_secret_manager_resolves_and_redacts_a_real_secret() -> None:
    project = os.environ.get("DOT_GCP_TEST_PROJECT")
    if not project:
        pytest.fail("set DOT_GCP_TEST_PROJECT to a project where a test secret may be created")
    client = secretmanager.SecretManagerServiceClient()
    secret_id = f"dot-g4-check-{uuid.uuid4().hex[:12]}"
    value = "g4-" + secrets.token_urlsafe(24)
    parent = f"projects/{project}"
    secret = client.create_secret(
        request={"parent": parent, "secret_id": secret_id, "secret": {"replication": {"automatic": {}}}}
    )
    try:
        client.add_secret_version(request={"parent": secret.name, "payload": {"data": value.encode()}})
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            credential_backend="secret_manager",
            credential_bindings={
                "cred:smtp": f"{secret.name}/versions/latest",
                "cred:missing": f"{parent}/secrets/{secret_id}-absent/versions/latest",
            },
        )
        redactor = Redactor()
        broker = build_credential_broker(settings, redactor)

        assert broker.resolve("cred:smtp") == value
        assert redactor.text(f"token={value}") == f"token={MASK}"
        with pytest.raises(CredentialError) as missing:
            broker.resolve("cred:missing")
        assert secret_id not in str(missing.value) and value not in str(missing.value)
    finally:
        client.delete_secret(request={"name": secret.name})
