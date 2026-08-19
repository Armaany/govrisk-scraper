"""Standalone dry-run of UNDPAdapter only — shows live results with real descriptions."""
import asyncio
import os
os.environ["RUN_MODE"] = "dry_run"

from config import load_config
from engine.keyword_filter import KeywordFilter
from portals.undp_adapter import UNDPAdapter

async def main():
    config = load_config()
    adapter = UNDPAdapter(config)
    kf = KeywordFilter(config)

    print("=" * 60)
    print("UNDP Adapter — live run with detail-page descriptions")
    print("=" * 60)

    results = await adapter.fetch_opportunities()

    print(f"\nTotal matched: {len(results)}")
    print()
    for i, opp in enumerate(results, 1):
        title = opp.get("opportunity_title", "")
        country = opp.get("country_region", "")
        deadline = opp.get("deadline", "")
        desc = opp.get("description_snippet", "")
        matched = kf.get_matched_keywords(opp)

        print(f"[{i}] {title}")
        print(f"     Country:  {country}")
        print(f"     Deadline: {deadline}")
        print(f"     Keywords: {matched}")
        # Show first 200 chars of description
        print(f"     Desc:     {desc[:200]}")
        print(f"     Is title? {desc == title}")
        print()

asyncio.run(main())
