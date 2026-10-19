# Watch events

Watcher reports its **edges** to Archiver as facts on `info.watch-events` (#369;
contract `docs/change_bus/watch.md` at cannobserv v0.37.0, consumer group
`archiver.watch-events`). The `info.watch-status` stream carries **levels**;
this one carries what happened. Watcher's own notification dispatch runs beside
it until F3 (#376) moves composition to Archiver.

## The three edges

| Fact | Staged in | When |
|---|---|---|
| `change_detected` | `apply_extraction_outcome` (`src/workers/pipeline.py`) | A notified content change: the fingerprint moved and Option A did not call it a re-baseline |
| `watch_error` | `record_check_failure` (`src/workers/fetch_commands.py`) | The edge **into** ERROR from any other health, `unknown` and `probing` included |
| `watch_recovered` | `_record_check_success` (same file) | ERROR → OK |

**Nothing else is an edge.** A baseline, a cache hit (including the #293
renewal), an Option A re-baseline (`CHECK_REBASELINED`), a later failure inside
the episode and a #71 re-notify reminder stage nothing: re-notify cadence is
Archiver's, from the error edge plus `info.watch-status` `health`. PROBING →
OK is not a recovery.

**What each fact carries:**
- `watch_error.reason` is the `health_reason` written at the same edge, and
  `status_code` is `health_status_code`. So the extraction-failure path sends
  `processing_failed`, even though its notification metadata still says
  `extraction_failed`.
- `watch_recovered.error_since` is `watched_items.error_since`. That column is
  stamped on the error edge and cleared on every exit from ERROR, including the
  dashboard's URL edit into PROBING. That exit ends the episode with no recovery
  fact, which the contract allows. There is **no backfill**: an item already in
  ERROR at deploy recovers with `null`, which is stated in the payload rather
  than omitted.
- `change_detected.current_text_uri` is the processor's `output_uri` from the
  answer that closed the check. `previous_text_uri` comes from
  `stored_text_location` (`process_commands`, the way the diff finds it), and
  is `null` for a text stored before shadow mode.

## The outbox: `pending_watch_events`

Each writer (`src/core/watch_events.py`) adds one row to the caller's session
and commits nothing. The edge's own commit holds both the state change and its
fact, or neither. Tests prove this with a commit log
(`tests/workers/watch_event_helpers.py`): the first commit that leaves the item
in its new health contains the fact.

- **`id` is the `event_id`**, which is also the envelope key. It is minted at
  the edge, so a redelivery (a crash between `XADD` and the drain's commit)
  carries the same key.
- **`payload` is the fact's field map.** A new kind is an entry in
  `WATCH_EVENT_EMITS` and needs no migration (#371's `check_rejected`).
- **No FK to the item.** A fact outlives a revoke, because the edge happened.
  Archiver closes an open episode on the revoke itself.
- **Validated at drain time, never at the edge.** An Emit refusal at the edge
  would raise inside the transaction holding the health change and roll it
  back, which is the hazard co-core's `WatchHealthReason` comment names. A
  `null` reason (a fetch fact that carried none) is staged as it is and
  dead-letters at the drain.

## The drain: `drain_pending_watch_events`

`src/workers/watch_events_drain.py` runs every minute and publishes in **id
order**, with no `maxlen` (#327).

- **Head-of-line.** A pass stops at the first row it cannot send: a transient
  publish failure, or a row still serving its backoff. A recovery therefore
  never reaches the stream before the error edge it closes. The head's backoff
  (60 s doubling to 1 h, `mark_failure`) is the whole queue's. Once the head
  publishes, the rest follow in the same pass, so `clear_backoffs` (#291) has
  no counterpart here.
- **Failure classes** match `content.revisions`. A payload its Emit refuses,
  or an unknown kind, dead-letters at once and the queue moves past it. A
  broker refusal (down, full, or `NOPERM`) is transient and never
  dead-letters. `TRANSIENT_PUBLISH_ERRORS` is shared (`src/core/bus.py`).
- **No bus** is logged at ERROR only while a fact is waiting.

**Deploy gate.** Watcher's ACL user needs `+xadd` on `~info.watch-events`
(CannObserv/broker#93). If the drain is deployed without it, every edge becomes
one `NOPERM` per backoff step in the broker's `ACL LOG`, and each one needs
attributing (#387). Verify a live publish with a granted read: `XINFO STREAM`,
or `XRANGE` with an explicit start. Never use `XREVRANGE`, which is refused.

## Operating it

```bash
source scripts/load-env.sh
# Queued facts: count, oldest, the head's attempts — a growing count with a
# stuck head means the broker is refusing (journal: "holding the queue").
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT count(*), min(created_at) AS oldest, max(attempts) AS max_attempts
   FROM pending_watch_events WHERE dead_lettered_at IS NULL;"
# Facts that will never publish on their own.
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT id, event_type, info_item_id, last_error FROM pending_watch_events
   WHERE dead_lettered_at IS NOT NULL ORDER BY id DESC LIMIT 10;"
```
