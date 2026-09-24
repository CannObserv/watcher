# Observo-derived extraction and the change diff — design

**Status:** approved in conversation 2026-09-24; not started. **Issue:** #222 (to be
retitled and split — see **#222 disposition**). **Cross-repo work:** filed as issues
in co-core, broker, Observo and Archiver after this doc is reviewed; none of it is
implemented from a Watcher session.

**Context read for this design (all 2026-09-24):** archiver#179 (`content.process`,
open, no comments), replicator#69 (closed; the boundaries charter's *Asked and
answered*), replicator#114 (permanent bucket, open), observo#628 (content-addressed
archive storage, open), observo#488 (`pdf_extract_text`, shipped), cannobserv#475
(lift `BlobStore`, open). Built against co-core **0.13.2** on the Watcher side;
Observo pins `<0.12`, co-core HEAD is 0.19.1.

## Problem

Watcher notifies *that* a page changed, never *what* changed. Since #156 the
`ChangeRevision` holds only a fingerprint; #221 stripped the inert diff toggles;
#222 has been waiting on somewhere to read previous content from. Its last recorded
plan (2026-08-27) re-extracts the previous *raw blob* at change time under
Replicator's 7-day-from-last-reference retention, and needs three Watcher-side
workarounds to hold: touch-forward on the cache-hit branch (rotating nonces mint a new
raw blob per check while the fingerprint sits still), a validator keepalive (a 304
never re-references), and a cadence guard against TTL − slack.

Meanwhile the cohort's roles moved. Observo is the stream and asset processor —
given bits, it transforms them — and observo#628 makes its storage permanent,
append-only and content-addressed at `blobs/<sha256>.bin` under Replicator's key
scheme. The user's stated split: **Watcher monitors and notifies; Replicator handles
bits; Archiver is registrar and control plane; Observo transforms.** Extraction is a
transform, and it is the one Watcher still runs in-process.

### First principles

- **Change detection never needed durable storage.** Every check fetches fresh
  bytes; the comparison is against a fingerprint Watcher keeps forever. The raw blob
  must survive only fetch → extraction (minutes). Cadences longer than the blob TTL
  already work for the *signal*.
- **Only the diff needs the past, and it needs the past *extracted text*, not the
  past raw bytes.** Extracted text is small, moves only when content moves, and is
  exactly what Watcher's fingerprint already hashes:
  `sha256("\n".join(chunk.text))`. Stored permanently under its hash, Watcher's
  existing `content_fingerprint` *is* its storage address — the whole revision
  history already names the future locations.
- Storing extracted text by hash dissolves every #222 workaround: no touch-forward,
  no keepalive, no cadence guard, no `blob_uri` columns on `ChangeRevision`, and
  storage impermanence is fully decoupled from cadence.

## Decisions (approved)

