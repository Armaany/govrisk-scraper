"""Live verification helper — runs the UNDP adapter in dry-run mode.

Usage: python run_undp_only.py

Reports:
  - Listing page fetch status
  - Card counts (total, LATAM, expired, active)
  - Detail-page enrichment (fetched, fallback, cancelled)
  - Keyword filter results
  - Matched keywords for each passing opportunity

Does NOT print secrets or full config. Does NOT write to any store.
"""
import asyncio
import os

# Force dry_run
os.environ.setdefault("RUN_MODE", "dry_run")

from config import load_config
from engine.keyword_filter import MATCHING_TEXT_KEY, KeywordFilter
from portals.undp_adapter import UNDPAdapter


async def main():
    config = load_config()
    config.run_mode = "dry_run"

    adapter = UNDPAdapter(config)
    kf = KeywordFilter(config)

    print("=" * 70)
    print("UNDP Adapter — Live Verification (dry-run)")
    print("=" * 70)

    results = await adapter.fetch_opportunities()

    print(f"\n{'='*70}")
    print(f"RESULTS: {len(results)} opportunities passed all filters")
    print(f"{'='*70}\n")

    for i, opp in enumerate(results, 1):
        title = opp.get("opportunity_title", "")[:70]
        country = opp.get("country_region", "")
        deadline = opp.get("deadline", "")
        snippet_len = len(opp.get("description_snippet", ""))
        has_matching_text = MATCHING_TEXT_KEY in opp
        matched = kf.get_matched_keywords(opp)

        print(f"[{i:2d}] {title}")
        print(f"     Country: {country} | Deadline: {deadline}")
        print(f"     Snippet length: {snippet_len} chars | Has full matching text: {has_matching_text}")
        print(f"     Matched keywords: {matched}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
