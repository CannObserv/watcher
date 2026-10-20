# Watch check

Archiver asks Watcher to "check this item now" with a `watch_check` command on
`info.watch-check` (#371; contract `docs/change_bus/watch.md` at cannobserv
v0.37.0). Watcher is the stream's one consumer group. Nobody issues commands
until F4 (#376), and Watcher holds group read only, so it cannot `XADD` one.

## The consumer: `src/workers/watch_check.py`

- **Group `watcher.watch-check`**, one member `watcher-watch-check-1`, started
  in the lifespan beside the fact inboxes. It runs on the same loop
  (`fetch_facts.run_fact_consumer`, a blocking `XREADGROUP` through
  co-core-aio), with two differences that come from this being a command stream:
  - **The group is created at `0`, never `$`** (CannObserv/broker#93). A
    command issued before the group existed is still owed an answer.
  - **An undecodable command is dead-lettered** to `info.watch-check.dlq`. It
    is not acked past. The loop reclaims the frame with `claim_stale_page`,
    which returns its raw fields, and `dead_letter` copies them there and acks
    the original. The DLQ is Watcher's: it writes, audits (`XLEN`/`XRANGE`)
    and `XDEL`s it, and nothing trims it.
- **Already accepted** (a `fetch_commands` row has this `check_command_id`)
  → `duplicate`, and nothing is done. A crash between the commit and the ack
  redelivers the command while its fetch is open, and pre-flight alone would
  refuse a running check as `fetch_open`.
- **Rejected** → a `check_rejected` fact through the
  [watch-events outbox](WATCH-EVENTS.md), committed before the ack.
- **Accepted** → a forced full fetch issued inline (`issue_fetch_command`,
  the path `check_watched_item` uses) and audited
  `watched_item.check_requested` with `source: bus`.

## Pre-flight: `src/core/check_now.py`

The route `POST /api/v1/watched-items/{id}/check-now` and the consumer share
one verdict, `preflight_check`, so the consumer imports nothing from `src.api`
(#378 deletes `/api/v1`). Checked in order, first match wins:

| Refusal | Route | `check_rejected.reason` |
|---|---|---|
| archived | 409 | `paused` (no token of its own; an archived item is inactive — operator decision on #371) |
| paused (`is_active` false) | 409 | `paused` |
| domain suspended | 409 | `domain_suspended` |
| no `effective_url` | 422 | `no_url` |
| a fetch command open (pending publish, in flight, processing) | 409 | `fetch_open` |
| no WatchedItem for `info_item_id`, or the id is not a ULID | n/a | `unknown_item`, `info_source_id` null |

## The outcome is a level

There is no success fact. Archiver reads the check's outcome on
`info.watch-status`: `last_full_fetch_at` passing the command's `requested_at`,
plus `health` and `health_reason`. `last_attempt_at` is not the reading,
because a scheduled 304 can move it.

That stream publishes only on transitions and its periodic tick. So **a
commanded check's completion republishes it, whatever the outcome**, a 304
included. Every terminal path takes the fetch row's `check_command_id` into
`record_check_failure` or `_record_check_success`, and `republish_status`
defers one republish when either a health transition or a command asks for one
(`src/workers/fetch_commands.py`). A commanded fetch that is **superseded**
republishes too: the newer check that closed first may be a scheduled one,
which publishes nothing of its own.

**`fetch_commands.check_command_id` is lineage**, like `forced_full_fetch`:
the reaper's re-issue and the processor's `input_unreadable` re-fetch carry it,
so whichever command in the lineage completes the check republishes.

## Operating it

```bash
# Consumer joined, and what each command came to (accepted / duplicate /
# rejected_<reason>):
sudo journalctl -u watcher --since today -o cat | grep -E 'info.watch-check|watch_check'
```

The rejections are queued in `pending_watch_events`; see
[WATCH-EVENTS.md](WATCH-EVENTS.md) → *Operating it*.
