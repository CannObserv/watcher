"""Guard: Watcher serves no dashboard and builds no CSS (#377).

Archiver's control plane (CannObserv/archiver#359) became the operator surface
at the headless jump (#368); the journal from 2026-10-25 to #377 held no
dashboard request at all. #377 deleted ``src/dashboard`` — its routes,
templates and static assets — the Jinja/HTMX stack, the Tailwind CLI and its
build and staleness scripts, the pre-commit hook and CI job that ran them, and
the three docs that described them. A surface that came back would be an
operator path Archiver does not know about, served from the process that runs
the worker.

Scope of the name scan: ``src/``, ``scripts/``, ``.github/``, ``deploy/``, the
pre-commit config, ``pyproject.toml`` and AGENTS.md — where a retired tool would
be *run*, *installed* or *instructed* (``deploy/`` since #381 rewrote its
comments). Not scanned, on purpose: ``docs/`` and ``docs/plans/`` (history) and
``tests/`` (this file names them).

Kept apart from ``tests/test_notification_subsystem_retired.py``: a different
subsystem, retired for a different reason.
"""

import ast
import tomllib
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from src.api.main import app

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

_RETIRED_PATHS = (
    "src/dashboard",
    "src/core/scheduling/schedule.py",
    "scripts/build-css.sh",
    "scripts/check-css.sh",
    "scripts/wrap-vendor-css.py",
    "scripts/check-touch-targets.sh",
    "docs/STYLE.md",
    "docs/UI.md",
    "docs/WATCHED-ITEMS-DASHBOARD.md",
)
_RETIRED_DEPENDENCIES = ("jinja2", "python-multipart", "beautifulsoup4")
# Module prefixes that only a server-rendered UI imports.
_RETIRED_IMPORTS = (
    "src.dashboard",
    "jinja2",
    "fastapi.templating",
    "starlette.templating",
    "fastapi.staticfiles",
    "starlette.staticfiles",
    "multipart",
    "python_multipart",
    "bs4",
)
# A form body is python-multipart's reason to exist (#377 brief: no `Form(`,
# `UploadFile` or `request.form(` outside the dashboard).
_RETIRED_CALLS = ("Form", "File", "UploadFile")
_SCANNED_TREES = ("src", "scripts", ".github", "deploy")
_SCANNED_FILES = (".pre-commit-config.yaml", "pyproject.toml", "AGENTS.md")
_RETIRED_NAMES = ("tailwind", "check-css", "build-css", "output.css", "htmx")
# Each prefix the dashboard served (#377), and one live path as the control.
_GONE = (
    "/",
    "/settings",
    "/settings/api-keys",
    "/static/css/output.css",
    "/watched-items",
    "/domains",
    "/audit",
)


def _present(path: Path) -> bool:
    """A file, or a directory holding anything but bytecode: a deploy checkout
    keeps a deleted package's ``__pycache__`` behind, which imports nothing."""
    if path.is_dir():
        return any(p.suffix != ".pyc" and p.is_file() for p in path.rglob("*"))
    return path.exists()


def test_the_retired_paths_are_gone():
    present = [path for path in _RETIRED_PATHS if _present(_ROOT / path)]
    assert not present, f"{present} came back: the operator surface is Archiver's (#377)"


def _declared() -> set[str]:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    specs = list(pyproject["project"]["dependencies"])
    for group in pyproject.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    return {canonicalize_name(Requirement(s).name) for s in specs}


def test_the_scan_reads_the_declared_dependencies():
    """Non-vacuity: the parser finds what is really declared."""
    assert {"fastapi", "pytest"} <= _declared()


def test_the_ui_dependencies_are_not_declared():
    offenders = sorted(_declared() & {canonicalize_name(d) for d in _RETIRED_DEPENDENCIES})
    assert not offenders, f"{offenders} declared again: nothing renders or parses a form (#377)"


def _sources() -> list[tuple[str, ast.AST]]:
    return [
        (str(p.relative_to(_ROOT)), ast.parse(p.read_text(), filename=str(p)))
        for p in sorted(_SRC.rglob("*.py"))
    ]


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def _calls(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                found.add(func.id)
            elif isinstance(func, ast.Attribute):
                found.add(func.attr)
    return found


def test_the_import_scan_sees_the_source_tree():
    """Non-vacuity: the scan reads the real tree and finds a real import."""
    sources = dict(_sources())
    assert "src/api/main.py" in sources
    assert "fastapi" in _imports(sources["src/api/main.py"])


def test_no_module_imports_a_ui_stack():
    offenders = [
        (path, name)
        for path, tree in _sources()
        for name in _imports(tree)
        if any(name == r or name.startswith(r + ".") for r in _RETIRED_IMPORTS)
    ]
    assert not offenders, f"{offenders}: Watcher renders no page (#377)"


def test_no_module_parses_a_form_body():
    offenders = [
        (path, name) for path, tree in _sources() for name in _calls(tree) & set(_RETIRED_CALLS)
    ]
    form_reads = [
        path
        for path, tree in _sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "form"
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    ]
    assert not offenders and not form_reads, (
        f"{offenders} {form_reads}: no form body, so no python-multipart (#377)"
    )


def _scanned_files() -> dict[str, list[Path]]:
    files = {
        tree: sorted(
            p for p in (_ROOT / tree).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
        for tree in _SCANNED_TREES
    }
    files.update({name: [_ROOT / name] for name in _SCANNED_FILES})
    return files


def test_the_name_scan_sees_every_tree():
    """Non-vacuity: each tree and file the name scan reads exists and has files."""
    files = _scanned_files()
    assert all(files.values()), {t: len(f) for t, f in files.items()}
    assert all(p.is_file() for paths in files.values() for p in paths)


def test_no_file_names_the_css_tooling():
    offenders = [
        (str(p.relative_to(_ROOT)), name)
        for files in _scanned_files().values()
        for p in files
        for name in _RETIRED_NAMES
        if name in p.read_text(errors="ignore").lower()
    ]
    assert not offenders, f"{offenders}: the CSS tooling went with the dashboard (#377)"


async def test_the_dashboard_paths_404():
    """Non-vacuity first: ``/health`` answers on the same transport."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/health")).status_code == 200
        served = {path: (await c.get(path)).status_code for path in _GONE}
    assert all(code == 404 for code in served.values()), f"{served}: still served (#377)"


def test_the_scanners_catch_each_form():
    """The scanners themselves, on synthetic source."""
    assert "jinja2" in _imports(ast.parse("import jinja2\n"))
    assert "fastapi.templating.Jinja2Templates" in _imports(
        ast.parse("from fastapi.templating import Jinja2Templates\n")
    )
    assert "Form" in _calls(ast.parse("def f(x=Form(...)):\n    pass\n"))
    assert "form" in {
        n.attr
        for n in ast.walk(ast.parse("async def f(request):\n    await request.form()\n"))
        if isinstance(n, ast.Attribute)
    }
