"""Tests for the break-glass host suspension CLI (#372).

It is the one operator control Watcher keeps (charter rule 2), and it exists to
work when Archiver is down: no bus, no worker, no dashboard on its import path —
only the database, behind the production opt-in every ``src.ops`` command takes.
"""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.watched_item import WatchedItem
from src.ops import suspend_domain
from tests.conftest import TEST_DATABASE_URL, make_watched_item

PRODUCTION_URL = "postgresql+asyncpg://watcher_app:pw@localhost:5432/watcher"
DEV_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher_dev"

#: Module prefixes the CLI must never load: a bus producer or worker (#262),
#: the dashboard #377 deletes, the API.
FORBIDDEN_PREFIXES = (
    "src.workers",
    "src.dashboard",
    "src.api",
    "src.core.bus",
    "src.core.fetch_policy",
    "redis",
    "co_core_aio",
    "procrastinate",
)


def test_imports_nothing_that_reaches_the_broker_or_the_dashboard() -> None:
    """In a fresh interpreter: this process has long since imported all of them."""
    probe = "import json, sys, src.ops.suspend_domain; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    loaded = json.loads(out)
    leaked = [m for m in loaded if m.startswith(FORBIDDEN_PREFIXES)]
    assert leaked == []


# --- main: gating and arguments --------------------------------------------


@pytest.fixture
def run(monkeypatch) -> AsyncMock:
    fake = AsyncMock(return_value=suspend_domain.Outcome(0, "ok"))
    monkeypatch.setattr(suspend_domain, "_run", fake)
    monkeypatch.setattr(suspend_domain, "configure_logging", MagicMock())
    return fake


def test_an_entry_point_configures_logging(monkeypatch) -> None:
    configure = MagicMock()
    monkeypatch.setattr(suspend_domain, "configure_logging", configure)
    suspend_domain.main(["example.com"], environ={})
    configure.assert_called_once_with()


def test_refuses_when_no_database_is_configured(run, capsys) -> None:
    assert suspend_domain.main(["example.com"], environ={}) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    run.assert_not_awaited()


def test_refuses_a_production_database_without_the_opt_in(run, capsys) -> None:
    code = suspend_domain.main(["example.com"], environ={"DATABASE_URL": PRODUCTION_URL})
    assert code == 2
    assert "WATCHER_ALLOW_PRODUCTION_DB" in capsys.readouterr().err
    run.assert_not_awaited()


def test_production_is_written_with_the_opt_in(run) -> None:
    environ = {"DATABASE_URL": PRODUCTION_URL, "WATCHER_ALLOW_PRODUCTION_DB": "1"}
    assert suspend_domain.main(["example.com"], environ=environ) == 0
    run.assert_awaited_once_with(PRODUCTION_URL, "example.com", resume=False)


def test_resume_is_a_flag(run) -> None:
    suspend_domain.main(["example.com", "--resume"], environ={"DATABASE_URL": DEV_URL})
    run.assert_awaited_once_with(DEV_URL, "example.com", resume=True)


def test_the_host_is_lowercased_as_domain_names_are_stored(run) -> None:
    """``Domain.name`` is ``urlparse().hostname``, which lowercases."""
    suspend_domain.main(["LCB.WA.Gov"], environ={"DATABASE_URL": DEV_URL})
    assert run.await_args.args[1] == "lcb.wa.gov"


