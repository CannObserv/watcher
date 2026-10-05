"""The change diff (#222): previous vs current canonical text, at dispatch time.

The diff target is the canonical extracted text (cannobserv#486) — the exact
bytes a ``ChangeRevision.content_fingerprint`` hashes. The processor stores
that text permanently, content-addressed, so a fingerprint *is* the address of
the text it names (design 2026-09-24, Section 4). Nothing here persists a diff:
it is computed per event, rendered per recipient, and handed to the notifier.

**Where the text lives** comes from the processor's own answer:
``process_commands.output_uri`` on a completed row whose ``output_digest`` is
the fingerprint. No bucket is configured on this side, and a fingerprint the
processor never answered for (a revision older than shadow mode) has no
location — the diff is then unavailable, never guessed.

**The current text may be in hand.** Local and shadow extraction notify before
the processor has answered for this occasion, so its text is not stored yet;
the pipeline passes the bytes it just fingerprinted instead. They are trusted
only if they hash to the event's ``current_fingerprint``.

**Every read is hash-checked, every input capped, ``difflib`` runs in a
thread** (one process serves the API, the consumers and the tasks), and
**nothing raises**: any failure is a ``ChangeDiff`` naming why, the
notification still goes out, and a WARNING says what broke.
"""

import asyncio
import difflib
import re
import textwrap
from dataclasses import dataclass

from co_core.pure.util.hashing import prefixed_sha256, sha256
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.content_config import ContentOptions
from src.core.blobs import BlobReadError, aread_blob
from src.core.fetch_commands import env_number
from src.core.logging import get_logger
from src.core.models.process_command import ProcessCommand, ProcessCommandStatus

logger = get_logger(__name__)

DIFF_MAX_INPUT_BYTES_ENV = "WATCHER_DIFF_MAX_INPUT_BYTES"
DEFAULT_DIFF_MAX_INPUT_BYTES = 1024 * 1024

# The template variables that carry a diff; a custom body naming neither has
# no use for one.
DIFF_TEMPLATE_VARIABLES = ("diff_snippet", "diff_full")

# The diff's unit is a sentence, not a line. A chunk's text is one
# whitespace-collapsed line, and a spec usually selects one element — every
# live item extracted to a single 8–17 KB line on 2026-10-05 — so a line diff
# would print the whole page as one `-` and one `+` line, and the snippet's
# line cap would bound nothing. A sentence end is `.`/`!`/`?` before
# whitespace; a run with none is wrapped at word boundaries so no diff line
# outgrows MAX_SEGMENT_CHARS.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MAX_SEGMENT_CHARS = 400


@dataclass(frozen=True)
class ChangeDiff:
    """One event's diff, or why there is none.

    ``unified`` is ``difflib``'s unified diff over the two canonical texts;
    ``unavailable`` is set exactly when it could not be produced.
    """

    unified: str = ""
    unavailable: str | None = None


@dataclass(frozen=True)
class StoredText:
    """Where the processor stored one canonical text, and its recorded size."""

    uri: str
    size_bytes: int | None


class _Unavailable(Exception):
    """A diff that cannot be made, with the reason a reader is shown."""


def diff_max_input_bytes() -> int:
    """The largest text either side may be; over it, the diff is unavailable."""
    return env_number(
        DIFF_MAX_INPUT_BYTES_ENV,
        DEFAULT_DIFF_MAX_INPUT_BYTES,
        int,
        warn_non_positive="every change diff is unavailable",
    )


def diff_requested(options: ContentOptions) -> bool:
    """Whether a recipient with these options would show a diff.

    A custom body ignores the toggles (``build_body``), so it asks for one only
    by naming a diff variable.
    """
    if options.body_template:
        return any(name in options.body_template for name in DIFF_TEMPLATE_VARIABLES)
    return options.include_diff_snippet or options.include_diff_full


def _segments(text: str) -> list[str]:
    """The text as diff lines: chunk lines, split into sentences, long ones wrapped.

    Chunk boundaries (the canonical ``\\n``) always end a segment. Wrapping
    shifts after an edit, so a change inside a long unpunctuated run shows that
    whole run — bounded by the run, never the page.
    """
    out: list[str] = []
    for line in text.splitlines():
        for sentence in _SENTENCE_END.split(line):
            if len(sentence) <= MAX_SEGMENT_CHARS:
                out.append(sentence)
            else:
                out.extend(
                    textwrap.wrap(sentence, MAX_SEGMENT_CHARS, break_on_hyphens=False) or [sentence]
                )
    return out


