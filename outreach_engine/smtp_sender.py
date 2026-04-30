from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .models import EmailDraft, SMTPAccount, SendResult


class SMTPSender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
        use_tls: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_email = from_email
        self._use_tls = use_tls

    def send(self, to_email: str, draft: EmailDraft) -> SendResult:
        message = EmailMessage()
        message["From"] = self._from_email
        message["To"] = to_email
        message["Subject"] = draft.subject
        message.set_content(draft.body)

        try:
            with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                if self._use_tls:
                    smtp.starttls()
                smtp.login(self._username, self._password)
                smtp.send_message(message)
            return SendResult(success=True, message_id=message.get("Message-ID"), error=None)
        except Exception as exc:
            return SendResult(success=False, message_id=None, error=str(exc))

    def send_with_account(self, to_email: str, draft: EmailDraft, account: SMTPAccount) -> SendResult:
        message = EmailMessage()
        message["From"] = account.from_email
        message["To"] = to_email
        message["Subject"] = draft.subject
        message.set_content(draft.body)

        try:
            with smtplib.SMTP(account.host, account.port, timeout=30) as smtp:
                if account.use_tls:
                    smtp.starttls()
                smtp.login(account.username, account.password)
                smtp.send_message(message)
            return SendResult(success=True, message_id=message.get("Message-ID"), error=None)
        except Exception as exc:
            return SendResult(success=False, message_id=None, error=str(exc))
