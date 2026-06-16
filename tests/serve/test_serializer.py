"""Tests for ``serve.serializer.serialize_flow``.

The Flow IR → template-ready dict conversion is a thin wrapper around
``dataclasses.asdict`` plus per-FunctionNode Pygments highlighting. These
tests cover the parts that aren't a plain pass-through: signature suffix
extraction, ``source_html`` presence on FunctionNodes (and absence on the
other variants), StrEnum serialisation, and JSON-dumpability of the whole
tree (Layer 3 embeds it inline).

The fixtures pull from the session-scoped ``solmate_flows`` defined in
``tests/conftest.py`` so we don't pay a second compile pass.
"""

from __future__ import annotations

import json
from typing import Any

from solidity_flow_navigator.analysis.types import SourceLocation, TypeDef
from solidity_flow_navigator.flow.types import (
    Binding,
    Flow,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)
from solidity_flow_navigator.serve.serializer import serialize_flow


def _flow_by_canonical_and_contract(
    flows: tuple[Flow, ...], canonical: str, contract: str
) -> Flow:
    """Look up a Flow by (invoker canonical_name, invoking-contract).

    The ``(canonical, contract)`` pair is retained from the pre-fb42c18 era
    when ``entry_point_canonical_name`` was the declarer's canonical and not
    unique per Flow. Layer 2 now exposes ``entry_point_invoker_canonical_name``
    which is unique by construction, so contract is redundant here — but the
    extra filter is cheap and keeps the existing call sites stable.
    """
    for f in flows:
        if (
            f.entry_point_invoker_canonical_name == canonical
            and f.entry_point_contract_name == contract
        ):
            return f
    raise AssertionError(f"flow {contract}/{canonical} not found")


# ---------------------------------------------------------------------------
# Interface-binding node metadata (v0.17.0, §10.2, §13.2)
# ---------------------------------------------------------------------------

_SL = SourceLocation(
    filename_absolute="/a.sol",
    filename_relative="src/a.sol",
    start=0,
    length=1,
    lines=(1,),
    starting_column=1,
    ending_column=2,
)


def _fn(**over: Any) -> FunctionNode:
    base: dict[str, Any] = dict(
        canonical_name="C.f()",
        name="f",
        full_name="f()",
        declarer_contract_name="C",
        invoked_via_contract_name="C",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code="function f() {}",
        source_location=_SL,
        builtins_used=(),
        children=(),
    )
    base.update(over)
    return FunctionNode(**base)


def _binding_flow() -> Flow:
    bound_child = _fn(
        canonical_name="AaveStrategy.run()",
        name="run",
        full_name="run()",
        declarer_contract_name="AaveStrategy",
        invoked_via_contract_name="AaveStrategy",
        call_kind="external",
        bound_via=Binding("IStrategy", "AaveStrategy"),
    )
    unresolved_child = UnresolvedNode(
        reason=UnresolvedReason.INTERFACE_CALL_NO_BINDING,
        descriptor="IOracle.price(...)",
        call_site=_SL,
        raw_kind="high_level",
        raw_subkind=None,
    )
    root = _fn(children=(bound_child, unresolved_child))
    return Flow(
        entry_point_declarer_canonical_name="C.f()",
        entry_point_invoker_canonical_name="C.f()",
        entry_point_contract_name="C",
        entry_point_function_name="f",
        root=root,
        unresolved_count=1,
        max_depth=1,
    )


_CTX = {
    "candidates": {"IStrategy": ("AaveStrategy",), "IOracle": ("ChainlinkOracle",)},
    "bound": {"IStrategy": "AaveStrategy", "IOracle": None},
}


def test_no_bindings_ctx_attaches_no_binding_metadata() -> None:
    d = serialize_flow(_binding_flow())  # no ctx → Flow page not in play
    assert "binding" not in d["root"]["children"][0]
    assert "binding" not in d["root"]["children"][1]


def test_bound_node_gets_binding_control() -> None:
    d = serialize_flow(_binding_flow(), _CTX)
    bound = d["root"]["children"][0]
    assert bound["binding"]["interface"] == "IStrategy"
    assert bound["binding"]["bound_to"] == "AaveStrategy"
    assert bound["binding"]["candidates"] == ["AaveStrategy"]


