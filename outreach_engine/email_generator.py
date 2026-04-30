from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from openai import OpenAI

from .models import ApolloContact, Creator, EmailDraft, Lead


logger = logging.getLogger(__name__)


class EmailGenerator:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def draft_email(self, creator: Creator, lead: Lead, contact: ApolloContact) -> EmailDraft:
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
            return EmailDraft(subject=subject, body=body)
        except Exception as exc:
            logger.warning("Email generation fallback triggered due to error: %s", exc)
            if current_draft:
                return current_draft
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
            return EmailDraft(subject=subject, body=body)

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

    @staticmethod
    def preview_payload(draft: EmailDraft) -> Dict[str, str]:
        return {"subject": draft.subject, "body": draft.body}
