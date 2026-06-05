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
    target_stubbed,
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
    """The hard-coded defaults are part of the user-visible contract.

    v0.2 broadened ``exclude_paths`` to add ``**/mocks/**`` and broadened
    ``exclude_contracts`` from prefix-only ``Mock*`` to wildcard ``*Mock*``
    so suffix conventions (``ERC20Mock``) match too. ``stub_paths`` is the
    new v0.2 field, defaulting empty.
    """

    assert DEFAULT_SCOPE.exclude_paths == (
        "**/*.t.sol",
        "**/test/**",
        "**/tests/**",
        "**/mocks/**",
    )
    assert DEFAULT_SCOPE.exclude_contracts == ("*Mock*",)
    assert DEFAULT_SCOPE.inline_libraries == ()
    assert DEFAULT_SCOPE.stub_paths == ()


def test_empty_scope_constructs() -> None:
    """An empty Scope (no defaults) must construct — needed by the
    config-file ``[]``-clears-default path (the v0.10.0 Stage 2
    replacement for ``--no-default-excludes``) and by tests that want
    to exercise raw matching."""

    s = Scope()
    assert s.exclude_paths == ()
    assert s.exclude_contracts == ()
    assert s.inline_libraries == ()
    assert s.stub_paths == ()


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


def test_path_excluded_default_matches_mocks_directory() -> None:
    """v0.2 default: ``**/mocks/**`` covers Morpho-style mock layouts where
    helpers live under ``src/mocks/`` rather than under a test directory."""

    assert path_excluded(DEFAULT_SCOPE, "src/mocks/OracleMock.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "src/mocks/sub/Helper.sol") is True
    assert path_excluded(DEFAULT_SCOPE, "mocks/ERC20Mock.sol") is True


def test_path_excluded_default_does_not_match_production() -> None:
    """Production paths must pass through cleanly under the defaults."""

    assert path_excluded(DEFAULT_SCOPE, "src/tokens/ERC20.sol") is False
    assert path_excluded(DEFAULT_SCOPE, "src/auth/Owned.sol") is False
    assert path_excluded(DEFAULT_SCOPE, "src/Vault.sol") is False


def test_path_excluded_empty_scope_excludes_nothing() -> None:
    """An empty exclude_paths tuple matches no filename — used by the
    config-file ``exclude_paths = []`` rule (the v0.10.0 Stage 2
    replacement for ``--no-default-excludes``) to bring excluded files
    back."""

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


def test_contract_excluded_default_matches_solmate_mock_prefix() -> None:
    """The v0.2 ``*Mock*`` default still catches Solmate's prefix style
    (``MockERC20``, ``MockERC721``) — the broadening from ``Mock*`` to
    ``*Mock*`` must not regress prefix coverage."""

    assert contract_excluded(DEFAULT_SCOPE, "MockERC20") is True
    assert contract_excluded(DEFAULT_SCOPE, "MockERC721") is True
    assert contract_excluded(DEFAULT_SCOPE, "Mock") is True


def test_contract_excluded_default_matches_morpho_mock_suffix() -> None:
    """v0.2 broadens to ``*Mock*`` so suffix conventions match too. Morpho
    Blue uses ``ERC20Mock``, ``OracleMock``, ``IrmMock`` — all caught by the
    new glob, none caught by the v0.1 ``Mock*`` prefix-only default."""

    assert contract_excluded(DEFAULT_SCOPE, "ERC20Mock") is True
    assert contract_excluded(DEFAULT_SCOPE, "OracleMock") is True
    assert contract_excluded(DEFAULT_SCOPE, "IrmMock") is True


def test_contract_excluded_default_does_not_match_production() -> None:
    """Names that don't carry ``Mock`` anywhere pass through."""

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


# ---------------------------------------------------------------------------
# target_stubbed (v0.2)
# ---------------------------------------------------------------------------


def test_target_stubbed_default_stubs_nothing() -> None:
    """Default scope has empty ``stub_paths`` — every in-tree call target
    recurses normally. The v0.2 default is opt-in: auditors compress
    explicitly, never by accident."""

    assert target_stubbed(DEFAULT_SCOPE, "src/libraries/MathLib.sol") is False
    assert target_stubbed(DEFAULT_SCOPE, "anything/anywhere.sol") is False


def test_target_stubbed_empty_scope_stubs_nothing() -> None:
    """An empty Scope (no defaults at all) matches no path."""

    empty = Scope()
    assert target_stubbed(empty, "src/libraries/MathLib.sol") is False
    assert target_stubbed(empty, "src/Vault.sol") is False


def test_target_stubbed_single_pattern() -> None:
    """A single glob in ``stub_paths`` matches its targets and nothing else."""

    s = Scope(stub_paths=("src/libraries/**",))
    assert target_stubbed(s, "src/libraries/MathLib.sol") is True
    assert target_stubbed(s, "src/libraries/sub/Helper.sol") is True
    assert target_stubbed(s, "src/Vault.sol") is False


def test_target_stubbed_multi_pattern() -> None:
    """Each pattern in the tuple is OR'd: a target matching any one pattern
    is stubbed."""

    s = Scope(stub_paths=("src/libraries/**", "src/utils/**"))
    assert target_stubbed(s, "src/libraries/MathLib.sol") is True
    assert target_stubbed(s, "src/utils/Strings.sol") is True
    assert target_stubbed(s, "src/Vault.sol") is False


def test_target_stubbed_filename_glob() -> None:
    """``stub_paths`` accepts gitignore-style filename globs, not just
    directory globs — useful for one-off compression of a single file."""

    s = Scope(stub_paths=("**/MathLib.sol",))
    assert target_stubbed(s, "src/libraries/MathLib.sol") is True
    assert target_stubbed(s, "src/MathLib.sol") is True
    assert target_stubbed(s, "src/libraries/Other.sol") is False


def test_target_stubbed_can_match_lib_paths() -> None:
    """``stub_paths`` is matched against any filename — including paths under
    ``lib/``. The Layer 2 conflict rule (§11.8) gives ``stub_paths`` priority
    over the default lib stub and over ``inline_libraries``; the matcher
    itself is shape-agnostic."""

    s = Scope(stub_paths=("lib/forge-std/**",))
    assert target_stubbed(s, "lib/forge-std/src/Vm.sol") is True
    assert target_stubbed(s, "lib/openzeppelin/contracts/ERC20.sol") is False
