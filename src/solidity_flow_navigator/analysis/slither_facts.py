"""Extracts raw facts from Slither's object model into the RepoFacts dataclass tree.

Layer 1's only interpretive job: mapping SlithIR operation classes to the fixed
``CallEdge.kind`` taxonomy. Everything else is verbatim transcription of what
Slither already computed (inheritance order, source mappings, source text,
binding outcomes).
"""

from pathlib import Path

from crytic_compile import CryticCompile
from slither import Slither
from slither.core.declarations import Contract as SlitherContract
from slither.core.declarations import Function as SlitherFunction
from slither.core.declarations import FunctionContract
from slither.core.declarations import Modifier as SlitherModifier
from slither.core.variables.state_variable import StateVariable
from slither.slithir.operations import (
    HighLevelCall,
    InternalCall,
    LibraryCall,
    LowLevelCall,
    NewContract,
    Operation,
    Send,
    SolidityCall,
    Transfer,
)

from .types import (
    CallEdge,
    Contract,
    Function,
    Parameter,
    RepoFacts,
    SourceLocation,
)


def extract_facts(
    crytic_compile_obj: CryticCompile, repo_path: Path | str
) -> RepoFacts:
    """Run Slither over the compiled artifacts and return the raw facts tree.

    ``repo_path`` is resolved to an absolute path, recorded on the result, and
    used as the base for every ``SourceLocation.filename_relative`` so all
    relative paths in the output are repo-rooted (not CWD-relative).
    """

    repo_root = Path(repo_path).resolve()
    slither = Slither(crytic_compile_obj)
    contracts = tuple(_build_contract(c, repo_root) for c in slither.contracts)
    free_functions = _build_free_functions(slither, repo_root)
    return RepoFacts(
        repo_path=str(repo_root),
        contracts=contracts,
        free_functions=free_functions,
    )


# ---------------------------------------------------------------------------
# Contract / function / modifier construction
# ---------------------------------------------------------------------------


def _build_contract(c: SlitherContract, repo_root: Path) -> Contract:
    declared = tuple(
        _build_function(f, repo_root, is_modifier=False) for f in c.functions_declared
    )
    # Public state variables auto-generate getter functions in Solidity. Slither
    # resolves cross-contract calls to those getters but doesn't surface them in
    # ``functions_declared``; we synthesize Function records so they're indexable
    # by canonical_name like any other entry point.
    getters = tuple(
        _build_synthetic_getter(v, c, repo_root)
        for v in c.state_variables_declared
        if v.visibility == "public"
    )
    functions = declared + getters
    modifiers = tuple(
        _build_function(m, repo_root, is_modifier=True) for m in c.modifiers_declared
    )
    return Contract(
        name=c.name,
        kind=_contract_kind(c),
        is_interface=c.is_interface,
        is_library=c.is_library,
        is_abstract=bool(getattr(c, "is_abstract", False)),
        linearized_base_contract_names=tuple(b.name for b in c.inheritance),
        immediate_base_contract_names=tuple(b.name for b in c.immediate_inheritance),
        source_location=_source_location(c.source_mapping, repo_root),
        functions=functions,
        modifiers=modifiers,
    )


def _contract_kind(c: SlitherContract) -> str:
    # Slither sets contract_kind to "contract" / "interface" / "library" (it can be
    # None on legacy AST output, but Solidity 0.8.x always sets it). We collapse
    # the None case to "contract" since is_interface / is_library are independent
    # signals and is_abstract carries the abstract flag.
    return c.contract_kind or "contract"


