"""Watcher's bus code must agree with co-core's stream taxonomy (#285).

cannobserv#384 made the three stream kinds machine-readable — ``stream_kind``
returns ``command`` / ``fact`` / ``config_state`` for any canonical stream — and
co-core's ``streams`` docstring says to *branch on the kind* rather than
pattern-match a name. Watcher does not pattern-match, but until now nothing
checked the kinds either: the correspondence between a stream and how Watcher
reads or writes it lived only in the choice of class at each call site.

What the kind actually decides, and therefore what these tests pin:

* **Group cardinality.** A config/state stream takes **no** consumer group —
  every worker needs every message, and a group there accumulates a PEL nothing
  drains. ``group_name`` already raises on one, which blocks the hazard through
  that path; nothing blocked a literal group string handed straight to
  ``AsyncBusConsumer``.
* **Which reader applies.** Config/state is replayed from ``0-0`` with
  ``AsyncBusTailReader``; reading one from ``$`` fails **silently** — a booting
  worker sees nothing, indistinguishable from a genuinely empty stream.
* **Retention.** On a config/state stream retention is a contract property, not
  a deployment knob: the producer republishes the full set, so the trim rides on
  the publish (``BusPublish.maxlen``) rather than an out-of-band operator
  ``XTRIM``. A config/state publish without ``maxlen`` grows without bound and
  the replay-at-boot cost rises forever.

These read the source rather than the runtime, because the failure they guard
against is a *new* call site written the wrong way — which no existing test
would exercise. Each one asserts it found something, so a rename of the co-core
classes fails loudly instead of passing vacuously.
"""

import ast
import pathlib

import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.streams import stream_kind

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text())


def _topic_of(call: ast.Call, position: int | None = None) -> str | None:
    """The canonical stream a bus call names, or None if it names none.

    Accepts ``topic=streams.X`` and, for ``BusPublish``, the positional first
    argument. A topic that is not a ``streams.<CONST>`` attribute yields None —
    the separate coverage test is what refuses those.
    """
    node = None
    for keyword in call.keywords:
        if keyword.arg == "topic":
            node = keyword.value
    if node is None and position is not None and len(call.args) > position:
        node = call.args[position]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "streams":
            return getattr(streams, node.attr)
    return None


def _calls(name: str):
    """Every call to ``name`` across src/, with its module path and tree."""
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == name:
                    yield path, tree, node


class TestReaderMatchesStreamKind:
    """The class at the call site must match the kind of the stream it reads."""

    def test_every_grouped_consumer_reads_a_grouped_stream(self):
        found = 0
        for path, _tree, call in _calls("AsyncBusConsumer"):
            topic = _topic_of(call)
            assert topic is not None, f"{path}: AsyncBusConsumer topic is not a streams constant"
            assert stream_kind(topic) != "config_state", (
                f"{path}: {topic!r} is a config/state stream and takes no consumer group — "
                "every worker needs every message, and a group there accumulates a PEL "
                "nothing drains. Read it with AsyncBusTailReader."
            )
            found += 1
        assert found, "no AsyncBusConsumer call sites found — has the class been renamed?"

    def test_every_tail_reader_reads_a_config_state_stream(self):
        found = 0
        for path, _tree, call in _calls("AsyncBusTailReader"):
            topic = _topic_of(call)
            assert topic is not None, f"{path}: AsyncBusTailReader topic is not a streams constant"
            assert stream_kind(topic) == "config_state", (
                f"{path}: {topic!r} is a {stream_kind(topic)} stream. A tail reader replays it "
                "from 0-0 with no group and no ack, so a fact or command read this way is "
                "re-processed on every boot and never acknowledged."
            )
            found += 1
        assert found, "no AsyncBusTailReader call sites found — has the class been renamed?"


class TestGroupNamesAreDerived:
    """#384's actual lesson: a convention beside a free-string parameter loses.

    All five cluster groups diverged from the written-only rule. The helper is
    the contract, so a group handed to ``AsyncBusConsumer`` must trace back to a
    ``group_name`` call — which is also what keeps the config/state guard inside
    that helper reachable at all.
    """

    def test_no_consumer_group_is_a_string_literal(self):
        found = 0
        for path, tree, call in _calls("AsyncBusConsumer"):
            group = next((k.value for k in call.keywords if k.arg == "group"), None)
            assert group is not None, f"{path}: AsyncBusConsumer built without an explicit group"
            assert not isinstance(group, ast.Constant), (
                f"{path}: consumer group is a literal. Derive it with "
                "group_name(<stream>, 'watcher') — a free string at the call site is the "
                "defect cannobserv#384 found on all five cluster groups."
            )
            assert isinstance(group, ast.Name), f"{path}: unexpected group expression"
            assigned = [
                node.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == group.id for t in node.targets)
            ]
            assert assigned, f"{path}: {group.id} is not assigned at module level"
            assert any(
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "group_name"
                for value in assigned
            ), f"{path}: {group.id} must be derived by group_name(), not written out"
            found += 1
        assert found, "no AsyncBusConsumer call sites found — has the class been renamed?"


class TestConfigStatePublishesAreTrimmed:
    """Retention on a config/state stream is a contract property, not a knob.

    Its producer republishes the full set periodically, so the stream grows
    without bound and a consumer's replay length tracks policy *history* rather
    than key count — boot cost rising forever. The trim therefore rides on the
    publish, which is what distinguishes it from ordinary Redis capacity
    management an operator could handle out of band.
    """

    def test_every_config_state_publish_passes_maxlen(self):
        checked = set()
        for path, _tree, call in _calls("BusPublish"):
            topic = _topic_of(call, position=0)
            if topic is None or stream_kind(topic) != "config_state":
                continue
            assert any(k.arg == "maxlen" for k in call.keywords), (
                f"{path}: publish to config/state stream {topic!r} without maxlen. The "
                "republished full set grows without bound and the boot replay grows with "
                "it; the trim belongs on the publish, not on an operator's XTRIM."
            )
            checked.add(topic)
        assert checked, "no config/state publishes found — has BusPublish been renamed?"


class TestTaxonomyCoverage:
    """A stream Watcher touches but co-core does not classify is the drift the
    kind table exists to prevent — ``stream_kind`` raises on one, which is the
    assertion here."""

    def test_every_stream_watcher_references_is_classifiable(self):
        referenced = set()
        for _path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == "streams" and node.attr.isupper():
                        referenced.add(getattr(streams, node.attr))
        assert referenced, "no streams.* references found in src/"
        for topic in sorted(referenced):
            assert stream_kind(topic) in {"command", "fact", "config_state"}

    @pytest.mark.parametrize(
        ("topic", "kind"),
        [
            (streams.CONTENT_BLOBS, "fact"),
            (streams.CONTENT_REVISIONS, "fact"),
            (streams.CONTENT_FETCH, "command"),
            (streams.CONTENT_FETCH_POLICY, "config_state"),
            (streams.INFO_REGISTRY, "config_state"),
            (streams.INFO_WATCH_STATUS, "config_state"),
        ],
    )
    def test_watchers_inventory_has_the_kinds_the_docs_claim(self, topic, kind):
        """AGENTS.md and ARCHITECTURE.md describe this inventory in prose —
        four published streams and two consumed, one of the consumed pair
        groupless. If co-core reclassifies one, that prose is wrong and this
        fails rather than the description quietly drifting."""
        assert stream_kind(topic) == kind
