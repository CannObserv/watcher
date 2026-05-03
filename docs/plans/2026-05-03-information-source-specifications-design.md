---
title: Information Source Specifications & Watcher Boundary Realignment
date: 2026-05-03
status: approved (design)
---

# Information Source Specifications & Watcher Boundary Realignment

## Goal

Reposition Watcher as a narrowly-scoped **change detector** within a larger information-tracking architecture. Stand up a sibling **Information service** (prototyped in this repo, designed for later extraction) that owns the canonical registry of Information targets and their Source Specifications. Watcher and Storage become consumers that pull InfoSpecs from the Information service at runtime — no local copies, no drift. Move heavy data capture and archival to a sibling **Storage** service. The **InformationSourceSpecification (InfoSpec)** is the load-bearing artifact, but the Information service — not the artifact's mere portability — is what guarantees consistency across consumers.

## Background

Watcher today: URL + selector → fetch HTML fragment → simhash → "changed/not changed" → notification. The artifact Watcher emits is a *signal*, the data is incidental, and notifications are the only consumer. The notifier extraction (#132) already separated user-facing alerts into a sibling service.

The next evolution shifts the conceptual core from "URL changed" to "the *Information* at this source has new content." That data must be captured, versioned, and archived — but doing so inside Watcher would balloon storage and entangle it with concerns it should not own. The right move is to externalize *both* the description of an Information target and the specification of how to source it, behind a dedicated service that all consumers query.

## Vocabulary

| Term | Definition | Lifecycle |
|---|---|---|
| **Information** | A stable, externally-named target being tracked. Identified by ULID. Owned by the Information service. Persistent identity across all consumer services. | Created once in the Information service, never re-keyed. Different source data ⇒ different Information. |
| **InformationSourceSpecification (InfoSpec)** | An immutable document describing *how* to source a given Information (URL, selector, fetch options, fingerprint algorithm, fallback locators). Identified by ULID. ULID timestamp gives free chronological ordering — no separate version field. | Owned by the Information service. Many InfoSpecs can exist per `information_id`; the most recent ULID is canonical. Source revision ⇒ author a new InfoSpec, never mutate an existing one. |
| **Watch** | Watcher-internal runtime record: "we monitor Information X on schedule Z, and emit changes + notifications when fingerprints drift." Bound 1:1 to an `information_id`; resolves the current InfoSpec dynamically from the Information service. | Created in Watcher when the user requests monitoring of a known `information_id`. |
| **Change event** | The structured, machine-facing event Watcher publishes when a fingerprint shifts. Carries `information_id` and the `info_spec_id` Watcher used. No payload bytes. | Streamed via Redis. Replayable by consumers. |
| **Notification** | User-facing alert (email, Slack, push) routed through the Notifier service. | Distinct from Change events. |

## Key architectural decisions

### 1. Information service is the canonical registry

A new FastAPI service, prototyped in this repo as `src/information/` and run as its own systemd unit on a separate port (e.g. 8002), with extraction to a sibling repo modeled on the Notifier pattern (#132).

**Responsibilities:**
- CRUD for Information records (`information_id` ULID, name, description, owner, lineage metadata).
- CRUD for InfoSpecs (`info_spec_id` ULID, `information_id` foreign key, document body, schema version).
- Read API: "give me the latest InfoSpec for `information_id` X" (latest ULID timestamp wins).
- Authoring tools surface (the tools listed under decision 5 below — these belong on the Information service, not Watcher).

**Out of scope for the Information service:** fingerprinting, scheduling, archival, fetching at monitoring cadence. It's a registry + authoring surface, not a runtime.

### 2. Consumer model: pull + cache, never replicate

Watcher (and later Storage) does **not** store its own copy of InfoSpec documents. Instead:

- A Watch references an `information_id`.
- At runtime, Watcher resolves the current InfoSpec by calling Information service's `GET /informations/{id}/info-spec/latest` (or equivalent).
- Watcher caches the resolved InfoSpec by `info_spec_id` with a short TTL (default 60s — tunable). Cache is keyed by ULID, so it's content-stable: the *same* `info_spec_id` is always the *same* document.
- On cache miss / expiry, refresh. On extraction failure that looks like a stale spec, force-refresh before flagging breakage.
- Future optimization: Information service publishes `info_spec.created` events to the bus; consumers subscribe and invalidate caches reactively. Out of scope for Phase 1; TTL is enough.

This eliminates drift entirely. There is exactly one source of truth, and consumers always converge on it within their cache TTL.

### 3. No version field on InfoSpec — ULID timestamp orders the lineage

InfoSpecs are immutable, ULID-identified documents. The ULID's embedded timestamp gives chronological ordering for free. To represent a source revision, the user authors a *new* InfoSpec against the same `information_id`; the new ULID's timestamp will be later, so it's automatically the latest.

The InfoSpec **schema** itself (the *shape* of the document) is still versioned: a `schema_version: int` field on each document, with major bumps for incompatible changes. This is orthogonal to InfoSpec identity — it describes the document's grammar, not the spec's place in a lineage.

| Layer | Versioned how |
|---|---|
| InfoSpec **schema** (document shape) | `schema_version: 1` field on each document |
| InfoSpec **lineage** (per-Information history) | ULID timestamps on `info_spec_id` — no separate field |

### 4. Payload shape: opaque, non-persistent extraction

Watcher fetches origin → extracts via the resolved InfoSpec → computes a fingerprint → discards the bytes. Watcher's persistent state per Watch:

```
{ watch_id, information_id, current_info_spec_id,
  current_fingerprint, last_checked_at, last_changed_at,
  recent_fingerprint_history }
```

`current_info_spec_id` records *which* spec produced the current fingerprint, but the InfoSpec document itself lives only in cache (re-fetched from the Information service on demand). No payload bytes are stored long-term. The Storage service fetches origin itself when notified of a change — it also resolves InfoSpecs from the Information service. Origin sees ~1× traffic per actual change.

### 5. Tools-first authoring lives on the Information service (Q4 = A, refined)

The authoring tools previously sketched for Watcher belong on the **Information service**, since that's where InfoSpecs live:

- `fetch_and_render(url)` — returns rendered HTML + headers + screenshot
- `propose_selectors(url, description)` — ranked candidate selectors with extracted preview and stability score
- `preview_extraction(url, selector, options)` — returns what would be captured + computed fingerprint
- `validate_info_spec(doc)` — schema validation + dry-run extraction
- `create_info_spec(information_id, doc)` — appends a new InfoSpec to the Information's lineage (immutable, new ULID)
- `create_information(name, description, initial_info_spec_doc)` — registers a new Information + first InfoSpec atomically
- `find_information(query)` — search existing Informations to avoid duplicates

Watcher exposes a separate, narrower tool surface for *monitoring* concerns:

- `create_watch(information_id, schedule)` — start monitoring a known Information
- `test_watch(information_id)` — one-shot fetch + extract + diff against current baseline (Watcher resolves the InfoSpec from the Information service internally)

User flow: Claude composes/looks up an Information + InfoSpec via the Information service → Information service returns the `information_id` → user (or Claude) calls Watcher's `create_watch` with that ULID. No InfoSpec ever crosses directly between user and Watcher.

No service embeds an LLM agent loop. The agent lives in Claude. Each service exposes capabilities; Claude orchestrates across them.

### 6. Outbound transport: Redis Streams behind a vendored abstraction

Watcher publishes change events (and, in later phases, spec events and notifications) to **Redis Streams** via a thin `EventPublisher` interface:

```python
class EventPublisher(Protocol):
    async def publish(
        self,
        topic: str,
        key: str,           # partition key
        payload: bytes,
        headers: dict[str, str],
    ) -> str: ...           # message id / offset
```

A `RedisStreamsPublisher` ships now; a `KafkaPublisher` slot is reserved for future migration. **Partition key = `information_id`**, so all events for a given Information are ordered relative to each other across InfoSpec revisions.

A **durable local outbox** sits between change detection and publish: detect → write changeset row to Postgres → publish to Redis → mark published → purge. The outbox is small (purges on ack) and exists solely as insurance against broker downtime — change events cannot be re-derived after the fact.

### 7. Self-healing: fallback locators + structured rebind events (Q6)

Three layers, in order:

1. **Floor — detect + alert.** Every breakage produces a dashboard alert and a notification. Always available.
2. **Auto-heal common cases — fallback locators.** Each InfoSpec includes 1–2 fallback locators (DOM landmarks like "the section whose H2 contains 'Active Licenses'") authored alongside the primary. When primary fails, Watcher tries fallbacks; if one yields plausible content, it switches and emits `info_spec.healed_via_fallback`. Catches the bulk of routine breakages (renamed classes, added wrappers).
3. **Structured handoff for hard breakages — `info_spec.rebind_requested` event.** When fallbacks can't recover, Watcher emits a rich event (current `info_spec_id`, recent page samples, fingerprint history). An external agent (initially human + Claude) consumes it, authors a *new* InfoSpec on the Information service against the same `information_id`. Watcher's next cache refresh picks up the new spec automatically.

Watcher itself never invokes an LLM. Healing intelligence lives outside.

### 8. Outbound bus: unified across event types

Redis Streams becomes Watcher's general outbound event bus:

```
info.changes        → Storage (and any other change consumer)
info.spec_events    → spec.broken, spec.healed_via_fallback, spec.rebind_requested
info.notifications  → Notifier (Phase 4 — HTTP path remains during transition)
```

Future: the Information service may also publish to its own topics (`info_spec.created`, `information.created`) so consumers can warm caches reactively. Out of scope for Phase 1.

All Watcher topics keyed by `information_id`. All publish through the same `EventPublisher`.

Notifications and Change events stay **conceptually distinct** (different audiences, schemas, retention, fanout, idempotency keys) but ride the same physical infrastructure.

### 9. Pre-production state: rename, no preservation

No production data; no backwards compatibility constraints. Existing `watches` table is renamed and reshaped:

- `watch_id` (ULID) — retained
- `information_id` (ULID, new) — required, unique per Watch
- `current_info_spec_id` (ULID, new) — last InfoSpec ULID Watcher resolved + used
- existing fingerprint / scheduling fields retained
- **No** `info_spec_version`, **no** local `info_specs` table — those concerns belong to the Information service

The Information service starts with empty tables: `informations`, `info_specs` (immutable, append-only).

## Sequencing

### Phase 1 — Information service prototype + InfoSpec model

- Scaffold `src/information/` as a FastAPI app on port 8002, separate systemd unit (`information.service`), shared Postgres database with its own schema/tables
- Define InfoSpec schema (`src/information/schemas/`), JSON Schema export, validation
- CRUD endpoints: create/read Informations, create/read InfoSpecs (immutable), `GET /informations/{id}/info-spec/latest`
- Smoke tests: round-trip an InfoSpec, validate ULID lineage ordering

### Phase 2 — Watcher consumer model + change-event bus

- Watcher gets an `InformationServiceClient` with TTL cache keyed by `info_spec_id`
- Replace existing Watch creation flow: takes an `information_id`, resolves InfoSpec from Information service
- Implement `EventPublisher` abstraction + `RedisStreamsPublisher`
- Add Redis to deployment (systemd, env vars)
- Local outbox for change events; drain worker via Procrastinate
- Publish `info.changes` events on fingerprint drift
- Reference consumer in `tools/` that subscribes, validates, writes JSONL — proves the contract end-to-end

### Phase 3 — Authoring tools on the Information service + Storage service stand-up

- Ship `fetch_and_render`, `propose_selectors`, `preview_extraction`, `validate_info_spec`, `create_information`, `create_info_spec`, `find_information` on the Information service
- Stand up minimal FastAPI **Storage** service on this VM (port 8003), uses the same `InformationServiceClient` pattern to resolve InfoSpecs, subscribes to `info.changes`, fetches origin, archives, versions
- End-to-end: Claude authors Information+InfoSpec on Information service → user creates Watch on Watcher with the resulting `information_id` → Watcher detects change → Storage archives

### Phase 4 — Self-healing surfaces

- Fallback-locator support in InfoSpec schema and Watcher runtime
- `info.spec_events` topic with `spec.broken`, `spec.healed_via_fallback`, `spec.rebind_requested`
- Dashboard surfacing: alert UI, "needs review" Watch state, rebind event detail view

### Phase 5 — Notifier on the bus

- Notifier consumes `info.notifications` from Redis
- Watcher dual-writes to HTTP + Redis behind a flag (mirror of `USE_REMOTE_NOTIFY` pattern)
- Deprecate HTTP path once Redis path is proven

### Later (off this VM)

- Information service extracts to a sibling repo and relocates off-VM
- Storage service relocates off-VM

## Out of scope

- **Structured extraction.** Watcher captures opaque data only. Schema-typed extraction (option C from Q1) is a Storage / downstream concern if ever needed.
- **Embedded LLM agent in any service.** Tools-first only. No agent loops in any process in this architecture.
- **Dashboard "Configure with Claude" form.** Possible future addition; not in this design.
- **Reactive cache invalidation via `info_spec.created` events.** Phase 1 uses TTL only; reactive invalidation is a future optimization.
- **Notifier migration to Redis** (Phase 5 — fenced from earlier phases).
- **Cross-service auth / authz beyond what the Notifier extraction already established.** API keys + bearer tokens reused from existing patterns.
- **Automatic rebind agents.** `info_spec.rebind_requested` is consumed by humans + Claude initially.
- **Information service / Storage service relocation off-VM.** They stay on this VM until contracts settle.
- **InfoSpec schema package extraction.** Stays inside the Information service until consumers stabilize.
- **Production data migration.** Pre-production state.

## Open questions / follow-ups

- Exact JSON Schema for InfoSpec v1 (selector grammar, fetch options, fingerprint algorithms supported, fallback-locator shape) — to be drafted as Phase 1 kicks off.
- Whether the Information service shares the existing Postgres database with a separate schema, or gets its own database. Default: separate schema, same instance, cleanest for prototyping.
- Cache TTL default and whether it should be per-InfoSpec (some Informations change fast, some don't). Default: 60s global, revisit.
- Detection heuristics for "broken InfoSpec" (empty extraction, size anomalies, churn rate) — pin down during Phase 4.
- Storage service's archival format and retention policy — owned by Storage, not Watcher.
- How `EventPublisher` exposes Redis Stream `MAXLEN` trimming without leaking abstraction — likely a `retention` arg on stream creation, not per-publish.
- Whether `tools/` reference consumer lives in this repo or graduates to a sibling repo when Storage stands up.

## References

- #132 — Notifier extraction (parent architectural pattern)
- `docs/plans/2026-05-02-notifier-adapter.md` — recent notifier work
- `AGENTS.md` — project conventions
