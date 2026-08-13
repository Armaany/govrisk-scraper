# Implementation Plan: Multi-Portal Adapter Architecture

## Overview

Refactor the GovRisk scraper from a single tightly-coupled Devex pipeline into a generic
multi-portal adapter architecture. Each portal is encapsulated as a self-contained adapter
implementing `BasePortalAdapter`. The orchestrator iterates over all active adapters uniformly.
Two new adapters are added (SAM.gov, Perplexity). `source_portal` is persisted in both stores.

## Tasks

- [x] 1. Extend Config with new portal fields and validation
  - Add `devex_enabled: bool = True`, `samgov_api_key: Optional[str] = None`,
    `samgov_enabled: bool = False`, `perplexity_api_key: Optional[str] = None`,
    `perplexity_enabled: bool = False` to the `Config` dataclass in `config.py`
  - Add `load_config()` reads for `DEVEX_ENABLED`, `SAM_GOV_API_KEY`, `SAM_GOV_ENABLED`,
    `PERPLEXITY_API_KEY`, `PERPLEXITY_ENABLED` using existing `_parse_bool_env` helpers
  - Add validation: raise `ValueError` when `samgov_enabled=True` and `samgov_api_key` is
    absent/empty; same for `perplexity_enabled` / `perplexity_api_key`
  - Update `.env.example` with `DEVEX_ENABLED`, `SAM_GOV_API_KEY`, `SAM_GOV_ENABLED`,
    `PERPLEXITY_API_KEY`, `PERPLEXITY_ENABLED` entries (with safe placeholder values)
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9_

  - [ ]* 1.1 Write property test for Config credential validation
    - **Property 8: Config credential validation raises on enabled-but-missing key**
    - **Validates: Requirements 5.6, 5.7**

- [x] 2. Add source_portal field to OpportunityRecord
  - Add `source_portal: str = "devex"` field to `OpportunityRecord` dataclass in `models.py`
    (place after `anna_benchmark` for minimal diff)
  - Update `to_dict()` to include `"source_portal": self.source_portal`
  - Update `from_dict()` to read `source_portal=str(data.get("source_portal", "devex"))`
  - _Requirements: 9.1, 9.2_

  - [ ]* 2.1 Write property test for source_portal round-trip
    - **Property 2: source_portal identity preservation**
    - **Validates: Requirements 6.6, 9.1, 9.2**

- [x] 3. Create BasePortalAdapter abstract base class
  - Create `portals/base_adapter.py` with `BasePortalAdapter(ABC)` class
  - Define `__init__(self, config: Config)` storing `self.config`
  - Define abstract property `portal_name -> str`
  - Define abstract async method `fetch_opportunities() -> list[dict]`
  - Add helper methods `_log_error`, `_log_http_error`, `_log_parse_error`,
    `_log_auth_error` that delegate to `AuditLogger` and `Notifier` (accept both as
    constructor args alongside `config`, or instantiate internally)
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 3.1 Write unit test for BasePortalAdapter ABC enforcement
    - Verify that instantiating a subclass missing `portal_name` or `fetch_opportunities`
      raises `TypeError`
    - _Requirements: 1.3_

- [x] 4. Implement DevexAdapter
  - Create `portals/devex_adapter.py` with `DevexAdapter(BasePortalAdapter)`
  - Set `portal_name = "devex"`
  - Implement `fetch_opportunities()`: instantiate `DevexAuth`, call `load_session()`,
    create `DevexSearch` and `DevexParser`, collect URLs, parse each URL in a try/except,
    remap `devex_opportunity_id` → `opportunity_id`, set `source_portal = "devex"`
  - Catch `AuthenticationError`: log via `AuditLogger`, alert via `Notifier`, return `[]`
  - Close all Playwright resources in `finally` block unconditionally
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 7.1, 7.2_

  - [ ]* 4.1 Write unit tests for DevexAdapter error paths
    - Test `AuthenticationError` path: assert `[]` returned, audit + notifier called
    - Test per-URL parse failure: assert partial results returned, loop continues
    - Test Playwright `finally` close: assert `auth.close()` called even when exception raised
    - _Requirements: 2.3, 2.4, 2.5_

  - [ ]* 4.2 Write property test for Devex opportunity_id format
    - **Property 11: Devex opportunity_id matches portal-prefixed format**
    - **Validates: Requirements 10.1**

