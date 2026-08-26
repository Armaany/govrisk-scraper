# Technical Design: Multi-Portal Adapter Architecture

## Overview

This design refactors GovRisk's scraper from a single tightly-coupled Devex pipeline into a
generic multi-portal adapter architecture. Each procurement portal is encapsulated as a
self-contained adapter implementing a shared abstract interface (`BasePortalAdapter`). The
orchestrator in `main.py` iterates over all active adapters uniformly, feeding results through
the existing keyword filter → LLM interpreter → store pipeline unchanged.

Two new adapters are added alongside the refactored Devex adapter:
- **SAMGov_Adapter** — queries the free `api.sam.gov` REST API
- **Perplexity_Adapter** — uses Perplexity `sonar-pro` for real-time web search discovery

The `OpportunityRecord` model gains a `source_portal` field, persisted as a column in both
Google Sheets and Airtable. Opportunity IDs are portal-prefixed to guarantee uniqueness across
sources.

---

## Architecture

```mermaid
flowchart TD
    subgraph Orchestrator["main.py — Orchestrator"]
        REG[Adapter Registry\ndevex / samgov / perplexity]
        LOOP[Unified Adapter Loop]
        DEDUP[Deduplication\nopportunity_id + opportunity_link]
        FILTER[KeywordFilter]
        LLM[LLMInterpreter]
        STORE[Store\nSheetsAdapter | AirtableAdapter]
        AUDIT[AuditLogger]
        NOTIFY[Notifier]
    end

    subgraph Portals["portals/"]
        BASE[BasePortalAdapter\nABC]
        DEV[DevexAdapter\nPlaywright + BeautifulSoup]
        SAM[SAMGovAdapter\nhttpx REST]
        PERP[PerplexityAdapter\nhttpx + sonar-pro]
    end

    CONFIG[Config\nconfig.py] --> REG
    BASE --> DEV
    BASE --> SAM
    BASE --> PERP
    DEV --> LOOP
    SAM --> LOOP
    PERP --> LOOP
    REG --> LOOP
    LOOP --> DEDUP
    DEDUP --> FILTER
    FILTER --> LLM
    LLM --> STORE
    LOOP --> AUDIT
    LOOP --> NOTIFY
```

**Data flow per adapter:**
1. `load_config()` reads enabled flags and credentials
2. Orchestrator builds adapter registry — only enabled adapters are instantiated
3. Each adapter's `fetch_opportunities()` returns `list[Opportunity_Dict]`
4. All results are merged into a single unified list
5. Deduplication removes repeated `opportunity_id` values (and `opportunity_link` as fallback)
6. `KeywordFilter` applies sector + geography checks
7. `LLMInterpreter` enriches matched opportunities via Claude
8. Store adapter writes `OpportunityRecord` (including `source_portal`) to Sheets or Airtable
9. `AuditLogger` records each event; `Notifier` sends completion/error emails

---

## Module Structure

### New files

| Path | Purpose |
|---|---|
| `portals/base_adapter.py` | `BasePortalAdapter` abstract base class |
| `portals/devex_adapter.py` | `DevexAdapter` — wraps existing auth + search + parser |
| `portals/samgov_adapter.py` | `SAMGovAdapter` — httpx REST client for api.sam.gov |
| `portals/perplexity_adapter.py` | `PerplexityAdapter` — httpx POST to Perplexity sonar-pro |

### Modified files

| Path | Change |
|---|---|
| `config.py` | Add `devex_enabled`, `samgov_api_key`, `samgov_enabled`, `perplexity_api_key`, `perplexity_enabled` fields + validation |
| `models.py` | Add `source_portal: str` field to `OpportunityRecord`; update `to_dict()` and `from_dict()` |
| `main.py` | Replace single-portal pipeline with adapter registry loop |
| `store/adapter_sheets.py` | Add `"source_portal"` to `HEADERS`; update `write_record()` and `get_records_since()` |
| `store/adapter_airtable.py` | Update `write_record()` and `get_records_since()` to handle `source_portal` |

### Unchanged files

`engine/keyword_filter.py`, `engine/search.py`, `engine/parser.py`, `portals/devex_auth.py`,
`llm/interpreter.py`, `llm/validator.py`, `utils/audit.py`, `utils/notifier.py`

---

## Components and Interfaces

### BasePortalAdapter (`portals/base_adapter.py`)

```python
from abc import ABC, abstractmethod
from config import Config

class BasePortalAdapter(ABC):
    """Abstract base class all portal adapters must implement."""

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    @abstractmethod
    def portal_name(self) -> str:
        """Human-readable portal identifier, e.g. 'devex', 'samgov', 'perplexity'."""
        ...

    @abstractmethod
    async def fetch_opportunities(self) -> list[dict]:
        """Fetch and return normalized Opportunity_Dict instances from this portal."""
        ...
```

The `Opportunity_Dict` contract (plain `dict`) that every adapter must populate:

| Key | Type | Notes |
|---|---|---|
| `opportunity_id` | `str` | Portal-prefixed unique ID |
| `opportunity_title` | `str \| None` | |
| `funder_organisation` | `str \| None` | |
| `country_region` | `str \| None` | |
| `deadline` | `str \| None` | ISO date string or raw text |
| `contract_value` | `str \| None` | |
| `opportunity_link` | `str` | Canonical URL |
| `description_snippet` | `str \| None` | First ~500 chars |
| `source_portal` | `str` | `"devex"`, `"samgov"`, or `"perplexity"` |
| `matched_keywords` | `list[str]` | Populated by `KeywordFilter`, empty initially |

---

### DevexAdapter (`portals/devex_adapter.py`)

Wraps the existing `DevexAuth`, `DevexSearch`, and `DevexParser` classes. No changes to those
classes are required.

