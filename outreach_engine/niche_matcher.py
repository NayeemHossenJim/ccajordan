from __future__ import annotations

import json
import logging
import re
from typing import Any, Set

from openai import OpenAI

from .models import Lead, MatchResult


logger = logging.getLogger(__name__)


class NicheMatcher:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def match(self, creator_niche: str, lead: Lead) -> MatchResult:
        prompt = self._build_prompt(creator_niche=creator_niche, lead=lead)
        try:
            response = self._client.responses.create(
                model=self._model,
                input=prompt,
                temperature=0,
            )
            payload = self._parse_model_payload(response)
            matched = self._to_bool(payload.get("matched", payload.get("match", False)))
            confidence = self._to_confidence(payload.get("confidence", 0.0))
            rationale = str(payload.get("rationale", "")).strip() or "No rationale provided"
            return MatchResult(matched=matched, confidence=confidence, rationale=rationale)
        except Exception as exc:
            logger.warning("LLM match fallback triggered due to error: %s", exc)
            return self._fallback_match(creator_niche, lead)

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
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "matched"}:
                return True
            if normalized in {"false", "no", "0", "not_matched", "unmatched"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return False

    @staticmethod
    def _to_confidence(value: Any) -> float:
        try:
            numeric = float(value)
        except Exception:
            return 0.0
        if numeric > 1.0:
            numeric = numeric / 100.0
        if numeric < 0.0:
            return 0.0
        if numeric > 1.0:
            return 1.0
        return numeric

    def _build_prompt(self, creator_niche: str, lead: Lead) -> str:
        return (
            "You are a strict niche matching engine. Return only JSON with keys: "
            "matched(boolean), confidence(0-1), rationale(string). "
            "Use high precision; avoid false positives.\n\n"
            f"Creator niche: {creator_niche}\n"
            f"Extracted brand niche: {lead.brand_niche or 'UNKNOWN'}\n"
            f"Lead title: {lead.title}\n"
            f"Lead summary: {lead.summary}\n"
            f"Lead link: {lead.link}\n"
        )

    def _fallback_match(self, creator_niche: str, lead: Lead) -> MatchResult:
        niche_tokens = self._tokens(creator_niche)
        lead_tokens = self._tokens(f"{lead.title} {lead.summary}")
        if not niche_tokens:
            return MatchResult(matched=False, confidence=0.0, rationale="Creator niche is empty")

        overlap = niche_tokens.intersection(lead_tokens)
        confidence = len(overlap) / max(1, len(niche_tokens))
        matched = confidence >= 0.2
        rationale = f"Fallback lexical overlap tokens: {sorted(overlap)}"
        return MatchResult(matched=matched, confidence=confidence, rationale=rationale)

    @staticmethod
    def _tokens(text: str) -> Set[str]:
        return {token for token in re.findall(r"[a-zA-Z0-9]+", text.lower()) if len(token) > 2}
