# Google Sheets adapter for appending and querying opportunity records.
# Schema v1.1: 14-column canonical schema with header-name-driven writing.
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import Config
from models import OpportunityRecord


class StoreWriteError(Exception):
    """Raised when writing a record to Google Sheets fails."""


class SheetsSchemaError(Exception):
    """Raised when the Google Sheet header row does not match the required
    schema. Schema v1.1 requires all 14 canonical columns to be present
    (in any order), rejects duplicates (including whitespace/case-normalized),
    and rejects missing required columns before any write."""


class SheetsAdapter:
    """Provides append and lookup operations for Google Sheets storage.

    Schema v1.1 extends the frozen contract from 12 to 14 columns by appending
    ``scraped_at`` and ``matched_keywords``.  Writing is header-name-driven:
    values are projected by normalized column name, not by positional index.
    """

    # Canonical 14-column schema v1.1 — initialization order.
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
        "review_status",
        "scraped_at",
        "matched_keywords",
    ]

    # Mapping from external column name → canonical internal dict key.
    CANONICAL_KEY_FOR_COLUMN = {
        "portal_source": "source_portal",
        "opportunity_title": "opportunity_title",
        "funder_organisation": "funder_organisation",
        "country_region": "country_region",
        "deadline": "deadline",
        "contract_value": "contract_value",
        "opportunity_link": "opportunity_link",
        "summary": "summary",
        "relevance_score": "relevance_score",
        "bid_recommendation": "bid_recommendation",
        "risk_flags": "risk_flags",
        "review_status": "review_status",
        "scraped_at": "scraped_at",
        "matched_keywords": "matched_keywords",
    }

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
            data_rows = self.worksheet.get_all_values()
            if len(data_rows) <= 1:
                print("[Sheets] Sheet already empty — nothing to clear")
                return
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
        """Initialize or validate the header row (schema v1.1).

        - If the sheet is empty, write the canonical 14-column header.
        - If row 1 is populated, validate it. Reject missing required columns,
          duplicates, etc. Accept any column order.
        - Never rewrite a populated header row.
        """
        first_row = self.worksheet.row_values(1)
        if not any((cell or "").strip() for cell in first_row):
            self.worksheet.append_row(self.HEADERS, value_input_option="RAW")
            self._header_index = {h: i for i, h in enumerate(self.HEADERS)}
            self._row_length = len(self.HEADERS)
            return
        self._validate_headers(first_row)

    def _validate_headers(self, header_row: list) -> None:
        """Validate a populated header row against v1.1 required columns.

        Rules:
        - All 14 canonical columns must be present (any order).
        - No duplicates after trim + casefold normalization.
        - Unknown additional columns are allowed (blanks written under them).
        - A legacy 12-column header missing scraped_at/matched_keywords
          produces an explicit migration error.
        """
        raw = [(h if h is not None else "") for h in header_row]
        # Drop trailing fully-empty padding cells.
        trimmed = list(raw)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()

        # Duplicate detection (case/whitespace normalized).
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for original in trimmed:
            norm = original.strip().casefold()
            if not norm:
                continue
            if norm in seen:
                duplicates.append(original)
            else:
                seen[norm] = original
        if duplicates:
            raise SheetsSchemaError(
                "Google Sheet header contains duplicate columns (including "
                f"duplicates differing only by whitespace/case): {duplicates}. "
                f"Expected the canonical schema: {self.HEADERS}"
            )

        # Check required columns present.
        actual_normed = {h.strip().casefold(): h.strip() for h in trimmed if h.strip()}
        required_set = set(self.HEADERS)
        missing = [h for h in self.HEADERS if h.casefold() not in actual_normed]

        if missing:
            # Produce explicit v1.0 → v1.1 migration error for 12-col sheets.
            raise SheetsSchemaError(
                f"schema incompatible — missing columns: {', '.join(missing)}. "
                f"The v1.1 schema requires all 14 canonical columns. "
                f"Current header: {trimmed}"
            )

        # Build header index mapping (actual column positions).
        # Use the original (non-normalized) header values for index lookup.
        self._header_index = {}
        for idx, h in enumerate(trimmed):
            norm = h.strip().casefold()
            # Map canonical column names to their position.
            for canonical in self.HEADERS:
                if canonical.casefold() == norm:
                    self._header_index[canonical] = idx
                    break
        self._row_length = len(trimmed)

    def get_all_ids(self) -> set:
        """Deprecated — raises NotImplementedError."""
        raise NotImplementedError(
            "SheetsAdapter.get_all_ids() is unsupported under the v1.1 schema. "
            "Use get_all_links() for cross-run deduplication."
        )

    def record_exists(self, devex_opportunity_id: str) -> bool:
        """Deprecated — raises NotImplementedError."""
        raise NotImplementedError(
            "SheetsAdapter.record_exists() is unsupported under the v1.1 schema. "
            "Use get_all_links() for cross-run deduplication."
        )

    def _project_row(self, record: OpportunityRecord) -> list:
        """Project a canonical record onto the header-driven row.

        Values are placed by header name, not index. Unknown columns get blanks.
        Special handling:
        - source_portal → portal_source column
        - risk_flags → comma-separated string
        - matched_keywords → JSON array (UTF-8 safe, no ASCII escaping)
        - scraped_at → UTC ISO 8601 with Z suffix
        """
        payload = record.to_dict()
        row = [""] * self._row_length
        for header, idx in self._header_index.items():
            canonical_key = self.CANONICAL_KEY_FOR_COLUMN.get(header)
            if canonical_key is None:
                continue
            value = payload.get(canonical_key)
            if header == "risk_flags" and isinstance(value, list):
                value = ", ".join(str(flag) for flag in value)
            elif header == "matched_keywords":
                value = record.serialize_matched_keywords_for_sheet()
            elif header == "scraped_at":
                value = payload.get("scraped_at", "")
            if value is None:
                value = ""
            row[idx] = value
        return row

    def write_record(self, record: OpportunityRecord) -> str:
        """Append one record via header-name-driven projection."""
        try:
            row = self._project_row(record)
            self.worksheet.append_row(row, value_input_option="RAW")
            return str(len(self.worksheet.col_values(1)))
        except Exception as exc:
            raise StoreWriteError(f"Failed to write record to Google Sheets: {exc}") from exc

    def get_all_links(self) -> set:
        """Return persisted non-empty opportunity_link values for cross-run dedup.

        Reads the opportunity_link column by its actual header position.
        """
        try:
            link_index = self._header_index["opportunity_link"]
            link_column = self.worksheet.col_values(link_index + 1)
        except Exception:
            return set()
        if len(link_column) <= 1:
            return set()
        return {value.strip() for value in link_column[1:] if value.strip()}

    def get_records_since(self, since: datetime) -> list:
        """Unsupported — raises NotImplementedError.

        Cross-run deduplication uses get_all_links() instead.
        """
        raise NotImplementedError(
            "get_records_since() is not supported. "
            "Use get_all_links() for cross-run deduplication."
        )
