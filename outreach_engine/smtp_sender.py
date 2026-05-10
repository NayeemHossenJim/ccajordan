from __future__ import annotations

import html
import re
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
        message = self._build_message(from_email=self._from_email, to_email=to_email, draft=draft)

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
        message = self._build_message(from_email=account.from_email, to_email=to_email, draft=draft)

        try:
            with smtplib.SMTP(account.host, account.port, timeout=30) as smtp:
                if account.use_tls:
                    smtp.starttls()
                smtp.login(account.username, account.password)
                smtp.send_message(message)
            return SendResult(success=True, message_id=message.get("Message-ID"), error=None)
        except Exception as exc:
            return SendResult(success=False, message_id=None, error=str(exc))

    def _build_message(self, from_email: str, to_email: str, draft: EmailDraft) -> EmailMessage:
        message = EmailMessage()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = draft.subject
        plain_body = (draft.body or "").strip()
        message.set_content(plain_body)
        message.add_alternative(self._plain_to_html(plain_body), subtype="html")
        return message

    @staticmethod
    def _plain_to_html(body: str) -> str:
        normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        if not paragraphs:
            paragraphs = [normalized.strip()]

        html_paragraphs = []
        for paragraph in paragraphs:
            escaped = html.escape(paragraph).replace("\n", "<br>")
            html_paragraphs.append(f"<p style=\"margin:0 0 12px;\">{escaped}</p>")

        content = "".join(html_paragraphs)
        return (
            "<!doctype html>"
            "<html>"
            "<body style=\"font-family:Arial,Helvetica,sans-serif; line-height:1.5; color:#111;\">"
            f"{content}"
            "</body>"
            "</html>"
        )
