"""Layer 1 output dataclasses: raw facts produced by the Slither wrapper.

These types are the public boundary between Layer 1 and Layer 2. Layer 2
imports this module and consumes these objects without any other Slither
contact. All fields are stdlib-typed (no Slither references leak through).

Frozen + slotted: Layer 2 cannot mutate these in place, and they're cheap
to construct in bulk on a multi-thousand-LOC repo.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Where a thing lives in the source.

    Byte offsets, line numbers, and columns are taken verbatim from Slither's
    ``source_mapping``. ``filename_absolute`` is the resolved absolute path on
    disk; ``filename_relative`` is the path relative to the analyzed
    repository root (``RepoFacts.repo_path``) — recomputed by Layer 1, NOT
    inherited from Slither's CWD-relative naming.

    If a source file lies outside the repository root (e.g. an installed
    dependency referenced by absolute path), ``filename_relative`` falls back
    to the absolute path so it remains a usable filesystem reference.
    """

    filename_absolute: str
    filename_relative: str  # relative to the analyzed repository root
    start: int  # byte offset into the file (UTF-8)
    length: int  # bytes
    lines: tuple[int, ...]  # 1-indexed line numbers covered, in order
    starting_column: int
    ending_column: int


@dataclass(frozen=True, slots=True)
class Parameter:
    """A function parameter or return value. Name may be empty for unnamed returns."""

    name: str
    type_str: str  # e.g. "uint256", "address[]", "MyStruct"


@dataclass(frozen=True, slots=True)
class CallEdge:
    """One outgoing call from a function or modifier body.

    ``kind`` preserves Slither's SlithIR operation category VERBATIM:
        "internal"     - InternalCall (same contract or inherited)
        "high_level"   - HighLevelCall (call on another contract via its type)
        "library"      - LibraryCall (using-for or Library.fn(...))
        "low_level"    - LowLevelCall (.call / .delegatecall / .staticcall / .callcode)
        "solidity"     - SolidityCall (require, assert, keccak256, etc.)
        "send"         - Send (.send)
        "transfer"     - Transfer (.transfer)
        "new_contract" - NewContract (``new Foo(...)``)

    Layer 1 does NOT decide what's "unresolved" - that's a Layer 2 concept
    governed by user scope (spec §12). Layer 1 reports ``is_resolved=False``
    only when Slither itself could not bind the call to a concrete Function.

    ``is_resolved`` is meaningful only for ``kind in {"internal", "high_level",
    "library"}`` - the kinds where Slither attempts function-level binding.
    For "low_level", "solidity", "send", "transfer", and "new_contract", the
    operation is terminal at the SlithIR level (no concrete callee Function
    to bind to), and ``is_resolved`` is reported as False uniformly. Layer 2
    must NOT treat a False on those terminal kinds as a P4 "unresolved
    branch" - they are simply opaque-by-nature operations.
    """

    kind: str
    # "delegatecall"/"staticcall"/... for low_level;
    # builtin name for solidity; None otherwise.
    subkind: str | None
    # e.g. "ERC20.transferFrom(address,address,uint256)".
    target_canonical_name: str | None
    target_function_name: str | None  # bare name when known
    # static type at the call site (e.g. "IERC20");
    # NOT the runtime impl - that's Layer 2's job.
    target_contract_name: str | None
    is_resolved: bool  # see docstring above for meaning per-kind
    source_location: SourceLocation  # location of the call site (statement-level)


@dataclass(frozen=True, slots=True)
class Function:
    """A function OR modifier. ``is_modifier`` discriminates.

    Modifiers share this shape because Slither models them as Function
    subclasses with the same surface (parameters, source mapping, calls in body).
    Carrying both in one type lets Layer 2 walk them uniformly.

    ``contract_declarer_name`` is the only contract-name field: each Function
    record appears exactly once in the RepoFacts tree, attached to its
    declaring contract. Layer 2 reconstructs what's visible on derived
    contracts by walking ``Contract.linearized_base_contract_names``.
    """

    canonical_name: str  # "Contract.fn(types)" - globally unique
    name: str  # bare name (or "fallback"/"receive"/"constructor")
    full_name: str  # "fn(types)" - no contract prefix
    contract_declarer_name: str  # contract that originally declared this function
    visibility: str  # "public" | "external" | "internal" | "private"
    is_constructor: bool
    is_fallback: bool
    is_receive: bool
    is_modifier: bool
    is_implemented: bool  # False for interface/abstract declarations
    # external/public on a non-interface non-library declarer, OR receive/fallback.
    # Layer 2 still walks inheritance to enumerate per-contract entry points
    # (incl. inherited).
    is_entry_point: bool
    payable: bool
    view: bool
    pure: bool
    parameters: tuple[Parameter, ...]
    returns: tuple[Parameter, ...]
    # names of applied modifiers, in source order; Layer 2 resolves to
    # Modifier objects via contract_declarer_name + linearized_base_contract_names.
    modifier_names: tuple[str, ...]
    source_location: SourceLocation
    source_code: str  # raw text of the declaration; for Pygments in Layer 3
    calls: tuple[CallEdge, ...]  # outgoing calls in source order


@dataclass(frozen=True, slots=True)
class Contract:
    """A contract, interface, or library.

    ``functions`` and ``modifiers`` contain only what's DECLARED here. Inherited
    members appear under their declarer's Contract; Layer 2 walks
    ``linearized_base_contract_names`` (C3 order) to assemble what's visible
    on a concrete contract. This avoids duplicating Function records and
    matches Slither's own model.
    """

    name: str
    kind: str  # "contract" | "interface" | "library"
    is_interface: bool
    is_library: bool
    is_abstract: bool  # orthogonal to kind; abstract contracts are kind=="contract"
    linearized_base_contract_names: tuple[str, ...]  # C3 order, names only
    immediate_base_contract_names: tuple[str, ...]
    source_location: SourceLocation
    functions: tuple[Function, ...]  # declared here only
    modifiers: tuple[Function, ...]  # declared here only; all have is_modifier=True


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """Top-level Layer 1 output. Everything Layer 2 needs to build Flows.

    ``free_functions`` carries Solidity's top-level (file-scope) functions,
    which are genuinely contract-less — Slither does not place them under
    any Contract. Their ``Function.contract_declarer_name`` is the empty
    string. Layer 2 needs them to resolve internal call edges whose
    ``target_canonical_name`` lacks a contract prefix.
    """

    repo_path: str  # absolute path to the analyzed repo root
    contracts: tuple[Contract, ...]
    free_functions: tuple[Function, ...]  # Solidity top-level functions
