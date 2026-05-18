# InfoItem Picker + Standalone WatchedItem Create Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ULID-paste Watch-create form with a reusable two-step InfoItem typeahead picker; reuse the picker on a new standalone WatchedItem-create flow; replace the WatchedItem detail-page InfoItem summary card with the picker's binding-tree partial in readonly mode.

**Architecture:** Build the picker as one partial pair (`typeahead.html` + `results.html` for Step 1, `binding_tree.html` for Step 2). A `mode` template parameter (`select_with_target` / `select_only` / `readonly_tree`) controls which rows are selectable. Backend routes generalize from `/watches/new/info-items` → `/info-items/search` and `/info-items/{id}/binding-tree` so both create flows and the detail page share the same endpoints. Standalone WatchedItem creation is a new additive surface; auto-create-on-first-Watch stays.

**Tech Stack:** FastAPI + Jinja2 + HTMX + Tailwind v4 (component classes from `input.css`). ArchiverClient SDK (`find_info_item`, `get_info_item`, `get_info_source`). pytest + httpx.AsyncClient against the integration DB.

**Issue:** [#162](https://github.com/CannObserv/watcher/issues/162) — expanded per 2026-05-18 to include WatchedItem create reuse + detail-page partial swap.

**Supersedes / amends:** [docs/plans/2026-05-17-watched-item-crud-ui-plan.md:3312](../../docs/plans/2026-05-17-watched-item-crud-ui-plan.md#L3312) "no standalone create" — now in scope. The auto-create-on-first-Watch path remains.

**Depends on:** Archiver SDK ≥ v3.1.0 (`find_info_item` pg_trgm-backed typeahead; CannObserv/archiver#23). Already on path — confirmed at [/home/exedev/archiver/clients/python/src/archiver_client/client.py:483](../../../archiver/clients/python/src/archiver_client/client.py#L483).

---

## Scope Check

This plan covers three deltas to the same surface (picker partial, Watch-create form, WatchedItem-create flow + detail-page partial). The deltas share the picker partial and the search/binding-tree routes, so splitting would force duplicate work and a coordinated re-merge. Keep as one plan.

---

## File Structure

### New files

| Path | Purpose |
|---|---|
| `src/dashboard/templates/partials/info_item_picker/typeahead.html` | Step 1: text input + HTMX live results target. Parameterized by `target_form_id`, `hidden_field_name`, `mode`, `current_id`. |
| `src/dashboard/templates/partials/info_item_picker/results.html` | Server-rendered result list (HTMX swap target). One `<li role="option">` per match; "no results" fallback. |
| `src/dashboard/templates/partials/info_item_picker/binding_tree.html` | Step 2: bindings partitioned by role. Modes control which rows are selectable. |
| `src/dashboard/templates/pages/watched_item_form.html` | New standalone "Create WatchedItem" page. |
| `src/dashboard/static/js/info-item-picker.js` | Keyboard nav (↑/↓/Enter/Esc), `aria-activedescendant` updates. ~80 LOC. |
| `tests/dashboard/test_info_item_picker_routes.py` | Search + binding-tree routes in each mode. |
| `tests/dashboard/test_watched_item_create.py` | Standalone WatchedItem-create flow. |

### Modified files

| Path | Change |
|---|---|
| `src/dashboard/templates/partials/target_picker.html` | **Delete.** Replaced by picker include. |
| `src/dashboard/templates/partials/watched_item_info_item_card.html` | **Delete.** Replaced by `binding_tree.html` in `readonly_tree` mode. |
| `src/dashboard/templates/pages/watch_form.html` | Include picker partial pair. Add a `<details>` "Paste ULID" power-user fallback. |
| `src/dashboard/templates/pages/watched_item_detail.html` | Swap `watched_item_info_item_card.html` include → render `binding_tree.html` in readonly mode. |
| `src/dashboard/templates/pages/watched_items.html` | Add "New WatchedItem" button; replace empty-state CTA. |
| `src/dashboard/routes.py` | Add `info_items_search`, `info_item_binding_tree`, `watched_item_create_form`, `watched_item_create_submit` routes. Update detail-page route to pass partitioned bindings + `mode="readonly_tree"`. |
| `src/api/routes/watched_items.py` | Add `POST ""` (create); handle duplicate-409 on `info_item_id` uniqueness. |
| `src/api/schemas/watched_item.py` | Add `WatchedItemCreate` schema. |
| `src/core/watches/__init__.py` | Emit `WATCHED_ITEM_CREATED` audit event from `_get_or_create_watched_item` only when a new row is inserted (auto-create path); standalone path emits explicitly. Add `source` field for both. |
| `src/core/models/audit_log.py` | Add `WATCHED_ITEM_CREATED = "watched_item.created"`. |
| `tests/conftest.py` | Add `find_info_item` to the `info_client` fake (returns `InfoItem`s by name ILIKE). |
| `tests/dashboard/test_routes.py` | Update Watch-create tests: assert picker markup, drop dead `test_create_form_has_target_picker` assertion against deleted `target_picker.html`. |
| `tests/dashboard/test_watched_item_routes.py` | Update detail-page tests to assert `binding_tree.html` markup (no `watched_item_info_item_card.html` strings). Add list-page assertion for "New WatchedItem" button. |
| `docs/plans/2026-05-17-watched-item-crud-ui-plan.md` | Update line 3312 note: "Standalone `POST /api/v1/watched-items` create endpoint — **superseded by 2026-05-18 plan, in scope**". |

### Not modified (intentionally)

- `src/core/models/watched_item.py` — model is unchanged; `info_item_id` stays immutable identity.
- `src/api/routes/watches.py` — Watch-create POST shape unchanged (`info_item_id`, optional `target_info_source_id`).
- `src/core/watches/info_item_fetch.py` — bindings helper used as-is by the new binding-tree route.

---

## Conventions

- TDD, Red → Green → Refactor. No production code without a failing test first.
- Lint after each task with `uv run ruff check .`; format only if it complains.
- `Decimal`/UTC everywhere; ISO 8601 strings on the wire.
- HTMX detection in mutation routes: `request.headers.get("HX-Request") == "true"` only. Boosted-nav fallback to full-page redirect.
- A11y per `docs/STYLE.md`: `role="combobox"`/`role="listbox"`/`role="option"`, `aria-activedescendant`, `aria-expanded`, `focus-visible` rings, 44px hit targets, `aria-live="polite"` on the results region.
- Commit messages: `#162 <type>: <short>` per `AGENTS.md`. **One commit per task.**

---

## SLICE 1 — Audit event + create-watched-item core

### Task 1: Add `WATCHED_ITEM_CREATED` audit event type

**Files:**
- Modify: `src/core/models/audit_log.py:48` (insert after `WATCHED_ITEM_THROTTLED`)
- Test: `tests/api/test_audit_log.py` (no edit; existing tests stay green)

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_audit_log.py — append
from src.core.models.audit_log import EventType


def test_watched_item_created_event_exists():
    assert EventType.WATCHED_ITEM_CREATED == "watched_item.created"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_audit_log.py::test_watched_item_created_event_exists -v`
Expected: FAIL with `AttributeError: type object 'EventType' has no attribute 'WATCHED_ITEM_CREATED'`.

- [ ] **Step 3: Add the constant**

Edit `src/core/models/audit_log.py`, after `WATCHED_ITEM_THROTTLED`:

```python
    WATCHED_ITEM_CREATED = "watched_item.created"
```

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/api/test_audit_log.py::test_watched_item_created_event_exists -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/models/audit_log.py tests/api/test_audit_log.py
git commit -m "#162 feat: add WATCHED_ITEM_CREATED audit event"
```

---

### Task 2: Stamp `WATCHED_ITEM_CREATED` from `_get_or_create_watched_item`

**Files:**
- Modify: `src/core/watches/__init__.py:37-59` (the `_get_or_create_watched_item` helper)
- Test: `tests/test_create_watch_service.py` or wherever the helper has unit coverage

**Why now:** the auto-create path silently inserts a WatchedItem with no audit trail today. Once we add a standalone create endpoint, operators will expect a uniform audit record either way.

- [ ] **Step 1: Find existing coverage**

```bash
grep -rn "_get_or_create_watched_item\|create_watch.*WatchedItem" tests/ | head -10
```

Expected: at least one test asserting WatchedItem is created when absent. If not present, add the failing test below to `tests/test_create_watch_service.py`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_create_watch_service.py — new or appended class
import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType


@pytest.mark.integration
class TestWatchedItemAutoCreateAudit:
    async def test_auto_create_emits_audit(self, db_session, info_client):
        # Use the existing helpers for InfoItem + primary InfoSource binding,
        # mirroring tests/dashboard/test_routes.py::_seed_info_item.
        from tests.conftest import bind_primary_source, make_info_item, make_info_source
        from src.core.probe import ProbeResult
        from src.core.watches import create_watch

        item = await make_info_item(db_session, name="A")
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        await db_session.commit()

        async def fake_probe(url):
            return ProbeResult(effective_url=url, effective_domain="example.com")

        await create_watch(
            session=db_session, probe_fn=fake_probe, info_client=info_client,
            name="W", info_item_id=str(item.info_item_id),
        )
        events = (await db_session.execute(
            select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].payload["source"] == "auto_create"
```

- [ ] **Step 3: Run test, verify it fails**

Run: `uv run pytest tests/test_create_watch_service.py::TestWatchedItemAutoCreateAudit -v -m integration`
Expected: FAIL — zero audit rows.

- [ ] **Step 4: Update `_get_or_create_watched_item`**

```python
# src/core/watches/__init__.py
async def _get_or_create_watched_item(
    session: AsyncSession, *, info_item_id: ULID, fallback_name: str
) -> WatchedItem:
    existing = (
        await session.execute(select(WatchedItem).where(WatchedItem.info_item_id == info_item_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    wi = WatchedItem(info_item_id=info_item_id, name=fallback_name)
    session.add(wi)
    await session.flush()
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        info_item_id=str(info_item_id),
        name=fallback_name,
        source="auto_create",
    )
    return wi
```

Import `audit` and `EventType` at top of file if not already imported.

- [ ] **Step 5: Verify pass + full sweep of `tests/test_create_watch_service.py` and `tests/api/test_watches.py`**

Run: `uv run pytest tests/test_create_watch_service.py tests/api/test_watches.py -v -m integration`
Expected: PASS — no regressions from the new audit row.

- [ ] **Step 6: Commit**

```bash
git add src/core/watches/__init__.py tests/test_create_watch_service.py
git commit -m "#162 feat: audit WatchedItem auto-create from Watch-create path"
```

---

## SLICE 2 — API: `POST /api/v1/watched-items`

### Task 3: `WatchedItemCreate` schema

**Files:**
- Modify: `src/api/schemas/watched_item.py:1-30`
- Test: `tests/api/test_schemas.py` (existing schema test module)

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_schemas.py — append
def test_watched_item_create_requires_info_item_id():
    from pydantic import ValidationError
    from src.api.schemas.watched_item import WatchedItemCreate
    with pytest.raises(ValidationError):
        WatchedItemCreate(name="X")


def test_watched_item_create_minimal_ok():
    from src.api.schemas.watched_item import WatchedItemCreate
    schema = WatchedItemCreate(info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00")
    assert schema.info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
    assert schema.name is None
    assert schema.default_tags is None


def test_watched_item_create_full_ok():
    from src.api.schemas.watched_item import WatchedItemCreate
    schema = WatchedItemCreate(
        info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        name="Custom Name",
        description="Note",
        default_schedule_config={"interval": "15m"},
        default_content_type="html",
        default_tags=["regulatory"],
    )
    assert schema.default_content_type == "html"
```

- [ ] **Step 2: Run test, verify fail**

Run: `uv run pytest tests/api/test_schemas.py::test_watched_item_create_requires_info_item_id -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add the schema**

```python
# src/api/schemas/watched_item.py — append above WatchedItemPatch
class WatchedItemCreate(BaseModel):
    """Create a standalone WatchedItem.

    Identity is ``info_item_id`` (must reference a known Archiver InfoItem,
    1:1). The InfoItem's existence is NOT validated here — the route layer
    does that via the Archiver SDK and maps NotFound → 422.
    """

    info_item_id: ULIDStr
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None

    @field_validator("default_content_type")
    @classmethod
    def _ct(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ContentType(v)
        except ValueError as exc:
            raise ValueError(f"Invalid default_content_type: {v!r}") from exc
        return v
```

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/api/test_schemas.py -v -k watched_item_create`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas/watched_item.py tests/api/test_schemas.py
git commit -m "#162 feat: add WatchedItemCreate schema"
```

---

### Task 4: `POST /api/v1/watched-items` route

**Files:**
- Modify: `src/api/routes/watched_items.py:25-78` (add POST handler after `list_watched_items`)
- Test: `tests/api/test_watched_items.py:23` (add `TestCreateWatchedItem` class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/api/test_watched_items.py`:

```python
class TestCreateWatchedItem:
    async def test_creates_with_info_item_name_fallback(self, client, db_session, info_client):
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="Source Item")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={"info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["info_item_id"] == str(item.info_item_id)
        # Name falls back to the InfoItem's name when not supplied.
        assert body["name"] == "Source Item"
        assert body["default_schedule_config"] is None
        assert body["archived_at"] is None

    async def test_uses_supplied_name(self, client, db_session, info_client):
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="Source")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "info_item_id": str(item.info_item_id),
                "name": "Overridden",
                "default_schedule_config": {"interval": "10m"},
                "default_tags": ["regulatory"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Overridden"
        assert body["default_schedule_config"] == {"interval": "10m"}
        assert body["default_tags"] == ["regulatory"]

    async def test_duplicate_info_item_id_returns_409(self, client, db_session, info_client):
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="X")
        await db_session.commit()
        r1 = await client.post(
            "/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)}
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)}
        )
        assert r2.status_code == 409
        assert "already" in r2.json()["detail"].lower()

    async def test_unknown_info_item_returns_422(self, client, info_client):
        from archiver_client import NotFound
        from unittest.mock import AsyncMock
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.post(
            "/api/v1/watched-items",
            json={"info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ"},
        )
        assert response.status_code == 422

    async def test_emits_audit_event(self, client, db_session, info_client):
        from sqlalchemy import select
        from src.core.models.audit_log import AuditLog, EventType
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="A")
        await db_session.commit()
        await client.post(
            "/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)}
        )
        events = (await db_session.execute(
            select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].payload["source"] == "api"
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/api/test_watched_items.py::TestCreateWatchedItem -v -m integration`
Expected: All FAIL (404 because route doesn't exist).

- [ ] **Step 3: Add the POST route**

```python
# src/api/routes/watched_items.py — insert after list_watched_items (line ~48)
import httpx
from archiver_client import AuthError, NotFound as ArchiverNotFound, ServerError
from sqlalchemy.exc import IntegrityError

from src.api.schemas.watched_item import WatchedItemCreate
from src.core.logging import get_logger
from src.core.registry import get_registry
from ulid import ULID

logger = get_logger(__name__)


@router.post("", response_model=WatchedItemResponse, status_code=201)
async def create_watched_item(
    data: WatchedItemCreate, session: AsyncSession = Depends(get_db_session)
):
    """Create a standalone WatchedItem bound to an Archiver InfoItem.

    The InfoItem's existence + name are resolved via the Archiver SDK.
    Errors map exactly like the Watch-create route: NotFound → 422,
    AuthError → 500, ServerError/network → 503 with Retry-After.
    """
    info_client = get_registry().get_archiver_client()
    try:
        info_item = await info_client.get_info_item(data.info_item_id)
    except ArchiverNotFound as exc:
        raise HTTPException(
            status_code=422, detail=f"info_item_id {data.info_item_id} does not exist"
        ) from exc
    except AuthError:
        logger.exception("ArchiverClient auth failure during watched_item create")
        raise HTTPException(status_code=500, detail="Information service auth failed") from None
    except (ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=503,
            detail="Information service unavailable; retry shortly",
            headers={"Retry-After": "30"},
        ) from exc

    wi = WatchedItem(
        info_item_id=ULID.from_str(data.info_item_id),
        name=data.name or info_item.name,
        description=data.description,
        default_schedule_config=data.default_schedule_config,
        default_content_type=data.default_content_type,
        default_tags=data.default_tags,
    )
    session.add(wi)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"WatchedItem for info_item_id {data.info_item_id} already exists",
        ) from exc
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        info_item_id=data.info_item_id,
        name=wi.name,
        source="api",
    )
    await session.commit()
    await session.refresh(wi)
    return wi
```

Lift any new imports to the top of the file per `AGENTS.md` "no inline module imports".

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/api/test_watched_items.py -v -m integration`
Expected: all `TestCreateWatchedItem` + the existing list/get/patch/archive tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/watched_items.py tests/api/test_watched_items.py
git commit -m "#162 feat: POST /api/v1/watched-items standalone create"
```

---

## SLICE 3 — Picker partials + search/binding-tree routes

### Task 5: Mock `find_info_item` in the `info_client` fixture

**Files:**
- Modify: `tests/conftest.py:506-545` (the `info_client` fixture)

- [ ] **Step 1: Add a sanity test**

```python
# tests/dashboard/test_info_item_picker_routes.py — new file
import pytest

pytestmark = pytest.mark.integration


class TestFindInfoItemFixture:
    async def test_fake_client_find_returns_db_matches(self, db_session, info_client):
        from tests.conftest import make_info_item
        await make_info_item(db_session, name="Alpha Item")
        await make_info_item(db_session, name="Bravo Item")
        await db_session.commit()
        results = await info_client.find_info_item("alpha")
        assert any(r.name == "Alpha Item" for r in results)
        assert not any(r.name == "Bravo Item" for r in results)
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestFindInfoItemFixture -v -m integration`
Expected: FAIL — `find_info_item` not on the fake.

- [ ] **Step 3: Add the fake**

In `tests/conftest.py` inside the `info_client` fixture body, after the existing `_get_info_item` definition:

```python
    async def _find_info_item(query: str, *, limit: int = 20):
        # ILIKE on name + description, mirroring Archiver's pg_trgm-backed
        # find_info_item, but minus the trigram ranking — substring is fine
        # for tests.
        from sqlalchemy import or_
        q = f"%{query}%"
        result = await db_session.execute(
            select(InfoItem)
            .where(or_(InfoItem.name.ilike(q), InfoItem.description.ilike(q)))
            .order_by(InfoItem.created_at.desc())
            .limit(limit)
        )
        items = result.scalars().all()
        out = []
        for item in items:
            entry = MagicMock()
            entry.info_item_id = str(item.info_item_id)
            entry.name = item.name
            entry.description = item.description
            entry.created_at = item.created_at or datetime.now(UTC)
            entry.updated_at = item.updated_at or datetime.now(UTC)
            out.append(entry)
        return out

    fake_client.find_info_item = AsyncMock(side_effect=_find_info_item)
```

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestFindInfoItemFixture -v -m integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/dashboard/test_info_item_picker_routes.py
git commit -m "#162 test: fake find_info_item on info_client fixture"
```

---

### Task 6: `GET /info-items/search` — typeahead results route

**Files:**
- Modify: `src/dashboard/routes.py` (add new route module-block before `/watches/new`)
- Create: `src/dashboard/templates/partials/info_item_picker/results.html`
- Test: `tests/dashboard/test_info_item_picker_routes.py`

The route is a public partial endpoint — no auth gate beyond the dashboard's existing `Depends(get_dashboard_user)`. URL = `/info-items/search`, not `/info-items` so it never collides with a future detail-page route.

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_info_item_picker_routes.py`:

```python
class TestSearchRoute:
    async def test_search_returns_results_partial(self, client, db_session, info_client):
        from tests.conftest import make_info_item
        await make_info_item(db_session, name="LCB Annual Report")
        await db_session.commit()
        response = await client.get("/info-items/search?q=lcb&mode=select_with_target")
        assert response.status_code == 200
        body = response.text
        assert "LCB Annual Report" in body
        # role="option" makes the row a combobox option
        assert 'role="option"' in body

    async def test_search_no_results_renders_empty_state(self, client, info_client):
        response = await client.get("/info-items/search?q=zzzzzzzz")
        assert response.status_code == 200
        assert "No matches" in response.text

    async def test_search_empty_query_returns_no_results(self, client, info_client):
        # Don't fan out to the SDK on an empty query — render the empty hint.
        response = await client.get("/info-items/search?q=")
        assert response.status_code == 200
        info_client.find_info_item.assert_not_called()

    async def test_search_limit_capped(self, client, info_client, db_session):
        from tests.conftest import make_info_item
        for i in range(25):
            await make_info_item(db_session, name=f"Item {i:02d}")
        await db_session.commit()
        await client.get("/info-items/search?q=Item")
        # SDK is called with limit=20 (the design's recommended bound)
        args, kwargs = info_client.find_info_item.call_args
        assert kwargs.get("limit") == 20
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestSearchRoute -v -m integration`
Expected: 4 FAIL — route doesn't exist.

- [ ] **Step 3: Create the results partial**

`src/dashboard/templates/partials/info_item_picker/results.html`:

```jinja
{# Typeahead results — HTMX swap target. Caller passes:
   results: list of InfoItemOut-shaped objects (SDK + test fakes both expose .info_item_id / .name / .description)
   mode: "select_with_target" | "select_only"
   target_form_id: form to populate
   query: original query (for "no results" message)
#}
<ul role="listbox" aria-label="InfoItem search results" class="divide-y divide-gray-200 dark:divide-gray-700">
  {% if results %}
    {% for item in results %}
    <li id="iip-opt-{{ loop.index0 }}" role="option" aria-selected="false"
        class="px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer focus-within:bg-gray-50 dark:focus-within:bg-gray-700">
      <button type="button"
        class="block w-full text-start min-h-[44px]"
        data-info-item-id="{{ item.info_item_id }}"
        data-info-item-name="{{ item.name }}"
        {% if mode == "select_with_target" %}
        hx-get="/info-items/{{ item.info_item_id }}/binding-tree?mode=select_with_target&target_form_id={{ target_form_id }}"
        hx-target="#{{ target_form_id }}-binding-tree"
        hx-swap="innerHTML"
        {% else %}
        data-info-item-select="true"
        {% endif %}>
        <span class="block font-medium text-gray-900 dark:text-white">{{ item.name }}</span>
        {% if item.description %}
        <span class="block text-xs text-gray-500 dark:text-gray-400">{{ item.description }}</span>
        {% endif %}
        <span class="block text-xs font-mono text-gray-400">{{ item.info_item_id }}</span>
      </button>
    </li>
    {% endfor %}
  {% else %}
    <li class="px-3 py-2 text-sm text-gray-500 dark:text-gray-400">No matches{% if query %} for "{{ query }}"{% endif %}.</li>
  {% endif %}
</ul>
```

- [ ] **Step 4: Add the route**

In `src/dashboard/routes.py`, before the `/watches/new` block:

```python
@router.get("/info-items/search")
async def info_items_search(
    request: Request,
    q: str = "",
    mode: Literal["select_with_target", "select_only"] = "select_with_target",
    target_form_id: str = "watch-create",
):
    """Typeahead results partial for the InfoItem picker.

    Mirrors the design's `/watches/new/info-items` route but generalized:
    the picker is reused on /watched-items/new (mode=select_only) and (via
    a future surface) the /watches/new flow (mode=select_with_target).
    """
    query = q.strip()
    if not query:
        return templates.TemplateResponse(
            request,
            "partials/info_item_picker/results.html",
            {"results": [], "mode": mode, "target_form_id": target_form_id, "query": ""},
        )
    info_client = get_registry().get_archiver_client()
    try:
        results = await info_client.find_info_item(query, limit=20)
    except (ServerError, httpx.ConnectError, httpx.TimeoutException, AuthError):
        logger.warning("find_info_item failed during picker search", extra={"q": query})
        results = []
    return templates.TemplateResponse(
        request,
        "partials/info_item_picker/results.html",
        {"results": results, "mode": mode, "target_form_id": target_form_id, "query": query},
    )
```

- [ ] **Step 5: Verify pass**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestSearchRoute -v -m integration`
Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/info_item_picker/results.html tests/dashboard/test_info_item_picker_routes.py
git commit -m "#162 feat: GET /info-items/search typeahead results route"
```

---

### Task 7: `GET /info-items/{info_item_id}/binding-tree`

**Files:**
- Modify: `src/dashboard/routes.py`
- Create: `src/dashboard/templates/partials/info_item_picker/binding_tree.html`
- Test: `tests/dashboard/test_info_item_picker_routes.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/dashboard/test_info_item_picker_routes.py — append
class TestBindingTreeRoute:
    async def test_renders_primary_only(self, client, db_session, info_client):
        from tests.conftest import bind_primary_source, make_info_item, make_info_source
        item = await make_info_item(db_session, name="X")
        primary = await make_info_source(db_session, url="https://example.com/p")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        assert response.status_code == 200
        body = response.text
        assert "primary" in body.lower()
        assert "https://example.com/p" in body

    async def test_renders_sub_aspect_selectable(self, client, db_session, info_client):
        from tests.conftest import (bind_primary_source, bind_sub_aspect,
                                     make_info_item, make_info_source)
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        sub = await make_info_source(db_session,
                                      parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(db_session, info_item_id=item.info_item_id,
                              info_source_id=sub.info_source_id)
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        body = response.text
        # sub_aspect row is selectable
        assert "sub_aspect" in body
        assert str(sub.info_source_id) in body
        # selectable controls show name attributes
        assert 'value="' + str(sub.info_source_id) + '"' in body

    async def test_cross_check_muted(self, client, db_session, info_client):
        from tests.conftest import (bind_primary_source, make_info_item,
                                     make_info_source)
        from src.core.models.info_item_source import InfoItemSource
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        cc = await make_info_source(db_session,
                                     parent_info_source_id=primary.info_source_id)
        db_session.add(InfoItemSource(
            info_item_id=item.info_item_id,
            info_source_id=cc.info_source_id, role="cross_check",
        ))
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        body = response.text
        assert "cross_check" in body
        # cross_check is NOT exposed as a selectable form value
        assert 'value="' + str(cc.info_source_id) + '"' not in body

    async def test_readonly_mode_omits_form_controls(self, client, db_session, info_client):
        from tests.conftest import (bind_primary_source, bind_sub_aspect,
                                     make_info_item, make_info_source)
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        sub = await make_info_source(db_session,
                                      parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(db_session, info_item_id=item.info_item_id,
                              info_source_id=sub.info_source_id)
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=readonly_tree"
        )
        body = response.text
        assert "sub_aspect" in body
        # Readonly: no <input>/<button type=radio>/select controls
        assert "<input " not in body
        assert 'type="radio"' not in body

    async def test_404_unknown_info_item(self, client, info_client):
        from archiver_client import NotFound
        from unittest.mock import AsyncMock
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.get(
            "/info-items/01ZZZZZZZZZZZZZZZZZZZZZZZZ/binding-tree"
        )
        assert response.status_code == 404
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestBindingTreeRoute -v -m integration`
Expected: all FAIL.

- [ ] **Step 3: Create the binding-tree partial**

`src/dashboard/templates/partials/info_item_picker/binding_tree.html`:

```jinja
{# Step-2 / readonly InfoItem binding tree. Caller passes:
   info_item: InfoItemOut-shaped (.info_item_id, .name)
   primary_url: str | None (resolved by route from get_info_source on the primary)
   cross_checks: list of InfoSourceOut-shaped objects
   sub_aspects: list of InfoSourceOut-shaped objects
   mode: "select_with_target" | "select_only" | "readonly_tree"
   target_form_id: str (form to scope the radio name)
   new_subaspect_ids: set[str] | None (sub_aspects to flag as "new" — only used in readonly_tree)
#}
<div class="stat-card" aria-label="InfoItem binding tree">
  <header class="mb-3">
    <p class="text-sm text-gray-500 dark:text-gray-400">Archiver InfoItem</p>
    <p class="font-semibold text-gray-900 dark:text-white">{{ info_item.name }}</p>
    <p class="text-xs font-mono text-gray-400">{{ info_item.info_item_id }}</p>
  </header>

  <ul class="space-y-2 text-sm">
    <li class="flex items-start gap-2">
      {% if mode == "select_with_target" %}
      <label class="flex items-start gap-2 cursor-pointer min-h-[44px]">
        <input type="radio" name="{{ target_form_id }}__target" value=""
          form="{{ target_form_id }}" checked
          class="mt-1">
        <span>
          <span class="badge badge-active">primary</span>
          <span class="break-all text-gray-700 dark:text-gray-300">{{ primary_url or "—" }}</span>
        </span>
      </label>
      {% else %}
      <span class="flex items-start gap-2">
        <span class="badge badge-active">primary</span>
        <span class="break-all text-gray-700 dark:text-gray-300">{{ primary_url or "—" }}</span>
      </span>
      {% endif %}
    </li>

    {% for src in sub_aspects %}
    <li class="flex items-start gap-2">
      {% if mode == "select_with_target" %}
      <label class="flex items-start gap-2 cursor-pointer min-h-[44px]">
        <input type="radio" name="{{ target_form_id }}__target"
          value="{{ src.info_source_id }}" form="{{ target_form_id }}"
          class="mt-1">
        <span>
          <span class="badge badge-info">sub_aspect</span>
          {% if new_subaspect_ids and src.info_source_id in new_subaspect_ids %}
          <span class="badge badge-warning">new</span>
          {% endif %}
          <span class="font-mono text-xs text-gray-500">{{ src.info_source_id }}</span>
        </span>
      </label>
      {% else %}
      <span class="flex items-start gap-2">
        <span class="badge badge-info">sub_aspect</span>
        {% if new_subaspect_ids and src.info_source_id in new_subaspect_ids %}
        <span class="badge badge-warning">new</span>
        {% endif %}
        <span class="font-mono text-xs text-gray-500">{{ src.info_source_id }}</span>
      </span>
      {% endif %}
    </li>
    {% endfor %}

    {% for src in cross_checks %}
    <li class="flex items-start gap-2 opacity-70">
      <span class="badge badge-inactive">cross_check</span>
      <span class="font-mono text-xs text-gray-500">{{ src.info_source_id }}</span>
      <span class="text-xs text-gray-400">(infrastructure)</span>
    </li>
    {% endfor %}
  </ul>

  {% if mode == "select_only" %}
  <input type="hidden" name="info_item_id" value="{{ info_item.info_item_id }}" form="{{ target_form_id }}">
  {% elif mode == "select_with_target" %}
  <input type="hidden" name="info_item_id" value="{{ info_item.info_item_id }}" form="{{ target_form_id }}">
  {% endif %}
</div>
```

- [ ] **Step 4: Add the route**

In `src/dashboard/routes.py`:

```python
@router.get("/info-items/{info_item_id}/binding-tree")
async def info_item_binding_tree(
    request: Request,
    info_item_id: str,
    mode: Literal["select_with_target", "select_only", "readonly_tree"] = "select_with_target",
    target_form_id: str = "watch-create",
    new_subaspect_ids: str = "",  # comma-separated; only used by readonly_tree callers
):
    """Step-2 binding tree partial.

    Bound to the search route's result-row hx-get target. Renders
    primary + sub_aspects (selectable in step-2 modes) + cross_checks (muted,
    never selectable). NotFound → 404 partial.
    """
    info_client = get_registry().get_archiver_client()
    try:
        bindings = await fetch_info_item_bindings(info_client, info_item_id)
        info_item = await info_client.get_info_item(info_item_id)
    except NotFound:
        return HTMLResponse(
            '<p class="text-sm text-red-600">InfoItem not found.</p>', status_code=404
        )
    flagged = {s.strip() for s in new_subaspect_ids.split(",") if s.strip()} or None
    return templates.TemplateResponse(
        request,
        "partials/info_item_picker/binding_tree.html",
        {
            "info_item": info_item,
            "primary_url": bindings.primary_url,
            "cross_checks": bindings.cross_checks,
            "sub_aspects": bindings.sub_aspects,
            "mode": mode,
            "target_form_id": target_form_id,
            "new_subaspect_ids": flagged,
        },
    )
```

Import `fetch_info_item_bindings` at the top of `routes.py`:

```python
from src.core.watches.info_item_fetch import fetch_info_item_bindings
```

- [ ] **Step 5: Verify pass**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestBindingTreeRoute -v -m integration`
Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/info_item_picker/binding_tree.html tests/dashboard/test_info_item_picker_routes.py
git commit -m "#162 feat: GET /info-items/{id}/binding-tree partial"
```

---

### Task 8: Typeahead step-1 partial + keyboard JS

**Files:**
- Create: `src/dashboard/templates/partials/info_item_picker/typeahead.html`
- Create: `src/dashboard/static/js/info-item-picker.js`
- Modify: `src/dashboard/templates/base.html:108-113` (load the new JS with `defer`)
- Test: `tests/dashboard/test_a11y_attributes.py` (extend with picker assertions)

- [ ] **Step 1: Write the failing partial test**

Add to `tests/dashboard/test_info_item_picker_routes.py`:

```python
class TestTypeaheadPartial:
    async def test_typeahead_renders_combobox_attributes(self, client, db_session, info_client):
        # We exercise the partial via the Watch-create page which includes it.
        response = await client.get("/watches/new")
        assert response.status_code == 200
        body = response.text
        assert 'role="combobox"' in body
        assert 'aria-expanded' in body
        assert 'aria-activedescendant' in body
        assert 'hx-get="/info-items/search' in body
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestTypeaheadPartial -v -m integration`
Expected: FAIL — partial not in the page yet.

- [ ] **Step 3: Create the typeahead partial**

`src/dashboard/templates/partials/info_item_picker/typeahead.html`:

```jinja
{# Step-1 InfoItem typeahead. Caller passes:
   target_form_id: str (the form that contains the eventual <input name="info_item_id">)
   mode: "select_with_target" | "select_only"
   field_label: str (defaults to "InfoItem")
#}
<div class="info-item-picker" data-target-form="{{ target_form_id }}" data-mode="{{ mode }}">
  <label for="{{ target_form_id }}-iip-input" class="form-label">{{ field_label or "InfoItem" }}</label>
  <input
    id="{{ target_form_id }}-iip-input"
    type="search"
    class="form-input mt-1"
    autocomplete="off"
    placeholder="Type to search by name…"
    role="combobox"
    aria-expanded="false"
    aria-autocomplete="list"
    aria-controls="{{ target_form_id }}-iip-results"
    aria-activedescendant=""
    hx-get="/info-items/search?mode={{ mode }}&target_form_id={{ target_form_id }}"
    hx-trigger="input changed delay:250ms, search"
    hx-target="#{{ target_form_id }}-iip-results"
    hx-swap="innerHTML"
    hx-params="q,mode,target_form_id"
    name="q">
  <div id="{{ target_form_id }}-iip-results"
       class="border border-gray-200 dark:border-gray-700 rounded-md mt-1 max-h-72 overflow-y-auto"
       aria-live="polite" aria-atomic="false">
    {# Initial empty state — server-renders no <ul> until first search. #}
  </div>
  <div id="{{ target_form_id }}-binding-tree" class="mt-3">
    {# Binding-tree partial swapped in here when an option is chosen
       (mode=select_with_target). For select_only, a hidden info_item_id
       input is injected into the parent form via the binding-tree partial. #}
  </div>
</div>
```

- [ ] **Step 4: Create the keyboard-nav JS**

`src/dashboard/static/js/info-item-picker.js`:

```javascript
/* InfoItem picker keyboard navigation.
 *
 * Listens on `.info-item-picker` containers. Arrow keys move
 * aria-activedescendant across the listbox; Enter activates the highlighted
 * option; Escape clears.
 */
(function () {
  function activate(option) {
    if (!option) return;
    const btn = option.querySelector('button[data-info-item-id]');
    if (btn) btn.click();
  }

  function wire(picker) {
    const input = picker.querySelector('input[role="combobox"]');
    const resultsId = input.getAttribute('aria-controls');
    if (!input || !resultsId) return;

    let activeIdx = -1;

    function options() {
      const region = document.getElementById(resultsId);
      return region ? region.querySelectorAll('[role="option"]') : [];
    }

    function highlight(idx) {
      const opts = options();
      if (!opts.length) {
        activeIdx = -1;
        input.setAttribute('aria-activedescendant', '');
        return;
      }
      activeIdx = Math.max(0, Math.min(opts.length - 1, idx));
      opts.forEach((o, i) => o.setAttribute('aria-selected', i === activeIdx ? 'true' : 'false'));
      input.setAttribute('aria-activedescendant', opts[activeIdx].id || '');
    }

    input.addEventListener('keydown', function (e) {
      const opts = options();
      if (e.key === 'ArrowDown') { e.preventDefault(); highlight(activeIdx + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(activeIdx - 1); }
      else if (e.key === 'Enter') {
        if (activeIdx >= 0 && opts[activeIdx]) { e.preventDefault(); activate(opts[activeIdx]); }
      }
      else if (e.key === 'Escape') {
        input.value = '';
        document.getElementById(resultsId).innerHTML = '';
        highlight(-1);
        input.setAttribute('aria-expanded', 'false');
      }
    });

    /* Reset highlight after HTMX swaps in new results. */
    document.body.addEventListener('htmx:afterSwap', function (e) {
      if (e.detail && e.detail.target && e.detail.target.id === resultsId) {
        activeIdx = -1;
        input.setAttribute('aria-expanded', options().length > 0 ? 'true' : 'false');
      }
    });
  }

  document.querySelectorAll('.info-item-picker').forEach(wire);
})();
```

- [ ] **Step 5: Wire the JS in base.html**

Edit `src/dashboard/templates/base.html` after the existing scripts (~line 112):

```html
<script src="/static/js/info-item-picker.js?v={{ build_id }}" defer></script>
```

- [ ] **Step 6: Add a stub include in `watch_form.html` (replaces target_picker)**

Replace the `{% include "partials/target_picker.html" %}` block in `pages/watch_form.html` (line 18) with:

```jinja
<input type="hidden" name="info_item_id" value="" form="watch-create" id="watch-create-info-item-id">
{% with target_form_id="watch-create", mode="select_with_target" %}
{% include "partials/info_item_picker/typeahead.html" %}
{% endwith %}
```

(The full form rewrite lands in Task 9; this stub satisfies the typeahead-partial-renders test.)

- [ ] **Step 7: Verify pass**

Run: `uv run pytest tests/dashboard/test_info_item_picker_routes.py::TestTypeaheadPartial -v -m integration`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/templates/partials/info_item_picker/ src/dashboard/static/js/info-item-picker.js src/dashboard/templates/base.html src/dashboard/templates/pages/watch_form.html tests/dashboard/test_info_item_picker_routes.py
git commit -m "#162 feat: InfoItem typeahead partial + keyboard navigation"
```

---

## SLICE 4 — Wire Watch-create to the new picker

### Task 9: Update Watch-create form + handler

**Files:**
- Modify: `src/dashboard/templates/pages/watch_form.html`
- Modify: `src/dashboard/routes.py:252-318` (POST handler — read radio name now)
- Delete: `src/dashboard/templates/partials/target_picker.html`
- Modify: `tests/dashboard/test_routes.py:150-283` (`TestWatchCreate`)

- [ ] **Step 1: Update / add tests**

Edit `tests/dashboard/test_routes.py`:

```python
class TestWatchCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        assert b"New Watch" in response.content

    async def test_create_form_renders_typeahead_picker(self, client):
        response = await client.get("/watches/new")
        body = response.content
        # The form switched from ULID-paste to typeahead.
        assert b'role="combobox"' in body
        assert b"hx-get=\"/info-items/search" in body
        # Hidden info_item_id input lives on the form.
        assert b'name="info_item_id"' in body
        # Power-user paste-ULID fallback is wrapped in <details>.
        assert b"<details" in body
        assert b"Paste ULID" in body
        # Legacy minimal-picker text input is gone.
        assert b'pattern="[0-9A-Za-z]{26}"' not in body

    # test_create_watch_redirects: keep, but submit using watch-create__target= ""
    async def test_create_watch_redirects(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Created Watch")
        response = await client.post(
            "/watches/new",
            data={
                "name": "Created Watch",
                "info_item_id": info_item_id,
                "watch-create__target": "",  # primary
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_with_subaspect_target(self, client, db_session):
        from tests.conftest import (bind_primary_source, bind_sub_aspect,
                                     make_info_item, make_info_source)
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(db_session, info_item_id=item.info_item_id,
                                  info_source_id=primary.info_source_id)
        sub = await make_info_source(db_session,
                                      parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(db_session, info_item_id=item.info_item_id,
                              info_source_id=sub.info_source_id)
        await db_session.commit()
        response = await client.post(
            "/watches/new",
            data={
                "name": "Sub Watch",
                "info_item_id": str(item.info_item_id),
                "watch-create__target": str(sub.info_source_id),
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    # Existing tests for missing-name, unreachable URL, info_item_only, bad-id
    # remain unchanged but should pass watch-create__target="" where applicable.
```

Delete the old `test_create_form_has_target_picker` test entirely.

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_routes.py::TestWatchCreate -v -m integration`
Expected: at least `test_create_form_renders_typeahead_picker` FAIL.

- [ ] **Step 3: Rewrite `pages/watch_form.html`**

```jinja
{% extends "base.html" %}
{% block title %}New Watch — Watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">New Watch</h2>

{% include "partials/flash.html" %}

<form id="watch-create" method="post" action="/watches/new" class="max-w-xl space-y-6">
  <div>
    <label for="name" class="form-label">Name</label>
    <input type="text" name="name" id="name" required class="form-input mt-1">
  </div>

  <fieldset>
    <legend class="form-label">Target</legend>
    {% with target_form_id="watch-create", mode="select_with_target" %}
    {% include "partials/info_item_picker/typeahead.html" %}
    {% endwith %}
  </fieldset>

  <details class="text-sm">
    <summary class="cursor-pointer text-gray-600 dark:text-gray-400">Paste ULID</summary>
    <div class="mt-2 space-y-2">
      <label for="info_item_id_manual" class="form-label">InfoItem ULID</label>
      <input type="text" name="info_item_id_manual" id="info_item_id_manual"
        pattern="[0-9A-Za-z]{26}" class="form-input mt-1 font-mono"
        placeholder="01XXXXXXXXXXXXXXXXXXXXXXXXX">
      <label for="target_info_source_id_manual" class="form-label">sub_aspect ULID (optional)</label>
      <input type="text" name="target_info_source_id_manual" id="target_info_source_id_manual"
        pattern="[0-9A-Za-z]{26}" class="form-input mt-1 font-mono"
        placeholder="(leave blank for primary)">
      <p class="text-xs text-gray-500 dark:text-gray-400">Power-user fallback. Either use the picker above OR these inputs — not both.</p>
    </div>
  </details>

  <div>
    <label for="content_type" class="form-label">Content Type</label>
    <select name="content_type" id="content_type" class="form-input mt-1">
      {% for ct in content_types %}
      <option value="{{ ct.value }}">{{ ct.value | upper }}</option>
      {% endfor %}
    </select>
  </div>

  <div>
    <label for="description" class="form-label">Description</label>
    <textarea name="description" id="description" rows="2" class="form-input mt-1"></textarea>
  </div>

  <div class="flex gap-3">
    <button type="submit" class="btn btn-primary">Create Watch</button>
    <a href="/watches" class="btn btn-secondary">Cancel</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Update the POST handler**

Modify `src/dashboard/routes.py` `watch_create_submit`:

```python
@router.post("/watches/new")
async def watch_create_submit(
    request: Request,
    name: str = Form(""),
    info_item_id: str = Form(""),
    info_item_id_manual: str = Form(""),
    target_info_source_id_manual: str = Form(""),
    content_type: str = Form("html"),
    description: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """#162: form now accepts EITHER picker output (info_item_id +
    watch-create__target radio) OR the paste-ULID fallback. Picker wins
    when both are present; the manual block is read only when the picker's
    info_item_id is empty."""
    form = await request.form()
    target_radio = form.get("watch-create__target", "").strip()

    if info_item_id.strip():
        resolved_info_item_id = info_item_id.strip()
        resolved_target = target_radio or None
    else:
        resolved_info_item_id = info_item_id_manual.strip()
        resolved_target = target_info_source_id_manual.strip() or None

    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not resolved_info_item_id:
        errors.append("InfoItem is required")

    # ... existing error rendering + try/except, but pass
    # resolved_info_item_id / resolved_target through to _create_watch.
```

(Keep the rest of the existing error-mapping logic verbatim.)

- [ ] **Step 5: Delete the orphaned partial**

```bash
git rm src/dashboard/templates/partials/target_picker.html
```

- [ ] **Step 6: Verify pass**

Run: `uv run pytest tests/dashboard/test_routes.py::TestWatchCreate -v -m integration`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/pages/watch_form.html src/dashboard/routes.py tests/dashboard/test_routes.py
git commit -m "#162 feat: switch Watch-create form to typeahead picker"
```

---

## SLICE 5 — WatchedItem-create UI

### Task 10: Dashboard route + template for `/watched-items/new`

**Files:**
- Modify: `src/dashboard/routes.py` (insert routes near `/watched-items` block ~line 848)
- Create: `src/dashboard/templates/pages/watched_item_form.html`
- Modify: `src/dashboard/templates/pages/watched_items.html`
- Test: `tests/dashboard/test_watched_item_create.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/dashboard/test_watched_item_create.py — new file
import pytest

pytestmark = pytest.mark.integration


class TestWatchedItemCreateForm:
    async def test_form_returns_200(self, client):
        response = await client.get("/watched-items/new")
        assert response.status_code == 200
        assert b"New WatchedItem" in response.content

    async def test_form_renders_typeahead_picker(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'role="combobox"' in body
        assert b"select_only" in body  # mode

    async def test_form_has_default_fields(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        # Optional defaults — pre-populate on create
        assert b'name="name"' in body
        assert b'name="description"' in body
        assert b'name="default_schedule_interval"' in body
        assert b'name="default_content_type"' in body


class TestWatchedItemCreateSubmit:
    async def test_redirects_on_success(self, client, db_session, info_client):
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="Pre-Bound")
        await db_session.commit()
        response = await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id), "name": "WI X"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/watched-items/")

    async def test_persists_defaults(self, client, db_session, info_client):
        from sqlalchemy import select
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="Pre-Bound")
        await db_session.commit()
        await client.post(
            "/watched-items/new",
            data={
                "info_item_id": str(item.info_item_id),
                "name": "WI Y",
                "description": "note",
                "default_schedule_interval": "15m",
                "default_content_type": "html",
                "default_tags": "regulatory, legislative",
            },
            follow_redirects=False,
        )
        wi = (await db_session.execute(
            select(WatchedItem).where(WatchedItem.info_item_id == item.info_item_id)
        )).scalar_one()
        assert wi.name == "WI Y"
        assert wi.description == "note"
        assert wi.default_schedule_config == {"interval": "15m"}
        assert wi.default_content_type == "html"
        assert set(wi.default_tags) == {"regulatory", "legislative"}

    async def test_duplicate_info_item_shows_flash(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session, name="X")
        db_session.add(WatchedItem(info_item_id=item.info_item_id, name="exists"))
        await db_session.commit()
        response = await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 200
        assert b"already exists" in response.content

    async def test_missing_info_item_id_shows_flash(self, client):
        response = await client.post("/watched-items/new", data={"name": "X"})
        assert response.status_code == 200
        assert b"required" in response.content.lower()


class TestListPageHasCreateLink:
    async def test_list_page_has_new_button(self, client):
        response = await client.get("/watched-items")
        body = response.content
        assert b"/watched-items/new" in body
        assert b"New WatchedItem" in body
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_watched_item_create.py -v -m integration`
Expected: all FAIL (404 / no route).

- [ ] **Step 3: Create the form template**

`src/dashboard/templates/pages/watched_item_form.html`:

```jinja
{% extends "base.html" %}
{% block title %}New WatchedItem — Watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">New WatchedItem</h2>

{% include "partials/flash.html" %}

<form id="wi-create" method="post" action="/watched-items/new" class="max-w-xl space-y-6">
  <fieldset>
    <legend class="form-label">InfoItem</legend>
    {% with target_form_id="wi-create", mode="select_only" %}
    {% include "partials/info_item_picker/typeahead.html" %}
    {% endwith %}
    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
      Subscribes this WatchedItem to one Archiver InfoItem (1:1). Defaults below
      apply to all child Watches that don't override.
    </p>
  </fieldset>

  <div>
    <label for="name" class="form-label">Name <span class="text-xs text-gray-500">(optional)</span></label>
    <input type="text" name="name" id="name" maxlength="255" class="form-input mt-1"
      placeholder="Defaults to the InfoItem's name.">
  </div>

  <div>
    <label for="description" class="form-label">Description <span class="text-xs text-gray-500">(optional)</span></label>
    <textarea name="description" id="description" rows="2" class="form-input mt-1"></textarea>
  </div>

  <div>
    <label for="default_schedule_interval" class="form-label">Default interval <span class="text-xs text-gray-500">(optional)</span></label>
    <input type="text" name="default_schedule_interval" id="default_schedule_interval"
      placeholder="e.g. 15m, 6h, 1d" class="form-input mt-1">
    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Child Watches inherit unless they override.</p>
  </div>

  <div>
    <label for="default_content_type" class="form-label">Default content type <span class="text-xs text-gray-500">(optional)</span></label>
    <select name="default_content_type" id="default_content_type" class="form-input mt-1">
      <option value="">—</option>
      <option value="html">HTML</option>
      <option value="pdf">PDF</option>
    </select>
  </div>

  <div>
    <label for="default_tags" class="form-label">Default tags <span class="text-xs text-gray-500">(optional, comma-separated)</span></label>
    <input type="text" name="default_tags" id="default_tags" class="form-input mt-1"
      placeholder="regulatory, legislative">
  </div>

  <div class="flex gap-3">
    <button type="submit" class="btn btn-primary">Create WatchedItem</button>
    <a href="/watched-items" class="btn btn-secondary">Cancel</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Update the list page**

Edit `src/dashboard/templates/pages/watched_items.html`:

```jinja
{% extends "base.html" %}
{% block title %}Watched Items — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6 flex-wrap gap-4">
  <h2 class="text-2xl font-semibold text-gray-900 dark:text-white">Watched Items</h2>
  <a href="/watched-items/new" class="btn btn-primary">New WatchedItem</a>
</div>

{# ... rest unchanged, except the empty-state CTA: #}
{% if not watched_items %}
<div class="stat-card text-center py-12">
  <p class="text-lg font-medium text-gray-900 dark:text-white mb-2">No watched items yet</p>
  <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
    Create a WatchedItem to stage defaults, or jump straight to creating a Watch
    (a WatchedItem is auto-created on the first Watch under an InfoItem).
  </p>
  <div class="flex justify-center gap-3">
    <a href="/watched-items/new" class="btn btn-primary">New WatchedItem</a>
    <a href="/watches/new" class="btn btn-secondary">New Watch</a>
  </div>
</div>
{% endif %}
```

- [ ] **Step 5: Add dashboard routes**

In `src/dashboard/routes.py`, before `@router.get("/watched-items")` (line 848):

```python
@router.get("/watched-items/new")
async def watched_item_create_form(request: Request):
    """Standalone WatchedItem create form."""
    return templates.TemplateResponse(
        request,
        "pages/watched_item_form.html",
        {"active_page": "watched-items", "flash": None},
    )


@router.post("/watched-items/new")
async def watched_item_create_submit(
    request: Request,
    info_item_id: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    default_schedule_interval: str = Form(""),
    default_content_type: str = Form(""),
    default_tags: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a standalone WatchedItem.

    Mirrors the API route's error handling but renders flashes instead of
    raising HTTPException. Delegates to the same registry-backed SDK.
    """
    async def _render_with_flash(message: str, level: str = "error"):
        return templates.TemplateResponse(
            request,
            "pages/watched_item_form.html",
            {"active_page": "watched-items", "flash": {"type": level, "message": message}},
        )

    iid = info_item_id.strip()
    if not iid:
        return await _render_with_flash("InfoItem is required")

    interval_raw = default_schedule_interval.strip()
    if interval_raw:
        try:
            parse_interval(interval_raw)
        except ValueError as exc:
            return await _render_with_flash(str(exc))

    tags = [t.strip() for t in default_tags.split(",") if t.strip()] or None

    info_client = get_registry().get_archiver_client()
    try:
        info_item = await info_client.get_info_item(iid)
    except NotFound:
        return await _render_with_flash(f"InfoItem {iid} does not exist")
    except AuthError:
        return await _render_with_flash("Information service auth failed")
    except (ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
        return await _render_with_flash(f"Information service unavailable: {exc}")

    wi = WatchedItem(
        info_item_id=ULID.from_str(iid),
        name=(name.strip() or info_item.name),
        description=description.strip() or None,
        default_schedule_config={"interval": interval_raw} if interval_raw else None,
        default_content_type=(default_content_type.strip() or None),
        default_tags=tags,
    )
    session.add(wi)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return await _render_with_flash(
            f"WatchedItem for InfoItem {iid} already exists"
        )
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        info_item_id=iid,
        name=wi.name,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url=f"/watched-items/{wi.id}", status_code=303)
```

Add the necessary top-of-file imports: `WatchedItem`, `ULID`, `IntegrityError`, and `parse_interval` (already imported via `src.core.scheduler`).

- [ ] **Step 6: Verify pass**

Run: `uv run pytest tests/dashboard/test_watched_item_create.py -v -m integration`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/watched_item_form.html src/dashboard/templates/pages/watched_items.html tests/dashboard/test_watched_item_create.py
git commit -m "#162 feat: standalone /watched-items/new create flow"
```

---

## SLICE 6 — Detail page reuses the binding tree

### Task 11: Swap `watched_item_info_item_card.html` for the picker partial in readonly mode

**Files:**
- Modify: `src/dashboard/routes.py:869-951` (`watched_item_detail_page`)
- Modify: `src/dashboard/templates/pages/watched_item_detail.html:19`
- Delete: `src/dashboard/templates/partials/watched_item_info_item_card.html`
- Modify: `tests/dashboard/test_watched_item_routes.py:78-211` (`TestDetailPage`)

- [ ] **Step 1: Update / add tests**

Edit `tests/dashboard/test_watched_item_routes.py::TestDetailPage` — the following replacements:

```python
    async def test_renders_info_item_summary(self, client, db_session, info_client):
        # Detail page now renders the shared binding-tree partial.
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Summary Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=_fake_info_item_out(info_item_id=str(item.info_item_id))
        )
        info_client.get_info_source = AsyncMock(
            return_value=_fake_info_source_out(url="https://example.org/foo")
        )
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        # Primary URL still surfaces.
        assert b"https://example.org/foo" in body
        # Readonly mode: no radio inputs in the tree.
        assert b'type="radio"' not in body

    async def test_renders_when_get_info_item_fails(
        self, client, db_session, info_client, exc_factory
    ):
        # Updated message — the placeholder is now rendered by the picker
        # partial itself (binding-tree route's 404 doesn't apply here; the
        # detail page handles SDK failure and renders an inline notice).
        ...
        assert b"InfoItem summary unavailable" in response.content
```

Add an assertion to the existing sub_aspect-banner test that the binding-tree shows the `new` badge on a fresh sub_aspect:

```python
    async def test_new_sub_aspects_get_badge(self, client, db_session, info_client):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="WI",
            last_reviewed_at=datetime.now(UTC) - timedelta(days=7),
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        info_client.get_info_item = AsyncMock(return_value=SimpleNamespace(
            info_item_id=str(item.info_item_id), name="N", description=None,
            owner=None,
            info_item_sources=[
                SimpleNamespace(info_source_id="primary",
                                role=None, created_at=datetime.now(UTC) - timedelta(days=14)),
                SimpleNamespace(info_source_id="new-sub",
                                role="sub_aspect", created_at=datetime.now(UTC)),
            ],
        ))
        info_client.get_info_source = AsyncMock(
            return_value=_fake_info_source_out(url="https://example.com")
        )
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        assert b"new-sub" in body
        # The "new" badge fires because created_at > last_reviewed_at.
        assert b"badge-warning" in body
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestDetailPage -v -m integration`
Expected: at least the new test FAILs; existing tests may FAIL because the partial path changes.

- [ ] **Step 3: Update the detail route to partition bindings**

Modify `watched_item_detail_page`:

```python
    # Replace the existing single-call get_info_item + manual primary_url
    # block with fetch_info_item_bindings, which already partitions roles.
    from src.core.watches.info_item_fetch import fetch_info_item_bindings

    client_sdk = get_registry().get_archiver_client()
    info_item = None
    primary_url: str | None = None
    cross_checks: list = []
    sub_aspects: list = []
    new_subaspect_ids: set[str] = set()
    try:
        info_item = await client_sdk.get_info_item(str(wi.info_item_id))
        bindings = await fetch_info_item_bindings(client_sdk, str(wi.info_item_id))
        primary_url = bindings.primary_url
        cross_checks = bindings.cross_checks
        sub_aspects = bindings.sub_aspects
        # Build the "new" set from the raw bindings list (it has .role +
        # .created_at), keyed by info_source_id. The partial reads by id, so
        # ordering between bindings.sub_aspects and info_item.info_item_sources
        # never has to line up.
        raw_subaspects = [
            b for b in (info_item.info_item_sources or []) if b.role == "sub_aspect"
        ]
        if wi.last_reviewed_at is None:
            new_subaspect_ids = {str(b.info_source_id) for b in raw_subaspects}
        else:
            new_subaspect_ids = {
                str(b.info_source_id)
                for b in raw_subaspects
                if b.created_at > wi.last_reviewed_at
            }
    except NotFound:
        info_item = None
    except (httpx.ConnectError, ServerError, AuthError):
        logger.warning(
            "Archiver unavailable while rendering watched_item detail",
            extra={"watched_item_id": str(wi.id)},
        )
        info_item = None
```

Pass new keys to the template:

```python
    return templates.TemplateResponse(
        request,
        "pages/watched_item_detail.html",
        {
            ...
            "info_item": info_item,
            "primary_url": primary_url,
            "cross_checks": cross_checks,
            "sub_aspects": sub_aspects,
            "new_subaspect_ids": new_subaspect_ids,
            ...
        },
    )
```

(The `count_new_subaspects` import / call stays — it powers the banner above the binding tree.)

- [ ] **Step 4: Update the page template**

Replace the `{% include "partials/watched_item_info_item_card.html" %}` line in `pages/watched_item_detail.html` with:

```jinja
{% if info_item is none %}
<div class="stat-card mb-6 text-sm text-gray-500">Archiver InfoItem summary unavailable.</div>
{% else %}
<div class="mb-6">
  {% with mode="readonly_tree" %}
  {% include "partials/info_item_picker/binding_tree.html" %}
  {% endwith %}
</div>
{% endif %}
```

- [ ] **Step 5: Delete the orphaned partial**

```bash
git rm src/dashboard/templates/partials/watched_item_info_item_card.html
```

- [ ] **Step 6: Verify pass**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestDetailPage -v -m integration`
Expected: all PASS, including the new badge test.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/watched_item_detail.html tests/dashboard/test_watched_item_routes.py
git commit -m "#162 refactor: detail page reuses picker binding-tree partial"
```

---

## SLICE 7 — Final verification + housekeeping

### Task 12: Update the #161 plan note

**Files:**
- Modify: `docs/plans/2026-05-17-watched-item-crud-ui-plan.md:3312`

- [ ] **Step 1: Update the OOS line**

Edit line 3312:

```markdown
- Standalone `POST /api/v1/watched-items` create endpoint. **— Superseded by [2026-05-18 plan](2026-05-18-info-item-picker-and-watched-item-create.md); in scope as part of #162's expanded scope.**
```

- [ ] **Step 2: Commit**

```bash
git add docs/plans/2026-05-17-watched-item-crud-ui-plan.md
git commit -m "#162 docs: mark standalone create endpoint as in-scope under #162"
```

---

### Task 13: Lift inline imports + lint

- [ ] **Step 1: Sweep new inline imports**

Run:

```bash
grep -n "^    from src\|^    import" src/dashboard/routes.py src/api/routes/watched_items.py src/core/watches/__init__.py
```

Expected: only imports inside conftest/test fixtures, never in `src/`. Promote any to file top per `AGENTS.md`.

- [ ] **Step 2: Lint**

```bash
uv run ruff check .
```

Expected: pass.

- [ ] **Step 3: Commit lint cleanup (only if anything changed)**

```bash
git add -u
git commit -m "#162 chore: lift inline imports + ruff cleanup"
```

---

### Task 14: Full sweep + manual smoke

- [ ] **Step 1: Full test sweep**

```bash
uv run pytest -m integration
```

Expected: green. Pay attention to any regression in:
- `tests/dashboard/test_routes.py::TestWatchCreate`
- `tests/dashboard/test_watched_item_routes.py::TestDetailPage`
- `tests/api/test_watched_items.py`
- `tests/test_create_watch_service.py`

- [ ] **Step 2: Manual smoke on the dev server**

```bash
bash scripts/build-css.sh
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

Open `https://watcher.exe.xyz:8001/`:

1. `/watched-items/new` — search, select, fill defaults, submit, lands on detail page.
2. `/watches/new` — search, select InfoItem, pick primary (then sub_aspect), submit, lands on watch detail.
3. `/watched-items/{id}` detail — confirm the binding tree renders with primary URL, sub_aspect rows muted, no radios.
4. Keyboard nav on the typeahead: ↓/↑ moves highlight, Enter selects, Esc clears.
5. Dark-mode toggle: no contrast regressions on the picker.

- [ ] **Step 3: Push & open PR**

Match the project's "merge to main" guidance:

```bash
git push origin <branch>
gh pr create --title "#162 feat: InfoItem typeahead picker + standalone WatchedItem create" --body "..."
```

Or merge to `main` locally per `feedback_merge_to_main` user preference; the user picks at handoff time.

- [ ] **Step 4: Update #162 description**

Re-paste the expanded scope (Watch-create + WatchedItem-create + detail-page reuse) into the issue body so the GitHub record matches what shipped.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Archiver `find_info_item` slows under load (typeahead-per-keystroke) | Low | `delay:250ms` on the input's HTMX trigger; `limit=20`. Archiver-side pg_trgm GIN is already in place. |
| Picker keyboard nav races with HTMX swaps | Low | `htmx:afterSwap` listener resets highlight; tested via the a11y assertions. Manual smoke step covers the live behavior. |
| Power-user paste-ULID fallback conflicts with picker submission | Medium | Submit handler prefers picker output; the fallback fields only fire when picker is empty. Documented in the `<details>` hint copy. |
| Duplicate-409 surface differs between API and dashboard | Low | Both raise on `IntegrityError`; tests cover both surfaces. |
| Sub_aspect "new" badge mis-aligns when bindings list is reordered | Low | The route builds the badge set from the InfoItemSourceOut list (preserves SDK ordering); the partial reads by id, not index. |
| Auto-create audit row breaks audit-log consumers | Low | Audit rows are append-only; new event type is a strict additive change. Tests pin the source field so consumers can filter by `source IN ('auto_create','api','dashboard')`. |
| Vendor JS load order causes picker JS to run before HTMX is ready | Low | All `<script>` tags use `defer`; `info-item-picker.js` adds listeners on DOMContentLoaded-equivalent timing (immediate IIFE; HTMX swap listener is bound to `document.body`, which exists by `defer` time). |
| Sub_aspect "new" set drifts if Archiver SDK changes ordering | Low | Set is built by id-keyed lookup against `info_item.info_item_sources` (role + created_at), then the partial reads by id — no positional pairing. |

---

## Out of scope (do not implement in this plan)

- Editing a WatchedItem's `info_item_id` — identity is immutable; would require a new WatchedItem.
- Bulk "Add Watches for all new sub_aspects" — #164.
- A dedicated `GET /watched-items/{id}/edit` separate from the inline-edit detail page — current inline-edit surface is sufficient.
- Cross-InfoItem Collection grouping — descoped from #160.
- Reranking / fuzzy scoring in the picker beyond what `find_info_item` returns.
- Server-side caching of typeahead results.
