# Requirements Document

## Introduction

GovRisk's existing Python scraper currently targets a single portal (Devex) through tightly coupled code spread across `engine/`, `portals/`, and `main.py`. This feature refactors the system into a generic multi-portal adapter architecture, where each procurement portal is encapsulated as a self-contained adapter implementing a shared abstract interface. The refactor adds two new portal adapters Ã¢â‚¬â€ SAM.gov (free REST API) and Perplexity AI (search-based discovery) Ã¢â‚¬â€ alongside a refactored Devex adapter, and updates the orchestration layer in `main.py` to iterate over all active adapters uniformly. The existing keyword filter, LLM interpreter, store adapters, audit logger, and notifier remain unchanged.

## Glossary

- **Adapter**: A concrete class implementing `BasePortalAdapter` that encapsulates all logic for fetching opportunities from one specific portal.
- **BasePortalAdapter**: The abstract base class defining the common interface all portal adapters must implement.
- **Devex_Adapter**: The refactored adapter for the Devex portal, replacing the current `portals/devex_auth.py` + `engine/search.py` + `engine/parser.py` combination.
- **SAMGov_Adapter**: The adapter for the SAM.gov REST API (`api.sam.gov`).
- **Perplexity_Adapter**: The adapter for Perplexity AI's search API used to discover procurement opportunities.
- **Orchestrator**: The `main.py` async function that loads config, iterates over active adapters, and routes results through the filter Ã¢â€ â€™ LLM Ã¢â€ â€™ store pipeline.
- **Config**: The `config.py` `Config` dataclass and `load_config()` function.
- **OpportunityRecord**: The `models.py` dataclass representing one normalized procurement opportunity.
- **KeywordFilter**: The `engine/keyword_filter.py` class that applies sector and geography filters.
- **LLMInterpreter**: The `llm/interpreter.py` class that calls Claude to enrich matched opportunities.
- **Store**: Either `SheetsAdapter` or `AirtableAdapter` from the `store/` module.
- **Portal**: An external data source (website, REST API, or AI search) from which procurement opportunities are fetched.
- **Active_Adapter**: An adapter whose corresponding `ENABLED` flag in `Config` is `True`.
- **Opportunity_Dict**: A plain Python `dict` with the normalized field set returned by every adapter's `fetch_opportunities()` method.
- **UNDP_Adapter**: The `portals/undp_adapter.py` `UNDPAdapter` class that scrapes the UNDP Procurement Notices portal and enriches listings with detail-page Overview text.
- **Matching_Text**: The full searchable text used exclusively for keyword matching, carried on an `Opportunity_Dict` under the transient key `_matching_text` (constant `MATCHING_TEXT_KEY` in `engine/keyword_filter.py`). Distinct from `description_snippet`, which is a truncated display/storage value.
- **Transient_Field**: An `Opportunity_Dict` key (`_matching_text` and `_full_overview`) that exists only in memory during a run and MUST be removed before an `OpportunityRecord` is constructed or written to any `Store`.
- **Enrichment_Phase**: The bounded-concurrency detail-page fetching stage in `UNDP_Adapter` during which each active listing's detail page is retrieved to extract full Overview text; governed by the `_DETAIL_ENRICHMENT_DEADLINE` of 120 seconds.
- **Enrichment_Deadline**: The 120-second wall-clock budget (`_DETAIL_ENRICHMENT_DEADLINE`) applied to the Enrichment_Phase only, excluding the listing-page fetch.
- **source_portal (canonical field)**: The internal/canonical field name carried on `OpportunityRecord` and in the canonical serialization (`OpportunityRecord.to_dict()`) that identifies the originating portal (e.g. `"devex"`, `"samgov"`, `"perplexity"`). This is the name used throughout the in-memory data model.
- **portal_source (external Sheet column)**: The external Google Sheets column label at column 1 of the authoritative live 12-column schema. The canonical `source_portal` value is mapped onto this external `portal_source` column when writing to Google Sheets. The two names refer to the same logical datum under different representations: `source_portal` is the internal name, `portal_source` is the external column label.
- **Live_Sheet_Schema**: The fixed, operational 12-column Google Sheets schema that predates this feature and is authoritative: (1) `portal_source`, (2) `opportunity_title`, (3) `funder_organisation`, (4) `country_region`, (5) `deadline`, (6) `contract_value`, (7) `opportunity_link`, (8) `summary`, (9) `relevance_score`, (10) `bid_recommendation`, (11) `risk_flags`, (12) `review_status`. This schema contains no `devex_opportunity_id` column and no `scraped_at` column and is not migrated by this feature.