```python
class DevexAdapter(BasePortalAdapter):
    portal_name = "devex"

    async def fetch_opportunities(self) -> list[dict]:
        auth = DevexAuth(self.config)
        try:
            page = await auth.load_session()
            search = DevexSearch(self.config, page)
            parser = DevexParser(self.config, page)
            urls = await search.collect_opportunity_urls()
            results = []
            for url in urls:
                try:
                    parsed = await parser.parse_opportunity(url)
                    parsed["opportunity_id"] = parsed.pop("devex_opportunity_id", "devex-unknown")
                    parsed["source_portal"] = "devex"
                    results.append(parsed)
                except Exception as exc:
                    self._log_and_continue(exc, url)
            return results
        except AuthenticationError as exc:
            self._log_auth_error(exc)
            return []
        finally:
            await auth.close()
```

**Error handling:**
- `AuthenticationError` → log via `AuditLogger`, alert via `Notifier`, return `[]`
- Per-URL parse failure → log and continue; partial results returned
- All Playwright resources closed in `finally` block unconditionally

---

### SAMGovAdapter (`portals/samgov_adapter.py`)

Uses `httpx` (sync or async) to query the SAM.gov v2 opportunities search endpoint.

```python
class SAMGovAdapter(BasePortalAdapter):
    BASE_URL = "https://api.sam.gov/opportunities/v2/search"
    portal_name = "samgov"

    async def fetch_opportunities(self) -> list[dict]:
        if not self.config.samgov_enabled:
            return []
        params = {
            "api_key": self.config.samgov_api_key,
            "q": " ".join(self.config.sector_keywords),
            "limit": self.config.max_results,
            "postedFrom": _thirty_days_ago(),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(self.BASE_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._log_http_error(exc)
                return []
        # SAM.gov returns global results — post-filter to LATAM-relevant opportunities only
        raw_items = response.json().get("opportunitiesData", [])
        latam_items = [item for item in raw_items if self._is_latam_relevant(item)]
        return [self._map_result(item) for item in latam_items]

    def _map_result(self, item: dict) -> dict:
        notice_id = item.get("noticeId", "")
        country_name = item.get("placeOfPerformance", {}).get("country", {}).get("name") or ""
        return {
            "opportunity_id": f"samgov-{notice_id}",
            "opportunity_title": item.get("title"),
            "funder_organisation": item.get("organizationName"),
            "country_region": country_name,
            "deadline": item.get("responseDeadLine"),
            "contract_value": item.get("award", {}).get("amount"),
            "opportunity_link": f"https://sam.gov/opp/{notice_id}/view",
            "description_snippet": (item.get("description") or "")[:500],
            "source_portal": "samgov",
            "matched_keywords": [],
        }

    def _is_latam_relevant(self, item: dict) -> bool:
        """Return True when the opportunity's place-of-performance matches a target country.

        SAM.gov returns global results. This post-filter restricts results to opportunities
        whose ``country_region`` (derived from ``placeOfPerformance``) contains at least one
        value from ``Config.target_countries``. The check is case-insensitive and uses
        substring matching so that partial names (e.g. ``"colombia"`` matching
        ``"Colombia, South America"``) are handled correctly.

        The ``description`` field is also checked as a fallback for opportunities where
        ``placeOfPerformance`` is absent or empty.
        """
        target = [c.lower() for c in (self.config.target_countries or [])]
        if not target:
            return True  # no filter configured — pass everything through

        pop = item.get("placeOfPerformance", {})
        country_name = (pop.get("country", {}).get("name") or "").lower()
        state_name = (pop.get("state", {}).get("name") or "").lower()
        description = (item.get("description") or "").lower()

        for country in target:
            if country in country_name or country in state_name or country in description:
                return True
        return False
```

`fetch_opportunities()` applies `_is_latam_relevant()` as a post-filter before returning
results:

```python
    async def fetch_opportunities(self) -> list[dict]:
        if not self.config.samgov_enabled:
            return []
        params = {
            "api_key": self.config.samgov_api_key,
            "q": " ".join(self.config.sector_keywords),
            "limit": self.config.max_results,
            "postedFrom": _thirty_days_ago(),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(self.BASE_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._log_http_error(exc)
                return []
        raw_items = response.json().get("opportunitiesData", [])
        latam_items = [item for item in raw_items if self._is_latam_relevant(item)]
        return [self._map_result(item) for item in latam_items]
```

**SAM.gov API notes:**
- Endpoint: `GET https://api.sam.gov/opportunities/v2/search`
- Auth: `api_key` query parameter (free public key, rate-limited)
- Key response fields: `noticeId`, `title`, `organizationName`, `responseDeadLine`,
  `placeOfPerformance`, `description`
- No Playwright dependency; pure HTTP

---

### PerplexityAdapter (`portals/perplexity_adapter.py`)

Uses Perplexity's OpenAI-compatible chat completions API with model `sonar-pro`, which has
real-time web search built in.

```python
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

class PerplexityAdapter(BasePortalAdapter):
    portal_name = "perplexity"

    async def fetch_opportunities(self) -> list[dict]:
        if not self.config.perplexity_enabled:
            return []
        prompt = self._build_prompt()
        headers = {
            "Authorization": f"Bearer {self.config.perplexity_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(PERPLEXITY_API_URL, json=payload, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                self._log_http_error(exc)
                return []
        return self._parse_response(response.json())

    def _build_prompt(self) -> str:
        keywords = ", ".join(self.config.sector_keywords)
        countries = ", ".join(self.config.target_countries)
        return (
            f"Search for current open procurement opportunities, tenders, and grants "
            f"related to: {keywords}. Focus on opportunities in: {countries}. "
            f"Return a JSON array of up to {self.config.max_results} opportunities. "
            f"Each object must have these exact keys: opportunity_title, funder_organisation, "
            f"country_region, deadline (ISO date or null), opportunity_link (URL), "
            f"description_snippet (max 500 chars). Return ONLY the JSON array, no other text."
        )

    def _parse_response(self, data: dict) -> list[dict]:
        try:
            text = data["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            items = json.loads(text)
            results = []
            for item in items:
                link = item.get("opportunity_link", "")
                results.append({
                    "opportunity_id": f"perplexity-{_deterministic_hash(link)}",
                    "source_portal": "perplexity",
                    "matched_keywords": [],
                    **{k: item.get(k) for k in [
                        "opportunity_title", "funder_organisation", "country_region",
                        "deadline", "opportunity_link", "description_snippet",
                    ]},
                })
            return results
        except Exception as exc:
            self._log_parse_error(exc)
            return []
```

