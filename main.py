# Main async orchestrator wiring all scraper components end-to-end.
import argparse
import asyncio

from config import load_config
from engine.keyword_filter import KeywordFilter
from llm.interpreter import LLMInterpreter
from models import OpportunityRecord
from portals.base_adapter import BasePortalAdapter
from portals.devex_adapter import DevexAdapter
from portals.iadb_adapter import IADBAdapter
from portals.oecd_adapter import OECDAdapter
from portals.perplexity_adapter import PerplexityAdapter
from portals.samgov_adapter import SAMGovAdapter
from portals.undp_adapter import UNDPAdapter
from portals.usaid_adapter import USAIDAdapter
from portals.worldbank_adapter import WorldBankAdapter
from store.adapter_airtable import AirtableAdapter
from store.adapter_sheets import SheetsAdapter, StoreWriteError
from utils.audit import AuditLogger
from utils.notifier import Notifier


async def build_adapter_registry(config) -> list[BasePortalAdapter]:
    """Return a list of enabled adapters based on config flags."""
    adapters: list[BasePortalAdapter] = []
    if config.devex_enabled:
        adapters.append(DevexAdapter(config))
    if config.undp_enabled:
        adapters.append(UNDPAdapter(config))
    if config.worldbank_enabled:
        adapters.append(WorldBankAdapter(config))
    if config.usaid_enabled:
        adapters.append(USAIDAdapter(config))
    if config.iadb_enabled:
        adapters.append(IADBAdapter(config))
    if config.oecd_enabled:
        adapters.append(OECDAdapter(config))
    if config.samgov_enabled:
        adapters.append(SAMGovAdapter(config))
    if config.perplexity_enabled:
        adapters.append(PerplexityAdapter(config))
    return adapters


def deduplicate_opportunities(
    all_opportunities: list[dict],
    existing_ids: set,
) -> tuple[list[dict], int]:
    """Deduplicate opportunities by opportunity_id then opportunity_link.

    Returns (deduplicated_list, duplicates_skipped_count).
    """
    seen_ids: set[str] = set()
    seen_links: set[str] = set()
    deduplicated: list[dict] = []
    duplicates_skipped = 0

    for opp in all_opportunities:
        opp_id = opp.get("opportunity_id", "")
        opp_link = opp.get("opportunity_link", "")

        if opp_id and (opp_id in existing_ids or opp_id in seen_ids):
            duplicates_skipped += 1
            continue

        if opp_link and opp_link in seen_links:
            duplicates_skipped += 1
            continue

        if opp_id:
            seen_ids.add(opp_id)
        if opp_link:
            seen_links.add(opp_link)
        deduplicated.append(opp)

    return deduplicated, duplicates_skipped


async def run_scraper():
    """Run the full GovRisk scrape pipeline from adapter registry to notifications."""
    config = load_config()
    audit = AuditLogger()
    audit.log_run_start(mode=config.run_mode, max_results=config.max_results)
    notifier = Notifier(config)

    store = SheetsAdapter(config) if config.store_type == "sheets" else AirtableAdapter(config)

    if not store.test_connection():
        print("WARNING: Store connection failed — running in dry_run mode as fallback")
        config.run_mode = "dry_run"
    else:
        print(f"Store ready — RUN_MODE={config.run_mode}")

    existing_ids: set[str] = set(store.get_all_ids())

    total_scraped = 0
    total_matched = 0
    total_written = 0
    llm_calls_made = 0
    duplicates_skipped = 0
    errors = 0

    keyword_filter = KeywordFilter(config)
    interpreter = LLMInterpreter(config)

    # Build adapter registry — only enabled adapters
    adapters = await build_adapter_registry(config)

    all_opportunities: list[dict] = []

    # Unified adapter loop — each adapter is isolated; one failure never blocks others
    for adapter in adapters:
        try:
            results = await adapter.fetch_opportunities()
            all_opportunities.extend(results)
            audit.log(
                event_type="adapter_complete",
                detail=f"portal={adapter.portal_name} results={len(results)}",
            )
        except Exception as exc:
            errors += 1
            audit.log_error(str(exc))
            notifier.send_error_alert(str(exc), component=adapter.portal_name)
            continue

    # Deduplication across all adapters
    deduplicated, run_duplicates = deduplicate_opportunities(all_opportunities, existing_ids)
    duplicates_skipped += run_duplicates

    # Filter → LLM → Store pipeline
    try:
        for opp in deduplicated:
            total_scraped += 1
            source_portal = opp.get("source_portal", "devex")

            opportunity_id = opp.get("opportunity_id", "")
            title = opp.get("opportunity_title")

            if not keyword_filter.passes_filter(opp):
                audit.log_filtered_out(opp.get("opportunity_link", ""))
                continue

            total_matched += 1
            opp["matched_keywords"] = keyword_filter.get_matched_keywords(opp)

            # Map opportunity_id → devex_opportunity_id for OpportunityRecord compatibility
            opp.setdefault("devex_opportunity_id", opportunity_id)

            try:
                llm_result = interpreter.interpret(opp)
                llm_calls_made += 1
                opp["llm_called"] = True
            except Exception as exc:
                errors += 1
                audit.log_error(str(exc), opportunity_id=opportunity_id)
                opp["llm_called"] = False
                llm_result = {}

            merged = {**opp, **llm_result}
            record = OpportunityRecord.from_dict(merged)

            if config.run_mode == "live":
                try:
                    store.write_record(record)
                    total_written += 1
                    existing_ids.add(record.devex_opportunity_id)
                except StoreWriteError as exc:
                    errors += 1
                    audit.log_error(str(exc), opportunity_id=record.devex_opportunity_id)
            else:
                print(f"dry_run — would write: {record.opportunity_title}")

            audit.log(
                event_type="opportunity_processed",
                detail=(
                    f"Processed {record.opportunity_link} "
                    f"source_portal={source_portal}"
                ),
                opportunity_id=record.devex_opportunity_id,
                title=record.opportunity_title,
                llm_called=bool(opp.get("llm_called", False)),
                confidence=merged.get("llm_confidence"),
            )

    finally:
        audit.log_run_complete(
            total_scraped=total_scraped,
            total_matched=total_matched,
            total_written=total_written,
            llm_calls_made=llm_calls_made,
            duplicates_skipped=duplicates_skipped,
            errors=errors,
        )
        notifier.send_completion_summary(
            total_scraped=total_scraped,
            total_matched=total_matched,
            total_written=total_written,
            llm_calls_made=llm_calls_made,
            duplicates_skipped=duplicates_skipped,
            errors=errors,
            run_mode=config.run_mode,
        )
        print(
            "Final summary: "
            f"scraped={total_scraped}, matched={total_matched}, written={total_written}, "
            f"llm_calls={llm_calls_made}, duplicates={duplicates_skipped}, errors={errors}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GovRisk procurement scraper")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all data rows from the sheet (keeps header) then exit",
    )
    args = parser.parse_args()

    if args.clear:
        config = load_config()
        store = SheetsAdapter(config) if config.store_type == "sheets" else AirtableAdapter(config)
        store.clear_and_reset()
    else:
        asyncio.run(run_scraper())
