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
