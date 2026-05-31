"""Layer 2 integration tests: build_flows against the Solmate test repo.

The session-scoped ``solmate_flows`` fixture lives in tests/conftest.py and
is shared across the Layer 2 test files so the forge build + Layer 1
extraction + Layer 2 build run once per pytest session.

Lookup helpers raise ``AssertionError`` with diagnostic context (available
names, tree summary) on miss — matches Layer 1's convention so future
Solmate updates that rename or remove an entry point produce immediately
diagnosable failures rather than ``StopIteration`` or empty matches.
"""

from collections.abc import Callable, Iterator

from solidity_flow_navigator.flow.types import (
    ExternalNode,
    Flow,
    FlowNode,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)

# ---------------------------------------------------------------------------
# Tree-walking and lookup helpers
# ---------------------------------------------------------------------------


def _walk(node: FlowNode) -> Iterator[FlowNode]:
    """Yield every node in the tree rooted at ``node`` in pre-order."""

    yield node
    if isinstance(node, FunctionNode):
        for child in node.children:
            yield from _walk(child)


def _flow_by_entry_point(
    flows: tuple[Flow, ...], contract_name: str, full_name: str
) -> Flow:
    """Return the Flow whose entry-point matches; raise with diagnostic on miss."""

    for f in flows:
        if (
            f.entry_point_contract_name == contract_name
            and f.root.full_name == full_name
        ):
            return f
    available = sorted(
        f"{f.entry_point_contract_name}.{f.root.full_name}" for f in flows
    )
    raise AssertionError(
        f"flow {contract_name}.{full_name} not found in build_flows output; "
        f"available entry points (first 20 of {len(available)}): "
        f"{available[:20]}"
    )


def _find_node(root: FlowNode, predicate: Callable[[FlowNode], bool]) -> FlowNode:
    """Return the first node in the tree matching ``predicate``; raise on miss."""

    for node in _walk(root):
        if predicate(node):
            return node
    summary = [type(n).__name__ for n in _walk(root)]
    raise AssertionError(
        f"no node matching predicate found in tree rooted at "
        f"{getattr(root, 'canonical_name', root)!r}; tree shape: {summary[:30]}"
    )


def _entry_point_full_names(flows: tuple[Flow, ...], contract_name: str) -> set[str]:
    """Return the full_names of all entry-point Flows for ``contract_name``."""

    return {
        f.root.full_name for f in flows if f.entry_point_contract_name == contract_name
    }


# ---------------------------------------------------------------------------
# 1. Entry-point enumeration: regular functions + synthetic getters
# ---------------------------------------------------------------------------