- [x] 5. Implement SAMGovAdapter with LATAM post-filter
  - Create `portals/samgov_adapter.py` with `SAMGovAdapter(BasePortalAdapter)`
  - Set `portal_name = "samgov"` and `BASE_URL = "https://api.sam.gov/opportunities/v2/search"`
  - Implement `fetch_opportunities()`: guard on `samgov_enabled`, build params dict with
    `api_key`, `q` (space-joined `sector_keywords`), `limit`, `postedFrom` (30 days ago),
    make async `httpx` GET, raise on HTTP error, post-filter with `_is_latam_relevant()`,
    map results with `_map_result()`
  - Implement `_is_latam_relevant(item)`: check `placeOfPerformance.country.name`,
    `placeOfPerformance.state.name`, and `description` against `Config.target_countries`
    (case-insensitive substring match); return `True` if any match, `True` if no target
    countries configured
  - Implement `_map_result(item)`: map `noticeId`, `title`, `organizationName`,
    `responseDeadLine`, `placeOfPerformance`, `award.amount`, `description` to
    `Opportunity_Dict`; set `opportunity_id = f"samgov-{noticeId}"`,
    `source_portal = "samgov"`
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 7.1_

  - [ ]* 5.1 Write unit tests for SAMGovAdapter guard and HTTP error paths
    - Test `samgov_enabled=False` returns `[]` without making HTTP calls
    - Test HTTP 4xx/5xx: assert `[]` returned, error logged
    - _Requirements: 3.5, 3.6_

  - [ ]* 5.2 Write property test for SAMGovAdapter query params
    - **Property 9: SAM.gov query params reflect Config values**
    - **Validates: Requirements 3.3**

  - [ ]* 5.3 Write property test for SAM.gov opportunity_id format
    - **Property 12: SAM.gov opportunity_id matches portal-prefixed format**
    - **Validates: Requirements 10.2**

  - [ ]* 5.4 Write property test for LATAM post-filter
    - **Property 16: SAMGovAdapter LATAM post-filter excludes non-target countries**
    - **Validates: Requirements 3.4, 3.3**

- [x] 6. Implement PerplexityAdapter
  - Create `portals/perplexity_adapter.py` with `PerplexityAdapter(BasePortalAdapter)`
  - Set `portal_name = "perplexity"` and `PERPLEXITY_API_URL`
  - Implement `fetch_opportunities()`: guard on `perplexity_enabled`, build prompt via
    `_build_prompt()`, POST to Perplexity API with `sonar-pro` model, handle HTTP errors,
    parse response via `_parse_response()`
  - Implement `_build_prompt()`: embed `sector_keywords`, `target_countries`, `max_results`;
    request JSON array with exact keys matching `Opportunity_Dict` contract
  - Implement `_parse_response()`: strip markdown fences, `json.loads`, map each item to
    `Opportunity_Dict` with `opportunity_id = f"perplexity-{_deterministic_hash(link)}"`,
    `source_portal = "perplexity"`; catch all exceptions, log, return `[]`
  - Implement `_deterministic_hash(link)`: `hashlib.sha256(link.encode()).hexdigest()[:12]`
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 7.1_

  - [ ]* 6.1 Write unit tests for PerplexityAdapter guard and error paths
    - Test `perplexity_enabled=False` returns `[]` without HTTP calls
    - Test HTTP error returns `[]` with error logged
    - Test unparseable JSON response returns `[]` with parse failure logged
    - _Requirements: 4.5, 4.6, 4.7_

  - [ ]* 6.2 Write property test for Perplexity prompt content
    - **Property 10: Perplexity prompt contains all configured keywords and countries**
    - **Validates: Requirements 4.3**

  - [ ]* 6.3 Write property test for Perplexity opportunity_id determinism
    - **Property 13: Perplexity opportunity_id is deterministic**
    - **Validates: Requirements 10.3**

