"""CLI entry point.

Default mode (Layer 3, v0): compile the repository, extract Layer 1 facts,
build Layer 2 Flows, and start a local Flask server that renders an index
of entry points plus one navigable page per Flow. The CLI prints the
``localhost`` URL on startup and blocks until Ctrl-C.

``--json`` mode preserves the Layer 2 stdout dump used during Layers 1/2
development and as a debugging fallback.

JSON output shape:
    {"repo_path": str, "flows": [Flow, ...]}

The ``flows`` list is built from ``dataclasses.asdict`` over each Flow.
Discriminated FlowNode variants serialize via the ``node_type`` field
("function" | "unresolved" | "external"); StrEnum values (UnresolvedReason)
serialize as their string values without a custom encoder.

v0.1 added scope-rule flags (``--exclude-path``, ``--exclude-contract``,
``--inline-library``, ``--no-default-excludes``, ``--config``) per spec
§11.2. v0.2 adds ``--stub-path`` (repeatable) for compressing in-tree
libraries — matched call targets emit ExternalNode instead of recursing
(§11.2 / §11.8). Resolution layers defaults → optional default-clear →
file values → CLI appends; the final ``Scope`` is passed to ``build_flows``.

Exit codes:
    0  - success (server stopped cleanly, or JSON written successfully)
    1  - crytic-compile rejected the repository (per spec §9.1, the underlying
         error is printed verbatim to stderr; nothing is written to stdout)
    1  - server bind failure (e.g. requested port already in use)
    1  - --config file not found, or solflow.toml is malformed / has bad
         value types (printed to stderr)

Other failures (Slither crashes during fact extraction, builder bugs, etc.)
propagate as Python tracebacks so wrapper bugs stay debuggable.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from .analysis.compile import CompilationFailure, compile_repo
from .analysis.slither_facts import extract_facts
from .flow.builder import build_flows
from .flow.config import (
    ConfigError,
    PartialScope,
    apply_partial,
    load_partial_scope_from_toml,
)
from .flow.scope import DEFAULT_SCOPE, Scope
from .serve.app import create_app, run_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_CONFIG_FILENAME = "solflow.toml"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solflow",
        description=(
            "Solidity Flow Navigator: compile a Solidity repository, extract "
            "facts, and serve one navigable Flow per entry point in the "
            "browser. Use --json to dump the Layer 2 IR to stdout instead."
        ),
    )
    parser.add_argument(
        "repo_path",
        help="Path to the Solidity repository root (crytic-compile target).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit Layer 2 Flow IR as JSON to stdout and exit, instead of "
        "starting the local web server.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host interface to bind the local server to (default {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind the local server to (default {DEFAULT_PORT}).",
    )
    # ------------------------------------------------------------------
    # v0.1 scope-rule flags (spec §11.2)
    # ------------------------------------------------------------------
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a TOML config file. When omitted, solflow looks for "
            f"'{DEFAULT_CONFIG_FILENAME}' in the current working directory "
            "and silently skips if absent. When set, the named file must "
            "exist — a missing file is a hard error."
        ),
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "Gitignore-style glob for filenames whose contracts should not "
            "produce Flows. Matched against each contract's source path. "
            "Repeatable: appends to the defaults and any config-file values."
        ),
    )
    parser.add_argument(
        "--exclude-contract",
        action="append",
        default=None,
        metavar="PATTERN",
        help=(
            "Gitignore-style glob matched against contract names (treated as "
            "single path segments — no '/' involved). Matching contracts "
            "produce no Flows. Repeatable: appends to defaults and config."
        ),
    )
    parser.add_argument(
        "--inline-library",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Name of a library under lib/<NAME>/ whose source should be "
            "recursed into instead of stubbed as ExternalNode. Default is "
            "empty (everything under lib/ is stubbed). Repeatable."
        ),
    )
    parser.add_argument(
        "--stub-path",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "Gitignore-style glob for call targets whose bodies should NOT "
            "be recursed into; matching targets emit ExternalNode (terminal) "
            "instead. Used to compress dense in-tree libraries (math, utility, "
            "etc.). Per spec §11.8, --stub-path wins over --inline-library "
            "when both match the same path. Repeatable."
        ),
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=(
            "Clear the built-in default exclude patterns ('**/*.t.sol', "
            "'**/test/**', '**/tests/**', '**/mocks/**' for paths and "
            "'*Mock*' for contract names) before applying file and CLI "
            "values. Does not affect --inline-library or --stub-path "
            "(no built-in defaults to clear)."
        ),
    )
    args = parser.parse_args(argv)

    # Resolve the Scope BEFORE compilation: a broken config shouldn't make
    # the user wait through an 18-second forge build to learn their TOML
    # has a typo.
    try:
        scope = _resolve_scope(args, Path.cwd())
    except FileNotFoundError as exc:
        # Only the explicit --config path raises this; default-lookup
        # silently skips. ``exc.filename`` is the path tomllib tried to open.
        print(
            f"solflow: config file not found: {exc.filename}",
            file=sys.stderr,
        )
        return 1
    except ConfigError as exc:
        print(f"solflow: {exc}", file=sys.stderr)
        return 1

    try:
        cc = compile_repo(args.repo_path)
        facts = extract_facts(cc, args.repo_path)
    except CompilationFailure as exc:
        # Per spec §9.1, print the underlying error verbatim and exit non-zero.
        print(str(exc), file=sys.stderr)
        return 1

    flows = build_flows(facts, scope)

    if args.json:
        output = {
            "repo_path": facts.repo_path,
            "flows": [asdict(f) for f in flows],
        }
        json.dump(output, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    return _serve(facts, flows, args.host, args.port)


def _resolve_scope(args: argparse.Namespace, cwd: Path) -> Scope:
    """Resolve the final ``Scope`` per spec §11.2's four-step order.

    1. Start with ``DEFAULT_SCOPE``.
    2. If ``--no-default-excludes`` is set, clear ``exclude_paths`` and
       ``exclude_contracts`` on the base (``inline_libraries`` and
       ``stub_paths`` have no defaults to clear).
    3. If a config file is present (explicit --config or default
       ``solflow.toml`` in CWD), apply its ``PartialScope``: for each key,
       file values replace the current value when present.
    4. Append CLI flag values to the resulting tuples.

    ``cwd`` is passed explicitly (rather than calling ``Path.cwd()``) so
    tests can exercise resolution against a tmp directory without
    monkey-patching the process CWD.
    """

    base = DEFAULT_SCOPE
    if args.no_default_excludes:
        base = replace(base, exclude_paths=(), exclude_contracts=())

    partial = _load_partial_scope(args.config, cwd)
    base = apply_partial(base, partial)

    return Scope(
        exclude_paths=base.exclude_paths + tuple(args.exclude_path or ()),
        exclude_contracts=base.exclude_contracts + tuple(args.exclude_contract or ()),
        inline_libraries=base.inline_libraries + tuple(args.inline_library or ()),
        stub_paths=base.stub_paths + tuple(args.stub_path or ()),
    )


def _load_partial_scope(config_arg: str | None, cwd: Path) -> PartialScope:
    """Load the TOML PartialScope per the --config / default-lookup rules.

    - ``config_arg is None`` (default lookup): try ``cwd/solflow.toml``.
      Missing file → ``PartialScope()`` (silent skip per spec §11.2).
      Malformed file → ``ConfigError`` propagates (broken config is always
      a hard error; "silent skip" is only for *missing* file, not *broken*
      file — a broken default config that silently no-ops would be a
      debugging footgun).
    - ``config_arg`` set (explicit --config): load that path. Missing file
      raises ``FileNotFoundError`` (the user asked for this file by name;
      missing it is a user error worth a non-zero exit).
    """

    if config_arg is None:
        default_path = cwd / DEFAULT_CONFIG_FILENAME
        try:
            return load_partial_scope_from_toml(default_path)
        except FileNotFoundError:
            return PartialScope()
    return load_partial_scope_from_toml(Path(config_arg))


def _serve(facts, flows, host: str, port: int) -> int:
    """Start the local server. Hard-fails on bind errors with a clear message."""
    from .serve.app import _check_port_available

    try:
        _check_port_available(host, port)
    except OSError as exc:
        print(
            f"solflow: could not bind to {host}:{port} ({exc.strerror or exc}). "
            f"Pick a different --port or stop the process holding it.",
            file=sys.stderr,
        )
        return 1

    app = create_app(facts, flows)
    print(f"Solidity Flow Navigator running at http://{host}:{port}", flush=True)
    try:
        run_server(app, host, port)
    except KeyboardInterrupt:
        return 0
    return 0
