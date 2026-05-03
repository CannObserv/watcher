---
title: Information Source Specifications & Watcher Boundary Realignment
date: 2026-05-03
status: approved (design)
---

# Information Source Specifications & Watcher Boundary Realignment

## Goal

Reposition Watcher as a narrowly-scoped **Change detector** within a larger information-tracking architecture. Stand up a sibling **Information service** (prototyped in this repo, designed for later extraction) that owns the canonical registry of Information Items and their Source Specifications. Watcher and the Archive service become consumers that pull InfoSpecs from the Information service via a generated Python SDK at runtime — no replication, no drift. The **InformationSourceSpecification (InfoSpec)** is the load-bearing artifact, but the Information service — not the artifact's portability — guarantees consistency across consumers.

## Background

Watcher today: URL + selector → fetch HTML fragment → simhash → "changed/not changed" → Notification. The artifact Watcher emits is a *signal*, the data is incidental, and Notifications are the only consumer. The Notifier extraction (#132) already separated user-facing alerts into a sibling service and established the cross-service patterns we'll reuse: separate FastAPI app, separate systemd unit, generated Python SDK from OpenAPI, cross-repo iteration on this VM before later relocation.

The next evolution shifts the conceptual core from "URL changed" to "the *Information Item* at this source has new content." That data must be captured, versioned, and archived — but doing so inside Watcher would balloon storage and entangle it with concerns it should not own. The right move is to externalize *both* the registry of Information Items and the specifications for sourcing them, behind a dedicated service that all consumers query.

## Vocabulary

The word "information" is ambiguous (singular vs. plural). To avoid that, we use:

- **Information Item** (singular) — one specific thing being tracked.
- **Information Items** (plural) — the collection.
- **Information service** — the service that manages Information Items.

| Term | Definition | Lifecycle |
|---|---|---|
| **Information Item** | A stable, externally-named target being tracked. Identified by a `information_item_id` ULID. Owned by the Information service. Persistent identity across all consumer services. | Created once in the Information service, never re-keyed. Different source data ⇒ different Information Item. |
| **InformationSourceSpecification (InfoSpec)** | An immutable document describing *one way* to source a given Information Item (URL, selector, fetch options, fingerprint algorithm). Identified by `info_spec_id` ULID. | Owned by the Information service. An Information Item has a prioritized list of InfoSpecs; the primary is tried first, fallbacks in order. New revisions are authored as new InfoSpecs and inserted into the priority list. |
| **Watch** | Watcher-internal runtime record: "we monitor Information Item X on schedule Z, and emit Changes + Notifications when fingerprints drift." Bound 1:1 to an `information_item_id`; resolves the prioritized InfoSpec list dynamically from the Information service. | Created in Watcher when the user requests monitoring of a known `information_item_id`. |
| **Change** | The structured, machine-facing record Watcher publishes when a fingerprint shifts. Carries `information_item_id` and the `info_spec_id` Watcher used. No payload bytes. Aligns with Watcher's existing `changes` table terminology. | Streamed via Redis. Replayable by consumers. |
| **Notification** | User-facing alert (email, Slack, push) routed through the Notifier service. | Distinct from Changes. |

## Key architectural decisions

### 1. Information service is the canonical registry (port 8020)

A new FastAPI service, prototyped in this repo as `src/information/`, run as its own systemd unit (`information.service`) on **port 8020**, with its own Postgres schema in the existing instance. Extraction to a sibling repo follows the Notifier pattern (#132).

**Responsibilities:**
- CRUD for Information Items (`information_item_id` ULID, name, description, owner, lineage metadata).
- CRUD for InfoSpecs (immutable; `info_spec_id` ULID, schema version, document body).
- A junction model that prioritizes InfoSpecs per Information Item (see decision 3).
- Read API: "give me the prioritized active InfoSpec list for `information_item_id` X."
- Authoring tools surface (decision 6 below) — these belong here, not in Watcher.

**Out of scope for the Information service:** fingerprinting, scheduling, archival, fetching at monitoring cadence. It is a registry + authoring surface, not a runtime.

### 2. Consumer model: pull + cache via generated SDK

Consumer services (Watcher, Archive) do **not** persist InfoSpec documents. Instead, they consume the Information service through a generated Python SDK and cache resolved data short-term.

**SDK pattern, mirroring Notifier (`/home/exedev/notifier/clients/python/`):**
- Information service ships `scripts/dump_openapi.py` and `clients/python/scripts/regen.sh`.
- `regen.sh` runs `openapi-python-client generate` against a dumped spec, output goes to `clients/python/src/information_client/generated/`.
- Hand-written wrapper (`information_client`) exposes ergonomic helpers around the generated low-level client.
- Watcher and Archive add the SDK as a path dependency during the on-VM prototype phase (matching how Watcher consumes `notifier_client` today); the SDK publishes to a real index after extraction.

**Runtime resolution:**
- A Watch references an `information_item_id`.
- Watcher calls `client.list_active_info_specs(information_item_id)` → ordered list of InfoSpecs by priority.
- Watcher caches the list keyed by `information_item_id` with a short TTL (default 60s, tunable).
- Individual InfoSpec documents are content-stable by ULID; cache hits are unambiguous.
- On extraction failure that looks like a stale list, force-refresh before flagging breakage.
- Future optimization (out of scope for Phase 1): the Information service publishes `info_spec.created` Changes to a topic and consumers reactively invalidate caches.

This eliminates drift entirely — one source of truth, consumers converge within their TTL.

### 3. Fallbacks are alternative InfoSpecs in a priority junction

A previous draft of this design baked "fallback locators" into a single InfoSpec. Refactor: fallbacks are simply **alternative InfoSpecs** for the same Information Item, ordered by priority via an explicit junction.

**Schema (Information service):**

```
information_items
  information_item_id  ULID  PK
  name, description, owner, …

info_specs
  info_spec_id    ULID  PK
  schema_version  int
  document        JSONB    -- the actual spec body
  created_at      timestamptz

information_item_info_specs        -- ordered junction
  information_item_id  ULID  FK
  info_spec_id         ULID  FK
  priority             int          -- 1 = primary, 2..N = fallbacks
  active               bool
  PRIMARY KEY (information_item_id, info_spec_id)
  partial UNIQUE INDEX (information_item_id, priority) WHERE active
```

**Consequence — InfoSpec ordering is explicit, not inferred from ULID timestamps.** A newer InfoSpec is not automatically primary; placement is a deliberate authoring action.

**Authoring API (Information service):**
- `GET /information-items/{id}/info-specs` — ordered list of active InfoSpecs by priority.
- `POST /information-items/{id}/info-specs` — append/insert a new InfoSpec at a given priority. Server reshuffles other priorities as needed; previous occupant of the slot may be demoted or deactivated per request payload.
- `PATCH /information-items/{id}/info-specs/{spec_id}` — change priority or `active` flag.
- (No update endpoint on InfoSpec body — InfoSpecs are immutable. Edits create a new InfoSpec.)

**Watcher runtime:**
- Resolves the list, tries InfoSpecs in priority order until one yields plausible content.
- If primary works → normal Change.
- If a non-primary works → emit `spec.healed_via_fallback` Change on the spec-changes topic so users can decide whether to promote it.
- If none work → emit `spec.rebind_requested`.

**Schema version (`schema_version`)** is still maintained per InfoSpec document — it describes the document grammar and is independent of priority ordering. Consumers refuse documents with unknown `schema_version`.

### 4. Payload shape: opaque, non-persistent extraction

Watcher fetches origin → extracts via the resolved InfoSpec → computes a fingerprint → discards the bytes. Watcher's persistent state per Watch:

```
{ watch_id, information_item_id, current_info_spec_id,
  current_fingerprint, last_checked_at, last_changed_at,
  recent_fingerprint_history }
```

`current_info_spec_id` records *which* InfoSpec produced the current fingerprint (useful for distinguishing primary vs. fallback success). The InfoSpec document itself lives only in the SDK's cache. No payload bytes are stored long-term. The Archive service fetches origin itself when notified of a Change — it also resolves InfoSpecs from the Information service. Origin sees ~1× traffic per actual Change.

### 5. Outbound transport: Redis Streams, no abstraction layer (YAGNI)

The previous draft proposed an `EventPublisher` Protocol with a Kafka-future placeholder. **Drop the abstraction.** The Observo project has been working through a similar broker abstraction and it has accumulated complexity that reaches into consumer code. Here we forego it.

What remains:

- A concrete `ChangePublisher` class in `src/core/changes/` with a Redis-Streams-specific implementation.
- Direct, simple API: `publisher.publish_change(topic, key, payload, headers) -> message_id`.
- No Protocol, no swappable backend, no Kafka slot. If a future migration becomes necessary, refactor at that point with knowledge of actual operational constraints, not speculation.

The publisher remains a discrete class (testable, isolated) — just not an abstraction-as-architecture.

**Topics and keys:**

```
info.changes        → Archive (and any other Change consumer)
info.spec_changes   → spec.broken, spec.healed_via_fallback, spec.rebind_requested
info.notifications  → Notifier (Phase 5 — HTTP path remains during transition)
```

All keyed by `information_item_id`, so all Changes for an Information Item are ordered relative to each other.

**Durable local outbox** sits between Change detection and publish: detect → write changeset row to Postgres (Watcher's existing `changes` table extends naturally) → publish to Redis → mark published. The outbox is small (rapidly purges on ack) and exists solely as insurance against broker downtime — Changes cannot be re-derived after the fact.

### 6. Tools-first authoring lives on the Information service (Q4 = A, refined)

The authoring tools belong on the Information service since that is where InfoSpecs and Information Items live:

- `fetch_and_render(url)` — returns rendered HTML + headers + screenshot
- `propose_selectors(url, description)` — ranked candidate selectors with extracted preview and stability score
- `preview_extraction(url, selector, options)` — what would be captured + computed fingerprint
- `validate_info_spec(doc)` — schema validation + dry-run extraction
- `create_info_spec(information_item_id, doc, priority)` — append a new InfoSpec to the priority list (immutable, new ULID)
- `create_information_item(name, description, initial_info_spec_doc)` — register a new Item + first InfoSpec atomically
- `find_information_item(query)` — search to avoid duplicates

Watcher exposes a separate, narrower tool surface for *monitoring* concerns:

- `create_watch(information_item_id, schedule)` — start monitoring a known Information Item
- `test_watch(information_item_id)` — one-shot fetch + extract + diff against the current baseline (Watcher resolves InfoSpecs from the Information service internally)

User flow: Claude composes/looks up an Information Item + InfoSpec on the Information service → returns the `information_item_id` → user (or Claude) calls Watcher's `create_watch` with that ULID. No InfoSpec ever crosses directly between user and Watcher.

No service embeds an LLM agent loop. The agent lives in Claude. Each service exposes capabilities; Claude orchestrates across them.

### 7. Self-healing: priority list + structured rebind Changes

Folds naturally out of decisions 3 and 5:

1. **Floor — detect + alert.** Every breakage produces a dashboard alert and a Notification.
2. **Auto-heal common cases — fallback InfoSpecs.** The priority list is the mechanism. When primary fails, Watcher tries the next active InfoSpec in priority order. If one succeeds, emit `spec.healed_via_fallback` on `info.spec_changes`. The user sees this on the dashboard and can promote the fallback to primary on the Information service.
3. **Structured handoff for hard breakages — `spec.rebind_requested`.** When all InfoSpecs in the list fail, Watcher emits a rich Change (current `information_item_id`, list of attempted `info_spec_id`s, recent page samples, fingerprint history). An external agent (initially human + Claude) authors a new InfoSpec on the Information service and inserts it at priority 1. Watcher's next cache refresh picks it up.

Watcher itself never invokes an LLM.

### 8. Archive service stand-up (port 8030)

Renamed from "Storage" to **Archive** to align with the broader information-pipeline vocabulary.

A new FastAPI service, prototyped on this VM as a sibling repo (modeled on Notifier extraction), running as `archive.service` on **port 8030**. Consumes:

- The Information service via the same generated `information_client` SDK.
- `info.changes` from Redis Streams.

On each Change, Archive resolves the InfoSpec used (the `info_spec_id` carried in the Change), fetches origin, archives, versions. Owns its archival format and retention policy.

### 9. Pre-production state: rename, no preservation

No production data; no backwards compatibility constraints. Watcher's existing `watches` table is reshaped:

- `watch_id` (ULID) — retained
- `information_item_id` (ULID, new) — required, unique per Watch
- `current_info_spec_id` (ULID, new) — last InfoSpec ULID Watcher resolved + used
- existing fingerprint / scheduling fields retained

The existing `changes` table extends to carry the Change records published to `info.changes` (also serves as the local outbox). No `info_specs` or `information_items` tables in Watcher — those live in the Information service.

The Information service starts with empty tables: `information_items`, `info_specs`, `information_item_info_specs`.

## Sequencing

### Phase 1 — Information service prototype + InfoSpec model

- Scaffold `src/information/` as a FastAPI app on **port 8020**, separate systemd unit (`information.service`), shared Postgres database with its own schema/tables.
- Define InfoSpec schema (`src/information/schemas/`), JSON Schema export, validation.
- CRUD endpoints: Information Items (create/read), InfoSpecs (create immutable / read), priority junction (create/reorder/deactivate), `GET /information-items/{id}/info-specs` (ordered list).
- Smoke tests: round-trip an InfoSpec, validate priority reordering semantics.

### Phase 2 — Information SDK + Watcher consumer model + Change bus

- Add `scripts/dump_openapi.py` to the Information service.
- Scaffold `clients/python/` with `regen.sh`, mirroring `/home/exedev/notifier/clients/python/`.
- Generate `information_client` SDK; hand-write ergonomic wrappers + TTL cache.
- Watcher adopts `information_client` via path dependency. Watch creation flow takes an `information_item_id`.
- Implement concrete `ChangePublisher` (Redis Streams, no Protocol).
- Add Redis to deployment (systemd, env vars).
- Local outbox via the existing `changes` table; drain worker via Procrastinate.
- Publish `info.changes` on fingerprint drift.
- Reference consumer in `tools/` that subscribes, validates, writes JSONL — proves the contract end-to-end.

### Phase 3 — Authoring tools on the Information service + Archive stand-up

- Ship `fetch_and_render`, `propose_selectors`, `preview_extraction`, `validate_info_spec`, `create_information_item`, `create_info_spec`, `find_information_item` on the Information service.
- Stand up minimal FastAPI **Archive** service on this VM (**port 8030**), reuses `information_client`, subscribes to `info.changes`, fetches origin, archives, versions.
- End-to-end: Claude authors Information Item + InfoSpec on Information service → user creates Watch on Watcher with the `information_item_id` → Watcher detects Change → Archive archives.

### Phase 4 — Self-healing surfaces

- Watcher tries InfoSpec list in priority order at runtime.
- Emit `spec.healed_via_fallback` and `spec.rebind_requested` to `info.spec_changes`.
- Dashboard: alert UI, "needs review" Watch state, rebind detail view, "promote fallback" action that calls the Information service.

### Phase 5 — Notifier on the bus

- Notifier consumes `info.notifications` from Redis.
- Watcher dual-writes to HTTP + Redis behind a flag (mirror of `USE_REMOTE_NOTIFY` pattern).
- Deprecate HTTP path once Redis path is proven.

### Later (off this VM)

- Information service extracts to a sibling repo and relocates off-VM.
- Archive service relocates off-VM.

## Out of scope

- **Structured extraction.** Watcher captures opaque data only.
- **Embedded LLM agent in any service.** Tools-first only.
- **Dashboard "Configure with Claude" form.** Possible future addition; not in this design.
- **Reactive cache invalidation via `info_spec.created` Changes.** Phase 1 uses TTL only.
- **Notifier migration to Redis** (Phase 5 — fenced from earlier phases).
- **Cross-service auth / authz beyond what the Notifier extraction already established.**
- **Automatic rebind agents.** `spec.rebind_requested` is consumed by humans + Claude initially.
- **Information service / Archive relocation off-VM.**
- **InfoSpec schema package extraction.** Stays inside the Information service until consumers stabilize.
- **Production data migration.** Pre-production state.
- **A swappable broker abstraction.** Deferred indefinitely. Use Redis Streams directly.

## Open questions / follow-ups

- Exact JSON Schema for InfoSpec v1 (selector grammar, fetch options, fingerprint algorithms supported) — Phase 1.
- Whether Information service shares Postgres with Watcher (separate schema, same instance) or gets its own database. Default: separate schema, same instance.
- Cache TTL default and whether it should be per-Information Item. Default: 60s global; revisit.
- Detection heuristics for "broken InfoSpec" (empty extraction, size anomalies, churn rate) — Phase 4.
- Archive's archival format and retention policy — owned by Archive.
- Whether `tools/` reference consumer lives in this repo or graduates to the Archive repo when it stands up.
- Priority-reorder semantics on `POST /information-items/{id}/info-specs` (auto-shift vs. require explicit priority on every existing entry) — Phase 1.

## References

- #132 — Notifier extraction (parent architectural pattern)
- `/home/exedev/notifier/clients/python/` — SDK pattern to mirror for `information_client`
- `docs/plans/2026-05-02-notifier-adapter.md` — recent Notifier work
- `AGENTS.md` — project conventions