---

## Requirements

### Requirement 1: Abstract Base Adapter Interface

**User Story:** As a developer, I want a shared abstract interface for all portal adapters, so that the orchestrator can treat every portal uniformly without portal-specific branching.

#### Acceptance Criteria

1. THE `BasePortalAdapter` SHALL define an abstract async method `fetch_opportunities() -> list[Opportunity_Dict]`.
2. THE `BasePortalAdapter` SHALL define an abstract property `portal_name -> str` that returns a human-readable portal identifier.
3. WHEN a concrete class inherits from `BasePortalAdapter` without implementing all abstract members, THE Python runtime SHALL raise `TypeError` on instantiation.
4. THE `BasePortalAdapter` SHALL accept a `Config` instance as its sole constructor argument.
5. THE `BasePortalAdapter` SHALL be importable from `portals/base_adapter.py`.

---

### Requirement 2: Devex Portal Adapter

**User Story:** As a developer, I want the existing Devex scraping logic consolidated into a single adapter class, so that Devex-specific code is isolated and the orchestrator does not need to know about Playwright sessions.

#### Acceptance Criteria

1. THE `Devex_Adapter` SHALL implement `BasePortalAdapter` and reside in `portals/devex_adapter.py`.
2. WHEN `fetch_opportunities()` is called, THE `Devex_Adapter` SHALL authenticate via `DevexAuth`, collect opportunity URLs via `DevexSearch`, parse each URL via `DevexParser`, and return a list of `Opportunity_Dict` instances.
3. WHEN `DevexAuth` raises `AuthenticationError`, THE `Devex_Adapter` SHALL log the error via `AuditLogger`, send an error alert via `Notifier`, and return an empty list without raising.
4. WHEN a single opportunity URL fails to parse, THE `Devex_Adapter` SHALL log the error and continue processing remaining URLs.
5. THE `Devex_Adapter` SHALL close all Playwright resources in a `finally` block regardless of success or failure.
6. THE `Devex_Adapter.portal_name` SHALL return `"devex"`.

---

### Requirement 3: SAM.gov Portal Adapter

**User Story:** As a developer, I want a SAM.gov adapter that queries the free public REST API, so that US federal procurement opportunities relevant to GovRisk's sectors are discovered without a browser.

#### Acceptance Criteria

1. THE `SAMGov_Adapter` SHALL implement `BasePortalAdapter` and reside in `portals/samgov_adapter.py`.
2. WHEN `fetch_opportunities()` is called, THE `SAMGov_Adapter` SHALL send HTTP GET requests to `https://api.sam.gov/opportunities/v2/search` using the `SAM_GOV_API_KEY` from `Config`.
3. THE `SAMGov_Adapter` SHALL construct query parameters using `Config.sector_keywords` joined as a space-separated string for the `q` parameter, and `Config.max_results` for the `limit` parameter.
4. WHEN the SAM.gov API returns a successful response, THE `SAMGov_Adapter` SHALL map each result to an `Opportunity_Dict` containing at minimum: `opportunity_title`, `funder_organisation`, `country_region`, `deadline`, `contract_value`, `opportunity_link`, `description_snippet`, and a `source_portal` field set to `"samgov"`.
5. WHEN the SAM.gov API returns an HTTP error status, THE `SAMGov_Adapter` SHALL log the error and return an empty list without raising.
6. WHEN `Config.samgov_enabled` is `False`, THE `SAMGov_Adapter` SHALL return an empty list immediately without making any API calls.
7. THE `SAMGov_Adapter.portal_name` SHALL return `"samgov"`.

---

### Requirement 4: Perplexity Portal Adapter

**User Story:** As a developer, I want a Perplexity AI adapter that uses the Perplexity search API to surface procurement opportunities, so that opportunities not indexed on structured portals can be discovered.

