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
