"""Tests for the ``Scope`` dataclass, ``DEFAULT_SCOPE`` constant, and matcher
helpers (spec §11.2).

Pins the dataclass invariants (frozen + slotted), the v0.1 default values,
and the behavior of ``path_excluded`` / ``contract_excluded`` /
``library_inlined`` on representative inputs from Solmate-shaped codebases.
"""

from solidity_flow_navigator.flow.scope import (
    DEFAULT_SCOPE,
    Scope,
    contract_excluded,
    library_inlined,
    path_excluded,
)

# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


def test_default_scope_is_a_scope_instance() -> None:
    assert isinstance(DEFAULT_SCOPE, Scope)


def test_scope_is_frozen_and_slotted() -> None:
    """Scope must use ``frozen=True, slots=True`` so Layer 2's traversal
    cannot mutate it and so it can't grow stray attributes from typos."""

    params = Scope.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True, "Scope.__dataclass_params__.frozen is not True"
    assert hasattr(Scope, "__slots__"), "Scope is not slotted"


def test_default_scope_matches_spec_11_2() -> None:
    """The hard-coded defaults are part of the user-visible contract."""

    assert DEFAULT_SCOPE.exclude_paths == (
        "**/*.t.sol",
        "**/test/**",
        "**/tests/**",
    )
    assert DEFAULT_SCOPE.exclude_contracts == ("Mock*",)
    assert DEFAULT_SCOPE.inline_libraries == ()


def test_empty_scope_constructs() -> None:
    """An empty Scope (no defaults) must construct — needed by
    ``--no-default-excludes`` and by tests that want to exercise raw matching."""

    s = Scope()
    assert s.exclude_paths == ()
    assert s.exclude_contracts == ()
    assert s.inline_libraries == ()


def test_scope_is_hashable() -> None:
    """Tuples-only fields make Scope hashable; the lru_cache on compiled
    PathSpecs relies on this property indirectly (via the patterns tuple)."""

    assert hash(DEFAULT_SCOPE) == hash(DEFAULT_SCOPE)
    assert hash(Scope()) == hash(Scope())


# ---------------------------------------------------------------------------
# path_excluded
# ---------------------------------------------------------------------------


def test_path_excluded_default_matches_dot_t_dot_sol() -> None:
    """The ``**/*.t.sol`` default covers Foundry-style test filenames."""

    assert path_excluded(DEFAULT_SCOPE, "src/test/Vault.t.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "test/Token.t.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "Foo.t.sol") is True


