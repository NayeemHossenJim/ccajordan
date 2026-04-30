from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from .apollo_client import ApolloClient
from .config import Settings
from .email_generator import EmailGenerator
from .models import Creator, Lead, SMTPAccount
from .niche_matcher import NicheMatcher
from .rss_parser import RSSParser
from .sheets_loader import SheetsLoader
from .slack_approval import ApprovalContext, SlackApprover
from .smtp_sender import SMTPSender
from .state_store import StateStore


logger = logging.getLogger(__name__)


class OutreachWorkflow:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = StateStore(settings.state_db_path)
        self._sheets = SheetsLoader(settings)
        self._rss = RSSParser(
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
        )
        self._matcher = NicheMatcher(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
        self._apollo = ApolloClient(
            api_key=settings.apollo_api_key,
            base_url=settings.apollo_base_url,
            requests_per_minute=settings.apollo_requests_per_minute,
            max_retries=settings.apollo_max_retries,
            initial_backoff_seconds=settings.apollo_initial_backoff_seconds,
            search_max_pages=settings.apollo_search_max_pages,
            search_per_page=settings.apollo_search_per_page,
            search_contact_email_statuses=settings.apollo_search_contact_email_statuses,
            allowed_email_statuses=settings.apollo_allowed_email_statuses,
        )
        self._email = EmailGenerator(api_key=settings.openai_api_key, model=settings.openai_model)
        self._smtp_accounts = self._build_accounts(settings.smtp_accounts)
        self._approver = SlackApprover(
            bot_token=settings.slack_bot_token,
            channel_id=settings.slack_approval_channel_id,
            timeout_minutes=settings.slack_approval_timeout_minutes,
            poll_interval_seconds=settings.slack_poll_interval_seconds,
            smtp_accounts=self._smtp_accounts,
        )
        self._sender = SMTPSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_email=settings.smtp_from_email,
            use_tls=settings.smtp_use_tls,
        )

    def run(self, run_id: str) -> None:
        self._state.start_run(run_id, "Run started")
        metrics = self._new_metrics()
        try:
            creators = self._sheets.load_creators()
            feeds = self._sheets.load_rss_feeds()
            self._state.update_run(run_id, "running", f"Loaded {len(creators)} creators and {len(feeds)} feeds")
            self._execute_loop(run_id, creators, feeds, metrics)
            self._state.set_json(f"run_metrics:{run_id}", metrics)
            summary = self._metrics_summary(metrics)
            self._state.update_run(run_id, "completed", f"Run completed successfully. {summary}", completed=True)
        except Exception as exc:
            logger.exception("Workflow failed")
            self._state.set_json(f"run_metrics:{run_id}", metrics)
            self._state.update_run(run_id, "failed", f"Run failed: {exc}", completed=True)
            raise

    def _execute_loop(self, run_id: str, creators: List[Creator], feeds: List, metrics: Dict[str, int]) -> None:
        cursor = self._state.get_json("cursor") or {"creator_index": 0, "feed_index": 0, "lead_index": 0}
        start_creator = int(cursor.get("creator_index", 0))

        for creator_index in range(start_creator, len(creators)):
            creator = creators[creator_index]
            feed_start = int(cursor.get("feed_index", 0)) if creator_index == start_creator else 0

            for feed_index in range(feed_start, len(feeds)):
                feed = feeds[feed_index]
                lead_start = (
                    int(cursor.get("lead_index", 0))
                    if creator_index == start_creator and feed_index == feed_start
                    else 0
                )

                self._state.update_run(
                    run_id,
                    "running",
                    (
                        f"Processing creator={creator.creator_name} "
                        f"feed={feed.feed_url} (creator index {creator_index + 1}/{len(creators)})"
                    ),
                )

                leads = self._safe_fetch_leads(feed.feed_url)
                if not leads:
                    metrics["feeds_without_leads"] += 1
                    self._save_cursor(creator_index, feed_index + 1, 0)
                    continue

                metrics["feeds_with_leads"] += 1

                for lead_index in range(lead_start, len(leads)):
                    lead = leads[lead_index]
                    self._save_cursor(creator_index, feed_index, lead_index)
                    metrics["leads_processed"] += 1
                    result = self._process_single_lead(run_id, creator, feed.feed_url, lead)
                    metrics[result] = metrics.get(result, 0) + 1

                self._save_cursor(creator_index, feed_index + 1, 0)

            self._save_cursor(creator_index + 1, 0, 0)

    def _process_single_lead(self, run_id: str, creator: Creator, feed_url: str, lead: Lead) -> str:
        logger.info(
            "━━━━━━━━━━ PROCESSING LEAD ━━━━━━━━━━"
        )
        logger.info(
            "Creator: %s (niche=%s) | Brand: %s (niche=%s) | Link: %s",
            creator.creator_name, creator.creator_niche,
            lead.company_name or "UNKNOWN", lead.brand_niche or "UNKNOWN",
            lead.link[:100]
        )

        if not creator.creator_niche:
            logger.warning("❌ Creator niche missing for creator=%s", creator.creator_name)
            return "creator_niche_missing"

        match_result = self._matcher.match(creator_niche=creator.creator_niche, lead=lead)
        if not match_result.matched or match_result.confidence < self._settings.niche_match_min_confidence:
            logger.info(
                "❌ NICHE MISMATCH: creator_niche='%s' brand_niche='%s' confidence=%.2f threshold=%.2f rationale=%s",
                creator.creator_niche,
                lead.brand_niche or "UNKNOWN",
                match_result.confidence,
                self._settings.niche_match_min_confidence,
                match_result.rationale,
            )
            return "niche_mismatch"

        logger.info(
            "✓ NICHE MATCH: creator_niche='%s' brand_niche='%s' confidence=%.2f rationale=%s",
            creator.creator_niche,
            lead.brand_niche or "UNKNOWN",
            match_result.confidence,
            match_result.rationale,
        )

        roles = [
            # CMO & Leadership
            "CMO", "Chief Marketing Officer",
            "Vice President of Marketing", "VP Marketing",
            "Head of Marketing",
            "Marketing Director",
            "Senior Marketing Manager",
            "Marketing Manager",

            # Digital & Content
            "Digital Marketing Manager",
            "Content Marketing Manager",
            "Social Media Manager",
            "Social Media Director",
            "Community Manager",

            # Partnerships & Influencer
            "Influencer Marketing Manager",
            "Partnerships Manager",
            "Business Development Manager",
            "Strategic Partnerships Director",
            "Channel Partner Manager",
            "Brand Partnerships Manager",

            # Brand & Communications
            "Brand Manager",
            "Communications Manager",
            "Public Relations Manager",
            "PR Manager",
            "Brand Director",

            # Growth & Performance
            "Growth Manager",
            "Marketing Manager",
            "Performance Marketing Manager",
            "Demand Generation Manager",
        ]

        # Step 1: Try to find contact by company name if available
        contact = None
        if lead.company_name:
            company_candidates = self._company_search_candidates(lead.company_name)
            for candidate in company_candidates:
                logger.info(
                    "📍 Step 1: Searching Apollo | Company: %s | Roles: %s",
                    candidate,
                    roles,
                )
                try:
                    contact = self._apollo.find_first_valid_contact_by_company(company_name=candidate, roles=roles)
                except Exception as exc:
                    logger.warning("Apollo company search failed for company=%s: %s", candidate, exc)
                    continue

                if contact:
                    logger.info(
                        "✓ Found brand contact: %s <%s> (%s) at %s",
                        contact.full_name,
                        contact.email,
                        contact.title,
                        candidate,
                    )
                    break

            if not contact:
                logger.info("❌ No valid contact found at company variants=%s", company_candidates)
        else:
            logger.info("⚠ No company name extracted, skipping company search")

        # Step 2: Fallback to keyword search if no company contact found
        if not contact:
            keyword = f"{lead.brand_niche or creator.creator_niche} {lead.company_name or 'brand'}".strip()
            logger.info("📍 Step 2: Fallback - Keyword search: '%s'", keyword)
            try:
                contact = self._apollo.find_first_valid_contact(keyword=keyword, roles=roles)
            except Exception as exc:
                logger.warning("Apollo keyword search failed for keyword='%s': %s", keyword, exc)
                contact = None
            if contact:
                logger.info(
                    "✓ Found contact via keyword: %s <%s> (%s)",
                    contact.full_name, contact.email, contact.title
                )
            else:
                logger.info("❌ No contact found via keyword search")

        # Step 3: Validate email
        if not contact or not contact.email:
            logger.info("❌ No valid contact with email found")
            return "no_valid_contact"

        logger.info("✓ Valid email: %s (status=%s)", contact.email, contact.email_status)

        # Step 4: Check for duplicates
        if self._state.already_sent(creator.creator_name, contact.email):
            logger.info("⚠ DUPLICATE: Already sent to %s for creator %s", contact.email, creator.creator_name)
            return "duplicate_skipped"

        # Step 5: Draft and approve email
        logger.info("📧 Drafting email for %s", contact.email)
        draft = self._email.draft_email(creator=creator, lead=lead, contact=contact)
        approval_result = self._approver.review_until_approved(
            draft=draft,
            context=ApprovalContext(creator=creator, lead=lead, contact=contact),
            email_generator=self._email,
        )

        if not approval_result:
            logger.info("❌ Email not approved")
            return "not_approved"

        approved_draft, account_index = approval_result
        logger.info("✓ Email approved")

        # Step 6: Send email (using selected account or default)
        selected_account = self._get_account(account_index)
        sender_label = selected_account.from_email if selected_account else self._settings.smtp_from_email
        logger.info("📨 SENDING EMAIL | To: %s at %s | From: %s | Creator: %s | Brand Niche: %s",
                   contact.email, contact.organization_name, sender_label,
                   creator.creator_name, lead.brand_niche)

        if selected_account:
            send_result = self._sender.send_with_account(
                to_email=contact.email, draft=approved_draft, account=selected_account,
            )
        else:
            send_result = self._sender.send(to_email=contact.email, draft=approved_draft)

        if not send_result.success:
            logger.error("❌ SMTP send failed for %s: %s", contact.email, send_result.error)
            return "send_failed"

        # Step 7: Mark as sent
        self._state.mark_sent(
            creator_name=creator.creator_name,
            lead_email=contact.email,
            feed_url=feed_url,
            lead_link=lead.link,
            run_id=run_id,
            message_id=send_result.message_id,
        )

        # Step 8: Schedule 7-day follow-up
        sender_account_json = None
        if selected_account:
            sender_account_json = json.dumps({
                "label": selected_account.label,
                "host": selected_account.host,
                "port": selected_account.port,
                "username": selected_account.username,
                "password": selected_account.password,
                "from_email": selected_account.from_email,
                "use_tls": selected_account.use_tls,
            })
        self._state.mark_follow_up_pending(
            creator_name=creator.creator_name,
            lead_email=contact.email,
            original_subject=approved_draft.subject,
            original_body=approved_draft.body,
            follow_up_days=7,
            sender_account_json=sender_account_json,
            run_id=run_id,
        )

        logger.info(
            "✓✓✓ SUCCESS ✓✓✓ | Sent to %s <%s> at %s | Creator: %s (niche=%s) | Brand: %s (niche=%s) | Follow-up in 7 days",
            contact.full_name, contact.email, contact.organization_name,
            creator.creator_name, creator.creator_niche,
            lead.company_name or "UNKNOWN", lead.brand_niche
        )
        return "sent"

    @staticmethod
    def _new_metrics() -> Dict[str, int]:
        return {
            "feeds_with_leads": 0,
            "feeds_without_leads": 0,
            "leads_processed": 0,
            "creator_niche_missing": 0,
            "niche_mismatch": 0,
            "no_valid_contact": 0,
            "duplicate_skipped": 0,
            "not_approved": 0,
            "send_failed": 0,
            "sent": 0,
        }

    @staticmethod
    def _metrics_summary(metrics: Dict[str, int]) -> str:
        return (
            ""
            f"feeds_with_leads={metrics.get('feeds_with_leads', 0)}, "
            f"feeds_without_leads={metrics.get('feeds_without_leads', 0)}, "
            f"leads_processed={metrics.get('leads_processed', 0)}, "
            f"niche_mismatch={metrics.get('niche_mismatch', 0)}, "
            f"no_valid_contact={metrics.get('no_valid_contact', 0)}, "
            f"duplicate_skipped={metrics.get('duplicate_skipped', 0)}, "
            f"not_approved={metrics.get('not_approved', 0)}, "
            f"send_failed={metrics.get('send_failed', 0)}, "
            f"sent={metrics.get('sent', 0)}"
        )

    @staticmethod
    def _company_search_candidates(company_name: str) -> List[str]:
        normalized = re.sub(r"\s+", " ", (company_name or "").strip())
        if not normalized:
            return []

        stripped_suffixes = re.sub(
            r"\b(incorporated|inc|llc|ltd|limited|plc|corp|corporation|company|co|gmbh|sa|s\.a\.|bv)\b\.?",
            " ",
            normalized,
            flags=re.IGNORECASE,
        )
        stripped_suffixes = re.sub(r"[\.,]", " ", stripped_suffixes)
        stripped_suffixes = re.sub(r"\s+", " ", stripped_suffixes).strip()

        candidates: List[str] = []
        for value in [normalized, stripped_suffixes]:
            if not value:
                continue
            if value.lower() not in {c.lower() for c in candidates}:
                candidates.append(value)

        return candidates

    def _save_cursor(self, creator_index: int, feed_index: int, lead_index: int) -> None:
        self._state.set_json(
            "cursor",
            {
                "creator_index": creator_index,
                "feed_index": feed_index,
                "lead_index": lead_index,
            },
        )

    def _safe_fetch_leads(self, feed_url: str) -> List[Lead]:
        try:
            return self._rss.fetch_and_parse(feed_url)
        except Exception as exc:
            logger.warning("Failed to parse RSS feed %s: %s", feed_url, exc)
            return []

    @property
    def state_store(self) -> StateStore:
        return self._state

    def _get_account(self, account_index: Optional[int]) -> Optional[SMTPAccount]:
        if account_index is not None and 0 <= account_index < len(self._smtp_accounts):
            return self._smtp_accounts[account_index]
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
