"""Outgoing email. Only password reset sends any.

Three backends, chosen by LOCQA_MAIL_BACKEND:

- console  logs the message, link included — local development, where there
           is nobody to email and you want to click the link yourself
- resend   POSTs to Resend's HTTP API with RESEND_API_KEY
- memory   appends to `outbox` — tests

Sending happens after the response has gone out (a FastAPI background task),
so a reset request takes the same time whether or not the email has an
account, and a slow mail API never holds a request open.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

from .settings import Settings

log = logging.getLogger("locqa_web.mail")

RESEND_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str


class ConsoleMailer:
    def send(self, message: Message) -> None:
        log.warning("email to %s — %s\n%s", message.to, message.subject, message.text)


class MemoryMailer:
    def __init__(self) -> None:
        self.outbox: list[Message] = []

    def send(self, message: Message) -> None:
        self.outbox.append(message)


class ResendMailer:
    def __init__(self, api_key: str, sender: str) -> None:
        self._api_key = api_key
        self._sender = sender

    def send(self, message: Message) -> None:
        body = json.dumps({
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }).encode()
        request = urllib.request.Request(
            RESEND_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except Exception:                       # noqa: BLE001 - logged; the user saw a
            # generic "check your email" page either way, so there is nobody to
            # report this to but the logs.
            log.exception("sending email to %s failed", message.to)


def make_mailer(settings: Settings):
    if settings.mail_backend == "resend":
        return ResendMailer(settings.resend_api_key, settings.mail_from)
    if settings.mail_backend == "memory":
        return MemoryMailer()
    return ConsoleMailer()