def _build_function(
    f: SlitherFunction, repo_root: Path, *, is_modifier: bool
) -> Function:
    # Modifiers are FunctionContract subclasses but their .visibility is a
    # placeholder ("internal"). Keep the raw value either way.
    declarer = (
        f.contract_declarer.name
        if isinstance(f, FunctionContract) and f.contract_declarer is not None
        else ""
    )
    parameters = tuple(_parameter(p) for p in f.parameters)
    returns = tuple(_parameter(p) for p in f.returns)
    modifier_names = tuple(m.name for m in f.modifiers)
    # Walk this function's OWN CFG nodes only. ``all_slithir_operations()``
    # would recursively flatten ops from every reachable callee into this
    # function's edge list, including ops inside library / assembly blocks
    # of those callees. That defeats the purpose of having one CallEdge per
    # actual outgoing call site in the function's body.
    calls = tuple(
        edge
        for node in f.nodes
        for edge in (_call_edge(op, repo_root) for op in node.irs)
        if edge is not None
    )
    return Function(
        canonical_name=f.canonical_name,
        name=f.name,
        full_name=f.full_name,
        contract_declarer_name=declarer,
        visibility=f.visibility,
        is_constructor=bool(getattr(f, "is_constructor", False)),
        is_fallback=bool(getattr(f, "is_fallback", False)),
        is_receive=bool(getattr(f, "is_receive", False)),
        is_modifier=is_modifier,
        is_implemented=bool(f.is_implemented),
        is_entry_point=_is_entry_point(f, is_modifier=is_modifier),
        payable=bool(getattr(f, "payable", False)),
        view=bool(getattr(f, "view", False)),
        pure=bool(getattr(f, "pure", False)),
        parameters=parameters,
        returns=returns,
        modifier_names=modifier_names,
        source_location=_source_location(f.source_mapping, repo_root),
        source_code=f.source_mapping.content or "",
        calls=calls,
    )


def _parameter(p) -> Parameter:
    return Parameter(
        name=p.name or "", type_str=str(p.type) if p.type is not None else ""
    )


def _is_entry_point(f: SlitherFunction, *, is_modifier: bool) -> bool:
    if is_modifier:
        return False
    if getattr(f, "is_constructor", False):
        return False
    if not isinstance(f, FunctionContract):
        # Free functions / top-level functions are never entry points in the
        # per-contract Flow model.
        return False
    declarer = f.contract_declarer
    if declarer is None or declarer.is_interface or declarer.is_library:
        return False
    if getattr(f, "is_receive", False) or getattr(f, "is_fallback", False):
        return True
    return f.visibility in ("public", "external")


# ---------------------------------------------------------------------------
# Call-edge construction
# ---------------------------------------------------------------------------


def _call_edge(op: Operation, repo_root: Path) -> CallEdge | None:
    """Translate a SlithIR operation into a CallEdge, or None for non-call ops."""

    # LibraryCall is a subclass of HighLevelCall in Slither - check it first.
    if isinstance(op, LibraryCall):
        return _high_or_library_edge(op, repo_root, kind="library")
    if isinstance(op, HighLevelCall):
        return _high_or_library_edge(op, repo_root, kind="high_level")
    if isinstance(op, InternalCall):
        return _internal_edge(op, repo_root)
    if isinstance(op, LowLevelCall):
        return CallEdge(
            kind="low_level",
            subkind=str(op.function_name) if op.function_name is not None else None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
        )
    if isinstance(op, SolidityCall):
        sf_name = op.function.name if op.function is not None else None
        return CallEdge(
            kind="solidity",
            subkind=sf_name,
            target_canonical_name=None,
            target_function_name=sf_name,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
        )
    if isinstance(op, Send):
        return CallEdge(
            kind="send",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
        )
    if isinstance(op, Transfer):
        return CallEdge(
            kind="transfer",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
        )
    if isinstance(op, NewContract):
        # NewContract.contract_name is a Slither UserDefinedType, not a Python
        # str. str() on it returns the contract type's name (e.g. "MockAuth").
        raw = getattr(op, "contract_name", None)
        return CallEdge(
            kind="new_contract",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=str(raw) if raw is not None else None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
        )
    return None


def _internal_edge(op: InternalCall, repo_root: Path) -> CallEdge:
    target_fn = op.function
    target_canonical = target_fn.canonical_name if target_fn is not None else None
    target_name = (
        target_fn.name
        if target_fn is not None
        else (str(op.function_name) if op.function_name is not None else None)
    )
    target_contract = (
        target_fn.contract.name
        if isinstance(target_fn, FunctionContract) and target_fn.contract is not None
        else None
    )
    return CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name=target_canonical,
        target_function_name=target_name,
        target_contract_name=target_contract,
        is_resolved=target_fn is not None,
        source_location=_op_location(op, repo_root),
    )