| # | Decision | Alternatives rejected |
|---|---|---|
| D1 | The diff is in scope from day one: permanent storage of extracted text is part of the processing contract, and #222 is restored on top of it. | Signal-only now, diff later (a second round of Observo work; every interim change diff-less). Diff first with Watcher writing its own store (pushes Watcher's role the wrong way). |
| D2 | A shared `content.process` / `content.derived` command/fact pair on the cohort broker, Observo as the processor. | Watcher calling Observo's HTTP job API (per-check Job rows, polling/SSE, no service auth, reintroduces the HTTP coupling #311 removed). Observo consuming `content.blobs` directly (needs Watcher's item→spec map; breaks `command_id`-only correlation; leaks the monitoring domain). |
| D3 | One `source_spec` per command; the fallback loop stays in Watcher as a chain of commands. Observo is a pure function of (bytes, spec, version). | Command carries the ordered list and Observo loops. |
| D4 | `processor_version` is **reported** on the fact, not pinned on the command. | archiver#179's pin — Archiver can add it when its permanent record needs it. |
| D5 | Empty extraction is a *result* (`empty: true`, nothing stored), not a failure. Watcher judges it (#258 rule preserved). | `processing_failed` with `empty_extraction`. |
| D6 | Extraction-only change policy (**Option A**): spec changed → notify, labelled; processor version changed alone → silent re-baseline + `CHECK_REBASELINED` audit. | Always notify with a label (a notification burst on every co-core upgrade). Always re-baseline (a spec edit masks a coincident page change). |
| D7 | Cutover is shadow → switch → delete under `WATCHER_EXTRACT_MODE`, the Phase 4 pattern. The diff ships **in shadow mode**. | Hard cutover. |
| D8 | `significance` is won't-do; chunk semantics (`added`/`modified`/`removed`/`chunks_changed`) are out of scope. | Restore from `89d64eb`. |

## Section 1 — the contract

Defined in co-core, names proposed:

- **`content.process`** — commands, issuer → processor. Observo's group:
  `group_name(CONTENT_PROCESS, "observo")` → `observo.process`.
- **`content.derived`** — facts, broadcast. Watcher's group `watcher.derived`; a fact
  for a `command_id` Watcher did not issue is discarded, as on `content.blobs`.

**`ContentProcessCommand`**

| Field | Notes |
|---|---|
| `command_id` | fresh ULID per occasion (MUST-1 analogue) |
| `info_source_id` | reporting only, never routing (#252 posture) |
| `input_uri`, `input_digest` | the raw blob as the `content.blobs` fact named it |
| `processor` | `"extract"` |
| `source_spec` | **one** spec document (D3) |
| `media_type_hint`, `media_type_override` | the fact's normalized `media_type`; the item's operator override |

**`ProcessingCompleteEvent`**

| Field | Notes |
|---|---|
| `command_id`, `info_source_id` | echoed |
| `output_digest`, `output_uri` | `sha256:…` and `gs://…/blobs/<sha256>.bin`; both null when `empty` |
| `output_size_bytes`, `output_media_type` | `text/plain; charset=utf-8` |
| `empty` | `true` ⇒ the spec bound nothing; nothing was stored (D5) |
| `spec_fingerprint`, `schema_version` | co-core's derivation over the spec that ran |
| `processor_version` | `"{co-core version}+{Observo local extraction generation}"` (D4) |

**`ProcessingFailedEvent`**: `command_id`, `reason`, `terminal`, `detail`. Reasons:
`extraction_error` (terminal), `unsupported_media_type` (terminal),
`input_unreadable` (terminal for this command; the issuer re-fetches),
`input_digest_mismatch` (terminal), `transient` (non-terminal — Observo publishes
this only if it chooses to surface a retry; otherwise it publishes nothing and the
pending entry is reclaimed).

**Guarantees**

- **Canonical bytes.** co-core defines `canonical_text(chunks) -> bytes` once:
  chunk texts joined with `\n`, UTF-8, no trailing newline — byte-identical to
  Watcher's current `_extract_and_fingerprint`. A golden-digest test pins it.
  Therefore `output_digest == ChangeRevision.content_fingerprint`.
- **Write-if-absent, never delete** (observo#628 D1). Redelivery is idempotent by
  construction.
- **The input need only be readable at processing time.** The output is permanent.
- **Location is derived from the digest** (observo#626's rule); Watcher stores no
  output URI.

### The change path

```
schedule → content.fetch → Replicator → content.blobs → Watcher
  apply_fetch_blob: stamp last_full_fetch_at, copy validators,
                    fetch row → PROCESSING, persist + publish content.process(spec[0])
  Observo:          read gs:// input, verify digest, extract(one spec),
                    write blobs/<sha256>.bin if absent, publish content.derived
  apply_derived:    supersession guard →
                    empty      → next spec (new command_id) | last spec → extraction-failure path
                    no latest  → baseline
                    equal      → no change; #293 renewal with the raw blob's provenance
                    different  → Option A policy → ChangeRevision + content.revisions
                                 + CHANGE_DETECTED(previous_fingerprint, current_fingerprint,
                                                   extraction_changed)
```

A 304 produces no bytes and no process command; that path is unchanged. Raw-blob
provenance (`blob_uri`, `blob_expires_at`, `source_media_type`) still comes from the
fetch fact and still travels to Archiver on `content.revisions`; Watcher simply never
opens the raw blob again.

## Section 2 — Watcher

### State

**`process_commands`** — the issuer's outbox/pending-map/inbox for the processing
leg, one row per occasion, keyed on `command_id`:

```
process_commands
  command_id          TEXT PK
  fetch_command_id    TEXT NOT NULL  -- FK fetch_commands ON DELETE CASCADE
  watched_item_id     ULID NOT NULL  -- ON DELETE CASCADE
  spec_index          INT  NOT NULL  -- which source_spec this occasion ran
  intent_id           TEXT NOT NULL  -- lineage across re-issues
  status              TEXT NOT NULL  -- pending_publish | in_flight | completed |
                                     -- failed | superseded | expired
  issued_at, published_at, fact_at   TIMESTAMPTZ
  reissue_count       INT NOT NULL DEFAULT 0
  -- fact fields, NULL until the fact lands:
  output_digest, output_uri, output_size_bytes, output_media_type,
  empty, spec_fingerprint, schema_version, processor_version,
  failure_reason, failure_detail
```

Same issuer discipline as `fetch_commands`: persist-before-publish plus the
every-minute publish sweep; correlate on `command_id` only; idempotent fact upsert;
a reaper on signal age with its own timeout (`WATCHER_PROCESS_COMMAND_TIMEOUT_SECONDS`)
re-issuing under a fresh id against the **shared** `WATCHER_FETCH_MAX_REISSUES` cap
(it caps a lineage; the lineage now spans both legs).

**`FetchCommandStatus.PROCESSING`** — set by `apply_fetch_blob` in place of
`SUCCEEDED`; a member of `OPEN_STATUSES`, so the one-open-command gate and the
fetch reaper keep working off one table. The derived fact closes the fetch row
(`SUCCEEDED`, or `FAILED` with `failure_reason="processing_failed"` /
`"processing_timeout"`).

**`ChangeRevision`** gains `spec_fingerprint TEXT NULL` and `processor_version TEXT
NULL`. Both nullable: a NULL on an existing row means *unknown* and triggers neither
label nor re-baseline. No URI column.

**`WatchedItem`** gains `processor_version TEXT NULL` — the value from the item's
latest derived fact, feeding `validator_source_key` (below).

### Apply table

| Fact | Handling |
|---|---|
| blob fact | stamp `last_full_fetch_at`, copy validators (#269 unchanged), fetch row → `PROCESSING`, issue process command for spec[0] |
| derived, non-empty | supersession guard; then baseline / equal (#293 renewal) / change (Option A) |
| derived, `empty` | spec[i+1] exists → issue it (new `command_id`, same `intent_id`); else the existing extraction-failure path: `CHECK_EXTRACTION_FAILED`, ERROR health, `clear_validators` |
| `processing_failed`, terminal | extraction-failure path, `failure_detail` in the audit |
| `processing_failed`, `input_unreadable` | #275 semantics: re-fetch, capped; `failure_reason="blob_unreadable"` |
| `processing_failed`, non-terminal | nothing; the reaper re-issues on timeout |
| derived for a superseded command | discarded, `SUPERSEDED` |

### What leaves Watcher

- `_extract_and_fingerprint`, `_extract_with_spec`, `_spec_fingerprint_or_none`,
  the registry's extractor slot, `resolve_dispatch_essence` /
  `extraction_overrides_for_essence` (lifted into co-core), and the `co-core[extract]`
  extra with its PDF/Excel dependencies (a #307 memory win).
- Raw-blob reads on the apply path. `aread_blob` survives for one purpose: reading
  derived text from Observo's bucket (Section 4).
- `process_watched_item` reduces to a pure fingerprint comparison plus the Option A
  policy; it takes a `DerivedOutcome` in place of `raw_content`.

### Conditional GET (#269)

`EXTRACTION_GENERATION` is currently read from the installed co-core. After the
switch Watcher has none, so the generation component of `validator_source_key`
becomes `WatchedItem.processor_version`. Residual: Watcher learns of an Observo
upgrade only on the next full fetch, so a 304-ing item inherits its fingerprint until
the 168 h age ceiling forces one. Accepted; the gate is off in production.

### Bus inventory

Publishes 4 → **5** (`content.process`), consumes 2 → **3** (`content.derived`).
`tests/test_bus_stream_kinds.py`'s pinned set and the broker's `docs/STREAMS.md`
table both change; `group_name`, never a literal.

## Section 3 — Observo (requirements, not implementation)

1. **Broker membership** (broker repo): ACL user `observo` — consumer-group read on
   `content.process`, publish on `content.derived`; participants table; tailnet
   reachability `observo-primary` → `broker`.
2. **Consumer** — group `observo.process` via `group_name`; at-least-once tolerated
   by write-if-absent; undecodable frames to a DLQ; transient errors publish nothing
   (pending entry reclaimed); deterministic errors publish `terminal=true`.
3. **Input** — `gs://co-gcs-blobs` via the co-core `BlobStore` (cannobserv#475) with
   an `objectViewer` grant. observo#491's `gs://` refusal governs operator-typed job
   URLs; bus inputs are a separate path and that rule is not extended to them. Verify
   the bytes hash to `input_digest`; mismatch is terminal.
4. **Extraction** — co-core HTML/PDF/CSV extractors from one spec, co-core's
   media-type dispatch; co-core pin raised, and **matched to Watcher's version at
   cutover** (Section 5). `processor_version` per D4.
5. **Output** — `canonical_text` bytes to `gs://co-gcs-observo/blobs/<sha256>.bin`
   through observo#628 step 2's write path and `blobs` table; write-if-absent, never
   deleted; `empty` writes nothing.
6. **A scoped read grant for Watcher** — observo#628 puts every artifact under
   `blobs/`; a bucket-wide grant would expose all of it. Scope to derived text (an
   IAM condition or a dedicated prefix/bucket — Observo's call). The contract states:
   read-only, derived text only.
7. **Failure isolation** — text extraction runs in its own process, outside the
   media worker fleet's failure domain (archiver#179's stated counterweight). Whether
   a command also mints an asset Job is Observo's decision; at Watcher's volume
   (≤ ~100 commands/day) a lighter path than Job + 2 processes is recommended, not
   required.

## Section 4 — the diff and the notification

**Event metadata** (`CHANGE_DETECTED`) gains `previous_fingerprint`,
`current_fingerprint` (the two storage addresses) and `extraction_changed ∈ {null,
"spec", "processor"}`, derived by comparing the two revisions' `spec_fingerprint` and
`processor_version`.

**Computation** — in `dispatch_event_notifications`, once per event, before the
recipient loop, only if some recipient's `ContentOptions` requests a diff:

1. `aread_blob` both texts from Observo's bucket, location derived from the digest.
2. Verify `sha256(text) == fingerprint` for each; mismatch ⇒ no diff + WARNING.
3. Cap each input (`WATCHER_DIFF_MAX_INPUT_BYTES`, default 1 MiB); over ⇒ "diff
   unavailable: content too large".
4. `difflib.unified_diff` in `asyncio.to_thread` — one process serves API, consumers
   and tasks.

**Restored from `89d64eb`**: `_build_diff_text`, `_render_unified_diff_block`,
`_normalize_unified_diff_lines`, `_truncate_unified_diff_lines` (hunk-boundary-aware,
with its tests), `_DEFAULT_DIFF_SNIPPET_CAP`; `ContentOptions.include_diff_snippet` /
`diff_snippet_lines` / `include_diff_full`; the `diff_snippet` / `diff_full` template
variables and the Changes form group; preview-fixture parity (`unified_diff` on both
sides of `test_change_detected_matches_pipeline_metadata`).

**Not restored** (D8): `significance`; `added` / `modified` / `removed` /
`chunks_changed` / `change_summary` — the canonical bytes do not preserve chunk
boundaries.

**Failure handling** — any read/verify/diff error ⇒ no diff, notification still sent
with "(diff unavailable)", WARNING logged. Nothing raises into the apply path.

**Defaults** — diff snippet on (capped), full diff off. #221's toggle-only-when-
observable criterion is met once the diff is real.

**Option A (D6)**

| `extraction_changed` | Behaviour |
|---|---|
| `null` | notify as today |
| `"spec"` | notify; body carries *"the source spec changed — some of this difference may come from the new selector"* |
| `"processor"` | record the revision, send **nothing**, write `CHECK_REBASELINED` (dashboard-visible). Accepted residual: a real change coincident with an extractor upgrade is absorbed. |

**Dashboard** — no diff view in v1. #273 proceeds (delete the orphaned diff2html
assets).

## Section 5 — cutover, failure modes, testing

### Cutover: `WATCHER_EXTRACT_MODE = local | shadow | observo`

- **Step 0 (before any mode exists).** co-core ships `canonical_text`; Watcher calls
  it in place of its `"\n".join` with a golden-digest test proving byte identity;
  Watcher starts recording `spec_fingerprint` + `processor_version` (from local
  `EXTRACTION_GENERATION`) on new revisions.
- **`shadow`.** Local extraction still decides. Watcher also issues process commands
  and records facts on `process_commands`; a comparator checks `output_digest`
  against the local fingerprint and records any mismatch (log line + audit). Observo
  is storing text throughout, so **the diff ships here**: any revision whose local
  fingerprint equals Observo's digest already has its text stored.
- **`observo`.** Gated on evidence, not a calendar: **zero** mismatches across a
  shadow window that contains at least one real change event (the equal-fingerprint
  case exercises less of the extractor than a change does). A mismatch that slips through is caught by Option A — the processor version
  differs, so the item re-baselines silently rather than firing a false notification.
- **Delete.** After a post-switch soak: the local path, the mode variable, the
  `[extract]` extra. Until then rollback is `local`.

### Failure modes

| Failure | Behaviour |
|---|---|
| Observo down | Commands wait in the stream; the reaper re-issues to the cap, then ERROR health (`processing_timeout`) — visible. Beyond the raw blob's TTL, `input_unreadable` triggers a capped re-fetch. |
| Broker down | Existing #287/#288 behaviour. |
| Derived-text read fails | No diff; notification still sent. |
| Stored text corrupt | Hash check fails ⇒ no diff + WARNING. |
| Fact for a superseded command | Existing supersession guard, applied to the derived leg. |
| Observo publishes a wrong digest | Shadow comparator catches it pre-switch; Option A absorbs it post-switch. |

### Testing (TDD, red first)

- **co-core**: golden digests for `canonical_text`; model tests for the three types;
  `stream_kind` for both streams.
- **Watcher unit**: every apply branch in Section 2's table; `process_commands`
  publish sweep + reaper + the `PROCESSING` gate; Option A (label, silent re-baseline,
  audit); the shadow comparator; the diff (cap, thread offload, failure ⇒ no diff,
  hash check); preview parity; `test_bus_stream_kinds.py` re-pinned at 5/3.
- **Integration**: a fake Observo publishing on the scratch bus
  (`WATCHER_DEV_BUS_REDIS_URL`).
- **Production acceptance for the switch**: the shadow mismatch count.

## Section 6 — work split and order

| # | Repo | Work | Depends on |
|---|---|---|---|
| 1 | co-core | `canonical_text`; media-type dispatch lifted; the three models; two streams in `streams.py`; `processor_version` scheme | — |
| 2 | co-core | cannobserv#475 — `BlobStore` lifted (open) | — |
| 3 | Watcher | Step 0 | 1 |
| 4 | broker | `observo` ACL user, two streams, participants table, tailnet | 1 |
| 5 | Observo | Section 3 items 2–7 | 1, 2, 4, observo#628 step 2 |
| 6 | Watcher | `process_commands` + `PROCESSING`, issuer, consumer, reaper, shadow comparator | 1, 5 |
| 7 | Watcher | Section 4 — ships in shadow (#222 proper) | 6 |
| 8 | Watcher | Switch after zero mismatches; soak; delete local extraction | 7 |
| 9 | Archiver | Informational: archiver#179 notes the reusable contract; `content.revisions` unchanged | 1 |

**Critical path** 1 → 5 → 6; 3 and 4 run in parallel with 5.

### #222 disposition

Retitle to *Restore change detail by diffing Observo-derived text*; replace the stale
"blocked on Replicator" header with a pointer to this doc; #222 becomes step 7. New
Watcher issues for steps 3, 6, 8. Chunk semantics and `significance` close won't-do.
#273 proceeds.

## Out of scope

Chunk-level `added`/`removed`; a dashboard diff view; JSON extraction
(cannobserv#354); Archiver issuing derivations; a processor-version pin on the
command; Observo's internal choice of Job vs lighter path; the GCS grant mechanism.
