"""Tests for the CLI layer.

The bulk of this file exercises ``_resolve_scope`` — Stage 3's bug-prone
piece per the v0.1 sprint prompt. Tests construct ``argparse.Namespace``
objects directly rather than invoking the CLI: this lets us pin every
precedence path in §11.2's resolution order without spinning up Slither,
Flask, or argparse itself. The real ``main()`` calls
``_resolve_scope(args, Path.cwd())`` so the function is one composition
away from production.

CWD is passed as an explicit parameter (not read from ``os.getcwd()``) so
each test points it at its own ``tmp_path``, avoiding any monkey-patching
of process state.
"""

import argparse
from pathlib import Path

import pytest

from solidity_flow_navigator.cli import _resolve_scope
from solidity_flow_navigator.flow.config import ConfigError
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope


def _args(
    *,
    config: str | None = None,
    exclude_path: list[str] | None = None,
    exclude_contract: list[str] | None = None,
    inline_library: list[str] | None = None,
    stub_path: list[str] | None = None,
    no_default_excludes: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring what ``main()``'s parser produces.

    All flag fields are filled so ``_resolve_scope`` finds the attributes
    it expects regardless of which subset of flags the test exercises.
    """

    return argparse.Namespace(
        config=config,
        exclude_path=exclude_path,
        exclude_contract=exclude_contract,
        inline_library=inline_library,
        stub_path=stub_path,
        no_default_excludes=no_default_excludes,
    )


def _write_toml(tmp_path: Path, body: str, name: str = "solflow.toml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Defaults-only
# ---------------------------------------------------------------------------


def test_resolve_defaults_only(tmp_path: Path) -> None:
    """No flags, no config file in CWD → DEFAULT_SCOPE survives intact."""

    result = _resolve_scope(_args(), tmp_path)
    assert result == DEFAULT_SCOPE


# ---------------------------------------------------------------------------
# --no-default-excludes
# ---------------------------------------------------------------------------


def test_resolve_no_default_excludes_clears_two_keys(tmp_path: Path) -> None:
    """--no-default-excludes clears exclude_paths + exclude_contracts; the
    inline_libraries and stub_paths defaults (both empty tuples already)
    are untouched."""

    result = _resolve_scope(_args(no_default_excludes=True), tmp_path)
    assert result.exclude_paths == ()
    assert result.exclude_contracts == ()
    # Verify the flag did NOT touch inline_libraries / stub_paths (no
    # defaults to clear, so behavior is identical, but the equality check
    # pins the intent).
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries
    assert result.stub_paths == DEFAULT_SCOPE.stub_paths


# ---------------------------------------------------------------------------
# Config file precedence
# ---------------------------------------------------------------------------


def test_resolve_file_only_replaces_defaults(tmp_path: Path) -> None:
    """A solflow.toml with all three keys populated replaces every default."""

    _write_toml(
        tmp_path,
        "[scope]\n"
        'exclude_paths = ["src/legacy/**"]\n'
        'exclude_contracts = ["*Test"]\n'
        'inline_libraries = ["forge-std"]\n',
    )
    result = _resolve_scope(_args(), tmp_path)
    assert result.exclude_paths == ("src/legacy/**",)
    assert result.exclude_contracts == ("*Test",)
    assert result.inline_libraries == ("forge-std",)


def test_resolve_file_explicit_empty_clears_one_key(tmp_path: Path) -> None:
    """An empty list in the file clears that key explicitly per §11.2; other
    keys keep their default values (since they were absent from the file)."""

    _write_toml(tmp_path, "[scope]\nexclude_paths = []\n")
    result = _resolve_scope(_args(), tmp_path)
    assert result.exclude_paths == ()
    assert result.exclude_contracts == DEFAULT_SCOPE.exclude_contracts
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries


def test_resolve_file_absent_key_keeps_default(tmp_path: Path) -> None:
    """A key absent from the file leaves the default in place; keys present
    in the file override only themselves."""

    _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["custom/**"]\n',
    )
    result = _resolve_scope(_args(), tmp_path)
    assert result.exclude_paths == ("custom/**",)
    assert result.exclude_contracts == DEFAULT_SCOPE.exclude_contracts
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries


# ---------------------------------------------------------------------------
# CLI append precedence
# ---------------------------------------------------------------------------


def test_resolve_cli_only_appends_onto_defaults(tmp_path: Path) -> None:
    """CLI values append onto defaults — they don't replace."""

    result = _resolve_scope(
        _args(
            exclude_path=["src/legacy/**", "another/**"],
            exclude_contract=["*Test"],
            inline_library=["forge-std"],
        ),
        tmp_path,
    )
    assert result.exclude_paths == DEFAULT_SCOPE.exclude_paths + (
        "src/legacy/**",
        "another/**",
    )
    assert result.exclude_contracts == DEFAULT_SCOPE.exclude_contracts + ("*Test",)
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries + ("forge-std",)


# ---------------------------------------------------------------------------
# --no-default-excludes + CLI / + file interactions
# ---------------------------------------------------------------------------


def test_resolve_no_defaults_plus_cli_yields_only_cli_values(
    tmp_path: Path,
) -> None:
    """--no-default-excludes clears first, then CLI appends → result is
    exactly the CLI values (defaults are gone)."""

    result = _resolve_scope(
        _args(
            no_default_excludes=True,
            exclude_path=["only/this/**"],
            exclude_contract=["OnlyThis*"],
        ),
        tmp_path,
    )
    assert result.exclude_paths == ("only/this/**",)
    assert result.exclude_contracts == ("OnlyThis*",)


def test_resolve_no_defaults_plus_file(tmp_path: Path) -> None:
    """With --no-default-excludes, file values still apply on top of the
    cleared base. Absent file keys yield ``()`` (the cleared base survives)."""

    _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["from/file/**"]\n',
    )
    result = _resolve_scope(_args(no_default_excludes=True), tmp_path)
    assert result.exclude_paths == ("from/file/**",)
    # exclude_contracts was cleared and the file didn't define it.
    assert result.exclude_contracts == ()


