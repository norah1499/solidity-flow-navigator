"""Unit tests for ``resolve_virtual_override`` (spec §11.5, v0.3 stage 1).

These tests construct synthetic ``Contract`` / ``Function`` records directly
so they isolate the helper's C3-walk logic from Layer 1's Slither plumbing
and Layer 2's call-edge dispatch. Stage 2 covers the integration via
``build_flows``; stage 3 extends to high-level self-calls and modifiers.
"""

import dataclasses

from solidity_flow_navigator.analysis.types import (
    Contract,
    Function,
    SourceLocation,
)
from solidity_flow_navigator.flow.virtual_dispatch import resolve_virtual_override

# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------


def _sl(filename_relative: str = "src/x.sol") -> SourceLocation:
    return SourceLocation(
        filename_absolute="/abs/" + filename_relative,
        filename_relative=filename_relative,
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )


def _func(
    declarer: str,
    full_name: str,
    *,
    is_virtual: bool,
    is_implemented: bool = True,
    is_modifier: bool = False,
) -> Function:
    return Function(
        canonical_name=f"{declarer}.{full_name}",
        name=full_name.split("(")[0],
        full_name=full_name,
        contract_declarer_name=declarer,
        visibility="internal",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=is_modifier,
        is_implemented=is_implemented,
        is_virtual=is_virtual,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_sl(),
        source_code="",
        calls=(),
    )


def _contract(
    name: str,
    linearized_bases: tuple[str, ...],
    *,
    functions: tuple[Function, ...] = (),
    modifiers: tuple[Function, ...] = (),
) -> Contract:
    return Contract(
        name=name,
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=linearized_bases,
        immediate_base_contract_names=linearized_bases[:1] if linearized_bases else (),
        source_location=_sl(),
        functions=functions,
        modifiers=modifiers,
    )


# ---------------------------------------------------------------------------
# 1. Non-virtual target → returns target unchanged
# ---------------------------------------------------------------------------


def test_non_virtual_returns_target_unchanged() -> None:
    target = _func("Base", "foo()", is_virtual=False)
    base = _contract("Base", (), functions=(target,))
    derived_override = _func("Derived", "foo()", is_virtual=False)
    derived = _contract(
        "Derived", ("Base",), functions=(derived_override,)
    )  # would-be override
    by_name = {"Base": base, "Derived": derived}

    result = resolve_virtual_override(derived, target, by_name)

    assert result is target, (
        f"non-virtual target should pass through unchanged, got " f"{result!r}"
    )


# ---------------------------------------------------------------------------
# 2. Virtual target, no override in invoker chain → returns the target itself
# ---------------------------------------------------------------------------


def test_virtual_no_override_returns_target_itself() -> None:
    target = _func("Base", "foo()", is_virtual=True)
    base = _contract("Base", (), functions=(target,))
    # Derived inherits but does not override
    derived = _contract("Derived", ("Base",), functions=())
    by_name = {"Base": base, "Derived": derived}

    result = resolve_virtual_override(derived, target, by_name)

    assert result is target, (
        f"virtual target with no override in Derived's chain should resolve "
        f"to Base.foo (the lexical target itself), got {result!r}"
    )


# ---------------------------------------------------------------------------
# 3. Virtual target, single-level override → returns the override
# ---------------------------------------------------------------------------


def test_virtual_single_level_override_returned() -> None:
    base_foo = _func("Base", "foo()", is_virtual=True)
    derived_foo = _func("Derived", "foo()", is_virtual=False)
    base = _contract("Base", (), functions=(base_foo,))
    derived = _contract("Derived", ("Base",), functions=(derived_foo,))
    by_name = {"Base": base, "Derived": derived}

    result = resolve_virtual_override(derived, base_foo, by_name)

    assert result is derived_foo, (
        f"virtual call on Derived's chain should resolve to Derived.foo (the "
        f"override), got {result!r}"
    )


# ---------------------------------------------------------------------------
# 4. Virtual target, multi-level override chain → returns most-derived
# ---------------------------------------------------------------------------


def test_virtual_multi_level_override_returns_most_derived() -> None:
    base_foo = _func("Base", "foo()", is_virtual=True)
    mid_foo = _func("Mid", "foo()", is_virtual=True)  # virtual override
    top_foo = _func("Top", "foo()", is_virtual=False)  # final override
    base = _contract("Base", (), functions=(base_foo,))
    mid = _contract("Mid", ("Base",), functions=(mid_foo,))
    # C3 order: most-derived first. Top's bases are (Mid, Base).
    top = _contract("Top", ("Mid", "Base"), functions=(top_foo,))
    by_name = {"Base": base, "Mid": mid, "Top": top}

    result = resolve_virtual_override(top, base_foo, by_name)

    assert result is top_foo, (
        f"virtual call on Top's chain should resolve to Top.foo (most-derived "
        f"of Top → Mid → Base), got {result!r}"
    )