def test_unresolved_interface_node_gets_binding_control() -> None:
    d = serialize_flow(_binding_flow(), _CTX)
    unres = d["root"]["children"][1]
    assert unres["binding"]["interface"] == "IOracle"
    assert unres["binding"]["bound_to"] is None
    assert unres["binding"]["candidates"] == ["ChainlinkOracle"]


def test_root_non_interface_node_has_no_binding_control() -> None:
    d = serialize_flow(_binding_flow(), _CTX)
    assert "binding" not in d["root"]


def test_interface_with_no_candidates_gets_no_control() -> None:
    """An unresolved interface call whose interface has no candidates and no
    binding offers no dropdown (an empty control would be useless)."""
    node = UnresolvedNode(
        reason=UnresolvedReason.INTERFACE_CALL_NO_BINDING,
        descriptor="IFoo.bar(...)",
        call_site=_SL,
        raw_kind="high_level",
        raw_subkind=None,
    )
    flow = Flow(
        entry_point_declarer_canonical_name="C.f()",
        entry_point_invoker_canonical_name="C.f()",
        entry_point_contract_name="C",
        entry_point_function_name="f",
        root=_fn(children=(node,)),
        unresolved_count=1,
        max_depth=1,
    )
    ctx = {"candidates": {"IFoo": ()}, "bound": {"IFoo": None}}
    d = serialize_flow(flow, ctx)
    assert "binding" not in d["root"]["children"][0]


def test_top_level_shape(solmate_flows: tuple[Flow, ...]) -> None:
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "Owned.transferOwnership(address)", "Owned"
    )
    d = serialize_flow(flow)
    assert d["entry_point_invoker_canonical_name"] == "Owned.transferOwnership(address)"
    assert d["entry_point_contract_name"] == "Owned"
    assert d["entry_point_function_name"] == "transferOwnership"
    assert d["signature_suffix"] == "(address)"
    assert isinstance(d["root"], dict)


def test_function_node_has_source_html(solmate_flows: tuple[Flow, ...]) -> None:
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "Owned.transferOwnership(address)", "Owned"
    )
    d = serialize_flow(flow)
    root = d["root"]
    assert root["node_type"] == "function"
    assert "<span" in root["source_html"]
    assert root["source_code"].startswith("function transferOwnership")


def test_modifier_child_round_trips(solmate_flows: tuple[Flow, ...]) -> None:
    """Owned.transferOwnership has exactly one child: the onlyOwner modifier."""
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "Owned.transferOwnership(address)", "Owned"
    )
    d = serialize_flow(flow)
    children = d["root"]["children"]
    assert len(children) == 1
    mod = children[0]
    assert mod["node_type"] == "function"
    assert mod["is_modifier"] is True
    assert "<span" in mod["source_html"]


def test_unresolved_node_round_trips(solmate_flows: tuple[Flow, ...]) -> None:
    """ERC4626.deposit reaches a yul_dynamic_dispatch unresolved branch
    through SafeTransferLib's inline assembly call.

    ERC4626 is itself abstract — under v0.3's within-body virtual dispatch
    (§11.5), abstract virtual methods like ``totalAssets()`` whose lexical
    declarer is ERC4626 also surface as unresolved nodes with reason
    ``abstract_no_implementation``. The yul check below scans the whole
    tree to find the specific reason being asserted rather than just the
    first unresolved encountered DFS-first.
    """
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "ERC4626.deposit(uint256,address)", "ERC4626"
    )
    d = serialize_flow(flow)

    def find_unresolved_with_reason(node: dict, reason: str) -> dict | None:
        if node["node_type"] == "unresolved" and node["reason"] == reason:
            return node
        if node["node_type"] != "function":
            return None
        for c in node["children"]:
            hit = find_unresolved_with_reason(c, reason)
            if hit is not None:
                return hit
        return None

    u = find_unresolved_with_reason(d["root"], "yul_dynamic_dispatch")
    assert u is not None, (
        "expected a yul_dynamic_dispatch unresolved node somewhere in "
        "ERC4626.deposit's tree (SafeTransferLib delegatecall path)"
    )
    # StrEnum -> str via dataclasses.asdict.
    assert isinstance(u["reason"], str)
    assert u["reason"] == "yul_dynamic_dispatch"
    assert "source_html" not in u  # only FunctionNodes get a body


