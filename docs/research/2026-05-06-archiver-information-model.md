# Archiver: Information Model Research

_Findings from design exploration on 2026-05-06._

---

## Service Overview

**Archiver** is a new FastAPI + PostgreSQL service that becomes the source of truth for Information Items. It replaces WordPress as the authoritative record for this domain entity. WordPress is being redesigned concurrently; its role shifts to a display layer backed by a custom cache table that Replicator populates.

---

## Core Design Principle: Separate Data from Meaning

The central insight is a two-layer model:

- **Information Source + Revisions** — the physical layer. URL-keyed, content-addressed, monitors what actually exists at a location over time.
- **Information Item** — the semantic layer. References one or more Source Revisions, carries domain meaning (what this content *means* in context).

This mirrors content-addressed storage systems (Git blobs vs. commits vs. refs): the data layer is dumb and stable; the meaning layer is operator-declared and evolvable.

---

## Information Source

An `InfoSource` represents a single URL + the specification of how to target content at that URL.

**Key fields:**
- ULID identifier
- `SourceSpec` — an Information Source Specification (see below)
- `created_at`

**Design decisions:**
- One Source per URL. The Source is URL-keyed; content changes over time are captured as Revisions.
- Sources are the unit that Watcher monitors and Replicator downloads from.

---

## Information Source Specification (SourceSpec)

A **portable, service-agnostic specification** describing a URL and how to extract the content of interest from it.

**Fields:**
- URL (or reference to another Information Item — allows chaining)
- Selectors targeting the specific content of interest within the page
- Trim configuration for removing extraneous content from the extracted result

**Portability:** SourceSpec is designed to be consumed by multiple services without modification:
- **Watcher** uses it to know *what* to monitor (the selector-extracted content, not the whole page)
- **Replicator** uses it to know *what* to download and persist

**Fingerprinting unit:** The content fingerprint (see Revisions below) is computed over the *selector-extracted, trimmed content* — not the raw fetched page. This prevents spurious Revisions from nav changes, ads, or other page chrome.

**Storage:** SourceSpec lives in Archiver and is referenced from the `InfoSource` record.

---

## Information Source Revision (`SourceRevision`)

A `SourceRevision` is a captured snapshot of the content at an `InfoSource` at a point in time.

