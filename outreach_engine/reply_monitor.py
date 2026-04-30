from __future__ import annotations

import email as email_lib
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parseaddr
from typing import List, Optional, Set

from slack_sdk import WebClient

from .config import Settings
from .state_store import StateStore


logger = logging.getLogger(__name__)


class ReplyMonitor:
    """Monitors IMAP inboxes for replies from leads and auto-cancels follow-ups."""

    def __init__(self, settings: Settings) -> None:
        self._state = StateStore(settings.state_db_path)
        self._slack = WebClient(token=settings.slack_bot_token)
        self._channel_id = settings.slack_approval_channel_id
        self._accounts = settings.smtp_accounts  # raw dicts

    def check_all_accounts(self) -> dict:
        """Check all SMTP account inboxes for replies. Returns summary counts."""
        counts = {"accounts_checked": 0, "accounts_skipped": 0, "replies_found": 0}

        pending_emails = self._state.get_pending_follow_up_emails()
        if not pending_emails:
            logger.debug("No pending follow-ups to monitor for replies.")
            return counts

        logger.info(
            "Checking %d inbox(es) for replies from %d pending lead(s)",
            len(self._accounts), len(pending_emails),
        )

        for acct in self._accounts:
            label = acct.get("label", "Unknown")
            from_email = acct.get("from_email", "")

            # Skip placeholder accounts
            if "your-email" in acct.get("username", "") or "your-app-password" in acct.get("password", ""):
                logger.debug("Skipping placeholder account: %s", label)
                counts["accounts_skipped"] += 1
                continue

            try:
                found = self._check_account(acct, pending_emails)
                counts["accounts_checked"] += 1
                counts["replies_found"] += found
                self.save_last_check(from_email)
            except Exception as exc:
                logger.warning("Failed to check inbox for %s (%s): %s", label, from_email, exc)
                counts["accounts_skipped"] += 1

        if counts["replies_found"]:
            logger.info("Reply monitor found %d reply(ies)", counts["replies_found"])

        return counts

    def _check_account(self, acct: dict, pending_emails: Set[str]) -> int:
        """Check a single IMAP inbox. Returns number of replies found."""
        imap_host = acct.get("imap_host") or self._derive_imap_host(acct["host"])
        imap_port = int(acct.get("imap_port", 993))
        username = acct["username"]
        password = acct["password"]
        label = acct.get("label", username)

        # Get last check timestamp for this account
        kv_key = f"imap_last_check:{acct['from_email']}"
        last_check_data = self._state.get_json(kv_key)
        if last_check_data and "ts" in last_check_data:
            since_date = datetime.fromisoformat(last_check_data["ts"])
        else:
            # First run: check last 7 days
            since_date = datetime.now(timezone.utc) - timedelta(days=7)

        # Format date for IMAP SINCE search (DD-Mon-YYYY)
        since_str = since_date.strftime("%d-%b-%Y")

        logger.debug("Checking IMAP inbox for %s since %s", label, since_str)

        conn = imaplib.IMAP4_SSL(imap_host, imap_port)
        try:
            conn.login(username, password)
            conn.select("INBOX", readonly=True)

            # Search for emails since last check
            status, msg_ids = conn.search(None, f'(SINCE "{since_str}")')
            if status != "OK" or not msg_ids[0]:
                logger.debug("No new emails in %s since %s", label, since_str)
                return 0

            id_list = msg_ids[0].split()
            logger.debug("Found %d email(s) in %s since %s", len(id_list), label, since_str)

            replies_found = 0
            # Process in batches, newest first
            for msg_id in reversed(id_list):
                sender = self._get_sender(conn, msg_id)
                if not sender:
                    continue

                sender_lower = sender.lower().strip()
                if sender_lower in {e.lower() for e in pending_emails}:
                    logger.info(
                        "✉️ Reply detected from %s in %s inbox", sender, label
                    )
                    count = self._state.mark_replied(sender)
                    if count:
                        replies_found += 1
                        # Remove from pending set so we don't double-process
                        pending_emails.discard(sender_lower)
                        self._notify_slack(sender, count, label)

            return replies_found
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _get_sender(self, conn: imaplib.IMAP4_SSL, msg_id: bytes) -> Optional[str]:
        """Fetch only the From header of an email and extract the address."""
        try:
            status, data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if status != "OK" or not data or not data[0]:
                return None

            raw_header = data[0][1] if isinstance(data[0], tuple) else data[0]
            if isinstance(raw_header, bytes):
                raw_header = raw_header.decode("utf-8", errors="replace")

            msg = email_lib.message_from_string(raw_header)
            from_header = msg.get("From", "")

            # Decode if encoded
            decoded_parts = decode_header(from_header)
            decoded_from = ""
            for part, charset in decoded_parts:
                if isinstance(part, bytes):
                    decoded_from += part.decode(charset or "utf-8", errors="replace")
                else:
                    decoded_from += part

            _, addr = parseaddr(decoded_from)
            return addr if addr else None
        except Exception as exc:
            logger.debug("Failed to parse sender from msg %s: %s", msg_id, exc)
            return None

    def _notify_slack(self, lead_email: str, count: int, account_label: str) -> None:
        """Post a Slack notification about an auto-detected reply."""
        try:
            self._slack.chat_postMessage(
                channel=self._channel_id,
                text=(
                    f"✅ *Auto-detected reply* from `{lead_email}` "
                    f"(inbox: {account_label})\n"
                    f"Cancelled {count} pending follow-up(s). No follow-up will be sent."
                ),
            )
        except Exception as exc:
            logger.warning("Failed to post reply notification to Slack: %s", exc)

    # Save last check timestamp after successful run
    def save_last_check(self, from_email: str) -> None:
        kv_key = f"imap_last_check:{from_email}"
        self._state.set_json(kv_key, {"ts": datetime.now(timezone.utc).isoformat()})

    @staticmethod
    def _derive_imap_host(smtp_host: str) -> str:
        """Derive IMAP host from SMTP host. smtp.gmail.com -> imap.gmail.com"""
        if smtp_host.startswith("smtp."):
            return "imap." + smtp_host[5:]
        return smtp_host
