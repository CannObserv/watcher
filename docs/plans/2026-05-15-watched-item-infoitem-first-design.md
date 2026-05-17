# Watcher: InfoItem-first Watch model — design

**Status:** Approved 2026-05-15; reassessed 2026-05-17 against shipped Archiver v3.0.0–v3.2.0
**Tracking issue:** #160
**Supersedes:** #158 (dynamic InfoSource selector for Watch-create form). The original UX shortcut around ULID-paste is superseded by this larger reframing.
**Archiver dependencies — all shipped:**
- `CannObserv/archiver#21` (v3.0.0) — role refactor: `role IS NULL` for the InfoItem's root binding (the implicit primary); `role ∈ {cross_check, sub_aspect}` for fragment bindings. SDK `add_info_source(info_item_id, info_source_id, role=None)` with `Literal['cross_check', 'sub_aspect'] | None`. **Also reshaped `SourceRevisionCapturedEvent`** — see "Reassessment notes" below.
- `CannObserv/archiver#22` (v3.1.0) — cascade contract codified in `source_spec_schema/v1.json` description; content-kind families `{html_text: css|xpath|regex|full_page; json: jsonpath}` enforced at bind time. Cross-family attempts return `422 domain` with `code='algorithm_family_mismatch'` and `data={expected_family, actual_algorithm}`.
- `CannObserv/archiver#23` (v3.1.0) — **shipped differently than designed.** Instead of adding `name_contains` to `list_info_items`, the existing `find_info_item(query, limit)` SDK tool method is now backed by `pg_trgm` GIN indexes on `info_items.name` and `description`. See Section 5.1.
- `CannObserv/archiver#24` (v3.2.0) — **new, not in original design.** All bus event payloads on `info.changes` carry `schema_version: int = 1`; consumer mirrors must parse with `extra="ignore"`. No current Watcher consumer; documentation note for future.
- `CannObserv/archiver#20` — `list_info_sources` URL/search filter (still open at last check; not needed by this design).

## Goal

Resolve the impedance mismatch between Archiver's InfoItem-centric data model and Watcher's flat per-InfoSource Watch model. Operators think InfoItem-first ("monitor the Oregon OLCC license registry"); the current UX forces them to think in raw InfoSource ULIDs and provides no abstraction for shared defaults across the Watches that make up a single conceptual subscription.

This design introduces `WatchedItem` as a 1:1 mirror of an Archiver InfoItem subscription, reshapes `Watch` to point at "a content target within an InfoItem" rather than at an InfoSource directly, and codifies the "InfoItem = fetch group" invariant cooperatively across Archiver and Watcher.

## Approved approach (summary)

| Layer | Change |
|---|---|
| Archiver | Refactor `info_item_sources.role` so primary is implicit (the URL-bearing root); role applies only to fragment bindings with values `cross_check` (selector cross-check feeding selector-rot signal) or `sub_aspect` (watchable distinct content area). |
| Archiver | Document the cascade contract: every InfoSource's extraction runs against the root's fetched bytes (no chaining). Enforce content-kind compatibility across an InfoItem's bindings at the bind-source tool layer. |
| Watcher | Replace `watches.info_source_id` with `(info_item_id, target_info_source_id)` — primary content when target is NULL, specific sub_aspect fragment when set. |
| Watcher | Add `WatchedItem` table (1:1 with InfoItem) holding shared defaults (schedule, content_type, tags, notification templates). |
| Watcher | Schedule lives at WatchedItem level; one fetch per InfoItem per cycle; all child Watches observe the same fetched bytes. |
| Watcher | Live inheritance: `Watch.field → WatchedItem.default_field → SYSTEM_DEFAULTS[field]`. Tags merge additively. Notification templates live-propagate via union (no suppression in v1). |
| Watcher UX | Two-step picker: InfoItem typeahead → content-target picker showing the binding tree. Cross_checks shown but not selectable. WatchedItem auto-created on first Watch. |
| Watcher UX | Fragment review: diff-on-view. WatchedItem detail page surfaces new sub_aspect bindings since `last_reviewed_at`. |

## Sections

### Section 1 — Archiver role refactor (Archiver issue)

In current schema, `info_item_sources.role` is a free-text column with one enforced constraint (one active `role='primary'` per InfoItem). This permits drift: the operator could bind a fragment-shape as primary or a root-shape as secondary, and the schema would not catch it.

**The refactor:** `primary` is no longer an explicit role value. The InfoItem's primary is *defined as* the unique active URL-bearing (root-shaped) InfoSource bound to it. The `role` column becomes nullable and applies only to fragment-shaped bindings, taking values:

- `cross_check` — the fragment selector extracts the **same content** as primary, via a different selector. Used at fetch time to detect selector rot when extractions diverge. Never directly watched by an operator.
- `sub_aspect` — the fragment selector extracts a **different content area** of the same fetched page. Operator-watchable.

