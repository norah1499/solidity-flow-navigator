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
    contracts_by_uid: dict[tuple[str, str], Contract] | None = None,
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

    Contract identity is resolved by stable uid when ``contracts_by_uid`` is
    supplied and the invoker carries base uids (spec §11.5) — exact even when
    two contracts share a name across files. Without it (synthetic fixtures)
    the walk falls back to the name chain via ``contracts_by_name``, which is
    correct for fixtures that do not collide names. A chain link not present in
    the chosen map is skipped silently — same as the entry-point enumeration's
    tolerance for facts.contracts being a subset of what Slither sees.
    """

    if not lexical_target.is_virtual:
        return lexical_target

    target_full_name = lexical_target.full_name
    is_modifier = lexical_target.is_modifier

    if contracts_by_uid is not None and invoker_contract.linearized_base_contract_uids:
        chain = (invoker_contract.uid,) + invoker_contract.linearized_base_contract_uids
        lookup = contracts_by_uid
    else:
        chain = (
            invoker_contract.name,
        ) + invoker_contract.linearized_base_contract_names
        lookup = contracts_by_name
    for key in chain:
        contract = lookup.get(key)
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
