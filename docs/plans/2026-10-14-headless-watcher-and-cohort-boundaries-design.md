# Headless Watcher and cohort boundaries — design

**Date:** 2026-10-14 · **Status:** approved in brainstorming, pending doc review
**Repos touched:** watcher (parent epic), cannobserv, archiver, broker, replicator, notifier

## Goal

Make Watcher a **headless service**: no HTML dashboard, no `/api/v1` operator API,
only `GET /health` / `GET /ready`. Archiver is the cohort's control plane and its only
human and programmatic surface (UI + API + SDK). Every operator control Watcher holds
today either moves to Archiver, travelling over the bus, or is deliberately retired.

A second goal, equal in weight: **state the boundaries between the services and the
rules for how they communicate in one normative place**. Today they are scattered
across four repos (archiver#137, replicator `docs/contracts/replicator-boundaries.md`,
the registry-announcement design, notifier `AGENTS.md`). This work makes them a charter.

## Context

- No script or service calls Watcher's `/api/v1`. Archiver retired its Watcher SDK
  and deeplinks (archiver#142) and makes no HTTP call to Watcher.
- Already registry-owned (#251, #254): create, `effective_url`, `source_specs`,
  `announced_schedule_config`, `is_active`.
- Still Watcher-only, with no bus path: notification templates and dispatch, domains
  (`min_interval` → `content.fetch-policy`, domain cadence, the `domain_suspended`
  break-glass), temporal profiles, the throttle floor, archive/restore/delete,
  item-local fields, check-now, spec acknowledgement, audit, API keys.
- Production usage measured 2026-10-13:

  | | count |
  |---|---|
  | watched items | 5, all reconciled |
  | temporal profiles | 0 |
  | throttle floors | 0 |
  | item cadences set | 1 |
  | domains | 4, all `min_interval=1`, none suspended, no domain cadence |
  | notification templates | 2, global, no custom Jinja |
  | dispatches, last 30 days | 17 |
  | API keys | 1 |

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Fully bus-only (no `/api/v1`)**, not "HTML only" | No callers. Archiver is becoming the API + SDK. |
| D2 | **Placement rule 1 is the governing charter** (below) | Already written in archiver#137, never applied across the board. |
| D3 | **Notifications: Watcher emits facts; Archiver owns subscriptions, composition and dispatch; Notifier delivers** | Who-hears-what is operator intent (rule 1). Whether a change occurred is Watcher's observation (rule 3). Notifier's own non-goals rule out a notifier that consumes the bus. Supersedes "Watcher monitors and notifies" (the 2026-09-24 Observo design) with **"Watcher monitors and reports; Archiver decides who hears; Notifier delivers."** |
| D4 | **Domain policy: Archiver is the only owner and produces `content.fetch-policy` directly**. Domain cadence is resolved inside Archiver. Watcher keeps only `domain_suspended` as host-local break-glass. | Watcher's `min_interval` has no local consumer; it only relays. One owner, one fewer hop, and a stream mirror moved rather than duplicated. Supersedes #137's placement of `min_interval` with Watcher. |
| D5 | **Ported to Archiver:** temporal profiles with their post-actions and the throttle floor; archive/restore/delete; tags; item cadence; global/domain/item notification scopes; check-now; error reasons; `last_full_fetch_at` | User decision. Usage is low, but these remain operator intent. |
| D6 | **Retired:** item-local `name`/`description` (Archiver composes from InfoItem), the `content_media_type` override, spec acknowledgement (#274), API create, `/probe`, API keys and app users, `/revisions` | No consumer once Watcher is headless, or already duplicated in Archiver. |
| D7 | **Temporal profiles are executed by Archiver**, which re-announces resolved interval/`active` at each boundary | Rule 4: resolved document, never a program. No new spec grammar for Watcher to interpret, so no mirror obligation. |
| D8 | **Cutover: contracts first → build dark → one hard flip per capability → Watcher deletes in one jump** | Two fetch-policy producers fight LWW. Two notification paths double-send. |
| D9 | **Charter lives in `cannobserv/docs/BOUNDARIES.md`. Design of record and parent epic live in watcher. One child epic per affected repo.** | The charter is cohort-wide and sits next to `streams.py` / `CHANGE_BUS.md`. Under the cross-repo policy this session files issues and does not edit sibling repos. |
| D10 | **`audit_log` stays** as Watcher's local forensic record, with no surface | Retiring it is work with no payoff. |
| D11 | **No metrics endpoint is built** | None exists today. Archiver's panel and the broker probes are the observability. |

## The charter (proposed text for `cannobserv/docs/BOUNDARIES.md`)

### Service roles

| Service | Owns | Never owns |
|---|---|---|
| **Archiver** | Registrar and control plane. The cohort's only human/programmatic surface (UI, API, SDK). **All operator-declared intent**: what is watched, whether, how often (incl. temporal profiles), host politeness, lifecycle (archive/restore/delete), tags, who is told and how. Composes and dispatches notifications. System of record for items, sources, revisions. | Fetching, extracting, fingerprinting |
| **Watcher** | **Execution of watching**: scheduling against announced cadence, issuing fetch and process commands, change determination (change vs. re-baseline vs. renewal), health. Reports observations. | Any operator intent. Any surface beyond health and a host-local break-glass CLI. |
| **Replicator** | Byte mechanism (fetch, replicate, persist); politeness mechanism | Policy |
| **Processor / Observo** | Transforms | Policy |
| **Notifier** | Delivery, **outside the cluster**: HTTP, publication only, no event taxonomy, never loads consumer data | Routing, subscriptions, composition |
| **Broker** | Transport: streams, ACLs, retention probes | Payload semantics |

### Placement rules

1. **Operator-declared intent lives in Archiver.** Every operator-settable knob, without exception.
2. **Mechanism lives with the service that executes it.** If a mechanism has an operator
   lever, that lever is intent (rule 1). The one exception is **break-glass**, which stays
   host-local so it works when Archiver is down.
3. **Observations and derivations live with the service that computes them.** They are
   published, never stored as another service's intent.
4. **Time-evolving intent is resolved by its owner.** Announce resolved values, never a
   program for another service to evaluate.

### Channel rules

| What travels | Channel |
|---|---|
| Intent | **Config/state** stream from the owner: LWW per key, generation-ordered, full-set republish, explicit tombstones. The payload is a resolved spec. |
| An occasion ("do this once") | **Command** stream to the executor; one group |
| An observed edge ("this happened") | **Fact** stream; one group per consuming service; outboxed atomically with the state change that produced it |
| An observed level ("this is the case") | **Config/state** stream back to the intent owner; never an ack path |
| HTTP | Only at the cohort edge: humans and clients ↔ Archiver; any service → Notifier. **No service-to-service HTTP inside the cluster.** |

### Specs

- Each spec is owned by its authoring service as a versioned JSON Schema, with
  `schema_version` required.
- Specs travel untyped; co-core models envelopes, not grammars.
- Where a consumer must interpret a grammar, the interpretation lives in co-core `pure`
  and is declared as a **mirror obligation**.
- Glossary (for the charter):

  | Spec | What it is | Where it lives on the consumer |
  |---|---|---|
  | Source Specification (`source_specs`) | Formerly "InfoSpec" | — |
  | WatchSpec | Cadence | Watcher's `announced_schedule_config` |
  | RepSpec | Never travels as a document | Rendered into `ContentReplicateCommand` |
  | `*State` models | Config/state payloads | — |

## Contracts (cannobserv / co-core)

### C-a. New fact stream `info.watch-events` (Watcher → Archiver)

Observed edges. Each one is outboxed in the same transaction as the Watcher state change
that produced it, which replaces today's "dispatch before commit, no outbox".

Every model carries the common fields `event_id` (occurrence key), `info_item_id`,
`info_source_id` and `occurred_at`, plus:

| Model | Fields | Emitted on |
|---|---|---|
| `ChangeDetectedEvent` | `previous_fingerprint`, `current_fingerprint`, `extraction_changed` (`"spec"` or null), `previous_changed_at`, `previous_text_uri` + `previous_text_sha256`, `current_text_uri` + `current_text_sha256` | A detected change |
| `WatchErrorEvent` | `reason`, `status_code?` | OK→ERROR edge only |
| `WatchRecoveredEvent` | `error_since` | ERROR→OK edge |
| `CheckRejectedEvent` | `command_id`, `reason` ∈ {`paused`, `fetch_open`, `domain_suspended`, `no_url`, `unknown_item`} | Rejected check command (outcome fact for C-b) |

Rules for this stream:

- Re-baselines (only the extractor changed) and #293 renewals emit **nothing**.
- Text URIs are resolved by Watcher through `process_commands`, so Archiver needs no
  `content.derived` join.
- **Re-notify cadence moves to Archiver**, timed from the `WatchErrorEvent` edge plus the
  `health` level. Watcher emits nothing per failed check.
- **Rejected alternative:** folding change into `content.revisions`. That stream's key
  identifies a slot (#293 re-announces under it); notification needs an occurrence key.
- `*Emit` twins; DLQ permitted (fact stream); Archiver group `archiver.watch-events`.

### C-b. New command stream `info.watch-check` (Archiver → Watcher)

- `WatchCheckCommand{command_id, info_item_id, requested_at}`. It always forces a full
  fetch, the #269 semantics of check-now. Group `watcher.watch-check`; DLQ permitted.
- Success is observed as a level: watch-status `last_attempt_at` passes `requested_at`,
  plus a `ChangeDetectedEvent` if the content changed.
- Rejection is `CheckRejectedEvent`.

### C-c. `WatchStatusState` gains three levels

`last_full_fetch_at` (closes archiver#172), `health_reason`, `health_status_code`.

### C-d. `content.fetch-policy` changes producer, Watcher → Archiver

- Payload unchanged, plus an **optional `generation`**. Replicator applies a frame only if
  its generation is newer than the stored one.
- Reason: Archiver publishes through an outbox, and an outbox can reorder frames.
- The retention mirror moves to Archiver: the cap of 500, the floor of
  `RETAINED_FULL_SETS`=10, and the broker `FullSetFloor` thresholds (#292, #319).

### C-e. `info.registry` unchanged

- Stays at WatchSpec v1.
- After F2, Archiver always sends a concrete `interval`, resolved in the order
  profile → item → domain.
- Watcher keeps its system default only for an absent interval.

## Sibling-repo work

### Archiver

- **Domains (sole owner):**
  - Add `min_interval` and default cadence to `domains`.
  - Produce `content.fetch-policy`, with tombstones, the retention mirror and `generation`.
  - Domain deactivation is intent: it resolves into `active=false` on that host's
    announced items and publishes the fetch-policy tombstone.
- **Cadence resolution:** profile → item `watch_spec.interval` → domain default, announced
  as a concrete interval.
- **Temporal profiles:**
  - Model, API and UI; one per InfoItem.
  - A daily boundary job re-announces resolved values. It is idempotent across restarts.
  - Post-actions resolve as: `reduce_frequency` → item interval 1d; `deactivate` →
    `active=false`; `archive` → archived state.
- **Watch lifecycle:** archive and restore. The archived-vs-paused distinction is
  Archiver-local; the wire carries only `active`. Delete stays the existing `revoked`.
- **Tags** on InfoItem.
- **Notifications:**
  - A subscription model: scope global/domain/item, an event set, content options,
    `remote_channel_id`.
  - A composer ported from Watcher's `content.py`, `diff.py`, `diff_loader.py`,
    `default_templates.py` and `preview_fixtures.py`, Archiver-local. It needs read access
    to the processor bucket.
  - An `info.watch-events` consumer with a shadow mode, then dispatch through an outbox
    with idempotency key `archiver:{event}:{subscription_id}:{event_id}`.
  - The error-reminder timer, preview and test-send.
  - Message links point at Archiver's item page.
  - A notifier credential behind a unit-only enable flag (the watcher #277/#278 pattern).
- **Check-now:** publish `info.watch-check`. The UI shows "pending" until `last_attempt_at`
  passes `requested_at`, or shows the rejection reason.
- **Watch panel:** `last_full_fetch_at`, health reason and code.
- **Docs:**
  - Fix the stale "remove the WatchedItem in Watcher by hand"
    (`src/api/routes/info_items.py:872,903`, `docs/SCHEMA.md:117`).
  - Point the break-glass runbook (`deploy/README.md`) at Watcher's CLI.
  - Link the charter.

### Notifier (operations only, no code)

- Provision an Archiver tenant and API key.
- Recreate the 3 production channels under it, or move them between tenants if notifier
  supports that; the issue asks.
- After F3 and the jump, revoke Watcher's tenant and keys.

### Replicator

- Apply `FetchPolicyState.generation` only if newer, when it is present.
- `replicator-boundaries.md` names Archiver as the fetch-policy producer and links the
  charter as its parent.

### Broker

- **ACLs:**
  - Archiver: `+xadd content.fetch-policy`, `+xadd info.watch-check`.
  - Watcher: `+xadd info.watch-events` and group read on `info.watch-check`; at the jump,
    `content.fetch-policy` publish is revoked.
  - Archiver: group read on `info.watch-events`.
- `STREAMS.md` inventory and probes for both new streams.
- `FullSetFloor`'s mirrored constants are sourced from Archiver.

## Watcher end state

**Remains**

- **Process:** one uvicorn process: lifespan, embedded Procrastinate worker, consumers,
  sweeper.
- **HTTP:** `GET /health`, `GET /ready`.
- **Publishes (5):** `content.fetch`, `content.process`, `content.revisions`,
  `info.watch-status`, `info.watch-events`.
- **Consumes (4):** `content.blobs`, `content.derived`, `info.registry`,
  `info.watch-check`.
- **Core:**
  - scheduling against the announced interval
  - the fetch, process, revisions and watch-events outboxes
  - change determination, health, media-type detection
  - the watch-status producer
  - the check-command consumer
  - `audit_log`
- **Break-glass:** a `src/ops/` CLI, `suspend-domain <host>` / `--resume`. It sets
  `domain_suspended` and writes an audit row.
  - After F1 it no longer tombstones `content.fetch-policy`, because Watcher no longer
    produces that stream.
  - It stops Watcher issuing `content.fetch` for the host. Those commands are the only
    origin traffic Watcher causes.

**Deleted**

- **Code:**
  - `src/dashboard/`
  - `/api/v1` and its schemas
  - `src/core/notifications/`, `src/core/notifier_client/`, `src/core/public_base_url.py`,
    `src/core/probe.py`, `src/core/fetch_policy.py`
  - `src/workers/notify.py`, `src/workers/fetch_policy.py`
  - temporal-profile evaluation and the post-actions in `schedule_tick`
  - the domain cadence tier in `src/core/scheduling/`
- **Tables:** `notification_templates`, `temporal_profiles`, `api_keys`, `app_users`,
  `fetch_policy_tombstones`.
- **`watched_items` columns:** `name`, `description`, `default_tags`,
  `default_schedule_config`, `domain_default_schedule_config`, `throttle_floor_interval`,
  `last_reviewed_at`, `last_error_notified_at`, `archived_at`.
- **`domains` columns:** `min_interval`, `default_schedule_config`, `notes`,
  `archived_at`. `domains` keeps the suspension flag and the derived facts.
- **Config:**
  - `WATCHER_NOTIFIER_*`, `WATCHER_DEV_NOTIFIER_*`, `/etc/watcher/notifier.env`, the
    `WATCHER_NOTIFIER_ENABLED` gate
  - `WATCHER_PUBLIC_BASE_URL`, `WATCHER_ERROR_RENOTIFY_INTERVAL`
  - `WATCHER_FETCH_POLICY_STREAM_MAXLEN`
- **Dependencies:** the notifier SDK, Jinja/HTMX assets.
- **Tooling:** Tailwind CLI, `build-css.sh`, `check-css.sh` and its pre-commit hook, the
  npm pin in AGENTS.md.
- **Docs:**
  - Deleted: `STYLE.md`, `UI.md`, `WATCHED-ITEMS-DASHBOARD.md`.
  - Rewritten: `WATCHED-ITEMS.md`, `REGISTRY.md`, `BUS.md`, `ARCHITECTURE.md`,
    `ENVIRONMENT.md`, AGENTS.md, linking the charter.
- **Tests:**
  - `tests/dashboard/` and the API route tests are deleted.
  - `tests/test_bus_stream_kinds.py` is re-pinned to 5 published / 4 consumed.

**Kept on purpose**

- `content_media_type`: auto-detected mechanism, no override.
- `domain_suspended`: break-glass.
- `audit_log`: forensic record.

## Cutover

**Phase 0 — contracts (blocks everything)**

- cannobserv: `BOUNDARIES.md`, C-a…C-d, then a co-core release.
- broker: additive ACLs and inventory.

**Phase 1 — build additive and dark (repos in parallel)**

- **Watcher:**
  - Outbox `info.watch-events` while still dispatching itself.
  - Publish the new watch-status levels.
  - Run the `info.watch-check` consumer live. This is safe because nobody publishes to it
    yet.
- **Archiver:** all surfaces behind flags. The watch-events consumer runs in **shadow
  mode**: it composes and records what it would send, and sends nothing.
- **Replicator:** consumer applies `generation`.
- **Notifier:** Archiver tenant and channels.
- **Gate into Phase 2 — shadow parity:** every Watcher `NOTIFICATION_DISPATCHED` in the
  shadow window has a matching Archiver composition.

**Phase 2 — hard flips, one at a time, no dual-run**

| Flip | Steps |
|---|---|
| **F1 fetch-policy** | Import the 4 `min_interval`s into Archiver → Watcher's producer stops → Archiver's producer starts with a full-set republish. The gap between the two runs on Replicator's conservative default, which is benign. |
| **F2 cadence / profiles** | Migrate the 1 item cadence → Archiver announces concrete intervals → Watcher drops the domain tier and profile code. There are 0 profiles to lose, and this also removes the unguarded `is_active` write in `src/workers/tasks.py` post-actions. |
| **F3 notifications** | Migrate the 2 templates → set a **cutover instant T** in both services. Watcher dispatches events with `occurred_at` < T; Archiver dispatches events at or after T. No gap and no double-send, because the events persist in Archiver's consumer group. |
| **F4 check-now** | Enable Archiver's button. |

**Phase 3 — the jump**

1. Export remaining data: names, descriptions, tags.
2. Watcher deletes everything listed under "Deleted"; migrations drop the tables and
   columns.
3. Notifier revokes Watcher's tenant.
4. Broker revokes Watcher's `content.fetch-policy` grant.
5. Docs are rewritten.

**Phase 4 — hygiene, in parallel, non-blocking**

- cannobserv: the DLQ wording contradiction (config/state has "no DLQ" vs. "the DLQ is its
  terminal state").
- archiver:
  - the schemas' `$id` URLs, which still point at the released `watcher.exe.xyz`
  - `rep_spec` lacks `schema_version`
  - `source_spec` "cross-check" vs. co-core "fallback" wording
  - `info.registry` 50k cap → size it against the set (the #292 lesson)
- broker:
  - `STREAMS.md` "event" → "fact"
  - the roster is missing the live `provisioner.revisions` group

## Failure modes

| Down | Effect | Recovery |
|---|---|---|
| Archiver | No notifications, no cadence changes, fetch-policy frozen at the last LWW value | Events wait in `archiver.watch-events`; on return they are delivered at least once and deduped by `event_id`. Cadence stays at the last announcement. |
| Watcher | No checks, so no events | Nothing is lost. |
| Broker | Outboxes hold on both sides | They drain on return; that path already exists. |
| Malformed `WatchCheckCommand` | — | Goes to `info.watch-check.dlq`. |
| Unknown or revoked item in a check command | `CheckRejectedEvent` with `unknown_item` | — |

The break-glass works with Archiver down. That is the reason it stays host-local.

## Verification

- **co-core:**
  - `*Emit` twins
  - `_STREAM_KINDS` entries for both new topics
  - DLQ only on fact and command streams
- **Watcher:**
  - The event row commits if and only if the state change commits.
  - No event on re-baseline or renewal.
  - Pre-flight produces the rejection fact.
  - Stream-kind pins at 5/4.
  - Break-glass CLI tests.
  - TDD throughout, per AGENTS.md.
- **Archiver:**
  - Composer tests ported along with the preview fixtures.
  - Shadow parity before F3.
  - The profile boundary job is idempotent.
  - The fetch-policy producer keeps the #292/#319 retention pins.

## Issue breakdown

Epics are cross-linked. Dependencies are in brackets.

### watcher — parent epic: "Headless Watcher and cohort boundaries"

- **W1** Outbox `info.watch-events` (change, error, recovered) alongside existing dispatch [C2]
- **W2** Publish `last_full_fetch_at`, `health_reason`, `health_status_code` on watch-status [C4]
- **W3** Consume `info.watch-check`: pre-flight, forced full fetch, `CheckRejectedEvent` [C3, B1]
- **W4** Break-glass CLI `suspend-domain` / `--resume`
- **W5** Export production data for Archiver's migration: templates, item cadence, domain
  intervals, names, descriptions, tags
- **W6** F1: stop the `content.fetch-policy` producer [A2]
- **W7** F2: drop the domain cadence tier, temporal profiles, post-actions and throttle floor [A3, A4]
- **W8** F3: cutover instant T; stop dispatch [A9]
- **W9** Jump: delete `src/dashboard/` and the CSS tooling
- **W10** Jump: delete `/api/v1` (keep health), `api_keys`, `app_users`, probe
- **W11** Jump: delete the notification subsystem, notifier client, config and gates
- **W12** Jump: migrations dropping the retired tables and columns
- **W13** Docs and AGENTS.md rewrite; link the charter

### cannobserv — epic: "Cohort boundary charter and headless-Watcher contracts"

- **C1** `docs/BOUNDARIES.md` (charter + spec glossary)
- **C2** `info.watch-events` stream and models
- **C3** `info.watch-check` stream and `WatchCheckCommand`
- **C4** `WatchStatusState` new levels
- **C5** Optional `FetchPolicyState.generation`
- **C6** DLQ wording fix
- **C7** co-core release carrying C2–C5

### archiver — epic: "Control plane absorbs Watcher's operator surface"

- **A1** Domains as sole owner (`min_interval`, default cadence, deactivation resolution)
- **A2** `content.fetch-policy` producer, dark until F1 [C5, B1]
- **A3** Cadence resolution → concrete announced interval
- **A4** Temporal profiles + daily boundary job + post-actions
- **A5** Watch lifecycle archive/restore
- **A6** InfoItem tags
- **A7** Notification subscriptions model + API/UI (global/domain/item)
- **A8** Composer port, preview, test-send, processor-bucket read access
- **A9** `info.watch-events` consumer: shadow → dispatch, outbox, idempotency, cutover T,
  reminder timer [C2, B1, N1, A10]
- **A10** Notifier credential + unit-only enable flag
- **A11** Check-now via `info.watch-check` [C3]
- **A12** Watch panel: `last_full_fetch_at`, health reason (closes #172) [C4]
- **A13** Docs: stale "remove by hand", break-glass runbook, charter link
- **A14** Hygiene: `$id` URLs, `rep_spec` `schema_version`, cross-check wording,
  `info.registry` cap

### broker — epic: "Streams and ACLs for headless Watcher"

- **B1** Additive ACLs + `STREAMS.md` inventory + probes for both new streams
- **B2** `FullSetFloor` mirror sourced from Archiver (at F1)
- **B3** Revoke Watcher's `content.fetch-policy` grant (Phase 3)
- **B4** Hygiene: "event" → "fact", `provisioner.revisions` in the roster

### replicator — epic: "Fetch-policy producer moves to Archiver"

- **R1** Apply `FetchPolicyState.generation` only if newer [C5]
- **R2** `replicator-boundaries.md`: Archiver is the producer; the charter is its parent

### notifier — epic: "Archiver becomes the notifying tenant"

- **N1** Provision an Archiver tenant and key; recreate or move the 3 channels
- **N2** Revoke Watcher's tenant and keys after F3 and the jump

## Out of scope

- A metrics endpoint.
- A co-core home for the composer or the diff algorithm (YAGNI).
- WatchSpec v2.
- Moving `domain_suspended` off the Watcher host.
- Digest notifications (#63). They now belong to Archiver's subscriber; watcher#63 is to
  be re-sited.
- Observo's per-job stream scheme, which is outside the taxonomy.
- The #293 slot-key re-announcement question. It is noted, works today, and stays
  unchanged.

## Research sources

- **co-core:** `co_core/pure/adapters/bus/streams.py`, `envelope.py`, `models/changes.py`.
- **Watcher:** `docs/BUS.md`, `docs/REGISTRY.md`, `docs/WATCHED-ITEMS*.md`,
  `src/core/notifications/`.
- **Archiver:**
  - archiver#137, #142, #150, #151, #158, #172
  - `docs/plans/2026-08-10-registry-announcement-channel-design.md`
  - `docs/plans/2026-06-25-observer-cluster-integration-strategy-design.md` (decision 6:
    Notifier out of cluster)
- **Replicator:** `docs/contracts/replicator-boundaries.md`.
- **Notifier:** `AGENTS.md` (scope: publication only), notifier#43.