#### Acceptance Criteria

1. THE `Perplexity_Adapter` SHALL implement `BasePortalAdapter` and reside in `portals/perplexity_adapter.py`.
2. WHEN `fetch_opportunities()` is called, THE `Perplexity_Adapter` SHALL send a POST request to the Perplexity chat completions API endpoint using the `PERPLEXITY_API_KEY` from `Config` and the model `sonar-pro`.
3. THE `Perplexity_Adapter` SHALL construct a prompt using `Config.sector_keywords` and `Config.target_countries` to request a structured list of current procurement opportunities, leveraging `sonar-pro`'s real-time web search capability.
4. WHEN the Perplexity API returns a valid response, THE `Perplexity_Adapter` SHALL parse the response text into a list of `Opportunity_Dict` instances, each containing at minimum: `opportunity_title`, `funder_organisation`, `country_region`, `deadline`, `opportunity_link`, `description_snippet`, and a `source_portal` field set to `"perplexity"`.
5. WHEN the Perplexity API response cannot be parsed into structured opportunity data, THE `Perplexity_Adapter` SHALL log the parse failure and return an empty list.
6. WHEN the Perplexity API returns an HTTP error status, THE `Perplexity_Adapter` SHALL log the error and return an empty list without raising.
7. WHEN `Config.perplexity_enabled` is `False`, THE `Perplexity_Adapter` SHALL return an empty list immediately without making any API calls.
8. THE `Perplexity_Adapter.portal_name` SHALL return `"perplexity"`.

---

### Requirement 5: Config Extension for New Portals

**User Story:** As an operator, I want SAM.gov and Perplexity credentials and enable flags in `config.py`, so that each portal can be independently activated or deactivated without code changes.

#### Acceptance Criteria

1. THE `Config` dataclass SHALL include a `devex_enabled: bool` field defaulting to `True`.
2. THE `Config` dataclass SHALL include a `samgov_api_key: Optional[str]` field defaulting to `None`.
3. THE `Config` dataclass SHALL include a `samgov_enabled: bool` field defaulting to `False`.
4. THE `Config` dataclass SHALL include a `perplexity_api_key: Optional[str]` field defaulting to `None`.
5. THE `Config` dataclass SHALL include a `perplexity_enabled: bool` field defaulting to `False`.
6. WHEN `samgov_enabled` is `True` and `samgov_api_key` is absent or empty, THE `load_config()` function SHALL raise `ValueError` with a message identifying the missing variable.
7. WHEN `perplexity_enabled` is `True` and `perplexity_api_key` is absent or empty, THE `load_config()` function SHALL raise `ValueError` with a message identifying the missing variable.
8. THE `load_config()` function SHALL read `DEVEX_ENABLED`, `SAM_GOV_API_KEY`, `SAM_GOV_ENABLED`, `PERPLEXITY_API_KEY`, and `PERPLEXITY_ENABLED` from environment variables.
9. THE existing `Config` fields and validation logic SHALL remain unchanged.

---

### Requirement 6: Orchestrator Adapter Loop

**User Story:** As a developer, I want `main.py` to iterate over all active adapters in a uniform loop, so that adding a new portal requires only registering a new adapter instance Ã¢â‚¬â€ not modifying pipeline logic.

#### Acceptance Criteria