@pytest.mark.parametrize("arg", ["https://lcb.wa.gov/notices", "lcb.wa.gov/notices", ""])
def test_a_url_or_empty_host_is_a_usage_error(arg, run, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        suspend_domain.main([arg], environ={"DATABASE_URL": DEV_URL})
    assert exc.value.code == 2
    assert "host" in capsys.readouterr().err
    run.assert_not_awaited()


@pytest.mark.parametrize(("code", "stream"), [(0, "out"), (1, "err")])
def test_the_outcome_is_printed_and_returned(code, stream, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        suspend_domain, "_run", AsyncMock(return_value=suspend_domain.Outcome(code, "said"))
    )
    monkeypatch.setattr(suspend_domain, "configure_logging", MagicMock())
    assert suspend_domain.main(["example.com"], environ={"DATABASE_URL": DEV_URL}) == code
    assert getattr(capsys.readouterr(), stream) == "said\n"


@pytest.mark.parametrize(
    "error",
    [
        OSError("[Errno 111] Connect call failed ('127.0.0.1', 5432)"),
        TimeoutError(),  # CR 5: asyncpg's connect timeout carries no message
        ProgrammingError("SELECT 1", {}, Exception('relation "domains" does not exist')),
    ],
)
def test_a_database_failure_exits_2_in_one_line(error, monkeypatch, capsys) -> None:
    """CR 2: exit 1 means the host was refused. A database the command could
    not use is "never ran" — 2, one line, no traceback."""
    monkeypatch.setattr(suspend_domain, "_run", AsyncMock(side_effect=error))
    monkeypatch.setattr(suspend_domain, "configure_logging", MagicMock())
    assert suspend_domain.main(["example.com"], environ={"DATABASE_URL": DEV_URL}) == 2
    err = capsys.readouterr().err
    assert err.startswith("could not use the database")
    assert err.count("\n") == 1


# --- apply: against the database -------------------------------------------


async def _audits(session, name: str) -> list[AuditLog]:
    rows = (await session.execute(select(AuditLog))).scalars().all()
    return [r for r in rows if r.payload.get("domain_name") == name]


@pytest.mark.integration
async def test_suspends_a_host_and_audits_it_as_the_cli(db_session) -> None:
    wi = await make_watched_item(
        db_session, primary_url="https://cli-s.example/p", domain_name="cli-s.example"
    )

    outcome = await suspend_domain.apply(db_session, "cli-s.example", resume=False)
    await db_session.flush()

    await db_session.refresh(wi)
    assert outcome.code == 0
    assert "suspended cli-s.example" in outcome.message
    assert "1 item" in outcome.message
    assert wi.domain_suspended is True
    [entry] = await _audits(db_session, "cli-s.example")
    assert entry.event_type == EventType.DOMAIN_DEACTIVATED
    assert entry.payload["source"] == "cli"


@pytest.mark.integration
async def test_resumes_a_host(db_session) -> None:
    db_session.add(Domain(name="cli-r.example", is_active=False))
    await db_session.flush()

    outcome = await suspend_domain.apply(db_session, "cli-r.example", resume=True)
    await db_session.flush()

    assert outcome.code == 0
    assert "resumed cli-r.example" in outcome.message
    [entry] = await _audits(db_session, "cli-r.example")
    assert entry.event_type == EventType.DOMAIN_ACTIVATED


@pytest.mark.integration
@pytest.mark.parametrize(
    ("is_active", "resume", "says"),
    [(False, False, "already suspended"), (True, True, "not suspended")],
)
async def test_the_current_state_is_a_no_op_that_says_so(
    db_session, is_active, resume, says
) -> None:
    name = f"cli-noop-{resume}.example".lower()
    db_session.add(Domain(name=name, is_active=is_active))
    await db_session.flush()

    outcome = await suspend_domain.apply(db_session, name, resume=resume)
    await db_session.flush()

    assert outcome.code == 0
    assert says in outcome.message
    assert await _audits(db_session, name) == []


@pytest.mark.integration
async def test_an_already_suspended_host_reports_items_it_re_flagged(db_session) -> None:
    db_session.add(Domain(name="cli-drift.example", is_active=False))
    await make_watched_item(
        db_session,
        primary_url="https://cli-drift.example/p",
        domain_name="cli-drift.example",
        domain_suspended=False,
    )

    outcome = await suspend_domain.apply(db_session, "cli-drift.example", resume=False)

    assert "already suspended" in outcome.message
    assert "re-flagged 1 item" in outcome.message


@pytest.mark.integration
async def test_an_unknown_host_is_refused(db_session) -> None:
    outcome = await suspend_domain.apply(db_session, "cli-absent.example", resume=False)
    assert outcome.code == 1
    assert "cli-absent.example" in outcome.message


@pytest.mark.integration
@pytest.mark.parametrize("resume", [False, True])
async def test_an_archived_host_is_refused(db_session, resume) -> None:
    name = f"cli-arch-{resume}.example".lower()
    db_session.add(Domain(name=name, archived_at=datetime.now(UTC)))
    await db_session.flush()

    outcome = await suspend_domain.apply(db_session, name, resume=resume)

    assert outcome.code == 1
    assert "archived" in outcome.message
    assert await _audits(db_session, name) == []


@pytest.mark.integration
async def test_the_archived_refusal_does_not_claim_the_host_is_stopped(db_session) -> None:
    """CR 1: archiving revokes the fetch policy but back-fills no item, so a live
    item on an archived host is still scheduled. The refusal must say how to
    stop it, not that it is already stopped."""
    db_session.add(Domain(name="cli-arch-msg.example", archived_at=datetime.now(UTC)))
    await db_session.flush()

    outcome = await suspend_domain.apply(db_session, "cli-arch-msg.example", resume=False)

    assert "already stops" not in outcome.message
    assert "restore" in outcome.message


# --- main: end to end ---------------------------------------------------------

E2E_HOST = "cli-e2e.example"


async def _seed_e2e() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with AsyncSession(engine) as session:
            await make_watched_item(
                session, primary_url=f"https://{E2E_HOST}/p", domain_name=E2E_HOST
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _state_and_cleanup() -> tuple[bool, bool, list[str]]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with AsyncSession(engine) as session:
            domain = (
                await session.execute(select(Domain).where(Domain.name == E2E_HOST))
            ).scalar_one()
            wi = (
                await session.execute(
                    select(WatchedItem).where(WatchedItem.domain_name == E2E_HOST)
                )
            ).scalar_one()
            audits = [a.payload["source"] for a in await _audits(session, E2E_HOST)]
            state = (domain.is_active, wi.domain_suspended, audits)
            await session.execute(
                delete(AuditLog).where(AuditLog.payload["domain_name"].astext == E2E_HOST)
            )
            await session.execute(delete(WatchedItem).where(WatchedItem.domain_name == E2E_HOST))
            await session.execute(delete(Domain).where(Domain.name == E2E_HOST))
            await session.commit()
            return state
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_main_commits_the_suspension(monkeypatch, capsys) -> None:
    """The one test that crosses the commit: ``apply`` alone never does."""
    monkeypatch.setattr(suspend_domain, "configure_logging", MagicMock())
    asyncio.run(_seed_e2e())
    try:
        code = suspend_domain.main([E2E_HOST], environ={"DATABASE_URL": TEST_DATABASE_URL})
    finally:
        is_active, suspended, audits = asyncio.run(_state_and_cleanup())

    assert code == 0
    assert f"suspended {E2E_HOST}" in capsys.readouterr().out
    assert (is_active, suspended, audits) == (False, True, ["cli"])
