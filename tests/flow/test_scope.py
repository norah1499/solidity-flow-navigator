"""Smoke tests for the Scope dataclass and DEFAULT_SCOPE constant.

In v0 the Scope is intentionally empty (the v0.1 design pass settles its
fields); these tests pin the contract that ``DEFAULT_SCOPE`` exists, is a
``Scope`` instance, and is a frozen dataclass so accidental mutation by
Layer 2's traversal is impossible.
"""

import dataclasses

from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope


def test_default_scope_is_a_scope_instance() -> None:
    assert isinstance(DEFAULT_SCOPE, Scope)


def test_scope_is_frozen_and_slotted() -> None:
    """Scope must use ``frozen=True, slots=True`` so Layer 2's traversal
    cannot mutate it and so it can't grow stray attributes from typos."""

    params = Scope.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True, "Scope.__dataclass_params__.frozen is not True"
    assert hasattr(Scope, "__slots__"), "Scope is not slotted"


def test_default_scope_serializes_to_empty_dict() -> None:
    """v0 Scope has no fields; asdict round-trips to an empty mapping."""

    assert dataclasses.asdict(DEFAULT_SCOPE) == {}
