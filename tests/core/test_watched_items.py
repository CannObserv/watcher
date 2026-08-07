"""Tests for resolve_watch_target — the probe-free operator URL edit (#241, #251).

Nothing touches the network: the item re-enters PROBING with the URL as
submitted and the first fact resolves it. Its one caller since #251 is the
dashboard's ``POST /watched-items/{id}/effective-url`` — creates take the URL
from Archiver and start UNKNOWN (route-level coverage lives in
tests/dashboard/test_watched_item_routes.py::TestWatchedItemUrlReprobe).
"""

import pytest

from src.core.models.watched_item import WatchHealthStatus
from src.core.watched_items import resolve_watch_target


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
