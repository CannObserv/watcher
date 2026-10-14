"""Tests for the read-only export Archiver's migration imports (#373).

The file carries notifier channel ids and channel hints out of a public repo's
service, so two properties outrank the shape: it is read-only by construction
(a ``READ ONLY`` transaction, not a convention), and its contents reach only the
file — never stdout, where they would land in a terminal or a transcript.
"""

import json
import os
import stat
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from src.core.models.domain import Domain
from src.core.models.notification_template import NotificationTemplate
from src.core.models.temporal_profile import TemporalProfile
from src.ops import export_for_archiver
from tests.conftest import TEST_DATABASE_URL, make_watched_item

PRODUCTION_URL = "postgresql+asyncpg://watcher_app:pw@localhost:5432/watcher"
DEV_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher_dev"
EXPORTED_AT = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SECRET_CHANNEL = "01JCHANNEL0000000000000000"


def _document() -> dict:
    return export_for_archiver.build_document(
        {
            "items": {},
            "domains": {},
            "notification_templates": [{"id": "x", "remote_channel_id": SECRET_CHANNEL}],
        },
        exported_at=EXPORTED_AT,
    )


# --- main: gating ---------------------------------------------------------


def test_an_entry_point_configures_logging(monkeypatch, tmp_path) -> None:
    configure = MagicMock()
    monkeypatch.setattr(export_for_archiver, "configure_logging", configure)
    export_for_archiver.main(["--output", str(tmp_path / "x.json")], environ={})
    configure.assert_called_once_with()


def test_refuses_when_no_database_is_configured(tmp_path, capsys) -> None:
    code = export_for_archiver.main(["--output", str(tmp_path / "x.json")], environ={})
    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_refuses_a_production_database_without_the_opt_in(monkeypatch, tmp_path, capsys) -> None:
    collect = AsyncMock()
    monkeypatch.setattr(export_for_archiver, "_export", collect)
    code = export_for_archiver.main(
        ["--output", str(tmp_path / "x.json")], environ={"DATABASE_URL": PRODUCTION_URL}
    )
    assert code == 2
    assert "WATCHER_ALLOW_PRODUCTION_DB" in capsys.readouterr().err
    collect.assert_not_awaited()
    assert not (tmp_path / "x.json").exists()


def test_production_is_read_with_the_opt_in(monkeypatch, tmp_path) -> None:
    collect = AsyncMock(return_value=_document())
    monkeypatch.setattr(export_for_archiver, "_export", collect)
    environ = {"DATABASE_URL": PRODUCTION_URL, "WATCHER_ALLOW_PRODUCTION_DB": "1"}
    assert export_for_archiver.main(["--output", str(tmp_path / "x.json")], environ=environ) == 0
    assert collect.await_args.args[0] == PRODUCTION_URL


def test_output_is_required(capsys) -> None:
    """There is no stdout mode: the contents must never reach a terminal."""
    with pytest.raises(SystemExit) as exc:
        export_for_archiver.main([], environ={"DATABASE_URL": DEV_URL})
    assert exc.value.code == 2
    assert "--output" in capsys.readouterr().err


# --- main: the file, and only the file, holds the contents ----------------


