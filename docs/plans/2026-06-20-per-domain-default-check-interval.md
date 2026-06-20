---
title: Per-domain default check interval (3-tier schedule resolution)
date: 2026-06-20
status: draft
---

# Per-domain default check interval (3-tier schedule resolution)

GitHub: #205. Builds on #204 (already on `main`).

## Problem

Schedule resolution is 2-tier — `WatchedItem.default_schedule_config` → system
default `{"interval": "1d"}` ([resolution.py:13](../../src/core/watches/resolution.py#L13)).
There is no domain-level cadence layer, so every WatchedItem without its own
interval falls straight to the global `1d`. Operators want one cadence per
domain (slow regulator at `7d`, active rulemaking feed at `6h`) that its items
inherit. This is the operator's desired *check cadence* (a `schedule_config`
interval string), conceptually identical to `WatchedItem.default_schedule_config`
— **not** `Domain.min_interval`, which is a per-request rate-limiter floor in
seconds. The two stay separate.

## Approach

Add a domain cadence tier by **denormalizing** it onto `WatchedItem`, mirroring
the existing `domain_suspended` pattern — not by eager-loading the `Domain` row
at resolve time. New nullable column `Domain.default_schedule_config` holds the
operator value; new nullable column `WatchedItem.domain_default_schedule_config`
is the denormalized copy, maintained on every create/PATCH path via the shared
[`ensure_domain_and_resolve_suspension`](../../src/core/domains.py#L30) helper
(extended to return `(suspended, domain_default)`) and back-filled across a
domain's items when the operator edits the domain default. `resolved_schedule_config`
stays **single-arg** and becomes 3-tier: item config → item's denormalized domain
default → system default. Because the resolver signature is unchanged, all three
call sites — `schedule_tick`, `_interval_display`, `_build_next_check_map` — pick
up the domain tier automatically (the #204 "flows through" property). The
display marker widens from a bool to a source label so an inherited value reads
`· domain` vs `· default`.

Why denormalize over eager-load: the codebase already denormalizes domain facts
onto `WatchedItem` (`domain_name`, `domain_suspended`) specifically so the
scheduler hot path carries **no live Domain join** ([tasks.py:241](../../src/workers/tasks.py#L241)).
Eager-loading the domain default into `schedule_tick` would reintroduce exactly
the read that pattern exists to avoid, and a split model (suspension denormalized,
cadence joined) is worse than either pure approach. The cost — JSONB duplicated
across a domain's rows, plus a bounded back-fill UPDATE on a rare operator edit
— is precisely the cost already paid for `domain_suspended` (back-fill precedent:
[`domain_toggle_active`, routes.py:1333](../../src/dashboard/routes.py#L1333)).

## Tradeoffs / alternatives

- **Eager-load the `Domain` (or its default) at resolve time (issue Option 1)** —
  rejected: reintroduces a Domain read into the scheduler that `domain_suspended`
  was built to avoid; changes the resolver signature and all three call sites;
  produces an inconsistent two-model design. Cheap to batch, but cuts against the
  established grain.
- **Resolve the domain default live only in the two dashboard sites, keep the
  scheduler on a separate path** — rejected: splits the scheduling cadence across
  two code paths that would drift; the scheduler is the authoritative consumer and
  must see the same tier the UI advertises.
- **Reuse `Domain.min_interval` / `current_interval`** — rejected by the issue and
  confirmed in code: those are float-seconds rate-limiter/backoff state, not a
  cadence string. Overloading them conflates politeness with scheduling.

## Steps

1. **Migration + models.** Add `Domain.default_schedule_config`
   (`JSONB(none_as_null=True)`, nullable, per #198) and
   `WatchedItem.domain_default_schedule_config` (same). One Alembic revision;
   `__init__` defaults `None`. No trigger. Red→green a model test asserting both
   columns persist `None` as SQL `NULL`.
2. **Resolver → 3-tier.** Extend `resolved_schedule_config(watched_item)` (still
   single-arg): item config (non-`None`) wins; else item's
   `domain_default_schedule_config` (non-`None`); else `SYSTEM_DEFAULT_SCHEDULE_CONFIG`.
   Preserve the `None`-vs-`{}` discipline at each tier. Tests:
   item-wins, domain-wins, system-fallback, `{}`-at-each-tier.
3. **Denormalization helper.** Change `ensure_domain_and_resolve_suspension` to
   return `(suspended: bool, domain_default: dict | None)` (read
   `Domain.default_schedule_config`); update its five callers (API create ×2, API
   PATCH effective_url, dashboard create, dashboard effective-url) to also set
   `wi.domain_default_schedule_config`. Tests per call path.
4. **Domain-default edit + back-fill (API).** Accept `default_schedule_config` in
   `PATCH /domains/{name}` (`upsert_domain`), validate it as a schedule_config
   (reuse the `WatchedItem.default_schedule_config` validator), and back-fill
   `UPDATE watched_items SET domain_default_schedule_config = :val WHERE
   domain_name = :name` — the `domain_toggle_active` analog. Audit it. Tests:
   set/clear/invalid + back-fill reaches existing items.
5. **Domain-default edit (dashboard).** Editable field on the `/domains/{name}`
   detail page posting to a dashboard route that reuses the step-4 logic
   (validation + back-fill + audit), HTMX + non-HTMX fallback. Test the route and
   back-fill.
6. **`reduce_frequency` correctness.** `reduce_frequency` is a **live, API-reachable,
   tested** post-action (`POST /api/v1/watched-items/{id}/profiles`,
   `post_action="reduce_frequency"`) — kept, not removed. Today it hard-writes
   `{"interval": "1d"}` ([tasks.py:292](../../src/workers/tasks.py#L292)), which
   under a domain tier can *speed up* an item on a slower-than-1d domain. Fix:
   throttle to 1d **only when the currently-effective cadence is faster than 1d**;
   otherwise no-op and leave the item config untouched (preserve domain
   inheritance):
   ```python
   current = parse_interval(resolved_schedule_config(wi).get("interval"))
   if current < ONE_DAY:
       wi.default_schedule_config = {**(wi.default_schedule_config or {}), "interval": "1d"}
       audit(... WATCHED_ITEM_THROTTLED, new_interval="1d")
   # else ≥1d → no-op, preserve inheritance
   ```
   Tests: (a) `7d`-domain inheriting item + reduce_frequency profile stays at 7d,
   item config still `None`, no `WATCHED_ITEM_THROTTLED` audit; (b) `6h` item
   slows to 1d as before (regression guard on existing `TestPostActions`).
7. **Display marker → source label.** Widen `_interval_display`'s return from
   `(value, inherited: bool)` to carry the source (`item` / `domain` / `default`);
   thread through `_build_interval_map`, the list template, and the detail
   template so an inherited value renders `· domain` vs `· default`. Update the
   #204 marker tests. No change to `_build_next_check_map` logic (it already
   routes through the resolver).
8. **Docs.** Update `AGENTS.md` (schedule-resolution is now 3-tier; note the new
   columns and the back-fill writer) and close the loop on #205.

## Open questions / risks

All resolved (2026-06-20):

- **`reduce_frequency` — resolved: keep + fix (step 6).** Investigated: it is not
  dead code. `post_action="reduce_frequency"` is reachable via the live, tested
  API create/patch routes; only a dashboard affordance is missing. Keeping it and
  fixing the hardcoded-1d speed-up (see step 6).
- **Domain `{}` value — resolved: reject at write.** Steps 4–5 reject an
  empty-but-non-null domain default at the write boundary; the resolver still
  treats any stray `{}` as "no interval" consistent with the item tier.
- **Back-fill scale — resolved: accepted.** A domain-default edit updates every
  WatchedItem on that domain in one UPDATE, same property as `domain_suspended`.
  No batching.
- **Source-label marker — resolved: implement (step 7).** Widen the display marker
  to `item` / `domain` / `default`; keep #204's existing marker tests green and
  add the `· domain` case.
