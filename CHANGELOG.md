# Changelog

## 2026-08-19 — UNDP Description Enrichment (v3)

- Fixed UNDP adapter to fetch real Overview text from each detail page instead of using the short title as `description_snippet`.
- Keyword matching uses the authoritative `_matching_text` field (full Overview, not truncated) via a unified `KeywordFilter.get_matching_text()` helper.
- Detail-page fetching uses bounded concurrency (semaphore of 8, per network attempt). Retry policy: 3 attempts for transient errors, Retry-After in both delay-seconds and HTTP-date formats.
- Completed records preserved when enrichment deadline is reached; partial results returned with logged warning.
- 22 tests covering extraction, adversarial, concurrency/semaphore, timing, retry, orchestration end-to-end, and timeout preservation.
- Result: filter pass rate increased from ~2 to ~18 UNDP opportunities per run.
