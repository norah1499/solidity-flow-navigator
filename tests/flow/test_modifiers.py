"""Tests for modifier folding (spec §11.6) and the resolve_modifier lookup.

Modifiers fold into the Flow's call tree as inline pseudo-nodes — FunctionNode
records with ``is_modifier=True`` that appear as the first children of the
function they decorate, before any body call children.

The dedup check in test_modifier_does_not_consume_body_calls is the regression
guard for the modifier-dedup logic in builder.py: Slither's IR represents
applied modifiers in TWO places (Function.modifier_names AND an InternalCall
edge in the body), and Layer 2 emits the modifier child only via the
modifier_names path. If that dedup ever drops too aggressively (e.g. skipping
all internal calls instead of just modifier-targeting ones), legitimate body
calls would disappear and this test would catch it.
"""

from solidity_flow_navigator.flow.types import Flow, FunctionNode


def test_owned_transferownership_first_child_is_only_owner_modifier(
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Owned.transferOwnership has onlyOwner as its first (and only) child,
    with is_modifier=True (§11.6)."""

    flow = next(
        f
        for f in solmate_flows
        if f.entry_point_contract_name == "Owned"
        and f.root.full_name == "transferOwnership(address)"
    )
    assert (
        len(flow.root.children) >= 1
    ), "Owned.transferOwnership has no children; expected at least 1 modifier"
    first = flow.root.children[0]
    assert isinstance(
        first, FunctionNode
    ), f"first child is {type(first).__name__}, expected FunctionNode"
    assert (
        first.is_modifier is True
    ), f"first child {first.canonical_name} has is_modifier=False"
    assert first.name == "onlyOwner"

    # And exactly one onlyOwner — defends against the dedup regression that
    # surfaced during Stage 2 probing (Slither emits modifiers via both
    # modifier_names AND an internal call edge; we de-dup).
    only_owner_count = sum(
        1
        for c in flow.root.children
        if isinstance(c, FunctionNode) and c.is_modifier and c.name == "onlyOwner"
    )
    assert (
        only_owner_count == 1
    ), f"expected exactly one onlyOwner child, got {only_owner_count}"


def test_modifier_does_not_consume_body_calls(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """Find a Flow whose root has BOTH modifier children AND body children.
    Verify the body children survive (i.e. the modifier-dedup didn't eat
    them) and the modifier(s) come first.

    Solmate has 44+ such flows (test contracts using `brutalizeMemory` etc.).
    The test discovers a candidate dynamically — if Solmate is later refactored
    such that no entry point has both shapes, the assertion fires loudly so
    the regression guard's preconditions are checked.

    Uses the unfiltered fixture because the modifier+body combinations
    Solmate ships live exclusively in test contracts under ``src/test/**``;
    the default scope removes all of them and starves the discovery loop.
    """

    candidates = []
    for f in solmate_flows_unfiltered:
        children = f.root.children
        if not children:
            continue
        has_mod = any(isinstance(c, FunctionNode) and c.is_modifier for c in children)
        has_body = any(
            not (isinstance(c, FunctionNode) and c.is_modifier) for c in children
        )
        if has_mod and has_body:
            candidates.append(f)

    assert candidates, (
        "no Solmate Flow has both modifier children AND body children — "
        "the modifier-dedup regression guard cannot be exercised. If this "
        "fires after a Solmate update, find a new entry point that combines "
        "a modifier with at least one body call."
    )

    flow = candidates[0]
    saw_body_child = False
    for child in flow.root.children:
        is_modifier_child = isinstance(child, FunctionNode) and child.is_modifier
        if is_modifier_child:
            assert not saw_body_child, (
                f"modifier {child.canonical_name!r} appears AFTER a body "
                f"child in {flow.entry_point_contract_name}.{flow.root.full_name}"
                f" — §11.6 requires modifiers first"
            )
        else:
            saw_body_child = True

    assert saw_body_child, (
        f"{flow.entry_point_contract_name}.{flow.root.full_name} candidate "
        f"reports body children but iteration found none — discovery bug"
    )


# ---------------------------------------------------------------------------
# Unit test for resolve_modifier — exercises the lookup helper without
# needing the full Solmate fixture (no forge build required).
# ---------------------------------------------------------------------------


def _make_function(
    canonical_name: str,
    name: str,
    full_name: str,
    declarer: str,
    *,
    is_modifier: bool,
):
    from solidity_flow_navigator.analysis.types import Function, SourceLocation

    sl = SourceLocation(
        filename_absolute="/x",
        filename_relative="x",
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )
    return Function(
        canonical_name=canonical_name,
        name=name,
        full_name=full_name,
        contract_declarer_name=declarer,
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=is_modifier,
        is_implemented=True,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=sl,
        source_code="",
        calls=(),
    )


def _make_contract(
    name: str, bases: tuple[str, ...], modifiers: tuple, functions: tuple = ()
):
    from solidity_flow_navigator.analysis.types import Contract, SourceLocation

    sl = SourceLocation(
        filename_absolute="/x",
        filename_relative="x",
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )
    return Contract(
        name=name,
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=bases,
        immediate_base_contract_names=bases,
        source_location=sl,
        functions=functions,
        modifiers=modifiers,
    )


def test_resolve_modifier_walks_inheritance_chain() -> None:
    """resolve_modifier finds modifiers declared on ancestors via C3 walk."""

    from solidity_flow_navigator.flow.modifiers import resolve_modifier

    base_mod = _make_function(
        "Base.onlyOwner()", "onlyOwner", "onlyOwner()", "Base", is_modifier=True
    )
    base = _make_contract("Base", bases=(), modifiers=(base_mod,))
    child = _make_contract("Child", bases=("Base",), modifiers=())

    contracts = {c.name: c for c in (base, child)}
    found = resolve_modifier("onlyOwner", child, contracts)
    assert found is base_mod


def test_resolve_modifier_most_derived_wins() -> None:
    """When a modifier is overridden, the most-derived declaration wins."""

    from solidity_flow_navigator.flow.modifiers import resolve_modifier

    base_mod = _make_function(
        "Base.onlyOwner()", "onlyOwner", "onlyOwner()", "Base", is_modifier=True
    )
    derived_mod = _make_function(
        "Child.onlyOwner()",
        "onlyOwner",
        "onlyOwner()",
        "Child",
        is_modifier=True,
    )
    base = _make_contract("Base", bases=(), modifiers=(base_mod,))
    child = _make_contract("Child", bases=("Base",), modifiers=(derived_mod,))

    contracts = {c.name: c for c in (base, child)}
    found = resolve_modifier("onlyOwner", child, contracts)
    assert found is derived_mod


def test_resolve_modifier_raises_with_diagnostic_on_miss() -> None:
    """Missing modifier raises KeyError with the inheritance chain in the message."""

    import pytest

    from solidity_flow_navigator.flow.modifiers import resolve_modifier

    contract = _make_contract("Empty", bases=(), modifiers=())
    contracts = {"Empty": contract}
    with pytest.raises(KeyError, match=r"modifier 'missing' .* not found"):
        resolve_modifier("missing", contract, contracts)
