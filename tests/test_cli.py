"""Tests for the CLI layer.

The first block exercises ``_resolve_scope`` — Stage 3's bug-prone piece
per the v0.1 sprint prompt. Tests construct ``argparse.Namespace`` objects
directly rather than invoking the CLI: this lets us pin every precedence
path in §11.2's resolution order without spinning up Slither, Flask, or
argparse itself. The real ``main()`` calls ``_resolve_scope(args,
Path.cwd())`` so the function is one composition away from production.

CWD is passed as an explicit parameter (not read from ``os.getcwd()``) so
each test points it at its own ``tmp_path``, avoiding any monkey-patching
of process state.

The second block exercises ``--expand-all`` parsing (v0.10.0 Stage 1).

The third block exercises ``--port`` selection (v0.10.0 Stage 2): the
default-lookup auto-select walks upward from 8080 when the default is
busy; an explicit ``--port N`` that's already in use is a hard error
with no silent reassignment.

v0.10.0 Stage 2 removed ``--legacy``, ``--json``, ``--host``, and
``--no-default-excludes`` from the CLI. Their dedicated tests are gone;
the ``[]``-clears-default capability that ``--no-default-excludes`` used
to overlap with is still covered via the config-file path
(``test_resolve_file_explicit_empty_clears_one_key`` and friends).
"""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

import pytest

from solidity_flow_navigator.cli import (
    DEFAULT_PORT,
    SERVER_HOST,
    _bind_probe,
    _build_parser,
    _resolve_scope,
    _select_port,
)
from solidity_flow_navigator.flow.config import ConfigError
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope


def _args(
    *,
    config: str | None = None,
    exclude_path: list[str] | None = None,
    exclude_contract: list[str] | None = None,
    inline_library: list[str] | None = None,
    stub_path: list[str] | None = None,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring what ``main()``'s parser produces.

    Only the scope-related subset is filled in — ``_resolve_scope`` reads
    nothing else. The Namespace shape matches what argparse produces in
    ``main()`` after v0.10.0 Stage 2 (no ``legacy`` / ``no_default_excludes``
    / ``host`` / ``json`` fields).
    """

    return argparse.Namespace(
        config=config,
        exclude_path=exclude_path,
        exclude_contract=exclude_contract,
        inline_library=inline_library,
        stub_path=stub_path,
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
    keys keep their default values (since they were absent from the file).

    Since v0.10.0 Stage 2 removed ``--no-default-excludes``, this
    config-file ``[]`` path is the only way to drop a built-in default.
    """

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


# ---------------------------------------------------------------------------
# --expand-all (v0.10.0 Stage 1)
# ---------------------------------------------------------------------------


def test_expand_all_flag_defaults_false() -> None:
    """Omitting ``--expand-all`` leaves ``args.expand_all`` False so the
    Flow page renders root-only (the default progressive-render path)."""
    parser = _build_parser()
    args = parser.parse_args(["some/repo"])
    assert args.expand_all is False


def test_expand_all_flag_parses_true() -> None:
    """``--expand-all`` flips ``args.expand_all`` to True; the value is
    threaded into ``create_app(expand_all=...)`` by ``main()`` and emerges
    as ``data-expand-all="true"`` on the rendered Flow page (covered by
    tests/serve/test_app.py::test_flow_page_expand_all_propagates_to_data_attribute)."""
    parser = _build_parser()
    args = parser.parse_args(["--expand-all", "some/repo"])
    assert args.expand_all is True


# ---------------------------------------------------------------------------
# v0.10.0 Stage 2 removals — pin that the dead flags are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--legacy",
        "--json",
        "--host",
        "--no-default-excludes",
    ],
)
def test_removed_flags_no_longer_parse(flag: str) -> None:
    """v0.10.0 Stage 2 retired four flags. Pin that they error out so a
    future refactor that re-adds one without spec backing trips the test.
    argparse exits the process via ``SystemExit`` on an unknown flag.
    """
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([flag, "some/repo"])


# ---------------------------------------------------------------------------
# --port: default auto-select + explicit semantics (v0.10.0 Stage 2)
# ---------------------------------------------------------------------------


