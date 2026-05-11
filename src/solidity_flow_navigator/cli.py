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

Exit codes:
    0  - success (server stopped cleanly, or JSON written successfully)
    1  - crytic-compile rejected the repository (per spec §9.1, the underlying
         error is printed verbatim to stderr; nothing is written to stdout)
    1  - server bind failure (e.g. requested port already in use)

Other failures (Slither crashes during fact extraction, builder bugs, etc.)
propagate as Python tracebacks so wrapper bugs stay debuggable.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from .analysis.compile import CompilationFailure, compile_repo
from .analysis.slither_facts import extract_facts
from .flow.builder import build_flows
from .flow.scope import DEFAULT_SCOPE
from .serve.app import create_app, run_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


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
    args = parser.parse_args(argv)

    try:
        cc = compile_repo(args.repo_path)
        facts = extract_facts(cc, args.repo_path)
    except CompilationFailure as exc:
        # Per spec §9.1, print the underlying error verbatim and exit non-zero.
        print(str(exc), file=sys.stderr)
        return 1

    flows = build_flows(facts, DEFAULT_SCOPE)

    if args.json:
        output = {
            "repo_path": facts.repo_path,
            "flows": [asdict(f) for f in flows],
        }
        json.dump(output, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    return _serve(facts, flows, args.host, args.port)


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
