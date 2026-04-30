from __future__ import annotations

import json
import logging
from typing import List, Optional

from .config import Settings
from .email_generator import EmailGenerator
from .models import SMTPAccount
from .slack_approval import SlackApprover
from .smtp_sender import SMTPSender
from .state_store import StateStore


logger = logging.getLogger(__name__)


class FollowUpRunner:
    """Processes due follow-up emails: drafts, approves via Slack, sends."""

    def __init__(self, settings: Settings) -> None:
        self._state = StateStore(settings.state_db_path)
        self._email = EmailGenerator(api_key=settings.openai_api_key, model=settings.openai_model)
        self._sender = SMTPSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_email=settings.smtp_from_email,
            use_tls=settings.smtp_use_tls,
        )
        self._smtp_accounts = self._build_accounts(settings.smtp_accounts)
        self._approver = SlackApprover(
            bot_token=settings.slack_bot_token,
            channel_id=settings.slack_approval_channel_id,
            timeout_minutes=settings.slack_approval_timeout_minutes,
            poll_interval_seconds=settings.slack_poll_interval_seconds,
            smtp_accounts=self._smtp_accounts,
        )

    def run(self) -> dict:
        """Check and send all due follow-ups. Returns summary counts."""
        due = self._state.get_due_follow_ups()
        counts = {"due": len(due), "sent": 0, "skipped": 0, "failed": 0}

        if not due:
            logger.info("No follow-ups due.")
            return counts

        logger.info("Found %d follow-up(s) due", len(due))

        for row in due:
            follow_up_id = row["id"]
            lead_email = row["lead_email"]
            creator_name = row["creator_name"]
            original_subject = row.get("original_subject") or ""
            original_body = row.get("original_body") or ""
            sender_json = row.get("sender_account_json")

            logger.info(
                "Processing follow-up #%d: %s -> %s",
                follow_up_id, creator_name, lead_email,
            )

            # Draft follow-up email
            draft = self._email.draft_follow_up(
                original_subject=original_subject,
                original_body=original_body,
                lead_email=lead_email,
            )

            # Post to Slack for approval
            self._approver.post_message(
                f"📬 *Follow-up due* for `{lead_email}` (creator: {creator_name})\n\n"
                f"Subject: {draft.subject}\n\nBody:\n{draft.body}\n\n"
                "Reply APPROVE to send, REJECT to skip."
            )

            # Use simple channel-level polling for follow-up approval
            result = self._wait_for_simple_approval(draft, lead_email)

            if result is None:
                logger.info("Follow-up #%d skipped (rejected/timed out)", follow_up_id)
                counts["skipped"] += 1
                continue

            approved_draft, account_index = result

            # Send the follow-up
            account = self._resolve_account(sender_json, account_index)
            if account:
                send_result = self._sender.send_with_account(
                    to_email=lead_email, draft=approved_draft, account=account,
                )
            else:
                send_result = self._sender.send(to_email=lead_email, draft=approved_draft)

            if send_result.success:
                self._state.mark_follow_up_sent(follow_up_id, send_result.message_id)
                logger.info("✓ Follow-up #%d sent to %s", follow_up_id, lead_email)
                counts["sent"] += 1
            else:
                logger.error("❌ Follow-up #%d send failed: %s", follow_up_id, send_result.error)
                counts["failed"] += 1

        logger.info("Follow-up run complete: %s", counts)
        return counts

    def _wait_for_simple_approval(self, draft, lead_email):
        """Simple Slack approval: post and wait for APPROVE/REJECT in channel."""
        import time

        seen = set()
        deadline = time.time() + 120 * 60  # 2 hour timeout

        # Post the guidance
        guidance = self._approver._client.chat_postMessage(
            channel=self._approver._channel_id,
            text=(
                f"Reply with APPROVE to send follow-up to `{lead_email}`, "
                "or REJECT to skip."
            ),
        )
        guidance_ts = str(guidance.get("ts") or "").strip()
        if guidance_ts:
            seen.add(guidance_ts)

        while time.time() < deadline:
            history = self._approver._client.conversations_history(
                channel=self._approver._channel_id, limit=10,
            ).get("messages", [])

            for msg in history:
                ts = msg.get("ts")
                if not ts or ts in seen:
                    continue
                seen.add(ts)

                if not self._approver._is_human_reviewer_message(msg):
                    continue

                text = str(msg.get("text") or "").strip().upper()
                if text.startswith("APPROVE"):
                    # Ask for sender selection if accounts available
                    if self._smtp_accounts:
                        lines = ["Which email account for this follow-up?\n"]
                        for i, acct in enumerate(self._smtp_accounts):
                            lines.append(f"  *{i + 1}.* {acct.label} — `{acct.from_email}`")
                        lines.append("\nReply with the number or CANCEL.")
                        self._approver.post_message("\n".join(lines))

                        # Wait for selection
                        sel_seen = set(seen)
                        while time.time() < deadline:
                            h2 = self._approver._client.conversations_history(
                                channel=self._approver._channel_id, limit=5,
                            ).get("messages", [])
                            for m2 in h2:
                                t2 = m2.get("ts")
                                if not t2 or t2 in sel_seen:
                                    continue
                                sel_seen.add(t2)
                                if not self._approver._is_human_reviewer_message(m2):
                                    continue
                                reply = str(m2.get("text") or "").strip()
                                if reply.upper() == "CANCEL":
                                    return None
                                try:
                                    choice = int(reply)
                                    if 1 <= choice <= len(self._smtp_accounts):
                                        return (draft, choice - 1)
                                except ValueError:
                                    pass
                            time.sleep(self._approver._poll_interval_seconds)
                        return None
                    return (draft, None)

                if text.startswith("REJECT"):
                    return None

            time.sleep(self._approver._poll_interval_seconds)

        return None

    def _resolve_account(self, sender_json: Optional[str], account_index: Optional[int]) -> Optional[SMTPAccount]:
        """Resolve the SMTPAccount to use for sending."""
        # Prefer the account selected during follow-up approval
        if account_index is not None and 0 <= account_index < len(self._smtp_accounts):
            return self._smtp_accounts[account_index]

        # Fall back to the account used for the original email
        if sender_json:
            try:
                data = json.loads(sender_json)
                return SMTPAccount(
                    label=data.get("label", "Original"),
                    host=data["host"],
                    port=int(data["port"]),
                    username=data["username"],
                    password=data["password"],
                    from_email=data["from_email"],
                    use_tls=data.get("use_tls", True),
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Failed to parse sender_account_json: %s", exc)

        return None

    @staticmethod
    def _build_accounts(raw_accounts: list) -> List[SMTPAccount]:
        accounts = []
        for acct in raw_accounts:
            accounts.append(SMTPAccount(
                label=acct["label"],
                host=acct["host"],
                port=int(acct["port"]),
                username=acct["username"],
                password=acct["password"],
                from_email=acct["from_email"],
                use_tls=acct.get("use_tls", True),
            ))
        return accounts
