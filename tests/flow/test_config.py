"""Tests for ``flow.config``: TOML loader, PartialScope tri-state, and the
``apply_partial`` merge function (spec §11.2).

Pins:
- Tri-state semantics: ``None`` (absent) vs. ``()`` (explicit clear) vs.
  populated.
- Unknown-key handling: silent for top-level sections, stderr warning for
  unknown keys under ``[scope]``.
- Error mapping: malformed TOML and bad value types raise ``ConfigError``;
  missing file propagates ``FileNotFoundError`` so the CLI can distinguish
  default-lookup-skip from --config-flag-fail.
- ``apply_partial`` correctly applies step 3 of the resolution order.

All TOML inputs are written via ``tmp_path`` to keep the tests hermetic.
"""

from pathlib import Path

import pytest

from solidity_flow_navigator.flow.config import (
    ConfigError,
    PartialScope,
    apply_partial,
    load_partial_scope_from_toml,
)
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope


def _write_toml(tmp_path: Path, body: str) -> Path:
    """Write ``body`` to a ``solflow.toml`` under ``tmp_path`` and return the path."""

    path = tmp_path / "solflow.toml"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# PartialScope construction
# ---------------------------------------------------------------------------


def test_partial_scope_defaults_to_all_none() -> None:
    """A bare PartialScope() is the absent-everywhere sentinel."""

    p = PartialScope()
    assert p.exclude_paths is None
    assert p.exclude_contracts is None
    assert p.inline_libraries is None
    assert p.stub_paths is None


def test_partial_scope_distinguishes_absent_from_empty() -> None:
    """The tri-state must distinguish ``None`` (absent) from ``()`` (cleared)."""

    absent = PartialScope()
    cleared = PartialScope(exclude_paths=())
    assert absent.exclude_paths is None
    assert cleared.exclude_paths == ()
    assert absent != cleared


# ---------------------------------------------------------------------------
# load_partial_scope_from_toml — absent/empty cases
# ---------------------------------------------------------------------------


def test_load_empty_file_returns_all_none(tmp_path: Path) -> None:
    """An empty TOML file has no ``[scope]`` table; all fields stay None."""

    path = _write_toml(tmp_path, "")
    p = load_partial_scope_from_toml(path)
    assert p == PartialScope()


def test_load_file_without_scope_section_returns_all_none(tmp_path: Path) -> None:
    """Unrelated content (e.g. a future ``[other-tool]`` section) is ignored
    and yields PartialScope(None, None, None) — top-level unknown sections
    are silently accepted for forward compat."""

    path = _write_toml(
        tmp_path,
        '[other-tool]\nsomething = "value"\n',
    )
    p = load_partial_scope_from_toml(path)
    assert p == PartialScope()


def test_load_empty_scope_table_returns_all_none(tmp_path: Path) -> None:
    """Present-but-empty ``[scope]`` is indistinguishable from missing:
    nothing to apply."""

    path = _write_toml(tmp_path, "[scope]\n")
    p = load_partial_scope_from_toml(path)
    assert p == PartialScope()


# ---------------------------------------------------------------------------
# load_partial_scope_from_toml — per-key tri-state
# ---------------------------------------------------------------------------


def test_load_each_key_absent_individually(tmp_path: Path) -> None:
    """When only one key is present, the others stay None."""

    path = _write_toml(tmp_path, '[scope]\nexclude_paths = ["src/legacy/**"]\n')
    p = load_partial_scope_from_toml(path)
    assert p.exclude_paths == ("src/legacy/**",)
    assert p.exclude_contracts is None
    assert p.inline_libraries is None
    assert p.stub_paths is None


def test_load_each_key_empty_list_clears(tmp_path: Path) -> None:
    """Empty list explicitly clears (becomes ``()``), distinct from absent."""

    path = _write_toml(
        tmp_path,
        "[scope]\n"
        "exclude_paths = []\n"
        "exclude_contracts = []\n"
        "inline_libraries = []\n"
        "stub_paths = []\n",
    )
    p = load_partial_scope_from_toml(path)
    assert p.exclude_paths == ()
    assert p.exclude_contracts == ()
    assert p.inline_libraries == ()
    assert p.stub_paths == ()


