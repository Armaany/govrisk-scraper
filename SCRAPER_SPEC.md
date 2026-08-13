# GovRisk Devex Scraper — Spec Summary

## Approach
Python-first. BeautifulSoup extracts all fields for free.
LLM only called after keyword filter passes.

## Stack
- Playwright — browser login and navigation
- BeautifulSoup + lxml — HTML parsing and field extraction
- Python dateutil — deadline date parsing
- Claude API — interpretation only (summary, score, recommendation)
- Google Sheets OR Airtable — data store (STORE_TYPE env var)
- python-dotenv — environment variable loading
- pydantic — data validation

## What Python extracts (free)
- devex_opportunity_id (from URL)
- opportunity_title
- funder_organisation
- country_region
- deadline (YYYY-MM-DD)
- contract_value
- opportunity_link
- description_snippet (first 500 chars)
- matched_keywords (Python set intersection)

## What LLM adds (only if keyword match)
- summary (3-4 sentences for BD team)
- relevance_score (high/medium/low/unclear)
- relevance_reason (one sentence)
- bid_recommendation (pursue/monitor/pass/insufficient_info)
- risk_flags
- llm_confidence

## Keyword filters
Sectors: AML, anti-corruption, illicit flows, human trafficking,
justice reform, corruption, illicit finance, beneficial ownership,
asset recovery, money laundering

Countries: Mexico, Colombia, Peru, Brazil, Ecuador, Bolivia,
Guatemala, Honduras, El Salvador, Nicaragua, Costa Rica, Panama,
Dominican Republic, Haiti, Jamaica, Trinidad, Guyana, Venezuela,
Cuba, LATAM, Latin America, Caribbean, Regional

## Filter logic
Include if: title OR description contains ANY sector keyword
AND country field contains ANY target country.
If country is empty or Global: include only if LATAM mentioned
anywhere in description.

## Run modes
RUN_MODE=dry_run — scrape but do not write to store
RUN_MODE=live — full run with store writes
HEADLESS=false — show browser window during testing
HEADLESS=true — invisible browser for production

## Project structure
govrisk-scraper/
  main.py
  models.py
  config.py
  auth/
    devex_auth.py
  scraper/
    search.py
    parser.py
    keyword_filter.py
  llm/
    interpreter.py
    validator.py
  store/
    adapter_sheets.py
    adapter_airtable.py
  utils/
    audit.py
    notifier.py
  tests/
    test_parser.py
    test_filter.py
    test_llm.py
    fixtures/
  .env
  .env.example
  .gitignore
  requirements.txt
  README.md