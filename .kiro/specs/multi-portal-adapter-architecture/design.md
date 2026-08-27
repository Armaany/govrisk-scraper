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
5. Deduplication: cross-run by `opportunity_link` (seeded from `store.get_all_links()`), plus
   within-run skipping of repeated `opportunity_id` and `opportunity_link`
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
| `models.py` | Add `source_portal: str` field to `OpportunityRecord`; make `to_dict()` a canonical round-trippable serializer (all internal field names, no `portal_source`); update `from_dict()` |
| `main.py` | Replace single-portal pipeline with adapter registry loop; seed cross-run dedup from `store.get_all_links()` keyed on `opportunity_link` |
| `store/adapter_sheets.py` | Keep the existing 12-column `HEADERS` (Live_Sheet_Schema) unchanged; update `write_record()` to project the canonical dict onto the 12 external columns (`source_portal` → `portal_source` at column 1); add `get_all_links()`; deprecate `get_records_since()` |
| `store/adapter_airtable.py` | `write_record()` passes canonical `to_dict()` (including `source_portal`) through; update `get_records_since()` default handling |

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

Add the `source_portal` field with a backward-compatible default and make `to_dict()` the
**canonical, round-trippable serializer**.

> **Amendment (Option A).** This replaces the current broken `to_dict()`, which emitted the
> external column name `"portal_source"` and dropped most dataclass fields
> (`devex_opportunity_id`, `description_snippet`, `matched_keywords`, `relevance_reason`,
> `llm_confidence`, `llm_called`, `anna_benchmark`, `scraped_at`). The canonical `to_dict()`
> emits **all** dataclass fields under their **internal** names and MUST NOT emit both
> `portal_source` and `source_portal` — it emits only the internal `source_portal`. Mapping the
> canonical `source_portal` onto the external `portal_source` column is the responsibility of the
> `SheetsAdapter` presentation layer (see Store Adapter Changes), not of the model.

```python
@dataclass
class OpportunityRecord:
    # ... existing fields unchanged ...
    source_portal: str = "devex"   # NEW — defaults to "devex" for legacy records

    def to_dict(self) -> dict[str, Any]:
        """Canonical, round-trippable serialization: every dataclass field under its
        INTERNAL name. Never emits the external ``portal_source`` label, and never emits
        both ``portal_source`` and ``source_portal``."""
        return {
            "devex_opportunity_id": self.devex_opportunity_id,
            "opportunity_title": self.opportunity_title,
            "funder_organisation": self.funder_organisation,
            "country_region": self.country_region,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "contract_value": self.contract_value,
            "opportunity_link": self.opportunity_link,
            "description_snippet": self.description_snippet,
            "matched_keywords": self.matched_keywords,
            "summary": self.summary,
            "relevance_score": self.relevance_score.value if self.relevance_score else None,
            "relevance_reason": self.relevance_reason,
            "bid_recommendation": self.bid_recommendation.value if self.bid_recommendation else None,
            "risk_flags": self.risk_flags,
            "llm_confidence": self.llm_confidence.value if self.llm_confidence else None,
            "review_status": self.review_status.value if self.review_status else "pending_review",
            "llm_called": self.llm_called,
            "anna_benchmark": self.anna_benchmark,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
            "source_portal": self.source_portal,   # INTERNAL name only
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpportunityRecord":
        return cls(
            # ... existing fields unchanged ...
            source_portal=str(data.get("source_portal", "devex")),   # NEW
        )
```