# ---------------------------------------------------------------------------
# File + CLI append
# ---------------------------------------------------------------------------


def test_resolve_file_then_cli_appends_onto_file_value(tmp_path: Path) -> None:
    """File replaces the default; CLI appends onto the file value. Final
    exclude_paths is file_value + cli_values, not default + cli_values."""

    _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["from/file/**"]\n',
    )
    result = _resolve_scope(
        _args(exclude_path=["from/cli/**"]),
        tmp_path,
    )
    assert result.exclude_paths == ("from/file/**", "from/cli/**")


# ---------------------------------------------------------------------------
# All three layers
# ---------------------------------------------------------------------------


def test_resolve_no_defaults_plus_file_plus_cli(tmp_path: Path) -> None:
    """--no-default-excludes clears, file replaces (or doesn't), CLI appends.
    Verify the full chain on a single key."""

    _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["from/file/**"]\n',
    )
    result = _resolve_scope(
        _args(
            no_default_excludes=True,
            exclude_path=["from/cli/**"],
        ),
        tmp_path,
    )
    # Clear → file replaces empty base → CLI appends.
    assert result.exclude_paths == ("from/file/**", "from/cli/**")


# ---------------------------------------------------------------------------
# --no-default-excludes does NOT clear inline_libraries
# ---------------------------------------------------------------------------


def test_resolve_no_defaults_does_not_clear_inline_libraries(
    tmp_path: Path,
) -> None:
    """Per spec §11.2: --no-default-excludes affects only exclude_paths and
    exclude_contracts. inline_libraries has no default to clear — and the
    CLI must not retroactively zero it out via the same flag."""

    result = _resolve_scope(
        _args(no_default_excludes=True, inline_library=["forge-std"]),
        tmp_path,
    )
    # DEFAULT_SCOPE.inline_libraries is () already, so the only signal that
    # the flag didn't misbehave is that the CLI append still appears.
    assert result.inline_libraries == ("forge-std",)


# ---------------------------------------------------------------------------
# --config <path>
# ---------------------------------------------------------------------------


def test_resolve_explicit_config_uses_named_file(tmp_path: Path) -> None:
    """--config <path> loads that file instead of cwd/solflow.toml. A
    different solflow.toml in CWD must NOT bleed into the result."""

    # A "wrong" config at the default lookup location that should be ignored:
    _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["wrong/**"]\n',
    )
    # The right config under a non-default filename in the same directory.
    explicit = _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["right/**"]\n',
        name="my-config.toml",
    )
    result = _resolve_scope(_args(config=str(explicit)), tmp_path)
    assert result.exclude_paths == ("right/**",)


