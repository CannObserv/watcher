"""The dashboard's public base URL — where a notification's links point (#296 D6).

This used to be a constant naming the shared VM (``https://watcher.exe.xyz``).
Two things made that wrong. The service moves to ``co-watcher``, so the host is
per deployment. And every process built its links from the constant, so a dev
server's notifications pointed readers at the production dashboard — at item
ids that exist only in the dev database.

Unset means **no link**, never a guessed host: a relative ``/watched-items/…``
is useless in Slack or email, and a default would keep pointing at whatever VM
last served the dashboard. Archiver's ``ARCHIVER_PUBLIC_BASE_URL`` makes the
same call (its ``dashboard_url`` is null when unconfigured). Read per call, not
at import, so a test or a restarted process sees the current value.
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

from src.core.logging import get_logger

logger = get_logger(__name__)

PUBLIC_BASE_URL_ENV = "WATCHER_PUBLIC_BASE_URL"

#: Spelled here rather than imported from ``src.core.notifier_client``: this
#: module is imported by the notification render path, which must stay light,
#: and the name is a contract with ``deploy/watcher.service`` either way.
_NOTIFIER_ENABLED_ENV = "WATCHER_NOTIFIER_ENABLED"


class PublicBaseUrlInvalid(ValueError):
    """``WATCHER_PUBLIC_BASE_URL`` is set, but not to an absolute http(s) base."""


def public_base_url(environ: Mapping[str, str]) -> str | None:
    """Return the configured base without a trailing slash, or ``None`` if unset.

    Accepts ``http``/``https`` with a host, and an optional numeric port and
    path prefix (the dev server lives on ``:8001``). Refuses, always as
    ``PublicBaseUrlInvalid`` — never a bare ``ValueError`` from ``urlsplit`` or
    ``.port``, which the lifespan and the render path would not recognise:

    - a ``?`` or ``#`` anywhere, since every caller appends ``/watched-items/…``
      and either would swallow it — bare ones included, which ``urlsplit``
      reports as empty;
    - a username or password, which every link would publish;
    - whitespace, which ``urlsplit`` keeps in the host.

    A value holding ``@`` is never quoted back: the refusal is logged CRITICAL.
    """
    raw = (environ.get(PUBLIC_BASE_URL_ENV) or "").strip()
    if not raw:
        return None
    shown = "(value withheld: it holds an '@')" if "@" in raw else repr(raw)
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises on a non-numeric or out-of-range port
    except ValueError as exc:
        raise PublicBaseUrlInvalid(
            f"{PUBLIC_BASE_URL_ENV}={shown} does not parse as a URL ({exc})"
        ) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PublicBaseUrlInvalid(
            f"{PUBLIC_BASE_URL_ENV}={shown} is not an absolute http(s) URL with a host "
            "(e.g. https://co-watcher.exe.xyz)"
        )
    if parts.username is not None or parts.password is not None:
        raise PublicBaseUrlInvalid(
            f"{PUBLIC_BASE_URL_ENV} carries a username or password; every link would publish it"
        )
    if "?" in raw or "#" in raw:
        raise PublicBaseUrlInvalid(
            f"{PUBLIC_BASE_URL_ENV}={shown} carries a query or fragment; links are built "
            "by appending a path, so it must be a bare base"
        )
    if any(char.isspace() for char in raw):
        raise PublicBaseUrlInvalid(f"{PUBLIC_BASE_URL_ENV}={shown} contains whitespace")
    return raw.rstrip("/")


def assert_public_base_url(environ: Mapping[str, str]) -> None:
    """Startup check: refuse a malformed base, and say so when links will be absent.

    A malformed value is a typo, so it raises — the lifespan logs it CRITICAL and
    refuses to start, like the other environment guards. An *absent* value only
    degrades notifications (they lose their dashboard link), so it is a WARNING,
    and only when the notifier is enabled: with no notifications there are no
    links to build, and silence is correct.
    """
    if public_base_url(environ) is None and environ.get(_NOTIFIER_ENABLED_ENV) == "1":
        logger.warning(
            "%s is not set — notifications will be sent without a dashboard link",
            PUBLIC_BASE_URL_ENV,
        )
