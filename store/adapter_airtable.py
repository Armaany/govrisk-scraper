# Airtable adapter for writing and querying opportunity records.
import time
from datetime import datetime

from pyairtable import Api

from config import Config
from models import OpportunityRecord
from store.adapter_sheets import StoreWriteError


class AirtableAdapter:
    """Provides cached ID checks and record writes to Airtable."""

    def __init__(self, config: Config):
        """Initialize Airtable API client and cache existing opportunity IDs."""
        self.config = config
        self.api = Api(self.config.airtable_api_key)
        self.table = self.api.table(self.config.airtable_base_id, self.config.airtable_table_name)
        self._existing_ids = self.get_all_ids()

    def test_connection(self) -> bool:
        """Verify the Airtable connection is live and the table is accessible."""
        try:
            records = self.table.all(max_records=1)
            print(f"[Airtable] Connected successfully to '{self.config.airtable_table_name}'")
            print(f"[Airtable] Table reachable — sample record count: {len(records)}")
            return True
        except Exception as e:
            print(f"[Airtable] Connection failed: {e}")
            return False

    def get_all_ids(self) -> set:
        """Return all devex_opportunity_id values currently present in Airtable."""
        try:
            records = self.table.all()
        except Exception:
            return set()
        if not records:
            return set()

        ids: set[str] = set()
        for record in records:
            fields = record.get("fields", {})
            devex_id = fields.get("devex_opportunity_id")
            if devex_id:
                ids.add(str(devex_id))
        return ids

    def record_exists(self, devex_opportunity_id: str) -> bool:
        """Check cached existing IDs without hitting Airtable on each call."""
        return devex_opportunity_id in self._existing_ids

    def write_record(self, record: OpportunityRecord) -> str:
        """Create Airtable record, update ID cache, and return Airtable record ID."""
        try:
            payload = record.to_dict()
            cleaned_payload = {
                key: ("" if value is None else value)
                for key, value in payload.items()
            }
            created = self.table.create(cleaned_payload)
            self._existing_ids.add(record.devex_opportunity_id)
            time.sleep(0.25)
            return str(created.get("id", ""))
        except Exception as exc:
            raise StoreWriteError(f"Failed to write record to Airtable: {exc}") from exc

    def get_records_since(self, since: datetime) -> list:
        """Return Airtable records whose scraped_at timestamp is on/after since."""
        try:
            records = self.table.all()
        except Exception:
            return []

        results: list[dict] = []
        for record in records:
            fields = record.get("fields", {})
            scraped_at_raw = fields.get("scraped_at")
            if not scraped_at_raw:
                continue
            try:
                scraped_at = datetime.fromisoformat(str(scraped_at_raw))
            except ValueError:
                continue
            if scraped_at >= since:
                fields.setdefault("source_portal", "devex")
                results.append(fields)
        return results
