# Google Sheets adapter for appending and querying opportunity records.
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import Config
from models import OpportunityRecord


class StoreWriteError(Exception):
    """Raised when writing a record to Google Sheets fails."""


class SheetsSchemaError(Exception):
    """Raised when the Google Sheet header row does not match the canonical
    12-column Live_Sheet_Schema (missing, duplicate, reordered, or unexpected
    headers). Schema v1.0 (Option A) rejects any deviation to prevent
    positional corruption; header-order-independent writing is deferred to
    Phase B."""


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

    # Explicit projection from each external Live_Sheet_Schema column (HEADERS)
    # onto its canonical source key emitted by ``OpportunityRecord.to_dict()``.
    # The canonical model uses the internal name ``source_portal``; the external
    # column-1 label is ``portal_source`` — this mapping bridges the two.
    # Canonical fields with no external column (devex_opportunity_id,
    # description_snippet, matched_keywords, relevance_reason, llm_confidence,
    # llm_called, anna_benchmark, scraped_at) are intentionally NOT projected.
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
        """Initialize or strictly validate the header row (schema v1.0, Option A).

        - If the sheet is empty, write the canonical 12-column header.
        - If row 1 is populated, validate it BEFORE any read or write. Reject
          missing, duplicate (including duplicates that differ only by
          surrounding whitespace or letter case), reordered, or unexpected
          headers by raising :class:`SheetsSchemaError`.
        - Never rewrite or repair a populated live header automatically.
        """
        first_row = self.worksheet.row_values(1)
        if not any((cell or "").strip() for cell in first_row):
            self.worksheet.append_row(self.HEADERS, value_input_option="RAW")
            return
        self._validate_headers(first_row)

    def _validate_headers(self, header_row: list) -> None:
        """Strictly validate a populated header row against the canonical schema."""
        raw = [(h if h is not None else "") for h in header_row]
        # Drop trailing fully-empty padding cells that Sheets may append.
        trimmed = list(raw)
        while trimmed and not trimmed[-1].strip():
            trimmed.pop()

        # Duplicate detection normalized by trim + casefold (catches dups that
        # differ only by whitespace or letter case).
        seen = {}
        duplicates = []
        for original in trimmed:
            norm = original.strip().casefold()
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

        expected = self.HEADERS
        expected_set = set(expected)
        actual_set = set(trimmed)
        missing = [h for h in expected if h not in actual_set]
        unexpected = [h for h in trimmed if h not in expected_set]
        if missing or unexpected:
            raise SheetsSchemaError(
                "Google Sheet header does not match the canonical schema. "
                f"missing={missing} unexpected={unexpected}. "
                f"Expected exactly (in order): {expected}"
            )

        # Same set of columns but a different order: rejected under schema v1.0
        # (order-dependent writing; reordering risks positional corruption).
        if trimmed != expected:
            raise SheetsSchemaError(
                "Google Sheet header is present but reordered. Schema v1.0 "
                "requires the exact canonical column order. "
                f"expected={expected} actual={trimmed}"
            )

    def get_all_ids(self) -> set:
        """Deprecated under the 12-column Live_Sheet_Schema.

        The frozen schema has no persisted opportunity-ID column (column 1 is
        ``portal_source``), so there is no stable ID to read. Raises immediately
        (before any worksheet call). Use :meth:`get_all_links` for cross-run
        deduplication instead.
        """
        raise NotImplementedError(
            "SheetsAdapter.get_all_ids() is unsupported under the 12-column "
            "Live_Sheet_Schema: column 1 is 'portal_source', not an opportunity "
            "ID. Use get_all_links() for cross-run deduplication."
        )

    def record_exists(self, devex_opportunity_id: str) -> bool:
        """Deprecated under the 12-column Live_Sheet_Schema (no persisted ID).

        Raises immediately (before any worksheet call). Use
        :meth:`get_all_links` for cross-run deduplication instead.
        """
        raise NotImplementedError(
            "SheetsAdapter.record_exists() is unsupported under the 12-column "
            "Live_Sheet_Schema: there is no persisted opportunity-ID column. "
            "Use get_all_links() for cross-run deduplication."
        )

    def _project_row(self, record: OpportunityRecord) -> list:
        """Project a canonical record onto the frozen 12-column Live_Sheet_Schema.

        Performs an EXPLICIT ordered projection: for each external column in
        ``HEADERS`` (in order), pull the value from the canonical
        ``record.to_dict()`` payload using ``CANONICAL_KEY_FOR_COLUMN``. This
        keeps writes positionally aligned with the live header row and maps the
        canonical ``source_portal`` onto the external ``portal_source`` column
        (column 1). ``risk_flags`` (a canonical list) is joined into a
        comma-separated string to match prior display behavior. ``None`` values
        are substituted with ``""`` so the row stays aligned.
        """
        payload = record.to_dict()
        row = []
        for header in self.HEADERS:
            canonical_key = self.CANONICAL_KEY_FOR_COLUMN[header]
            value = payload.get(canonical_key)
            if header == "risk_flags" and isinstance(value, list):
                value = ", ".join(str(flag) for flag in value)
            row.append("" if value is None else value)
        return row

    def write_record(self, record: OpportunityRecord) -> str:
        """Append one record via explicit projection and return written row number."""
        try:
            row = self._project_row(record)
            self.worksheet.append_row(row, value_input_option="RAW")
            return str(len(self.worksheet.col_values(1)))
        except Exception as exc:
            raise StoreWriteError(f"Failed to write record to Google Sheets: {exc}") from exc

    def get_all_links(self) -> set:
        """Return persisted non-empty opportunity_link values for cross-run dedup.

        Reads the ``opportunity_link`` column (its index in ``HEADERS``, i.e.
        column 7 / index 6) excluding the header row. Uses a defensive
        try/except — returns an empty set on error. (Note: ``get_all_ids`` is
        deprecated and raises immediately; it no longer shares this code path.)
        """
        try:
            link_index = self.HEADERS.index("opportunity_link")
            link_column = self.worksheet.col_values(link_index + 1)
        except Exception:
            return set()
        if len(link_column) <= 1:
            return set()
        return {value.strip() for value in link_column[1:] if value.strip()}

    def get_records_since(self, since: datetime) -> list:
        """Unsupported under the Live_Sheet_Schema — always raises.

        The authoritative live 12-column schema has no ``scraped_at`` column, so
        there is no timestamp to filter on. This method therefore cannot be
        implemented against the live sheet and is deprecated. ``main.py`` does
        not call it, so raising here is safe. Cross-run deduplication is instead
        seeded from :meth:`get_all_links`.
        """
        raise NotImplementedError(
            "get_records_since() is not supported under the Live_Sheet_Schema: "
            "the frozen 12-column Google Sheet has no 'scraped_at' column to "
            "filter on. Use get_all_links() for cross-run deduplication instead."
        )
