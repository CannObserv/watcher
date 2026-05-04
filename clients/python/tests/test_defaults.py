"""Tests for the consumer-defaults module."""

from information_client.defaults import (
    DEFAULT_FETCH_RENDER,
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    fetch_render,
    fetch_timeout_seconds,
)


def test_default_render_is_false():
    assert DEFAULT_FETCH_RENDER is False


def test_default_timeout_is_30_seconds():
    assert DEFAULT_FETCH_TIMEOUT_SECONDS == 30


def test_fetch_render_resolves_explicit_value():
    doc = {"target": {"fetch": {"render": True}}}
    assert fetch_render(doc) is True


def test_fetch_render_resolves_default_when_absent():
    doc = {"target": {"url": "https://example.com"}}
    assert fetch_render(doc) is False


def test_fetch_render_resolves_default_when_target_missing():
    doc = {}
    assert fetch_render(doc) is False


def test_fetch_timeout_resolves_explicit_value():
    doc = {"target": {"fetch": {"timeout_seconds": 90}}}
    assert fetch_timeout_seconds(doc) == 90


def test_fetch_timeout_resolves_default_when_absent():
    doc = {"target": {"url": "https://example.com"}}
    assert fetch_timeout_seconds(doc) == 30
