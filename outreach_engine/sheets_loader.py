from __future__ import annotations

from pathlib import Path
from typing import List

import gspread

from .config import Settings
from .models import Creator, RSSFeed


class SheetsLoader:
    def __init__(self, settings: Settings) -> None:
        key_path = Path(settings.google_service_account_json)
        if not key_path.exists():
            raise FileNotFoundError(
                f"Google service account file not found: {settings.google_service_account_json}. "
                "Set GOOGLE_SERVICE_ACCOUNT_JSON to a valid file path."
            )
        self._client = gspread.service_account(filename=settings.google_service_account_json)
        self._spreadsheet = self._client.open_by_key(settings.creators_spreadsheet_id)
        self._creators_worksheet_name = settings.creators_worksheet
        self._rss_worksheet_name = settings.rss_worksheet

    def load_creators(self) -> List[Creator]:
        worksheet = self._spreadsheet.worksheet(self._creators_worksheet_name)
        records = worksheet.get_all_records()
        creators: List[Creator] = []

        for index, row in enumerate(records, start=2):
            creators.append(
                Creator(
                    creator_name=self._nullable_text(row, "creator_name"),
                    creator_niche=self._nullable_text(row, "creator_niche"),
                    creator_platform=self._nullable_text(row, "creator_platform"),
                    creator_followers=self._nullable_text(row, "creator_followers"),
                    instagram_profile=self._nullable_text(row, "instagram_profile"),
                    tiktok_profile=self._nullable_text(row, "tiktok_profile"),
                    youtube_profile=self._nullable_text(row, "youtube_profile"),
                )
            )

        creators = [creator for creator in creators if creator.creator_name and creator.creator_niche]
        if not creators:
            raise ValueError("No valid creators found in creators worksheet")
        return creators

    def load_rss_feeds(self) -> List[RSSFeed]:
        worksheet = self._spreadsheet.worksheet(self._rss_worksheet_name)
        records = worksheet.get_all_records()
        feeds: List[RSSFeed] = []

        for index, row in enumerate(records, start=2):
            if "feed_url" not in row:
                raise ValueError(f"RSS sheet row {index} missing feed_url field")
            feed_url = str(row.get("feed_url", "")).strip()
            if feed_url:
                feeds.append(RSSFeed(feed_url=feed_url))

        if not feeds:
            raise ValueError("No RSS feed URLs found in RSS worksheet")
        return feeds

    @staticmethod
    def _nullable_text(row: dict, key: str) -> str | None:
        value = row.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
