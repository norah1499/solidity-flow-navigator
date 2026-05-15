"""Build one Flow per entry point from Layer 1's RepoFacts.

This is the public Layer 2 entry point (spec §11.1). The transformation is
pure: facts in, ``tuple[Flow, ...]`` out. No I/O, no Slither imports.

The dispatch from ``CallEdge`` → ``FlowNode`` follows §11.9's table. Cycle
detection uses path-tracking (a per-Flow ``frozenset[str]`` of canonical
names threaded through the recursion); on a back-edge the cycle target is
emitted as a ``FunctionNode`` with empty ``children`` per spec §11.10's v0
limitation list.
"""

import re
from collections.abc import Iterator

from solidity_flow_navigator.analysis.types import (
    CallEdge,
    Contract,
    Function,
    RepoFacts,
)

from .modifiers import resolve_modifier
from .scope import (
    Scope,
    contract_excluded,
    library_inlined,
    path_excluded,
    target_stubbed,
)
from .types import (
    ExternalNode,
    Flow,
    FlowNode,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)
from .virtual_dispatch import resolve_virtual_override

# Names of Slither-synthesized pseudo-functions that occasionally surface as
# real-looking entry points in some Slither configurations. Layer 1 already
# marks them is_entry_point=False, but we filter by name as defense-in-depth
# per the original Stage 2 brief — the cost is one set lookup per function.
_SYNTHETIC_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "slitherConstructorVariables",
        "slitherConstructorConstantVariables",
    }
)

# §11.9 / §13.3 — Yul opcodes whose subkind string identifies dynamic
# external dispatch. The match is by prefix on the opcode name plus "(", to
# distinguish them from other kind="solidity" subkinds that share leading
# characters (e.g. "callvalue()", "calldatacopy(...)", "code(...)").
_YUL_DYNAMIC_DISPATCH_PREFIXES: tuple[str, ...] = (
    "call(",
    "callcode(",
    "delegatecall(",
    "staticcall(",
)


def is_yul_dynamic_dispatch_subkind(subkind: str | None) -> bool:
    """True if a kind="solidity" CallEdge's subkind names a Yul dynamic dispatch op (§13.3)."""
    if subkind is None:
        return False
    return subkind.startswith(_YUL_DYNAMIC_DISPATCH_PREFIXES)


def _is_external_path(filename_relative: str) -> bool:
    """True for sources under ``lib/`` or ``node_modules/`` (§11.8)."""
    return filename_relative.startswith("lib/") or filename_relative.startswith(
        "node_modules/"
    )


def build_flows(facts: RepoFacts, scope: Scope) -> tuple[Flow, ...]:
    """Build one Flow per entry point in ``facts``, applying ``scope`` rules.

    Scope rules (spec §11.4) apply at the outer Contract enumeration: an
    invoker contract whose source path matches ``scope.exclude_paths`` or
    whose name matches ``scope.exclude_contracts`` produces no Flows. The
    filter does NOT propagate into the base-walk inside ``entry_points_for``:
    an in-scope contract that inherits from a filtered base still surfaces
    the inherited entry points (the spec's "the contract" in §11.4 refers
    to the invoker, not the declarer).
    """

    builder = _FlowBuilder(facts, scope)
    flows: list[Flow] = []
    for contract in facts.contracts:
        if contract.kind != "contract":
            continue  # interfaces and libraries don't produce entry points (§11.4)
        if path_excluded(scope, contract.source_location.filename_relative):
            continue
        if contract_excluded(scope, contract.name):
            continue
        for entry_func in builder.entry_points_for(contract):
            flows.append(builder.build_flow(entry_func, invoker_contract=contract))
    return tuple(flows)


