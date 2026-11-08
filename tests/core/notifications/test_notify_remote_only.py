"""Regression guards on what ``src.core.notifications.notify`` no longer holds.

Phase 5 (#137) removed the local Apprise path: the notifier service is the
only delivery mechanism. F3 (#376) then removed Watcher's own dispatcher —
Archiver notifies from the facts Watcher stages — and with it the cutover gate
that held Watcher back between the flip and that deletion.
"""

import pytest
from ulid import ULID

import src.core.notifications.notify as notify
from src.core.notifications.notify import DispatchCandidate


def test_dispatch_candidate_has_no_apprise_url_field():
    """The local Apprise URL is gone — the dataclass shouldn't carry it."""
    candidate = DispatchCandidate(source="local", source_id=str(ULID()), remote_channel_id="c")
    assert not hasattr(candidate, "apprise_url")


@pytest.mark.parametrize(
    "removed_symbol",
    [
        # The Apprise strip (#137): the local entry point, and the context
        # manager that gated the notifier client behind USE_REMOTE_NOTIFY.
        "dispatch_event",
        "nullcontext",
        # F3 part 2 (#376): the dispatcher, the T gate it read, and the client
        # factory only it opened.
        "dispatch_event_notifications",
        "notify_cutover_at",
        "get_notifier_client",
    ],
)
def test_removed_symbols_stay_removed(removed_symbol):
    assert not hasattr(notify, removed_symbol), f"{removed_symbol} is back in notify.py"
