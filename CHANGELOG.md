# Changelog

## 2026-08-19 - UNDP Description Enrichment

- Fixed UNDP adapter to fetch real Overview text from each detail page instead of using the short title as `description_snippet`.
- Keyword matching now uses the full Overview text (not truncated to 1000 chars), so opportunities with relevant content beyond the display limit are no longer missed.
- Detail-page fetching uses bounded concurrency (semaphore of 8) with per-request and adapter-level timeouts to avoid blocking the multi-portal run.
- Result: filter pass rate increased from ~2 to ~18 UNDP opportunities per run.