**Prompt design rationale:** Requesting a JSON array directly from `sonar-pro` avoids a
secondary parsing step. The prompt specifies exact key names matching the `Opportunity_Dict`
contract. `sonar-pro` performs real-time web search before generating the response, surfacing
opportunities not indexed on structured portals.

**Deterministic hash:** `hashlib.sha256(link.encode()).hexdigest()[:12]` — short enough to be
readable, collision-resistant for the expected volume.

---

### UNDPAdapter (`portals/undp_adapter.py`)

The `UNDPAdapter` scrapes the UNDP Procurement Notices portal
(`https://procurement-notices.undp.org`). Unlike the listing-only adapters, UNDP listing rows
carry almost no descriptive text — the useful sector/geography keywords live in the **Overview**
section of each opportunity's *detail page*. The adapter therefore performs a second,
concurrency-bounded enrichment pass that fetches each active detail page and extracts its full
Overview text for keyword matching, while keeping only a truncated snippet for display/storage.

#### Authoritative matching-text contract (`engine/keyword_filter.py`)

The enrichment feature relies on a single, authoritative source of searchable text so that
`passes_filter()` and `get_matched_keywords()` can never disagree:

- `engine/keyword_filter.py` defines the constant `MATCHING_TEXT_KEY = "_matching_text"`.
- A single helper `KeywordFilter.get_matching_text(parsed) -> (normalized_title,
  normalized_searchable_text)` is the **sole** searchable-text source for both
  `passes_filter()` and `get_matched_keywords()`. Both methods call this helper and therefore
  always observe identical searchable text for the same `Opportunity_Dict`.
- When `parsed["_matching_text"]` is present and non-empty, it is used as the searchable text.
  Otherwise the helper falls back to `description_snippet`. This keeps behavior fully
  backward-compatible for every non-UNDP adapter (which never set `_matching_text`).

```python
MATCHING_TEXT_KEY = "_matching_text"

class KeywordFilter:
    @staticmethod
    def get_matching_text(parsed: dict) -> tuple[str, str]:
        title = _normalize(parsed.get("opportunity_title") or "")
        explicit = parsed.get(MATCHING_TEXT_KEY)
        if explicit:
            searchable = _normalize(explicit)
        else:
            searchable = _normalize(parsed.get("description_snippet") or "")
        return title, searchable
```

Because `passes_filter()` and `get_matched_keywords()` both derive their searchable text from
`get_matching_text()`, a keyword that appears only deep in the full Overview (e.g. after
character 1000) is matched exactly the same way by both methods.

#### Full text vs. display snippet

On a successful detail-page extraction, the adapter sets **two distinct fields**:

| Field | Value | Used for |
|---|---|---|
| `_matching_text` | Full, **untruncated** Overview text extracted from the detail page | Keyword matching only (transient) |
| `description_snippet` | Overview truncated to at most `_DESCRIPTION_DISPLAY_MAX` (1000) chars | Display/storage value; used as the compatibility/failure matching fallback only when `_matching_text` is absent or empty |

```python
_DESCRIPTION_DISPLAY_MAX = 1000  # display/storage truncation (matching fallback only when _matching_text absent/empty)
...
opp[MATCHING_TEXT_KEY] = overview                       # full text — matching
opp["description_snippet"] = overview[:_DESCRIPTION_DISPLAY_MAX]  # truncated — display
```

#### Transient-field removal (no new store column)

`_matching_text` (and the reserved `_full_overview`) are **transient**: they exist only in
memory during a run. `main.run_scraper()` strips them with `merged.pop("_matching_text", None)`
and `merged.pop("_full_overview", None)` **before** constructing `OpportunityRecord.from_dict()`
and calling `store.write_record()`. As a result, neither field ever reaches Google Sheets or
Airtable, and **no new store column or `OpportunityRecord` field is introduced** by this feature.

#### Bounded concurrency

Detail-page fetches are bounded by an `asyncio.Semaphore` with a maximum of
`_MAX_CONCURRENT_DETAIL_FETCHES = 8` simultaneous in-flight requests.

#### Per-network-attempt semaphore (permits are not held during backoff)

The semaphore is acquired **only** around each individual `client.get()` attempt inside
`_fetch_detail_with_retry`, and is **released before any retry backoff sleep**. Retry counting
and backoff sleeping happen *outside* the semaphore-protected region.

```python
for attempt in range(1, _MAX_ATTEMPTS + 1):
    remaining = max(0, deadline - loop.time()) if deadline else 999
    if remaining <= 0:
        return None
    try:
        async with semaphore:            # permit held ONLY around the network call
            resp = await client.get(url)
        # ... classify status ...
        if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS:
            wait = _parse_retry_after(resp.headers.get("Retry-After", ""), remaining) \
                   or min(_BASE_BACKOFF * 2 ** (attempt - 1) + jitter, remaining)
            await asyncio.sleep(wait)     # OUTSIDE the semaphore — permit already released
            continue
    except (httpx.ConnectError, httpx.TimeoutException):
        await asyncio.sleep(backoff)      # OUTSIDE the semaphore
```