def _high_or_library_edge(op: HighLevelCall, repo_root: Path, *, kind: str) -> CallEdge:
    target_fn = op.function
    if isinstance(target_fn, StateVariable):
        # Slither models a high_level call to a public state variable as a call
        # to the variable itself; ``StateVariable.canonical_name`` is
        # ``"Contract.varname"`` (no parentheses). Layer 2 looks targets up by
        # the with-signature canonical_name of the synthetic getter we emit on
        # the same contract; build that form here so the two sides match.
        target_canonical = f"{target_fn.contract.name}.{target_fn.full_name}"
        target_name = target_fn.name
        target_contract = target_fn.contract.name
    else:
        target_canonical = target_fn.canonical_name if target_fn is not None else None
        target_name = (
            target_fn.name
            if target_fn is not None
            else (str(op.function_name) if op.function_name is not None else None)
        )
        target_contract = (
            target_fn.contract.name
            if isinstance(target_fn, FunctionContract)
            and target_fn.contract is not None
            else None
        )
    return CallEdge(
        kind=kind,
        subkind=None,
        target_canonical_name=target_canonical,
        target_function_name=target_name,
        target_contract_name=target_contract,
        is_resolved=target_fn is not None,
        source_location=_op_location(op, repo_root),
    )


def _build_synthetic_getter(
    var: StateVariable, contract: SlitherContract, repo_root: Path
) -> Function:
    """Build a Function record for a public state variable's auto-getter.

    Mirrors what the Solidity compiler synthesizes: a public, view function
    whose ``full_name`` encodes the getter signature derived from the
    variable's type (e.g. ``balanceOf(address)`` for
    ``mapping(address => uint256) public balanceOf``). Slither populates
    ``StateVariable.full_name`` with this signature already.

    Parameters and returns are left empty in v0 — Layer 2 only uses the
    canonical_name for lookup and source_code/source_location for display.
    Reconstructing structured Parameter records from the variable's type
    isn't needed for the v0 use case.
    """

    declarer_name = contract.name
    is_ep = (not contract.is_interface) and (not contract.is_library)
    return Function(
        canonical_name=f"{declarer_name}.{var.full_name}",
        name=var.name,
        full_name=var.full_name,
        contract_declarer_name=declarer_name,
        visibility="public",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_entry_point=is_ep,
        payable=False,
        view=True,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_source_location(var.source_mapping, repo_root),
        source_code=var.source_mapping.content or "",
        calls=(),
    )


def _build_free_functions(slither: Slither, repo_root: Path) -> tuple[Function, ...]:
    """Extract Solidity top-level (file-scope) free functions.

    Free functions live outside any contract; Slither places them under
    ``CompilationUnit.functions_top_level``. They share the regular
    ``Function`` shape — ``contract_declarer_name`` is the empty string,
    ``is_entry_point`` is False, modifiers/visibility quirks are absent.

    Dedupes across compilation units by ``canonical_name`` defensively;
    Slither typically reports each free function once, but multiple
    compilation units can report overlapping declarations after some
    crytic-compile configurations.
    """

    seen: set[str] = set()
    out: list[Function] = []
    for cu in slither.compilation_units:
        for f in cu.functions_top_level:
            if f.canonical_name in seen:
                continue
            seen.add(f.canonical_name)
            out.append(_build_function(f, repo_root, is_modifier=False))
    return tuple(out)


# ---------------------------------------------------------------------------
# Source mapping helpers
# ---------------------------------------------------------------------------


def _source_location(sm, repo_root: Path) -> SourceLocation:
    abs_path = Path(str(sm.filename.absolute))
    try:
        rel = str(abs_path.relative_to(repo_root))
    except ValueError:
        # File is outside the analyzed repo root (e.g. a system-wide installed
        # dependency referenced by absolute path). Fall back to the absolute
        # path so the field remains a usable filesystem reference.
        rel = str(abs_path)
    return SourceLocation(
        filename_absolute=str(abs_path),
        filename_relative=rel,
        start=sm.start,
        length=sm.length,
        lines=tuple(sm.lines),
        starting_column=sm.starting_column,
        ending_column=sm.ending_column,
    )


def _op_location(op: Operation, repo_root: Path) -> SourceLocation:
    # SlithIR operations carry their location via the parent CFG node. For
    # multi-call statements the same node-level location is reused for each
    # contained call; that's an acceptable v0 fidelity loss.
    return _source_location(op.node.source_mapping, repo_root)


# Make linter happy about unused symbols imported only for isinstance checks.
_ = SlitherModifier
