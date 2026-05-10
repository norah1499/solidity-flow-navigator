"""CLI entry point.

In v0 (Layers 1 + 2), the CLI compiles the repository, extracts raw facts via
the Slither wrapper, builds one Flow per entry point via Layer 2, and dumps
the resulting Flow tree as JSON to stdout. Layer 3 will later replace the
JSON dump with a local Flask server that renders Flows in the browser; the
compile / extract / build pipeline stays the same.

JSON output shape:
    {"repo_path": str, "flows": [Flow, ...]}

The ``flows`` list is built from ``dataclasses.asdict`` over each Flow.
Discriminated FlowNode variants serialize via the ``node_type`` field
("function" | "unresolved" | "external"); StrEnum values (UnresolvedReason)
serialize as their string values without a custom encoder.

Exit codes:
    0  - success; JSON written to stdout
    1  - crytic-compile rejected the repository (per spec §9.1, the underlying
         error is printed verbatim to stderr; nothing is written to stdout)

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solflow",
        description=(
            "Solidity Flow Navigator (v0 / Layers 1+2): compile a Solidity "
            "repository, extract facts, and emit one Flow per entry point "
            "as JSON."
        ),
    )
    parser.add_argument(
        "repo_path",
        help="Path to the Solidity repository root (crytic-compile target).",
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
    output = {
        "repo_path": facts.repo_path,
        "flows": [asdict(f) for f in flows],
    }
    json.dump(output, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0
