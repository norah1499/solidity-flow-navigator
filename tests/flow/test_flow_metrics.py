"""Unit tests for the v0.8.0 Flow derivations ``unresolved_count`` and
``max_depth`` (spec §8.3, per-entry index metadata column).

These tests construct synthetic Flows directly via the public dataclasses
rather than going through Layer 1 + Layer 2 on a real repo — the derivation
is a pure tree walk and the per-shape cases (leaf-root, deeply nested,
mixed unresolved) are easier to exercise without the Solmate fixture in
the loop. Layer 2 integration with these fields is covered indirectly by
the existing ``solmate_flows`` fixture in ``test_builder.py`` (every Flow
constructed by ``build_flows`` now carries them).
"""

from solidity_flow_navigator.analysis.types import SourceLocation
from solidity_flow_navigator.flow.builder import _summarize_subtree
from solidity_flow_navigator.flow.types import (
    ExternalNode,
    Flow,
    FlowNode,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)


def _sl(filename_relative: str = "src/Foo.sol") -> SourceLocation:
    return SourceLocation(
        filename_absolute="/abs/" + filename_relative,
        filename_relative=filename_relative,
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )


def _fn(name: str, children: tuple[FlowNode, ...] = ()) -> FunctionNode:
    """Construct a minimal FunctionNode for tree-shape tests."""
    return FunctionNode(
        canonical_name=f"C.{name}()",
        name=name,
        full_name=f"{name}()",
        declarer_contract_name="C",
        invoked_via_contract_name="C",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code=f"function {name}() external {{}}",
        source_location=_sl(),
        builtins_used=(),
        children=children,
    )


def _unr() -> UnresolvedNode:
    return UnresolvedNode(
        reason=UnresolvedReason.INTERFACE_CALL_NO_BINDING,
        descriptor="<interface>.x(...)",
        call_site=_sl(),
        raw_kind="high_level",
        raw_subkind=None,
    )


def _ext() -> ExternalNode:
    return ExternalNode(
        target_canonical_name="Lib.helper()",
        target_function_name="helper",
        target_contract_name="Lib",
        source_path="lib/widget/Lib.sol",
        call_site=_sl(),
    )


def _wrap(root: FunctionNode) -> Flow:
    """Wrap a FunctionNode into a Flow, computing the derivations the way
    ``build_flow`` would. Mirrors what Layer 2 produces."""
    unresolved_count, max_depth = _summarize_subtree(root)
    return Flow(
        entry_point_declarer_canonical_name=root.canonical_name,
        entry_point_invoker_canonical_name=f"C.{root.full_name}",
        entry_point_contract_name="C",
        entry_point_function_name=root.name,
        root=root,
        unresolved_count=unresolved_count,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# unresolved_count
# ---------------------------------------------------------------------------


def test_leaf_root_flow_has_zero_unresolved_and_zero_depth() -> None:
    """A FunctionNode root with no children: ``max_depth == 0``,
    ``unresolved_count == 0``."""
    flow = _wrap(_fn("entry"))
    assert flow.unresolved_count == 0
    assert flow.max_depth == 0


def test_unresolved_descendant_increments_unresolved_count() -> None:
    """A single Unresolved leaf one level under the root contributes 1."""
    flow = _wrap(_fn("entry", children=(_unr(),)))
    assert flow.unresolved_count == 1
    assert flow.max_depth == 1


def test_multiple_unresolved_descendants_sum() -> None:
    """All ``UnresolvedNode`` descendants are counted, no matter how deeply
    buried, and irrespective of sibling ``ExternalNode``s."""
    deep = _fn(
        "a",
        children=(
            _unr(),
            _fn("b", children=(_unr(), _ext())),
            _ext(),
        ),
    )
    flow = _wrap(_fn("entry", children=(deep, _unr())))
    assert flow.unresolved_count == 3


def test_external_descendants_do_not_count_as_unresolved() -> None:
    """``ExternalNode`` is a distinct leaf type; it does not bump
    ``unresolved_count``."""
    flow = _wrap(_fn("entry", children=(_ext(), _ext(), _ext())))
    assert flow.unresolved_count == 0


# ---------------------------------------------------------------------------
# max_depth
# ---------------------------------------------------------------------------


def test_single_function_child_has_max_depth_one() -> None:
    """One body call → one edge → ``max_depth == 1``."""
    flow = _wrap(_fn("entry", children=(_fn("child"),)))
    assert flow.max_depth == 1


def test_max_depth_is_longest_root_to_leaf_not_node_count() -> None:
    """Mixed Flow with one deep chain and one shallow branch: depth is the
    longest path, not the total node count."""
    deep_chain = _fn(
        "a", children=(_fn("b", children=(_fn("c", children=(_fn("d"),)),)),)
    )
    shallow = _fn("x", children=(_fn("y"),))
    flow = _wrap(_fn("entry", children=(deep_chain, shallow)))
    # deep_chain branch: entry → a → b → c → d = 4 edges
    # shallow branch: entry → x → y = 2 edges
    assert flow.max_depth == 4


def test_max_depth_with_unresolved_leaf_descendant() -> None:
    """An ``UnresolvedNode`` leaf is depth 0; depth measurement should still
    travel through it via its containing FunctionNode chain."""
    deep = _fn("a", children=(_fn("b", children=(_unr(),)),))
    flow = _wrap(_fn("entry", children=(deep,)))
    # entry → a → b → unr = 3 edges
    assert flow.max_depth == 3
    assert flow.unresolved_count == 1


def test_max_depth_with_external_leaf_descendant() -> None:
    """An ``ExternalNode`` leaf is depth 0; depth measurement travels through
    its containing FunctionNode chain identically to the unresolved case."""
    deep = _fn("a", children=(_fn("b", children=(_ext(),)),))
    flow = _wrap(_fn("entry", children=(deep,)))
    assert flow.max_depth == 3
    assert flow.unresolved_count == 0


# ---------------------------------------------------------------------------
# Root-shape sanity (Unresolved root)
# ---------------------------------------------------------------------------


def test_summarize_unresolved_root_counts_itself() -> None:
    """An Unresolved root is uniform-predicate sanity: not a shape Layer 2
    produces in practice (Flow.root is typed FunctionNode), but the helper's
    contract is that an UnresolvedNode counts itself as 1 and has depth 0.
    Test the helper directly rather than wrapping in a Flow object."""
    unresolved_count, max_depth = _summarize_subtree(_unr())
    assert unresolved_count == 1
    assert max_depth == 0


def test_summarize_external_root_counts_zero() -> None:
    """Companion to the unresolved-root case: an ExternalNode root
    contributes nothing to either metric."""
    unresolved_count, max_depth = _summarize_subtree(_ext())
    assert unresolved_count == 0
    assert max_depth == 0