def compute_unified_diff(previous: bytes, current: bytes) -> str:
    """Unified diff of two canonical texts, by sentence. Pure CPU — callers off
    the loop use a thread.

    ``lineterm=""`` and a ``\\n`` join: every line carries its own prefix, so
    no empty line ever appears between content lines. Hunk ranges count
    segments (``_segments``), not lines of the stored text.
    """
    before = _segments(previous.decode("utf-8", errors="replace"))
    after = _segments(current.decode("utf-8", errors="replace"))
    return "\n".join(
        difflib.unified_diff(before, after, fromfile="previous", tofile="current", lineterm="")
    )


async def stored_text_location(session: AsyncSession, fingerprint: str) -> StoredText | None:
    """The processor's stored copy of the text ``fingerprint`` names, or ``None``.

    Any completed answer will do: the store is content-addressed and
    write-if-absent, so every row reporting this digest names the same bytes.

    **Under a savepoint** (CR 1): the session is the pipeline's, mid-apply. A
    server-side error would abort its whole transaction, and the revision and
    audit rows the caller has yet to commit would go with it — swallowing the
    exception here is not enough; the rollback must stop at the savepoint.
    """
    async with session.begin_nested():
        row = (
            await session.execute(
                select(ProcessCommand.output_uri, ProcessCommand.output_size_bytes)
                .where(
                    ProcessCommand.output_digest == fingerprint,
                    ProcessCommand.status == ProcessCommandStatus.COMPLETED,
                    ProcessCommand.output_uri.is_not(None),
                )
                .order_by(ProcessCommand.fact_at.desc().nulls_last())
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    return StoredText(uri=row.output_uri, size_bytes=row.output_size_bytes)


def _address(text: bytes) -> str:
    return prefixed_sha256(sha256(text))


async def _read_stored(session: AsyncSession, fingerprint: str, side: str, cap: int) -> bytes:
    """One side's text, located, size-checked, read and hash-checked."""
    stored = await stored_text_location(session, fingerprint)
    if stored is None:
        raise _Unavailable(f"{side} text not stored")
    # The recorded size spares a download that could only be refused.
    if stored.size_bytes is not None and stored.size_bytes > cap:
        raise _Unavailable("content too large")
    try:
        text = await aread_blob(stored.uri)
    except BlobReadError as exc:
        logger.warning(
            "stored text unreadable — no diff",
            extra={"side": side, "fingerprint": fingerprint, "error": str(exc)},
        )
        raise _Unavailable(f"{side} text unreadable") from exc
    if len(text) > cap:
        raise _Unavailable("content too large")
    if _address(text) != fingerprint:
        logger.warning(
            "stored text failed its hash check — no diff",
            extra={"side": side, "fingerprint": fingerprint, "uri": stored.uri},
        )
        raise _Unavailable(f"{side} text failed its hash check")
    return text


async def load_change_diff(
    session: AsyncSession, metadata: dict, *, current_text: bytes | None = None
) -> ChangeDiff | None:
    """The diff for one ``change_detected`` event; never raises.

    ``None`` when the event names no ``previous_fingerprint`` /
    ``current_fingerprint`` pair — nothing to diff, so nothing to say (a test
    notification). Otherwise a ``ChangeDiff``: the diff, or why there is none.
    """
    previous_fp = metadata.get("previous_fingerprint")
    current_fp = metadata.get("current_fingerprint")
    if not previous_fp or not current_fp:
        return None
    try:
        cap = diff_max_input_bytes()
        previous = await _read_stored(session, previous_fp, "previous", cap)
        if current_text is not None and _address(current_text) == current_fp:
            if len(current_text) > cap:
                raise _Unavailable("content too large")
            current = current_text
        else:
            current = await _read_stored(session, current_fp, "current", cap)
        unified = await asyncio.to_thread(compute_unified_diff, previous, current)
    except _Unavailable as exc:
        logger.info(
            "change diff unavailable",
            extra={"reason": str(exc), "previous_fingerprint": previous_fp},
        )
        return ChangeDiff(unavailable=str(exc))
    except Exception:
        logger.warning(
            "change diff failed — sending without it",
            extra={"previous_fingerprint": previous_fp, "current_fingerprint": current_fp},
            exc_info=True,
        )
        return ChangeDiff(unavailable="error")
    return ChangeDiff(unified=unified)
