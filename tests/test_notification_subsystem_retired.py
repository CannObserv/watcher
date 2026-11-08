"""Guard: Watcher has no notification subsystem (#376 F3 part 2, #379).

F3 handed notifications to Archiver at T = 2026-11-06T21:00:00Z. Archiver
notifies from the ``info.watch-events`` facts Watcher stages (#369). #376
removed Watcher's three automatic dispatch sites, the T gate and #71's
reminder; #379 deleted what was left: the templates' renderer and test sends,
the notifier client, the change diff and the blob reader only it used, and
every setting that configured them. A site that came back would send beside
Archiver, on a tenant notifier#118 revokes.

Scope, explicitly: ``src/``, ``scripts/`` and ``deploy/`` — where a retired
name would be *live*, read by code, set by a script or set on the unit — plus
AGENTS.md, which tells agents what to set. Not scanned, on purpose: ``docs/``
(``ENVIRONMENT.md`` keeps the history), ``docs/plans/``, ``alembic/``
(migrations are history) and ``tests/`` (this file names them).

This file replaced #166's guard, which tied every subscribable event to a
dispatch site, and #376's, which forbade the dispatcher and two knobs; it
absorbed the notifier isolation and unit-only credential guards (#277, #278),
which guarded names nothing reads any more.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
# Where a retired name could come back: read by code, set by a script, set on
# the unit (an ``Environment=`` or ``EnvironmentFile=`` line), or instructed.
_SCANNED_TREES = ("src", "scripts", "deploy")
_SCANNED_FILES = ("AGENTS.md",)
_DISPATCH = "dispatch_event_notifications"
_RETIRED_NAMES = (
    # #376
    "WATCHER_NOTIFY_CUTOVER_AT",
    "WATCHER_ERROR_RENOTIFY_INTERVAL",
    # #379: the notifier client, its credentials, gate and unit-only file
    "WATCHER_NOTIFIER_",
    "WATCHER_DEV_NOTIFIER_",
    "notifier_client",
    "notifier.env",
    # #379: the dashboard links in a notification
    "WATCHER_PUBLIC_BASE_URL",
    "WATCHER_DEV_PUBLIC_BASE_URL",
    # #379: the change diff and the blob reader only it used
    "WATCHER_DIFF_MAX_INPUT_BYTES",
    "GCS_BLOB_CREDENTIALS",
)
_RETIRED_PATHS = (
    "src/core/notifications",
    "src/core/notifier_client",
    "src/core/public_base_url.py",
    "src/core/blobs.py",
    "src/api/routes/notification_templates.py",
    "src/api/routes/watched_item_notifications.py",
    "src/dashboard/routes/notifications.py",
    "src/dashboard/static/js/notifications.js",
)


def _sources() -> list[tuple[str, str]]:
    return [(str(p.relative_to(_SRC.parent)), p.read_text()) for p in sorted(_SRC.rglob("*.py"))]


def _names(tree: ast.AST) -> set[str]:
    """Every identifier a module calls, defines, imports or reads by attribute."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def test_the_scan_sees_the_source_tree():
    """Non-vacuity: the scan reads the real tree, not an empty one."""
    paths = {path for path, _ in _sources()}
    assert {"src/workers/pipeline.py", "src/workers/fetch_commands.py"} <= paths


def test_no_module_names_the_dispatcher():
    offenders = [p for p, text in _sources() if _DISPATCH in _names(ast.parse(text, filename=p))]
    assert not offenders, f"{offenders} name {_DISPATCH}: Watcher's dispatch is Archiver's (#376)"


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


def test_no_file_names_a_retired_name():
    offenders = [
        (str(p.relative_to(_ROOT)), name)
        for files in _scanned_files().values()
        for p in files
        for name in _RETIRED_NAMES
        if name in p.read_text(errors="ignore")
    ]
    assert not offenders, f"{offenders}: retired with Watcher's notifications (#376, #379)"


def _present(path: Path) -> bool:
    """A file, or a directory holding anything but bytecode: a deploy checkout
    keeps a deleted package's ``__pycache__`` behind, which imports nothing."""
    if path.is_dir():
        return any(p.suffix != ".pyc" and p.is_file() for p in path.rglob("*"))
    return path.exists()


def test_the_retired_modules_are_gone():
    present = [path for path in _RETIRED_PATHS if _present(_ROOT / path)]
    assert not present, f"{present} came back: Watcher sends no notification (#379)"


def test_the_notifier_sdk_is_not_a_dependency():
    """Neither declared nor locked: the SDK went with its last caller (#379)."""
    offenders = [
        name
        for name in ("pyproject.toml", "uv.lock")
        if "notifier-client" in (_ROOT / name).read_text()
    ]
    assert not offenders, f"{offenders} still name notifier-client"


def test_the_scanner_finds_a_call_an_import_and_a_definition():
    """The scanner itself, on synthetic source: each form a site could take."""
    for source in (
        "async def f(s, e):\n    await dispatch_event_notifications(session=s, event=e)\n",
        "from src.core.notifications.notify import dispatch_event_notifications\n",
        "async def dispatch_event_notifications(session, event):\n    pass\n",
        "async def f(s, e):\n    await notify.dispatch_event_notifications(s, e)\n",
    ):
        assert _DISPATCH in _names(ast.parse(source)), source
