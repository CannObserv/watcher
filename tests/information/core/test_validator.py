"""Validator tests for InfoSpec v1 documents."""

import pytest

from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)


def _minimal_valid() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com/page"},
        "extraction": {"algorithm": "css", "selector": ".content"},
        "fingerprint": {"algorithm": "sha256"},
    }


def test_minimal_valid_doc_passes():
    validate_info_spec(_minimal_valid())


def test_full_page_does_not_require_selector():
    doc = _minimal_valid()
    doc["extraction"] = {"algorithm": "full_page"}
    validate_info_spec(doc)


def test_css_requires_selector():
    doc = _minimal_valid()
    doc["extraction"] = {"algorithm": "css"}
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_unknown_algorithm_rejected():
    doc = _minimal_valid()
    doc["extraction"]["algorithm"] = "magic"
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_unknown_schema_version_rejected():
    doc = _minimal_valid()
    doc["schema_version"] = 2
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_missing_url_rejected():
    doc = _minimal_valid()
    doc["target"] = {}
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_extra_top_level_key_rejected():
    doc = _minimal_valid()
    doc["unexpected"] = "field"
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)
