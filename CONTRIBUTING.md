# Contributing to SolFlow

Thanks for your interest in improving SolFlow. This is a small, focused project;
clear bug reports and tightly-scoped pull requests are the most useful
contributions.

## Reporting issues

- **Bugs in SolFlow** — incorrect call resolution, crashes, layout defects,
  missing or misattributed edges, anything where SolFlow shows you something
  demonstrably wrong about your code — open a
  [GitHub issue](https://github.com/norah1499/solidity-flow-navigator/issues)
  with the contract or repository that triggers it and what you expected to see.
- **Security-sensitive bugs** — please follow [SECURITY.md](SECURITY.md) and use
  GitHub's private vulnerability reporting rather than a public issue.

SolFlow is a read-only visualizer, not a vulnerability scanner. By design it
does not auto-remediate build failures, modify your environment, or produce
partial analyses when compilation fails. Feature requests that change that
scope are unlikely to be accepted.

## Development setup

Requires Python 3.11+ and a Solidity toolchain (for example `solc-select`) to
run against real repositories.

```
git clone https://github.com/norah1499/solidity-flow-navigator
cd solidity-flow-navigator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the checks

```
pytest          # test suite
black .         # format
ruff check .    # lint
```

All three run in CI on Python 3.11–3.14; please make sure they pass locally
before opening a pull request. A few integration tests build an external
Solidity repository checked out alongside this one; the pure unit tests are the
core of the suite and run standalone.

## Architecture

SolFlow is organized in three layers with a strict import discipline:

- **Layer 1 — `analysis/`**: runs Slither / crytic-compile and extracts raw
  facts. The only layer that imports Slither.
- **Layer 2 — `flow/`**: pure transformation from facts to Flow data. Standard
  library and `typing` only — no Slither, no Flask, no I/O.
- **Layer 3 — `serve/`**: the Flask app and the static frontend. The only layer
  that imports Flask.

Keep changes within a layer's responsibility and avoid introducing cross-layer
imports — that separation is what keeps the analysis testable and the tool
local-only.

## Pull requests

- Keep each PR focused on a single change.
- Include or update tests for behavior changes (Layer 2 especially).
- Match the surrounding code style; `black` and `ruff` settle formatting.
