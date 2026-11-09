"""Tests for the inert item-cadence writer (#375).

``set_item_schedule_config`` is the API PATCH's path since #377 deleted the
dashboard's string-shaped front door; it goes with ``/api/v1`` (#378) and the
columns with #380.
"""

from ulid import ULID

from src.core.models.watched_item import WatchedItem
from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG, resolved_schedule_config
from src.core.watched_items import set_item_schedule_config


def _item(**overrides) -> WatchedItem:
    return WatchedItem(
        archiver_info_source_id=str(ULID()), archiver_info_item_id=ULID(), name="x", **overrides
    )


class TestThrottleFloorWriterIsInert:
    """The operator's item-cadence edit still clears the floor (#254 CR-1). The
    writer stays until #378 deletes the API; since #375 neither column moves
    the resolved cadence."""

    def test_an_explicit_item_config_still_clears_the_column(self):
        wi = _item(throttle_floor_interval="1d")

        set_item_schedule_config(wi, {"interval": "30m"})

        assert wi.throttle_floor_interval is None
        assert wi.default_schedule_config == {"interval": "30m"}
        assert resolved_schedule_config(wi) == SYSTEM_DEFAULT_SCHEDULE_CONFIG

    def test_clearing_the_item_config_still_clears_the_column(self):
        wi = _item(throttle_floor_interval="1d", default_schedule_config={"interval": "15m"})

        set_item_schedule_config(wi, None)

        assert wi.throttle_floor_interval is None
        assert wi.default_schedule_config is None
