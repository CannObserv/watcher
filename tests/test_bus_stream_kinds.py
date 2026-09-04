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

ROOT = pathlib.Path(__file__).resolve().parent.parent
# `src/` is where the bus code lives today, but the hazard's boundary is anything
# that can reach the broker, and #262's gate exists precisely because "an agent
# shell, a one-off script, a python -c" can (CR-30). Nothing under scripts/ touches
# the bus yet; this is what notices the first one that does.
SCANNED_ROOTS = (ROOT / "src", ROOT / "scripts")
STREAMS_MODULE = "co_core.pure.adapters.bus.streams"
STREAMS_PACKAGE = "co_core.pure.adapters.bus"

# The helper that derives a conforming consumer-group name. One-element sets on
# both sides because there is one right answer today; they are sets so adding a
# second resolver is a data change rather than an edit to the check.
GROUP_NAME_DERIVERS = frozenset({"group_name"})

# The resolvers that guarantee a *bounded* positive cap. Any other call satisfies
# "maxlen comes from somewhere" while guaranteeing nothing — and a resolver that
# can return 0 emits `XADD MAXLEN 0`, trimming a config/state stream to a single
# entry so a consumer's replay-from-0-0 returns a partial set it cannot tell from
# a complete one. That is the silent failure the taxonomy exists to prevent (CR-28).
BOUNDED_MAXLEN_RESOLVERS = frozenset({"resolve_stream_maxlen"})

# Every stream Watcher touches: four published, two consumed. AGENTS.md and
# ARCHITECTURE.md say exactly that in prose; asserting the set — not just each
# kind — is what makes an *addition* fail here rather than drift silently (CR-29).
WATCHER_STREAMS = frozenset(
    {
        streams.CONTENT_BLOBS,
        streams.CONTENT_REVISIONS,
        streams.CONTENT_FETCH,
        streams.CONTENT_FETCH_POLICY,
        streams.INFO_REGISTRY,
        streams.INFO_WATCH_STATUS,
    }
)

# Every canonical stream name, so a topic written as a bare string still resolves.
STREAM_VALUES = frozenset(
    value
    for name, value in vars(streams).items()
    if name.isupper() and not name.startswith("_") and isinstance(value, str)
)


def _modules():
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            yield path, ast.parse(path.read_text())


