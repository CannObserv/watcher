"""Tests for src/core/ulids.py — the canonical-ULID parse guard (#432).

python-ulid 4.1.0 made ``ULID.from_str`` case-insensitive; 2.6.0–4.0.x rejected
lowercase. Watcher's contract is the uppercase canonical form it advertises
(``ULID_PATTERN``), so every inbound parse goes through ``parse_canonical_ulid``
and the accepted set no longer moves with the library.
"""

import ast
import re
from pathlib import Path

import pytest
from ulid import ULID

from src.api.schemas import types as schema_types
from src.core.ulids import ULID_PATTERN, parse_canonical_ulid

CANONICAL = "01ABCDEFGHJKMNPQRSTVWXYZ00"
SRC = Path(__file__).resolve().parents[2] / "src"

# The only modules allowed a bare ``ULID.from_str``, each with its reason.
BARE_FROM_STR_ALLOWED = {
    # The guard itself.
    "core/ulids.py",
    # The column type's read path: it parses what storage holds, and storage is
    # written only by process_bind_param's ``str(ULID)`` — never by a client.
    "core/models/base.py",
}


class TestParseCanonicalULID:
    def test_accepts_the_canonical_form(self):
        assert parse_canonical_ulid(CANONICAL) == ULID.from_str(CANONICAL)

    @pytest.mark.parametrize(
        "bad",
        [
            CANONICAL.lower(),
            "01abcdefGHJKMNPQRSTVWXYZ00",
            "not-a-ulid",
            "",
            "8" + "0" * 25,
            CANONICAL + "\n",
            " " + CANONICAL,
        ],
        ids=["lowercase", "mixed-case", "garbage", "empty", "overflow", "newline", "space"],
    )
    def test_rejects_anything_off_the_pattern(self, bad):
        with pytest.raises(ValueError):
            parse_canonical_ulid(bad)

    @pytest.mark.parametrize("bad", [12345, None, b"01ABCDEFGHJKMNPQRSTVWXYZ00", True])
    def test_non_string_is_a_type_error(self, bad):
        with pytest.raises(TypeError):
            parse_canonical_ulid(bad)

    def test_schema_types_advertise_the_same_pattern(self):
        """One pattern: the OpenAPI document and the guard cannot drift apart."""
        assert schema_types.ULID_PATTERN is ULID_PATTERN
        assert re.fullmatch(ULID_PATTERN, CANONICAL)


class TestNoBareFromStr:
    """Fitness: an inbound parse that skips the guard silently widens the
    contract on the next python-ulid release. Route it through
    ``parse_canonical_ulid``, or add it to the allowlist with its reason."""

    def test_only_allowlisted_modules_call_ulid_from_str(self):
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            if rel in BARE_FROM_STR_ALLOWED:
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "from_str"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ULID"
                ):
                    offenders.append(f"src/{rel}:{node.lineno}")
        assert offenders == []

    def test_allowlist_has_no_stale_entries(self):
        for rel in BARE_FROM_STR_ALLOWED:
            assert "ULID.from_str" in (SRC / rel).read_text(), rel
