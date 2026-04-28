"""Entry point for ``python -m solidity_flow_navigator``; delegates to the CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