1. THE `Orchestrator` SHALL instantiate all adapters whose corresponding `enabled` flag in `Config` is `True`, including `Devex_Adapter` (controlled by `Config.devex_enabled`), `SAMGov_Adapter`, and `Perplexity_Adapter`.
2. WHEN iterating adapters, THE `Orchestrator` SHALL call `adapter.fetch_opportunities()` for each active adapter and collect all returned `Opportunity_Dict` instances into a single unified list.
3. THE `Orchestrator` SHALL apply `KeywordFilter`, duplicate detection, `LLMInterpreter`, and `Store` writes to the unified list using the same logic as the current single-portal pipeline.
4. WHEN an adapter's `fetch_opportunities()` raises an unhandled exception, THE `Orchestrator` SHALL log the error, send an error alert identifying the adapter by `portal_name`, and continue processing remaining adapters.
5. THE `Orchestrator` SHALL include the `source_portal` value from each `Opportunity_Dict` in the audit log entry for that opportunity.
6. THE `Orchestrator` SHALL pass the `source_portal` value from each `Opportunity_Dict` through to the `OpportunityRecord` so that it is persisted as `portal_source` (the external column label) in Google Sheets and as `source_portal` in Airtable.
7. THE `Orchestrator` SHALL deduplicate across adapters using the opportunity's `opportunity_link` as the unique key when a portal-specific ID is unavailable.
8. WHEN the `Orchestrator` initializes cross-run deduplication, THE `Orchestrator` SHALL seed its set of already-persisted opportunities from the `Store`'s persisted `opportunity_link` values obtained via a store method (such as `get_all_links`) that reads the persisted link column (column 7 of the Live_Sheet_Schema), rather than from the `portal_source` column (column 1).

---

### Requirement 7: Import Correctness After Refactor

**User Story:** As a developer, I want all import paths updated to reflect the new module locations, so that the project runs without `ModuleNotFoundError` after the refactor.

#### Acceptance Criteria

1. THE `Orchestrator` SHALL import `Devex_Adapter`, `SAMGov_Adapter`, and `Perplexity_Adapter` from `portals/devex_adapter.py`, `portals/samgov_adapter.py`, and `portals/perplexity_adapter.py` respectively.
2. THE `Orchestrator` SHALL NOT import from `engine/search.py`, `engine/parser.py`, or `portals/devex_auth.py` directly Ã¢â‚¬â€ those imports SHALL be encapsulated inside the respective adapter.
3. WHEN the project is executed with `python main.py`, THE Python interpreter SHALL resolve all imports without raising `ModuleNotFoundError` or `ImportError`.
4. THE `engine/keyword_filter.py`, `llm/interpreter.py`, `store/adapter_sheets.py`, `store/adapter_airtable.py`, `utils/audit.py`, and `utils/notifier.py` modules SHALL remain importable at their existing paths.

---

### Requirement 9: source_portal Persistence in Store Adapters

**User Story:** As an operator, I want the `source_portal` field stored as a column in Google Sheets and Airtable, so that I can filter and audit opportunities by their originating portal.

#### Acceptance Criteria

1. THE `OpportunityRecord` dataclass SHALL include a `source_portal: str` field defaulting to `"devex"` for backward compatibility.
2. THE `OpportunityRecord.to_dict()` method SHALL produce a canonical, round-trippable representation containing all dataclass fields under their internal names - including `source_portal`, `devex_opportunity_id`, `description_snippet`, `matched_keywords`, `relevance_reason`, `llm_confidence`, `llm_called`, `anna_benchmark`, and `scraped_at` - and SHALL NOT contain both `portal_source` and `source_portal`.
3. THE `SheetsAdapter` SHALL preserve the existing 12-column HEADERS (the Live_Sheet_Schema) unchanged, and SHALL NOT add a new column for `source_portal`.
4. WHEN `SheetsAdapter.write_record()` is called, THE `SheetsAdapter` SHALL map the canonical `source_portal` value onto the external `portal_source` column (column 1 of the Live_Sheet_Schema) when writing.
5. WHEN `AirtableAdapter.write_record()` is called, THE `AirtableAdapter` SHALL include `source_portal` in the field payload sent to Airtable.
6. WHERE the Live_Sheet_Schema is in use, THE `SheetsAdapter.get_records_since()` method SHALL be unsupported (documented and deprecated) because the schema contains no `scraped_at` column, and SHALL raise `NotImplementedError`.
7. WHEN reading existing records via `AirtableAdapter.get_records_since()`, THE `AirtableAdapter` SHALL return the `source_portal` value if present, or `"devex"` as a default for legacy records that predate this field.
8. WHEN a `SheetsAdapter` is initialized, THE `SheetsAdapter` SHALL validate the Sheet header row against the canonical 12-column Live_Sheet_Schema before any read or write: IF the Sheet is empty THEN the `SheetsAdapter` SHALL write the canonical 12-column header; IF row 1 is populated THEN the `SheetsAdapter` SHALL reject, by raising a dedicated `SheetsSchemaError`, any missing, duplicate (including duplicates that differ only by surrounding whitespace or letter case), reordered, or unexpected headers, and SHALL NOT rewrite or repair a populated header automatically.
9. THE `SheetsAdapter.get_all_ids()` and `SheetsAdapter.record_exists()` methods SHALL raise `NotImplementedError` because the 12-column Live_Sheet_Schema has no persisted opportunity-ID column (column 1 is `portal_source`); cross-run deduplication SHALL instead use `SheetsAdapter.get_all_links()`. THE corresponding `AirtableAdapter.get_all_ids()` and `AirtableAdapter.record_exists()` methods SHALL remain functional because Airtable persists `devex_opportunity_id`.

