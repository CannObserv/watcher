"""Guard: Watcher serves no operator API, takes no API key and probes no URL (#378).

Design D1 (``docs/plans/2026-10-14-headless-watcher-and-cohort-boundaries-design.md``):
Watcher is bus-only; Archiver is the API. The journal from 2026-10-25 to #378
held no ``/api/v1`` request, and no service in the cohort calls it. #378
deleted the ``/api/v1`` routes and schemas, the ``X-API-Key`` dependency, the
URL probe and its egress guard. With them went FastAPI's generated docs: a
headless service publishes no API contract. What is left on HTTP is the two
probes systemd and the operator read, ``/health`` and ``/ready``.

Scope, as #377's guard: routes served, paths present, names imported under
``src/``. Not a string ban across the tree on purpose: ``src/ops/checkin.py``
speaks ``/api/v1`` and ``X-API-Key`` to a different service (the status
monitor's check-in), and that is not this surface.

The ``api_keys`` and ``app_users`` tables, and their models, stay inert until
#380 drops them.

Kept apart from ``tests/test_dashboard_retired.py``: a different surface,
retired by a different issue.
"""

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from src.api.main import app

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

_RETIRED_PATHS = (
    "src/api/routes/audit_log.py",
    "src/api/routes/domains.py",
    "src/api/routes/helpers.py",
    "src/api/routes/probe.py",
    "src/api/routes/watched_items.py",
    "src/api/schemas",
    "src/core/probe.py",
    "src/core/egress.py",
)
# Module prefixes only the deleted surface imported.
_RETIRED_IMPORTS = (
    "src.api.schemas",
    "src.api.routes.audit_log",
    "src.api.routes.domains",
    "src.api.routes.helpers",
    "src.api.routes.probe",
    "src.api.routes.watched_items",
    "src.core.probe",
    "src.core.egress",
    "fastapi.security",
)
# Names whose return would mean an authenticated surface came back.
_RETIRED_NAMES = ("require_api_key", "APIKeyHeader", "get_probe_fn")
# One path per deleted router, the generated docs, and a write route.
_GONE = (
    ("GET", "/api/v1/watched-items"),
    ("POST", "/api/v1/watched-items"),
    ("GET", "/api/v1/domains"),
    ("GET", "/api/v1/audit"),
    ("POST", "/api/v1/probe"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/redoc"),
)
_SERVED = {"/health", "/ready"}


def _present(path: Path) -> bool:
    """A file, or a directory holding anything but bytecode: a deploy checkout
    keeps a deleted package's ``__pycache__`` behind, which imports nothing."""
    if path.is_dir():
        return any(p.suffix != ".pyc" and p.is_file() for p in path.rglob("*"))
    return path.exists()


def test_the_retired_paths_are_gone():
    present = [path for path in _RETIRED_PATHS if _present(_ROOT / path)]
    assert not present, f"{present} came back: Watcher is bus-only (#378, D1)"


def test_the_app_serves_only_the_probes():
    """The API routes are the two probes, and nothing is mounted beside them.

    ``app.openapi()`` still builds with the docs off, and it is FastAPI's public
    view of every ``APIRoute``, through included routers (0.162 wraps those
    lazily, so ``app.routes`` alone does not show their paths). The generated
    docs and any static mount are plain Starlette routes and mounts, which it
    does not list but which carry a ``path`` at the top level, so the second
    assertion catches those.
    """
    paths = set(app.openapi()["paths"])
    assert paths == _SERVED, f"{sorted(paths - _SERVED)} served: Watcher is bus-only (#378)"
    mounted = [r.path for r in app.routes if hasattr(r, "path") and not isinstance(r, APIRoute)]
    assert not mounted, f"{mounted} mounted: no docs, no static files (#378)"


def test_the_app_publishes_no_api_contract():
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)


async def test_the_operator_api_paths_404():
    """Non-vacuity first: ``/health`` answers on the same transport."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/health")).status_code == 200
        served = {
            f"{method} {path}": (await c.request(method, path)).status_code
            for method, path in _GONE
        }
    assert all(code == 404 for code in served.values()), f"{served}: still served (#378)"


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


def _names(tree: ast.AST) -> set[str]:
    """Every identifier defined, referenced or imported as."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def test_the_scan_sees_the_source_tree():
    """Non-vacuity: the scan reads the real tree and finds a real import and name."""
    sources = dict(_sources())
    assert "src/api/routes/health.py" in sources
    assert "src.api.deps.get_db_session" in _imports(sources["src/api/routes/health.py"])
    assert "get_db_session" in _names(sources["src/api/routes/health.py"])


def test_no_module_imports_the_retired_surface():
    offenders = [
        (path, name)
        for path, tree in _sources()
        for name in _imports(tree)
        if any(name == r or name.startswith(r + ".") for r in _RETIRED_IMPORTS)
    ]
    assert not offenders, f"{offenders}: no operator API, no probe (#378)"


def test_no_module_names_an_api_key_dependency():
    offenders = [
        (path, name) for path, tree in _sources() for name in _names(tree) & set(_RETIRED_NAMES)
    ]
    assert not offenders, f"{offenders}: nothing authenticates an operator (#378)"


def test_the_scanners_catch_each_form():
    """The scanners themselves, on synthetic source."""
    assert "fastapi.security.APIKeyHeader" in _imports(
        ast.parse("from fastapi.security import APIKeyHeader\n")
    )
    assert "src.core.probe" in _imports(ast.parse("import src.core.probe\n"))
    assert "require_api_key" in _names(ast.parse("async def require_api_key():\n    pass\n"))
    assert "APIKeyHeader" in _names(ast.parse("h = security.APIKeyHeader(name='x')\n"))
    assert "get_probe_fn" in _names(ast.parse("Depends(get_probe_fn)\n"))
