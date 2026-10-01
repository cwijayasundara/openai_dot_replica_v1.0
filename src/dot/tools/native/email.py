"""Draft an email, or send one through a transport that holds the credential."""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from dot.tools.native.deps import SMTP_CREDENTIAL, CredentialError, ToolDeps
from dot.tools.results import fail, ok

MAX_CHARS = 100_000


def _address(value: str) -> str | None:
    text = value.strip()
    if "@" not in text or text.startswith("@") or text.endswith("@") or " " in text:
        return None
    return text


def build_draft_email(deps: ToolDeps) -> BaseTool:
    def draft_email(to: str, subject: str, body: str) -> str:
        """Save an email draft and return its artifact id. This does not send the email."""
        recipient = _address(to)
        if recipient is None or not subject.strip() or not body.strip():
            return fail("to, subject and body are required")
        if len(body) > MAX_CHARS:
            return fail("draft is too long")
        artifact_id = deps.artifacts.put_text(body)
        return ok(artifact_id=artifact_id, to=recipient, subject=subject.strip(), byte_count=len(body.encode("utf-8")))

    return StructuredTool.from_function(draft_email, name="draft_email")


def build_send_email(deps: ToolDeps) -> BaseTool:
    def send_email(to: str, subject: str, body: str) -> str:
        """Send an email. Credentials stay with the broker; the result never includes them."""
        recipient = _address(to)
        if recipient is None or not subject.strip() or not body.strip():
            return fail("to, subject and body are required")
        if len(body) > MAX_CHARS:
            return fail("message is too long")
        if deps.email is None or deps.credentials is None:
            return fail("email transport is not configured")
        try:
            credential = deps.credentials.resolve(SMTP_CREDENTIAL)
        except CredentialError:
            return fail("could not resolve email credentials")
        try:
            message_id = deps.email.send(to=recipient, subject=subject.strip(), body=body, credential=credential)
        except RuntimeError as exc:
            return fail(str(exc))
        artifact_id = deps.artifacts.put_text(body)
        return ok(artifact_id=artifact_id, to=recipient, subject=subject.strip(), message_id=message_id)

    return StructuredTool.from_function(send_email, name="send_email")