**Rationale:** a throttled request that is sleeping through its backoff must not hold a
concurrency permit; otherwise a few slow/429'd requests would starve the remaining
opportunities and waste the enrichment budget.

#### Retry policy

- `_MAX_ATTEMPTS = 3` — each detail page is attempted at most three times.
- **Retryable** failures: `httpx.ConnectError`, `httpx.TimeoutException`, and HTTP status codes
  in `_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}`. These retry until the 3-attempt
  limit is reached.
- **Permanent** failures: HTTP status codes in `_PERMANENT_FAIL_STATUS_CODES =
  {400, 401, 403, 404}` — a single attempt, no retry.
- **Backoff:** exponential `_BASE_BACKOFF (0.5) * 2 ** (attempt - 1)` seconds plus random
  jitter, clamped to the remaining enrichment deadline.

#### Retry-After handling

`_parse_retry_after(value, remaining_deadline)` honors the `Retry-After` header in **both**
supported formats and clamps the result to the remaining deadline:

- **delay-seconds** — parsed via `float(value)`, returns `min(delay, remaining_deadline)`.
- **HTTP-date (RFC 7231)** — parsed via `email.utils.parsedate_to_datetime`, converted to a
  non-negative delta and clamped to `remaining_deadline`.
- **unparseable** — returns `0`, which causes the caller to fall back to exponential backoff.

#### Deadline scope and per-attempt timeout

- `_DETAIL_ENRICHMENT_DEADLINE = 120` seconds applies **only** to the enrichment phase. It does
  **not** constrain the listing-page fetch, which relies on the shared client's own per-request
  timeout.
- `_DETAIL_REQUEST_TIMEOUT = 12` seconds is the per-attempt timeout applied to each individual
  detail-page fetch (and configured on the shared client).

#### Partial-result / cancellation policy

Enrichment uses `asyncio.wait(tasks, timeout=remaining)`:

```python
done, pending = await asyncio.wait(tasks, timeout=remaining_time)
for task in pending:
    task.cancel()
if pending:
    await asyncio.gather(*pending, return_exceptions=True)  # no task remains pending
```

- Pending tasks are **cancelled and then awaited** via `asyncio.gather(..., return_exceptions=
  True)`, so no enrichment task is left pending after the phase ends.
- Opportunities whose enrichment **completed** before the deadline keep their enriched
  `_matching_text` and truncated `description_snippet`.
- Opportunities that were **cancelled or failed** fall back to their title-based
  `description_snippet` (set at parse time) and are **still passed through the
  `KeywordFilter`** — a deadline never silently drops an opportunity, it only downgrades its
  matching text.
- A warning is logged with the `total`, `completed`, `fallback`, and `cancelled` counts.

#### Overview extraction

`_extract_overview_from_detail(html)` uses a two-tier strategy and returns the **full** text
(no truncation):

1. **Heading-based (primary):** find the `postContent` div whose `<h2>` heading contains
   "Overview" and return its text (heading stripped).
2. **Longest-block fallback:** if no Overview heading is found, select the longest `postContent`
   block and emit a `logger.warning`.

#### Shared HTTP client

A **single** `httpx.AsyncClient` (configured with the 12s timeout and a browser User-Agent) is
used for both the listing fetch and all detail fetches, so connections are pooled across the
entire adapter run.

---

## Data Models

### OpportunityRecord changes (`models.py`)

Add `source_portal` field with backward-compatible default:

```python
@dataclass
class OpportunityRecord:
    # ... existing fields unchanged ...
    source_portal: str = "devex"   # NEW — defaults to "devex" for legacy records

    def to_dict(self) -> dict[str, Any]:
        return {
            # ... existing keys unchanged ...
            "source_portal": self.source_portal,   # NEW
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpportunityRecord":
        return cls(
            # ... existing fields unchanged ...
            source_portal=str(data.get("source_portal", "devex")),   # NEW
        )
```

The `devex_opportunity_id` field is retained for backward compatibility with existing Sheets/
Airtable rows. New records from non-Devex adapters will have `devex_opportunity_id` set to the
portal-prefixed `opportunity_id` value (e.g. `samgov-ABC123`).

### Config changes (`config.py`)

```python
@dataclass
class Config:
    # ... existing fields unchanged ...
    devex_enabled: bool = True                    # NEW
    samgov_api_key: Optional[str] = None          # NEW
    samgov_enabled: bool = False                  # NEW
    perplexity_api_key: Optional[str] = None      # NEW
    perplexity_enabled: bool = False              # NEW
```

`load_config()` additions:

```python
devex_enabled = _parse_bool_env("DEVEX_ENABLED", True)
samgov_enabled = _parse_bool_env("SAM_GOV_ENABLED", False)
samgov_api_key = os.getenv("SAM_GOV_API_KEY", "").strip() or None
perplexity_enabled = _parse_bool_env("PERPLEXITY_ENABLED", False)
perplexity_api_key = os.getenv("PERPLEXITY_API_KEY", "").strip() or None

if samgov_enabled and not samgov_api_key:
    raise ValueError("Missing required environment variable: SAM_GOV_API_KEY")
if perplexity_enabled and not perplexity_api_key:
    raise ValueError("Missing required environment variable: PERPLEXITY_API_KEY")
```

---

## Orchestrator Refactor (`main.py`)

### Adapter registry pattern