**Round-trip guarantee:** `from_dict(to_dict(record))` reproduces every field, including an
arbitrary `source_portal` value. Because `to_dict()` uses only internal names, its output is a
valid input to `from_dict()` with no key translation.

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
    # Cross-run dedup is keyed on opportunity_link (Req 6.8, 10.5). Seed from the persisted
    # link column via get_all_links() — NOT get_all_ids(), which under the Live_Sheet_Schema
    # reads column 1 (portal_source, i.e. portal names) and cannot identify prior opportunities.
    existing_links: set[str] = set(store.get_all_links())
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

    # Deduplication across all adapters.
    #  - Cross-run dedup: keyed on opportunity_link, seeded from store.get_all_links() (Req 10.5).
    #  - Within-run dedup: skips repeated opportunity_id AND repeated opportunity_link (Req 10.4).
    seen_ids: set[str] = set()
    seen_links: set[str] = set(existing_links)   # seed persisted links for cross-run dedup
    deduplicated: list[dict] = []
    for opp in all_opportunities:
        opp_id = opp.get("opportunity_id", "")
        opp_link = opp.get("opportunity_link", "")
        # Cross-run + within-run link dedup (primary key under the Live_Sheet_Schema)
        if opp_link and opp_link in seen_links:
            duplicates_skipped += 1
            continue
        # Within-run opportunity_id dedup
        if opp_id and opp_id in seen_ids:
            duplicates_skipped += 1
            continue
        if opp_id:
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
- Cross-run deduplication is keyed on `opportunity_link`, seeded from `store.get_all_links()`
  (the persisted link column). The prior `get_all_ids()` seeding is removed because, under the
  Live_Sheet_Schema, column 1 holds `portal_source` (portal names), not opportunity identifiers.
- Within a run, both a repeated `opportunity_id` and a repeated `opportunity_link` are skipped
- `source_portal` is carried through `Opportunity_Dict` → `OpportunityRecord` → store write
- The filter/LLM/store pipeline code is identical to the current implementation

## Schema Contract (Option A)

This feature adopts **Option A**: the live Google Sheet is treated as a fixed, authoritative
schema that is **not migrated**. The contract that governs serialization and persistence:

- **Canonical internal vs. external names.** The in-memory data model uses the canonical field
  name **`source_portal`**. The Google Sheet exposes the same datum under the external column
  label **`portal_source`** (column 1). `OpportunityRecord.to_dict()` is canonical and emits only
  `source_portal`; the `SheetsAdapter` presentation layer maps `source_portal → portal_source`
  when writing. The two names are the same logical value in different representations.
- **12-column Sheet is frozen.** `SheetsAdapter.HEADERS` remains the existing 12 columns
  (`portal_source`, `opportunity_title`, `funder_organisation`, `country_region`, `deadline`,
  `contract_value`, `opportunity_link`, `summary`, `relevance_score`, `bid_recommendation`,
  `risk_flags`, `review_status`). No `source_portal`, `devex_opportunity_id`, or `scraped_at`
  column is added, and the live Sheet is not rewritten.
- **Canonical `to_dict()` is round-trippable.** It emits every dataclass field under its internal
  name so that `from_dict(to_dict(record))` reproduces the record. Fields with no external column
  are simply not projected by `SheetsAdapter.write_record()`.
- **Link-based cross-run dedup.** Cross-run deduplication is keyed on `opportunity_link`, seeded
  from `SheetsAdapter.get_all_links()` (column 7), replacing the old `get_all_ids()`-on-column-1
  approach.
- **`get_records_since()` deprecated.** With no `scraped_at` column in the Live_Sheet_Schema,
  `SheetsAdapter.get_records_since()` is unsupported and raises `NotImplementedError` by contract (matching the implementation).

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

Under Option A the live Google Sheet uses a **fixed, authoritative 12-column schema**
(`Live_Sheet_Schema`) that predates this feature and is **not migrated**. `HEADERS` therefore
stays **exactly as it is** — `source_portal` is **not** appended:

```python
# UNCHANGED — the frozen 12-column Live_Sheet_Schema
HEADERS = [
    "portal_source",       # col 1  — external label for canonical source_portal
    "opportunity_title",   # col 2
    "funder_organisation", # col 3
    "country_region",      # col 4
    "deadline",            # col 5
    "contract_value",      # col 6
    "opportunity_link",    # col 7  — persisted link, used for cross-run dedup
    "summary",             # col 8
    "relevance_score",     # col 9
    "bid_recommendation",  # col 10
    "risk_flags",          # col 11
    "review_status",       # col 12
]
```

There is **no** `devex_opportunity_id` column and **no** `scraped_at` column in the live sheet.

