from __future__ import annotations

import html
import logging
import re
from typing import List

import feedparser
import requests
from openai import OpenAI

from .models import Lead


logger = logging.getLogger(__name__)


class RSSParser:
    def __init__(self, timeout_seconds: int = 20, openai_api_key: str | None = None, openai_model: str = "gpt-4o-mini") -> None:
        self._timeout_seconds = timeout_seconds
        self._openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self._openai_model = openai_model
        self._max_article_chars = 6000

    def fetch_and_parse(self, feed_url: str) -> List[Lead]:
        logger.info("Fetching RSS feed from: %s", feed_url)
        response = requests.get(feed_url, timeout=self._timeout_seconds)
        response.raise_for_status()

        parsed = feedparser.parse(response.text)
        leads: List[Lead] = []
        company_extracted_count = 0
        niche_extracted_count = 0

        for entry in parsed.entries:
            title = str(getattr(entry, "title", "")).strip()
            summary = str(getattr(entry, "summary", "")).strip()
            link = str(getattr(entry, "link", "")).strip()
            published = str(getattr(entry, "published", "")).strip() or None

            if not (title or summary or link):
                continue

            article_content = self._fetch_article_content(link) if link else ""
            extraction_source = self._build_extraction_source(title=title, summary=summary, article_content=article_content)

            company_name = self._extract_company_name(title, extraction_source) if self._openai_client else None
            brand_niche = self._extract_brand_niche(title, extraction_source) if self._openai_client else None

            if company_name:
                company_extracted_count += 1
            if brand_niche:
                niche_extracted_count += 1

            leads.append(
                Lead(
                    title=title,
                    summary=summary,
                    link=link,
                    published=published,
                    company_name=company_name,
                    brand_niche=brand_niche,
                )
            )

        logger.info("Parsed %d leads from feed=%s (company=%d, brand_niche=%d)",
                   len(leads), feed_url, company_extracted_count, niche_extracted_count)
        return leads

    def _fetch_article_content(self, link: str) -> str:
        try:
            response = requests.get(link, timeout=self._timeout_seconds)
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Failed to fetch article content from link=%s: %s", link, exc)
            return ""

        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and ("html" not in content_type and "xml" not in content_type and "text" not in content_type):
            logger.debug("Skipping non-text article content at link=%s content_type=%s", link, content_type)
            return ""

        text = self._extract_text_from_markup(response.text)
        if not text:
            logger.debug("No extractable article text at link=%s", link)
            return ""

        return text[: self._max_article_chars]

    @staticmethod
    def _extract_text_from_markup(markup: str) -> str:
        cleaned = re.sub(r"<script[\\s\\S]*?</script>", " ", markup, flags=re.IGNORECASE)
        cleaned = re.sub(r"<style[\\s\\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<!--([\\s\\S]*?)-->", " ", cleaned)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\\s+", " ", cleaned).strip()
        return cleaned

    def _build_extraction_source(self, title: str, summary: str, article_content: str) -> str:
        if article_content:
            return f"Summary: {summary}\n\nArticle: {article_content}".strip()
        return summary or title

    def _extract_company_name(self, title: str, content: str) -> str | None:
        """Extract company name from article title and summary using LLM."""
        if not self._openai_client:
            logger.debug("OpenAI client not configured, skipping company extraction for title=%s", title[:80])
            return None

        try:
            response = self._openai_client.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Extract the company/brand name mentioned in this article.

Title: {title}
Content: {content}

Return ONLY the company name, nothing else. If no company is mentioned, return "NONE".""",
                    }
                ],
                temperature=0,
                max_tokens=50,
            )
            company = response.choices[0].message.content.strip()
            result = company if company != "NONE" else None

            if result:
                logger.debug("Extracted company_name=%s from title=%s", result, title[:80])
            else:
                logger.debug("No company extracted from title=%s", title[:80])

            return result
        except Exception as exc:
            logger.warning("Failed to extract company name from title=%s: %s", title[:80], exc)
            return None

    def _extract_brand_niche(self, title: str, content: str) -> str | None:
        """Extract the niche/industry from article content using LLM."""
        if not self._openai_client:
            return None

        try:
            response = self._openai_client.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Extract the business niche/industry mentioned in this article.

Title: {title}
Content: {content}

Examples: food, beauty, fashion, technology, healthcare, sports, finance, travel, etc.

Return ONLY the niche word, nothing else. If no clear niche is mentioned, return "NONE".""",
                    }
                ],
                temperature=0,
                max_tokens=30,
            )
            niche = response.choices[0].message.content.strip().lower()
            result = niche if niche != "none" else None

            if result:
                logger.debug("Extracted brand_niche=%s from title=%s", result, title[:80])
            else:
                logger.debug("No brand niche extracted from title=%s", title[:80])

            return result
        except Exception as exc:
            logger.warning("Failed to extract brand niche from title=%s: %s", title[:80], exc)
            return None
