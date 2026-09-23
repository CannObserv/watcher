"""Parse systemd unit text the way the deploy tests need it.

Shared by ``test_installed_unit_matches_repo`` and ``test_memory_dropins``: both
read ``deploy/`` unit files, and a sign or suffix parsed differently in each
would let one assert what the other contradicts.
"""


def directive_values(unit_text: str, directive: str) -> list[str]:
    """Return every value a directive is given, verbatim and in file order.

    Verbatim because a leading ``-`` is not punctuation in general: it is an
    ``EnvironmentFile=`` prefix meaning "skip if missing", but it is the sign of
    the number in ``OOMScoreAdjust=-500``. Stripping it here once cost that
    assertion its meaning — it read -500 as 500 and passed a unit that had made
    the service *more* attractive to the OOM killer, not less. The one caller
    that wants the prefix gone strips it itself.
    """
    prefix = f"{directive}="
    values: list[str] = []
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            values.append(stripped.removeprefix(prefix))
    return values


def memory_size_to_bytes(value: str) -> int:
    """Parse a systemd memory size (``512M``, ``1G``, ``1048576``) into bytes.

    Only the suffixes systemd itself documents for ``MemoryLow=`` are accepted.
    An unparseable value raises rather than defaulting to zero: a typo in a
    reservation must fail this test loudly, not quietly assert nothing.
    """
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    suffix = value[-1:].upper()
    if suffix in multipliers:
        return int(value[:-1]) * multipliers[suffix]
    return int(value)
