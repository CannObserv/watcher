"""Tests for resolve_watch_target — the probe-free operator URL edit (#241, #251) —
and the inert item-cadence writer (#375).

Nothing touches the network: the item re-enters PROBING with the URL as
submitted and the first fact resolves it. Its one caller since #251 is the
dashboard's ``POST /watched-items/{id}/effective-url`` — creates take the URL
from Archiver and start UNKNOWN (route-level coverage lives in
tests/dashboard/test_watched_item_routes.py::TestWatchedItemUrlReprobe).
"""

import pytest
from ulid import ULID

from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config
from src.core.watched_items import resolve_watch_target, set_item_schedule_interval


class TestResolveWatchTarget:
    def test_defers_the_probe_to_the_first_fetch(self):
        effective_url, domain, health = resolve_watch_target("https://LCB.wa.gov/Notices")

        assert effective_url == "https://LCB.wa.gov/Notices"  # submitted URL, untouched
        assert domain == "lcb.wa.gov"  # urlparse().hostname — the domain-key derivation
        assert health == WatchHealthStatus.PROBING


class TestUrlValidation:
    """CR-3: syntactic validation stays at the boundary."""

    def test_rejects_a_schemeless_url(self):
        with pytest.raises(ValueError, match="invalid URL"):
            resolve_watch_target("not a url")

    def test_rejects_a_non_http_scheme(self):
        with pytest.raises(ValueError, match="invalid URL"):
            resolve_watch_target("ftp://old.example/file")

    def test_rejects_a_hostless_url(self):
        with pytest.raises(ValueError, match="invalid URL"):
            resolve_watch_target("https:///nopath-host")


def _item(**overrides) -> WatchedItem:
    return WatchedItem(
        archiver_info_source_id=str(ULID()), archiver_info_item_id=ULID(), name="x", **overrides
    )


class TestThrottleFloorWriterIsInert:
    """The operator's item-interval edit still clears the floor (#254 CR-1). The
    writer stays until #377/#378 delete the surface; since #375 neither column
    moves the resolved cadence."""

    def test_an_explicit_item_interval_still_clears_the_column(self):
        wi = _item(throttle_floor_interval="1d")

        set_item_schedule_interval(wi, "30m")

        assert wi.throttle_floor_interval is None
        assert wi.default_schedule_config == {"interval": "30m"}
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_clearing_the_item_interval_still_clears_the_column(self):
        wi = _item(throttle_floor_interval="1d", default_schedule_config={"interval": "15m"})

        set_item_schedule_interval(wi, None)

        assert wi.throttle_floor_interval is None
        assert wi.default_schedule_config is None