def test_entry_points_include_regular_and_synthetic_getters(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """ERC20's entry points contain regular mutators AND synthetic getters.

    Synthetic getters (Layer 1 fix b8b4b4f) must be indistinguishable from
    regular functions in the Flow shape — same node_type, same canonical_name
    pattern. The exact entry-point count is not asserted because the count
    grew with the getter fix; instead we assert the presence of specific names.
    """

    erc20_eps = _entry_point_full_names(solmate_flows, "ERC20")
    for required in (
        "transfer(address,uint256)",
        "transferFrom(address,address,uint256)",
        "approve(address,uint256)",
        "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
    ):
        assert required in erc20_eps, (
            f"ERC20 missing regular entry point {required!r}; "
            f"available: {sorted(erc20_eps)}"
        )

    getter_candidates = {
        "balanceOf(address)",
        "totalSupply()",
        "name()",
        "symbol()",
        "decimals()",
        "allowance(address,address)",
        "nonces(address)",
    }
    found_getters = erc20_eps & getter_candidates
    assert found_getters, (
        f"no synthetic getters in ERC20 entry points; "
        f"available: {sorted(erc20_eps)}"
    )

    # Spot-check: a getter Flow looks like a normal FunctionNode.
    flow = _flow_by_entry_point(solmate_flows, "ERC20", "balanceOf(address)")
    assert flow.root.node_type == "function"
    assert flow.root.canonical_name == "ERC20.balanceOf(address)"
    assert flow.root.declarer_contract_name == "ERC20"
    assert flow.root.is_modifier is False
    assert flow.root.view is True


# ---------------------------------------------------------------------------
# 2. Inheritance metadata
# ---------------------------------------------------------------------------


def test_inheritance_metadata_on_inherited_entry_points(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """MockERC20 inherits from ERC20; declarer/invoked_via diverge correctly.

    Uses the unfiltered fixture because the default scope filters ``Mock*``
    contracts, and the inheritance-metadata behavior under test is most
    naturally illustrated by a Mock that thinly extends ERC20.
    """

    for full_name in (
        "transfer(address,uint256)",
        "approve(address,uint256)",
    ):
        flow = _flow_by_entry_point(solmate_flows_unfiltered, "MockERC20", full_name)
        assert flow.entry_point_contract_name == "MockERC20"
        assert flow.root.declarer_contract_name == "ERC20", (
            f"MockERC20.{full_name}: expected declarer=ERC20, "
            f"got {flow.root.declarer_contract_name!r}"
        )
        assert flow.root.invoked_via_contract_name == "MockERC20", (
            f"MockERC20.{full_name}: expected invoked_via=MockERC20, "
            f"got {flow.root.invoked_via_contract_name!r}"
        )

    # MockERC20.mint is declared on MockERC20 itself, not inherited.
    mint = _flow_by_entry_point(
        solmate_flows_unfiltered, "MockERC20", "mint(address,uint256)"
    )
    assert mint.root.declarer_contract_name == "MockERC20"
    assert mint.root.invoked_via_contract_name == "MockERC20"


# ---------------------------------------------------------------------------
# 3. Empty children case
# ---------------------------------------------------------------------------


def test_empty_children_case_for_transferfrom(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """ERC20.transferFrom has no callable IR ops in Solmate — defends against
    a regression that silently drops children."""

    flow = _flow_by_entry_point(
        solmate_flows, "ERC20", "transferFrom(address,address,uint256)"
    )
    assert flow.root.children == ()


# ---------------------------------------------------------------------------
# 3b. Children are ordered by call_site_line at the data-model boundary
# (v0.6.1 invariant; v0.9.1 JS within-rank reorder depends on it)
# ---------------------------------------------------------------------------


def _csl_key(child: FlowNode) -> float:
    """Universal call-site-line accessor mirroring builder._child_source_line_key.

    FunctionNode carries the value on ``call_site_line``; UnresolvedNode /
    ExternalNode carry it on ``call_site.lines[0]``. Missing values map to
    ``math.inf`` (sort last, preserving stable-sort relative order).
    """
    import math

    if isinstance(child, FunctionNode):
        return child.call_site_line if child.call_site_line is not None else math.inf
    lines = child.call_site.lines
    return float(lines[0]) if lines else math.inf


def test_children_ordered_by_call_site_line_at_data_model_boundary(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Every FunctionNode's ``children`` is sorted by ``call_site_line``
    ascending (stable; entries with no resolvable source line sort after).
    This is the v0.6.1 invariant established in ``builder._process_calls``
    via the stable sort with ``_child_source_line_key``. The v0.9.1
    progressive renderer's within-rank reorder pass relies on this order
    to drive grouped-by-parent source-line layout.
    """
    offenders: list[str] = []
    for flow in solmate_flows:
        for node in _walk(flow.root):
            if not isinstance(node, FunctionNode):
                continue
            keys = [_csl_key(c) for c in node.children]
            if keys != sorted(keys):
                offenders.append(
                    f"{node.canonical_name}: call_site_line sequence {keys!r}"
                )
    assert not offenders, (
        f"{len(offenders)} FunctionNode(s) have children not in "
        f"call_site_line order; first 5: {offenders[:5]}"
    )


def test_children_with_non_monotonic_call_sites_are_resorted(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Find at least one FunctionNode in the Solmate fixture where adjacent
    children have *different* call_site_line values (not all identical),
    so the previous test is meaningfully exercised rather than vacuously
    true on a fixture with only single-line bodies. This is the test-
    coverage contract that supports the v0.9.1 layout change: the JS
    reorder pass only matters when source lines differ across siblings.
    """
    import math

    found = False
    for flow in solmate_flows:
        for node in _walk(flow.root):
            if not isinstance(node, FunctionNode):
                continue
            lines = {_csl_key(c) for c in node.children}
            lines.discard(math.inf)
            if len(lines) >= 2:
                found = True
                break
        if found:
            break
    assert found, (
        "no FunctionNode in Solmate fixture has 2+ children with distinct "
        "call_site_line — the ordering invariant test is vacuously true; "
        "the v0.9.1 within-rank reorder cannot be exercised without "
        "multi-call parents in the fixture"
    )


# ---------------------------------------------------------------------------
# 4. ExternalNode boundaries (lib/ and free functions)
# ---------------------------------------------------------------------------


def test_external_node_lib_path_present(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """Some Flow contains an ExternalNode whose source_path starts with lib/.

    Solmate's test contracts inherit from ds-test (under lib/ds-test/),
    producing ExternalNode children at the lib/ boundary. Uses the
    unfiltered fixture because the test-contract path that reaches lib/
    is excluded by default scope's ``**/test/**`` rule; the lib/ boundary
    behavior under test is the same in both configurations.
    """

    found: tuple[Flow, ExternalNode] | None = None
    for flow in solmate_flows_unfiltered:
        for node in _walk(flow.root):
            if isinstance(node, ExternalNode) and node.source_path.startswith("lib/"):
                found = (flow, node)
                break
        if found:
            break

    assert found is not None, "no ExternalNode with source_path under lib/ found"
    _, node = found
    # lib/ targets are functions on a contract — target_contract_name set.
    assert node.target_contract_name is not None


def test_external_node_free_function_present(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """Some Flow contains an ExternalNode for a top-level free function.

    Free functions terminate as ExternalNode regardless of source path
    (per the §11.10 v0 simplification). Their target_contract_name is None
    and target_canonical_name has no contract prefix. Uses the unfiltered
    fixture because Solmate's free-function call sites are all reachable
    only through test contracts under ``src/test/**``.
    """

    found: tuple[Flow, ExternalNode] | None = None
    for flow in solmate_flows_unfiltered:
        for node in _walk(flow.root):
            if isinstance(node, ExternalNode) and node.target_contract_name is None:
                found = (flow, node)
                break
        if found:
            break

    assert (
        found is not None
    ), "no ExternalNode with target_contract_name=None (free function) found"
    _, node = found
    # Free function canonical_name lacks a contract prefix.
    head_before_paren = node.target_canonical_name.split("(")[0]
    assert "." not in head_before_paren, (
        f"expected free-function canonical without contract prefix, got "
        f"{node.target_canonical_name!r}"
    )


# ---------------------------------------------------------------------------
# 5. UnresolvedNode reasons (Yul dispatch + low-level call)
# ---------------------------------------------------------------------------


def test_unresolved_yul_dynamic_dispatch_present(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Some Flow somewhere contains a YUL_DYNAMIC_DISPATCH unresolved node.

    SafeTransferLib bottoms out in a Yul ``call(...)`` opcode; any flow
    that recurses into safeTransferFrom/safeTransfer hits it.
    """

    found: tuple[Flow, UnresolvedNode] | None = None
    for flow in solmate_flows:
        for node in _walk(flow.root):
            if (
                isinstance(node, UnresolvedNode)
                and node.reason == UnresolvedReason.YUL_DYNAMIC_DISPATCH
            ):
                found = (flow, node)
                break
        if found:
            break

    assert found is not None, "no UnresolvedNode with reason=YUL_DYNAMIC_DISPATCH"
    _, node = found
    assert node.raw_kind == "solidity"
    assert node.raw_subkind is not None
    assert node.raw_subkind.startswith(
        ("call(", "delegatecall(", "staticcall(", "callcode(")
    )


def test_unresolved_low_level_call_present(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """Some Flow somewhere contains a LOW_LEVEL_CALL unresolved node.

    Stage 2's aggregate showed 37 of these in Solmate (e.g. DSTest.failed
    using HEVM_ADDRESS.call to load cheatcode state). Uses the unfiltered
    fixture because the reachable LOW_LEVEL_CALL sites in Solmate live in
    DSTest under lib/ and reach the call tree only via test contracts under
    ``src/test/**`` — both filtered out by default scope.
    """

    found: tuple[Flow, UnresolvedNode] | None = None
    for flow in solmate_flows_unfiltered:
        for node in _walk(flow.root):
            if (
                isinstance(node, UnresolvedNode)
                and node.reason == UnresolvedReason.LOW_LEVEL_CALL
            ):
                found = (flow, node)
                break
        if found:
            break

    assert found is not None, "no UnresolvedNode with reason=LOW_LEVEL_CALL"
    _, node = found
    assert node.raw_kind == "low_level"


# ---------------------------------------------------------------------------
# 6. Builtins folding
# ---------------------------------------------------------------------------


def test_builtins_used_includes_require_on_erc4626_deposit(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """ERC4626.deposit guards on previewDeposit's return; that ``require``
    folds into the root FunctionNode's ``builtins_used`` (§11.7)."""

    flow = _flow_by_entry_point(solmate_flows, "ERC4626", "deposit(uint256,address)")
    assert "require(bool,string)" in flow.root.builtins_used, (
        f"expected require(bool,string) in deposit's root builtins_used; "
        f"got {flow.root.builtins_used}"
    )


# ---------------------------------------------------------------------------
# 7. Cycle detection is per-Flow, not global
# ---------------------------------------------------------------------------


def test_cycle_detection_state_is_per_flow(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """A helper called from two different entry-point Flows expands fully
    in BOTH — the visited set must not leak across recursion stacks.

    ERC4626.deposit and ERC4626.mint both call SafeTransferLib.safeTransferFrom
    and ERC20._mint. Each Flow has its own fresh path, so the helper
    expands identically in each.
    """

    deposit = _flow_by_entry_point(solmate_flows, "ERC4626", "deposit(uint256,address)")
    mint = _flow_by_entry_point(solmate_flows, "ERC4626", "mint(uint256,address)")

    target = "SafeTransferLib.safeTransferFrom(ERC20,address,address,uint256)"
    in_deposit = _find_node(
        deposit.root,
        lambda n: isinstance(n, FunctionNode) and n.canonical_name == target,
    )
    in_mint = _find_node(
        mint.root,
        lambda n: isinstance(n, FunctionNode) and n.canonical_name == target,
    )
    assert isinstance(in_deposit, FunctionNode)
    assert isinstance(in_mint, FunctionNode)

    # Expansions must match in shape — same number of children, same kinds,
    # same canonical names. A leaked visited set would truncate one of them.
    assert len(in_deposit.children) == len(in_mint.children), (
        f"{target} children count differs across Flows: "
        f"{len(in_deposit.children)} in deposit vs {len(in_mint.children)} in mint"
    )
    for d_child, m_child in zip(in_deposit.children, in_mint.children, strict=True):
        assert type(d_child) is type(m_child), (
            f"child node types differ: {type(d_child).__name__} vs "
            f"{type(m_child).__name__}"
        )
        if isinstance(d_child, FunctionNode):
            assert isinstance(m_child, FunctionNode)
            assert d_child.canonical_name == m_child.canonical_name
            assert len(d_child.children) == len(m_child.children)
        elif isinstance(d_child, UnresolvedNode):
            assert isinstance(m_child, UnresolvedNode)
            assert d_child.reason == m_child.reason

    # Builtins folded into the helper's own FunctionNode should also match.
    assert in_deposit.builtins_used == in_mint.builtins_used, (
        f"builtins_used differs across Flows: {in_deposit.builtins_used} vs "
        f"{in_mint.builtins_used}"
    )


# ---------------------------------------------------------------------------
# 8. Optional: super.X() — Solmate has no super calls in src/, skipped
# ---------------------------------------------------------------------------


# `grep -rn "super\." ../test-repos/solmate/src/` returns no hits as of the
# Solmate revision pinned for v0. The invoked_via_super=True branch in
# builder._is_super_internal_call is verified by inspection only; if a
# future Solmate update introduces a super call, this comment is the
# breadcrumb to add a real assertion here.


# ---------------------------------------------------------------------------
# 9. entry_point_invoker_canonical_name uniqueness (regression: 65 collisions
#    in Solmate when the field was keyed on the declarer)
# ---------------------------------------------------------------------------


def test_entry_point_invoker_canonical_name_unique_across_flows(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Every Flow's ``entry_point_invoker_canonical_name`` is unique.

    Regression for the inherited-entry-point collision: before the split,
    ``Flow.entry_point_canonical_name`` was keyed on the declaring contract,
    so e.g. ``MockOwned.transferOwnership`` and ``Owned.transferOwnership``
    both reported ``Owned.transferOwnership(address)`` — 65 such collisions
    across the Solmate fixture. The invoker variant is keyed on the
    invoking contract and must be globally unique so it can be used as the
    Layer 3 routing identifier.
    """

    invoker_names = [f.entry_point_invoker_canonical_name for f in solmate_flows]
    assert len(invoker_names) == len(set(invoker_names)), (
        f"entry_point_invoker_canonical_name duplicates in Solmate flows: "
        f"{sorted({n for n in invoker_names if invoker_names.count(n) > 1})}"
    )


# ---------------------------------------------------------------------------
# 10. Non-inherited entry points: declarer == invoker canonical
# ---------------------------------------------------------------------------


def test_non_inherited_entry_point_declarer_equals_invoker(
    solmate_flows: tuple[Flow, ...],
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """For an entry point declared on its own invoking contract (no
    inheritance), the declarer and invoker canonical_names coincide.

    ``MockERC20.mint(address,uint256)`` is declared on MockERC20 itself
    (the mint helper added for tests). The ``Owned.transferOwnership``
    flow invoked through ``Owned`` is similarly non-inherited from Owned's
    own perspective — only its MockOwned sibling carries an inheritance
    delta.

    The MockERC20 case uses the unfiltered fixture (the default scope
    filters ``Mock*``); Owned uses the default fixture, since it's a
    production contract that survives the default filter.
    """

    mint = _flow_by_entry_point(
        solmate_flows_unfiltered, "MockERC20", "mint(address,uint256)"
    )
    assert mint.entry_point_declarer_canonical_name == "MockERC20.mint(address,uint256)"
    assert (
        mint.entry_point_declarer_canonical_name
        == mint.entry_point_invoker_canonical_name
    )

    owned = _flow_by_entry_point(solmate_flows, "Owned", "transferOwnership(address)")
    assert (
        owned.entry_point_declarer_canonical_name == "Owned.transferOwnership(address)"
    )
    assert (
        owned.entry_point_declarer_canonical_name
        == owned.entry_point_invoker_canonical_name
    )


# ---------------------------------------------------------------------------
# 11. v0.1 scope-rule application (spec §11.2 / §11.4 / §11.8)
# ---------------------------------------------------------------------------


def test_default_scope_excludes_mock_contracts(
    solmate_flows: tuple[Flow, ...],
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """No Mock* contract appears as an invoker in the default-scope output;
    the unfiltered fixture has them, so the difference is the filter's
    effect (not Solmate happening to lack them)."""

    default_invokers = {f.entry_point_contract_name for f in solmate_flows}
    unfiltered_invokers = {
        f.entry_point_contract_name for f in solmate_flows_unfiltered
    }

    default_mocks = {n for n in default_invokers if n.startswith("Mock")}
    unfiltered_mocks = {n for n in unfiltered_invokers if n.startswith("Mock")}

    assert (
        default_mocks == set()
    ), f"default scope leaked Mock* contracts: {sorted(default_mocks)}"
    assert unfiltered_mocks, (
        "unfiltered fixture has no Mock* contracts; the test's premise (that "
        "Mocks exist in Solmate) is broken"
    )


def test_default_scope_excludes_test_paths(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """No Flow under default scope has a root whose source path matches any
    of the default ``**/test/**`` / ``**/tests/**`` / ``**/*.t.sol`` rules.

    Asserted on the FlowNode-side (root.source_location) rather than the
    contract-side, because the builder's filter applies pre-Flow but the
    invariant we care about is downstream: nothing the auditor sees in the
    rendered output lives in a test directory.
    """

    leaked = [
        f.entry_point_contract_name
        for f in solmate_flows
        if "/test/" in f.root.source_location.filename_relative
        or "/tests/" in f.root.source_location.filename_relative
        or f.root.source_location.filename_relative.endswith(".t.sol")
    ]
    assert (
        leaked == []
    ), f"default scope leaked entry points from test paths: {sorted(set(leaked))}"


def test_default_scope_keeps_production_contracts(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Production contracts survive the default filter — regression guard
    against an over-broad glob that would accidentally drop real code.

    The required list deliberately omits libraries (``SafeTransferLib``,
    ``MerkleProofLib``, ...) — libraries don't produce entry points per
    §11.4's last paragraph, so they never appear as invokers regardless
    of scope.
    """

    invokers = {f.entry_point_contract_name for f in solmate_flows}
    for required in ("ERC20", "ERC4626", "Owned", "WETH", "Auth"):
        assert required in invokers, (
            f"production contract {required} missing from default-scope flows; "
            f"sample invokers: {sorted(invokers)[:20]}"
        )


def test_inline_libraries_recurses_into_lib_path(
    solmate_facts,
) -> None:
    """With ``inline_libraries=("ds-test",)``, calls into lib/ds-test/ recurse
    as FunctionNodes instead of stubbing as ExternalNode.

    Builds a one-off Layer 2 pass (no session fixture) because this scope is
    specific to one test. The mechanism under test: ``library_inlined``
    short-circuits ``_is_external_path`` in builder.py.

    Concretely, AuthTest.testTransferOwnershipAsOwner under unfiltered scope
    reaches DSTest.assertEq via an ExternalNode (covered by other tests).
    Under inline_libraries=("ds-test",), the same call site recurses into
    DSTest's body.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    flows = build_flows(solmate_facts, Scope(inline_libraries=("ds-test",)))
    # Must use unfiltered-equivalent inputs to reach the test-contract path:
    # default-scope filtering of test contracts is orthogonal to inline_libraries.
    # Scope() with only inline_libraries set leaves exclude_paths/contracts empty.

    flow = _flow_by_entry_point(flows, "AuthTest", "testTransferOwnershipAsOwner()")

    # Walk the tree: any FunctionNode whose source_location is under
    # lib/ds-test/ proves recursion happened. Without inline_libraries this
    # would only appear as an ExternalNode.
    recursed: FunctionNode | None = None
    for node in _walk(flow.root):
        if isinstance(
            node, FunctionNode
        ) and node.source_location.filename_relative.startswith("lib/ds-test/"):
            recursed = node
            break

    assert recursed is not None, (
        "inline_libraries=('ds-test',) did not cause recursion into lib/ds-test/; "
        "no FunctionNode with source under lib/ds-test/ found in AuthTest flow"
    )
    # Sanity: the recursed node carries a real declarer name (not the empty
    # string a free-function stub would have).
    assert recursed.declarer_contract_name


# ---------------------------------------------------------------------------
# 12. Synthetic inline_libraries test — controlled facts, isolates the
# external-vs-recurse decision without Solmate coupling.
# ---------------------------------------------------------------------------


def _sl(filename_relative: str):
    """Build a minimal SourceLocation for synthetic facts."""

    from solidity_flow_navigator.analysis.types import SourceLocation

    return SourceLocation(
        filename_absolute="/abs/" + filename_relative,
        filename_relative=filename_relative,
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )


def _synthetic_facts_with_lib_call():
    """Build a tiny RepoFacts: one in-scope contract `App` calling
    `Lib.helper()` declared under `lib/widget/Lib.sol`.

    Returns the RepoFacts. `App.entry()` is the entry point; under default
    behavior it stubs Lib.helper as ExternalNode, under
    `inline_libraries=("widget",)` it recurses.
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    helper = Function(
        canonical_name="Lib.helper()",
        name="helper",
        full_name="helper()",
        contract_declarer_name="Lib",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("lib/widget/Lib.sol"),
        source_code="function helper() internal {}",
        calls=(),
    )

    entry_call = CallEdge(
        kind="library",  # using-for / Library.x() — recurses unless lib-external
        subkind=None,
        target_canonical_name="Lib.helper()",
        target_function_name="helper",
        target_contract_name="Lib",
        is_resolved=True,
        source_location=_sl("src/App.sol"),
    )
    entry = Function(
        canonical_name="App.entry()",
        name="entry",
        full_name="entry()",
        contract_declarer_name="App",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/App.sol"),
        source_code="function entry() external { Lib.helper(); }",
        calls=(entry_call,),
    )
    app = Contract(
        name="App",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/App.sol"),
        functions=(entry,),
        modifiers=(),
    )
    lib_contract = Contract(
        name="Lib",
        kind="library",
        is_interface=False,
        is_library=True,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("lib/widget/Lib.sol"),
        functions=(helper,),
        modifiers=(),
    )
    return RepoFacts(
        repo_path="/abs",
        contracts=(app, lib_contract),
        free_functions=(),
    )


def test_synthetic_lib_call_stubs_as_external_without_inline() -> None:
    """Without inline_libraries, a library under lib/ stubs as ExternalNode."""

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_lib_call()
    flows = build_flows(facts, Scope())  # empty scope — no defaults either
    assert len(flows) == 1
    children = flows[0].root.children
    assert len(children) == 1
    assert isinstance(children[0], ExternalNode)
    assert children[0].target_canonical_name == "Lib.helper()"
    assert children[0].source_path == "lib/widget/Lib.sol"


def test_synthetic_lib_call_recurses_with_inline_libraries() -> None:
    """With ``inline_libraries=("widget",)``, the same lib/ call recurses
    to a FunctionNode rather than stubbing.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_lib_call()
    flows = build_flows(facts, Scope(inline_libraries=("widget",)))
    assert len(flows) == 1
    children = flows[0].root.children
    assert len(children) == 1
    child = children[0]
    assert isinstance(child, FunctionNode), (
        f"expected FunctionNode (recursion), got {type(child).__name__} "
        f"with inline_libraries=('widget',)"
    )
    assert child.canonical_name == "Lib.helper()"
    assert child.source_location.filename_relative == "lib/widget/Lib.sol"


def test_synthetic_inline_libraries_name_must_match_segment() -> None:
    """``inline_libraries=("other",)`` does NOT inline lib/widget/* — the
    name matches the directory segment after `lib/`, not just any prefix.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_lib_call()
    flows = build_flows(facts, Scope(inline_libraries=("other",)))
    assert len(flows) == 1
    child = flows[0].root.children[0]
    assert isinstance(
        child, ExternalNode
    ), f"expected ExternalNode (no inlining match), got {type(child).__name__}"


# ---------------------------------------------------------------------------
# 13. Synthetic stub_paths tests — controlled facts isolating §11.8's
# in-tree-stub mechanism and the stub_paths-vs-inline_libraries conflict rule.
# ---------------------------------------------------------------------------


def _synthetic_facts_with_in_tree_lib_call():
    """Build a tiny RepoFacts: one in-scope contract `App` calling
    `MathLib.add()` declared under `src/libraries/MathLib.sol` (in-tree).

    Without ``stub_paths``, App.entry recurses into MathLib.add (it's not
    under lib/). With ``stub_paths=("src/libraries/**",)``, the call stubs
    as ExternalNode. This is the v0.2 in-tree compression mechanism.
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    add = Function(
        canonical_name="MathLib.add(uint256,uint256)",
        name="add",
        full_name="add(uint256,uint256)",
        contract_declarer_name="MathLib",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=True,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/libraries/MathLib.sol"),
        source_code="function add(uint256 a, uint256 b) internal pure returns (uint256) { return a + b; }",
        calls=(),
    )
    entry_call = CallEdge(
        kind="library",
        subkind=None,
        target_canonical_name="MathLib.add(uint256,uint256)",
        target_function_name="add",
        target_contract_name="MathLib",
        is_resolved=True,
        source_location=_sl("src/App.sol"),
    )
    entry = Function(
        canonical_name="App.entry()",
        name="entry",
        full_name="entry()",
        contract_declarer_name="App",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/App.sol"),
        source_code="function entry() external { MathLib.add(1, 2); }",
        calls=(entry_call,),
    )
    app = Contract(
        name="App",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/App.sol"),
        functions=(entry,),
        modifiers=(),
    )
    math_lib = Contract(
        name="MathLib",
        kind="library",
        is_interface=False,
        is_library=True,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/libraries/MathLib.sol"),
        functions=(add,),
        modifiers=(),
    )
    return RepoFacts(
        repo_path="/abs",
        contracts=(app, math_lib),
        free_functions=(),
    )


def test_stub_paths_stubs_in_tree_library_target() -> None:
    """An in-tree call target whose path matches ``stub_paths`` emits an
    ExternalNode (terminal) instead of recursing — the v0.2 in-tree
    compression mechanism per spec §11.2 / §11.8.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_in_tree_lib_call()
    flows = build_flows(facts, Scope(stub_paths=("src/libraries/**",)))
    assert len(flows) == 1
    children = flows[0].root.children
    assert len(children) == 1
    assert isinstance(children[0], ExternalNode), (
        f"expected ExternalNode (stub_paths match), got "
        f"{type(children[0]).__name__}"
    )
    assert children[0].target_canonical_name == "MathLib.add(uint256,uint256)"
    assert children[0].source_path == "src/libraries/MathLib.sol"


def test_stub_paths_negative_regression_unmatched_in_tree_recurses() -> None:
    """An in-tree library file that does NOT match any ``stub_paths`` glob
    still recurses normally — the v0.2 mechanism is opt-in per call target,
    not a blanket in-tree behavior change.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_in_tree_lib_call()
    flows = build_flows(facts, Scope(stub_paths=("src/utils/**",)))
    assert len(flows) == 1
    child = flows[0].root.children[0]
    assert isinstance(child, FunctionNode), (
        f"expected FunctionNode (stub_paths did not match MathLib path), "
        f"got {type(child).__name__}"
    )
    assert child.canonical_name == "MathLib.add(uint256,uint256)"


def test_stub_paths_wins_over_inline_libraries_conflict_rule() -> None:
    """Per spec §11.8 conflict rule: when a path matches BOTH ``stub_paths``
    and ``inline_libraries``, ``stub_paths`` wins. The auditor's explicit
    "stop here" beats the default-stub override.

    Setup: lib/widget/Lib.sol target. ``inline_libraries=("widget",)``
    would normally cause recursion (it overrides the default lib stub). A
    matching ``stub_paths`` glob must override that override → ExternalNode.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _synthetic_facts_with_lib_call()
    flows = build_flows(
        facts,
        Scope(
            inline_libraries=("widget",),
            stub_paths=("lib/widget/**",),
        ),
    )
    assert len(flows) == 1
    child = flows[0].root.children[0]
    assert isinstance(child, ExternalNode), (
        f"expected ExternalNode (stub_paths wins over inline_libraries), "
        f"got {type(child).__name__}"
    )
    assert child.source_path == "lib/widget/Lib.sol"


# ---------------------------------------------------------------------------
# 14. v0.3 stage 2: within-body virtual dispatch for kind="internal" calls
#     (spec §11.5). The Flow's invoker contract re-resolves a virtual call's
#     lexical target through its C3 chain.
# ---------------------------------------------------------------------------


def _synthetic_facts_virtual_internal(
    *,
    base_update_virtual: bool,
    derived_overrides_update: bool,
    base_update_implemented: bool = True,
):
    """Build a tiny RepoFacts mimicking the SablierLockup shape:

    ``Base.transferFrom() internal`` (the entry-point body, inherited)
    calls ``_update()`` (internal). ``Base._update()`` may be virtual.
    ``Derived`` inherits Base and optionally overrides ``_update()``.

    The Flow rooted at Derived.transferFrom should re-resolve ``_update``
    through Derived's chain when the lexical target is virtual.
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    base_update = Function(
        canonical_name="Base._update()",
        name="_update",
        full_name="_update()",
        contract_declarer_name="Base",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=base_update_implemented,
        is_virtual=base_update_virtual,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Base.sol"),
        source_code="function _update() internal virtual {}",
        calls=(),
    )
    update_call_in_transfer = CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name="Base._update()",
        target_function_name="_update",
        target_contract_name="Base",
        is_resolved=True,
        source_location=_sl("src/Base.sol"),
    )
    transfer_from = Function(
        canonical_name="Base.transferFrom()",
        name="transferFrom",
        full_name="transferFrom()",
        contract_declarer_name="Base",
        visibility="public",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Base.sol"),
        source_code="function transferFrom() public { _update(); }",
        calls=(update_call_in_transfer,),
    )
    base_functions: tuple[Function, ...] = (transfer_from, base_update)

    derived_functions: tuple[Function, ...] = ()
    if derived_overrides_update:
        derived_update = Function(
            canonical_name="Derived._update()",
            name="_update",
            full_name="_update()",
            contract_declarer_name="Derived",
            visibility="internal",
            is_constructor=False,
            is_fallback=False,
            is_receive=False,
            is_modifier=False,
            is_implemented=True,
            is_virtual=False,
            is_entry_point=False,
            payable=False,
            view=False,
            pure=False,
            parameters=(),
            returns=(),
            modifier_names=(),
            source_location=_sl("src/Derived.sol"),
            source_code="function _update() internal override {}",
            calls=(),
        )
        derived_functions = (derived_update,)

    base = Contract(
        name="Base",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/Base.sol"),
        functions=base_functions,
        modifiers=(),
    )
    derived = Contract(
        name="Derived",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("Base",),
        immediate_base_contract_names=("Base",),
        source_location=_sl("src/Derived.sol"),
        functions=derived_functions,
        modifiers=(),
    )
    return RepoFacts(repo_path="/abs", contracts=(base, derived), free_functions=())


def _derived_transfer_from_flow(facts):
    """Return the Flow rooted at Derived.transferFrom (the SablierLockup-
    shape inherited entry point) under no-op scope."""

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    flows = build_flows(facts, Scope())
    return _flow_by_entry_point(flows, "Derived", "transferFrom()")


def test_internal_virtual_call_resolves_to_override() -> None:
    """Derived inherits Base.transferFrom; Base._update() is virtual and
    Derived overrides it. The Flow rooted at Derived.transferFrom must
    show the call as recursing into Derived._update, not Base._update —
    this is the SablierLockup shape from the v0.3 motivating finding.
    """

    facts = _synthetic_facts_virtual_internal(
        base_update_virtual=True, derived_overrides_update=True
    )
    flow = _derived_transfer_from_flow(facts)
    assert len(flow.root.children) == 1
    child = flow.root.children[0]
    assert isinstance(child, FunctionNode), (
        f"expected internal call to recurse to FunctionNode, got "
        f"{type(child).__name__}"
    )
    assert child.canonical_name == "Derived._update()", (
        f"virtual call should resolve through Derived's chain to "
        f"Derived._update(), got {child.canonical_name!r}"
    )
    assert child.declarer_contract_name == "Derived"


def test_internal_virtual_call_no_override_resolves_to_lexical_target() -> None:
    """Base._update() is virtual but Derived does NOT override it.
    Resolution returns Base._update — same target as before v0.3, no
    regression for non-overridden virtual calls.
    """

    facts = _synthetic_facts_virtual_internal(
        base_update_virtual=True, derived_overrides_update=False
    )
    flow = _derived_transfer_from_flow(facts)
    child = flow.root.children[0]
    assert isinstance(child, FunctionNode)
    assert child.canonical_name == "Base._update()"
    assert child.declarer_contract_name == "Base"


def test_internal_non_virtual_call_unchanged_behavior() -> None:
    """Base._update() is NOT virtual; even if Derived has a same-named
    function, virtual dispatch must not fire. (In real Solidity this
    would be a compile error — Derived._update without override is
    illegal — but the helper's fast-path is what we're testing.)
    """

    facts = _synthetic_facts_virtual_internal(
        base_update_virtual=False, derived_overrides_update=True
    )
    flow = _derived_transfer_from_flow(facts)
    child = flow.root.children[0]
    assert isinstance(child, FunctionNode)
    assert child.canonical_name == "Base._update()", (
        f"non-virtual lexical target should pass through unchanged, got "
        f"{child.canonical_name!r}"
    )


def test_internal_virtual_abstract_no_impl_emits_unresolved() -> None:
    """Base._update() is virtual AND not implemented (e.g. an abstract
    declaration), Derived doesn't implement either. Layer 2 emits an
    UnresolvedNode with reason ABSTRACT_NO_IMPLEMENTATION.
    """

    facts = _synthetic_facts_virtual_internal(
        base_update_virtual=True,
        base_update_implemented=False,
        derived_overrides_update=False,
    )
    flow = _derived_transfer_from_flow(facts)
    child = flow.root.children[0]
    assert isinstance(
        child, UnresolvedNode
    ), f"expected UnresolvedNode, got {type(child).__name__}"
    assert child.reason is UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION
    assert child.raw_kind == "internal"
    assert child.children == () if hasattr(child, "children") else True


def test_invoked_via_constant_across_flow() -> None:
    """Every FunctionNode in a Flow carries the same
    ``invoked_via_contract_name`` value — the entry-point invoker
    contract — per spec §11.3 v0.3. Walks a multi-level tree built from
    Derived.transferFrom and asserts the invariant on every node.
    """

    facts = _synthetic_facts_virtual_internal(
        base_update_virtual=True, derived_overrides_update=True
    )
    flow = _derived_transfer_from_flow(facts)

    invokers: list[str] = []
    for node in _walk(flow.root):
        if isinstance(node, FunctionNode):
            invokers.append(node.invoked_via_contract_name)
    assert set(invokers) == {
        "Derived"
    }, f"expected invoked_via_contract_name to be 'Derived' everywhere, got {invokers}"


def test_library_call_skips_virtual_dispatch() -> None:
    """Library calls are kind=\"library\" and must not flow through
    ``resolve_virtual_override``. Libraries don't participate in
    inheritance; the call resolves to the lexical target unchanged.
    Verified by ensuring the synthetic-library tests at sections 12-13
    still pass (this is a sentinel — no regression suite needed here).
    """

    # Sentinel: explicitly construct a library-kind synthetic and verify
    # the resulting FunctionNode is the lexical target.
    facts = _synthetic_facts_with_lib_call()
    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    flows = build_flows(facts, Scope(inline_libraries=("widget",)))
    child = flows[0].root.children[0]
    assert isinstance(child, FunctionNode)
    assert child.canonical_name == "Lib.helper()"


def test_super_call_bypasses_virtual_dispatch() -> None:
    """A super.X() call's lexical target is Base.X(); even though
    Derived has its own X(), the super-call detection bypasses virtual
    dispatch (spec §11.5). Result: the FunctionNode is Base.X, with
    ``invoked_via_super=True``.
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    base_foo = Function(
        canonical_name="Base.foo()",
        name="foo",
        full_name="foo()",
        contract_declarer_name="Base",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=True,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Base.sol"),
        source_code="function foo() internal virtual {}",
        calls=(),
    )
    # Derived.foo() body contains `super.foo()` — Slither reports as
    # kind="internal" with target=Base.foo.
    super_call = CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name="Base.foo()",
        target_function_name="foo",
        target_contract_name="Base",
        is_resolved=True,
        source_location=_sl("src/Derived.sol"),
    )
    derived_foo = Function(
        canonical_name="Derived.foo()",
        name="foo",
        full_name="foo()",
        contract_declarer_name="Derived",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Derived.sol"),
        source_code="function foo() external override { super.foo(); }",
        calls=(super_call,),
    )
    base = Contract(
        name="Base",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/Base.sol"),
        functions=(base_foo,),
        modifiers=(),
    )
    derived = Contract(
        name="Derived",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("Base",),
        immediate_base_contract_names=("Base",),
        source_location=_sl("src/Derived.sol"),
        functions=(derived_foo,),
        modifiers=(),
    )
    facts = RepoFacts(repo_path="/abs", contracts=(base, derived), free_functions=())

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "Derived", "foo()")
    child = flow.root.children[0]
    assert isinstance(child, FunctionNode)
    # Critical: virtual dispatch did NOT fire — child is Base.foo, not
    # Derived.foo (which would create an infinite loop and be wrong).
    assert child.canonical_name == "Base.foo()", (
        f"super call should resolve to Base.foo (lexical target), got "
        f"{child.canonical_name!r}"
    )
    assert child.invoked_via_super is True
    assert child.invoked_via_contract_name == "Derived"


# ---------------------------------------------------------------------------
# 15. v0.3 stage 3: high_level self-call virtual dispatch (spec §11.5)
# ---------------------------------------------------------------------------


def _self_call_facts(
    *,
    derived_overrides_bar: bool,
    bar_virtual: bool = True,
    bar_implemented: bool = True,
):
    """Build a fact tree where Derived.foo() does ``this.bar()`` and bar is
    declared on Derived (so Slither's bound target is Derived.bar). When
    Derived inherits bar virtually from an ancestor that has the impl,
    the binding still points to whichever contract's chain Slither picked.

    Two specific shapes are needed for stage-3 coverage:
    - Self-call where Derived.bar IS declared on Derived → virtual
      dispatch is a no-op (Derived is most-derived). Sanity check.
    - Self-call where Derived inherits bar from Base, lexical target is
      Base.bar with target.contract != invoker — that path is
      cross-contract-shaped per the spec's literal reading and does NOT
      trigger virtual dispatch (handled by the negative test).
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    base_bar = Function(
        canonical_name="Base.bar()",
        name="bar",
        full_name="bar()",
        contract_declarer_name="Base",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=bar_implemented,
        is_virtual=bar_virtual,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Base.sol"),
        source_code="function bar() external virtual {}",
        calls=(),
    )
    # Slither binds the self-call to Derived.bar (the most-derived in
    # Derived's chain) when Derived has its own bar; to Base.bar otherwise.
    bound_target_canonical = "Derived.bar()" if derived_overrides_bar else "Base.bar()"
    self_call = CallEdge(
        kind="high_level",
        subkind=None,
        target_canonical_name=bound_target_canonical,
        target_function_name="bar",
        target_contract_name="Derived" if derived_overrides_bar else "Base",
        is_resolved=True,
        source_location=_sl("src/Derived.sol"),
    )
    derived_foo = Function(
        canonical_name="Derived.foo()",
        name="foo",
        full_name="foo()",
        contract_declarer_name="Derived",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Derived.sol"),
        source_code="function foo() external { this.bar(); }",
        calls=(self_call,),
    )
    derived_functions: tuple[Function, ...] = (derived_foo,)
    if derived_overrides_bar:
        derived_bar = Function(
            canonical_name="Derived.bar()",
            name="bar",
            full_name="bar()",
            contract_declarer_name="Derived",
            visibility="external",
            is_constructor=False,
            is_fallback=False,
            is_receive=False,
            is_modifier=False,
            is_implemented=True,
            is_virtual=False,
            is_entry_point=True,
            payable=False,
            view=False,
            pure=False,
            parameters=(),
            returns=(),
            modifier_names=(),
            source_location=_sl("src/Derived.sol"),
            source_code="function bar() external override {}",
            calls=(),
        )
        derived_functions = (derived_foo, derived_bar)
    base = Contract(
        name="Base",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/Base.sol"),
        functions=(base_bar,),
        modifiers=(),
    )
    derived = Contract(
        name="Derived",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("Base",),
        immediate_base_contract_names=("Base",),
        source_location=_sl("src/Derived.sol"),
        functions=derived_functions,
        modifiers=(),
    )
    return RepoFacts(repo_path="/abs", contracts=(base, derived), free_functions=())


def test_high_level_self_call_virtual_dispatch_no_op_when_derived_has_impl() -> None:
    """Self-call where Slither bound target = Derived.bar AND
    Derived.bar is itself an implementation. Virtual dispatch walks
    Derived's chain (most-derived first) and resolves to Derived.bar —
    a no-op vs the bound target. Confirms the path is exercised without
    surprises.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _self_call_facts(derived_overrides_bar=True)
    flows = build_flows(facts, Scope())
    foo_flow = _flow_by_entry_point(flows, "Derived", "foo()")
    child = foo_flow.root.children[0]
    assert isinstance(child, FunctionNode)
    assert child.canonical_name == "Derived.bar()"
    assert child.invoked_via_contract_name == "Derived"


def test_high_level_cross_contract_call_skips_virtual_dispatch() -> None:
    """Self-call where Slither bound target = Base.bar (Derived doesn't
    override). The bound target's contract is Base != invoker Derived,
    so per the literal §11.5 rule virtual dispatch does NOT fire — the
    cross-contract path emits a FunctionNode for Base.bar.

    This case captures the spec's distinction: \"high_level when the
    bound target's contract is C itself\" is the trigger; anything else
    is a cross-contract boundary.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _self_call_facts(derived_overrides_bar=False)
    flows = build_flows(facts, Scope())
    foo_flow = _flow_by_entry_point(flows, "Derived", "foo()")
    child = foo_flow.root.children[0]
    assert isinstance(child, FunctionNode)
    assert child.canonical_name == "Base.bar()"


def test_high_level_self_call_abstract_emits_unresolved() -> None:
    """Self-call to a virtual function declared on the invoker itself but
    not implemented anywhere in the invoker's chain. Emits
    ABSTRACT_NO_IMPLEMENTATION.
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )

    inv_bar = Function(
        canonical_name="Inv.bar()",
        name="bar",
        full_name="bar()",
        contract_declarer_name="Inv",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=False,  # abstract on the invoker itself
        is_virtual=True,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Inv.sol"),
        source_code="function bar() external virtual;",
        calls=(),
    )
    call = CallEdge(
        kind="high_level",
        subkind=None,
        target_canonical_name="Inv.bar()",
        target_function_name="bar",
        target_contract_name="Inv",
        is_resolved=True,
        source_location=_sl("src/Inv.sol"),
    )
    foo = Function(
        canonical_name="Inv.foo()",
        name="foo",
        full_name="foo()",
        contract_declarer_name="Inv",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Inv.sol"),
        source_code="function foo() external { this.bar(); }",
        calls=(call,),
    )
    inv = Contract(
        name="Inv",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=True,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/Inv.sol"),
        functions=(foo, inv_bar),
        modifiers=(),
    )
    facts = RepoFacts(repo_path="/abs", contracts=(inv,), free_functions=())

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    flows = build_flows(facts, Scope())
    foo_flow = _flow_by_entry_point(flows, "Inv", "foo()")
    child = foo_flow.root.children[0]
    assert isinstance(child, UnresolvedNode)
    assert child.reason is UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION
    assert child.raw_kind == "high_level"


# ---------------------------------------------------------------------------
# 16. v0.3 stage 3: modifier virtual dispatch (spec §11.5 + §11.6)
# ---------------------------------------------------------------------------


def _facts_with_modifier(
    *,
    base_mod_virtual: bool,
    derived_overrides_mod: bool,
    base_mod_implemented: bool = True,
):
    """Build a fact tree where Base.foo() is decorated by a guard modifier
    declared on Base (possibly virtual), and Derived inherits Base.foo
    optionally with a modifier override.
    """

    from solidity_flow_navigator.analysis.types import (
        Contract,
        Function,
        RepoFacts,
    )

    base_mod = Function(
        canonical_name="Base.guard()",
        name="guard",
        full_name="guard()",
        contract_declarer_name="Base",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=True,
        is_implemented=base_mod_implemented,
        is_virtual=base_mod_virtual,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/Base.sol"),
        source_code="modifier guard() virtual { _; }",
        calls=(),
    )
    base_foo = Function(
        canonical_name="Base.foo()",
        name="foo",
        full_name="foo()",
        contract_declarer_name="Base",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=("guard",),
        source_location=_sl("src/Base.sol"),
        source_code="function foo() external guard {}",
        calls=(),
    )
    base = Contract(
        name="Base",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("src/Base.sol"),
        functions=(base_foo,),
        modifiers=(base_mod,),
    )
    derived_modifiers: tuple[Function, ...] = ()
    if derived_overrides_mod:
        derived_mod = Function(
            canonical_name="Derived.guard()",
            name="guard",
            full_name="guard()",
            contract_declarer_name="Derived",
            visibility="internal",
            is_constructor=False,
            is_fallback=False,
            is_receive=False,
            is_modifier=True,
            is_implemented=True,
            is_virtual=False,
            is_entry_point=False,
            payable=False,
            view=False,
            pure=False,
            parameters=(),
            returns=(),
            modifier_names=(),
            source_location=_sl("src/Derived.sol"),
            source_code="modifier guard() override { _; }",
            calls=(),
        )
        derived_modifiers = (derived_mod,)
    derived = Contract(
        name="Derived",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("Base",),
        immediate_base_contract_names=("Base",),
        source_location=_sl("src/Derived.sol"),
        functions=(),
        modifiers=derived_modifiers,
    )
    return RepoFacts(repo_path="/abs", contracts=(base, derived), free_functions=())


def test_virtual_modifier_resolves_to_override_on_invoker() -> None:
    """Base.guard is virtual; Derived overrides. Flow rooted at
    Derived.foo (inherited from Base) shows the modifier as
    Derived.guard, not Base.guard — Solidity's modifier override
    semantics rendered correctly.
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _facts_with_modifier(base_mod_virtual=True, derived_overrides_mod=True)
    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "Derived", "foo()")
    # First child is the modifier
    mod_child = flow.root.children[0]
    assert isinstance(mod_child, FunctionNode)
    assert mod_child.is_modifier
    assert (
        mod_child.canonical_name == "Derived.guard()"
    ), f"expected Derived.guard (override), got {mod_child.canonical_name!r}"
    assert mod_child.invoked_via_contract_name == "Derived"


def test_virtual_modifier_no_override_resolves_to_base() -> None:
    """Base.guard is virtual but Derived does NOT override. Modifier
    resolves to Base.guard (unchanged behavior).
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _facts_with_modifier(base_mod_virtual=True, derived_overrides_mod=False)
    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "Derived", "foo()")
    mod_child = flow.root.children[0]
    assert isinstance(mod_child, FunctionNode)
    assert mod_child.canonical_name == "Base.guard()"


def test_non_virtual_modifier_unchanged_behavior() -> None:
    """Base.guard is NOT virtual; the helper's fast-path returns it
    unchanged, even if a same-named modifier exists on Derived (which
    in real Solidity would not compile without virtual/override).
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _facts_with_modifier(base_mod_virtual=False, derived_overrides_mod=True)
    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "Derived", "foo()")
    mod_child = flow.root.children[0]
    assert isinstance(mod_child, FunctionNode)
    assert mod_child.canonical_name == "Base.guard()"


def test_virtual_modifier_abstract_no_impl_emits_unresolved() -> None:
    """Virtual modifier with no implementation in invoker's chain →
    UnresolvedNode with ABSTRACT_NO_IMPLEMENTATION, raw_kind=\"modifier\".
    """

    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    facts = _facts_with_modifier(
        base_mod_virtual=True,
        base_mod_implemented=False,
        derived_overrides_mod=False,
    )
    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "Derived", "foo()")
    mod_child = flow.root.children[0]
    assert isinstance(mod_child, UnresolvedNode)
    assert mod_child.reason is UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION
    assert mod_child.raw_kind == "modifier"


# ---------------------------------------------------------------------------
# 17. Sablier-shape sanity check: synthetic three-contract chain mirroring
#     SablierLockup → SablierLockupBase → ERC721, where transferFrom is
#     inherited and calls _update virtually.
# ---------------------------------------------------------------------------


def test_sablier_shape_three_contract_chain_resolves_to_mid() -> None:
    """Three-contract chain mirroring the v0.3 motivating finding:

        ERC721 (lib/openzeppelin)  has transferFrom + virtual _update body
        SablierLockupBase (src/)   overrides _update (most-derived impl)
        SablierLockup (src/)       leaf — does NOT override _update

    Flow rooted at SablierLockup.transferFrom must show the _update call
    recursing into SablierLockupBase._update, not ERC721._update. Verifies
    end-to-end:
      - virtual dispatch fires on inherited transferFrom's body
      - C3 walk descends the chain in most-derived order
      - the resolved override's filename is NOT under lib/ so the
        external-vs-recurse check (which now runs post-resolution)
        recurses correctly
    """

    from solidity_flow_navigator.analysis.types import (
        CallEdge,
        Contract,
        Function,
        RepoFacts,
    )
    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    erc721_update = Function(
        canonical_name="ERC721._update()",
        name="_update",
        full_name="_update()",
        contract_declarer_name="ERC721",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=True,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("lib/openzeppelin/ERC721.sol"),
        source_code="function _update() internal virtual {}",
        calls=(),
    )
    update_in_transfer = CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name="ERC721._update()",
        target_function_name="_update",
        target_contract_name="ERC721",
        is_resolved=True,
        source_location=_sl("lib/openzeppelin/ERC721.sol"),
    )
    erc721_transfer = Function(
        canonical_name="ERC721.transferFrom()",
        name="transferFrom",
        full_name="transferFrom()",
        contract_declarer_name="ERC721",
        visibility="public",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("lib/openzeppelin/ERC721.sol"),
        source_code="function transferFrom() public { _update(); }",
        calls=(update_in_transfer,),
    )

    base_update = Function(
        canonical_name="SablierLockupBase._update()",
        name="_update",
        full_name="_update()",
        contract_declarer_name="SablierLockupBase",
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=True,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl("src/SablierLockupBase.sol"),
        source_code="function _update() internal override virtual {}",
        calls=(),
    )

    erc721 = Contract(
        name="ERC721",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=_sl("lib/openzeppelin/ERC721.sol"),
        functions=(erc721_transfer, erc721_update),
        modifiers=(),
    )
    base = Contract(
        name="SablierLockupBase",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("ERC721",),
        immediate_base_contract_names=("ERC721",),
        source_location=_sl("src/SablierLockupBase.sol"),
        functions=(base_update,),
        modifiers=(),
    )
    leaf = Contract(
        name="SablierLockup",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=("SablierLockupBase", "ERC721"),
        immediate_base_contract_names=("SablierLockupBase",),
        source_location=_sl("src/SablierLockup.sol"),
        functions=(),
        modifiers=(),
    )
    facts = RepoFacts(
        repo_path="/abs", contracts=(erc721, base, leaf), free_functions=()
    )

    flows = build_flows(facts, Scope())
    flow = _flow_by_entry_point(flows, "SablierLockup", "transferFrom()")
    # transferFrom's body has one child — the _update call. v0.3 must
    # resolve it to SablierLockupBase._update (mid-chain override).
    children = flow.root.children
    assert len(children) == 1
    child = children[0]
    assert isinstance(child, FunctionNode), (
        f"expected FunctionNode (in-tree override resolution), got "
        f"{type(child).__name__}; lib/-stub regression would surface here"
    )
    assert child.canonical_name == "SablierLockupBase._update()", (
        f"virtual dispatch should resolve to SablierLockupBase._update, "
        f"got {child.canonical_name!r}"
    )
    assert child.declarer_contract_name == "SablierLockupBase"
    assert child.invoked_via_contract_name == "SablierLockup"