def test_path_excluded_default_matches_test_directory() -> None:
    """The ``**/test/**`` default covers Solmate-style ``src/test/`` layouts
    even for files that don't carry the ``.t.sol`` suffix (e.g. helper
    contracts under the test directory)."""

    assert path_excluded(DEFAULT_SCOPE, "src/test/utils/Helper.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "test/Setup.sol") is True


def test_path_excluded_default_matches_tests_directory() -> None:
    """``**/tests/**`` (plural) is a separate default for codebases that use
    the plural directory name."""

    assert path_excluded(DEFAULT_SCOPE, "contracts/tests/Foo.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "tests/integration/Bar.sol") is True


def test_path_excluded_default_does_not_match_production() -> None:
    """Production paths must pass through cleanly under the defaults."""

    assert path_excluded(DEFAULT_SCOPE, "src/tokens/ERC20.sol") is False
    assert path_excluded(DEFAULT_SCOPE, "src/auth/Owned.sol") is False
    assert path_excluded(DEFAULT_SCOPE, "src/Vault.sol") is False


def test_path_excluded_empty_scope_excludes_nothing() -> None:
    """An empty exclude_paths tuple matches no filename — used by
    ``--no-default-excludes`` to bring excluded files back."""

    empty = Scope()
    assert path_excluded(empty, "src/test/Vault.t.sol") is False
    assert path_excluded(empty, "src/tokens/ERC20.sol") is False


def test_path_excluded_custom_pattern() -> None:
    """User-supplied patterns work the same as defaults."""

    s = Scope(exclude_paths=("src/legacy/**",))
    assert path_excluded(s, "src/legacy/Old.sol") is True
    assert path_excluded(s, "src/legacy/sub/Old.sol") is True
    assert path_excluded(s, "src/current/New.sol") is False


# ---------------------------------------------------------------------------
# contract_excluded
# ---------------------------------------------------------------------------


def test_contract_excluded_default_matches_mock_prefix() -> None:
    """The ``Mock*`` default catches Solmate's ``MockERC20`` etc."""

    assert contract_excluded(DEFAULT_SCOPE, "MockERC20") is True
    assert contract_excluded(DEFAULT_SCOPE, "MockERC721") is True
    assert contract_excluded(DEFAULT_SCOPE, "Mock") is True


def test_contract_excluded_default_does_not_match_production() -> None:
    """Names that don't carry the ``Mock`` prefix pass through."""

    assert contract_excluded(DEFAULT_SCOPE, "ERC20") is False
    assert contract_excluded(DEFAULT_SCOPE, "Vault") is False
    assert contract_excluded(DEFAULT_SCOPE, "Owned") is False


def test_contract_excluded_empty_scope_excludes_nothing() -> None:
    """An empty exclude_contracts tuple matches no contract name."""

    empty = Scope()
    assert contract_excluded(empty, "MockERC20") is False
    assert contract_excluded(empty, "Mock") is False


def test_contract_excluded_glob_pattern() -> None:
    """A bare ``*`` matches anything; specific patterns match only intended names."""

    catchall = Scope(exclude_contracts=("*",))
    assert contract_excluded(catchall, "Anything") is True
    assert contract_excluded(catchall, "Vault") is True

    test_suffix = Scope(exclude_contracts=("*Test",))
    assert contract_excluded(test_suffix, "VaultTest") is True
    assert contract_excluded(test_suffix, "Vault") is False


# ---------------------------------------------------------------------------
# library_inlined
# ---------------------------------------------------------------------------


def test_library_inlined_matches_named_library() -> None:
    """A library name in ``inline_libraries`` causes paths under
    ``lib/{name}/**`` to qualify for recursion."""

    s = Scope(inline_libraries=("forge-std",))
    assert library_inlined(s, "lib/forge-std/src/Vm.sol") is True
    assert library_inlined(s, "lib/forge-std/src/console.sol") is True


def test_library_inlined_does_not_match_other_libraries() -> None:
    """A library not in the list stays externalized."""

    s = Scope(inline_libraries=("forge-std",))
    assert library_inlined(s, "lib/openzeppelin/contracts/token/ERC20.sol") is False
    assert library_inlined(s, "lib/ds-test/src/test.sol") is False


def test_library_inlined_does_not_match_outside_lib() -> None:
    """Only paths under ``lib/`` qualify — production paths never trigger
    inlining even if the second segment happens to share a name."""

    s = Scope(inline_libraries=("forge-std",))
    assert library_inlined(s, "src/forge-std/something.sol") is False
    assert library_inlined(s, "node_modules/forge-std/index.sol") is False
    assert library_inlined(s, "Vault.sol") is False


def test_library_inlined_default_inlines_nothing() -> None:
    """Default scope has empty ``inline_libraries`` — everything under
    ``lib/`` stays externalized."""

    assert library_inlined(DEFAULT_SCOPE, "lib/forge-std/src/Vm.sol") is False
    assert library_inlined(DEFAULT_SCOPE, "lib/anything/anywhere.sol") is False


def test_library_inlined_handles_malformed_lib_path() -> None:
    """A ``lib/foo`` path with no trailing segment doesn't crash — treat as
    not inlinable. Real library directories always have children."""

    s = Scope(inline_libraries=("foo",))
    assert library_inlined(s, "lib/foo") is False
    assert library_inlined(s, "lib/") is False


def test_library_inlined_multiple_names() -> None:
    """All names in ``inline_libraries`` qualify independently."""

    s = Scope(inline_libraries=("forge-std", "openzeppelin-contracts"))
    assert library_inlined(s, "lib/forge-std/src/Vm.sol") is True
    assert library_inlined(s, "lib/openzeppelin-contracts/token/ERC20.sol") is True
    assert library_inlined(s, "lib/solmate/src/auth/Owned.sol") is False
