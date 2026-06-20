"""Unit + integration tests for src.core.domains (#196)."""

import pytest

from src.core.domains import domain_name_for_url, ensure_domain_and_resolve_suspension


class TestDomainNameForUrl:
    """Pure hostname derivation — no network, no DB."""

    def test_extracts_hostname(self):
        assert domain_name_for_url("https://lcb.wa.gov/laws/x") == "lcb.wa.gov"

    def test_strips_path_query_port(self):
        assert domain_name_for_url("https://example.com:8443/a/b?c=1") == "example.com"

    def test_lowercases_host(self):
        assert domain_name_for_url("https://Example.COM/path") == "example.com"

    def test_none_returns_none(self):
        assert domain_name_for_url(None) is None

    def test_empty_returns_none(self):
        assert domain_name_for_url("") is None

    def test_no_hostname_returns_none(self):
        assert domain_name_for_url("not-a-url") is None


@pytest.mark.integration
class TestDomainDefaultScheduleConfig:
    """#205: Domain.default_schedule_config — operator cadence, not the rate-limiter floor."""

    async def test_defaults_null_and_matches_is_null(self, db_session):
        from sqlalchemy import select

        from src.core.models.domain import Domain

        db_session.add(Domain(name="dom-null.example"))
        await db_session.flush()
        found = (
            await db_session.execute(
                select(Domain).where(
                    Domain.name == "dom-null.example",
                    Domain.default_schedule_config.is_(None),
                )
            )
        ).scalar_one()
        assert found.default_schedule_config is None

    async def test_round_trips(self, db_session):
        from sqlalchemy import select

        from src.core.models.domain import Domain

        db_session.add(
            Domain(name="dom-cadence.example", default_schedule_config={"interval": "6h"})
        )
        await db_session.flush()
        found = (
            await db_session.execute(select(Domain).where(Domain.name == "dom-cadence.example"))
        ).scalar_one()
        assert found.default_schedule_config == {"interval": "6h"}


@pytest.mark.integration
class TestEnsureDomainAndResolveSuspension:
    async def test_creates_missing_domain_and_returns_not_suspended(self, db_session):
        from sqlalchemy import select

        from src.core.models.domain import Domain

        suspended = await ensure_domain_and_resolve_suspension(db_session, "fresh-core.example")
        assert suspended is False
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "fresh-core.example"))
        ).scalar_one_or_none()
        assert domain is not None

    async def test_inactive_domain_returns_suspended(self, db_session):
        from src.core.models.domain import Domain

        db_session.add(Domain(name="inactive-core.example", is_active=False))
        await db_session.flush()
        suspended = await ensure_domain_and_resolve_suspension(db_session, "inactive-core.example")
        assert suspended is True

    async def test_active_domain_returns_not_suspended(self, db_session):
        from src.core.models.domain import Domain

        db_session.add(Domain(name="active-core.example", is_active=True))
        await db_session.flush()
        suspended = await ensure_domain_and_resolve_suspension(db_session, "active-core.example")
        assert suspended is False

    async def test_none_domain_returns_not_suspended(self, db_session):
        assert await ensure_domain_and_resolve_suspension(db_session, None) is False