def test_resolve_explicit_config_missing_raises_file_not_found(
    tmp_path: Path,
) -> None:
    """An explicit --config <missing> propagates FileNotFoundError so
    main() can print a clear stderr message and return 1."""

    missing = tmp_path / "nope.toml"
    with pytest.raises(FileNotFoundError):
        _resolve_scope(_args(config=str(missing)), tmp_path)


# ---------------------------------------------------------------------------
# Default-lookup error semantics
# ---------------------------------------------------------------------------


def test_resolve_default_lookup_missing_file_is_silent(tmp_path: Path) -> None:
    """No solflow.toml in CWD → silent skip; result == DEFAULT_SCOPE (and
    in particular no FileNotFoundError leaks out)."""

    result = _resolve_scope(_args(), tmp_path)
    assert result == DEFAULT_SCOPE


def test_resolve_default_lookup_malformed_file_raises(tmp_path: Path) -> None:
    """A broken solflow.toml in CWD is always a hard error — silent-skip
    applies to *missing* file, not *broken* file. Otherwise the user's
    config silently no-ops and they have no idea."""

    _write_toml(tmp_path, "[scope\nbroken = [")
    with pytest.raises(ConfigError):
        _resolve_scope(_args(), tmp_path)


# ---------------------------------------------------------------------------
# stub_paths (v0.2)
# ---------------------------------------------------------------------------


def test_resolve_stub_path_cli_appends_onto_empty_default(tmp_path: Path) -> None:
    """Default stub_paths is empty; --stub-path appends to that empty base."""

    result = _resolve_scope(
        _args(stub_path=["src/libraries/**", "src/utils/**"]),
        tmp_path,
    )
    assert result.stub_paths == ("src/libraries/**", "src/utils/**")


def test_resolve_stub_path_file_replaces_default(tmp_path: Path) -> None:
    """A populated ``stub_paths`` in solflow.toml replaces the empty default
    (no observable difference, but it pins the resolution path)."""

    _write_toml(
        tmp_path,
        '[scope]\nstub_paths = ["src/libraries/**"]\n',
    )
    result = _resolve_scope(_args(), tmp_path)
    assert result.stub_paths == ("src/libraries/**",)


def test_resolve_stub_path_file_then_cli_appends(tmp_path: Path) -> None:
    """File replaces default; CLI appends onto file value."""

    _write_toml(
        tmp_path,
        '[scope]\nstub_paths = ["from/file/**"]\n',
    )
    result = _resolve_scope(
        _args(stub_path=["from/cli/**"]),
        tmp_path,
    )
    assert result.stub_paths == ("from/file/**", "from/cli/**")


def test_resolve_no_default_excludes_does_not_clear_stub_paths(
    tmp_path: Path,
) -> None:
    """Per spec §11.2: ``--no-default-excludes`` only touches exclude_paths
    and exclude_contracts. ``stub_paths`` (like ``inline_libraries``) has no
    default to clear — and a CLI append must still survive the flag."""

    result = _resolve_scope(
        _args(no_default_excludes=True, stub_path=["src/libraries/**"]),
        tmp_path,
    )
    assert result.stub_paths == ("src/libraries/**",)


def test_resolve_stub_path_file_empty_clears(tmp_path: Path) -> None:
    """An explicit empty list clears (becomes ``()``); CLI appends onto that."""

    _write_toml(tmp_path, "[scope]\nstub_paths = []\n")
    result = _resolve_scope(
        _args(stub_path=["src/libraries/**"]),
        tmp_path,
    )
    assert result.stub_paths == ("src/libraries/**",)


# ---------------------------------------------------------------------------
# Return-type sanity
# ---------------------------------------------------------------------------


def test_resolve_returns_scope_instance(tmp_path: Path) -> None:
    """The final value passed to build_flows must be a Scope (not a
    PartialScope or a dict)."""

    result = _resolve_scope(_args(), tmp_path)
    assert isinstance(result, Scope)