#### `write_record()` — presentation mapping (canonical → 12 external columns)

`to_dict()` now returns the **canonical** dict keyed by internal field names. Because the
external sheet uses `portal_source` (not the internal `source_portal`) and exposes only 12 of the
canonical fields, `write_record()` performs an **explicit ordered projection** from canonical
keys onto the 12 external columns. In particular, canonical `source_portal` is written into the
external `portal_source` column (column 1); the other 11 columns are written from their canonical
counterparts. Writes stay positionally aligned with the live header row.

```python
def write_record(self, record: OpportunityRecord) -> str:
    payload = record.to_dict()  # canonical, internal names

    # Ordered projection: external HEADERS column -> canonical payload key.
    # Only the source_portal <-> portal_source name differs; the rest map 1:1.
    CANONICAL_KEY_FOR_COLUMN = {
        "portal_source":       "source_portal",   # external label <- canonical field
        "opportunity_title":   "opportunity_title",
        "funder_organisation": "funder_organisation",
        "country_region":      "country_region",
        "deadline":            "deadline",
        "contract_value":      "contract_value",
        "opportunity_link":    "opportunity_link",
        "summary":             "summary",
        "relevance_score":     "relevance_score",
        "bid_recommendation":  "bid_recommendation",
        "risk_flags":          "risk_flags",
        "review_status":       "review_status",
    }
    row = [payload.get(CANONICAL_KEY_FOR_COLUMN[header], "") for header in self.HEADERS]
    self.worksheet.append_row(row, value_input_option="RAW")
    ...
```

The canonical fields that have no column in the Live_Sheet_Schema (`devex_opportunity_id`,
`description_snippet`, `matched_keywords`, `relevance_reason`, `llm_confidence`, `llm_called`,
`anna_benchmark`, `scraped_at`) are simply not projected — they remain available in memory and in
`to_dict()` for round-tripping, but are not written to the sheet.

#### Deduplication: `get_all_links()` replaces `get_all_ids()`-on-column-1

The former `get_all_ids()` read **column 1**, which under the Live_Sheet_Schema is
`portal_source` (portal names such as `"devex"`), so it could not identify previously persisted
opportunities; it is now deprecated and raises `NotImplementedError`. Add a `get_all_links()` method that reads the **`opportunity_link` column
(column 7)** and returns the set of persisted links. The orchestrator seeds its cross-run dedup
set from these links (Req 6.8) and dedups across runs by `opportunity_link` (Req 10.5).

```python
def get_all_links(self) -> set[str]:
    """Return the set of persisted opportunity_link values (column 7, excluding header)."""
    try:
        link_column = self.worksheet.col_values(7)  # opportunity_link
    except Exception:
        return set()
    if len(link_column) <= 1:
        return set()
    return {value.strip() for value in link_column[1:] if value.strip()}
```

#### `get_records_since()` — unsupported/deprecated under the 12-column schema

The Live_Sheet_Schema has **no `scraped_at` column**, so time-based retrieval cannot be
supported. `get_records_since()` is deprecated and, matching the selected implementation,
raises `NotImplementedError` (Req 9.6):

```python
def get_records_since(self, since: datetime) -> list:
    """Unsupported under the Live_Sheet_Schema: there is no scraped_at column.
    Raises NotImplementedError by contract (Req 9.6); use get_all_links() for
    cross-run deduplication."""
    raise NotImplementedError(
        "get_records_since() is not supported under the Live_Sheet_Schema "
        "(no 'scraped_at' column). Use get_all_links() for cross-run dedup."
    )
```

#### Startup header validation (schema v1.0)

`_ensure_headers()` enforces the frozen schema on initialization, before any
read or write:

- If the sheet is empty, it writes the canonical 12-column header.
- If row 1 is populated, `_validate_headers()` checks it against `HEADERS` and
  raises `SheetsSchemaError` on any missing, duplicate (including duplicates
  that differ only by surrounding whitespace or letter case), reordered, or
  unexpected header. Because header-order-independent writing is deferred to
  Phase B, reordered/unexpected headers are rejected under v1.0 to avoid
  positional corruption. A populated header is never rewritten or auto-repaired.

