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
) -> Function:
    """Return the modifier ``name`` visible from ``declaring_contract``.

    Walks the declaring contract first, then linearized bases in C3 order
    (most-derived first). Returns the first match.

    Raises ``KeyError`` with diagnostic context if no modifier with this
    name is declared anywhere in the inheritance chain — that indicates a
    Layer 1 anomaly (Slither's ``modifier_names`` referenced something that
    isn't actually declared).
    """

    chain: tuple[str, ...] = (
        declaring_contract.name,
    ) + declaring_contract.linearized_base_contract_names

    for contract_name in chain:
        contract = contracts_by_name.get(contract_name)
        if contract is None:
            raise KeyError(
                f"contract {contract_name!r} (base of "
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
