<!-- Setup instructions and project overview for the GovRisk Devex scraper. -->

# GovRisk Devex Scraper



Stubbed project scaffold based on `SCRAPER_SPEC.md`.



## Setup



1. Create and activate a Python virtual environment.

2. Install dependencies:

   - `pip install -r requirements.txt`

3. Install Playwright browser binaries:

   - `playwright install`

4. Copy `.env.example` to `.env` and fill in required values.

5. Run in stub mode:

   - `python main.py`



## Project Structure



- `main.py` - orchestrator sequence

- `models.py` - shared dataclasses

- `config.py` - env loading and validation

- `auth/` - authentication

- `scraper/` - search, parse, and filtering

- `llm/` - interpretation and validation

- `store/` - output adapters

- `utils/` - audit and notifications

- `tests/` - placeholder test modules





## Known Issues / Recent Fixes



### UNDP Adapter - Description Enrichment (2026-08-19)



**Problem:** The UNDP adapter set `description_snippet` equal to the opportunity title, so keyword filtering only ever matched against short operational titles. This caused near-zero results despite a broad keyword list - most in-scope opportunities were being rejected for lack of matchable text.



**Fix:**



- `undp_adapter.py` now fetches each opportunity's detail page (bounded concurrency, semaphore of 8, adapter-level timeout) and extracts the Overview section using heading-based detection, falling back to the longest `postContent` block if no heading match is found.

- Keyword matching (`passes_filter` and `get_matched_keywords`) now uses the full extracted Overview text rather than the 1000-character truncated display snippet, so matches beyond character 1000 are no longer missed.

- 22 tests cover extraction, adversarial cases (keyword beyond 1000 chars, longer non-Overview blocks), concurrency bounds (semaphore-per-attempt), timing, retry/backoff (including Retry-After HTTP-date), timeout preservation, orchestration end-to-end, and matched-keyword visibility.



**Live verification (2026-08-19):** 108 LATAM UNDP cards scraped, 108 detail pages fetched successfully (0 fallbacks to title), 18 opportunities passed the filter - up from roughly 1-3 in prior runs.



**Known follow-ups (not yet done):**



- World Bank and USAID adapters have no logged successful completions in audit.log - needs separate diagnosis.

- Short sector keywords audited for substring false-positive risk; only "aml" (3 characters) flagged, risk assessed as negligible.

- Devex adapter currently disabled - authentication failing since April 2026.

