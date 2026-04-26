"""Tests for the wrap() helper in scripts/wrap-vendor-css.py.

The script lives outside the importable package tree (and uses a hyphenated
filename), so we load it via importlib.util from its absolute path.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "wrap-vendor-css.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wrap_vendor_css", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrap_vendor_css"] = mod
    spec.loader.exec_module(mod)
    return mod


wrap_vendor_css = _load_module()
wrap = wrap_vendor_css.wrap
LAYER_NAME = wrap_vendor_css.LAYER_NAME


def test_no_directives_wraps_body_only():
    src = ".foo{color:red}"
    out = wrap(src)
    assert out == f"@layer {LAYER_NAME} {{\n.foo{{color:red}}\n}}\n"


def test_charset_only_hoisted_above_layer():
    src = '@charset "UTF-8";\n'
    out = wrap(src)
    assert out == f'@charset "UTF-8";\n@layer {LAYER_NAME} {{\n\n}}\n'


def test_import_without_layer_gets_layer_suffix():
    src = '@import url("https://fonts.example/x.css");\n.foo{color:red}'
    out = wrap(src)
    assert out == (
        f'@import url("https://fonts.example/x.css") layer({LAYER_NAME});\n'
        f"@layer {LAYER_NAME} {{\n"
        ".foo{color:red}\n"
        "}\n"
    )


def test_import_with_existing_layer_is_idempotent():
    src = '@import "y.css" layer(vendor);\n.foo{color:red}'
    out = wrap(src)
    assert out == (
        f'@import "y.css" layer(vendor);\n@layer {LAYER_NAME} {{\n.foo{{color:red}}\n}}\n'
    )


def test_charset_plus_comment_plus_multiple_imports():
    src = (
        '@charset "UTF-8";\n'
        "/* a comment */\n"
        '@import url("a.css");\n'
        '@import "b.css" layer(vendor);\n'
        ".foo{color:red}"
    )
    out = wrap(src)
    assert out == (
        '@charset "UTF-8";\n'
        f'@import url("a.css") layer({LAYER_NAME});\n'
        '@import "b.css" layer(vendor);\n'
        f"@layer {LAYER_NAME} {{\n"
        ".foo{color:red}\n"
        "}\n"
    )


def test_empty_file_produces_empty_layer_block():
    out = wrap("")
    assert out == f"@layer {LAYER_NAME} {{\n\n}}\n"


def test_layer_name_override_is_threaded_through():
    src = '@import "a.css";\n.foo{}'
    out = wrap(src, layer_name="custom")
    assert out == ('@import "a.css" layer(custom);\n@layer custom {\n.foo{}\n}\n')