def test_writes_the_document_and_prints_only_a_summary(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(export_for_archiver, "_export", AsyncMock(return_value=_document()))
    out = tmp_path / "export.json"
    assert export_for_archiver.main(["--output", str(out)], environ={"DATABASE_URL": DEV_URL}) == 0

    body = out.read_text()
    assert json.loads(body) == _document()
    printed = capsys.readouterr()
    assert SECRET_CHANNEL not in printed.out + printed.err
    assert "notification_templates: 1" in printed.out
    assert export_for_archiver.sha256_hex(body) in printed.out


def test_the_file_is_owner_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(export_for_archiver, "_export", AsyncMock(return_value=_document()))
    out = tmp_path / "export.json"
    export_for_archiver.main(["--output", str(out)], environ={"DATABASE_URL": DEV_URL})
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_rerun_over_a_readable_file_makes_it_owner_only(tmp_path) -> None:
    """A re-run onto a file an operator loosened must tighten it, not inherit it."""
    out = tmp_path / "export.json"
    out.write_text("old, longer than the new contents")
    os.chmod(out, 0o644)
    export_for_archiver.write_private(out, "new\n")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text() == "new\n"


def test_a_failed_rerun_keeps_the_previous_export(tmp_path) -> None:
    """The previous file is the one that may be carried: a write that fails part
    way must leave it whole, and leave no partial file beside it (CR 2)."""
    out = tmp_path / "export.json"
    out.write_text("previous export\n")
    with pytest.raises(UnicodeEncodeError):
        export_for_archiver.write_private(out, "partial \ud800 unencodable")
    assert out.read_text() == "previous export\n"
    assert [p.name for p in tmp_path.iterdir()] == ["export.json"]


def test_a_symlinked_output_is_replaced_not_written_through(tmp_path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("not the export\n")
    out = tmp_path / "export.json"
    out.symlink_to(target)
    export_for_archiver.write_private(out, "new\n")
    assert not out.is_symlink()
    assert out.read_text() == "new\n"
    assert target.read_text() == "not the export\n"


# --- the document ---------------------------------------------------------


def test_render_is_deterministic_and_sorted() -> None:
    doc = export_for_archiver.build_document(
        {"items": {"b": {"z": 1, "a": 2}, "a": {}}, "domains": {}, "notification_templates": []},
        exported_at=EXPORTED_AT,
    )
    rendered = export_for_archiver.render(doc)
    assert rendered == export_for_archiver.render(json.loads(rendered))
    assert rendered.endswith("}\n")
    assert rendered.index('"a": {}') < rendered.index('"b": {')
    assert rendered.index('"a": 2') < rendered.index('"z": 1')


def test_the_document_carries_its_provenance() -> None:
    doc = _document()
    assert doc["schema_version"] == export_for_archiver.SCHEMA_VERSION
    assert doc["source"] == "watcher"
    assert doc["exported_at"] == "2026-10-14T12:00:00.000000Z"


# --- collect, against the test database -----------------------------------


@pytest.mark.integration
async def test_items_are_keyed_by_info_item_id(db_session) -> None:
    info_item_id = ULID()
    archived = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    await make_watched_item(
        db_session,
        archiver_info_item_id=info_item_id,
        name="Board agenda",
        description="Monthly",
        default_tags=["board", "agenda"],
        default_schedule_config={"interval": "6h"},
        announced_schedule_config={"interval": "1d"},
        throttle_floor_interval="12h",
        archived_at=archived,
    )

    data = await export_for_archiver.collect(db_session)

    assert data["items"][str(info_item_id)] == {
        "name": "Board agenda",
        "description": "Monthly",
        "default_tags": ["board", "agenda"],
        "default_schedule_config": {"interval": "6h"},
        "announced_schedule_config": {"interval": "1d"},
        "throttle_floor_interval": "12h",
        "archived_at": "2026-09-01T08:30:00.000000Z",
        "temporal_profile": None,
    }


@pytest.mark.integration
async def test_an_items_temporal_profile_travels_without_watcher_ids(db_session) -> None:
    info_item_id = ULID()
    wi = await make_watched_item(db_session, archiver_info_item_id=info_item_id)
    db_session.add(
        TemporalProfile(
            watched_item_id=wi.id,
            profile_type="deadline",
            reference_date=date(2026, 11, 3),
            rules=[{"days_before": 7, "interval": "1h"}],
            post_action="reduce_frequency",
        )
    )
    await db_session.flush()

    data = await export_for_archiver.collect(db_session)

    assert data["items"][str(info_item_id)]["temporal_profile"] == {
        "profile_type": "deadline",
        "reference_date": "2026-11-03",
        "date_range_start": None,
        "date_range_end": None,
        "rules": [{"days_before": 7, "interval": "1h"}],
        "post_action": "reduce_frequency",
        "is_active": True,
    }


@pytest.mark.integration
async def test_domains_are_keyed_by_name(db_session) -> None:
    name = f"{ULID()}.example".lower()
    db_session.add(
        Domain(
            name=name,
            min_interval=2.5,
            default_schedule_config={"interval": "1d"},
            notes="slow host",
            is_active=False,
        )
    )
    await db_session.flush()

    data = await export_for_archiver.collect(db_session)

    assert data["domains"][name] == {
        "min_interval": 2.5,
        "default_schedule_config": {"interval": "1d"},
        "notes": "slow host",
        "is_active": False,
        "archived_at": None,
    }


@pytest.mark.integration
async def test_an_item_scoped_template_names_the_info_item(db_session) -> None:
    """``watched_item_id`` is Watcher's own ULID, which Archiver has never seen:
    the file names the InfoItem instead."""
    info_item_id = ULID()
    wi = await make_watched_item(db_session, archiver_info_item_id=info_item_id)
    tpl = NotificationTemplate(
        title="Item alerts",
        channel_hint="slack",
        events=["change_detected"],
        visibility="watched_item",
        watched_item_id=wi.id,
        is_active=True,
        content_config={"default": {"include_tags": True}},
        remote_channel_id=SECRET_CHANNEL,
    )
    db_session.add(tpl)
    await db_session.flush()

    data = await export_for_archiver.collect(db_session)

    (row,) = [t for t in data["notification_templates"] if t["id"] == str(tpl.id)]
    assert row == {
        "id": str(tpl.id),
        "title": "Item alerts",
        "channel_hint": "slack",
        "events": ["change_detected"],
        "visibility": "watched_item",
        "domain_name": None,
        "archiver_info_item_id": str(info_item_id),
        "is_active": True,
        "content_config": {"default": {"include_tags": True}},
        "remote_channel_id": SECRET_CHANNEL,
    }


@pytest.mark.integration
async def test_templates_are_ordered_by_id(db_session) -> None:
    for title in ("b", "a", "c"):
        db_session.add(
            NotificationTemplate(title=title, channel_hint="mailto", visibility="global")
        )
        await db_session.flush()

    data = await export_for_archiver.collect(db_session)

    ids = [t["id"] for t in data["notification_templates"]]
    assert ids == sorted(ids)
    assert len(ids) >= 3


@pytest.mark.integration
async def test_the_export_transaction_refuses_writes(test_engine) -> None:
    """Read-only by construction: whatever the collector did, a write in its
    transaction is refused by Postgres, and all three tables share one snapshot."""
    async with export_for_archiver.read_only_session(TEST_DATABASE_URL) as session:
        assert (await session.execute(text("SHOW transaction_read_only"))).scalar() == "on"
        isolation = (await session.execute(text("SHOW transaction_isolation"))).scalar()
        assert isolation == "repeatable read"
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await session.execute(text("CREATE TEMP TABLE t373 (x int)"))


@pytest.mark.integration
async def test_exported_at_names_the_snapshot(monkeypatch, test_engine) -> None:
    """The stamp is the read transaction's ``now()``, the moment the data is as
    of — not the app clock after the read finished (CR 3)."""
    seen = []

    async def collect(session):
        seen.append((await session.execute(text("SELECT now()"))).scalar_one())
        return {"items": {}, "domains": {}, "notification_templates": []}

    monkeypatch.setattr(export_for_archiver, "collect", collect)
    document = await export_for_archiver._export(TEST_DATABASE_URL)
    assert document["exported_at"] == seen[0].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