---

### Requirement 10: Opportunity ID Normalization Across Portals

**User Story:** As a developer, I want each adapter to produce a consistent, portal-prefixed opportunity ID, so that duplicate detection and audit logging work correctly across all portals.

#### Acceptance Criteria

1. THE `Devex_Adapter` SHALL set `opportunity_id` in each `Opportunity_Dict` using the existing `devex-XXXXXX` format derived from the URL.
2. THE `SAMGov_Adapter` SHALL set `opportunity_id` in each `Opportunity_Dict` using the format `samgov-{noticeId}` where `noticeId` is the SAM.gov API field.
3. THE `Perplexity_Adapter` SHALL set `opportunity_id` in each `Opportunity_Dict` using the format `perplexity-{hash}` where `hash` is a deterministic hash of the `opportunity_link`.
4. WHEN, within a single run, two adapters return an `Opportunity_Dict` with the same `opportunity_id`, THE `Orchestrator` SHALL treat the second occurrence as a within-run duplicate and skip it.
5. WHERE cross-run (persisted-record) deduplication is performed, THE `Orchestrator` SHALL key deduplication on `opportunity_link` rather than `opportunity_id`, because the Live_Sheet_Schema has no persisted `opportunity_id` column.

---

### Requirement 11: UNDP Description Enrichment and Full-Text Keyword Matching

**User Story:** As an operator, I want UNDP opportunities to be keyword-matched against the full detail-page Overview text rather than a truncated snippet, so that relevant opportunities are captured even when the matching keyword appears deep in the description, without leaking transient enrichment data into the store.

#### Acceptance Criteria