#### Deprecated ID-based lookups

`get_all_ids()` and `record_exists()` raise `NotImplementedError`: the frozen
12-column schema has no persisted opportunity-ID column (column 1 is
`portal_source`). Cross-run deduplication uses `get_all_links()` instead. The
Airtable equivalents remain functional because Airtable persists
`devex_opportunity_id`.

### AirtableAdapter (`store/adapter_airtable.py`)

`write_record()` already calls `record.to_dict()` and sends the full payload, so the canonical
`source_portal` (and all other canonical fields such as `devex_opportunity_id`) now flow through
to Airtable automatically — no structural change is required.

> **Design note / caveat.** Because `write_record()` sends the canonical payload verbatim,
> Airtable writes will only succeed if the target table schema **already contains fields matching
> the canonical keys** — in particular `source_portal` and `devex_opportunity_id`. Airtable
> rejects writes to unknown field names. Ensuring these columns exist in the Airtable base is an
> operational prerequisite, not a code change in this feature. (Unlike the Google Sheet, Airtable
> is not constrained to the frozen 12-column Live_Sheet_Schema.)

For Airtable, `get_records_since()` keeps the legacy default (Req 9.7): records that predate the
`source_portal` field default to `"devex"`:

```python
fields.setdefault("source_portal", "devex")
```

**Deduplication under Option A (both stores).** Cross-run deduplication is keyed on
`opportunity_link`, not on any stored ID column. For `SheetsAdapter` the orchestrator seeds from
`get_all_links()` (column 7 of the Live_Sheet_Schema); the old `get_all_ids()`-on-column-1 path
is not used, because column 1 holds `portal_source` (portal names) under the frozen schema. The
Sheet is **not** migrated and no `devex_opportunity_id` or `scraped_at` column is added.

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

Deduplication under Option A: **cross-run** dedup is keyed on `opportunity_link` (seeded from
`store.get_all_links()`), because the Live_Sheet_Schema persists no `opportunity_id` column
(Req 10.5). **Within a single run**, the orchestrator additionally skips a repeated
`opportunity_id` as well as a repeated `opportunity_link` (Req 10.4), so two adapters surfacing
the same opportunity are collapsed by either key.

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

### Property 2: Canonical to_dict round-trips every field (incl. arbitrary source_portal)

*For any* `OpportunityRecord` built from any valid `Opportunity_Dict` (with any `source_portal`
value), `from_dict(to_dict(record))` must reproduce every field equal to the original, and
`to_dict()` must emit `source_portal` under its internal name while never emitting the external
label `portal_source` (i.e. the output must not contain both `portal_source` and `source_portal`,
and must not contain `portal_source` at all).

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

### Property 14: SheetsAdapter maps source_portal onto the portal_source column (col 1)

*For any* `OpportunityRecord` with any `source_portal` value, the row written by
`SheetsAdapter.write_record()` must be exactly 12 columns wide (the unchanged Live_Sheet_Schema)
and must contain that `source_portal` value at the index of `"portal_source"` in `HEADERS`
(column 1), with each remaining column populated from its canonical counterpart. `HEADERS` must
remain the frozen 12-column schema (no `source_portal` column appended).

**Validates: Requirements 9.3, 9.4**

---

### Property 15: get_all_links seeds link-based cross-run dedup; get_records_since is deprecated for Sheets

*For any* set of rows persisted under the Live_Sheet_Schema, `SheetsAdapter.get_all_links()` must
return exactly the set of non-empty values in the `opportunity_link` column (column 7, excluding
the header), and seeding the orchestrator's cross-run dedup from that set must cause any incoming
`Opportunity_Dict` whose `opportunity_link` is already persisted to be skipped. Under the
Live_Sheet_Schema, `SheetsAdapter.get_records_since()` must be unsupported — raising `NotImplementedError` by contract (matching the implementation). For `AirtableAdapter`, any stored record lacking a
`source_portal` field must still default to `"devex"`.

**Validates: Requirements 6.8, 9.6, 9.7, 10.5**

---