def test_virtual_multi_level_resolves_at_intermediate_when_top_absent() -> None:
    """If Top doesn't override but Mid does, Mid's version wins."""

    base_foo = _func("Base", "foo()", is_virtual=True)
    mid_foo = _func("Mid", "foo()", is_virtual=False)
    base = _contract("Base", (), functions=(base_foo,))
    mid = _contract("Mid", ("Base",), functions=(mid_foo,))
    top = _contract("Top", ("Mid", "Base"), functions=())  # no override on Top
    by_name = {"Base": base, "Mid": mid, "Top": top}

    result = resolve_virtual_override(top, base_foo, by_name)

    assert result is mid_foo


# ---------------------------------------------------------------------------
# 5. Virtual target, only abstract declarations in chain → returns None
# ---------------------------------------------------------------------------


def test_virtual_abstract_no_implementation_returns_none() -> None:
    """If the virtual target's lexical declarer is itself abstract and no
    derived contract in the invoker's chain implements the signature,
    return None — the caller emits ABSTRACT_NO_IMPLEMENTATION.
    """

    abstract_foo = _func("Iface", "foo()", is_virtual=True, is_implemented=False)
    abstract_mid_foo = _func("Mid", "foo()", is_virtual=True, is_implemented=False)
    iface = _contract("Iface", (), functions=(abstract_foo,))
    mid = _contract("Mid", ("Iface",), functions=(abstract_mid_foo,))
    # Top inherits but doesn't implement
    top = _contract("Top", ("Mid", "Iface"), functions=())
    by_name = {"Iface": iface, "Mid": mid, "Top": top}

    result = resolve_virtual_override(top, abstract_foo, by_name)

    assert result is None, (
        f"abstract-all-the-way-down virtual call should return None for "
        f"the caller to emit ABSTRACT_NO_IMPLEMENTATION, got {result!r}"
    )


def test_virtual_abstract_skips_to_first_implementation() -> None:
    """Mixed chain — abstract Mid, concrete Top — resolves to Top."""

    abstract_foo = _func("Base", "foo()", is_virtual=True, is_implemented=False)
    abstract_mid_foo = _func("Mid", "foo()", is_virtual=True, is_implemented=False)
    top_foo = _func("Top", "foo()", is_virtual=False, is_implemented=True)
    base = _contract("Base", (), functions=(abstract_foo,))
    mid = _contract("Mid", ("Base",), functions=(abstract_mid_foo,))
    top = _contract("Top", ("Mid", "Base"), functions=(top_foo,))
    by_name = {"Base": base, "Mid": mid, "Top": top}

    result = resolve_virtual_override(top, abstract_foo, by_name)

    assert result is top_foo


# ---------------------------------------------------------------------------
# 6. Helper does not mutate its inputs
# ---------------------------------------------------------------------------


def test_resolve_does_not_mutate_inputs() -> None:
    """The helper is pure; both Contract and Function are frozen anyway,
    but verify the shape via a dataclasses comparison snapshot."""

    base_foo = _func("Base", "foo()", is_virtual=True)
    derived_foo = _func("Derived", "foo()", is_virtual=False)
    base = _contract("Base", (), functions=(base_foo,))
    derived = _contract("Derived", ("Base",), functions=(derived_foo,))
    by_name = {"Base": base, "Derived": derived}

    invoker_snapshot = dataclasses.replace(derived)
    target_snapshot = dataclasses.replace(base_foo)

    resolve_virtual_override(derived, base_foo, by_name)

    assert derived == invoker_snapshot
    assert base_foo == target_snapshot


# ---------------------------------------------------------------------------
# 7. Modifier lookup (used by stage 3 — virtual modifier resolution)
# ---------------------------------------------------------------------------


def test_virtual_modifier_resolves_through_modifiers_tuple() -> None:
    """When the lexical target is a modifier, the walk inspects each
    contract's ``modifiers`` tuple rather than ``functions``."""

    base_mod = _func("Base", "guard()", is_virtual=True, is_modifier=True)
    derived_mod = _func("Derived", "guard()", is_virtual=False, is_modifier=True)
    base = _contract("Base", (), modifiers=(base_mod,))
    derived = _contract("Derived", ("Base",), modifiers=(derived_mod,))
    by_name = {"Base": base, "Derived": derived}

    result = resolve_virtual_override(derived, base_mod, by_name)

    assert result is derived_mod


# ---------------------------------------------------------------------------
# 8. Linearized base not in contracts_by_name is skipped silently
# ---------------------------------------------------------------------------


def test_missing_base_in_lookup_is_skipped() -> None:
    """A linearized base name that isn't present in ``contracts_by_name``
    (e.g. excluded by scope or absent from facts) is skipped without
    raising — matches entry-point enumeration's tolerance."""

    base_foo = _func("Base", "foo()", is_virtual=True)
    base = _contract("Base", (), functions=(base_foo,))
    derived = _contract("Derived", ("Missing", "Base"), functions=())
    by_name = {"Base": base, "Derived": derived}  # "Missing" absent

    result = resolve_virtual_override(derived, base_foo, by_name)

    assert result is base_foo
