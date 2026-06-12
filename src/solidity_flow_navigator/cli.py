"""CLI entry point.

Compile the repository, extract Layer 1 facts, build Layer 2 Flows, and
start a local Flask server bound to 127.0.0.1 that renders an index of
entry points plus one navigable page per Flow. The CLI prints the chosen
``http://127.0.0.1:<port>`` URL on startup and blocks until Ctrl-C.

v0.10.0 Stage 2 finalized the CLI surface:

- Removed ``--legacy``: the all-at-once renderer is retired in favor of
  ``--expand-all`` on the progressive renderer (spec §10.2). The
  bird's-eye-view use case is preserved; the second renderer is gone.
- Removed ``--json``: the Layer-2-IR-to-stdout dump was a debugging
  fallback during Layers 1/2 development. The tool is serve-only now.
  The internal Flow JSON the frontend reads is unchanged.
- Removed ``--host``: the server always binds 127.0.0.1, which makes the
  index page's privacy footer ("server bound to 127.0.0.1") honest.
- Removed ``--no-default-excludes``: the capability survives via the
  config-file ``[]``-clears-default rule (§11.2). The CLI flag was a
  redundant second path that didn't justify its surface.
- Added port auto-select to ``--port``: with no flag (the default), the
  server probes upward from 8080 for the first bindable port and prints
  the chosen URL. An *explicit* ``--port N`` that is already in use is a
  hard error (no silent reassignment off an explicit value).

v0.1 added the scope-rule flags ``--exclude-path``, ``--exclude-contract``,
``--inline-library``, ``--config``) per spec §11.2. v0.2 added
``--stub-path`` for compressing in-tree libraries — matched call targets
emit ExternalNode instead of recursing (§11.2 / §11.8). The Scope
resolution order is now defaults → file values → CLI appends (the
v0.10.0 Stage 2 cut removed the ``--no-default-excludes`` clear step;
file ``[]`` is the only way to clear a default key).

Exit codes:
    0  - server stopped cleanly via Ctrl-C
    1  - crytic-compile rejected the repository (per spec §9.1, the
         underlying error is printed verbatim to stderr; nothing is
         written to stdout)
    1  - explicit ``--port`` was already in use (auto-select is OFF for
         explicit values per spec §11.2)
    1  - port auto-select exhausted (no free port found in the probed
         range)
    1  - ``--config`` file not found, or solflow.toml is malformed / has
         bad value types (printed to stderr)

Other failures (Slither crashes during fact extraction, builder bugs,
etc.) propagate as Python tracebacks so wrapper bugs stay debuggable.
"""

import argparse
import errno
import importlib.metadata
import socket
import sys
from collections.abc import Sequence
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

# The server always binds the loopback interface — there is no ``--host``
# flag. Keeping the constant named so other modules / tests have a single
# source of truth (and so the printed startup banner reads from the same
# value the bind call uses).
SERVER_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
# Probe at most this many consecutive ports above DEFAULT_PORT when no
# ``--port`` flag was given. 64 is generous for a developer machine: any
# user with that many stale solflow instances already has worse problems
# than a port miss.
PORT_PROBE_LIMIT = 64
DEFAULT_CONFIG_FILENAME = "solflow.toml"


