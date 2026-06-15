"""Flow IR dataclasses: the per-entry-point Flow object, its node variants,
and the UnresolvedReason enum.

This module is the public Layer 2 → Layer 3 contract (spec §11.3). Layer 3
serializes these via ``dataclasses.asdict`` and exposes them over JSON; the
``node_type`` field discriminates the FlowNode union for the frontend.

Stdlib + ``typing`` only, per the architectural rule (CLAUDE.md, spec §7).
``SourceLocation`` is reused from Layer 1 rather than redefined: Layer 2
passes it through unchanged (per §11.3, "passed through from Layer 1"), and
redefining it would create two parallel types that must be kept in sync.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from solidity_flow_navigator.analysis.types import SourceLocation


class UnresolvedReason(StrEnum):
    """Why Layer 2 could not (or chose not to) resolve a call site to a concrete target.

    Values are spec-defined (§11.3) and serialize as their string values via
    ``StrEnum`` — no custom JSON encoder needed.
    """

    INTERFACE_CALL_NO_BINDING = "interface_call_no_binding"  # §13.1
    # §13.2 — a binding was set for this interface but did not resolve: the
    # bound contract is out of scope, or implements no function matching the
    # called signature. Distinct from INTERFACE_CALL_NO_BINDING (no binding at
    # all) so a wrong/typo'd binding is a loud error, never a silent fall-back.
    INTERFACE_BINDING_FAILED = "interface_binding_failed"  # §13.2
    LOW_LEVEL_CALL = "low_level_call"  # §13.3 Solidity-level
    YUL_DYNAMIC_DISPATCH = "yul_dynamic_dispatch"  # §13.3 Yul-level
    ABSTRACT_NO_IMPLEMENTATION = "abstract_no_implementation"  # §11.5 (v0.3)


@dataclass(frozen=True, slots=True)
class Binding:
    """An interface→contract binding recorded on a bound FunctionNode (§13.2).

    Present (non-None ``FunctionNode.bound_via``) only when an interface call
    was resolved into a concrete contract the auditor pinned via the Bindings
    panel, ``solflow.toml``'s ``[bindings]`` table, or a ``--bind`` flag. The
    renderer badges the node ``bound: {interface_name} → {contract_name}`` so
    the asserted (not statically proven) nature of the edge stays visible (P4).
    """

    interface_name: str  # static interface type at the call site, e.g. "IStrategy"
    contract_name: str  # concrete contract the auditor bound it to, e.g. "AaveStrategy"


@dataclass(frozen=True, slots=True)
class FunctionNode:
    """A resolved function or modifier in the call tree.

    ``invoked_via_contract_name`` records the contract through which this
    function was reached for the current Flow. For a Flow root that is an
    inherited entry point, this differs from ``declarer_contract_name`` (the
    contract that actually declared the function); for a non-inherited root
    or any internally-called function, the two are equal.

    ``builtins_used`` collects ``CallEdge.subkind`` values for ``kind="solidity"``
    edges that are NOT dynamic dispatch (per §13.3), plus folded markers for
    ``send``/``transfer``/``new_contract`` edges (§11.9 v0 simplification). Order
    and duplicates are preserved.

    ``call_site_line`` (v0.5 exploration; not in spec) is the 1-indexed
    absolute file line of the call statement that produced this node, taken
    from the originating ``CallEdge.source_location.lines[0]``. It is ``None``
    for the Flow root (no parent call) and for modifier children (the
    modifier is applied at the function header, not a body call). The
    progressive renderer uses it to identify which line of the parent's
    source body to make clickable for expansion; line-level granularity is
    sufficient for the prototype.

    ``call_kind`` (v0.10.4, spec §11.3) is how the parent reached this node:
    ``"internal"`` (direct or inherited internal call, including high-level
    self-calls re-resolved through the invoker's C3 chain), ``"library"``
    (any call whose resolved target is declared on a library — using-for
    calls, ``Library.fn(...)`` calls, and intra-library internal calls,
    which Slither reports as ``kind="internal"`` but which cross the same
    contract→library boundary), or ``"external"`` (high-level call on
    another contract or interface that Slither bound).
    ``None`` for the Flow root and for modifier children, which have no
    originating call edge. Disambiguates the three reasons
    ``declarer_contract_name`` can differ from ``invoked_via_contract_name``;
    the renderer keys title qualification and the relation badge off it
    (§10.2).
    """

    node_type: Literal["function"] = field(default="function", kw_only=True)
    canonical_name: str
    name: str
    full_name: str
    declarer_contract_name: str
    invoked_via_contract_name: str
    invoked_via_super: bool
    is_modifier: bool
    visibility: str  # "external" | "public" | "internal" | "private"
    payable: bool
    view: bool
    pure: bool
    source_code: str
    source_location: SourceLocation
    builtins_used: tuple[str, ...]
    children: tuple["FlowNode", ...]
    # v0.5 exploration field — see class docstring. Optional + kw_only so
    # existing test/code constructions remain valid without amendment.
    call_site_line: int | None = field(default=None, kw_only=True)
    # v0.10.4 relation field (spec §11.3) — see class docstring. Optional +
    # kw_only for the same construction-compatibility reason as above.
    call_kind: str | None = field(default=None, kw_only=True)
    # v0.17.0 interface-binding marker (spec §11.3, §13.2). Non-None iff this
    # node was reached by resolving an interface call through a user-supplied
    # binding. None for every normally-resolved node. A bound node carries
    # call_kind="external" (it remains a cross-contract boundary); bound_via
    # is the ADDITIONAL signal the renderer badges. Optional + kw_only so
    # existing constructions stay valid.
    bound_via: "Binding | None" = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class UnresolvedNode:
    """A call site whose target Layer 2 could not (or chose not to) resolve.

    The auditor's response to this node is "the tool cannot help me here";
    contrast with ``ExternalNode``, where the tool CAN see the target but
    deliberately does not follow into it (§11.8).
    """

    node_type: Literal["unresolved"] = field(default="unresolved", kw_only=True)
    reason: UnresolvedReason
    descriptor: str
    call_site: SourceLocation
    raw_kind: str
    raw_subkind: str | None


@dataclass(frozen=True, slots=True)
class ExternalNode:
    """A call into a dependency under ``lib/`` or ``node_modules/`` that
    Layer 2 chose not to follow (§11.8).

    Distinct from ``UnresolvedNode``: the target IS resolved, but the body is
    out of scope for the current Flow. v0.1 scope rules will allow per-glob
    overrides.
    """

    node_type: Literal["external"] = field(default="external", kw_only=True)
    target_canonical_name: str
    target_function_name: str
    target_contract_name: str | None
    source_path: str  # repo-relative path of the dependency file
    call_site: SourceLocation


FlowNode = FunctionNode | UnresolvedNode | ExternalNode


@dataclass(frozen=True, slots=True)
class Flow:
    """The rendered artifact for one entry point (spec §11.3).

    ``entry_point_invoker_canonical_name`` is the canonical name of the entry
    point keyed on the invoking contract (e.g.
    ``MockOwned.transferOwnership(address)``). Constructed by Layer 2 as
    ``f"{invoker_contract.name}.{function.full_name}"``. Unique across all
    Flows in a given ``RepoFacts`` — this is the load-bearing identifier
    downstream layers use for routing and lookup.

    ``entry_point_declarer_canonical_name`` is the canonical name of the
    C3-resolved declarer (where the function body lives), passed through
    from Layer 1 (e.g. ``Owned.transferOwnership(address)`` for an inherited
    entry point). Equal across all Flows that share an inherited function,
    so it is informational only — not unique per Flow. Equal to
    ``entry_point_invoker_canonical_name`` when the entry point is not
    inherited.

    ``entry_point_contract_name`` is the contract the entry point is invoked
    on; for an inherited entry point this differs from
    ``root.declarer_contract_name``.

    ``unresolved_count`` is the number of ``UnresolvedNode`` descendants
    reachable from ``root`` (inclusive — an unresolved root counts as 1; the
    function-node root case naturally contributes 0). v0.8.0 derivation
    consumed by the Layer 3 index page (§8.3 per-entry metadata column).

    ``max_depth`` is the maximum edge-distance from ``root`` to any leaf —
    a leaf-rooted Flow has ``max_depth == 0``. Same v0.8.0 derivation /
    consumer as ``unresolved_count``.
    """

    entry_point_declarer_canonical_name: str
    entry_point_invoker_canonical_name: str
    entry_point_contract_name: str
    entry_point_function_name: str
    root: FunctionNode
    unresolved_count: int
    max_depth: int
