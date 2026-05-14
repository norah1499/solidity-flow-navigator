"""Within-body virtual dispatch resolution (spec §11.5).

When a Flow rooted at an entry point on contract ``C`` contains a call to a
``virtual`` function ``F``, Solidity's runtime dispatch goes to the most-
derived implementation in ``C``'s C3 chain — not to Slither's lexically-
resolved target. This module's helper performs that re-resolution.

The helper is pure: it takes the invoker contract and a lexical target, and
returns the C3-resolved override (or ``None`` for the abstract-no-impl edge
case). It does not import Slither and does not mutate either argument.

Library calls do not flow through this helper: libraries do not participate
in inheritance, so there is nothing to re-resolve.
"""

from solidity_flow_navigator.analysis.types import Contract, Function


def resolve_virtual_override(
    invoker_contract: Contract,
    lexical_target: Function,
    contracts_by_name: dict[str, Contract],
) -> Function | None:
    """Return the most-derived implementation of ``lexical_target``'s
    signature reachable from ``invoker_contract``'s C3 chain.

    If ``lexical_target`` is not ``virtual``, returns it unchanged: there
    can be no override (a Solidity ``override`` declaration requires a
    ``virtual`` parent).

    Otherwise walks ``invoker_contract``'s linearization in C3 order
    (most-derived first), looking for a function or modifier whose
    ``full_name`` matches ``lexical_target.full_name`` AND is implemented.
    Returns the first match. The walk includes the invoker itself: an
    invoker that declares its own implementation of the signature is the
    most-derived candidate.

    Returns ``None`` only when ``lexical_target`` is virtual AND the
    invoker's entire chain contains only abstract declarations of the
    signature (no implementation anywhere). The caller emits an
    ``UnresolvedNode`` with ``ABSTRACT_NO_IMPLEMENTATION`` for this case.

    ``contracts_by_name`` provides the lookup from contract name to
    ``Contract`` record so we can read each chain link's ``functions``
    and ``modifiers``. A linearized base name not present in the map is
    skipped silently — same as the entry-point enumeration's tolerance
    for facts.contracts being a subset of what Slither sees.
    """

    if not lexical_target.is_virtual:
        return lexical_target

    target_full_name = lexical_target.full_name
    is_modifier = lexical_target.is_modifier

    chain: tuple[str, ...] = (
        invoker_contract.name,
    ) + invoker_contract.linearized_base_contract_names
    for contract_name in chain:
        contract = contracts_by_name.get(contract_name)
        if contract is None:
            continue
        candidates = contract.modifiers if is_modifier else contract.functions
        for candidate in candidates:
            if candidate.full_name != target_full_name:
                continue
            if not candidate.is_implemented:
                continue
            return candidate
    return None
