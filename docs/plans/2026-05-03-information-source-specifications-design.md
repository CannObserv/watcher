---
title: Information Source Specifications & Watcher Boundary Realignment
date: 2026-05-03
status: approved (design)
---

# Information Source Specifications & Watcher Boundary Realignment

## Goal

Reposition Watcher as a narrowly-scoped **change detector** within a larger information-tracking architecture. Stop treating Watcher as the custodian of captured data; instead, treat it as a runtime that monitors a portable, externally-authored specification and emits structured change events. Move heavy data capture and archival to a sibling **Storage** service. Establish the **InformationSourceSpecification (InfoSpec)** as the load-bearing artifact that bridges all participating services.

## Background

Watcher today: URL + selector → fetch HTML fragment → simhash → "changed/not changed" → notification. The artifact Watcher emits is a *signal*, the data is incidental, and notifications are the only consumer. The notifier extraction (#132) already separated user-facing alerts into a sibling service.

The next evolution shifts the conceptual core from "URL changed" to "the *Information* at this source has new content." That data must be captured, versioned, and archived — but doing so inside Watcher would balloon storage and entangle it with concerns it should not own. The right move is to externalize the *specification* of how to find a piece of Information, and let multiple services (Watcher, Storage, eventually others) consume that specification independently.

## Vocabulary

| Term | Definition | Lifecycle |
|---|---|---|
| **Information** | A stable, externally-named target being tracked. Identified by ULID. Persistent identity across all services. | Created once, never re-keyed. Different source data ⇒ different Information. |
| **InformationSourceSpecification (InfoSpec)** | A portable, versioned document describing *how* to source a given Information (URL, selector, fetch options, fingerprint algorithm, fallback locators, etc.). Identified by ULID. | Many revisions per `information_id`, monotonically versioned. Authored by Claude (with the user) via tools-first workflow; uploaded to each consumer service. |
| **Watch** | Watcher-internal runtime record: "we monitor Information X according to current InfoSpec Y on schedule Z, and emit changes + notifications when fingerprints drift." | Bound 1:1 to an `information_id`. Tracks `current_info_spec_id`, fingerprint history, schedule. |
| **Change event** | The structured, machine-facing event Watcher publishes when a fingerprint shifts. Carries `information_id`, fingerprints, spec version reference. No payload bytes. | Streamed via Redis. Replayable by consumers. |
| **Notification** | User-facing alert (email, Slack, push) routed through the Notifier service. | Distinct from Change events. |

## Key architectural decisions

### 1. Payload shape: opaque, non-persistent extraction

Watcher fetches origin → extracts via the InfoSpec selector → computes a fingerprint → discards the bytes. Watcher's persistent state per Watch:

```
{ watch_id, information_id, current_info_spec_id, info_spec_version,
  current_fingerprint, last_checked_at, last_changed_at,
  recent_fingerprint_history }
```

No payload bytes are stored long-term. The Storage service fetches origin itself, using its own copy of the InfoSpec, when notified of a change. Origin sees ~1× traffic per actual change (Watcher's fetch can be lighter than Storage's archival fetch).

### 2. Outbound transport: Redis Streams behind a vendored abstraction

Watcher publishes change events (and, in later phases, spec events and notifications) to **Redis Streams** via a thin `EventPublisher` interface. The interface exposes the lowest-common-denominator semantics across Redis Streams and Kafka:

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

A `RedisStreamsPublisher` ships now; a `KafkaPublisher` slot is reserved for future migration. **Partition key = `information_id`** so all events for a given Information are ordered relative to each other across spec revisions.

A **durable local outbox** sits between change detection and publish: detect → write changeset row to Postgres → publish to Redis → mark published → purge. The outbox is small (purges on ack) and exists solely as insurance against broker downtime — change events cannot be re-derived after the fact, so they must not be lost.

### 3. InfoSpec is portable and version-stamped (Q5 = D)

No central spec registry. Claude composes an InfoSpec, uploads it to each consumer service via that service's own API. Each consumer holds its own copy.

Drift is made first-class via version stamping:
- InfoSpecs are immutable; edits create a new `info_spec_version` (monotonic int per `information_id`).
- Every change event is stamped with the InfoSpec version Watcher used to produce it.
- Consumers refuse / park events whose `info_spec_version` they don't yet have, until their own copy catches up.

The InfoSpec **schema** itself is versioned separately (`schema_version`, major bumps for incompatible changes). Schema definitions and JSON Schema exports live in this repo for now under `src/core/info_spec/` — extracting them into a shared library is a follow-on concern, not a blocker.

| Layer | Versions | Mechanism |
|---|---|---|
| InfoSpec **schema** | The shape of the document itself | `schema_version: 1` field |
| InfoSpec **document** | A specific Information's evolving spec | `info_spec_version: int`, monotonic per `information_id` |

### 4. Tools-first authoring (Q4 = A)

Watcher exposes a small set of authoring tools (initially HTTP, eventually MCP-shaped) so Claude — running anywhere the user invokes it — can compose, validate, and publish InfoSpecs:

- `fetch_and_render(url)` — returns rendered HTML + headers + screenshot
- `propose_selectors(url, description)` — ranked candidate selectors with extracted preview and stability score
- `preview_extraction(url, selector, options)` — returns what would be captured + computed fingerprint
- `validate_info_spec(doc)` — schema validation + dry-run extraction
- `publish_info_spec(doc)` — registers/updates the InfoSpec in Watcher (mirrored separately to Storage)
- `test_watch(information_id)` — one-shot fetch + extract + diff against current baseline

Watcher does **not** embed an LLM agent loop. The agent lives in Claude (Claude Code, claude.ai, MCP clients). Watcher exposes capabilities; Claude orchestrates. A dashboard "Configure with Claude" form is a possible future addition but is out of scope here.

### 5. Self-healing: fallback locators + structured rebind events (Q6)

Three layers, in order:

1. **Floor — detect + alert.** Every breakage produces a dashboard alert and a notification. Always available.
2. **Auto-heal common cases — fallback locators.** InfoSpecs include 1–2 fallback locators (DOM landmarks like "the section whose H2 contains 'Active Licenses'") authored alongside the primary. When primary fails, Watcher tries fallbacks; if one yields plausible content, it switches and emits `info_spec.healed_via_fallback`. Catches the bulk of routine breakages (renamed classes, added wrappers).
3. **Structured handoff for hard breakages — `info_spec.rebind_requested` event.** When fallbacks can't recover, Watcher emits a rich event (broken spec, recent page samples, fingerprint history) that an external agent (initially human + Claude, later automated) consumes to author a new InfoSpec version.

Watcher itself never invokes an LLM. Healing intelligence lives outside.

### 6. Outbound bus: unified across event types (Q7 follow-up)

Redis Streams becomes Watcher's general outbound event bus:

```
info.changes        → Storage (and any other change consumer)
info.spec_events    → spec.broken, spec.healed_via_fallback, spec.rebind_requested
info.notifications  → Notifier (Phase 3 — HTTP path remains during transition)
```

All keyed by `information_id`. All publish through the same `EventPublisher`.

Notifications and Change events stay **conceptually distinct** (different audiences, schemas, retention, fanout, idempotency keys) but ride the same physical infrastructure. Reductively conflating them as "just notifications" creates a discriminated-union type whose branches share almost nothing — keep them as peer event types.

### 7. Pre-production migration: rename, no preservation

No production data; no backwards compatibility constraints. The existing `watches` table is renamed and extended:

- `watch_id` (ULID) — keeps existing column shape
- `information_id` (ULID, new) — required, unique per Watch
- `current_info_spec_id` (ULID, new)
- `current_info_spec_version` (int, new)
- `schema_version` (int, new)
- fallback-locator structure (stored on InfoSpec, referenced by Watch)
- existing fingerprint / scheduling fields retained

A new `info_specs` table holds the lineage of InfoSpec revisions per Information.

## Sequencing

### Phase 1 — InfoSpec model + change-event bus

- Define InfoSpec schema (`src/core/info_spec/`), JSON Schema export, validation
- Add `information_id`, InfoSpec lineage tables, schema migrations
- Implement `EventPublisher` abstraction + `RedisStreamsPublisher`
- Add Redis to deployment (systemd, env vars, dependency on `watcher.service`)
- Local outbox for change events; drain worker via Procrastinate
- Publish `info.changes` events on fingerprint drift
- Reference consumer in `tools/` that subscribes, validates, writes JSONL — proves the contract end-to-end

### Phase 2 — InfoSpec authoring tools + Storage service stand-up

- Ship `fetch_and_render`, `propose_selectors`, `preview_extraction`, `validate_info_spec`, `publish_info_spec`, `test_watch` as HTTP endpoints
- Stand up minimal FastAPI **Storage** service on this VM (sibling to Watcher and Notifier) — receives InfoSpec uploads, subscribes to `info.changes`, fetches origin, archives, versions
- End-to-end: Claude authors InfoSpec → uploads to Watcher + Storage → Watcher detects change → Storage archives

### Phase 3 — Self-healing surfaces

- Fallback-locator support in InfoSpec schema and runtime
- `info.spec_events` topic with `spec.broken`, `spec.healed_via_fallback`, `spec.rebind_requested`
- Dashboard surfacing: alert UI, "needs review" Watch state, rebind event detail view

### Phase 4 — Notifier on the bus

- Notifier consumes `info.notifications` from Redis
- Watcher dual-writes to HTTP + Redis behind a flag (mirror of `USE_REMOTE_NOTIFY` pattern)
- Deprecate HTTP path once Redis path is proven

### Later (off this VM)

- Storage service relocates off this VM
- InfoSpec schema package extracts into a shared library

## Out of scope

- **Structured extraction.** Watcher captures opaque data only. Schema-typed extraction (option C from Q1) is a Storage / downstream concern if ever needed.
- **Embedded LLM agent in Watcher.** Tools-first only. No agent loops in this process.
- **Dashboard "Configure with Claude" form.** Possible future addition; not in this design.
- **Notifier migration to Redis** (Phase 4 — fenced from Phase 1–3 work).
- **Cross-service auth / authz beyond what the Notifier extraction already established.** API keys + bearer tokens reused from existing patterns.
- **Automatic rebind agents.** `info_spec.rebind_requested` is consumed by humans + Claude initially; automation comes later without Watcher changes.
- **Storage service relocation off-VM.** Stays on this VM until contracts settle.
- **InfoSpec schema package extraction.** Stays in this repo until consumers stabilize.
- **Production data migration.** Pre-production state — rename freely.

## Open questions / follow-ups

- Exact JSON Schema for InfoSpec v1 (selector grammar, fetch options, fingerprint algorithms supported, fallback-locator shape) — to be drafted as Phase 1 kicks off.
- Detection heuristics for "broken InfoSpec" (empty extraction, size anomalies, churn rate) — implementation detail; pin down during Phase 3.
- Storage service's archival format and retention policy — owned by Storage, not Watcher; coordinated cross-service as Phase 2 progresses.
- How `EventPublisher` exposes Redis Stream `MAXLEN` trimming policy without leaking abstraction — likely a `retention` arg on stream creation, not per-publish.
- Whether reference consumer in `tools/` lives in this repo or graduates to a sibling repo when Storage stands up.

## References

- #132 — Notifier extraction (parent architectural pattern)
- `docs/plans/2026-05-02-notifier-adapter.md` — recent notifier work
- `AGENTS.md` — project conventions