- [x] 7. Checkpoint — Ensure all adapter unit and property tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Update SheetsAdapter to persist source_portal
  - Add `"source_portal"` to the end of `HEADERS` list in `store/adapter_sheets.py`
  - `write_record()` already uses `payload.get(header)` for each header — no logic change
    needed beyond the HEADERS addition
  - Update `get_records_since()` to call `row.setdefault("source_portal", "devex")` before
    appending each row to results
  - _Requirements: 9.3, 9.4, 9.6_

  - [ ]* 8.1 Write property test for SheetsAdapter source_portal column position
    - **Property 14: SheetsAdapter writes source_portal at correct column position**
    - **Validates: Requirements 9.3, 9.4**

  - [ ]* 8.2 Write property test for legacy source_portal default
    - **Property 15: Store get_records_since returns "devex" default for legacy rows**
    - **Validates: Requirements 9.6**

- [x] 9. Update AirtableAdapter to persist source_portal
  - Update `get_records_since()` in `store/adapter_airtable.py` to call
    `fields.setdefault("source_portal", "devex")` before appending each record to results
  - `write_record()` already calls `record.to_dict()` and passes the full payload — no
    structural change needed; `source_portal` flows through automatically once `to_dict()`
    includes it (Task 2)
  - _Requirements: 9.5, 9.6_

- [x] 10. Refactor main.py orchestrator with adapter registry and isolated exception handling
  - Replace all existing imports from `auth/`, `scraper/` with imports from `portals/`
  - Import `BasePortalAdapter`, `DevexAdapter`, `SAMGovAdapter`, `PerplexityAdapter`
  - Build adapter registry: append each adapter only when its `enabled` flag is `True`
  - Implement unified adapter loop: each `adapter.fetch_opportunities()` call wrapped in its
    own `try/except Exception` block; on exception: increment `errors`, call
    `audit.log_error()`, call `notifier.send_error_alert(component=adapter.portal_name)`,
    then `continue` — never `return` or `raise`
  - Collect all results into `all_opportunities: list[dict]`
  - Implement deduplication: check `opportunity_id` against `existing_ids` and `seen_ids`;
    check `opportunity_link` against `seen_links` as fallback
  - Thread `source_portal` through `Opportunity_Dict` → `OpportunityRecord.from_dict()` →
    store write; include `source_portal` in `audit.log()` detail for each processed record
  - Preserve existing filter → LLM → store pipeline logic unchanged
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 10.1 Write property test for adapter registry composition
    - **Property 5: Adapter registry contains exactly the enabled adapters**
    - **Validates: Requirements 6.1**

  - [ ]* 10.2 Write property test for unified list union
    - **Property 6: Unified list is the union of all adapter results**
    - **Validates: Requirements 6.2**

  - [ ]* 10.3 Write property test for failing adapter isolation
    - **Property 7: Failing adapters do not suppress results from healthy adapters**
    - **Validates: Requirements 6.4**

  - [ ]* 10.4 Write property test for deduplication by opportunity_id
    - **Property 3: Deduplication eliminates repeated opportunity_id values**
    - **Validates: Requirements 6.7, 10.4**

  - [ ]* 10.5 Write property test for deduplication by opportunity_link
    - **Property 4: Deduplication eliminates repeated opportunity_link values**
    - **Validates: Requirements 6.7**

- [x] 11. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Write property test for adapter result field completeness
  - Add `test_adapter_result_fields_complete` to the test suite
  - Mock HTTP responses for `SAMGovAdapter` and `PerplexityAdapter`; mock Playwright for
    `DevexAdapter`; assert every returned dict contains all required `Opportunity_Dict` keys
  - **Property 1: Adapter result fields are complete**
  - **Validates: Requirements 3.4, 4.4**

- [x] 13. Final checkpoint — Ensure all tests pass and imports resolve
  - Run `python -c "import main"` to verify no `ModuleNotFoundError` or `ImportError`
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests use Hypothesis; tag format: `# Feature: multi-portal-adapter-architecture, Property N: <text>`
- The per-adapter `try/except Exception` in the orchestrator loop is a hard requirement — see design Error Handling section
- `_is_latam_relevant()` in `SAMGovAdapter` is required because SAM.gov returns global results