class _FlowBuilder:
    """Per-RepoFacts builder. Holds the lookup maps used during traversal.

    ``_current_invoker_contract`` is the Flow-scoped state used by
    every helper that emits a ``FunctionNode``: per spec §11.3 (v0.3) and
    §11.5, ``invoked_via_contract_name`` is the entry-point invoker contract
    for EVERY node in a given Flow, and within-body virtual dispatch is
    resolved through this contract's C3 chain. Set in ``build_flow`` for the
    duration of a single Flow's construction.
    """

    __slots__ = (
        "facts",
        "scope",
        "_contracts_by_name",
        "_functions_by_canonical",
        "_current_invoker_contract",
    )

    def __init__(self, facts: RepoFacts, scope: Scope) -> None:
        self.facts = facts
        self.scope = scope
        self._contracts_by_name: dict[str, Contract] = {
            c.name: c for c in facts.contracts
        }
        # All callable Function records (contract functions, modifiers, and
        # top-level free functions) keyed by canonical_name. Layer 1's
        # canonical_name is globally unique. Free functions have no contract
        # prefix in their canonical_name (e.g. "toStringOZ(uint256)") and
        # carry an empty contract_declarer_name.
        self._functions_by_canonical: dict[str, Function] = {}
        for c in facts.contracts:
            for f in c.functions:
                self._functions_by_canonical[f.canonical_name] = f
            for m in c.modifiers:
                self._functions_by_canonical[m.canonical_name] = m
        for ff in facts.free_functions:
            self._functions_by_canonical[ff.canonical_name] = ff
        self._current_invoker_contract: Contract | None = None

    # ------------------------------------------------------------------
    # Entry-point enumeration (§11.4)
    # ------------------------------------------------------------------

    def entry_points_for(self, contract: Contract) -> Iterator[Function]:
        """Yield each entry-point Function visible on ``contract``.

        Walks the contract itself first, then its linearized bases in C3
        order (most-derived first). Dedupes by ``full_name``, keeping the
        most-derived occurrence.

        Layer 1 reports ``linearized_base_contract_names`` as bases-only
        (excluding the contract itself), mirroring Slither's
        ``contract.inheritance``. The spec's instruction to "walk
        linearized_base_contract_names" only makes sense if the contract
        itself comes first, otherwise the contract's own externals (e.g.
        MockERC20.mint) would never enumerate. We prepend ``contract.name``
        explicitly.
        """

        seen_full_names: set[str] = set()
        chain: tuple[str, ...] = (
            contract.name,
        ) + contract.linearized_base_contract_names
        for base_name in chain:
            base = self._contracts_by_name.get(base_name)
            if base is None:
                # A linearized base that isn't in facts.contracts: skip
                # silently rather than raise. This can happen if the project
                # has source files Slither can see but the recon analysis
                # excluded; raising here would block the entire entry-point
                # enumeration over what may be a benign omission.
                continue
            for func in base.functions:
                if not func.is_entry_point:
                    continue
                if func.name in _SYNTHETIC_FUNCTION_NAMES:
                    continue
                if func.full_name in seen_full_names:
                    continue
                seen_full_names.add(func.full_name)
                yield func

    # ------------------------------------------------------------------
    # Flow construction (§11.5, §11.6, §11.7, §11.9)
    # ------------------------------------------------------------------

    def build_flow(self, entry_func: Function, invoker_contract: Contract) -> Flow:
        """Build a single Flow rooted at ``entry_func``, reached on
        ``invoker_contract``.

        Sets ``_current_invoker_contract`` for the duration so every
        ``FunctionNode`` emitted while constructing this Flow carries
        ``invoked_via_contract_name = invoker_contract.name`` (spec §11.3
        v0.3) and so within-body virtual calls re-resolve through this
        contract's C3 chain (spec §11.5).
        """

        # Path tracking is per-Flow: each entry point starts with a fresh
        # set so the same function can be expanded under two different
        # entry points without confusion.
        path: frozenset[str] = frozenset({entry_func.canonical_name})

        prev_invoker = self._current_invoker_contract
        self._current_invoker_contract = invoker_contract
        try:
            modifier_children = self._build_modifier_children(entry_func, path)
            body_children, body_builtins = self._process_calls(
                entry_func.calls,
                caller_declarer=entry_func.contract_declarer_name,
                path=path,
            )
            root_node = FunctionNode(
                canonical_name=entry_func.canonical_name,
                name=entry_func.name,
                full_name=entry_func.full_name,
                declarer_contract_name=entry_func.contract_declarer_name,
                invoked_via_contract_name=invoker_contract.name,
                invoked_via_super=False,
                is_modifier=False,
                visibility=entry_func.visibility,
                payable=entry_func.payable,
                view=entry_func.view,
                pure=entry_func.pure,
                source_code=entry_func.source_code,
                source_location=entry_func.source_location,
                builtins_used=body_builtins,
                children=modifier_children + body_children,
            )
            return Flow(
                entry_point_declarer_canonical_name=entry_func.canonical_name,
                entry_point_invoker_canonical_name=(
                    f"{invoker_contract.name}.{entry_func.full_name}"
                ),
                entry_point_contract_name=invoker_contract.name,
                entry_point_function_name=entry_func.name,
                root=root_node,
            )
        finally:
            self._current_invoker_contract = prev_invoker

    def _build_modifier_children(
        self, func: Function, path: frozenset[str]
    ) -> tuple[FlowNode, ...]:
        """Build the inline modifier nodes that precede ``func``'s body children (§11.6).

        Resolution is two-stage. ``resolve_modifier`` finds the lexical
        modifier visible from ``func``'s declaring contract — this matches
        what Solidity's parser binds. ``resolve_virtual_override`` then
        re-resolves through the Flow's invoker's C3 chain to find the
        most-derived implementation (spec §11.5, v0.3 stage 3). Virtual
        modifiers without any concrete implementation in the invoker's
        chain produce an ``UnresolvedNode`` with reason
        ``ABSTRACT_NO_IMPLEMENTATION`` in place of the modifier node.
        """

        if not func.modifier_names:
            return ()

        declarer = self._contracts_by_name.get(func.contract_declarer_name)
        if declarer is None:
            raise KeyError(
                f"declarer {func.contract_declarer_name!r} of function "
                f"{func.canonical_name!r} not present in facts; cannot "
                f"resolve its modifiers"
            )

        children: list[FlowNode] = []
        for mod_name in func.modifier_names:
            lexical_mod = resolve_modifier(mod_name, declarer, self._contracts_by_name)
            resolved = resolve_virtual_override(
                self._require_invoker(), lexical_mod, self._contracts_by_name
            )
            # v0.5 iter-2: locate the modifier-name application within the
            # function's signature so the renderer can anchor the modifier's
            # left-placed node + edge at the line where the name appears.
            # None = not found; the renderer falls back to default routing.
            csl = _modifier_application_line(func, mod_name)
            if resolved is None:
                children.append(
                    UnresolvedNode(
                        reason=UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION,
                        descriptor=_describe_abstract_no_impl(lexical_mod),
                        call_site=lexical_mod.source_location,
                        raw_kind="modifier",
                        raw_subkind=None,
                    )
                )
                continue
            children.append(
                self._build_function_node(
                    resolved,
                    invoked_via_super=False,
                    path=path,
                    call_site_line=csl,
                )
            )
        return tuple(children)

    def _build_function_node(
        self,
        func: Function,
        invoked_via_super: bool,
        path: frozenset[str],
        call_site_line: int | None = None,
    ) -> FunctionNode:
        """Recursive node builder. Emits a terminal node (empty children) on cycle.

        ``invoked_via_contract_name`` is always set to
        ``self._current_invoker_contract.name`` (spec §11.3 v0.3). The
        ``func`` argument is the post-resolution function to emit a node
        for — callers in ``_handle_internal_or_library`` and
        ``_handle_high_level`` apply ``resolve_virtual_override`` before
        passing the function in (stages 2 and 3 progressively).

        ``call_site_line`` is the v0.5-exploration field on FunctionNode;
        see ``FunctionNode.call_site_line`` docstring. Edge-handling callers
        pass the originating call's first line so the progressive renderer
        can anchor an expansion affordance to it. Modifier children pass
        None (no body-call origin).
        """

        invoker_name = self._invoker_name()

        if func.canonical_name in path:
            # v0: implicit cycle termination per spec §11.10. Renderer can
            # spot the cycle by matching canonical_name against an ancestor.
            return self._terminal_function_node(
                func, invoker_name, invoked_via_super, call_site_line
            )

        new_path = path | {func.canonical_name}
        children, builtins = self._process_calls(
            func.calls,
            caller_declarer=func.contract_declarer_name,
            path=new_path,
        )
        return FunctionNode(
            canonical_name=func.canonical_name,
            name=func.name,
            full_name=func.full_name,
            declarer_contract_name=func.contract_declarer_name,
            invoked_via_contract_name=invoker_name,
            invoked_via_super=invoked_via_super,
            is_modifier=func.is_modifier,
            visibility=func.visibility,
            payable=func.payable,
            view=func.view,
            pure=func.pure,
            source_code=func.source_code,
            source_location=func.source_location,
            builtins_used=builtins,
            children=children,
            call_site_line=call_site_line,
        )

    def _terminal_function_node(
        self,
        func: Function,
        invoked_via: str,
        invoked_via_super: bool,
        call_site_line: int | None = None,
    ) -> FunctionNode:
        return FunctionNode(
            canonical_name=func.canonical_name,
            name=func.name,
            full_name=func.full_name,
            declarer_contract_name=func.contract_declarer_name,
            invoked_via_contract_name=invoked_via,
            invoked_via_super=invoked_via_super,
            is_modifier=func.is_modifier,
            visibility=func.visibility,
            payable=func.payable,
            view=func.view,
            pure=func.pure,
            source_code=func.source_code,
            source_location=func.source_location,
            builtins_used=(),
            children=(),
            call_site_line=call_site_line,
        )

    def _invoker_name(self) -> str:
        """Return the current Flow's invoker-contract name. Raises if no Flow
        is being built (would indicate a programming error reaching the
        per-Flow helpers outside ``build_flow``).
        """

        return self._require_invoker().name

    def _require_invoker(self) -> Contract:
        """Return the current Flow's invoker ``Contract``, asserting it is set."""

        invoker = self._current_invoker_contract
        if invoker is None:
            raise AssertionError(
                "_FlowBuilder helper called outside build_flow; "
                "_current_invoker_contract is None"
            )
        return invoker

    # ------------------------------------------------------------------
    # CallEdge dispatch (§11.9)
    # ------------------------------------------------------------------

    def _process_calls(
        self,
        calls: tuple[CallEdge, ...],
        caller_declarer: str,
        path: frozenset[str],
    ) -> tuple[tuple[FlowNode, ...], tuple[str, ...]]:
        """Split outgoing calls into FlowNode children and folded builtin markers."""

        children: list[FlowNode] = []
        builtins: list[str] = []
        for edge in calls:
            result = self._dispatch_edge(
                edge,
                caller_declarer=caller_declarer,
                path=path,
            )
            if result is None:
                continue
            if isinstance(result, str):
                builtins.append(result)
            else:
                children.append(result)
        return tuple(children), tuple(builtins)

    def _dispatch_edge(
        self,
        edge: CallEdge,
        caller_declarer: str,
        path: frozenset[str],
    ) -> FlowNode | str | None:
        """Apply §11.9's dispatch table to one edge.

        Returns:
          - ``FlowNode`` for edges that become tree nodes;
          - ``str`` for edges folded into the parent's ``builtins_used``;
          - ``None`` is reserved for forward compatibility (currently unused).
        """

        kind = edge.kind

        if kind == "internal":
            return self._handle_internal_or_library(
                edge,
                caller_declarer=caller_declarer,
                path=path,
                is_library=False,
            )
        if kind == "library":
            return self._handle_internal_or_library(
                edge,
                caller_declarer=caller_declarer,
                path=path,
                is_library=True,
            )
        if kind == "high_level":
            return self._handle_high_level(edge, path=path)
        if kind == "low_level":
            return UnresolvedNode(
                reason=UnresolvedReason.LOW_LEVEL_CALL,
                descriptor=_describe_low_level(edge),
                call_site=edge.source_location,
                raw_kind=edge.kind,
                raw_subkind=edge.subkind,
            )
        if kind == "solidity":
            if is_yul_dynamic_dispatch_subkind(edge.subkind):
                return UnresolvedNode(
                    reason=UnresolvedReason.YUL_DYNAMIC_DISPATCH,
                    descriptor=edge.subkind or "<yul dispatch>",
                    call_site=edge.source_location,
                    raw_kind=edge.kind,
                    raw_subkind=edge.subkind,
                )
            return edge.subkind if edge.subkind else "<solidity-builtin>"
        if kind in ("send", "transfer", "new_contract"):
            # v0 simplification per §11.9 / §11.10: fold into builtins_used
            # rather than emit a dedicated node type. The marker is the
            # kind name itself; the parent function's source_code retains
            # the actual expression for the auditor to read.
            return kind
        raise ValueError(f"unknown CallEdge.kind: {kind!r}")

    def _handle_internal_or_library(
        self,
        edge: CallEdge,
        caller_declarer: str,
        path: frozenset[str],
        is_library: bool,
    ) -> FlowNode | None:
        """Internal and library calls recurse into the target's body, except
        for two cases that produce ``ExternalNode`` instead: targets whose
        source lies under ``lib/`` or ``node_modules/`` (§11.8), and
        Solidity top-level free functions (v0 simplification — see §11.10).

        Returns ``None`` for internal calls whose target is a modifier — those
        edges are de-duplicated with the modifier-folding path (see §11.6).

        Stage-2 (v0.3): for ``kind="internal"`` calls that are NOT super
        calls, the lexical target is re-resolved through the Flow's invoker
        contract's C3 chain per spec §11.5. Library calls are not affected
        (libraries don't participate in inheritance). Super calls are
        explicitly C3-resolved at the call site and bypass virtual dispatch.
        """

        if not edge.is_resolved or edge.target_canonical_name is None:
            # Internal/library calls in valid Solidity are statically
            # resolvable. If Slither couldn't bind one, that's a Layer 1
            # anomaly worth surfacing diagnostically.
            raise ValueError(
                f"{edge.kind} call edge unresolved or has no target "
                f"(target={edge.target_canonical_name!r}) at "
                f"{edge.source_location.filename_relative}:"
                f"{edge.source_location.lines}"
            )
        lexical_target = self._functions_by_canonical.get(edge.target_canonical_name)
        if lexical_target is None:
            raise KeyError(
                f"{edge.kind} call target {edge.target_canonical_name!r} not "
                f"present in facts (called from "
                f"{edge.source_location.filename_relative}:"
                f"{edge.source_location.lines})"
            )

        # MODIFIER DEDUP — DO NOT REMOVE.
        # Slither's IR represents an applied modifier in TWO places: as an
        # entry on Function.modifier_names AND as an InternalCall edge in the
        # function body's IR ops. Layer 2 emits the modifier child via the
        # modifier_names path (§11.6, in _build_modifier_children); without
        # this skip, the same modifier would appear twice in children — once
        # from modifier-folding and once from body-call processing. Verified
        # against Owned.transferOwnership during Stage 2 probing: source has
        # one onlyOwner, modifier_names=("onlyOwner",), body calls also list
        # Owned.onlyOwner() as kind="internal". Skipping is_modifier targets
        # here is the correct fix.
        if lexical_target.is_modifier:
            return None  # type: ignore[return-value]

        # Detect super.X() calls BEFORE applying virtual dispatch. Super
        # calls bind explicitly to a specific base in C3 order and bypass
        # virtual dispatch (spec §11.5). Without this gate, a super call
        # would re-resolve back to the most-derived override — the opposite
        # of what `super` means.
        invoked_via_super = (not is_library) and self._is_super_internal_call(
            lexical_target, caller_declarer
        )

        # Virtual dispatch (§11.5, v0.3): re-resolve through the invoker's
        # C3 chain for non-super internal calls. Library calls are exempt.
        if is_library or invoked_via_super:
            target: Function = lexical_target
        else:
            resolved = resolve_virtual_override(
                self._require_invoker(),
                lexical_target,
                self._contracts_by_name,
            )
            if resolved is None:
                return UnresolvedNode(
                    reason=UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION,
                    descriptor=_describe_abstract_no_impl(lexical_target),
                    call_site=edge.source_location,
                    raw_kind=edge.kind,
                    raw_subkind=edge.subkind,
                )
            target = resolved

        # External-vs-recurse decision in this precedence order (§11.8):
        # 1. ``stub_paths`` matches the target → ExternalNode. The auditor's
        #    explicit "stop here" wins over any default-stub override; this
        #    is the §11.8 conflict rule for the stub_paths-vs-inline_libraries
        #    overlap (e.g. ``lib/forge-std/**`` configured both ways).
        # 2. Free function (v0 simplification, §11.10) → ExternalNode.
        # 3. Library path under ``lib/`` or ``node_modules/`` → ExternalNode
        #    unless ``scope.inline_libraries`` names the package, in which
        #    case Layer 2 recurses (§11.2 / §11.8). Free functions are NOT
        #    covered by inline_libraries — that mechanism targets
        #    package-name-keyed directories, not standalone top-level
        #    functions, per §11.10's v0 simplification.
        # 4. Otherwise: recurse into the target's body.
        #
        # The check runs on the RESOLVED target — after virtual dispatch the
        # override may live in-tree even when the lexical target was under
        # lib/, which is the entire point of v0.3 (e.g. SablierLockup's
        # override of an OpenZeppelin virtual ERC721 method).
        target_path = target.source_location.filename_relative
        is_free_function = target.contract_declarer_name == ""
        if (
            target_stubbed(self.scope, target_path)
            or is_free_function
            or (
                _is_external_path(target_path)
                and not library_inlined(self.scope, target_path)
            )
        ):
            return self._external_node(target, edge)

        return self._build_function_node(
            target,
            invoked_via_super=invoked_via_super,
            path=path,
            call_site_line=_first_line_or_none(edge.source_location.lines),
        )

    def _handle_high_level(self, edge: CallEdge, path: frozenset[str]) -> FlowNode:
        """High-level (cross-contract via type) calls: recurse if Slither
        bound a target, else INTERFACE_CALL_NO_BINDING (§11.9, §13.1).

        Stage-3 (v0.3): when the bound target's contract is the Flow's
        invoker itself (self-call shape — ``this.X()`` or implicit-this
        Slither reports as high-level), apply ``resolve_virtual_override``
        to re-resolve through the invoker's C3 chain per spec §11.5. The
        target's contract being the invoker means we ARE in the self-call
        scenario; cross-contract high_level calls (target on a different
        contract) bypass virtual dispatch as before.
        """

        if not edge.is_resolved or edge.target_canonical_name is None:
            return UnresolvedNode(
                reason=UnresolvedReason.INTERFACE_CALL_NO_BINDING,
                descriptor=_describe_high_level(edge),
                call_site=edge.source_location,
                raw_kind=edge.kind,
                raw_subkind=edge.subkind,
            )
        lexical_target = self._functions_by_canonical.get(edge.target_canonical_name)
        if lexical_target is None:
            # Slither bound a name but the target isn't in our facts. Treat
            # the same as an unbound interface call: the auditor sees the
            # boundary and can read the source themselves.
            return UnresolvedNode(
                reason=UnresolvedReason.INTERFACE_CALL_NO_BINDING,
                descriptor=_describe_high_level(edge),
                call_site=edge.source_location,
                raw_kind=edge.kind,
                raw_subkind=edge.subkind,
            )

        # Self-call virtual dispatch (§11.5). Triggered when the bound
        # target's contract IS the Flow's invoker. Cross-contract
        # high_level calls do not flow through virtual dispatch — runtime
        # dispatch goes through the target's own contract type, not the
        # invoker's chain.
        invoker = self._require_invoker()
        is_self_call = lexical_target.contract_declarer_name == invoker.name
        if is_self_call:
            resolved = resolve_virtual_override(
                invoker, lexical_target, self._contracts_by_name
            )
            if resolved is None:
                return UnresolvedNode(
                    reason=UnresolvedReason.ABSTRACT_NO_IMPLEMENTATION,
                    descriptor=_describe_abstract_no_impl(lexical_target),
                    call_site=edge.source_location,
                    raw_kind=edge.kind,
                    raw_subkind=edge.subkind,
                )
            target: Function = resolved
        else:
            target = lexical_target

        # Same precedence as _handle_internal_or_library: stub_paths wins
        # over the default lib/-stub-vs-inline decision (§11.8 conflict rule).
        # Check runs on the RESOLVED target's path, matching the §11.5
        # rationale for kind="internal" — an override that lives in-tree
        # must not stub just because the lexical target sits in lib/.
        target_path = target.source_location.filename_relative
        if target_stubbed(self.scope, target_path) or (
            _is_external_path(target_path)
            and not library_inlined(self.scope, target_path)
        ):
            return self._external_node(target, edge)
        return self._build_function_node(
            target,
            invoked_via_super=False,
            path=path,
            call_site_line=_first_line_or_none(edge.source_location.lines),
        )

    def _external_node(self, target: Function, edge: CallEdge) -> ExternalNode:
        return ExternalNode(
            target_canonical_name=target.canonical_name,
            target_function_name=target.name,
            target_contract_name=target.contract_declarer_name or None,
            source_path=target.source_location.filename_relative,
            call_site=edge.source_location,
        )

    def _is_super_internal_call(self, target: Function, caller_declarer: str) -> bool:
        """Heuristic super-call detection (§11.5).

        Layer 1 tags ``super.X()`` calls as ``kind="internal"`` with the
        resolved target — same as a normal inherited call. The two are
        distinguishable structurally: an internal call is a ``super`` call
        iff the target is declared on a base contract AND the caller's own
        contract independently declares a function with the same
        ``full_name``. Without ``super``, Solidity's C3 lookup would have
        bound the call to the caller's own version.

        This is a heuristic, not a Slither flag. Slither has
        ``SuperCallExpression`` internally but Layer 1's CallEdge
        intentionally doesn't import Slither types. Surfacing it would
        require a Layer 1 amendment; the heuristic is exact for normal
        inheritance and only ambiguous in pathological cases that Solmate
        does not exhibit.
        """

        if target.contract_declarer_name == caller_declarer:
            return False  # same-contract call cannot be super
        caller_contract = self._contracts_by_name.get(caller_declarer)
        if caller_contract is None:
            return False
        for f in caller_contract.functions:
            if (
                f.full_name == target.full_name
                and f.canonical_name != target.canonical_name
            ):
                return True
        return False


