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

from solidity_flow_navigator.flow.types import Flow
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
    through SafeTransferLib's inline assembly call."""
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "ERC4626.deposit(uint256,address)", "ERC4626"
    )
    d = serialize_flow(flow)

    def find_unresolved(node: dict) -> dict | None:
        if node["node_type"] == "unresolved":
            return node
        if node["node_type"] != "function":
            return None
        for c in node["children"]:
            hit = find_unresolved(c)
            if hit is not None:
                return hit
        return None

    u = find_unresolved(d["root"])
    assert u is not None
    # StrEnum -> str via dataclasses.asdict.
    assert isinstance(u["reason"], str)
    assert u["reason"] == "yul_dynamic_dispatch"
    assert "source_html" not in u  # only FunctionNodes get a body


def test_external_node_round_trips(solmate_flows: tuple[Flow, ...]) -> None:
    """An AuthTest entry point reaches DSTest.assertEq under lib/ds-test/."""
    flow = _flow_by_canonical_and_contract(
        solmate_flows, "AuthTest.testTransferOwnershipAsOwner()", "AuthTest"
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