```python
async def run_scraper():
    config = load_config()
    audit = AuditLogger()
    notifier = Notifier(config)

    # Build adapter registry — only enabled adapters
    adapters: list[BasePortalAdapter] = []
    if config.devex_enabled:
        adapters.append(DevexAdapter(config))
    if config.samgov_enabled:
        adapters.append(SAMGovAdapter(config))
    if config.perplexity_enabled:
        adapters.append(PerplexityAdapter(config))

    store = SheetsAdapter(config) if config.store_type == "sheets" else AirtableAdapter(config)
    existing_ids: set[str] = set(store.get_all_ids())
    keyword_filter = KeywordFilter(config)
    interpreter = LLMInterpreter(config)

    all_opportunities: list[dict] = []

    # Unified adapter loop
    for adapter in adapters:
        try:
            results = await adapter.fetch_opportunities()
            all_opportunities.extend(results)
            audit.log(event_type="adapter_complete",
                      detail=f"portal={adapter.portal_name} results={len(results)}")
        except Exception as exc:
            audit.log_error(str(exc))
            notifier.send_error_alert(str(exc), component=adapter.portal_name)

    # Deduplication across all adapters
    seen_ids: set[str] = set()
    seen_links: set[str] = set()
    deduplicated: list[dict] = []
    for opp in all_opportunities:
        opp_id = opp.get("opportunity_id", "")
        opp_link = opp.get("opportunity_link", "")
        if opp_id in existing_ids or opp_id in seen_ids:
            duplicates_skipped += 1
            continue
        if opp_link and opp_link in seen_links:
            duplicates_skipped += 1
            continue
        seen_ids.add(opp_id)
        if opp_link:
            seen_links.add(opp_link)
        deduplicated.append(opp)

    # Filter → LLM → Store pipeline (unchanged logic)
    for opp in deduplicated:
        # ... keyword filter, LLM, store write, audit log ...
        # source_portal flows through to OpportunityRecord.from_dict()
```

**Key design decisions:**
- Adapter errors are caught at the loop level — one failing adapter never blocks others
- Deduplication uses `opportunity_id` first, then `opportunity_link` as fallback
- `source_portal` is carried through `Opportunity_Dict` → `OpportunityRecord` → store write
- The filter/LLM/store pipeline code is identical to the current implementation

---

### Transient-field strip and second-pass filtering (UNDP enrichment)

`run_scraper()` applies the `KeywordFilter` a **second time** over the unified, deduplicated
list (the first application happens inside `UNDPAdapter` itself). Applying it over the unified
list keeps orchestration uniform across all adapters — the orchestrator does not need to know
which adapters enrich their text.

For each opportunity that passes the filter, the orchestrator merges the LLM result and then
**strips the transient enrichment fields before serialization**:

```python
merged = {**opp, **llm_result}
merged.pop("_matching_text", None)   # transient — must not reach the store
merged.pop("_full_overview", None)   # transient — must not reach the store
record = OpportunityRecord.from_dict(merged)

if config.run_mode == "live":
    store.write_record(record)       # called exactly once per passing opportunity
    total_written += 1
```

Guarantees:

- The `KeywordFilter` runs over the unified/deduplicated list, so a UNDP opportunity enriched
  with full Overview text is matched against that full text (via `_matching_text`).
- `_matching_text` and `_full_overview` are removed with `merged.pop(...)` before
  `OpportunityRecord.from_dict()` and `store.write_record()`, so neither reaches Sheets/Airtable
  and no new store column is required.
- In **live** mode, `store.write_record` is called **exactly once** per passing opportunity; in
  `dry_run` mode it is not called at all.

---

## Store Adapter Changes

### SheetsAdapter (`store/adapter_sheets.py`)

```python
HEADERS = [
    "devex_opportunity_id",
    "opportunity_title",
    # ... existing headers ...
    "scraped_at",
    "source_portal",   # NEW — appended at end for backward compatibility
]
```

`get_records_since()` returns `source_portal` from the row dict, or `"devex"` if absent:

```python
row.setdefault("source_portal", "devex")
```

### AirtableAdapter (`store/adapter_airtable.py`)

`write_record()` already calls `record.to_dict()` and passes the full payload — no structural
change needed beyond `to_dict()` including `source_portal`.

`get_records_since()` adds the same default:

```python
fields.setdefault("source_portal", "devex")
```

`get_all_ids()` currently reads `devex_opportunity_id`. After the refactor, new records from
non-Devex adapters store their portal-prefixed ID in `devex_opportunity_id` (e.g.
`samgov-ABC123`). This preserves the existing deduplication logic without schema changes.

---

## Opportunity ID Normalization

| Portal | Format | Source field | Example |
|---|---|---|---|
| Devex | `devex-{numeric_id}` | URL path `/projects/{id}` | `devex-123456` |
| SAM.gov | `samgov-{noticeId}` | API field `noticeId` | `samgov-ABC123XYZ` |
| Perplexity | `perplexity-{sha256[:12]}` | SHA-256 of `opportunity_link` | `perplexity-a3f9c1b2d4e7` |

**Rationale:**
- Devex: existing format preserved; no migration needed
- SAM.gov: `noticeId` is the canonical stable identifier in the SAM.gov data model
- Perplexity: no stable ID exists in free-text responses; deterministic hash of the URL
  provides stable, reproducible IDs across runs

The orchestrator uses `opportunity_id` as the primary deduplication key. `opportunity_link` is
the secondary fallback for cases where two adapters surface the same opportunity with different
IDs.

---

## Error Handling Patterns

| Scenario | Adapter behaviour | Orchestrator behaviour |
|---|---|---|
| `AuthenticationError` (Devex) | Log + alert + return `[]` | Continues to next adapter |
| Per-URL parse failure (Devex) | Log + continue loop | Partial results returned |
| HTTP 4xx/5xx (SAM.gov, Perplexity) | Log + return `[]` | Continues to next adapter |
| Unparseable JSON (Perplexity) | Log + return `[]` | Continues to next adapter |
| Adapter raises unhandled exception | Propagates up | Caught at loop level; log + alert + continue |
| `StoreWriteError` | N/A | Log + increment error counter; continue |
| `load_config()` validation failure | N/A | Raises `ValueError` before any adapter runs |

All adapter-level errors are non-fatal to the run. The orchestrator always completes the
`finally` block (audit log + notification) regardless of how many adapters fail.

