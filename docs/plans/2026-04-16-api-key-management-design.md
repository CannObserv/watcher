# API Key Management Design

**Date:** 2026-04-16
**Status:** Approved

## Goal

Enable programmatic (non-browser) R/W access to watcher from outside the VM via static API keys managed through the dashboard UI. Port the approach implemented in the sister project power-map (CannObserv/power-map#103) to watcher's SQLAlchemy + Alembic stack.

## Approved Approach

Make the exe.dev proxy public + application-layer `X-API-Key` auth on the existing `/api/v1/` router.

- `ssh exe.dev share set-public watcher` exposes port 8000 to the internet.
- Dashboard routes gain an exe.dev header guard (`get_dashboard_user` dep) so the UI stays protected after the port is made public.
- `require_api_key` dependency is added to the existing `v1_router` in `main.py`, gating all `/api/v1/` routes uniformly.
- API keys are managed in the dashboard under Settings → API Keys (new nav item after Notifications).

## Data Model

### `app_users`

Identity anchor keyed by exe.dev user ID. One row per user, created or email-updated lazily on every dashboard access via upsert in `get_dashboard_user`.

SQLAlchemy model in `src/core/models/app_user.py`:

```python
class AppUser(Base):
    __tablename__ = "app_users"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # X-ExeDev-UserID
    email: Mapped[str] = mapped_column(String, nullable=False)  # updated each login
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())
```

### `api_keys`

```python
class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # ULID
    user_id: Mapped[str] = mapped_column(ForeignKey("app_users.id"))
    label: Mapped[str] = mapped_column(String, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)    # first 8 chars, for display
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # SHA-256 hex
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

Single Alembic migration for both tables. No `archived_at` — hard delete only (keys are ephemeral credentials).

## Key Mechanics

- **Format:** `co_` + 32 random hex chars (128 bits via `os.urandom`). Example: `co_a3f8c2d1...`
- **Storage:** SHA-256 hex hash of the full raw key. Random keys have no dictionary attack surface; SHA-256 is appropriate (same as GitHub PATs, Stripe).
- **Prefix:** first 8 chars of the raw key stored in `key_prefix` for display (e.g. `co_a3f8c2`).
- **Show once:** full raw key returned in the create response only, displayed in a one-time modal with a copy button. Never retrievable after dismissal.

## Dashboard Auth Guard

New `src/dashboard/deps.py` with `get_dashboard_user()` dependency:

1. Reads `X-ExeDev-UserID` + `X-ExeDev-Email` from request headers
2. Raises `307 → /__exe.dev/login?redirect=<path>` if either is absent
3. Upserts into `app_users` (lazy provisioning)
4. Returns an `AppUser` dataclass

Applied to **all** dashboard routes so the dashboard stays protected once port 8000 is made public.

## API Key Auth

New `src/api/deps.py` with `require_api_key` dependency:

```python
async def require_api_key(
    raw_key: str | None = Depends(APIKeyHeader(name="X-API-Key", auto_error=False)),
    session: AsyncSession = Depends(get_session),
) -> str:
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    row = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash)
    )
    key = row.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key.last_used_at = datetime.utcnow()
    await session.commit()
    return key.user_id
```

Applied to `v1_router` in `src/api/main.py` as a router-level dependency. `/health` and `/ready` are root-level and unaffected.

## Settings UI

New `src/dashboard/settings.py` with routes under `/settings`, registered in the dashboard alongside existing route modules.

`/settings` — landing page (Settings nav item, after Notifications), API Keys card showing key count.

`/settings/api-keys` sub-routes (HTMX row-level edit, identical pattern to power-map):

| Route | Action |
|---|---|
| `GET /settings/api-keys` | Full page: key list (label, prefix, created, last used) + Generate button |
| `GET /settings/api-keys/new-row` | Inline blank form row |
| `POST /settings/api-keys` | Create key → one-time key modal |
| `GET /settings/api-keys/{id}/edit-row` | Inline label edit form |
| `POST /settings/api-keys/{id}/edit-row` | Save label → read row partial |
| `GET /settings/api-keys/{id}/read-row` | Read row (Cancel target) |
| `DELETE /settings/api-keys/{id}` | Hard delete → empty 200 + OOB flash |

Templates:
- `src/dashboard/templates/pages/settings.html` — settings landing page
- `src/dashboard/templates/pages/settings_api_keys.html` — full key list page
- `src/dashboard/templates/partials/api_key_row.html` — read row
- `src/dashboard/templates/partials/api_key_edit_row.html` — edit/new form row
- `src/dashboard/templates/partials/api_key_new_key_modal.html` — one-time key modal

## exe.dev Change

```bash
ssh exe.dev share set-public watcher
```

Makes port 8000 publicly accessible. Dashboard routes retain exe.dev header guard; unauthenticated browser users are redirected to `/__exe.dev/login`. API routes require `X-API-Key`.

## Key Decisions

- **Gate existing `/api/v1/`** rather than a new parallel router — the full API surface is immediately available externally without duplicating route registrations.
- **`X-API-Key` over `Authorization: Bearer`** — same security, simpler ergonomics for scripting; proven on exe.dev VMs.
- **SHA-256 over bcrypt** — appropriate for random high-entropy keys; faster auth on every API request.
- **Hard delete** — API keys are ephemeral credentials; show-once already treats them as disposable.
- **Lazy user provisioning** — no onboarding UI; row appears silently on first dashboard load after migration.
- **`co_` prefix** — Cannabis Observer brand prefix, mirrors `pm_` in power-map.

## Out of Scope

- Key expiry or automatic rotation
- Key scopes / permissions (all keys have full R/W access)
- Rate limiting on public API
- Multi-user support
