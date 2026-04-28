"""CLI entry point.

In v0 (Layer 1 only), the CLI runs the Slither wrapper and dumps the raw
RepoFacts as JSON to stdout. Layer 3 will later replace the JSON dump with a
local Flask server that renders Flows in the browser; the compile step and
fact extraction will stay the same.

Exit codes:
    0  - success; JSON written to stdout
    1  - crytic-compile rejected the repository (per spec §9.1, the underlying
         error is printed verbatim to stderr; nothing is written to stdout)

Other failures (Slither crashes during fact extraction, etc.) propagate as
Python tracebacks so wrapper bugs stay debuggable.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from .analysis.compile import CompilationFailure, compile_repo
from .analysis.slither_facts import extract_facts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solflow",
        description=(
            "Solidity Flow Navigator (v0 / Layer 1): compile a Solidity "
            "repository and emit the raw extracted facts as JSON."
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

    json.dump(asdict(facts), sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0