def test_external_node_round_trips(
    solmate_flows_unfiltered: tuple[Flow, ...],
) -> None:
    """An AuthTest entry point reaches DSTest.assertEq under lib/ds-test/.

    Uses the unfiltered fixture because ``AuthTest`` lives under
    ``src/test/`` and is dropped by the default scope's ``**/test/**``
    rule. The serializer behavior under test (round-tripping ExternalNode
    at the lib/ boundary) is identical regardless of scope.
    """
    flow = _flow_by_canonical_and_contract(
        solmate_flows_unfiltered, "AuthTest.testTransferOwnershipAsOwner()", "AuthTest"
    )
    d = serialize_flow(flow)

    def find_external(node: dict) -> dict | None:
        if node["node_type"] == "external":
            return node
        if node["node_type"] != "function":
            return None
        for c in node["children"]:
            hit = find_external(c)
            if hit is not None:
                return hit
        return None

    ext = find_external(d["root"])
    assert ext is not None
    assert ext["source_path"].startswith("lib/")
    assert ext["target_canonical_name"]
    assert "source_html" not in ext


def test_serialized_tree_is_json_dumpable(solmate_flows: tuple[Flow, ...]) -> None:
    """The dict tree must survive ``json.dumps`` — that's how Layer 3 embeds it.

    We don't assert round-trip equality: ``dataclasses.asdict`` keeps tuples
    as tuples, and ``json.loads`` restores them as lists. The important
    property is that ``dumps`` does not raise (no Pygments output, StrEnum,
    or unexpected type leaks through) and the round-tripped shape preserves
    the discriminator and a couple of marker fields.
    """
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "ERC4626.deposit(uint256,address)", "ERC4626"
    )
    d = serialize_flow(flow)
    blob = json.dumps(d)  # raises TypeError on unsupported type
    parsed = json.loads(blob)
    assert (
        parsed["entry_point_invoker_canonical_name"]
        == d["entry_point_invoker_canonical_name"]
    )
    assert parsed["root"]["node_type"] == "function"
    assert parsed["root"]["source_html"] == d["root"]["source_html"]
    assert isinstance(parsed["root"]["children"], list)


# ---------------------------------------------------------------------------
# Signature-type panel serialization (§10.2)
# ---------------------------------------------------------------------------


def _td() -> TypeDef:
    return TypeDef(
        kind="struct",
        canonical_name="MarketParams",
        name="MarketParams",
        source_location=_SL,
        source_code="struct MarketParams { address loanToken; uint256 lltv; }",
    )


def _sig_type_flow() -> Flow:
    root = _fn(signature_types=(_td(),))
    return Flow(
        entry_point_declarer_canonical_name="C.f()",
        entry_point_invoker_canonical_name="C.f()",
        entry_point_contract_name="C",
        entry_point_function_name="f",
        root=root,
        unresolved_count=0,
        max_depth=0,
    )


def test_signature_types_serialized_with_highlighted_body() -> None:
    """Each signature TypeDef round-trips and gains a Pygments ``source_html``
    alongside its raw ``source_code``, mirroring the FunctionNode body."""
    d = serialize_flow(_sig_type_flow())
    types = d["root"]["signature_types"]
    assert len(types) == 1
    t = types[0]
    assert t["kind"] == "struct"
    assert t["name"] == "MarketParams"
    assert t["source_code"].startswith("struct MarketParams")
    assert "<span" in t["source_html"]


def test_no_signature_types_serializes_empty() -> None:
    # asdict keeps the empty tuple as a tuple here; json.dumps later turns it
    # into [] (the dumpability test covers that). The property under test is
    # that the field is present and empty, not its container type.
    d = serialize_flow(_binding_flow())  # _fn default has no signature_types
    assert d["root"]["signature_types"] == ()


def test_other_node_variants_have_no_signature_types() -> None:
    """Only FunctionNodes carry the panel; unresolved/external dicts don't."""
    d = serialize_flow(_binding_flow())
    # children[1] is an UnresolvedNode in _binding_flow.
    assert "signature_types" not in d["root"]["children"][1]


def test_signature_type_flow_is_json_dumpable() -> None:
    d = serialize_flow(_sig_type_flow())
    parsed = json.loads(json.dumps(d))
    assert parsed["root"]["signature_types"][0]["name"] == "MarketParams"