def test_load_each_key_populated(tmp_path: Path) -> None:
    """Populated lists become tuples preserving order."""

    path = _write_toml(
        tmp_path,
        "[scope]\n"
        'exclude_paths = ["**/*.t.sol", "src/legacy/**"]\n'
        'exclude_contracts = ["*Mock*", "*Test"]\n'
        'inline_libraries = ["forge-std", "openzeppelin-contracts"]\n'
        'stub_paths = ["src/libraries/**", "src/utils/Strings.sol"]\n',
    )
    p = load_partial_scope_from_toml(path)
    assert p.exclude_paths == ("**/*.t.sol", "src/legacy/**")
    assert p.exclude_contracts == ("*Mock*", "*Test")
    assert p.inline_libraries == ("forge-std", "openzeppelin-contracts")
    assert p.stub_paths == ("src/libraries/**", "src/utils/Strings.sol")


def test_load_stub_paths_absent_stays_none(tmp_path: Path) -> None:
    """A TOML file that omits ``stub_paths`` leaves the partial sentinel
    None, so the default (empty tuple) survives the apply_partial step."""

    path = _write_toml(
        tmp_path,
        '[scope]\nexclude_paths = ["src/legacy/**"]\n',
    )
    p = load_partial_scope_from_toml(path)
    assert p.stub_paths is None


def test_load_mixed_absent_empty_populated(tmp_path: Path) -> None:
    """The three tri-states coexist independently per field."""

    path = _write_toml(
        tmp_path,
        "[scope]\n" 'exclude_paths = ["src/legacy/**"]\n' "exclude_contracts = []\n"
        # inline_libraries omitted entirely
        "",
    )
    p = load_partial_scope_from_toml(path)
    assert p.exclude_paths == ("src/legacy/**",)
    assert p.exclude_contracts == ()
    assert p.inline_libraries is None


# ---------------------------------------------------------------------------
# load_partial_scope_from_toml — error paths
# ---------------------------------------------------------------------------


def test_load_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """``FileNotFoundError`` propagates — caller distinguishes default-lookup
    (skip silently) from explicit --config (fail loudly)."""

    with pytest.raises(FileNotFoundError):
        load_partial_scope_from_toml(tmp_path / "does-not-exist.toml")


def test_load_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    """Syntactic TOML errors wrap as ``ConfigError`` with the path in the message."""

    path = _write_toml(tmp_path, "[scope\nexclude_paths = [")
    with pytest.raises(ConfigError) as exc_info:
        load_partial_scope_from_toml(path)
    assert str(path) in str(exc_info.value)
    assert "malformed TOML" in str(exc_info.value)


