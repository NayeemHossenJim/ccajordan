from __future__ import annotations

import json
import logging
import re
from typing import Any, List

from openai import OpenAI


logger = logging.getLogger(__name__)


class BrandFinder:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def find_brands(self, niche: str, count: int = 10, location: str = "") -> List[dict]:
        """Find top brands in a niche. Returns list of {name, website, description}."""
        location_text = f" based in or focused on the '{location}' market" if location else ""
        prompt = (
            f"List the top {count} well-known brands/companies in the '{niche}' niche{location_text}. "
            "Return only a JSON array of objects with keys: name, website, description. "
            "The description should be one sentence. Only include real, established brands. "
            "Do not invent fictional brands.\n\n"
            f"Niche: {niche}\n"
            f"Number of brands: {count}\n"
        )
        if location:
            prompt += f"Region/Market: {location}\n"

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000,
            )
            text = str(response.choices[0].message.content or "").strip()
            brands = self._parse_brands(text)
            logger.info("Brand finder returned %d brands for niche='%s'", len(brands), niche)
            return brands[:count]
        except Exception as exc:
            logger.error("Brand finder failed for niche='%s': %s", niche, exc)
            return []

    def _parse_brands(self, text: str) -> List[dict]:
        cleaned = self._clean_text(text)
        if not cleaned:
            return []

        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            candidate = self._extract_json_array(cleaned)
            if not candidate:
                return []
            try:
                result = json.loads(candidate)
            except json.JSONDecodeError:
                return []

        if not isinstance(result, list):
            return []

        brands: List[dict] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            brands.append({
                "name": name,
                "website": str(item.get("website", "N/A")).strip(),
                "description": str(item.get("description", "")).strip(),
            })
        return brands

    @staticmethod
    def _clean_text(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _extract_json_array(text: str) -> str | None:
        match = re.search(r"\[[\s\S]*\]", text)
        return match.group(0) if match else None
