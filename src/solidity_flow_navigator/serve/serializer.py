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

from ..flow.types import Flow, FlowNode, FunctionNode
from .highlight import highlight_solidity


def serialize_flow(flow: Flow) -> dict[str, Any]:
    """Convert a Flow into a plain dict ready for Jinja / JSON.

    The ``root`` key always carries a function-node dict (the entry point's
    body). Children are serialized recursively, preserving the discriminator
    field ``node_type``.

    ``signature_suffix`` is the ``(...)`` parameter list extracted from the
    canonical name; the template uses it together with
    ``entry_point_contract_name`` and ``entry_point_function_name`` to
    display the deployment-surface canonical (e.g.
    ``MockOwned.transferOwnership(address)``) instead of the declarer-keyed
    canonical Layer 2 emits.
    """
    canon = flow.entry_point_canonical_name
    paren = canon.find("(")
    signature_suffix = canon[paren:] if paren != -1 else ""
    return {
        "entry_point_canonical_name": flow.entry_point_canonical_name,
        "entry_point_contract_name": flow.entry_point_contract_name,
        "entry_point_function_name": flow.entry_point_function_name,
        "signature_suffix": signature_suffix,
        "root": _serialize_node(flow.root),
    }


def _serialize_node(node: FlowNode) -> dict[str, Any]:
    """Convert a FlowNode (function | unresolved | external) into a dict.

    For ``FunctionNode``: walks children recursively and adds ``source_html``.
    For the other variants: a plain ``asdict`` is sufficient — they have no
    children and no source body. ``asdict`` over a dataclass containing a
    ``StrEnum`` writes the enum's string value, which is what the template
    wants to render.
    """
    if isinstance(node, FunctionNode):
        d = asdict(node)
        d["source_html"] = highlight_solidity(node.source_code)
        d["children"] = [_serialize_node(c) for c in node.children]
        return d
    return asdict(node)