**Key fields:**
- ULID identifier
- `info_source_id` → `InfoSource`
- `content_fingerprint` (e.g., SHA-256 of extracted content)
- `captured_at`
- Reference to stored content (path in Replicator's storage)

**Fingerprint equality collapses Revisions:** If a new capture produces the same fingerprint as an existing Revision for that Source, no new Revision is created. The Revision record is unique per `(info_source_id, content_fingerprint)`.

**"Same content, different meaning":** Two different `InfoItem`s (e.g., for different committees) can reference the same `SourceRevision`. Deduplication is at the content layer; semantic distinctness is preserved by the Item layer.

**Revision pinning at Item creation:** When an operator creates an Information Item referencing a Source, they bind it to the Revision current at that moment. If the operator creates the Item after the URL has already been overwritten (e.g., a committee agenda URL reused across events), a prior Revision is still available for binding as long as it was captured before the overwrite.

---

## Information Item (`InfoItem`)

An `InfoItem` is the semantic layer — it represents what a piece of content *means* in the domain, and references one or more Source Revisions.

**Key fields:**
- ULID identifier (the stable identity across all downstream systems)
- Semantic metadata (title, type, domain associations)
- `source_revisions` — list of `SourceRevision` ULID references
- `replication_fields` — JSONB bag of namespaced, typed fields (see below)
- Replication spec assignments are managed via a separate join table (see `InfoItemReplicationSpec` below), not as a field on `InfoItem` itself.

**An Item references multiple Sources:** Keeping Items semantically narrow is the operator's responsibility. Grouping across Items remains possible through Information Sets (out of scope for this change).

**Versioning:** Content versioning is handled entirely at the Source/Revision layer. The Item itself does not version its content — it accumulates new Source Revision references as content changes. Operator-driven changes to Item metadata (adding a Source, updating title) are tracked in the Item's own audit history.

---

## InfoItem ↔ ReplicationSpec Assignments (`InfoItemReplicationSpec`)

The relationship between an `InfoItem` and a `ReplicationSpec` is a first-class record with its own lifecycle. This is modeled as a join table with **effective dating** rather than a flat list on the item.

```
InfoItemReplicationSpec
  id:                   ULID        (own identity)
  info_item_id:         → InfoItem
  replication_spec_id:  ULID        (foreign reference into Replicator's registry)
  activated_at:         datetime
  deactivated_at:       datetime | NULL   (NULL = currently active)
  resolved_path:        str | NULL        (actual storage path used under this spec)
```

**Why a join table:**
- History is implicit — deactivated rows (`deactivated_at IS NOT NULL`) are the full audit trail, queryable by time range or by spec.
- "Two active specs targeting the same provider" is handled naturally: two rows with independent ULIDs and independent `resolved_path` values. No special-casing required; the rows are distinguished by their own identity.
- Provider-agnostic: the table has no concept of backend. Provider details live in the `ReplicationSpec` record in Replicator's registry.

**Common queries:**
- Active specs for an item: `WHERE info_item_id = X AND deactivated_at IS NULL`
- Full spec history for an item: `WHERE info_item_id = X ORDER BY activated_at`
- What path did spec S store content at: `resolved_path` on the relevant row
- Which items use spec S: `WHERE replication_spec_id = S`

**`resolved_path`:** When Replicator executes a replication job, it writes the rendered storage path back to this field. This is the ground truth for locating replicated content — independent of any future template changes. If a spec is later deactivated and replaced, the old row retains the path where content was stored under the old spec, enabling future migration tooling to find and move that content.

**Note on granularity:** This design records one resolved path per spec assignment. If an item's content is updated over time (new `SourceRevision`s captured), Replicator re-executes against the same active spec and may write to the same path (overwrite) or a new one. A separate replication event log (one row per execution) is a future option if per-execution audit detail becomes necessary — not required for the initial design.

---

## `replication_fields` - The JSONB Bag of Values

Each Information Item carries a JSONB bag of namespaced, typed fields used by Replicator to render storage path and filename templates.

**Structure:** Namespaced, mirroring the existing `StorageContext` shape in `cannobserv.storage`:
```json
{
  "org": {
    "acronym": "wslcb",
    "title": "Washington State Liquor and Cannabis Board",
    "title_slug": "washington_state_liquor_and_cannabis_board",
    "acronym_or_title": "WSLCB",
    "acronym_or_title_slug": "wslcb"
  },
  "event": {
    "year": "2025",
    "date_segment": "2025_04_15",
    "date_label": "2025-04-15",
    "type_slug": "board_meeting"
  },
  "file": {
    "label": "Agenda",
    "label_slug": "agenda",
    "ext": "pdf"
  }
}
```

**Normalization responsibility:** The CLI populates both raw and slug-normalized forms. Replicator performs no normalization — it renders templates against the bag as provided. If required fields are absent or null, the replication for that spec fails (fail at replication time; CLI should validate at creation time by checking required fields against the spec's declared schema).

**Relationship to `cannobserv.storage`:** The existing `StorageContext` Vars classes and `StorageContextResolver` perform this same function today — resolving domain strings and models into typed variable objects. In the new architecture, the resolved values are persisted to Archiver (the bag) rather than re-derived on every operation. The CLI tools continue to own the domain-specific resolution logic (WP lookups, `util.normalize_string`, etc.).

---

## WordPress as Replication Target

WordPress is no longer the source of truth for Information Items. Under the redesigned WP site:

- Archiver (ULID) is the stable identity anchor.
- WordPress maintains a custom cache table with only the fields needed for display.
- This cache table is populated by Replicator as one of its replication targets.
- WordPress's role is read-only display; writes flow through Archiver.
