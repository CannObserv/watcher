# Conditional GET: storing and replaying validators (#269)

Landed only because replicator#17 and #249 part 1 are both **in production**: a
validator sent to a deployment that still classifies 304 as a plain fetch failure
marks a healthy item ERROR and notifies a user about it on every no-change check.

**Storage takes two hops**, because the fact and the apply are separated in time:

1. The `content.blobs` consumer upserts `etag` / `last_modified` onto the
   `fetch_commands` row — provenance for that occasion.
2. `apply_fetch_blob` copies the pair onto the **WatchedItem**, *after* its
   supersession guard. That guard is what makes "from the fact that closed the
   item's latest command" true, and is exactly MUST-5's protection: a validator
   pinned to a fingerprint replays a stale `If-None-Match` for precisely as long
   as the content is unchanged — the period conditional GET was supposed to help.

The item-level write is **always an overwrite, `None` included**: the pair must
describe the latest 200, so an origin that stops offering a validator must not
leave the old one replayable. A 304 apply touches neither the pair nor
`last_full_fetch_at` — no bytes arrived, and the stored pair is current by
definition.

**Replay is snapshotted at issue, not read at publish.** `create_fetch_command`
resolves the pair and stores it as `request_etag` / `request_last_modified`;
`publish_fetch_command` builds `headers` from the row. The pending-publish sweep
holds only the row (the same argument that put `info_source_id` there,
cannobserv#300), so without the snapshot a republish could carry different
headers than the command it is replaying — and a 304 with no record of which
validator earned it is undiagnosable. Values go out **verbatim and unparsed**:
`W/` prefix, quotes, and the origin's own date spelling.

**Six rules decide whether an occasion may replay** — `src/core/validators.py`,
one pure predicate. Listed by subject; the predicate short-circuits, and the
order it happens to evaluate them in is not a contract:

| Rule | Why |
|---|---|
| The gate (`WATCHER_CONDITIONAL_GET_ENABLED`) is off for this item | Off by default; a ULID list is the canary position, `true` the fleet |
| The caller forced a full fetch | The check-now button is always a real re-read |
| Nothing stored, or nothing **sendable** | The send-side guard mirrors Replicator's refusal list (printable US-ASCII, ≤1024, non-blank). A command it would refuse costs an ERROR health transition before any request goes out |
| `validator_source_key` disagrees with the item's current key | The URL moved, `source_specs` were re-announced, or the extraction generation changed. One key rather than a clear scattered across every writer of those fields |
| No `last_full_fetch_at` | Unknown provenance is not replayable |
| The pair is older than `WATCHER_VALIDATOR_MAX_AGE_HOURS` (default 168) | The residual net below |

**The fingerprint-continuity question, answered.** A 304 produces no bytes, so
nothing is extracted and no fingerprint is recomputed — the item's fingerprint is
inherited from the last 200. That is the point of the optimisation, but it means
an *extraction* drift would go unnoticed for as long as the origin keeps
answering 304. Rules 4 and 5 make the common causes deterministic: a spec edit, a
URL move, or an extractor bump moves `validator_source_key` and forces a full
fetch wherever that change is written. The age ceiling is what remains for the
cases they cannot see — an origin whose ETag tracks a template rather than the
watched region, or a wrong-but-stable validator. Four items at ~122 KB make a
weekly forced fetch free, which is why the default is set for confidence rather
than for bytes.

**The one loop hazard.** `invalid_request_options` is terminal *and* pre-request:
Replicator refuses the command's headers before contacting the origin. An
unsendable stored validator would therefore be re-snapshotted and refused every
cycle, forever, each one an ERROR health transition and a `WATCH_ERROR`. So
`apply_fetch_failure` clears the item's validators on that reason alone — the
next command is unconditional, and the item self-heals. Every other reason says
nothing about our request options and leaves the pair alone.

**The extraction generation is derived, not declared.** `EXTRACTION_GENERATION`
is `"{installed co-core version}+{LOCAL_EXTRACTION_GENERATION}"`. co-core owns
extraction and arrives through the wheelhouse with no human in the loop, so a
hand-bumped constant reproduced the `WATCHER_USER_AGENT` hazard one step
quieter: an extractor change nobody bumped for would leave every 304-ing item
inheriting a fingerprint the *old* extractor computed, invisible until the
origin's bytes happened to change. Reading the version makes an upgrade
invalidate every stored validator by itself — one full fetch per item. Bump
`LOCAL_EXTRACTION_GENERATION` by hand only for a watcher-side extraction change
(how chunks are joined, media-type dispatch, spec fallback order), which
co-core's version cannot see.

**A forced full fetch is lineage.** `fetch_commands.forced_full_fetch` records
that an occasion was asked for as an unconditional re-read, and `_reissue`
carries it onto the replacement alongside `intent_id`. Without it, a check-now
that stalled past the reaper's timeout came back as a conditional GET the origin
could answer 304 — the operator's forced re-read producing no bytes, with nothing
saying it had been downgraded.

**An extraction failure also clears the pair.** Bytes arrived and could not be
extracted (#258/#260), so the item is in ERROR with no new fingerprint — and a
304 apply records a *successful* check. Keeping the validators would let the next
cycle answer 304 and flip a broken item back to OK health without anything having
been extracted. Forgetting them makes the next command a full fetch that
re-asserts the failure until the spec is fixed.

**Freshness now reads as a triple**: `last_checked_at` (we tried),
`last_observed_at` (the content was confirmed current — a 304 counts), and
`last_full_fetch_at` (bytes actually arrived — stamped by every blob apply,
including one whose extraction then failed, because it records the fetch and not
its outcome). The gap between the last two is
how long a fingerprint has been inherited rather than recomputed; the WatchedItem
detail page renders it as *Last Full Fetch*.
