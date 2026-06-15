"""Flow IR → template-ready dict tree, with embedded highlighted HTML.

Layer 2 ``Flow`` objects are dataclasses; Jinja can render them directly,
but the recursive node walk is cleaner over plain dicts, and the templates
will eventually be the source for Stage 3's embedded JSON. Converting once
at request time keeps a single shape between the static template render
(Stage 2) and the JS-driven graph layout (Stage 3).

The conversion is structurally ``dataclasses.asdict`` plus one extra field:
each ``FunctionNode`` gains a ``source_html`` key holding Pygments output.
Other node variants (``UnresolvedNode``, ``ExternalNode``) round-trip
unchanged.

The output dicts are JSON-serializable (StrEnum values become their string
forms via the dataclass machinery), so Stage 3 can ``json.dumps`` them
straight into ``<script type="application/json">`` without further
transformation.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..flow.types import (
    ExternalNode,
    Flow,
    FlowNode,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)
from .highlight import highlight_solidity


def serialize_flow(
    flow: Flow, bindings_ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert a Flow into a plain dict ready for Jinja / JSON.

    The ``root`` key always carries a function-node dict (the entry point's
    body). Children are serialized recursively, preserving the discriminator
    field ``node_type``.

    ``signature_suffix`` is the ``(...)`` parameter list extracted from the
    invoker canonical name; the template uses it together with
    ``entry_point_contract_name`` and ``entry_point_function_name`` to
    display the deployment-surface canonical (e.g.
    ``MockOwned.transferOwnership(address)``).
    """
    canon = flow.entry_point_invoker_canonical_name
    paren = canon.find("(")
    signature_suffix = canon[paren:] if paren != -1 else ""
    return {
        "entry_point_invoker_canonical_name": flow.entry_point_invoker_canonical_name,
        "entry_point_declarer_canonical_name": flow.entry_point_declarer_canonical_name,
        "entry_point_contract_name": flow.entry_point_contract_name,
        "entry_point_function_name": flow.entry_point_function_name,
        "signature_suffix": signature_suffix,
        "unresolved_count": flow.unresolved_count,
        "max_depth": flow.max_depth,
        "root": _serialize_node(flow.root, bindings_ctx),
    }


def _serialize_node(
    node: FlowNode, bindings_ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert a FlowNode (function | unresolved | external) into a dict.

    For ``FunctionNode``: walks children recursively and adds ``source_html``.
    For the other variants: a plain ``asdict`` is sufficient — they have no
    children and no source body. ``asdict`` over a dataclass containing a
    ``StrEnum`` writes the enum's string value, which is what the template
    wants to render.

    v0.5 exploration: unresolved/external dicts are normalized to expose a
    ``call_site_line`` field (1-indexed absolute file line of the call),
    mirroring the field of the same name on FunctionNode. This lets the
    progressive renderer apply one rule to every child variant when
    mapping a call to a line in the parent's source body.

    v0.17.0 (§10.2, §13.2): when ``bindings_ctx`` is supplied (the Flow page),
    interface-call nodes gain a ``binding`` dict so the renderer can show an
    inline bind dropdown on the node — the in-context counterpart to the index
    Bindings panel, writing the same single global binding.
    """
    if isinstance(node, FunctionNode):
        d = asdict(node)
        d["source_html"] = highlight_solidity(node.source_code)
        d["children"] = [_serialize_node(c, bindings_ctx) for c in node.children]
    else:
        d = asdict(node)
        if isinstance(node, (UnresolvedNode, ExternalNode)):
            d["call_site_line"] = (
                node.call_site.lines[0] if node.call_site.lines else None
            )
    if bindings_ctx is not None:
        _attach_binding(d, node, bindings_ctx)
    return d


def _attach_binding(d: dict[str, Any], node: FlowNode, ctx: dict[str, Any]) -> None:
    """Attach a ``binding`` dict to an interface-call node (§10.2, §13.2).

    The Flow-page renderer turns this into an inline dropdown so the auditor can
    resolve the interface directly on the node. Only attached when there is a
    real candidate to pick or an existing binding to change/clear — an empty
    dropdown would be useless.
    """
    iface = _node_interface(node, ctx)
    if iface is None:
        return
    candidates = ctx["candidates"].get(iface, ())
    bound_to = ctx["bound"].get(iface)
    if candidates or bound_to:
        d["binding"] = {
            "interface": iface,
            "bound_to": bound_to,
            "candidates": list(candidates),
        }


def _node_interface(node: FlowNode, ctx: dict[str, Any]) -> str | None:
    """The interface a node represents a call to, if it is a bindable interface
    call site (§13.2): a bound node, an unbound interface call rendered as the
    interface's own declaration, or an unresolved interface node. Else None.

    ``ctx["candidates"]`` is keyed by every bindable interface name, so
    membership doubles as the "is this an interface we know about" test.
    """
    known = ctx["candidates"]
    if isinstance(node, FunctionNode):
        if node.bound_via is not None:
            return node.bound_via.interface_name
        if node.call_kind == "external" and node.declarer_contract_name in known:
            return node.declarer_contract_name
        return None
    if isinstance(node, UnresolvedNode):
        if node.reason == UnresolvedReason.INTERFACE_CALL_NO_BINDING:
            head = node.descriptor.split(".", 1)[0].strip()
            return head if head in known else None
        if node.reason == UnresolvedReason.INTERFACE_BINDING_FAILED:
            head = node.descriptor.split(" →", 1)[0].strip()
            return head if head in known else None
    return None
