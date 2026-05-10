from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import gspread

from .config import Settings
from .models import Creator, CreatorEmailTemplate, RSSFeed


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
        self._email_formats_spreadsheet_id = settings.email_formats_spreadsheet_id
        self._email_formats_worksheet_gid = settings.email_formats_worksheet_gid

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

    def load_creator_email_templates(self) -> Dict[str, List[CreatorEmailTemplate]]:
        spreadsheet = self._client.open_by_key(self._email_formats_spreadsheet_id)
        worksheet = self._worksheet_by_gid(spreadsheet, self._email_formats_worksheet_gid)
        rows = worksheet.get_all_values()
        templates: Dict[str, List[CreatorEmailTemplate]] = {}

        current_creator_name: str | None = None
        in_templates_block = False

        for row in rows:
            subject_cell = self._clean_cell(row, 0)
            body_cell = self._clean_cell(row, 1)

            if not subject_cell and not body_cell:
                continue

            if subject_cell.casefold() == "subject" and body_cell.casefold() == "copy":
                in_templates_block = True
                continue

            if subject_cell and not body_cell:
                current_creator_name = subject_cell
                in_templates_block = False
                continue

            if not (in_templates_block and current_creator_name and subject_cell and body_cell):
                continue

            key = self.normalize_creator_key(current_creator_name)
            if not key:
                continue

            templates.setdefault(key, []).append(
                CreatorEmailTemplate(
                    creator_name=current_creator_name,
                    subject=subject_cell,
                    body=body_cell,
                )
            )

        return templates

    @staticmethod
    def normalize_creator_key(name: str | None) -> str:
        if not name:
            return ""
        return " ".join(str(name).replace("\u00A0", " ").split()).casefold()

    @staticmethod
    def _worksheet_by_gid(spreadsheet: gspread.Spreadsheet, gid: int) -> gspread.Worksheet:
        for worksheet in spreadsheet.worksheets():
            if int(getattr(worksheet, "id", -1)) == gid:
                return worksheet
        raise ValueError(f"No worksheet found with gid={gid} in spreadsheet={spreadsheet.id}")

    @staticmethod
    def _clean_cell(row: List[str], index: int) -> str:
        if index >= len(row):
            return ""
        return str(row[index]).replace("\u00A0", " ").strip()

    @staticmethod
    def _nullable_text(row: dict, key: str) -> str | None:
        value = row.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
