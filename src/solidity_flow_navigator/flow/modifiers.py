"""Modifier resolution: find a modifier's Function record in a contract's
inheritance chain (spec §11.6).

Modifier folding itself — wrapping resolved modifiers as ``FunctionNode``
children with ``is_modifier=True`` before the function body's call children —
happens in builder.py because it shares the call-processing recursion.
This module only performs the lookup.

Modifiers are resolved from the perspective of the function that applies
them, not the contract the entry point was reached through. Solidity binds
modifier names at the declaration site, not at the call site.
"""

from solidity_flow_navigator.analysis.types import Contract, Function


def resolve_modifier(
    name: str,
    declaring_contract: Contract,
    contracts_by_name: dict[str, Contract],
    contracts_by_uid: dict[tuple[str, str], Contract] | None = None,
) -> Function:
    """Return the modifier ``name`` visible from ``declaring_contract``.

    Walks the declaring contract first, then linearized bases in C3 order
    (most-derived first). Returns the first match.

    Contract identity is resolved by stable uid when ``contracts_by_uid`` is
    supplied and the declaring contract carries base uids (spec §11.5) — this
    is what production passes, and it is exact even when two contracts share a
    name across files. Without it (synthetic fixtures), the walk falls back to
    the name chain, which is correct because such fixtures do not collide
    names. This name-collision blind spot was the original cause of a spurious
    "modifier not found in inheritance chain" ``KeyError`` (the linearization
    listed the right base by name, but the name resolved to a same-named
    contract that did not declare the modifier).

    Raises ``KeyError`` with diagnostic context if no modifier with this name
    is declared anywhere in the inheritance chain.
    """

    if (
        contracts_by_uid is not None
        and declaring_contract.linearized_base_contract_uids
    ):
        chain = (
            declaring_contract.uid,
        ) + declaring_contract.linearized_base_contract_uids
        lookup = contracts_by_uid
    else:
        chain = (
            declaring_contract.name,
        ) + declaring_contract.linearized_base_contract_names
        lookup = contracts_by_name

    for key in chain:
        contract = lookup.get(key)
        if contract is None:
            raise KeyError(
                f"contract {key!r} (base of "
                f"{declaring_contract.name!r}) not present in facts; cannot "
                f"resolve modifier {name!r}"
            )
        for modifier in contract.modifiers:
            if modifier.name == name:
                return modifier

    raise KeyError(
        f"modifier {name!r} applied to a function in "
        f"{declaring_contract.name!r} not found anywhere in inheritance "
        f"chain {chain}"
    )
