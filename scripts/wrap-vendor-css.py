#!/usr/bin/env python3
"""Wrap a vendor CSS file in `@layer LAYER_NAME { … }`.

Hoists any leading `@charset` and `@import` directives above the layer
opener (per CSS spec they must remain at the top of the file). `@import`
directives are rewritten with a `layer(LAYER_NAME)` suffix so the imported
sheet sorts in the same layer too.

`LAYER_NAME` is the module-level constant (default `"vendor"`); `wrap()`
also accepts a `layer_name` override for tests.

Usage: wrap-vendor-css.py <input.min.css> <output.layered.css>

Used by scripts/build-css.sh (regenerates on every build) and
scripts/check-css.sh (verifies committed *.layered.css matches source).
See docs/STYLE.md §11 (Overriding Vendored CSS).
"""

import re
import sys

# Single source of truth for the vendor cascade layer name. Must match the
# `@layer NAME;` declaration at the top of
# src/dashboard/static/css/input.css — if you rename here, rename there.
LAYER_NAME = "vendor"


def wrap(src_text: str, layer_name: str = LAYER_NAME) -> str:
    css = src_text

    charset = ""
    m = re.match(r'@charset\s+"[^"]*"\s*;', css)
    if m:
        charset = m.group(0)
        css = css[m.end() :]

    imports: list[str] = []
    import_re = re.compile(r"@import\s+[^;]+;", re.DOTALL)
    ws_or_comment = re.compile(r"(\s+|/\*.*?\*/)", re.DOTALL)
    i = 0
    while i < len(css):
        skip = ws_or_comment.match(css, i)
        if skip:
            i = skip.end()
            continue
        m = import_re.match(css, i)
        if not m:
            break
        raw = m.group(0)
        if re.search(r"\blayer\s*\(", raw):
            imports.append(raw)
        else:
            imports.append(raw[:-1].rstrip() + f" layer({layer_name});")
        i = m.end()

    body = css[i:]

    parts: list[str] = []
    if charset:
        parts.append(charset + "\n")
    for imp in imports:
        parts.append(imp + "\n")
    parts.append(f"@layer {layer_name} {{\n")
    parts.append(body.lstrip())
    parts.append("\n}\n")
    return "".join(parts)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.min.css> <output.layered.css>", file=sys.stderr)
        return 2
    src_path, out_path = sys.argv[1], sys.argv[2]
    # utf-8-sig transparently strips a leading BOM if present, so a BOM
    # before the optional @charset doesn't confuse the matcher.
    with open(src_path, encoding="utf-8-sig") as f:
        src_text = f.read()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(wrap(src_text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
