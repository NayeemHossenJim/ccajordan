from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from slack_sdk import WebClient

from .email_generator import EmailGenerator
from .models import ApolloContact, Creator, EmailDraft, Lead, SMTPAccount


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalContext:
    creator: Creator
    lead: Lead
    contact: ApolloContact


class SlackApprover:
    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        timeout_minutes: int,
        poll_interval_seconds: int,
        smtp_accounts: Optional[List[SMTPAccount]] = None,
    ) -> None:
        self._client = WebClient(token=bot_token)
        self._channel_id = channel_id
        self._timeout_seconds = timeout_minutes * 60
        self._poll_interval_seconds = max(5, poll_interval_seconds)
        self._smtp_accounts = smtp_accounts or []
        self._bot_user_id: Optional[str] = None
        try:
            auth = self._client.auth_test()
            self._bot_user_id = str(auth.get("user_id") or "").strip() or None
        except Exception as exc:
            logger.warning("Slack auth_test failed during approver init: %s", exc)

    def review_until_approved(
        self,
        draft: EmailDraft,
        context: ApprovalContext,
        email_generator: EmailGenerator,
    ) -> Optional[Tuple[EmailDraft, Optional[int]]]:
        """Returns (approved_draft, account_index) or None if rejected/timed out.

        account_index is the index into smtp_accounts, or None if no accounts configured.
        """
        post_response = self._client.chat_postMessage(
            channel=self._channel_id,
            text=self._build_preview_text(draft, context),
        )
        thread_ts = post_response["ts"]

        guidance_response = self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text=(
                "Reply in this thread with APPROVE to send, REJECT to skip, "
                "or any text instruction to revise the email."
            ),
        )

        current_draft = draft
        seen = {thread_ts}
        guidance_ts = str(guidance_response.get("ts") or "").strip()
        if guidance_ts:
            seen.add(guidance_ts)
        deadline = time.time() + self._timeout_seconds

        while time.time() < deadline:
            replies = self._client.conversations_replies(channel=self._channel_id, ts=thread_ts).get("messages", [])
            for message in replies:
                message_ts = message.get("ts")
                if not message_ts or message_ts in seen:
                    continue
                seen.add(message_ts)

                # Ignore bot/system messages so only human reviewer text drives revisions.
                if not self._is_human_reviewer_message(message):
                    continue

                text = str(message.get("text") or "").strip()
                if not text:
                    continue

                upper = text.upper()
                if upper.startswith("APPROVE"):
                    # If multiple sender accounts configured, ask which one to use
                    if self._smtp_accounts:
                        account_index = self._ask_sender_selection(thread_ts, seen, deadline)
                        if account_index is None:
                            self._client.chat_postMessage(
                                channel=self._channel_id,
                                thread_ts=thread_ts,
                                text="No sender selected. Skipping this contact.",
                            )
                            return None
                        self._client.chat_postMessage(
                            channel=self._channel_id,
                            thread_ts=thread_ts,
                            text=f"✓ Approved. Sending from: {self._smtp_accounts[account_index].from_email}",
                        )
                        return (current_draft, account_index)
                    else:
                        self._client.chat_postMessage(
                            channel=self._channel_id,
                            thread_ts=thread_ts,
                            text="Approved. Sending now.",
                        )
                        return (current_draft, None)

                if upper.startswith("REJECT"):
                    self._client.chat_postMessage(
                        channel=self._channel_id,
                        thread_ts=thread_ts,
                        text="Rejected. Skipping this contact.",
                    )
                    return None

                current_draft = email_generator.revise_email(current_draft=current_draft, instructions=text, strict=True)
                self._client.chat_postMessage(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    text="Updated draft preview:\n" + self._build_preview_text(current_draft, context),
                )

            time.sleep(self._poll_interval_seconds)

        self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text="Approval timed out. Skipping this contact.",
        )
        return None

    def _ask_sender_selection(
        self,
        thread_ts: str,
        seen: set,
        deadline: float,
    ) -> Optional[int]:
        """Post sender options and wait for numeric selection."""
        lines = ["Which email account should I use to send?\n"]
        for i, account in enumerate(self._smtp_accounts):
            lines.append(f"  *{i + 1}.* {account.label} — `{account.from_email}`")
        lines.append("\nReply with the number (e.g., `1`) or `CANCEL` to skip.")

        prompt_response = self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text="\n".join(lines),
        )
        prompt_ts = str(prompt_response.get("ts") or "").strip()
        if prompt_ts:
            seen.add(prompt_ts)

        while time.time() < deadline:
            replies = self._client.conversations_replies(
                channel=self._channel_id, ts=thread_ts
            ).get("messages", [])

            for message in replies:
                message_ts = message.get("ts")
                if not message_ts or message_ts in seen:
                    continue
                seen.add(message_ts)

                if not self._is_human_reviewer_message(message):
                    continue

                text = str(message.get("text") or "").strip()
                if not text:
                    continue

                if text.upper() == "CANCEL":
                    return None

                try:
                    choice = int(text)
                    if 1 <= choice <= len(self._smtp_accounts):
                        return choice - 1
                    else:
                        self._client.chat_postMessage(
                            channel=self._channel_id,
                            thread_ts=thread_ts,
                            text=f"Invalid choice. Please enter a number between 1 and {len(self._smtp_accounts)}.",
                        )
                except ValueError:
                    self._client.chat_postMessage(
                        channel=self._channel_id,
                        thread_ts=thread_ts,
                        text=f"Please reply with a number (1–{len(self._smtp_accounts)}) or CANCEL.",
                    )

            time.sleep(self._poll_interval_seconds)

        return None

    def post_brand_results(self, niche: str, brands: List[dict]) -> None:
        """Post brand finder results to the Slack channel."""
        if not brands:
            self._client.chat_postMessage(
                channel=self._channel_id,
                text=f"🔍 Brand Finder: No brands found for niche '{niche}'.",
            )
            return

        lines = [f"🔍 *Brand Finder Results: {niche}*\n"]
        for i, brand in enumerate(brands, 1):
            name = brand.get("name", "Unknown")
            website = brand.get("website", "N/A")
            description = brand.get("description", "")
            lines.append(f"  *{i}.* *{name}* — {website}")
            if description:
                lines.append(f"      _{description}_")
        lines.append(f"\n_Found {len(brands)} brands for '{niche}'_")

        self._client.chat_postMessage(
            channel=self._channel_id,
            text="\n".join(lines),
        )

    def post_message(self, text: str) -> None:
        """Post a plain message to the approval channel."""
        self._client.chat_postMessage(channel=self._channel_id, text=text)

    def _is_human_reviewer_message(self, message: dict) -> bool:
        subtype = str(message.get("subtype") or "").strip().lower()
        if subtype:
            return False
        if message.get("bot_id"):
            return False
        user_id = str(message.get("user") or "").strip()
        if not user_id:
            return False
        if self._bot_user_id and user_id == self._bot_user_id:
            return False
        return True

    @staticmethod
    def _build_preview_text(draft: EmailDraft, context: ApprovalContext) -> str:
        return (
            f"Creator: {context.creator.creator_name}\n"
            f"Creator niche: {context.creator.creator_niche}\n"
            f"Target: {context.contact.full_name} ({context.contact.title})\n"
            f"Organization: {context.contact.organization_name}\n"
            f"Lead: {context.lead.title}\n"
            f"Source: {context.lead.link}\n\n"
            f"Subject: {draft.subject}\n\n"
            f"Body:\n{draft.body}"
        )