def _stream_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name → streams attribute, for `from ...streams import CONTENT_BLOBS`.

    Without this a call site that imports the constant directly resolves to
    nothing, and a rule that skips what it cannot resolve blesses it (CR-22).
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == STREAMS_MODULE:
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _module_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to the streams *module*, however it was imported.

    Hard-coding ``"streams"`` made ``import ... as s`` resolve to nothing, which
    every rule then reports as "topic is not a streams constant" — a false
    positive accusing the author of the wrong mistake (CR-31).
    """
    names = {"streams"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == STREAMS_MODULE:
                    names.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom) and node.module == STREAMS_PACKAGE:
            for alias in node.names:
                if alias.name == "streams":
                    names.add(alias.asname or alias.name)
    return names


def _resolve_topic(
    node: ast.expr | None, aliases: dict[str, str], modules: set[str] | None = None
) -> str | None:
    """The canonical stream an expression names, or None if the guard cannot tell.

    Three forms, because all three are things a person writes: a qualified
    ``<module>.X`` under any local module name, a bare constant imported from
    that module, and a literal.
    """
    modules = modules if modules is not None else {"streams"}
    candidate = None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id in modules:
            candidate = getattr(streams, node.attr, None)
    elif isinstance(node, ast.Name) and node.id in aliases:
        candidate = getattr(streams, aliases[node.id], None)
    elif isinstance(node, ast.Constant):
        candidate = node.value
    # The module exports helpers and type aliases beside the stream constants, so
    # a name resolving to *something* is not the same as resolving to a stream —
    # `streams.group_name` and an imported `group_name` both resolve to a function.
    return candidate if candidate in STREAM_VALUES else None


def _topic_arg(call: ast.Call, position: int | None = None) -> ast.expr | None:
    """The expression a bus call passes as its topic, unresolved."""
    for keyword in call.keywords:
        if keyword.arg == "topic":
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _callee(call: ast.Call) -> str | None:
    """The called name, qualified or not — `X()` and `mod.X()` both yield "X".

    Matching only bare names left `bus.AsyncBusConsumer(...)` invisible to every
    rule here, masked purely by there being one call site for the `assert found`
    backstop to trip on (CR-23).
    """
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(name: str):
    """Every call to ``name`` across the scanned roots, with its path and tree."""
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) == name:
                yield path, tree, node


def _assigned_values(tree: ast.Module, name: str) -> list[ast.expr]:
    """Every value bound to ``name`` anywhere in the module.

    Annotated assignments count (CR-25), and function scope counts because the
    trim cap is resolved inside the publisher, not at import.
    """
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            values.append(node.value)
    return values


def _derives_from(tree: ast.Module, name: str, allowed: frozenset[str]) -> tuple[bool, list]:
    """Whether **every** binding of ``name`` is a call to one of ``allowed``.

    ``all``, not ``any`` (CR-33): one conforming assignment said nothing about a
    later rebinding, so a derived group name followed by
    ``CONSUMER_GROUP = "watcher"`` satisfied the rule that exists to forbid
    exactly that. And callee matching goes through ``_callee``, so the qualified
    form is accepted (CR-34): ``streams.group_name(...)`` is correct code and was
    rejected by a bare ``ast.Name`` comparison.

    Both call sites share this so the two rules cannot drift apart again — they
    were written independently, which is why CR-28's strictness fix reached one
    and CR-23's qualified-callee fix reached neither. The residual looseness is
    that ``_callee`` matches on the final attribute, so an unrelated method of
    the same name would pass; that is not a state anyone reaches by accident, and
    resolving callees back to their imports is more machinery than the risk earns.

    Returns the verdict and the callee names found, so a failure can name what it
    actually saw rather than only what it wanted.
    """
    values = _assigned_values(tree, name)
    callees = [_callee(v) if isinstance(v, ast.Call) else None for v in values]
    return bool(values) and all(c in allowed for c in callees), callees


class TestReaderMatchesStreamKind:
    """The class at the call site must match the kind of the stream it reads."""

    def test_every_grouped_consumer_reads_a_grouped_stream(self):
        found = 0
        for path, tree, call in _calls("AsyncBusConsumer"):
            argument = _topic_arg(call)
            assert argument is not None, f"{path}: AsyncBusConsumer built without a topic= argument"
            topic = _resolve_topic(argument, _stream_aliases(tree), _module_aliases(tree))
            assert topic is not None, (
                f"{path}: AsyncBusConsumer topic is not a recognizable stream — name it with a "
                "streams constant so this guard can classify it"
            )
            assert stream_kind(topic) != "config_state", (
                f"{path}: {topic!r} is a config/state stream and takes no consumer group — "
                "every worker needs every message, and a group there accumulates a PEL "
                "nothing drains. Read it with AsyncBusTailReader."
            )
            found += 1
        assert found, "no AsyncBusConsumer call sites found — has the class been renamed?"

    def test_every_tail_reader_reads_a_config_state_stream(self):
        found = 0
        for path, tree, call in _calls("AsyncBusTailReader"):
            argument = _topic_arg(call)
            assert argument is not None, (
                f"{path}: AsyncBusTailReader built without a topic= argument"
            )
            topic = _resolve_topic(argument, _stream_aliases(tree), _module_aliases(tree))
            assert topic is not None, (
                f"{path}: AsyncBusTailReader topic is not a recognizable stream — name it with a "
                "streams constant so this guard can classify it"
            )
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
            derived, callees = _derives_from(tree, group.id, GROUP_NAME_DERIVERS)
            assert derived, (
                f"{path}: every binding of {group.id} must be a "
                f"{'/'.join(sorted(GROUP_NAME_DERIVERS))}() call — found {callees!r}. A later "
                "rebinding to a literal is the free-string defect this rule exists to forbid, "
                "and an earlier derived assignment does not excuse it."
            )
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
        for path, tree, call in _calls("BusPublish"):
            argument = _topic_arg(call, position=0)
            assert argument is not None, f"{path}: BusPublish built without a topic"
            topic = _resolve_topic(argument, _stream_aliases(tree), _module_aliases(tree))
            assert topic is not None, (
                f"{path}: BusPublish topic is not a recognizable stream, so this guard "
                "cannot tell whether it needs a trim. Name it with a streams constant — "
                "skipping what it cannot classify is how a config/state publish evades "
                "this rule entirely (CR-22)."
            )
            if stream_kind(topic) != "config_state":
                continue
            maxlen = next((k.value for k in call.keywords if k.arg == "maxlen"), None)
            assert maxlen is not None, (
                f"{path}: publish to config/state stream {topic!r} without maxlen. The "
                "republished full set grows without bound and the boot replay grows with "
                "it; the trim belongs on the publish, not on an operator's XTRIM."
            )
            # Presence is not the contract — `maxlen=None` is BusPublish's own default
            # and trims nothing, so the rule would bless a disabled trim while reading
            # as enforced (CR-24).
            if isinstance(maxlen, ast.Constant):
                assert isinstance(maxlen.value, int) and maxlen.value > 0, (
                    f"{path}: maxlen={maxlen.value!r} on {topic!r} trims nothing. "
                    "None is BusPublish's default and means unbounded."
                )
            else:
                assert isinstance(maxlen, ast.Name), f"{path}: unexpected maxlen expression"
                bounded, callees = _derives_from(tree, maxlen.id, BOUNDED_MAXLEN_RESOLVERS)
                assert bounded, (
                    f"{path}: every binding of {maxlen.id} must come from a bounded resolver "
                    f"({', '.join(sorted(BOUNDED_MAXLEN_RESOLVERS))}) — found {callees!r}. Any "
                    "other call satisfies 'maxlen comes from somewhere' while guaranteeing "
                    "nothing, and a value of 0 trims the stream to one entry, so a boot replay "
                    "returns a partial set no consumer can tell from a complete one."
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
                        if node.attr.startswith("_"):
                            continue  # co-core's private kind table, not a stream
                        referenced.add(getattr(streams, node.attr))
        assert referenced, "no streams.* references found"
        for topic in sorted(referenced):
            assert stream_kind(topic) in {"command", "fact", "config_state"}

    def test_the_inventory_is_exactly_what_the_docs_describe(self):
        """AGENTS.md and ARCHITECTURE.md say four published and two consumed.
        Pinning each stream's *kind* catches a co-core reclassification but not
        an addition, and the prose is wrong either way (CR-29)."""
        referenced = set()
        for _path, tree in _modules():
            aliases, modules = _stream_aliases(tree), _module_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute | ast.Name | ast.Constant):
                    topic = _resolve_topic(node, aliases, modules)
                    if topic is not None:
                        referenced.add(topic)
        assert referenced == WATCHER_STREAMS, (
            "the set of streams Watcher touches changed — update the inventory here and "
            "the 'publishes four streams and consumes two' prose in AGENTS.md and "
            "docs/ARCHITECTURE.md, which this set exists to keep honest"
        )

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


class TestTheScannerSeesEveryCallForm:
    """The rules above were mutation-verified, but only against call sites
    written in the style already present — which proved the rules right without
    proving the *scan* complete. These pin the scan itself, because a guard that
    cannot see a violation is worse than no guard: it gets cited as coverage.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("BusPublish(streams.INFO_REGISTRY, f)", streams.INFO_REGISTRY),
            (
                "from co_core.pure.adapters.bus.streams import INFO_REGISTRY\n"
                "BusPublish(INFO_REGISTRY, f)",
                streams.INFO_REGISTRY,
            ),
            (
                "from co_core.pure.adapters.bus.streams import INFO_REGISTRY as R\n"
                "BusPublish(R, f)",
                streams.INFO_REGISTRY,
            ),
            ('BusPublish("info.registry", f)', streams.INFO_REGISTRY),
        ],
        ids=["qualified", "bare-import", "aliased-import", "literal"],
    )
    def test_a_topic_resolves_however_it_is_written(self, source, expected):
        """CR-22: the bare-import form is what let a config/state publish with
        no trim pass the whole suite — the rule skipped what it could not
        resolve, and blessed it by skipping."""
        tree = ast.parse(source)
        (call,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        assert _resolve_topic(_topic_arg(call, position=0), _stream_aliases(tree)) == expected

    def test_an_unrecognizable_topic_resolves_to_nothing(self):
        """And the rules assert on that rather than continuing past it."""
        tree = ast.parse("BusPublish(some_variable, f)")
        (call,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        assert _resolve_topic(_topic_arg(call, position=0), _stream_aliases(tree)) is None

    @pytest.mark.parametrize(
        "source",
        [
            "AsyncBusConsumer(c, topic=t, group=g, consumer=n)",
            "bus.AsyncBusConsumer(c, topic=t, group=g, consumer=n)",
            "co_core_aio.bus.AsyncBusConsumer(c, topic=t, group=g, consumer=n)",
        ],
        ids=["bare", "module-qualified", "fully-qualified"],
    )
    def test_a_qualified_callee_is_still_matched(self, source):
        """CR-23: matching only bare names left `bus.AsyncBusConsumer(...)`
        invisible to every rule. It was caught once only because a single call
        site made `assert found` trip on zero — incidental, and gone the moment
        a second consumer exists."""
        (call,) = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
        assert _callee(call) == "AsyncBusConsumer"

    @pytest.mark.parametrize(
        "source",
        ["X = f()", "X: str = f()", "def g():\n    X = f()"],
        ids=["assign", "annotated", "function-scope"],
    )
    def test_assignments_are_found_in_every_form(self, source):
        """CR-25: an annotated assignment used to read as 'not assigned at
        module level', accusing the author of the wrong mistake."""
        values = _assigned_values(ast.parse(source), "X")
        assert values and all(isinstance(v, ast.Call) for v in values)

    @pytest.mark.parametrize(
        "source",
        [
            "from co_core.pure.adapters.bus import streams as s\nBusPublish(s.INFO_REGISTRY, f)",
            "import co_core.pure.adapters.bus.streams as s\nBusPublish(s.INFO_REGISTRY, f)",
            "import co_core.pure.adapters.bus.streams\nBusPublish(streams.INFO_REGISTRY, f)",
        ],
        ids=["from-import-as", "import-as", "import-plain"],
    )
    def test_the_streams_module_resolves_under_any_local_name(self, source):
        """CR-31: hard-coding the name `streams` made an aliased import resolve
        to nothing, which every rule then reported as 'not a streams constant' —
        accusing the author of the wrong mistake."""
        tree = ast.parse(source)
        (call,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        topic = _resolve_topic(
            _topic_arg(call, position=0), _stream_aliases(tree), _module_aliases(tree)
        )
        assert topic == streams.INFO_REGISTRY

    def test_a_non_stream_export_does_not_resolve_as_a_topic(self):
        """The streams module exports helpers beside the constants, so resolving
        to *something* is not resolving to a stream — `group_name` resolved to a
        function object and landed in the inventory set."""
        tree = ast.parse("from co_core.pure.adapters.bus.streams import group_name\nf(group_name)")
        (call,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        aliases, modules = _stream_aliases(tree), _module_aliases(tree)
        assert _resolve_topic(call.args[0], aliases, modules) is None
        assert (
            _resolve_topic(ast.parse("streams.group_name").body[0].value, aliases, modules) is None
        )

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("X = group_name(a, b)", True),
            ("X = streams.group_name(a, b)", True),
            ("X = mod.streams.group_name(a, b)", True),
            ('X = group_name(a, b)\nX = "watcher"', False),
            ('X = "watcher"', False),
            ("X = other_call()", False),
        ],
        ids=["bare", "qualified", "deeply-qualified", "rebound", "literal", "wrong-callee"],
    )
    def test_derivation_requires_every_binding_to_be_a_derivation(self, source, expected):
        """CR-33/CR-34, in one place because the two rules that need this were
        written separately and drifted: ``any`` let a rebinding through, and a
        bare-name callee match rejected the qualified form of the very call the
        rule demands."""
        derived, _callees = _derives_from(ast.parse(source), "X", frozenset({"group_name"}))
        assert derived is expected

    def test_derivation_reports_what_it_found(self):
        """A failure should name the callee it saw, not only the one it wanted."""
        _derived, callees = _derives_from(
            ast.parse("X = group_name(a, b)\nX = int(y)"), "X", frozenset({"group_name"})
        )
        assert callees == ["group_name", "int"]