def test_load_wrong_value_type_string_raises_config_error(tmp_path: Path) -> None:
    """A scalar where a list is expected names the offending key."""

    path = _write_toml(tmp_path, '[scope]\nexclude_paths = "src/legacy/**"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_partial_scope_from_toml(path)
    msg = str(exc_info.value)
    assert "exclude_paths" in msg
    assert "list of strings" in msg
    assert str(path) in msg


def test_load_wrong_element_type_raises_config_error(tmp_path: Path) -> None:
    """A non-string element inside a list names the index of the offender."""

    path = _write_toml(
        tmp_path,
        '[scope]\nexclude_contracts = ["Mock*", 42, "Test*"]\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_partial_scope_from_toml(path)
    msg = str(exc_info.value)
    assert "exclude_contracts" in msg
    assert "[1]" in msg
    assert "string" in msg


def test_load_scope_as_non_table_raises_config_error(tmp_path: Path) -> None:
    """``scope = "value"`` (a scalar at top level, not a table) is rejected."""

    path = _write_toml(tmp_path, 'scope = "not-a-table"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_partial_scope_from_toml(path)
    assert "[scope]" in str(exc_info.value)
    assert "table" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Unknown-key warnings
# ---------------------------------------------------------------------------


def test_load_unknown_scope_key_warns_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd key under ``[scope]`` emits a warning but does not raise.
    Known keys still load."""

    path = _write_toml(
        tmp_path,
        "[scope]\n"
        'exclude_paths = ["**/*.t.sol"]\n'
        'excldue_contracts = ["Mock*"]\n',  # typo
    )
    p = load_partial_scope_from_toml(path)
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "excldue_contracts" in captured.err
    assert "[scope]" in captured.err
    # Known key still loaded correctly:
    assert p.exclude_paths == ("**/*.t.sol",)
    assert p.exclude_contracts is None  # not set by the typo'd line


def test_load_no_warning_for_unknown_top_level_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown top-level sections are silently ignored — other tools may
    share the file in future versions."""

    path = _write_toml(
        tmp_path,
        '[other-tool]\nfoo = "bar"\n\n[scope]\nexclude_paths = ["**/*.t.sol"]\n',
    )
    load_partial_scope_from_toml(path)
    captured = capsys.readouterr()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# apply_partial
# ---------------------------------------------------------------------------


def test_apply_partial_all_none_preserves_base() -> None:
    """An empty PartialScope leaves the base Scope untouched (per-field equality)."""

    result = apply_partial(DEFAULT_SCOPE, PartialScope())
    assert result == DEFAULT_SCOPE


def test_apply_partial_explicit_clear_clears_one_key() -> None:
    """``exclude_paths=()`` in the partial clears that key in the result;
    other keys preserve their base values."""

    partial = PartialScope(exclude_paths=())
    result = apply_partial(DEFAULT_SCOPE, partial)
    assert result.exclude_paths == ()
    assert result.exclude_contracts == DEFAULT_SCOPE.exclude_contracts
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries


def test_apply_partial_populated_replaces_one_key() -> None:
    """A populated value in the partial replaces that key; others preserved."""

    partial = PartialScope(exclude_paths=("src/legacy/**",))
    result = apply_partial(DEFAULT_SCOPE, partial)
    assert result.exclude_paths == ("src/legacy/**",)
    assert result.exclude_contracts == DEFAULT_SCOPE.exclude_contracts
    assert result.inline_libraries == DEFAULT_SCOPE.inline_libraries


def test_apply_partial_replaces_all_four_keys() -> None:
    """All four fields can be applied at once."""

    partial = PartialScope(
        exclude_paths=("a",),
        exclude_contracts=("B*",),
        inline_libraries=("forge-std",),
        stub_paths=("src/libraries/**",),
    )
    result = apply_partial(DEFAULT_SCOPE, partial)
    assert result.exclude_paths == ("a",)
    assert result.exclude_contracts == ("B*",)
    assert result.inline_libraries == ("forge-std",)
    assert result.stub_paths == ("src/libraries/**",)


def test_apply_partial_stub_paths_independent() -> None:
    """``stub_paths`` follows the same tri-state rule as the other fields:
    absent (None) preserves base, populated replaces, empty tuple clears."""

    base = Scope(stub_paths=("orig/**",))

    # Absent: base survives.
    assert apply_partial(base, PartialScope()).stub_paths == ("orig/**",)
    # Populated: replaces.
    assert apply_partial(base, PartialScope(stub_paths=("new/**",))).stub_paths == (
        "new/**",
    )
    # Cleared: empty tuple.
    assert apply_partial(base, PartialScope(stub_paths=())).stub_paths == ()


def test_apply_partial_does_not_mutate_base() -> None:
    """``base`` survives the call unmodified — Scope is frozen, but verify
    no aliasing snuck in either."""

    base = Scope(exclude_paths=("orig",), exclude_contracts=("C*",))
    apply_partial(base, PartialScope(exclude_paths=("new",)))
    assert base.exclude_paths == ("orig",)
    assert base.exclude_contracts == ("C*",)


def test_apply_partial_over_empty_base() -> None:
    """Applying onto an empty Scope (e.g. post-``--no-default-excludes``)
    yields exactly the partial's populated values."""

    base = Scope()
    partial = PartialScope(
        exclude_paths=("src/test/**",),
        exclude_contracts=(),
        inline_libraries=("forge-std",),
    )
    result = apply_partial(base, partial)
    assert result.exclude_paths == ("src/test/**",)
    assert result.exclude_contracts == ()
    assert result.inline_libraries == ("forge-std",)