### Hard requirement: per-adapter exception isolation

**Each adapter's `fetch_opportunities()` call MUST be wrapped in its own `try/except Exception`
block inside the orchestrator loop.** This is a hard architectural requirement, not a
best-effort guideline.

```python
for adapter in adapters:
    try:
        results = await adapter.fetch_opportunities()
        all_opportunities.extend(results)
        audit.log(event_type="adapter_complete",
                  detail=f"portal={adapter.portal_name} results={len(results)}")
    except Exception as exc:
        errors += 1
        audit.log_error(str(exc))
        notifier.send_error_alert(str(exc), component=adapter.portal_name)
        # MUST continue — next adapter must always run
```

**Consequences of this requirement:**

- An `AuthenticationError` raised inside `DevexAdapter.fetch_opportunities()` (if it somehow
  escapes the adapter's own handler) MUST NOT prevent `SAMGovAdapter` or `PerplexityAdapter`
  from running.
- Any unhandled exception from any adapter — including network timeouts, unexpected API
  schema changes, or programming errors — MUST be caught at this boundary.
- The `continue` after the `except` block is implicit in the `for` loop but MUST NOT be
  replaced with a `return` or `raise`.
- The error alert sent via `Notifier` MUST identify the failing adapter by `portal_name` so
  operators can diagnose which portal failed without inspecting logs.

This isolation guarantee is validated by Property 7 (failing adapters do not suppress results
from healthy adapters).

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions
of a system — essentially, a formal statement about what the system should do. Properties serve
as the bridge between human-readable specifications and machine-verifiable correctness
guarantees.*

### Property 1: Adapter result fields are complete

*For any* active adapter and any `Opportunity_Dict` it returns, the dict must contain all
required keys: `opportunity_id`, `opportunity_title`, `funder_organisation`, `country_region`,
`deadline`, `contract_value`, `opportunity_link`, `description_snippet`, and `source_portal`.

**Validates: Requirements 3.4, 4.4**

---

### Property 2: source_portal identity preservation

*For any* `Opportunity_Dict` with any `source_portal` value, constructing an `OpportunityRecord`
via `from_dict()` and then calling `to_dict()` must return a dict whose `source_portal` key
equals the original value.

**Validates: Requirements 6.6, 9.1, 9.2**

---

### Property 3: Deduplication eliminates repeated opportunity_id values

*For any* list of `Opportunity_Dict` instances where some share the same `opportunity_id`, the
deduplicated output list must contain no two entries with the same `opportunity_id`.

**Validates: Requirements 6.7, 10.4**

---

### Property 4: Deduplication eliminates repeated opportunity_link values

*For any* list of `Opportunity_Dict` instances where some share the same `opportunity_link` (and
no portal-specific ID is available), the deduplicated output list must contain no two entries
with the same `opportunity_link`.

**Validates: Requirements 6.7**

---

### Property 5: Adapter registry contains exactly the enabled adapters

*For any* combination of `devex_enabled`, `samgov_enabled`, and `perplexity_enabled` flags in
`Config`, the orchestrator's adapter registry must contain exactly the adapters whose flag is
`True` and no adapters whose flag is `False`.

**Validates: Requirements 6.1**

---

### Property 6: Unified list is the union of all adapter results

*For any* set of active adapters each returning any number of results, the unified list
collected before deduplication must have length equal to the sum of all individual adapter
result lengths.

**Validates: Requirements 6.2**

---

### Property 7: Failing adapters do not suppress results from healthy adapters

*For any* set of adapters where a random subset raises exceptions, the opportunities returned
by the non-failing adapters must all appear in the final unified list.

**Validates: Requirements 6.4**

---

### Property 8: Config credential validation raises on enabled-but-missing key

*For any* combination of `samgov_enabled=True` with absent/empty `SAM_GOV_API_KEY`, or
`perplexity_enabled=True` with absent/empty `PERPLEXITY_API_KEY`, `load_config()` must raise
`ValueError` with a message identifying the missing variable.

**Validates: Requirements 5.6, 5.7**

---

### Property 9: SAM.gov query params reflect Config values

*For any* `Config` with any `sector_keywords` list and any `max_results` value, the HTTP
request constructed by `SAMGovAdapter` must include a `q` parameter equal to the
space-joined keywords and a `limit` parameter equal to `max_results`.

**Validates: Requirements 3.3**

---

### Property 10: Perplexity prompt contains all configured keywords and countries

*For any* `Config` with any `sector_keywords` and `target_countries` lists, the prompt
constructed by `PerplexityAdapter._build_prompt()` must contain every keyword and every
country from those lists.

**Validates: Requirements 4.3**

---

### Property 11: Devex opportunity_id matches portal-prefixed format

*For any* Devex opportunity URL containing a numeric project ID, the extracted `opportunity_id`
must match the pattern `devex-\d+`.

**Validates: Requirements 10.1**

---

### Property 12: SAM.gov opportunity_id matches portal-prefixed format

*For any* SAM.gov API result with any `noticeId` string, the mapped `opportunity_id` must equal
`f"samgov-{noticeId}"`.

**Validates: Requirements 10.2**

---

### Property 13: Perplexity opportunity_id is deterministic

*For any* `opportunity_link` string, calling the Perplexity ID generation function twice must
produce the same `opportunity_id`, and that ID must match the pattern `perplexity-[a-f0-9]{12}`.

**Validates: Requirements 10.3**

---

### Property 14: SheetsAdapter writes source_portal at correct column position

*For any* `OpportunityRecord` with any `source_portal` value, the row written by
`SheetsAdapter.write_record()` must contain that `source_portal` value at the index
corresponding to `"source_portal"` in `HEADERS`.

**Validates: Requirements 9.3, 9.4**

---

### Property 15: Store get_records_since returns "devex" default for legacy rows

*For any* stored row that lacks a `source_portal` field, `get_records_since()` must return
`"devex"` as the `source_portal` value for that row.

**Validates: Requirements 9.6**

---

### Property 16: SAMGovAdapter LATAM post-filter excludes non-target countries

*For any* list of SAM.gov API result items where some have a `placeOfPerformance.country.name`
that does not match any value in `Config.target_countries`, `_is_latam_relevant()` must return
`False` for those items and `True` only for items whose country (or description) contains at
least one target country. The final list returned by `fetch_opportunities()` must contain no
item for which `_is_latam_relevant()` returns `False`.

**Validates: Requirements 3.4 (country_region field), 3.3 (LATAM focus)**

---

### Correctness invariants and acceptance scenarios (UNDP enrichment)

The following UNDP-enrichment invariants are verified by **example-based** tests in
`tests/test_undp_detail_fetch.py`. There are no Hypothesis/`@given` property tests for
UNDP enrichment; each item names its verifying test(s) and a coverage status
(FULL / PARTIAL / NONE).

### Invariant 17: Matching text is authoritative and consistent across both methods

*For any* `Opportunity_Dict` (with any combination of `opportunity_title`, `description_snippet`,
and `_matching_text`) and any `KeywordFilter` configuration, the searchable text used by
`passes_filter()` and by `get_matched_keywords()` must be identical — both must be exactly the
`(title, searchable_text)` pair returned by `get_matching_text()`. Consequently, every keyword
reported by `get_matched_keywords()` must be a keyword that is actually present in the same
searchable text that `passes_filter()` inspects.

**Validates: Requirements 11.2**

**Coverage: PARTIAL** — exercised by `test_keyword_after_1000_chars_passes_filter_and_matched_keywords` and `test_non_undp_opportunity_filters_normally`; no test explicitly asserts that `passes_filter()` and `get_matched_keywords()` derive identical text from `get_matching_text()` for the same dict.

---

### Invariant 18: _matching_text takes precedence, with description_snippet fallback

*For any* `Opportunity_Dict`, if `_matching_text` is present and non-empty then the searchable
text returned by `get_matching_text()` must equal the normalized `_matching_text`; and if
`_matching_text` is absent or empty then the searchable text must equal the normalized
`description_snippet`.

**Validates: Requirements 11.3, 11.4**

**Coverage: PARTIAL** — fallback exercised by `test_non_undp_opportunity_filters_normally`; `_matching_text` precedence exercised by `test_keyword_after_1000_chars_passes_filter_and_matched_keywords`; no dedicated `get_matching_text()` unit test asserts the exact precedence/fallback rule.

---

### Invariant 19: UNDP enrichment sets full matching text and a bounded display snippet

*For any* extracted Overview string, after `UNDPAdapter` enriches an opportunity, `_matching_text`
must equal the complete untruncated Overview, and `description_snippet` must equal the Overview
truncated to at most `_DESCRIPTION_DISPLAY_MAX` (1000) characters (`len(description_snippet) <=
1000` and `description_snippet == overview[:1000]`).

**Validates: Requirements 11.5, 11.6**

**Coverage: PARTIAL** — full untruncated text via `test_extract_overview_full_text_not_truncated`; snippet length bound (<= 1000) and `_matching_text` presence via `test_keyword_after_1000_chars_passes_filter_and_matched_keywords`; exact `description_snippet == overview[:1000]` prefix equality is not asserted.

---

### Invariant 20: Transient fields never reach the store

*For any* `Opportunity_Dict` that carries `_matching_text` and/or `_full_overview`, after the
orchestrator strips the transient fields and constructs an `OpportunityRecord`, the record's
`to_dict()` output must contain neither `_matching_text` nor `_full_overview`, and its key set
must be exactly the fixed `OpportunityRecord` field set (no new column introduced).

**Validates: Requirements 11.7, 11.8**

**Coverage: FULL** — `test_matching_text_excluded_before_serialization` (not a Sheet column) and `test_orchestration_end_to_end_keyword_after_1000` (`_matching_text` and `_full_overview` absent from `record.to_dict()` and from HEADERS).

---

### Invariant 21: Detail-fetch concurrency never exceeds 8

*For any* number of active opportunities to enrich, the maximum number of simultaneously
in-flight detail-page `client.get()` calls observed during the enrichment phase must never
exceed `_MAX_CONCURRENT_DETAIL_FETCHES` (8).

**Validates: Requirements 11.9**

**Coverage: FULL** — `test_concurrency_bounded_and_expired_skipped` asserts `1 < peak <= _MAX_CONCURRENT_DETAIL_FETCHES`.

---

### Invariant 22: Retryable vs. permanent classification and bounded attempts

*For any* simulated detail-page outcome: if it is a retryable failure (a connection error, a
timeout, or an HTTP status in `{429, 500, 502, 503, 504}`), `_fetch_detail_with_retry` must
attempt the fetch more than once but at most `_MAX_ATTEMPTS` (3) times; and if it is a permanent
failure (an HTTP status in `{400, 401, 403, 404}`), it must attempt the fetch exactly once and
return `None` without retrying.

**Validates: Requirements 11.12, 11.13, 11.14**

**Coverage: FULL** — `test_transient_failure_then_success` (2 attempts), `test_repeated_transient_failures_exhaust_attempts` (== _MAX_ATTEMPTS), `test_404_gets_exactly_one_attempt` (1 attempt).

---

### Invariant 23: Retry-After parsing for delay-seconds and HTTP-date, clamped with fallback

*For any* remaining deadline `r >= 0`: for any non-negative delay-seconds value `d`,
`_parse_retry_after(str(d), r)` must equal `min(d, r)`; for any HTTP-date string, the result must
lie in the closed interval `[0, r]`; and for any string that is neither a valid delay-seconds
value nor a valid HTTP-date, `_parse_retry_after` must return `0` (causing the caller to fall
back to exponential backoff).

**Validates: Requirements 11.16, 11.17**

**Coverage: FULL** — `test_parse_retry_after_delay_seconds` (incl. clamp), `test_parse_retry_after_http_date`, `test_parse_retry_after_invalid_returns_zero`, `test_retry_after_numeric_respected`.

---

### Invariant 24: Partial results are preserved when the deadline is reached

*For any* set of enrichment tasks where a random subset stalls past the enrichment deadline:
after `UNDPAdapter._run()` returns, no enrichment task remains pending (every task is either done
or cancelled-and-awaited); every opportunity whose enrichment completed before the deadline
retains its enriched `_matching_text` and truncated `description_snippet`; and every cancelled or
failed opportunity retains its title-based `description_snippet` and is still included in the list
passed to the `KeywordFilter`.

**Validates: Requirements 11.20, 11.21, 11.22**

**Coverage: PARTIAL** — `test_timeout_preserves_completed_records` verifies completed records preserved, the cancelled task awaited (no pending tasks), and exact warning counts; it does not directly assert that a cancelled opportunity retains its title-based `description_snippet` and is still passed through `KeywordFilter`.

---

### Invariant 25: Orchestration writes each passing opportunity exactly once in live mode

*For any* unified, deduplicated list of `Opportunity_Dict` instances processed by
`run_scraper()` in live run mode, `Store.write_record` must be called exactly once for each
opportunity that passes the `KeywordFilter` and zero times for each opportunity that does not;
in `dry_run` mode it must not be called at all.

**Validates: Requirements 11.24**

**Coverage: PARTIAL** — `test_orchestration_end_to_end_keyword_after_1000` verifies exactly one `Store.write_record()` in live mode; the dry-run "no write" case has no test.

---

### Invariant 26: Deep-keyword end-to-end match with clean storage

*For any* opportunity whose only matching sector keyword appears after character 1000 of its
`_matching_text` while its `description_snippet` is exactly 1000 characters and contains no
matching keyword, the orchestrator must pass that opportunity through the `KeywordFilter`, write
it exactly once (live mode), record the correct matched keyword, retain a `description_snippet`
of at most 1000 characters, and produce a stored record in which both `_matching_text` and
`_full_overview` are absent.

**Validates: Requirements 11.5, 11.6, 11.25**

**Coverage: FULL (live mode)** — `test_orchestration_end_to_end_keyword_after_1000` (keyword after char 1000 matched via `record.matched_keywords`, single write, transient fields absent, snippet <= 1000, no new column).

---

## Testing Strategy

### Property-based testing library

Use **Hypothesis** (Python) for all property-based tests. Each test runs a minimum of 100
iterations. Tag format: `# Feature: multi-portal-adapter-architecture, Property N: <text>`

### Unit tests (example-based)

Focus on:
- `BasePortalAdapter` ABC enforcement (Properties 1.1–1.4 from requirements)
- `DevexAdapter.portal_name == "devex"`, `SAMGovAdapter.portal_name == "samgov"`,
  `PerplexityAdapter.portal_name == "perplexity"`
- `samgov_enabled=False` / `perplexity_enabled=False` guard returns `[]` without HTTP calls
- `AuthenticationError` path in `DevexAdapter` — assert `[]` returned, audit + notifier called
- `DevexAdapter` closes Playwright resources even when exception is raised mid-run
- `OpportunityRecord` default `source_portal == "devex"`

### Property tests (Hypothesis)

| Test | Property | Strategy |
|---|---|---|
| `test_adapter_result_fields_complete` | Property 1 | `st.lists(st.fixed_dictionaries(...))` per adapter with mocked HTTP |
| `test_source_portal_round_trip` | Property 2 | `st.text(min_size=1)` for source_portal |
| `test_dedup_by_opportunity_id` | Property 3 | `st.lists(...)` with injected duplicates |
| `test_dedup_by_opportunity_link` | Property 4 | `st.lists(...)` with injected duplicate links |
| `test_adapter_registry_matches_flags` | Property 5 | `st.booleans()` × 3 for enabled flags |
| `test_unified_list_is_union` | Property 6 | `st.lists(st.integers(min_value=0, max_value=20))` for result counts |
| `test_failing_adapters_do_not_suppress` | Property 7 | Random subset of adapters raises; assert survivors present |
| `test_config_validation_raises_on_missing_key` | Property 8 | `st.booleans()` for enabled flags with absent keys |
| `test_samgov_query_params` | Property 9 | `st.lists(st.text())` for keywords, `st.integers()` for max_results |
| `test_perplexity_prompt_contains_config` | Property 10 | `st.lists(st.text())` for keywords/countries |
| `test_devex_id_format` | Property 11 | `st.integers(min_value=1)` for numeric project IDs |
| `test_samgov_id_format` | Property 12 | `st.text(min_size=1)` for noticeId |
| `test_perplexity_id_deterministic` | Property 13 | `st.text()` for opportunity_link URLs |
| `test_sheets_source_portal_column` | Property 14 | `st.text(min_size=1)` for source_portal |
| `test_store_legacy_source_portal_default` | Property 15 | Rows with/without source_portal field |
| `test_samgov_latam_post_filter` | Property 16 | `st.lists(st.fixed_dictionaries(...))` with mixed target/non-target countries |

### Integration tests

- End-to-end dry-run with all three adapters mocked — assert audit log contains
  `source_portal` for each processed opportunity
- `SheetsAdapter` and `AirtableAdapter` write/read round-trip with `source_portal` column
  (against test spreadsheet / Airtable sandbox)

### Regression tests

- Existing `tests/test_filter.py`, `tests/test_llm.py`, `tests/test_parser.py` must pass
  unchanged — the keyword filter, LLM interpreter, and parser are not modified
