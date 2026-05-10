from __future__ import annotations

import json as _json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv


load_dotenv()


def _env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {name}: {value}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid float value for {name}: {value}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    items = [item.strip().lower() for item in value.split(",")]
    normalized = tuple(item for item in items if item)
    if not normalized:
        raise ValueError(f"Invalid CSV value for {name}: {value}")
    return normalized


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"'", '"'}:
        return trimmed[1:-1].strip()
    return trimmed


def _normalize_mail_username(username: str) -> str:
    return _strip_wrapping_quotes(username)


def _normalize_mail_password(host: str, password: str) -> str:
    normalized = _strip_wrapping_quotes(password)
    # Gmail app passwords are often copied with spaces (xxxx xxxx xxxx xxxx).
    if "gmail.com" in host.lower():
        normalized = normalized.replace(" ", "")
    return normalized


def _normalize_mail_credentials(host: str, username: str, password: str) -> Tuple[str, str]:
    return _normalize_mail_username(username), _normalize_mail_password(host, password)


@dataclass(frozen=True)
class Settings:
    google_service_account_json: str
    creators_spreadsheet_id: str
    creators_worksheet: str
    rss_worksheet: str
    email_formats_spreadsheet_id: str
    email_formats_worksheet_gid: int
    openai_api_key: str
    openai_model: str
    apollo_api_key: str
    apollo_base_url: str
    apollo_requests_per_minute: int
    apollo_max_retries: int
    apollo_initial_backoff_seconds: int
    apollo_search_max_pages: int
    apollo_search_per_page: int
    apollo_search_contact_email_statuses: Tuple[str, ...]
    apollo_allowed_email_statuses: Tuple[str, ...]
    niche_match_min_confidence: float
    slack_bot_token: str
    slack_approval_channel_id: str
    slack_approval_timeout_minutes: int
    slack_poll_interval_seconds: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    smtp_use_tls: bool
    smtp_accounts: List[dict] = field(default_factory=list)
    state_db_path: str = ""
    log_level: str = "INFO"
    outreach_run_interval_hours: int = 12
    reply_check_interval_seconds: int = 300
    api_host: str = "127.0.0.1"
    api_port: int = 8090

    @classmethod
    def from_env(cls) -> "Settings":
        creators_spreadsheet_id = _env_required("CREATORS_SPREADSHEET_ID").strip()
        email_formats_spreadsheet_id = os.getenv("EMAIL_FORMATS_SPREADSHEET_ID", creators_spreadsheet_id).strip()
        if not email_formats_spreadsheet_id:
            email_formats_spreadsheet_id = creators_spreadsheet_id

        smtp_host = _env_required("SMTP_HOST").strip()
        smtp_username, smtp_password = _normalize_mail_credentials(
            smtp_host,
            _env_required("SMTP_USERNAME"),
            _env_required("SMTP_PASSWORD"),
        )
        return cls(
            google_service_account_json=_env_required("GOOGLE_SERVICE_ACCOUNT_JSON"),
            creators_spreadsheet_id=creators_spreadsheet_id,
            creators_worksheet=os.getenv("CREATORS_WORKSHEET", "creators"),
            rss_worksheet=os.getenv("RSS_WORKSHEET", "rss_media"),
            email_formats_spreadsheet_id=email_formats_spreadsheet_id,
            email_formats_worksheet_gid=max(0, _env_int("EMAIL_FORMATS_WORKSHEET_GID", 0)),
            openai_api_key=_env_required("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            apollo_api_key=_env_required("APOLLO_API_KEY"),
            apollo_base_url=os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1").rstrip("/"),
            apollo_requests_per_minute=_env_int("APOLLO_REQUESTS_PER_MINUTE", 50),
            apollo_max_retries=_env_int("APOLLO_MAX_RETRIES", 5),
            apollo_initial_backoff_seconds=_env_int("APOLLO_INITIAL_BACKOFF_SECONDS", 2),
            apollo_search_max_pages=max(1, _env_int("APOLLO_SEARCH_MAX_PAGES", 3)),
            apollo_search_per_page=max(1, _env_int("APOLLO_SEARCH_PER_PAGE", 25)),
            apollo_search_contact_email_statuses=_env_csv(
                "APOLLO_SEARCH_CONTACT_EMAIL_STATUSES",
                ("verified", "likely to engage", "unverified"),
            ),
            apollo_allowed_email_statuses=_env_csv(
                "APOLLO_ALLOWED_EMAIL_STATUSES",
                ("verified", "valid", "deliverable", "safe", "smtp_valid"),
            ),
            niche_match_min_confidence=max(0.0, min(1.0, _env_float("NICHE_MATCH_MIN_CONFIDENCE", 0.6))),
            slack_bot_token=_env_required("SLACK_BOT_TOKEN"),
            slack_approval_channel_id=_env_required("SLACK_APPROVAL_CHANNEL_ID"),
            slack_approval_timeout_minutes=_env_int("SLACK_APPROVAL_TIMEOUT_MINUTES", 120),
            slack_poll_interval_seconds=_env_int("SLACK_POLL_INTERVAL_SECONDS", 20),
            smtp_host=smtp_host,
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from_email=_env_required("SMTP_FROM_EMAIL"),
            smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
            smtp_accounts=_parse_smtp_accounts(),
            state_db_path=os.getenv("STATE_DB_PATH", str(Path("workflow_state.db"))),
            outreach_run_interval_hours=max(1, _env_int("OUTREACH_RUN_INTERVAL_HOURS", 12)),
            reply_check_interval_seconds=max(60, _env_int("REPLY_CHECK_INTERVAL_SECONDS", 300)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=_env_int("API_PORT", 8090),
        )


def _parse_smtp_accounts() -> List[dict]:
    """Parse SMTP_ACCOUNTS JSON env var. Returns empty list if not set."""
    raw = os.getenv("SMTP_ACCOUNTS", "").strip()
    if not raw:
        return []
    try:
        accounts = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in SMTP_ACCOUNTS: {exc}") from exc
    if not isinstance(accounts, list):
        raise ValueError("SMTP_ACCOUNTS must be a JSON array")
    required_keys = {"label", "host", "port", "username", "password", "from_email"}
    normalized_accounts = []
    for idx, acct in enumerate(accounts):
        if not isinstance(acct, dict):
            raise ValueError(f"SMTP_ACCOUNTS[{idx}] must be a JSON object")
        missing = required_keys - set(acct.keys())
        if missing:
            raise ValueError(f"SMTP_ACCOUNTS[{idx}] missing keys: {missing}")

        host = str(acct["host"]).strip()
        username, password = _normalize_mail_credentials(
            host=host,
            username=str(acct["username"]),
            password=str(acct["password"]),
        )

        normalized = dict(acct)
        normalized["host"] = host
        normalized["username"] = username
        normalized["password"] = password

        if normalized.get("imap_host") is not None:
            normalized["imap_host"] = str(normalized["imap_host"]).strip()
        imap_host = str(normalized.get("imap_host") or host)

        if normalized.get("imap_username") is not None:
            normalized["imap_username"] = _normalize_mail_username(str(normalized["imap_username"]))
        if normalized.get("imap_password") is not None:
            normalized["imap_password"] = _normalize_mail_password(imap_host, str(normalized["imap_password"]))

        normalized_accounts.append(normalized)

    return normalized_accounts
