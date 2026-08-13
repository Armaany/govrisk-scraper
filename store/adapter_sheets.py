# Google Sheets adapter for appending and querying opportunity records.
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import Config
from models import OpportunityRecord


class StoreWriteError(Exception):
    """Raised when writing a record to Google Sheets fails."""


class SheetsAdapter:
    """Provides append and lookup operations for Google Sheets storage."""

    HEADERS = [
        "portal_source",
        "opportunity_title",
        "funder_organisation",
        "country_region",
        "deadline",
        "contract_value",
        "opportunity_link",
        "summary",
        "relevance_score",
        "bid_recommendation",
        "risk_flags",
        "review_status"
    ]

    def __init__(self, config: Config):
        """Authenticate service account and open target spreadsheet worksheet."""
        self.config = config
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        credentials = Credentials.from_service_account_file(
            self.config.service_account_json,
            scopes=scopes,
        )
        client = gspread.authorize(credentials)
        self.spreadsheet = client.open_by_key(self.config.google_sheets_id)
        self.worksheet = self.spreadsheet.worksheet(self.config.sheets_tab_name)
        self._ensure_headers()

    def clear_and_reset(self) -> None:
        """Delete all data rows, keeping header row 1 intact."""
        try:
            total_rows = self.worksheet.row_count
            data_rows = self.worksheet.get_all_values()
            # Only clear if there are data rows beyond the header
            if len(data_rows) <= 1:
                print("[Sheets] Sheet already empty — nothing to clear")
                return
            # Delete rows from bottom up to avoid index shifting (rows 2 onward)
            num_data_rows = len(data_rows) - 1
            self.worksheet.delete_rows(2, len(data_rows))
            print(f"[Sheets] Sheet cleared — {num_data_rows} data rows removed, header kept")
            print("[Sheets] Ready for fresh run")
        except Exception as exc:
            print(f"[Sheets] Clear failed: {exc}")
            raise

    def test_connection(self) -> bool:
        """Verify the Google Sheets connection is live and the worksheet is accessible."""
        try:
            worksheet = self.spreadsheet.worksheet(self.config.sheets_tab_name)
            print(f"[Sheets] Connected successfully to '{self.config.sheets_tab_name}'")
            print(f"[Sheets] Sheet has {worksheet.row_count} rows")
            return True
        except Exception as e:
            print(f"[Sheets] Connection failed: {e}")
            return False

    def _ensure_headers(self) -> None:
        """Write header row only when sheet is empty and preserve existing headers."""
        first_row = self.worksheet.row_values(1)
        if first_row:
            return
        self.worksheet.append_row(self.HEADERS, value_input_option="RAW")

    def get_all_ids(self) -> set:
        """Return all stored opportunity IDs from first column excluding header."""
        try:
            id_column = self.worksheet.col_values(1)
        except Exception:
            return set()
        if len(id_column) <= 1:
            return set()
        return {value.strip() for value in id_column[1:] if value.strip()}

    def record_exists(self, devex_opportunity_id: str) -> bool:
        """Return True if an opportunity ID already exists in the sheet."""
        return devex_opportunity_id in self.get_all_ids()

    def write_record(self, record: OpportunityRecord) -> str:
        """Append one record in header order and return written row number."""
        try:
            payload = record.to_dict()
            row = [payload.get(header) for header in self.HEADERS]
            self.worksheet.append_row(row, value_input_option="RAW")
            return str(len(self.worksheet.col_values(1)))
        except Exception as exc:
            raise StoreWriteError(f"Failed to write record to Google Sheets: {exc}") from exc

    def get_records_since(self, since: datetime) -> list:
        """Return all records with scraped_at timestamp greater than or equal to since."""
        try:
            rows = self.worksheet.get_all_records()
        except Exception:
            return []

        results: list[dict] = []
        for row in rows:
            scraped_at_raw = row.get("scraped_at")
            if not scraped_at_raw:
                continue
            try:
                scraped_at = datetime.fromisoformat(str(scraped_at_raw))
            except ValueError:
                continue
            if scraped_at >= since:
                row.setdefault("source_portal", "devex")
                results.append(row)
        return results
