"""Cross-implementation parity for the production-database guard.

The ``_test``/``_dev`` suffix rule is enforced twice, in two languages:

  - ``src/core/db_safety.py``  — application lifespan guard (``urlsplit``)
  - ``scripts/dev_server.sh``  — launch-path guard (bash parameter expansion)

Bash cannot import the Python, so the duplication is unavoidable. What is
avoidable is the two drifting apart silently. This module feeds one shared
corpus through both and asserts they reach the same verdict, so a change to
either implementation that alters its judgement fails here.

The corpus deliberately includes the shapes that differ between a URL parser
and string munging: escaped slashes in passwords, query strings, missing
ports, and suffix near-misses.
"""

import subprocess
from pathlib import Path

import pytest

from src.core.db_safety import is_non_production_database

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_server.sh"

# (url, accepted) — accepted=True means "this is a disposable dev/test database".
CORPUS: list[tuple[str, bool]] = [
    # Plain accepts.
    ("postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_test", True),
    ("postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_dev", True),
    ("postgresql://u:p@host/watcher_test", True),
    # Driver prefix and host spelling are not a safety boundary — the name is.
    ("postgresql://watcher:watcher@127.0.0.1:5432/watcher_test", True),
    ("postgresql+psycopg://u:p@otherhost:5432/x_dev", True),
    # Plain rejects.
    ("postgresql+asyncpg://watcher:watcher@localhost:5432/watcher", False),
    ("postgresql://u:p@host/watcher", False),
    # Suffix near-misses — substring matching would wrongly accept these.
    ("postgresql://u:p@host/test_watcher", False),
    ("postgresql://u:p@host/watcher_testing", False),
    ("postgresql://u:p@host/devious", False),
    ("postgresql://u:p@host/dev", False),
    # Query string must not bleed into the name either way.
    ("postgresql://u:p@host:5432/watcher_test?sslmode=require", True),
    ("postgresql://u:p@host:5432/watcher?sslmode=require", False),
    # Escaped slash in the password must not be read as the path.
    ("postgresql://u:p%2Fw@host:5432/watcher", False),
    ("postgresql://u:p%2Fw@host:5432/watcher_test", True),
    # CR finding 1: '@' inside the query of a credential-less URL must not be
    # read as a credentials terminator — the tail is not the database name.
    # This names production 'watcher' and must be refused.
    ("postgresql://host:5432/watcher?options=endpoint%3Da@b_test", False),
    # ...and a proper _test name keeps its verdict with '@' in the query.
    ("postgresql://host:5432/watcher_test?options=endpoint%3Da@b", True),
    # A bare database name is not a connection URL — fail closed, both sides.
    ("watcher_test", False),
    # Unparseable — both must fail closed.
    ("not-a-url", False),
]


def _bash_accepts(url: str) -> bool:
    """True when scripts/dev_server.sh would serve this database."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "WATCHER_DEV_SERVER_DRY_RUN": "1",
            "WATCHER_DEV_SERVER_SKIP_ENV_FILES": "1",
            "TEST_DATABASE_URL": url,
        },
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def bash_verdicts() -> dict[str, bool]:
    """One bash invocation per corpus entry, shared by every test below.

    Each call spawns a subprocess, so evaluating the corpus per-test would
    make subprocess spawning the dominant cost of this module.
    """
    return {url: _bash_accepts(url) for url, _ in CORPUS}


@pytest.mark.parametrize(("url", "accepted"), CORPUS)
def test_python_verdict(url: str, accepted: bool) -> None:
    assert is_non_production_database(url) is accepted


@pytest.mark.parametrize(("url", "accepted"), CORPUS)
def test_bash_verdict(url: str, accepted: bool, bash_verdicts: dict[str, bool]) -> None:
    assert bash_verdicts[url] is accepted
