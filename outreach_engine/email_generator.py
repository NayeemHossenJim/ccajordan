from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, Optional

from openai import OpenAI

from .models import ApolloContact, Creator, CreatorEmailTemplate, EmailDraft, Lead


logger = logging.getLogger(__name__)


class EmailGenerator:
    _MATCH_STOPWORDS = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "your",
        "our",
        "you",
        "their",
        "into",
        "campaign",
        "content",
        "partnership",
        "collaboration",
        "viral",
        "podcast",
        "idea",
        "official",
        "reaching",
        "table",
        "company",
        "creator",
        "tod",
        "jims",
        "jim",
        "hungry",
    }

    def __init__(self, api_key: str, model: str, default_sender_first_name: str | None = None) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._default_sender_first_name = (default_sender_first_name or "Team").strip() or "Team"

    def draft_email(
        self,
        creator: Creator,
        lead: Lead,
        contact: ApolloContact,
        templates: Optional[Iterable[CreatorEmailTemplate]] = None,
    ) -> EmailDraft:
        template_draft = self._draft_from_template(creator=creator, lead=lead, contact=contact, templates=templates)
        if template_draft:
            return template_draft

        prompt = (
            "Write a concise personalized outreach email. Return only JSON with keys subject and body. "
            "Tone: credible and creator-specific. Length: 120-170 words.\n\n"
            f"Creator name: {creator.creator_name}\n"
            f"Creator niche: {creator.creator_niche}\n"
            f"Creator platform: {creator.creator_platform}\n"
            f"Creator followers: {creator.creator_followers}\n"
            f"Creator Instagram: {creator.instagram_profile}\n"
            f"Creator TikTok: {creator.tiktok_profile}\n"
            f"Creator YouTube: {creator.youtube_profile}\n"
            f"Target person: {contact.full_name}\n"
            f"Target title: {contact.title}\n"
            f"Target organization: {contact.organization_name}\n"
            f"Lead context title: {lead.title}\n"
            f"Lead context summary: {lead.summary}\n"
            f"Lead source: {lead.link}\n"
        )
        return self._generate_or_fallback(prompt, creator, contact)

    def revise_email(self, current_draft: EmailDraft, instructions: str, strict: bool = True) -> EmailDraft:
        strict_line = "Apply every instruction exactly unless illegal or impossible." if strict else "Apply instructions."
        prompt = (
            "Revise the existing outreach email and return only JSON with keys subject and body.\n"
            f"Rule: {strict_line}\n\n"
            f"Current subject: {current_draft.subject}\n"
            f"Current body: {current_draft.body}\n"
            f"Reviewer instructions: {instructions}\n"
        )
        return self._generate_or_fallback(prompt, None, None, current_draft=current_draft)

    def draft_follow_up(self, original_subject: str, original_body: str, lead_email: str) -> EmailDraft:
        prompt = (
            "Write a short follow-up email referencing a previous outreach. "
            "Return only JSON with keys subject and body. "
            "Tone: polite, brief, not pushy. Length: 50-80 words.\n\n"
            f"Original subject: {original_subject}\n"
            f"Original body: {original_body}\n"
            f"Recipient email: {lead_email}\n"
        )
        fallback = EmailDraft(
            subject=f"Re: {original_subject}",
            body=(
                "Hi,\n\n"
                "I wanted to follow up on my previous email regarding a potential collaboration. "
                "I understand you're busy, but I'd love to explore this opportunity if it's still of interest.\n\n"
                "Looking forward to hearing from you.\n\n"
                "Best regards"
            ),
        )
        return self._generate_or_fallback(prompt, None, None, current_draft=fallback)

    def _generate_or_fallback(
        self,
        prompt: str,
        creator: Creator | None,
        contact: ApolloContact | None,
        current_draft: EmailDraft | None = None,
    ) -> EmailDraft:
        try:
            response = self._client.responses.create(
                model=self._model,
                input=prompt,
                temperature=0.4,
            )
            payload = self._parse_model_payload(response)
            subject = str(payload.get("subject", "")).strip()
            body = str(payload.get("body", "")).strip()
            if not subject or not body:
                raise ValueError("Model returned empty subject/body")
            return self._finalize_draft(EmailDraft(subject=subject, body=body))
        except Exception as exc:
            logger.warning("Email generation fallback triggered due to error: %s", exc)
            if current_draft:
                return self._finalize_draft(current_draft)
            creator_name = creator.creator_name if creator else "Creator"
            org_name = contact.organization_name if contact else "your team"
            contact_name = contact.full_name if contact else "there"
            subject = f"Collaboration idea for {org_name}"
            body = (
                f"Hi {contact_name},\n\n"
                f"I am reaching out on behalf of {creator_name}. We create high-performing content in this niche "
                "and saw a strong fit with your current marketing direction. "
                "If useful, I can share a short concept deck and campaign options tailored to your goals.\n\n"
                "Best regards,\n"
                f"{creator_name} Team"
            )
            return self._finalize_draft(EmailDraft(subject=subject, body=body))

    def _parse_model_payload(self, response: Any) -> dict:
        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            text = self._extract_text_from_response_dump(response)

        cleaned = self._clean_text(text)
        if not cleaned:
            raise ValueError("Model returned empty text")

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            candidate = self._extract_json_object(cleaned)
            if not candidate:
                raise
            return json.loads(candidate)

    @staticmethod
    def _extract_text_from_response_dump(response: Any) -> str:
        try:
            dump = response.model_dump()
        except Exception:
            return ""

        chunks: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                chunks.append(value)
                return
            if isinstance(value, dict):
                for nested in value.values():
                    walk(nested)
                return
            if isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(dump)
        return "\n".join(chunks).strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        match = re.search(r"\{[\s\S]*\}", text)
        return match.group(0) if match else None

    def _draft_from_template(
        self,
        creator: Creator,
        lead: Lead,
        contact: ApolloContact,
        templates: Optional[Iterable[CreatorEmailTemplate]],
    ) -> EmailDraft | None:
        if not templates:
            return None

        template_list = list(templates)
        if not template_list:
            return None

        selected = self._select_template(lead=lead, templates=template_list)
        if not selected:
            return None

        subject = self._render_template_text(selected.subject, creator=creator, lead=lead, contact=contact)
        body = self._render_template_text(selected.body, creator=creator, lead=lead, contact=contact)
        if not subject or not body:
            return None
        return self._finalize_draft(EmailDraft(subject=subject, body=body))

    def _select_template(self, lead: Lead, templates: list[CreatorEmailTemplate]) -> CreatorEmailTemplate | None:
        if not templates:
            return None

        default_template = templates[0]
        lead_tokens = self._tokenize_for_match(
            " ".join(
                value
                for value in [
                    lead.title or "",
                    lead.summary or "",
                    lead.company_name or "",
                    lead.brand_niche or "",
                ]
                if value
            )
        )
        if not lead_tokens:
            return default_template

        best_template: CreatorEmailTemplate | None = None
        best_score = 0
        for template in templates:
            template_tokens = self._tokenize_for_match(template.subject)
            if not template_tokens:
                continue
            score = len(lead_tokens.intersection(template_tokens))
            if score > best_score:
                best_score = score
                best_template = template

        return best_template if best_score > 0 else default_template

    def _render_template_text(self, text: str, creator: Creator, lead: Lead, contact: ApolloContact) -> str:
        company_name = (contact.organization_name or lead.company_name or "your team").strip()
        first_name = self._extract_first_name(contact.full_name) or "there"

        values = {
            "company": company_name,
            "account_name": company_name,
            "first_name": first_name,
            "sender_first_name": self._default_sender_first_name,
            "creator_name": creator.creator_name or "",
            "creator_niche": creator.creator_niche or "",
        }

        def replace(match: re.Match[str]) -> str:
            raw_key = match.group(1)
            normalized = self._normalize_placeholder_key(raw_key)
            if normalized in {"account", "account_name"}:
                normalized = "account_name"
            if normalized in {"first", "firstname"}:
                normalized = "first_name"
            if normalized in {"sender", "sender_name", "senderfirstname"}:
                normalized = "sender_first_name"
            return values.get(normalized, "")

        rendered = re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace, text)
        rendered = re.sub(r"[ \t]+", " ", rendered)
        return rendered.strip()

    @classmethod
    def _tokenize_for_match(cls, text: str) -> set[str]:
        words = re.findall(r"[a-zA-Z0-9']+", (text or "").casefold())
        tokens = {word for word in words if len(word) >= 4 and word not in cls._MATCH_STOPWORDS}
        return tokens

    @staticmethod
    def _normalize_placeholder_key(raw_key: str) -> str:
        normalized = (raw_key or "").replace("\u00A0", " ")
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized.casefold()).strip("_")
        return normalized

    @staticmethod
    def _extract_first_name(full_name: str | None) -> str:
        if not full_name:
            return ""
        parts = re.findall(r"[A-Za-z]+", full_name)
        return parts[0].capitalize() if parts else ""

    @staticmethod
    def preview_payload(draft: EmailDraft) -> Dict[str, str]:
        return {"subject": draft.subject, "body": draft.body}

    def _finalize_draft(self, draft: EmailDraft) -> EmailDraft:
        subject = self._normalize_subject(draft.subject)
        body = self._normalize_body(draft.body)
        return EmailDraft(subject=subject, body=body)

    @staticmethod
    def _normalize_subject(subject: str) -> str:
        return re.sub(r"\s+", " ", (subject or "")).strip()

    def _normalize_body(self, body: str) -> str:
        normalized = (body or "").replace("\u00A0", " ")
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", " ")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
        compact_lines = self._collapse_blank_lines(lines)
        normalized = "\n".join(compact_lines).strip()
        if not normalized:
            return ""
        return self._ensure_email_structure(normalized)

    @staticmethod
    def _collapse_blank_lines(lines: list[str]) -> list[str]:
        collapsed: list[str] = []
        previous_blank = False
        for line in lines:
            is_blank = not line
            if is_blank and previous_blank:
                continue
            collapsed.append(line)
            previous_blank = is_blank
        return collapsed

    @staticmethod
    def _ensure_email_structure(body: str) -> str:
        text = body

        greeting_match = re.match(r"^(hi|hello|dear)\b[^,\n]{0,60},?", text, flags=re.IGNORECASE)
        if greeting_match:
            greeting = greeting_match.group(0).strip()
            remainder = text[greeting_match.end():].strip()
            if remainder and not remainder.startswith("\n"):
                text = f"{greeting}\n\n{remainder}"

        signoff_match = re.search(
            r"\s*(best regards|kind regards|warm regards|regards|sincerely|thanks|thank you|best),?\s+([A-Za-z][A-Za-z .'\-]{0,60})$",
            text,
            flags=re.IGNORECASE,
        )
        if signoff_match:
            content = text[: signoff_match.start(1)].rstrip()
            signoff = signoff_match.group(1).strip().capitalize()
            name = signoff_match.group(2).strip()
            text = f"{content}\n\n{signoff},\n{name}" if content else f"{signoff},\n{name}"

        if "\n\n" not in text and len(text.split()) >= 75:
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
            if len(sentences) >= 3:
                split_index = max(1, len(sentences) // 2)
                text = f"{' '.join(sentences[:split_index])}\n\n{' '.join(sentences[split_index:])}"

        return text.strip()
