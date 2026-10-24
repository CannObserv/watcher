"""The patching-hosts knob reads clean, and keeps the decisions it records (#382).

``.skills/patching-hosts`` tells the vendored ``patching-hosts`` skill what only
this repo's owner knows: the window, the backup that stands in for a dump, the
command that says whether a job is running. **A malformed line makes the host
report-only** until it is fixed, which reads as "nothing to do" at the next
monthly run rather than as an error, so the skill's own reader is run here.

The reader ships in the submodule, which CI's checkout does not initialise: off
the host that is a skip, on it a failure. The rest are pure parses of the
tracked files, run everywhere, plus the installed apt pins on the host:

- **The window is Mon 01:00-03:00 UTC**, the owner's Sun 17:00-19:00 Pacific
  once DST ends (decided 2026-10-23, epic #394), and clear of both timers.
- **No knob command escalates**: ``inflight`` and ``health`` run as the
  invoking user by design, so a ``sudo`` in one hands root to the knob's
  committers.
- **``inflight`` is ``src.ops.inflight``**, from the venv, reading the URL from
  the env file: never ``uv run``, which syncs the service's environment.
- **The backup stands in for ``watcher`` alone.** A ``backup`` line covers a
  whole datastore unit, and ``watcher-backup.service`` dumps one database, so
  the cluster's other two are exceptions, not declared databases.
- **Each followed origin is scoped by a tracked pin file**, catch-all below
  500 and its packages above (the skill's ``policy.md``, Two lanes).
- **A comment can't cut an exception short.** ``#`` after whitespace opens a
  comment, so ``(#331, #347)`` in a reason silently ends it at ``(#331,``.
- **No ``image-owner`` and no private ``caller``**: this repo is public
  (gregoryfoster/skills#350).
"""

import json
import math
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy.systemd_units import directive_values

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOB = REPO_ROOT / ".skills" / "patching-hosts"
READ_KNOB = REPO_ROOT / "skills" / "patching-hosts" / "scripts" / "read-knob.sh"
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")
PINS = REPO_ROOT / "deploy" / "apt-preferences.d"
INSTALLED_PINS = Path("/etc/apt/preferences.d")
HOST = "co-watcher"

#: Each followed origin's pin file, and the packages it was added for.
FOLLOWED = {
    "deb.nodesource.com": ("nodesource.pref", {"nodejs"}),
    "pkgs.tailscale.com": ("tailscale.pref", {"tailscale", "tailscale-archive-keyring"}),
}


def _on_host() -> bool:
    return INSTALLED_UNIT.exists()


def _strip_comment(raw: str) -> str:
    """The directive part of a line: ``#`` opens a comment at a line's start or
    after whitespace (the skill's knob.md), so a command keeps a bare ``#``."""
    words = []
    for word in raw.split():
        if word.startswith("#"):
            break
        words.append(word)
    return " ".join(words)


def _named(directive: str) -> list[list[str]]:
    lines = (_strip_comment(raw).split() for raw in KNOB.read_text().splitlines())
    return [words[1:] for words in lines if words and words[0] == directive]


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _span_minutes(value: str) -> float:
    """A timer's one-unit span (``10min``, ``1800``, ``1h``) in minutes."""
    match = re.fullmatch(r"(\d+)\s*(s|sec|min|m|h)?", value.strip())
    assert match, f"unsupported span {value!r}"
    scale = {None: 1 / 60, "s": 1 / 60, "sec": 1 / 60, "min": 1, "m": 1, "h": 60}
    return int(match.group(1)) * scale[match.group(2)]


def _timer_span(timer: str) -> tuple[str | None, int, int]:
    """A deploy timer's weekday (or None), its first and last start minute."""
    text = (REPO_ROOT / "deploy" / timer).read_text()
    (calendar,) = directive_values(text, "OnCalendar")
    match = re.fullmatch(r"(?:(\w{3}) |\*-\*-\* )?(\d\d:\d\d):00(?: UTC)?", calendar)
    assert match, f"{timer}: unexpected OnCalendar={calendar}"
    start = _minutes(match.group(2))
    slack = sum(
        _span_minutes(v)
        for d in ("RandomizedDelaySec", "AccuracySec")
        for v in directive_values(text, d)
    )
    return match.group(1), start, start + math.ceil(slack)


def _pins(path: Path) -> list[dict[str, str]]:
    stanzas = []
    for block in re.split(r"\n\s*\n", path.read_text()):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if line.strip() and not line.startswith("#")
        )
        if fields:
            stanzas.append(fields)
    return stanzas