1. THE `engine/keyword_filter.py` module SHALL define the constant `MATCHING_TEXT_KEY` with the value `"_matching_text"`.
2. THE `KeywordFilter.get_matching_text(parsed)` method SHALL return a two-tuple of `(normalized_title, normalized_searchable_text)` and SHALL be the sole source of searchable text used by both `KeywordFilter.passes_filter()` and `KeywordFilter.get_matched_keywords()`, so both methods observe identical searchable text for the same `Opportunity_Dict`.
3. WHERE an `Opportunity_Dict` carries a non-empty `_matching_text` value, THE `KeywordFilter.get_matching_text()` method SHALL use that value as the searchable text.
4. IF an `Opportunity_Dict` has no `_matching_text` value or an empty `_matching_text` value, THEN THE `KeywordFilter.get_matching_text()` method SHALL use `description_snippet` as the searchable text.
5. WHEN the `UNDP_Adapter` successfully extracts the Overview text from a detail page, THE `UNDP_Adapter` SHALL set the `_matching_text` field to the complete extracted Overview text with no truncation.
6. WHEN the `UNDP_Adapter` successfully extracts the Overview text from a detail page, THE `UNDP_Adapter` SHALL set `description_snippet` to the extracted Overview text truncated to at most 1000 characters; WHILE a non-empty `_matching_text` is present, THE `description_snippet` value SHALL NOT be used for keyword matching. THE `description_snippet` remains the compatibility/failure fallback used for keyword matching only for unenriched and non-UNDP opportunities (per 11.4).
7. WHEN the `Orchestrator` prepares an opportunity for persistence, THE `Orchestrator` SHALL remove the `_matching_text` and `_full_overview` fields (via `merged.pop`) before constructing the `OpportunityRecord`, so that neither field is written to Google Sheets or Airtable.
8. THE enrichment behavior SHALL NOT introduce any new column or field in the `Store` beyond the fields already defined for `OpportunityRecord`.
9. WHILE the `UNDP_Adapter` performs detail-page fetches, THE `UNDP_Adapter` SHALL bound concurrent detail-page HTTP requests to a maximum of 8 simultaneous requests using an `asyncio.Semaphore`.
10. THE `UNDP_Adapter` SHALL acquire the concurrency semaphore around each individual `client.get()` network attempt only, and SHALL release the semaphore before performing any retry backoff sleep, so that a request awaiting backoff does not hold a concurrency permit.
11. THE `UNDP_Adapter` SHALL perform retry counting and backoff sleeping outside the region protected by the concurrency semaphore.
12. THE `UNDP_Adapter` SHALL attempt each detail-page fetch at most 3 times.
13. WHEN a detail-page fetch encounters a connection error, a timeout, or an HTTP status of 429, 500, 502, 503, or 504, THE `UNDP_Adapter` SHALL treat the failure as retryable and retry until the 3-attempt limit is reached.
14. IF a detail-page fetch returns an HTTP status of 400, 401, 403, or 404, THEN THE `UNDP_Adapter` SHALL treat the failure as permanent and SHALL NOT retry that fetch.
15. WHEN the `UNDP_Adapter` computes a retry backoff delay, THE `UNDP_Adapter` SHALL use exponential backoff of `0.5 * 2^(attempt-1)` seconds plus random jitter, clamped to the remaining Enrichment_Deadline.
16. WHEN a retryable detail-page response includes a `Retry-After` header, THE `UNDP_Adapter` SHALL honor the header in both the delay-seconds format and the HTTP-date (RFC 7231) format, and SHALL clamp the resulting wait to the remaining Enrichment_Deadline.
17. IF a `Retry-After` header value cannot be parsed as either delay-seconds or an HTTP-date, THEN THE `UNDP_Adapter` SHALL fall back to the exponential backoff delay.
18. THE Enrichment_Deadline of 120 seconds SHALL apply only to the Enrichment_Phase and SHALL NOT constrain the listing-page fetch, which SHALL use the shared HTTP client's per-request timeout.
19. THE `UNDP_Adapter` SHALL apply a per-attempt request timeout of 12 seconds to each individual detail-page fetch attempt.
20. WHEN the Enrichment_Deadline is reached, THE `UNDP_Adapter` SHALL cancel all still-pending enrichment tasks and await them via `gather(..., return_exceptions=True)` so that no enrichment task remains pending.
21. WHEN an opportunity's enrichment completes successfully before the Enrichment_Deadline, THE `UNDP_Adapter` SHALL retain that opportunity's enriched `_matching_text` and `description_snippet` values.
22. WHEN an opportunity's enrichment is cancelled or fails, THE `UNDP_Adapter` SHALL fall back to the title-based `description_snippet` for that opportunity and SHALL still pass that opportunity through the `KeywordFilter`.
23. WHEN the Enrichment_Deadline causes one or more enrichment tasks to be cancelled, THE `UNDP_Adapter` SHALL log a warning containing the total, completed, fallback, and cancelled counts. No such warning is emitted when enrichment completes without any cancellation.
24. WHEN `main.run_scraper()` processes the unified deduplicated opportunity list, THE `Orchestrator` SHALL apply the `KeywordFilter` a second time over that list, strip the Transient_Fields, construct an `OpportunityRecord` for each passing opportunity, and — while in live run mode — call `Store.write_record` exactly once per passing opportunity.
25. WHEN an opportunity's only matching sector keyword appears after character 1000 of its `_matching_text` value while its `description_snippet` is exactly 1000 characters and contains no matching keyword, THE `Orchestrator` SHALL pass that opportunity through the `KeywordFilter`, record the correct matched keyword, retain a `description_snippet` of at most 1000 characters, and produce a stored record in which the `_matching_text` and `_full_overview` fields are absent; WHILE in live run mode THE `Orchestrator` SHALL call `Store.write_record` exactly once for that passing opportunity, and WHILE in `dry_run` mode THE `Orchestrator` SHALL NOT call `Store.write_record` for that opportunity.