def _distribution_version() -> str:
    """Version of the installed ``solflow`` distribution.

    Read from package metadata so pyproject stays the single source of
    truth (spec §14.1, v0.13.0). Falls back to ``"unknown"`` when no
    metadata is resolvable — e.g. running from a source tree without an
    editable install — rather than failing the parser build.
    """
    try:
        return importlib.metadata.version("solflow")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for ``solflow``.

    Extracted so tests can exercise flag parsing directly without going
    through the rest of ``main()`` (compilation, fact extraction, server
    bind). The shape mirrors what ``main()`` would build inline.

    Flags are organised into four argument groups so ``--help`` reads by
    concern rather than as a flat list (v0.10.0 Stage 3 cosmetic). The
    groups are cosmetic only — argparse parses the same Namespace shape
    regardless of grouping, so the groupings don't propagate to test
    fixtures or production code.

    - **Scope**: which contracts produce Flows (path/contract excludes,
      config file).
    - **Resolution**: how unresolved call targets get treated (library
      inlining vs in-tree stubbing).
    - **Rendering**: how Flow pages render in the browser.
    - **Server**: how the local server binds.
    """
    parser = argparse.ArgumentParser(
        prog="solflow",
        description=(
            "Solidity Flow Navigator: compile a Solidity repository, "
            "extract facts, and serve one navigable Flow per entry point "
            "on http://127.0.0.1."
        ),
    )
    parser.add_argument(
        "repo_path",
        help="Path to the Solidity repository root (crytic-compile target).",
    )
    # Top-level alongside -h, deliberately outside the four argument
    # groups: tool chrome, not a Scope/Resolution/Rendering/Server
    # concern (spec §11.2, §14.1 v0.13.0).
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_distribution_version()}",
        help="Print the solflow version and exit.",
    )

    # ------------------------------------------------------------------
    # Scope: which contracts produce Flows. (spec §11.2)
    # ------------------------------------------------------------------
    scope_group = parser.add_argument_group(
        "Scope",
        "Which contracts in the repository produce Flows.",
    )
    scope_group.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a TOML config file. When omitted, solflow looks for "
            f"'{DEFAULT_CONFIG_FILENAME}' in the current working directory "
            "and silently skips if absent. When set, the named file must "
            "exist — a missing file is a hard error."
        ),
    )
    scope_group.add_argument(
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
    scope_group.add_argument(
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

    # ------------------------------------------------------------------
    # Resolution: how unresolved call targets get treated. (§11.2 / §11.8)
    # ------------------------------------------------------------------
    resolution_group = parser.add_argument_group(
        "Resolution",
        "How unresolved call targets get treated — recurse into "
        "selected libraries or terminate at in-tree stubs.",
    )
    resolution_group.add_argument(
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
    resolution_group.add_argument(
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

    # ------------------------------------------------------------------
    # Rendering: how Flow pages render in the browser. (§10.2)
    # ------------------------------------------------------------------
    rendering_group = parser.add_argument_group(
        "Rendering",
        "How Flow pages render in the browser.",
    )
    rendering_group.add_argument(
        "--expand-all",
        action="store_true",
        help=(
            "Render every Flow page with its full call tree expanded at "
            "page load (equivalent to clicking every expandable call site). "
            "Same progressive renderer, just a different initial expansion "
            "state — per-line edge anchoring, the §10.3 direction model, "
            "and all v0.9 reorder passes apply identically. Default is "
            "root-only with click-to-expand."
        ),
    )

    # ------------------------------------------------------------------
    # Server: how the local server binds.
    # ------------------------------------------------------------------
    server_group = parser.add_argument_group(
        "Server",
        "How the local server binds. The host is always 127.0.0.1.",
    )
    server_group.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"Bind the local server to port N. With no flag, solflow binds "
            f"{DEFAULT_PORT} or the next free port above it (probes up to "
            f"{PORT_PROBE_LIMIT} ports) and prints the chosen URL. An "
            f"explicit --port N that is already in use is a hard error "
            f"— solflow will not silently reassign."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
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

    return _serve(
        facts,
        flows,
        port=args.port,
        expand_all=args.expand_all,
        scope=scope,
    )


def _resolve_scope(args: argparse.Namespace, cwd: Path) -> Scope:
    """Resolve the final ``Scope`` per spec §11.2's resolution order.

    1. Start with ``DEFAULT_SCOPE``.
    2. If a config file is present (explicit --config or default
       ``solflow.toml`` in CWD), apply its ``PartialScope``: for each key,
       file values replace the current value when present. A file value of
       ``[]`` explicitly clears that key — the only way to drop the
       built-in defaults since v0.10.0 Stage 2 removed
       ``--no-default-excludes``.
    3. Append CLI flag values to the resulting tuples.

    ``cwd`` is passed explicitly (rather than calling ``Path.cwd()``) so
    tests can exercise resolution against a tmp directory without
    monkey-patching the process CWD.
    """

    base = DEFAULT_SCOPE
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


def _is_port_in_use_error(exc: OSError) -> bool:
    """Distinguish "port already in use" from other bind failures.

    Used by the port auto-select loop to keep probing only on EADDRINUSE.
    Other errors (permission denied, invalid address, etc.) should abort
    the probe and propagate so the user sees the real failure.
    """
    return exc.errno in (errno.EADDRINUSE, errno.EACCES)


def _select_port(explicit_port: int | None) -> int | None:
    """Return the port to bind, or ``None`` on hard failure.

    Rules per spec §11.2 ``--port`` entry:

    - ``explicit_port`` is ``None`` (no flag) → probe upward from
      ``DEFAULT_PORT`` for the first bindable port, up to
      ``PORT_PROBE_LIMIT`` candidates. Print no diagnostic; the chosen
      port appears in the startup banner.
    - ``explicit_port`` is set → check that exact port. If it's in use,
      print a clear error and return ``None``. Do NOT auto-increment.
    - All probe candidates busy → print a clear error and return
      ``None``.

    The returned port is the one the caller should bind. The check is
    racy by definition (someone could grab the port between this probe
    and the actual ``app.run`` call), but it converts the common
    stale-solflow-on-8080 case into a deterministic next-port pick
    instead of a Werkzeug traceback.
    """
    if explicit_port is not None:
        try:
            _bind_probe(explicit_port)
        except OSError as exc:
            if _is_port_in_use_error(exc):
                print(
                    f"solflow: --port {explicit_port} is already in use. "
                    f"Pick a different port or stop the process holding it.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"solflow: could not bind to {SERVER_HOST}:"
                    f"{explicit_port} ({exc.strerror or exc}).",
                    file=sys.stderr,
                )
            return None
        return explicit_port

    last_exc: OSError | None = None
    for candidate in range(DEFAULT_PORT, DEFAULT_PORT + PORT_PROBE_LIMIT):
        try:
            _bind_probe(candidate)
        except OSError as exc:
            last_exc = exc
            if _is_port_in_use_error(exc):
                continue
            print(
                f"solflow: could not bind to {SERVER_HOST}:{candidate} "
                f"({exc.strerror or exc}).",
                file=sys.stderr,
            )
            return None
        return candidate

    # Exhausted the probe range. Every port DEFAULT_PORT .. +PROBE_LIMIT
    # was in use.
    last_msg = last_exc.strerror if last_exc else "no free port found"
    print(
        f"solflow: tried ports {DEFAULT_PORT}–{DEFAULT_PORT + PORT_PROBE_LIMIT - 1} "
        f"and all are in use ({last_msg}). Free a port or pass --port N.",
        file=sys.stderr,
    )
    return None


def _bind_probe(port: int) -> None:
    """Try to bind a fresh socket to ``(SERVER_HOST, port)``.

    Raises ``OSError`` (with ``errno`` set) on any bind failure. The
    socket is closed before returning, so the caller can re-bind on the
    same port immediately. ``SO_REUSEADDR`` mirrors what Werkzeug sets
    on its socket, so the probe doesn't false-positive on a port we'd
    actually be able to grab on the real bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((SERVER_HOST, port))


def _serve(
    facts,
    flows,
    *,
    port: int | None,
    expand_all: bool = False,
    scope: Scope | None = None,
) -> int:
    """Start the local server. Selects a port (auto or explicit) and binds."""
    chosen = _select_port(port)
    if chosen is None:
        return 1

    app = create_app(facts, flows, expand_all=expand_all, scope=scope)
    print(
        f"Solidity Flow Navigator running at http://{SERVER_HOST}:{chosen}",
        flush=True,
    )
    try:
        run_server(app, SERVER_HOST, chosen)
    except KeyboardInterrupt:
        return 0
    return 0
