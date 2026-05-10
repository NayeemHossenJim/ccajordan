from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Creator:
    creator_name: Optional[str]
    creator_niche: Optional[str]
    creator_platform: Optional[str]
    creator_followers: Optional[str]
    instagram_profile: Optional[str]
    tiktok_profile: Optional[str]
    youtube_profile: Optional[str]


@dataclass(frozen=True)
class CreatorEmailTemplate:
    creator_name: str
    subject: str
    body: str


@dataclass(frozen=True)
class RSSFeed:
    feed_url: str


@dataclass(frozen=True)
class Lead:
    title: str
    summary: str
    link: str
    published: Optional[str]
    company_name: Optional[str] = None
    brand_niche: Optional[str] = None


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    confidence: float
    rationale: str


@dataclass(frozen=True)
class ApolloContact:
    full_name: str
    title: str
    organization_name: str
    organization_domain: Optional[str]
    email: Optional[str]
    email_status: Optional[str]
    linkedin_url: Optional[str]
    apollo_person_id: Optional[str] = None
    apollo_has_email: Optional[bool] = None


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str


@dataclass(frozen=True)
class SendResult:
    success: bool
    message_id: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class SMTPAccount:
    label: str
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    last_message: str
