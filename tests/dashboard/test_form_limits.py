"""The dashboard's urlencoded forms are parsed under starlette's limits (#392).

Before starlette 1.3.1, ``request.form()`` ignored ``max_fields`` and
``max_part_size`` for ``application/x-www-form-urlencoded`` bodies
(PYSEC-2026-249): any body size, any field count, buffered into memory. Every
dashboard form is urlencoded, so the fix makes the defaults —
``max_fields=1000``, ``max_part_size=1 MiB`` — bind on all of them, both the
four routes that call ``request.form()`` and the ones whose ``Form()``
parameters FastAPI resolves through the same bare ``request.form()``.

What the limits must not do is reject a real form. The largest is the
notification form, about 41 fields (title, channel, hint, the event checkboxes,
the default card, the per-event override cards, ``preview_event``); its only
free text is the title and body templates. So the boundary sits far above any
legitimate submission. These tests pin it on one route of each kind: the field
count at 1000 and 1001, and a field whose value alone is 1 MiB (its name's
bytes put it over).
"""

from urllib.parse import urlencode

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from src.core.models import ApiKey

_MAX_FIELDS = 1000
_MAX_PART_SIZE = 1024 * 1024
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
# Counted by label, not table-wide: the test database is also the dev server's.
_LABEL = "form-limits-392"
_LABEL_COUNT = select(func.count()).select_from(ApiKey).where(ApiKey.label == _LABEL)


def _padded(fields: dict[str, str], total: int) -> str:
    """``fields`` plus throwaway ``padN=x`` pairs, ``total`` fields in all, encoded.

    Encoded by hand and sent as ``content=``: the count is the point, and an
    encoded body states it exactly.
    """
    pairs = list(fields.items()) + [(f"pad{i}", "x") for i in range(total - len(fields))]
    return urlencode(pairs)


@pytest.mark.integration
class TestRequestFormRoute:
    """``POST /notifications/preview`` reads ``await request.form()`` itself."""

    async def test_accepts_max_fields(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            content=_padded({"preview_event": "change_detected"}, _MAX_FIELDS),
            headers=_FORM,
        )
        assert resp.status_code == 200

    async def test_rejects_one_field_over(self, client: AsyncClient):
        resp = await client.post(
            "/notifications/preview",
            content=_padded({"preview_event": "change_detected"}, _MAX_FIELDS + 1),
            headers=_FORM,
        )
        assert resp.status_code == 400

    async def test_rejects_a_field_over_max_part_size(self, client: AsyncClient):
        # The limit counts the raw name and value bytes together: "x" encodes to
        # one byte, so the value is exactly 1 MiB and the name tips it over.
        resp = await client.post(
            "/notifications/preview",
            data={
                "preview_event": "change_detected",
                "content_config__body_template": "x" * _MAX_PART_SIZE,
            },
        )
        assert resp.status_code == 400


@pytest.mark.integration
class TestFormParameterRoute:
    """``POST /settings/api-keys`` takes ``label: str = Form(...)``."""

    async def test_accepts_max_fields(self, client: AsyncClient, db_session):
        resp = await client.post(
            "/settings/api-keys", content=_padded({"label": _LABEL}, _MAX_FIELDS), headers=_FORM
        )
        assert resp.status_code == 303
        assert await db_session.scalar(_LABEL_COUNT) == 1

    async def test_rejects_one_field_over(self, client: AsyncClient, db_session):
        resp = await client.post(
            "/settings/api-keys", content=_padded({"label": _LABEL}, _MAX_FIELDS + 1), headers=_FORM
        )
        assert resp.status_code == 400
        assert await db_session.scalar(_LABEL_COUNT) == 0
