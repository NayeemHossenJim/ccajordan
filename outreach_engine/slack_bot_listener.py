from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .apollo_client import ApolloClient
from .brand_finder import BrandFinder
from .config import Settings
from .state_store import StateStore

logger = logging.getLogger(__name__)

# Keywords that hint at a search/lead-finding intent
_SEARCH_TRIGGERS = {"find", "search", "discover", "lookup", "look", "get", "who"}


class SlackBotListener:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = WebClient(token=settings.slack_bot_token)
        self._channel_id = settings.slack_approval_channel_id
        
        self._state = StateStore(settings.state_db_path)
        self._brand_finder = BrandFinder(
            api_key=settings.openai_api_key, 
            model=settings.openai_model
        )
        self._openai = OpenAI(api_key=settings.openai_api_key)
        self._openai_model = settings.openai_model
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
        
        # We process messages newer than our last seen timestamp
        self._last_ts_key = "last_slack_msg_ts"
        self._bot_user_id: Optional[str] = None
        try:
            auth = self._client.auth_test()
            self._bot_user_id = str(auth.get("user_id") or "").strip() or None
        except Exception as exc:
            logger.warning("Slack auth_test failed during listener init: %s", exc)

    def _get_last_ts(self) -> float:
        data = self._state.get_json(self._last_ts_key)
        if data and "ts" in data:
            return float(data["ts"])
        return time.time()  # Start from now if no history

    def _set_last_ts(self, ts: float) -> None:
        self._state.set_json(self._last_ts_key, {"ts": ts})

    def run_once(self) -> None:
        """Polls Slack channel for new requested commands and processes them."""
        last_ts = self._get_last_ts()
        
        try:
            # Poll conversations history for messages newer than last_ts
            response = self._client.conversations_history(
                channel=self._channel_id,
                oldest=str(last_ts),
                limit=10,
            )
            messages = response.get("messages", [])
        except SlackApiError as e:
            logger.error("Error fetching conversations_history: %s", e)
            return

        new_max_ts = last_ts

        for msg in messages:
            ts_str = str(msg.get("ts") or "0")
            ts_float = float(ts_str) if ts_str else 0.0
            
            if ts_float > new_max_ts:
                new_max_ts = ts_float

            if not self._is_human_reviewer_message(msg):
                continue
            
            text = str(msg.get("text") or "").strip()
            self._process_message(text, msg)

        # Update cursor
        if new_max_ts > last_ts:
            self._set_last_ts(new_max_ts)

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

    def _process_message(self, text: str, msg: dict) -> None:
        """Parse incoming messages looking for trigger commands."""
        # Detect "replied <email>" to cancel pending follow-ups
        replied_match = re.search(r'(?:mark\s+)?replied\s+([\w.+-]+@[\w.-]+)', text, re.IGNORECASE)
        if replied_match:
            email = replied_match.group(1).strip()
            self._handle_mark_replied(email, msg.get("ts"))
            return

        # Detect brand search: "find brands in <niche>", "search <niche> brands", etc.
        # Pattern 1: "find/search brands in/for <niche>"
        match = re.search(
            r'(?:find|search|discover|lookup|look up)\s+(?:a\s+)?brands?\s+(?:in|for)\s+([\w\s]+)',
            text, re.IGNORECASE,
        )
        if not match:
            # Pattern 2: "find/search <niche> brands" (must end with brand/brands)
            match = re.search(
                r'(?:find|search|discover|lookup|look up)\s+([\w\s]+?)\s+brands?\s*$',
                text, re.IGNORECASE,
            )
        if match:
            niche = match.group(1).strip()
            msg_ts = msg.get("ts")
            self._handle_find_brand_command(niche, msg_ts)
            return

        # Natural language fallback: if message contains search-intent keywords,
        # use OpenAI to understand what the user wants
        words = set(text.lower().split())
        if words & _SEARCH_TRIGGERS:
            self._handle_natural_search(text, msg.get("ts"))

    def _handle_mark_replied(self, email: str, thread_ts: str) -> None:
        """Mark a lead email as replied, cancelling pending follow-ups."""
        logger.info("SlackBotListener: User marked '%s' as replied", email)
        count = self._state.mark_replied(email)
        if count:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=f"✅ Marked {count} pending follow-up(s) as replied for `{email}`. No follow-up will be sent.",
            )
        else:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=f"ℹ️ No pending follow-ups found for `{email}`.",
            )

    def _handle_natural_search(self, text: str, thread_ts: str) -> None:
        """Use OpenAI to parse a natural language lead/brand search request."""
        logger.info("SlackBotListener: Natural language search: '%s'", text)

        # 1. Acknowledge
        self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text=f"🔍 Understanding your request: _{text}_\nOne moment..."
        )

        # 2. Parse intent with OpenAI
        parsed = self._parse_search_intent(text)
        if not parsed:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text="❌ I couldn't understand that as a search request. Try something like:\n"
                     "• `find kitchen knife brands in UK`\n"
                     "• `search for fitness influencer marketing contacts`\n"
                     "• `find a brand collaboration person for badminton`"
            )
            return

        niche = parsed.get("niche", "")
        location = parsed.get("location", "")
        company = parsed.get("company", "")

        if not niche and not company:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text="❌ I need at least a niche or company name. Try: `find kitchen knife brands in UK`"
            )
            return

        # 3. If a specific company was mentioned, search Apollo directly
        if company:
            self._handle_company_search(company, niche, location, thread_ts)
            return

        # 4. Otherwise, find brands in the niche then search Apollo for contacts
        search_label = f"'{niche}'"
        if location:
            search_label += f" in {location}"

        self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text=f"🔍 Searching for top brands in {search_label} and looking up decision-makers..."
        )

        brands = self._brand_finder.find_brands(niche=niche, count=3, location=location)
        if not brands:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=f"❌ No brands found for {search_label}."
            )
            return

        self._post_brand_results_with_contacts(brands, niche, location, thread_ts)

    def _handle_company_search(self, company: str, niche: str, location: str, thread_ts: str) -> None:
        """Search Apollo for contacts at a specific company."""
        self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text=f"🔍 Searching for marketing/partnership contacts at *{company}*..."
        )

        roles = [
            "CMO", "Chief Marketing Officer", "Vice President of Marketing", "VP Marketing",
            "Head of Marketing", "Marketing Director", "Senior Marketing Manager", "Marketing Manager",
            "Influencer Marketing Manager", "Partnerships Manager", "Brand Partnerships Manager",
            "Brand Manager", "Communications Manager", "Public Relations Manager", "PR Manager"
        ]

        contact = self._apollo.find_first_valid_contact_by_company(company, roles=roles)
        if contact:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=(
                    f"✅ *Found contact at {company}:*\n"
                    f"• Name: {contact.full_name}\n"
                    f"• Title: {contact.title}\n"
                    f"• Email: `{contact.email}`\n"
                    f"• Organization: {contact.organization_name}"
                ),
            )
        else:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=f"⚠️ No valid email contacts found at *{company}* via Apollo."
            )

    def _post_brand_results_with_contacts(self, brands: list, niche: str, location: str, thread_ts: str) -> None:
        """Post brand results with Apollo contact lookups."""
        roles = [
            "CMO", "Chief Marketing Officer", "Vice President of Marketing", "VP Marketing",
            "Head of Marketing", "Marketing Director", "Senior Marketing Manager", "Marketing Manager",
            "Influencer Marketing Manager", "Partnerships Manager", "Brand Partnerships Manager",
            "Brand Manager", "Communications Manager", "Public Relations Manager", "PR Manager"
        ]

        label = f"'{niche}'"
        if location:
            label += f" in {location}"

        blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Brands & Contacts for {label}"}}]

        for i, brand in enumerate(brands, 1):
            name = brand.get("name", "Unknown")
            website = brand.get("website", "N/A")
            desc = brand.get("description", "")

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{i}. {name}* - <{website}>\n_{desc}_"}
            })

            contact = self._apollo.find_first_valid_contact_by_company(name, roles=roles)
            if contact:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"✅ *Found Contact:*\n• Name: {contact.full_name}\n• Title: {contact.title}\n• Email: `{contact.email}`"
                    }
                })
            else:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"⚠️ *No valid email contacts found via Apollo for {name}*."}
                })

            blocks.append({"type": "divider"})

        try:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text="Brand research results",
                blocks=blocks
            )
        except SlackApiError as e:
            logger.error("Failed to post brand details to slack: %s", e)

    def _parse_search_intent(self, text: str) -> Optional[dict]:
        """Use OpenAI to extract structured search parameters from natural language."""
        prompt = (
            "Extract search parameters from this user message. Return only JSON with keys:\n"
            '- "niche": the industry/product category (e.g., "kitchen knife", "badminton", "fitness")\n'
            '- "location": the country/region if mentioned (e.g., "UK", "Germany", "USA"), or "" if none\n'
            '- "company": a specific company name if mentioned (e.g., "Nike", "Yonex"), or "" if none\n'
            '- "is_search": true if this is a request to find brands/contacts/leads, false otherwise\n\n'
            f'User message: {text}\n'
        )

        try:
            response = self._openai.chat.completions.create(
                model=self._openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
            )
            raw = str(response.choices[0].message.content or "").strip()
            # Clean markdown code fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
                raw = re.sub(r"\s*```$", "", raw)

            parsed = json.loads(raw)
            if not parsed.get("is_search"):
                return None
            return parsed
        except Exception as exc:
            logger.warning("Failed to parse search intent: %s", exc)
            return None

    def _handle_find_brand_command(self, niche: str, thread_ts: str) -> None:
        logger.info("SlackBotListener: User requested finding a brand in niche='%s'", niche)
        # 1. Acknowledge receipt
        self._client.chat_postMessage(
            channel=self._channel_id,
            thread_ts=thread_ts,
            text=f"🔍 I'm researching top brands in the '{niche}' niche and looking up their key decision-makers. One moment..."
        )
        
        # 2. Find Brands
        brands = self._brand_finder.find_brands(niche=niche, count=2)
        if not brands:
            self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=f"❌ I could not find any brands for the niche '{niche}'."
            )
            return

        # 3. Post with contacts
        self._post_brand_results_with_contacts(brands, niche, "", thread_ts)
