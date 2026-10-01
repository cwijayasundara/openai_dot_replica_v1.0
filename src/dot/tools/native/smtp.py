"""Send mail through an SMTP relay. The password is resolved per send by the broker."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

_TIMEOUT_S = 30.0


class SmtpTransport:
    def __init__(self, host: str, port: int, username: str, sender: str, *, starttls: bool = True) -> None:
        self._host, self._port = host, port
        self._username, self._sender = username, sender
        self._starttls = starttls

    def send(self, *, to: str, subject: str, body: str, credential: str) -> str:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=_TIMEOUT_S) as smtp:
                if self._starttls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(self._username, credential)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # The server's reply can echo the login; report only the failure type.
            raise RuntimeError(f"email delivery failed ({type(exc).__name__})") from None
        return str(message["Message-ID"])
