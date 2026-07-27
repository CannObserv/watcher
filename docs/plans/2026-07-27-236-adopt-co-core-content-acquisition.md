# #236 — Adopt co-core v0.5.0 content-acquisition; delete the src/core mirror

**Status:** in progress (2026-07-27)
**Depends on:** #220 (co-core adopted via find-links; closed), #226 (closed, no-op audit)

## Problem

Watcher carries a byte-identical copy of the fetch → extract → fingerprint pipeline
under `src/core/fetchers/`, `src/core/extractors/`, `src/core/simhash.py`,
`src/core/extraction_defaults.py` ("mirror discipline"). co-core v0.5.0 ships this as
the canonical implementation. Adopt it and delete the mirror.

## Grounding (verified against co-core 0.5.0 wheels)

- **Fingerprint parity is byte-identical.** The pipeline fingerprint is
  `sha256("\n".join(chunk.text))` — it reads only `chunk.text`. co-core's
  `HtmlExtractor` emits identical chunk text → identical fingerprint (golden-checked).
  The mirror's computed `Chunk.content_hash/simhash/char_count/excerpt` fields are
  **dead** in the consumer (only `base.py` reads them); co-core's `Chunk` drops them —
  no impact.
- **`FetchResult` is field-identical** (`content, status_code, headers, duration_ms,
  fetcher_used` + `is_success`). `tasks.py` fetch consumption is unchanged.
- **Extractors are sync + zero-arg**; `extract(raw, config=None)` signature matches.
- **UA continuity:** `AsyncFetchDriver.execute` merges `{"user-agent": "co-core-aio",
  **effect.headers}`, so passing `user-agent: watcher/0.1.0` in `FetchContent.headers`
  preserves byte-continuity.
- **CSV error contract:** `CsvExcelExtractor` raises `ValueError` on bad bytes —
  already caught by the pipeline's broad `except Exception -> ExtractionError`.
- **`extraction_config_from_spec`** output is identical to the mirror's.
- **cannobserv#259** (un-followed 3xx `is_success`) — N/A: watcher fetches with
  `follow_redirects=True` everywhere.

## Approach

Delete the mirror; import from co-core. Preserve the `ServiceRegistry` injection seam
and the `.fetch(url, config) -> FetchResult` fetcher interface via a **thin
watcher-owned adapter** (`src/core/fetch.py`) that drives co-core's `AsyncFetchDriver`
with the watcher UA — so `tasks.py` and the fetch-faking tests are untouched.

## Steps

1. **Deps:** `pyproject.toml` → `co-core[extract]>=0.5,<0.6` + `co-core-aio>=0.5,<0.6`;
   wheelhouse re-synced (done); `uv lock`.
2. **`src/core/fetch.py`** (new, not a mirror): `HttpFetcher` adapter over
   `AsyncFetchDriver`, injects `user-agent: watcher/0.1.0`, returns co-core `FetchResult`;
   `.aclose()` parity. Small `Fetcher` Protocol for typing.
3. **`registry.py`:** extractor map → `co_core.pure.extract.{html,csv_excel,pdf}`
   classes; `Extractor` type → `co_core.pure.extract.Extractor`; fetcher → `src.core.fetch`.
4. **`pipeline.py`:** import `ExtractionResult/Extractor/extraction_config_from_spec`
   + `HtmlExtractor` from co-core; **drop `await`** on `extractor.extract(...)`.
5. **Delete mirror:** `src/core/fetchers/`, `src/core/extractors/`, `src/core/simhash.py`,
   `src/core/extraction_defaults.py`.
6. **Tests:** delete pure-unit mirror tests (now upstream: `test_http`, `test_html`,
   `test_pdf`, `test_csv_excel`, `test_base`, `test_simhash`, `test_extraction_defaults`);
   add `test_fetch.py` for the adapter (UA injection, effect construction, aclose);
   repoint `test_registry`, `test_pipeline`; keep `test_tasks`, `test_cannobserv_smoke`.
7. **Docs:** retire the content-acquisition half of the AGENTS.md "Mirrored
   content-acquisition code" note (narrow to `logging.py`, out of #236 scope); update
   the wheelhouse note to v0.5.0 / floors `>=0.5,<0.6` / `[extract]` extra.
8. **Verify:** full `pytest -m "not integration"`; golden fingerprint check; restart.

## Open questions

- ~~`logging.py` stays mirrored (not in #236 scope) → keep a narrowed mirror note rather
  than fully retiring it.~~ **Resolved in #159** (`d96ae44`): the mirror discipline is
  fully retired — `logging.py` is service-local in both repos, no sibling sync. Archiver's
  reciprocal AGENTS.md wording tracked in CannObserv/archiver#104.
