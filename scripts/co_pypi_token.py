"""Run one ``uv`` command with a co-pypi access token in its environment (#358).

    source scripts/load-env.sh
    bash scripts/co-pypi.sh uv lock
    bash scripts/co-pypi.sh uv sync --locked

``co-core`` and ``co-core-aio`` resolve from the co-pypi Artifact Registry index
(``pyproject.toml``'s ``[[tool.uv.index]]``). It takes HTTP basic auth: the
username ``oauth2accesstoken`` and an OAuth2 access token as the password, which
uv reads from ``UV_INDEX_CO_PYPI_USERNAME`` / ``UV_INDEX_CO_PYPI_PASSWORD``. The
host has no gcloud, so this mints the token from the service-account key at
``GOOGLE_APPLICATION_CREDENTIALS`` (``/etc/watcher/.env``: the co-pypi-reader
key, ``roles/artifactregistry.reader`` on co-pypi) and ``exec``s the command
with the pair set in that child's environment alone.

**The token is never printed, logged, put in argv or written anywhere.** It
exists in this process's memory and in the wrapped ``uv``'s environment, and
dies with it (an hour's lifetime at most). Only ``uv`` is wrapped: anything else
is refused before a token is minted. ``VIRTUAL_ENV``, set by the ``uv run``
this runs under, is dropped, so the wrapped ``uv`` sees the project's ``.venv``.
**Accepted limit:** whatever that ``uv`` spawns inherits its environment, the
pair included: the PEP 517 build backend for the editable project (and ``git``,
should a git source come back). uv has no per-index credential scoping short of
a keyring or netrc file; the bound is the identity's read-only role and the
token's hour.

Only a command that downloads from the index needs this: ``uv lock``, and a
``uv sync`` that needs a co-core file neither installed nor cached. ``uv sync
--locked --offline`` (the unit's start check) and ``uv audit --frozen`` don't.

Exit codes: the wrapped command's own · ``2`` usage, a refused command or no
key variable · ``1`` the mint failed · ``127`` the command could not be run.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account

USERNAME_VAR = "UV_INDEX_CO_PYPI_USERNAME"
PASSWORD_VAR = "UV_INDEX_CO_PYPI_PASSWORD"
USERNAME = "oauth2accesstoken"
KEY_VAR = "GOOGLE_APPLICATION_CREDENTIALS"
# Artifact Registry's documented scope for a token-as-password read; the
# identity's IAM role, artifactregistry.reader, is what bounds it.
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# Set by the `uv run --no-project` this runs under, pointing at its ephemeral
# env; inherited, the wrapped `uv sync` warns and a `--active` one would sync it.
DROPPED = ("VIRTUAL_ENV",)
USAGE = "usage: bash scripts/co-pypi.sh uv <command> [args...]"


def mint_token(key_path: str) -> str:
    """An access token for the service account whose key is at *key_path*."""
    credentials = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("the token endpoint returned no access token")
    return credentials.token


def main(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    mint: Callable[[str], str] = mint_token,
    execvpe: Callable[[str, list[str], dict[str, str]], object] = os.execvpe,
) -> int | None:
    """Exec *argv* (a ``uv`` command) with the co-pypi pair in its environment.

    Returns an exit code on failure; on success *execvpe* replaces the process,
    so in production nothing returns.
    """
    if not argv or Path(argv[0]).name != "uv":
        print(f"error: co-pypi wraps uv alone. {USAGE}", file=sys.stderr)
        return 2

    key_path = environ.get(KEY_VAR, "").strip()
    if not key_path:
        print(
            f"error: {KEY_VAR} is not set: it names the co-pypi-reader key."
            " Load it first: source scripts/load-env.sh",
            file=sys.stderr,
        )
        return 2

    try:
        token = mint(key_path)
    except Exception as exc:  # broad by design: any failure here runs nothing
        # The exception is the token endpoint's or the key file's, never the token.
        print(
            f"error: could not mint a co-pypi access token from {KEY_VAR}={key_path}:"
            f" {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    env = {k: v for k, v in environ.items() if k not in DROPPED}
    env[USERNAME_VAR] = USERNAME
    env[PASSWORD_VAR] = token
    try:
        execvpe(argv[0], list(argv), env)
    except OSError as exc:
        # Names the command and errno, never `env`.
        print(f"error: could not run {argv[0]}: {exc.strerror or exc}", file=sys.stderr)
        return 127
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
