"""Tests for conditional-GET validator state (#269, parts 2-4).

Pure unit tests: every rule that decides whether a command carries
``If-None-Match`` / ``If-Modified-Since`` lives in one predicate with no bus and
no database, so the seven invalidation rules are testable in isolation.

The rules under test, and why each exists, are in
``src/core/validators.py`` and docs/CONTENT-PIPELINE.md.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.validators import (
    CONDITIONAL_GET_ENV,
    DEFAULT_VALIDATOR_MAX_AGE_HOURS,
    MAX_VALIDATOR_LENGTH,
    VALIDATOR_MAX_AGE_ENV,
    canonical_specs,
    clear_validators,
    conditional_get_enabled,
    item_generation,
    record_validators,
    replayable_validators,
    sendable_validator,
    stamp_full_fetch,
    validator_max_age,
    validator_source_key,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

ITEM_ID = "01K2ZQWERTYUIOPASDFGHJKLZX"
OTHER_ID = "01K2ZQWERTYUIOPASDFGHJKLZY"

SPECS = [{"selector": "#content", "schema_version": 1}]
URL = "https://lcb.wa.gov/notices"
VERSION = "0.19.7+1"

# Replicator's temp horizon as observed in production — an example, not a
# contract: the rule under test must scale with whatever the fact carries.
HORIZON = timedelta(days=7)


def _key(url=URL, specs=SPECS, generation=VERSION):
    return validator_source_key(effective_url=url, source_specs=specs, generation=generation)


def _item(**kwargs):
    """A WatchedItem-shaped stand-in carrying only what the predicate reads."""
    base = {
        "id": ITEM_ID,
        "effective_url": URL,
        "source_specs": SPECS,
        "etag": 'W/"abc"',
        "last_modified": "Wed, 13 Aug 2026 10:00:00 GMT",
        "validator_source_key": _key(),
        "last_full_fetch_at": NOW - timedelta(hours=1),
        "blob_expires_at": NOW - timedelta(hours=1) + HORIZON,
        "processor_version": VERSION,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestSendableValidator:
    """The send-side guard: refuse before minting, never repair.

    Replicator refuses an unsendable header value as a terminal
    ``invalid_request_options`` *before any request goes out*. Minting a command
    we already know will be refused buys an ERROR health transition and a
    ``WATCH_ERROR`` for nothing.
    """

    def test_passes_a_weak_etag_verbatim(self):
        # Verbatim means verbatim: no W/ stripping, no quote repair (MUST-5).
        assert sendable_validator('W/"abc-123"') == 'W/"abc-123"'

    def test_passes_an_http_date_verbatim(self):
        value = "Wed, 13 Aug 2026 10:00:00 GMT"
        assert sendable_validator(value) == value

    def test_none_stays_none(self):
        assert sendable_validator(None) is None

    def test_blank_and_whitespace_only_are_unsendable(self):
        assert sendable_validator("") is None
        assert sendable_validator("   ") is None

    def test_control_characters_are_unsendable(self):
        # A CRLF in a header value is request splitting; Replicator refuses the
        # whole command for it.
        assert sendable_validator('"abc"\r\nX-Evil: 1') is None
        assert sendable_validator('"abc"\tdef') is None

    def test_obs_text_is_unsendable(self):
        # Latin-1 header decoding can surface \x80-\xff, which Replicator's send
        # half refuses (printable US-ASCII and SP only).
        assert sendable_validator('"caf\xe9"') is None

    def test_over_long_is_dropped_not_truncated(self):
        # A truncated ETag is a validator that can never match — worse than none.
        assert sendable_validator("x" * MAX_VALIDATOR_LENGTH) is not None
        assert sendable_validator("x" * (MAX_VALIDATOR_LENGTH + 1)) is None


class TestCanonicalSpecs:
    """The single notion of "did the specs change?" (#274).

    Extracted from ``validator_source_key`` rather than reimplemented beside it:
    two answers to that question in one codebase is the bug farm the issue named.
    The key itself cannot be reused for the comparison — it also folds in the URL
    and the extraction generation, so it moves on a co-core upgrade with
    byte-identical specs.
    """

    def test_is_stable_across_key_ordering(self):
        assert canonical_specs([{"a": 1, "b": 2}]) == canonical_specs([{"b": 2, "a": 1}])

    def test_spec_order_is_significant(self):
        # Same reason as the key: the fallback loop tries specs in order.
        assert canonical_specs([{"a": 1}, {"b": 2}]) != canonical_specs([{"b": 2}, {"a": 1}])

    def test_none_and_empty_are_the_same_absence(self):
        # A registry that stops announcing specs and one that announces `[]` have
        # said the same thing; the reconcile stores `[]` for both.
        assert canonical_specs(None) == canonical_specs([])

    def test_moves_when_a_spec_changes(self):
        assert canonical_specs([{"selector": "#content"}]) != canonical_specs(
            [{"selector": "main"}]
        )

    def test_survives_a_non_json_value(self):
        # `default=str`, matching the key: a JSONB round-trip can hand back a
        # Decimal or a datetime, and raising here would break the reconcile.
        assert canonical_specs([{"at": datetime(2026, 8, 27, tzinfo=UTC)}])

    @pytest.mark.parametrize(
        "left,right",
        [
            ([{"a": 1, "b": 2}], [{"b": 2, "a": 1}]),
            (None, []),
            ([{"a": 1}, {"b": 2}], [{"b": 2}, {"a": 1}]),
            ([{"selector": "#content"}], [{"selector": "main"}]),
        ],
    )
    def test_agrees_with_the_validator_key(self, left, right):
        """The two must never disagree about whether the specs moved.

        Held at the same URL and generation, so the key's verdict is the specs'
        verdict alone. This is the property the extraction exists to guarantee.
        """
        specs_same = canonical_specs(left) == canonical_specs(right)
        key_same = _key(specs=left) == _key(specs=right)
        assert specs_same == key_same


class TestValidatorSourceKey:
    """One key stands in for "everything that changes what the bytes mean".

    Storing it beside the validators means a spec edit, a URL move, or an
    extractor bump invalidates them wherever that change is written — no path
    has to remember to call a clear.
    """

    def test_is_stable_across_key_ordering(self):
        assert _key(specs=[{"a": 1, "b": 2}]) == _key(specs=[{"b": 2, "a": 1}])

    def test_moves_when_the_url_moves(self):
        assert _key() != _key(url="https://lcb.wa.gov/other")

    def test_moves_when_the_specs_move(self):
        assert _key() != _key(specs=[{"selector": "main"}])

    def test_moves_when_the_extraction_generation_moves(self):
        assert _key() != _key(generation="0.19.8+1")

    def test_spec_order_is_significant(self):
        # The fallback loop tries specs in order, so [a, b] and [b, a] can
        # extract different bytes from the same page.
        assert _key(specs=[{"a": 1}, {"b": 2}]) != _key(specs=[{"b": 2}, {"a": 1}])

    def test_no_specs_still_derives_a_key(self):
        assert _key(specs=None)

    def test_the_spelling_is_pinned(self):
        """Every stored pair was keyed by this exact function (#350).

        A key that moves silently invalidates the fleet's pairs at best, and at
        worst replays a pair against bytes it no longer names — the wedge #269
        exists to prevent. Moving it is allowed; doing so unnoticed is not.
        """
        assert _key() == ("sha256:5d558dd861a6172c18b212972454d1677cfb486200b32d8258d7e417cb679232")
        assert _key(generation=None) == (
            "sha256:96acaf40039e62c13181bb083f9cdaf0f2c305d79c2d664f4f8bccefbd7ac4fc"
        )


class TestItemGeneration:
    """Which extraction a stored pair is keyed to: the processor's (#326, #350).

    Watcher has no extractor of its own, so the generation is the version the
    processor last reported for the item. A processor upgrade invalidates once
    the next full fetch reports it (the design's accepted residual).
    """

    def test_is_the_items_reported_version(self):
        assert item_generation(_item(processor_version="0.19.8+1")) == "0.19.8+1"

    def test_with_no_report_yet_is_none(self):
        assert item_generation(_item(processor_version=None)) is None

    def test_a_processor_upgrade_stops_the_replay(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        item = _item(processor_version="0.19.7+1")
        record_validators(item, etag='"x"', last_modified=None, now=NOW)
        assert replayable_validators(item, now=NOW) == ('"x"', None)

        item.processor_version = "0.19.8+1"
        assert replayable_validators(item, now=NOW) == (None, None)


class TestConditionalGetEnabled:
    """Off by default; ``true`` for the fleet; a ULID list for the canary."""

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(CONDITIONAL_GET_ENV, raising=False)
        assert conditional_get_enabled(ITEM_ID) is False

    def test_false_is_off(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "false")
        assert conditional_get_enabled(ITEM_ID) is False

    def test_true_enables_every_item(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        assert conditional_get_enabled(ITEM_ID) is True
        assert conditional_get_enabled(OTHER_ID) is True

    def test_a_ulid_list_enables_only_those_items(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, f" {ITEM_ID} , 01OTHER")
        assert conditional_get_enabled(ITEM_ID) is True
        assert conditional_get_enabled(OTHER_ID) is False

    def test_the_list_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, ITEM_ID.lower())
        assert conditional_get_enabled(ITEM_ID) is True


class TestValidatorMaxAge:
    def test_defaults_to_a_week(self, monkeypatch):
        monkeypatch.delenv(VALIDATOR_MAX_AGE_ENV, raising=False)
        assert validator_max_age() == timedelta(hours=DEFAULT_VALIDATOR_MAX_AGE_HOURS)

    def test_reads_the_env_override(self, monkeypatch):
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "24")
        assert validator_max_age() == timedelta(hours=24)

    def test_a_non_positive_ceiling_is_logged(self, monkeypatch, caplog):
        # CR-6: safe direction (always re-fetch), but a typo'd sign silently
        # switches the feature off — say so.
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "-168")
        with caplog.at_level(logging.INFO, logger="src.core.validators"):
            assert validator_max_age() <= timedelta(0)
        assert any("conditional GET" in r.getMessage() for r in caplog.records)

    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch):
        # A typo'd knob must not wedge every command; the safe direction is
        # "re-fetch unconditionally more often", which the default already is.
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "soon")
        assert validator_max_age() == timedelta(hours=DEFAULT_VALIDATOR_MAX_AGE_HOURS)


class TestReplayableValidators:
    """The seven rules, one predicate."""

    def test_replays_the_stored_pair_when_everything_agrees(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        etag, last_modified = replayable_validators(_item(), now=NOW)
        assert etag == 'W/"abc"'
        assert last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"

    def test_the_gate_being_off_sends_nothing(self, monkeypatch):
        monkeypatch.delenv(CONDITIONAL_GET_ENV, raising=False)
        assert replayable_validators(_item(), now=NOW) == (None, None)

    def test_no_stored_validator_sends_nothing(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        item = _item(etag=None, last_modified=None)
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_one_of_the_pair_is_enough(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        item = _item(last_modified=None)
        assert replayable_validators(item, now=NOW) == ('W/"abc"', None)

    def test_a_changed_source_key_sends_nothing(self, monkeypatch):
        # Rules 2-4: specs, URL, or extraction generation moved. The stored pair
        # was earned under different meaning-of-the-bytes, so a 304 would inherit
        # a fingerprint nothing recomputed.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        item = _item(source_specs=[{"selector": "main"}])
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_a_missing_source_key_sends_nothing(self, monkeypatch):
        # Rows that predate the column: unknown provenance is not replayable.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        assert replayable_validators(_item(validator_source_key=None), now=NOW) == (None, None)

    def test_force_full_fetch_sends_nothing(self, monkeypatch):
        # Rule 5: the operator's "check now" is always a real re-read.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        assert replayable_validators(_item(), now=NOW, force_full_fetch=True) == (None, None)

    def test_an_aged_out_pair_sends_nothing(self, monkeypatch):
        # Rule 6: the residual net for an origin whose validator tracks
        # something other than the region we extract.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "24")
        item = _item(last_full_fetch_at=NOW - timedelta(hours=25))
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_inside_the_age_ceiling_still_replays(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "24")
        item = _item(last_full_fetch_at=NOW - timedelta(hours=23))
        assert replayable_validators(item, now=NOW)[0] == 'W/"abc"'

    def test_a_missing_last_full_fetch_sends_nothing(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        assert replayable_validators(_item(last_full_fetch_at=None), now=NOW) == (None, None)

    def test_an_unknown_blob_horizon_sends_nothing(self, monkeypatch):
        # Rule 7, #339: with no horizon there is no telling when the blob
        # behind the latest revision needs renewing — the same stance as a
        # missing last_full_fetch_at.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        assert replayable_validators(_item(blob_expires_at=None), now=NOW) == (None, None)

    def test_past_the_blob_half_life_sends_nothing(self, monkeypatch):
        # A 304 produces no blob and so no #293 renewal; past half the horizon
        # the next occasion fetches in full so Archiver's horizon moves first.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        fetched = NOW - HORIZON / 2 - timedelta(minutes=1)
        item = _item(last_full_fetch_at=fetched, blob_expires_at=fetched + HORIZON)
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_inside_the_blob_half_life_still_replays(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        fetched = NOW - HORIZON / 2 + timedelta(minutes=1)
        item = _item(last_full_fetch_at=fetched, blob_expires_at=fetched + HORIZON)
        assert replayable_validators(item, now=NOW)[0] == 'W/"abc"'

    def test_the_half_life_scales_with_the_observed_horizon(self, monkeypatch):
        # The horizon is Replicator's setting, not a watcher constant: a 48h
        # horizon forces a full fetch after 24h, well inside the age ceiling.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        fetched = NOW - timedelta(hours=25)
        item = _item(last_full_fetch_at=fetched, blob_expires_at=fetched + timedelta(hours=48))
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_no_age_ceiling_carries_a_pair_past_the_half_life(self, monkeypatch):
        # The #339 relationship, pinned: the ceiling is the drift net only, so
        # no value of the knob can hold a pair past the renewal point.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        monkeypatch.setenv(VALIDATOR_MAX_AGE_ENV, "100000")
        fetched = NOW - HORIZON / 2 - timedelta(minutes=1)
        item = _item(last_full_fetch_at=fetched, blob_expires_at=fetched + HORIZON)
        assert replayable_validators(item, now=NOW) == (None, None)

    def test_an_unsendable_stored_value_is_dropped(self, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        item = _item(etag='"abc"\r\nX-Evil: 1')
        assert replayable_validators(item, now=NOW) == (
            None,
            "Wed, 13 Aug 2026 10:00:00 GMT",
        )


class TestRecordAndClear:
    def test_record_stores_the_pair_with_its_provenance(self):
        item = _item(
            etag=None, last_modified=None, validator_source_key=None, last_full_fetch_at=None
        )
        record_validators(item, etag='"xyz"', last_modified=None, now=NOW)

        assert item.etag == '"xyz"'
        assert item.last_modified is None
        # Keyed to the item's processor version (#269, #350).
        assert item.validator_source_key == _key(generation=VERSION)

    def test_record_does_not_own_the_fetch_stamp(self):
        # CR-2: "bytes arrived" is a fetch fact, not a validator fact — an
        # extraction failure stores no pair and must still move the stamp.
        item = _item(last_full_fetch_at=None)
        record_validators(item, etag='"xyz"', last_modified=None, now=NOW)
        assert item.last_full_fetch_at is None

    def test_stamp_full_fetch_records_when_bytes_arrived(self):
        item = _item(last_full_fetch_at=None)
        stamp_full_fetch(item, now=NOW, blob_expires_at=NOW + HORIZON)
        assert item.last_full_fetch_at == NOW

    def test_stamp_full_fetch_records_the_blob_horizon_with_it(self):
        # One fact, one pair: the half-life is measured between the two, so
        # they must never describe different fetches (#339).
        item = _item()
        stamp_full_fetch(item, now=NOW, blob_expires_at=NOW + HORIZON)
        assert item.blob_expires_at == NOW + HORIZON

    def test_stamp_full_fetch_overwrites_with_an_unknown_horizon(self):
        # An old horizon beside a new stamp would place the half-life wrongly.
        item = _item()
        stamp_full_fetch(item, now=NOW, blob_expires_at=None)
        assert item.blob_expires_at is None

    def test_record_clears_a_pair_the_origin_stopped_sending(self):
        # Always overwrite, including None: the pair must describe the latest 200.
        item = _item()
        record_validators(item, etag=None, last_modified=None, now=NOW)
        assert item.etag is None
        assert item.last_modified is None

    def test_clear_drops_the_pair_and_its_key(self):
        item = _item()
        clear_validators(item)
        assert item.etag is None
        assert item.last_modified is None
        assert item.validator_source_key is None
        # last_full_fetch_at is a fetch fact, not a validator — untouched.
        assert item.last_full_fetch_at is not None