**Constraints (enforced app-layer in the bind-source tool, defended by integration tests):**

- `role IS NULL` ↔ underlying InfoSource is root-shaped (URL non-null)
- `role IN ('cross_check', 'sub_aspect')` ↔ underlying InfoSource is fragment-shaped (parent_info_source_id non-null)
- Exactly one active root binding per InfoItem (replaces the unique-active-primary constraint)
- Cross_check and sub_aspect bindings' underlying InfoSources must have `parent_info_source_id` reaching the primary's effective root (i.e., they share the primary's fetch URL)

**Pre-production migration:** drop any existing rows with non-conforming roles, no compatibility shims.

**Watcher implication:** Watcher implementation depends on this refactor landing first. Without enumerated `role`, Watcher cannot reliably distinguish cross_check (infrastructure) from sub_aspect (watchable) at the selector picker.

### Section 1.5 — Cascade contract + content-kind compatibility (Archiver issue)

Current implementation runs every InfoSource's extraction against the cached page bytes from the root fetch (per Phase 4 design). The schema does not declare this; it only forbids fragments from carrying their own `target` block.

**Codify the contract (L1):**

- Document in `source_spec_schema/v1.json` description and Archiver AGENTS.md that all InfoSources extract from the root's fetched bytes — no chaining off primary's extraction output.
- Define "InfoItem = fetch group": one URL fetched, one content kind produced, N selectors evaluated against those bytes.

**Enforce content-kind compatibility (L2):**

- At bind-source time, validate that the new InfoSource's `extraction.algorithm` is in the same content-kind family as the InfoItem's primary algorithm.
- Compatibility families: `{css, xpath, regex, full_page}` operate on HTML/text bytes; `{jsonpath}` operates on JSON bytes.
- Mixed families across one InfoItem's bindings → reject at bind-source time with a typed error.

**Not in v1:** explicit `content_kind` column on `info_sources` (derivable from algorithm; YAGNI today).

### Section 2 — Watcher Watch reshape + WatchedItem (Watcher work)

**`Watch` schema delta:**

- Drop `watches.info_source_id`
- Drop the fragment-root invariant (`require_root_watch_on_chain` — see [src/dashboard/routes.py:270](../../src/dashboard/routes.py#L270))
- Add `watches.info_item_id` (FK → `information.info_items`, NOT NULL)
- Add `watches.target_info_source_id` (FK → `information.info_sources`, NULL allowed). NULL = Watch covers the InfoItem's primary content (with cross_checks producing a rot signal at fetch). Non-NULL = Watch targets that specific sub_aspect fragment.
- Add `watches.watched_item_id` (FK → `watched_items.id`, NOT NULL — see open question below)
- Drop `schedule_config` from Watch (moves to WatchedItem; see 2.3)
- Keep `name`, `content_type` (override), `tags` (override, additive at resolve), `is_active`, `is_archived`, `description`, `health_status`, `effective_url`, `effective_domain`, `last_checked_at`, `last_changed_at`

**App-layer validation:**

- `target_info_source_id IS NOT NULL` requires the InfoSource be bound to `info_item_id` with `role='sub_aspect'` and active. Watch targeting a cross_check binding is rejected.

**`WatchedItem` (new table):**

```python
class WatchedItem(Base, TimestampMixin):
    id: ULID PK
    info_item_id: ULID FK NOT NULL UNIQUE  # 1:1 with Archiver InfoItem
    name: str  # defaults to InfoItem.name on creation; renamable
    description: str | None
    is_active: bool default True
    archived_at: datetime | None
    last_reviewed_at: datetime | None  # for sub_aspect diff-on-view (Section 5.3)

    default_schedule_config: dict | None  # JSONB
    default_content_type: ContentType | None
    default_tags: list[str] | None
```

**Scheduling becomes InfoItem-level:**

One fetch per InfoItem per cycle, driven by the WatchedItem's effective `default_schedule_config`. All Watches against that InfoItem observe the same fetched bytes. Each Watch evaluates its target's extraction and decides whether *its* fingerprint changed and whether to notify.

Consequences:
- `schedule_config` is not overridable per-Watch
- Sub_aspect Watches do not trigger additional fetches; they piggyback on the WatchedItem's cycle
- The Watcher scheduler is per-WatchedItem, not per-Watch (existing per-Watch scheduling logic is replaced)

**SourceRevision posting (unchanged semantics):**

Each InfoSource's extraction produces its own SourceRevision in Archiver (primary, cross_checks, sub_aspects). The change-bus events are unchanged. Cross_check SourceRevisions enable selector-rot detection (#157) via divergence from primary.

**`watched_item_id` requirement (closed question):** every Watch belongs to a WatchedItem; auto-created on first Watch under an InfoItem. No ad-hoc-Watch path. Simpler resolution; single code path.

### Section 4 — Inheritance & resolution (Watcher work)

Two-step resolution chain (Collection layer descoped to a follow-on workstream).

**Scalar fields (`schedule_config`, `content_type`):**

```python
def resolve(watch, field):
    if getattr(watch, field) is not None:
        return getattr(watch, field)
    if watch.watched_item and getattr(watch.watched_item, f"default_{field}") is not None:
        return getattr(watch.watched_item, f"default_{field}")
    return SYSTEM_DEFAULTS[field]
```

Live: edits to `WatchedItem.default_*` retroactively affect all non-overriding child Watches.

**Tags — additive merge:**

```python
def resolved_tags(watch):
    return sorted(set(watch.watched_item.default_tags or []) | set(watch.tags or []))
```

No exclusion semantics in v1.

**Notification configs — live union (Approach B):**

- `WatchedItem` has 0+ notification templates (new table `watched_item_notification_templates`)
- `Watch` has 0+ per-Watch notification configs (existing `watch_notification_configs`)
- At dispatch time: union of (WatchedItem templates ∪ Watch configs). De-duplication by identity only.
- Editing a template propagates immediately to all child Watches.
- Per-Watch suppression of WatchedItem templates is deferred to a follow-on (YAGNI v1).

**Implementation:** `src/core/watches/resolution.py` exposes `resolved_schedule_config`, `resolved_content_type`, `resolved_tags`, `resolved_notification_configs`. Called by the scheduler, fetcher, notification dispatcher, and dashboard.

### Section 5 — Selector UX + fragment review (Watcher work)

**Two-step picker on `/watches/new`:**

1. **InfoItem typeahead** — search via `archiver.find_info_item(query, limit)`. The SDK method searches `name + description` case-insensitively (backed by `pg_trgm` GIN indexes per Archiver v3.1.0, #23); returns up to `limit` matches, newest first. Defaults to `limit=20`; Watcher's picker should pass a small bounded limit (suggest 10–20) and re-query as the operator types. **No paginate-and-filter-client-side fallback is needed** — `find_info_item` is the supported typeahead primitive.
2. **Content-target picker** — server-renders the InfoItem's binding tree:

   ```
   InfoItem: <name>
   └── primary           https://...
       ├── (cross_check) — <selector>   [infrastructure, not selectable]
       ├── sub_aspect    — <selector>   [selectable]
       └── sub_aspect    — <selector>   [selectable]
   ```

   Operator picks the primary (Watch targets InfoItem-level content) or one sub_aspect (Watch targets that fragment).

3. **On first Watch under an InfoItem** — Watcher auto-creates the WatchedItem record (name defaults to InfoItem.name; operator can rename). On subsequent Watches, attach to the existing WatchedItem.

**Routes (Watcher dashboard):**

- `GET /watches/new` — step 1, InfoItem typeahead form
- `GET /watches/new/info-items?q=…` — typeahead results partial (HTMX); proxies `archiver.find_info_item`
- `GET /watches/new/binding-tree?info_item_id=…` — step 2, binding-tree partial; sources from `archiver.get_info_item(info_item_id)` which returns `InfoItemOut` including `info_item_sources: list[InfoItemSourceOut]` with `role: str | None`. Render `role IS NULL` rows as "primary" (always exactly one)
- `POST /watches/new` — submit (validates target against role; auto-creates WatchedItem if first)
- `GET /watched-items` — list page
- `GET /watched-items/{id}` — detail page (lists child Watches; surfaces sub_aspect review)
- `GET /watched-items/{id}/edit` — defaults editor
- `POST /watched-items/{id}/mark-reviewed` — stamps `last_reviewed_at = now()`

**Fragment review (sub_aspect diff-on-view):**

- WatchedItem detail page lists current `sub_aspect` bindings under the InfoItem
- For each, mark as "new" if `info_item_sources.created_at > watched_item.last_reviewed_at`
- "Add Watches for these" action creates Watches in bulk and stamps `last_reviewed_at`
- No background polling; the operator-load-page cadence drives discovery

**Watch create/edit form:** inheritance UX per Section 4.5 — show resolved value + override toggle + ghost-text WatchedItem default.

## Cross-cutting decisions and their rationale

| Decision | Why |
|---|---|
| Primary implicit from URL-bearing shape | Removes drift potential; one source of truth (the shape) for what "primary" means; replicator semantics unchanged ("replicate the URL-bearing InfoSource"). |
| Schedule at WatchedItem, not Watch | One fetch per InfoItem per cycle; aligns with Archiver's page-once cascade; no fetch-coalescing logic needed. |
| Live inheritance | Operator's "Shared defaults, frequent overrides" mental model; bulk-edit becomes editing the WatchedItem rather than touching every Watch. |
| Cross_check not operator-watchable | They're infrastructure for rot detection (#157); presenting them as watch targets dilutes the model. |
| Diff-on-view fragment review | Operator-driven cadence; no event subscription infrastructure; degrades gracefully if Archiver and Watcher are out of sync. |
| Collection descoped | Not blocking primary work; revisit when cross-InfoItem grouping needs surface in real usage. |

## Reassessment notes (2026-05-17)

Archiver shipped #21–24 ahead of Watcher implementation. The shipped surface matches the design with three deltas worth recording before implementers start:

1. **`#23` shipped as `find_info_item` indexing, not `list_info_items` filter.** Section 5.1 + routes updated above. Net effect: simpler than designed — the picker uses the existing tool-search SDK method (`find_info_item(query, limit)`), and the "paginate-and-filter-in-Python fallback" path is unnecessary. No design concept changes.

2. **`SourceRevisionCapturedEvent` payload reshaped** (rolled into #21, beyond original scope). `info_item_ids: list[str]` → `bindings: list[InfoItemBinding]` where each `{info_item_id: str, role: str | None}`. This is on the `info.changes` Redis Stream which Watcher does **not** consume today — Section 5.3's fragment review is "diff on view" via `get_info_item`, not event-driven. No Watcher v1 work. **Carry forward if/when Watcher gains an event consumer:** the per-binding `role` field is exactly the discriminator a future proactive sub_aspect-notification feature would want (filter on `bindings[*].role == 'sub_aspect'`).

3. **`#24` event `schema_version`** is producer-set on all `info.changes` payloads and consumers are required to parse with `extra="ignore"` per the new Archiver AGENTS convention. Same applicability: Watcher has no consumer today. **Carry forward:** any future Watcher event consumer must mirror this — pin a Pydantic model with `model_config = ConfigDict(extra="ignore")` and switch on `schema_version` only when the bump-on-incompatible-reshape convention forces it.

4. **`#22` content-kind family enforcement** is at bind-time in Archiver only. Watcher does not create `info_item_sources` rows — Archiver does. The `algorithm_family_mismatch` error is therefore not surfaced through any Watcher code path in v1. **Optional future surface:** Watcher's WatchedItem detail page could display the InfoItem's content-kind family (derived from primary's algorithm) as a small label — useful operator context but not blocking.

5. **Implicit primary in picker rendering.** SDK returns `InfoItemSourceOut.role: str | None`. Watcher renders `role IS NULL` rows with a "primary" badge; `role == 'cross_check'` as muted/infrastructure; `role == 'sub_aspect'` as selectable. No design change — this is a coding-detail note.

6. **No Watcher code changes triggered by the Archiver upgrade itself.** Watcher's existing SDK usage (`get_info_source`, `create_source_revision`, `defaults.*`) does not touch `role` or the reshaped event payload. The Phase 5 `require_root_watch_on_chain` invariant still exists at [src/core/watches/invariants.py:63](../../src/core/watches/invariants.py#L63) and gets dropped during Section 2 implementation, as designed.

## Out of scope

- **Cross-InfoItem `Collection` grouping.** Designed and descoped; pick up as a follow-on workstream.
- **Selector-rot signal pipeline.** This design produces the SourceRevision divergence primitive (cross_check vs primary). The signal aggregation, surfacing, and health-status integration is #157's scope.
- **Replicator changes.** Primary remains the URL-bearing root; RepSpec assignments unchanged. Replicating sub_aspect content as independent artifacts (instead of within the parent fetch) is not a v1 use case.
- **Data migration.** Pre-production; drop-and-recreate Watcher's tables and re-bind the small existing dataset.
- **`list_info_items` `name_contains` server filter (Watcher consumer).** Filed as a separate Archiver issue; Watcher works without it (paginate + filter in Python) but performance degrades with catalog size.
- **Suppression of WatchedItem notification templates per-Watch.** Approach B union model for v1; suppression is a follow-on.

## Implementation order (rough)

1. **Archiver:** ✅ role refactor (#21, v3.0.0).
2. **Archiver:** ✅ cascade contract + content-kind compat (#22, v3.1.0).
3. **Archiver:** ✅ `find_info_item` pg_trgm backing (#23, v3.1.0).
4. **Archiver:** ✅ event `schema_version` (#24, v3.2.0).
5. **Watcher:** new tables (`watched_items`, `watched_item_notification_templates`); Watch column reshape (drop `info_source_id`/`schedule_config`, add `info_item_id`/`target_info_source_id`/`watched_item_id`); drop fragment-root invariant.
6. **Watcher:** scheduler reshape — InfoItem-level fetching; resolution module.
7. **Watcher:** dashboard routes + picker UX (uses `find_info_item` + `get_info_item`).
8. **Watcher:** fragment-review UI (diff-on-view via `get_info_item` + `last_reviewed_at`).