# ---------------------------------------------------------------------------
# Descriptor formatters for unresolved-node labels
# ---------------------------------------------------------------------------


def _first_line_or_none(lines: tuple[int, ...]) -> int | None:
    """First line number from a SourceLocation's ``lines`` tuple, or ``None``
    if it is empty. Used to populate ``FunctionNode.call_site_line`` from
    the originating call edge (v0.5 exploration field).
    """
    return lines[0] if lines else None


def _modifier_application_line(func: Function, modifier_name: str) -> int | None:
    """Locate the 1-indexed absolute file line where ``modifier_name`` appears
    in ``func``'s signature (v0.5 exploration helper).

    Slither's IR doesn't expose per-modifier-application source locations,
    so this is a greppy fallback: word-boundary match for ``modifier_name``
    in the substring of ``func.source_code`` BEFORE the first ``{`` (i.e.,
    the signature/header). Returns the absolute file line of the first
    match, or ``None`` if no match is found (renderer then falls back to
    default routing for the modifier edge).

    Restricting the search to the header avoids false matches in the body
    where another local function/variable might share the modifier's name.
    Modifier names that collide with the function name itself remain a
    pathological edge case we accept missing for the prototype.
    """
    src = func.source_code or ""
    brace = src.find("{")
    header = src[:brace] if brace != -1 else src
    pat = re.compile(r"\b" + re.escape(modifier_name) + r"\b")
    m = pat.search(header)
    if m is None:
        return None
    relative_line = header[: m.start()].count("\n")
    if not func.source_location.lines:
        return None
    return func.source_location.lines[0] + relative_line


def _describe_low_level(edge: CallEdge) -> str:
    """Human label for a low_level UnresolvedNode (e.g. 'addr.call(...)')."""
    op = edge.subkind or "call"
    return f"<address>.{op}(...)"


def _describe_high_level(edge: CallEdge) -> str:
    """Human label for an unbound interface call."""
    iface = edge.target_contract_name or "<interface>"
    fn = edge.target_function_name or "<function>"
    return f"{iface}.{fn}(...)"


def _describe_abstract_no_impl(lexical_target: Function) -> str:
    """Human label for an ABSTRACT_NO_IMPLEMENTATION unresolved node.

    The descriptor names the lexical target whose signature has no
    concrete implementation in the invoker's C3 chain — useful for the
    renderer's label and for the auditor reading the source themselves.
    """
    declarer = lexical_target.contract_declarer_name or "<contract>"
    return f"{declarer}.{lexical_target.full_name} (no concrete impl)"
