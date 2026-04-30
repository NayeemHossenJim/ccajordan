from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set
from urllib.parse import urlparse

import requests

from .models import ApolloContact


logger = logging.getLogger(__name__)


@dataclass
class ApolloRateLimiter:
    requests_per_minute: int
    _last_request_ts: float = 0.0

    def wait(self) -> None:
        if self.requests_per_minute <= 0:
            return
        min_gap = 60.0 / self.requests_per_minute
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_request_ts = time.time()


class ApolloClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        requests_per_minute: int,
        max_retries: int,
        initial_backoff_seconds: int,
        search_max_pages: int,
        search_per_page: int,
        search_contact_email_statuses: Iterable[str],
        allowed_email_statuses: Iterable[str],
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            }
        )
        self._rate_limiter = ApolloRateLimiter(requests_per_minute=requests_per_minute)
        self._max_retries = max_retries
        self._initial_backoff_seconds = initial_backoff_seconds
        self._search_max_pages = max(1, search_max_pages)
        self._search_per_page = max(1, search_per_page)
        self._search_contact_email_statuses: List[str] = self._normalize_search_email_statuses(
            search_contact_email_statuses
        )
        self._allowed_email_statuses: Set[str] = {
            status.strip().lower() for status in allowed_email_statuses if str(status).strip()
        }
        if not self._allowed_email_statuses:
            raise ValueError("allowed_email_statuses cannot be empty")

    def search_decision_makers(
        self,
        keyword: str,
        roles: Iterable[str],
        *,
        max_pages: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> List[ApolloContact]:
        roles_list = list(roles)
        page_limit = max(1, max_pages or self._search_max_pages)
        page_size = max(1, per_page or self._search_per_page)
        contacts: List[ApolloContact] = []

        for page in range(1, page_limit + 1):
            payload = {
                "q_keywords": keyword,
                "person_titles": roles_list,
                "page": page,
                "per_page": page_size,
                "contact_email_status": self._search_contact_email_statuses,
            }
            data = self._post_with_retries("/mixed_people/api_search", payload)
            people = data.get("people", []) if isinstance(data, dict) else []
            logger.info(
                "Apollo keyword search page=%d/%d keyword=%s returned=%d",
                page,
                page_limit,
                keyword,
                len(people),
            )

            for item in people:
                first_name = str(item.get("first_name") or "").strip()
                last_name = str(item.get("last_name") or "").strip()
                name = str(item.get("name") or f"{first_name} {last_name}").strip()
                organization = item.get("organization") or {}
                domain = self._extract_domain(organization)
                has_email = item.get("has_email")
                contacts.append(
                    ApolloContact(
                        full_name=name,
                        title=str(item.get("title") or "").strip(),
                        organization_name=str(organization.get("name") or "").strip(),
                        organization_domain=domain or None,
                        email=self._extract_email_value(item),
                        email_status=str(item.get("email_status") or "").strip().lower()
                        or None,
                        linkedin_url=str(item.get("linkedin_url") or "").strip()
                        or None,
                        apollo_person_id=str(item.get("id") or "").strip() or None,
                        apollo_has_email=bool(has_email) if has_email is not None else None,
                    )
                )

            if len(people) < page_size:
                break

        return contacts

    def search_by_company(
        self,
        company_name: str,
        roles: Iterable[str],
        *,
        max_pages: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> List[ApolloContact]:
        """Search for decision makers at a specific company."""
        roles_list = list(roles)
        page_limit = max(1, max_pages or self._search_max_pages)
        page_size = max(1, per_page or self._search_per_page)
        logger.info(
            "🔍 Apollo Search | Company: %s | Roles: %s", company_name, roles_list
        )
        contacts: List[ApolloContact] = []
        seen: set[tuple[str, str, str]] = set()

        for page in range(1, page_limit + 1):
            payload = {
                "organization_name": company_name,
                "person_titles": roles_list,
                "page": page,
                "per_page": page_size,
                "contact_email_status": self._search_contact_email_statuses,
            }
            data = self._post_with_retries("/mixed_people/api_search", payload)
            people = data.get("people", []) if isinstance(data, dict) else []

            logger.info(
                "Apollo company search page=%d/%d company=%s returned=%d",
                page,
                page_limit,
                company_name,
                len(people),
            )

            for idx, item in enumerate(people):
                first_name = str(item.get("first_name") or "").strip()
                last_name = str(item.get("last_name") or "").strip()
                name = str(item.get("name") or f"{first_name} {last_name}").strip()
                organization = item.get("organization") or {}
                domain = self._extract_domain(organization)

                email = self._extract_email_value(item)
                email_status = (
                    str(item.get("email_status") or "").strip().lower() or None
                )
                has_email = item.get("has_email")
                title = str(item.get("title") or "").strip()
                org_name = str(organization.get("name") or "").strip()
                key = (name.lower(), title.lower(), org_name.lower())
                if key in seen:
                    continue
                seen.add(key)

                logger.debug(
                    "Result page=%d idx=%d | Name: %s | Title: %s | Email: %s | Status: %s",
                    page,
                    idx + 1,
                    name,
                    title,
                    email or "NO_EMAIL",
                    email_status or "NO_STATUS",
                )

                contacts.append(
                    ApolloContact(
                        full_name=name,
                        title=title,
                        organization_name=org_name,
                        organization_domain=domain or None,
                        email=email,
                        email_status=email_status,
                        linkedin_url=str(item.get("linkedin_url") or "").strip()
                        or None,
                        apollo_person_id=str(item.get("id") or "").strip() or None,
                        apollo_has_email=bool(has_email) if has_email is not None else None,
                    )
                )

            if len(people) < page_size:
                break

        logger.info(
            "✓ Search completed | Found %d contacts at company=%s",
            len(contacts),
            company_name,
        )
        return contacts

    def find_first_valid_contact(
        self, keyword: str, roles: Iterable[str]
    ) -> Optional[ApolloContact]:
        candidates = self.search_decision_makers(keyword=keyword, roles=roles)
        enriched = self._bulk_enrich_candidates_by_id(candidates)
        valid_enriched = self.first_valid_email(enriched)
        if valid_enriched:
            return valid_enriched

        for candidate in candidates:
            # Step 1: Find brand-related person ✓
            # Step 2: Check if they already have a valid email from the search
            if self.first_valid_email([candidate]):
                return candidate
            # If not, try to enrich/match their email
            matched = self.match_person_email(candidate)
            if matched and self.first_valid_email([matched]):
                return matched
        return None

    def find_first_valid_contact_by_company(
        self, company_name: str, roles: Iterable[str]
    ) -> Optional[ApolloContact]:
        """Find first valid contact at a specific company."""
        roles_list = list(roles)
        logger.info(
            "Finding first valid contact at company=%s with %d roles",
            company_name,
            len(roles_list),
        )
        candidates = self.search_by_company(company_name=company_name, roles=roles_list)

        if not candidates:
            logger.warning(
                "❌ Search returned 0 candidates at company=%s", company_name
            )
            return None

        logger.info("Evaluating %d candidates from search...", len(candidates))

        enriched = self._bulk_enrich_candidates_by_id(candidates)
        valid_enriched = self.first_valid_email(enriched)
        if valid_enriched:
            logger.info(
                "✓ Found valid contact after bulk enrichment: %s <%s> at %s",
                valid_enriched.full_name,
                valid_enriched.email,
                company_name,
            )
            return valid_enriched

        for i, candidate in enumerate(candidates):
            logger.debug(
                "Candidate %d/%d: %s | Title: %s | Email: %s | Status: %s",
                i + 1,
                len(candidates),
                candidate.full_name,
                candidate.title,
                candidate.email or "NO_EMAIL",
                candidate.email_status or "NO_STATUS",
            )

            if self.first_valid_email([candidate]):
                logger.info(
                    "✓ Found valid contact from initial search: %s <%s> at %s",
                    candidate.full_name,
                    candidate.email,
                    company_name,
                )
                return candidate

            logger.debug(
                "Initial email invalid, attempting enrichment for %s...",
                candidate.full_name,
            )
            matched = self.match_person_email(candidate)

            if matched:
                logger.debug(
                    "Enrichment returned: %s | New Email: %s | Status: %s",
                    matched.full_name,
                    matched.email or "NO_EMAIL",
                    matched.email_status or "NO_STATUS",
                )

                if self.first_valid_email([matched]):
                    logger.info(
                        "✓ Found valid contact after email enrichment: %s <%s> at %s",
                        matched.full_name,
                        matched.email,
                        company_name,
                    )
                    return matched
            else:
                logger.debug("Email enrichment failed for %s", candidate.full_name)

        logger.warning(
            "❌ No valid contact found at company=%s after checking %d candidates",
            company_name,
            len(candidates),
        )
        return None

    def _bulk_enrich_candidates_by_id(
        self, candidates: Iterable[ApolloContact], batch_size: int = 10
    ) -> List[ApolloContact]:
        with_ids = [candidate for candidate in candidates if candidate.apollo_person_id]
        if not with_ids:
            return []

        enriched: List[ApolloContact] = []
        for start in range(0, len(with_ids), batch_size):
            batch = with_ids[start : start + batch_size]
            details = [{"id": candidate.apollo_person_id} for candidate in batch if candidate.apollo_person_id]
            if not details:
                continue

            payload = {
                "details": details,
                "reveal_personal_emails": False,
                "reveal_phone_number": False,
            }

            try:
                data = self._post_with_retries("/people/bulk_match", payload)
            except Exception as exc:
                logger.warning("Apollo people/bulk_match failed: %s", exc)
                continue

            for person in self._extract_bulk_people(data):
                contact = self._person_to_contact(person)
                if contact:
                    enriched.append(contact)

        return enriched

    def match_person_email(self, contact: ApolloContact) -> Optional[ApolloContact]:
        if contact.apollo_person_id:
            try:
                data = self._post_with_retries(
                    "/people/match",
                    {
                        "id": contact.apollo_person_id,
                        "reveal_personal_emails": False,
                        "reveal_phone_number": False,
                    },
                )
                person = self._extract_person(data)
                direct_match = self._person_to_contact(person)
                if direct_match:
                    return direct_match
            except Exception as exc:
                logger.debug(
                    "Apollo people/match by id failed for %s (%s): %s",
                    contact.full_name,
                    contact.apollo_person_id,
                    exc,
                )

        first_name, last_name = self._split_name(contact.full_name)
        domain = contact.organization_domain or ""

        payload_candidates = [
            {
                "first_name": first_name,
                "last_name": last_name,
                "organization_name": contact.organization_name,
                "domain": domain,
                "linkedin_url": contact.linkedin_url,
            },
            {
                "name": contact.full_name,
                "organization_name": contact.organization_name,
                "domain": domain,
                "linkedin_url": contact.linkedin_url,
            },
        ]

        for payload in payload_candidates:
            compact_payload = {k: v for k, v in payload.items() if v}
            if not compact_payload:
                continue

            try:
                data = self._post_with_retries("/people/match", compact_payload)
            except Exception as exc:
                logger.warning(
                    "Apollo people/match failed for %s: %s", contact.full_name, exc
                )
                continue

            person = self._extract_person(data)
            if not person:
                continue

            email = self._extract_email_value(person)
            status = (
                str(person.get("email_status") or person.get("email_status_code") or "")
                .strip()
                .lower()
                or None
            )

            resolved = ApolloContact(
                full_name=str(person.get("name") or contact.full_name).strip(),
                title=str(person.get("title") or contact.title).strip(),
                organization_name=str(
                    (
                        (person.get("organization") or {}).get("name")
                        or contact.organization_name
                    )
                ).strip(),
                organization_domain=domain or None,
                email=email,
                email_status=status,
                linkedin_url=str(
                    person.get("linkedin_url") or contact.linkedin_url or ""
                ).strip()
                or None,
                apollo_person_id=str(person.get("id") or contact.apollo_person_id or "").strip() or None,
                apollo_has_email=bool(person.get("has_email")) if person.get("has_email") is not None else None,
            )
            return resolved

        return None

    def _person_to_contact(self, person: Optional[dict]) -> Optional[ApolloContact]:
        if not isinstance(person, dict):
            return None

        organization = person.get("organization") or {}
        domain = self._extract_domain(organization)
        email = self._extract_email_value(person)
        status = str(person.get("email_status") or person.get("email_status_code") or "").strip().lower() or None
        first_name = str(person.get("first_name") or "").strip()
        last_name = str(person.get("last_name") or "").strip()
        full_name = str(person.get("name") or f"{first_name} {last_name}").strip()

        return ApolloContact(
            full_name=full_name,
            title=str(person.get("title") or "").strip(),
            organization_name=str(organization.get("name") or person.get("organization_name") or "").strip(),
            organization_domain=domain or None,
            email=email,
            email_status=status,
            linkedin_url=str(person.get("linkedin_url") or "").strip() or None,
            apollo_person_id=str(person.get("id") or "").strip() or None,
            apollo_has_email=bool(person.get("has_email")) if person.get("has_email") is not None else None,
        )

    def first_valid_email(self, contacts: Iterable[ApolloContact]) -> Optional[ApolloContact]:
        """Find first contact with approved, deliverable email status."""
        for contact in contacts:
            if not contact.email:
                continue
            status = (contact.email_status or "").lower()
            if status and status in self._allowed_email_statuses:
                logger.debug(
                    "✓ Accepted email: %s (status=%s)",
                    contact.email,
                    status,
                )
                return contact
            logger.debug(
                "Rejected email: %s (status=%s, allowed=%s)",
                contact.email,
                status or "NO_STATUS",
                sorted(self._allowed_email_statuses),
            )
        return None

    def _post_with_retries(self, endpoint: str, payload: dict) -> dict:
        url = f"{self._base_url}{endpoint}"
        backoff = max(1, self._initial_backoff_seconds)

        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.wait()
            try:
                response = self._session.post(url, json=payload, timeout=30)
                if response.status_code == 429:
                    raise requests.HTTPError("Apollo rate limit hit", response=response)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == self._max_retries:
                    raise RuntimeError(
                        f"Apollo request failed after {attempt} attempts: {exc}"
                    ) from exc
                logger.warning(
                    "Apollo request failed (attempt %s/%s). Retrying in %ss. Error: %s",
                    attempt,
                    self._max_retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
                backoff *= 2

        return {}

    @staticmethod
    def _extract_email_value(item: dict) -> Optional[str]:
        direct = str(item.get("email") or "").strip()
        if direct:
            return direct
        emails = item.get("email_addresses") or []
        if isinstance(emails, list):
            for candidate in emails:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                if isinstance(candidate, dict):
                    value = str(candidate.get("email") or "").strip()
                    if value:
                        return value
        return None

    @staticmethod
    def _extract_person(payload: dict) -> Optional[dict]:
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("person"), dict):
            return payload["person"]
        if isinstance(payload.get("contact"), dict):
            return payload["contact"]
        if isinstance(payload.get("data"), dict):
            nested = payload["data"]
            if isinstance(nested.get("person"), dict):
                return nested["person"]
            if isinstance(nested.get("contact"), dict):
                return nested["contact"]
        return None

    @staticmethod
    def _extract_bulk_people(payload: dict) -> List[dict]:
        if not isinstance(payload, dict):
            return []
        matches = payload.get("matches")
        if isinstance(matches, list):
            return [item for item in matches if isinstance(item, dict)]
        people = payload.get("people")
        if isinstance(people, list):
            return [item for item in people if isinstance(item, dict)]
        return []

    @staticmethod
    def _normalize_search_email_statuses(values: Iterable[str]) -> List[str]:
        allowed = {"verified", "unverified", "likely to engage", "unavailable"}
        normalized: List[str] = []
        for value in values:
            candidate = str(value or "").strip().lower().replace("_", " ")
            if candidate in allowed and candidate not in normalized:
                normalized.append(candidate)
        if not normalized:
            return ["verified", "likely to engage", "unverified"]
        return normalized

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        cleaned = str(full_name or "").strip()
        if not cleaned:
            return "", ""
        parts = cleaned.split()
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], " ".join(parts[1:])

    @staticmethod
    def _extract_domain(organization: dict) -> str:
        direct_keys = ["primary_domain", "domain", "website_url", "website"]
        for key in direct_keys:
            raw = str(organization.get(key) or "").strip()
            if not raw:
                continue
            if "://" in raw:
                parsed = urlparse(raw)
                host = (parsed.netloc or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    return host
            value = raw.lower()
            if value.startswith("www."):
                value = value[4:]
            if "." in value:
                return value
        return ""