### Property 16: SAMGovAdapter LATAM post-filter excludes non-target countries

*For any* list of SAM.gov API result items where some have a `placeOfPerformance.country.name`
that does not match any value in `Config.target_countries`, `_is_latam_relevant()` must return
`False` for those items and `True` only for items whose country (or description) contains at
least one target country. The final list returned by `fetch_opportunities()` must contain no
item for which `_is_latam_relevant()` returns `False`.

**Validates: Requirements 3.4, 3.3**

---

### Property 17: SheetsAdapter rejects incompatible headers on init

*For any* populated header row that is not exactly the canonical 12-column
`HEADERS` (missing, duplicate incl. whitespace/case-only, reordered, or
unexpected), `SheetsAdapter._ensure_headers()` must raise `SheetsSchemaError`
before any write, and must never rewrite a populated header. An empty sheet is
initialized with the canonical header.

**Validates: Requirements 9.8**

### Property 18: Deprecated SheetsAdapter ID lookups raise

`SheetsAdapter.get_all_ids()` and `SheetsAdapter.record_exists()` raise
`NotImplementedError` before performing any worksheet call, directing callers
to `get_all_links()` for cross-run deduplication.

**Validates: Requirements 9.9**

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

Use **Hypothesis** (Python) for property-based tests. Example counts are configured per test (currently 30-300) according to execution cost and coverage needs. Tag format: `# Feature: multi-portal-adapter-architecture, Property N: <text>`

### Unit tests (example-based)

Focus on:
- `BasePortalAdapter` ABC enforcement (Requirement 1.3)
- `DevexAdapter.portal_name == "devex"`, `SAMGovAdapter.portal_name == "samgov"`,
  `PerplexityAdapter.portal_name == "perplexity"`
- `samgov_enabled=False` / `perplexity_enabled=False` guard returns `[]` without HTTP calls
- `AuthenticationError` path in `DevexAdapter`: assert `[]` returned, audit + notifier called
- `DevexAdapter` closes Playwright resources even when an exception is raised mid-run
- `OpportunityRecord` default `source_portal == "devex"`
- `SheetsAdapter` startup header validation: empty-sheet initialization, and rejection of missing / duplicate (incl. whitespace/case) / reordered / unexpected headers (Requirement 9.8)
- `SheetsAdapter.get_all_ids()` and `record_exists()` raise `NotImplementedError` before any worksheet call (Requirement 9.9)

### Property tests (Hypothesis)

| Test | Property | Strategy |
|---|---|---|
| `test_adapter_result_fields_complete` | Property 1 | `st.lists(st.fixed_dictionaries(...))` per adapter with mocked HTTP |
| `test_canonical_to_dict_round_trip` | Property 2 | `st.fixed_dictionaries(...)` over all fields + `st.text(min_size=1)` for source_portal; assert `from_dict(to_dict(r))` equals `r` and `"portal_source"` absent |
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
| `test_sheets_maps_source_portal_to_portal_source_col1` | Property 14 | `st.text(min_size=1)` for source_portal; assert 12-wide row, value at `portal_source` (col 1) |
| `test_get_all_links_and_get_records_since_deprecated` | Property 15 | Sheet rows with varied `opportunity_link` (col 7) → `get_all_links()` set; assert `get_records_since()` raises `NotImplementedError`; Airtable rows with/without `source_portal` default to `"devex"` |
| `test_samgov_latam_post_filter` | Property 16 | `st.lists(st.fixed_dictionaries(...))` with mixed target/non-target countries |

### Integration tests

- End-to-end dry-run with all three adapters mocked — assert audit log contains
  `source_portal` for each processed opportunity
- `SheetsAdapter` write/read round-trip verifying `source_portal` maps to external `portal_source` column; `AirtableAdapter` round-trip with `source_portal` field
  (against test spreadsheet / Airtable sandbox)
  that a full orchestrator dry-run produces stored records with the transient fields stripped

### Regression tests

- Existing `tests/test_filter.py`, `tests/test_llm.py`, `tests/test_parser.py` must pass
  unchanged — the keyword filter, LLM interpreter, and parser are not modified
