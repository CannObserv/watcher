"""The User-Agent literal is load-bearing — pin it (CR-23).

Deliberately a **unit** test, not part of the integration-marked
``test_fetch_commands`` module: CI's test job runs ``pytest -m "not
integration"``, so a guard living there would never run where it matters.
"""

from src.core.fetch_commands import WATCHER_USER_AGENT
from src.core.probe import PROBE_USER_AGENT


def test_user_agent_literal_is_pinned():
    """Fingerprints are UA-sensitive; this value re-baselines the whole fleet.

    Changing this string means the next check of **every** watched item reports
    a spurious change and fires a real notification to every configured
    channel. The wiring assertion in ``test_fetch_commands`` compares the header
    against this same constant, so it passes for any value — this is the guard.

    If you are here because this test failed: that is the point. Changing the UA
    is a fleet-wide re-baseline, not a version bump. Do it deliberately, with a
    plan for the notification storm — see the cutover section of
    ``docs/plans/2026-08-06-phase-4-content-fetch-producer-design.md``, whose
    "no re-baseline needed" result depended on this value staying constant.
    """
    assert WATCHER_USER_AGENT == "watcher/0.1.0"


def test_probe_user_agent_derives_from_the_fetch_user_agent():
    """One version string, two UAs (CR-26) — they must not drift apart."""
    assert PROBE_USER_AGENT == f"{WATCHER_USER_AGENT} (probe)"