def _bind_holder(port: int) -> socket.socket:
    """Bind a real socket to ``(127.0.0.1, port)`` and return it.

    Used to occupy a port for the duration of a test. The caller must
    close the socket in a ``try/finally`` so the port is released even if
    the assertion fails.

    ``SO_REUSEADDR`` is intentionally NOT set here — we want a TRUE bind
    so the cli's ``SO_REUSEADDR``-decorated probe still fails. (Two
    sockets both with ``SO_REUSEADDR`` on the same host:port WILL both
    bind on macOS / Linux, which would silently false-pass the test.)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((SERVER_HOST, port))
    sock.listen(1)
    return sock


def _find_free_port_above(start: int) -> int:
    """Return a port we can bind right now, starting from ``start``.

    Used by tests that need to know an unused port without racing the
    cli's own auto-select. We probe and immediately release; tests rely
    on no other process grabbing the port between this call and the
    next bind, which is realistic on a CI machine running one test at a
    time.
    """
    for p in range(start, start + 64):
        try:
            _bind_probe(p)
        except OSError:
            continue
        return p
    raise RuntimeError(f"no free port in [{start}, {start + 64})")


def test_select_port_default_returns_8080_when_free() -> None:
    """No --port flag, 8080 free → return 8080. The auto-select probe
    walks upward starting from DEFAULT_PORT; the first candidate is
    DEFAULT_PORT itself.
    """
    # Pre-flight: skip cleanly if our test environment doesn't actually
    # have 8080 free (some CI runners hold it). The test's subject is the
    # function's behavior, not the environment, so a skip on an occupied
    # 8080 is honest.
    try:
        _bind_probe(DEFAULT_PORT)
    except OSError:
        pytest.skip(f"port {DEFAULT_PORT} is occupied in the test environment")
    chosen = _select_port(None)
    assert chosen == DEFAULT_PORT


def test_select_port_default_skips_occupied_8080(capsys) -> None:
    """No --port flag, 8080 occupied → auto-select picks the next free
    port above 8080. The chosen port satisfies the bind probe; no error
    is printed to stderr because auto-select treats EADDRINUSE as a
    "keep walking" signal, not a hard failure.
    """
    holder = _bind_holder(DEFAULT_PORT)
    try:
        chosen = _select_port(None)
    finally:
        holder.close()
    assert chosen is not None
    assert chosen > DEFAULT_PORT
    assert chosen < DEFAULT_PORT + 64  # within the probe window
    err = capsys.readouterr().err
    assert err == "", (
        "auto-select must not print to stderr on a successful walk; " f"got: {err!r}"
    )


def test_select_port_explicit_busy_is_hard_error(capsys) -> None:
    """``--port N`` explicit + N occupied → return None and print a clear
    error. solflow MUST NOT silently reassign to N+1 (that would defeat
    the point of asking for a specific port).
    """
    busy = _find_free_port_above(DEFAULT_PORT + 100)
    holder = _bind_holder(busy)
    try:
        result = _select_port(busy)
    finally:
        holder.close()
    assert result is None
    err = capsys.readouterr().err
    assert f"--port {busy}" in err
    assert "already in use" in err


def test_select_port_explicit_free_returns_that_port() -> None:
    """``--port N`` explicit + N free → return N. No probing happens."""
    free = _find_free_port_above(DEFAULT_PORT + 100)
    assert _select_port(free) == free


def test_port_flag_default_is_none() -> None:
    """Omitting ``--port`` produces ``args.port is None`` so ``_select_port``
    knows to invoke the auto-select probe. An int default would short-
    circuit the probe and re-introduce the "explicit-busy crashes" bug.
    """
    parser = _build_parser()
    args = parser.parse_args(["some/repo"])
    assert args.port is None


def test_port_flag_parses_int() -> None:
    """``--port 9090`` produces an int Namespace value (argparse type=int).
    The value flows through ``_serve(port=args.port)`` straight into
    ``_select_port``'s explicit-port branch.
    """
    parser = _build_parser()
    args = parser.parse_args(["--port", "9090", "some/repo"])
    assert args.port == 9090


# ---------------------------------------------------------------------------
# --help grouping (v0.10.0 Stage 3, cosmetic)
# ---------------------------------------------------------------------------


def test_help_uses_four_argument_groups() -> None:
    """v0.10.0 Stage 3 regroups ``--help`` into four argparse argument
    groups by concern: Scope, Resolution, Rendering, Server. The labels
    are the source of truth; a tighter assertion on the rendered shape
    would over-fix to argparse's specific layout (and break on argparse
    upgrades). This loose pin trips only if a group title is dropped or
    renamed without an accompanying spec / HANDOFF update.
    """
    help_text = _build_parser().format_help()
    for label in ("Scope:", "Resolution:", "Rendering:", "Server:"):
        assert label in help_text, (
            f"--help is missing the {label!r} argument group "
            "(v0.10.0 Stage 3 regrouping)."
        )


def test_version_flag_prints_version_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.13.0 (spec §11.2 / §14.1): ``solflow --version`` prints
    ``solflow <version>`` and exits 0. The version comes from the installed
    distribution's metadata, so this asserts the shape rather than a literal
    value. ``solflow unknown`` means the metadata fallback fired, which in a
    dev or CI environment (editable or normal install present) is a real
    failure — most likely the editable install predates the v0.11.x
    distribution rename and needs ``pip install -e . --no-deps`` rerun.
    """
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("solflow "), f"unexpected --version output: {out!r}"
    assert out != "solflow unknown", (
        "--version fell back to 'unknown': no 'solflow' distribution "
        "metadata found in this environment."
    )