def test_the_knob_reads_clean() -> None:
    if not READ_KNOB.is_file():
        if _on_host():
            pytest.fail(f"{READ_KNOB} missing on the host: git submodule update --init")
        pytest.skip("patching-hosts submodule not initialised")
    out = subprocess.run(
        ["bash", str(READ_KNOB), "--config", str(KNOB), "--host", HOST],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    knob = json.loads(out)
    assert knob["findings"] == []
    assert knob["report_only"] is False, knob["report_only_reasons"]
    assert knob["class"]["value"] == "production"
    assert knob["posture"]["value"] == "scheduled"
    assert [(w["weekday"], w["start"], w["end"]) for w in knob["window"]] == [
        ("Mon", "01:00", "03:00")
    ]
    assert not any(e["expired"] for e in knob["exception"])
    # The reader's reason is the whole raw reason: no comment cut one short.
    raw = {words[0]: " ".join(words[2:]) for words in _named("exception")}
    assert {e["what"]: e["reason"] for e in knob["exception"]} == raw


def test_no_exception_reason_is_cut_short_by_a_comment() -> None:
    for raw in KNOB.read_text().splitlines():
        if raw.startswith("exception "):
            assert _strip_comment(raw) == " ".join(raw.split()), (
                f"' #' opens a comment, cutting this reason short: {raw}"
            )


def test_the_window_is_the_owners_slot_in_utc() -> None:
    assert _named("window") == [["Mon", "01:00-03:00"]]


@pytest.mark.parametrize("timer", ["watcher-backup.timer", "watcher-cleanup.timer"])
def test_each_timer_is_quiet_and_outside_the_window(timer: str) -> None:
    weekday, first, last = _timer_span(timer)
    quiet = [
        (_minutes(q[0].split("-")[0]), _minutes(q[0].split("-")[1]), q[1:]) for q in _named("quiet")
    ]
    assert any(
        start <= first and last <= end and days in ([], [weekday]) for start, end, days in quiet
    ), f"{timer} starts {first}-{last} min past midnight, and no quiet line covers it"
    (window,) = _named("window")
    w_start, w_end = (_minutes(t) for t in window[1].split("-"))
    if weekday in (None, window[0]):
        assert last <= w_start or first >= w_end, f"{timer} runs inside the window"


def test_no_knob_command_escalates() -> None:
    commands = [" ".join(words) for words in _named("health") + _named("inflight")]
    assert commands, "the knob declares no command"
    for command in commands:
        for word in ("sudo", "runuser", "pkexec", "doas", "su ", "systemd-run"):
            assert word not in command, f"{word!r} in a knob command: {command}"


def test_inflight_reads_the_queue_from_the_venv() -> None:
    (inflight,) = _named("inflight")
    command = " ".join(inflight)
    assert command == (
        "cd /home/exedev/watcher && WATCHER_ALLOW_PRODUCTION_DB=1 "
        ".venv/bin/python -m src.ops.inflight --env-file /etc/watcher/.env"
    )
    assert (REPO_ROOT / "src" / "ops" / "inflight.py").is_file()


def test_the_backup_stands_in_for_the_watcher_database_alone() -> None:
    assert _named("datastore") == [["postgres", "postgresql@16-main", "watcher"]]
    assert _named("backup") == [["watcher-backup.service", "postgresql@16-main"]]
    unit = (REPO_ROOT / "deploy" / "watcher-backup.service").read_text()
    (start,) = directive_values(unit, "ExecStart")
    assert start.endswith("-m src.ops.backup --database watcher")
    excepted = {words[0] for words in _named("exception")}
    assert {"database:watcher_dev", "database:watcher_test"} <= excepted


def test_the_service_is_the_runbook_service() -> None:
    assert _named("service") == [["watcher.service"]]


def test_each_followed_origin_has_a_scoping_pin_file() -> None:
    followed = {words[0] for words in _named("origin") if words[1] == "follow"}
    assert followed == set(FOLLOWED)
    for origin, (name, packages) in FOLLOWED.items():
        stanzas = _pins(PINS / name)
        assert all(s["Pin"] == f"origin {origin}" for s in stanzas), name
        by_package = {s["Package"]: int(s["Pin-Priority"]) for s in stanzas}
        assert by_package.pop("*") < 500, f"{name}: the catch-all must sit below 500"
        assert set(" ".join(by_package).split()) == packages
        assert all(priority > 500 for priority in by_package.values()), name


def test_tailscale_restarts_in_its_own_step() -> None:
    assert ["tailscale", "tailscale-archive-keyring", "tailscale"] in _named("hold")


def test_no_private_name_is_published() -> None:
    assert _named("image-owner") == []
    assert _named("caller") == []


@pytest.mark.parametrize("name", sorted(name for name, _ in FOLLOWED.values()))
def test_installed_pins_match_tracked(name: str) -> None:
    """Gated on the host, not on the file: gating on the file under test would
    pass silently on exactly the host that never received it."""
    if not _on_host():
        pytest.skip(f"{INSTALLED_UNIT} not present: not a host running the service")
    installed = INSTALLED_PINS / name
    hint = f"Install with:\n  sudo install -m 644 {PINS / name} {installed}"
    assert installed.exists(), f"{installed} is missing.\n{hint}"
    assert installed.read_text() == (PINS / name).read_text(), f"{installed} drifted.\n{hint}"
