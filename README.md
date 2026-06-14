# SolFlow

[![CI](https://github.com/norah1499/solidity-flow-navigator/actions/workflows/ci.yml/badge.svg)](https://github.com/norah1499/solidity-flow-navigator/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/solflow)](https://pypi.org/project/solflow/)
[![Python versions](https://img.shields.io/pypi/pyversions/solflow)](https://pypi.org/project/solflow/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

**Read a Solidity contract the way it executes, not the way its files are organized.**

SolFlow compiles a Solidity repository, extracts call-graph facts with [Slither](https://github.com/crytic/slither), and serves one interactive call-flow visualization (a **Flow**) per external entry point. Every node renders the target function's real source, syntax-highlighted and line-numbered. Analysis runs entirely on your machine. Built for Web3 security auditing.

![SolFlow on Morpho Blue: launching solflow from the terminal, the entry-point index, expanding the liquidate flow into its full call tree, then switching to dark mode](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/demo.gif)

*SolFlow on [Morpho Blue](https://github.com/morpho-org/morpho-blue): launch from the terminal, browse the entry-point index, expand the `liquidate` flow into its full call tree, then flip to dark mode. Pausable stills are under [Screenshots](#screenshots).*

> [!NOTE]
> SolFlow is **not a vulnerability scanner**. It emits no findings and makes no security claims. It shows you the code's shape so you can find the problems faster.

## Why

The first job in any audit is reconstructing what actually happens when someone calls an entry point. The answer is scattered across base contracts, libraries, and modifiers in a dozen files. SolFlow lays it out as a graph you can read.

## Features

- **One Flow per entry point.** The index lists every externally callable function, grouped by contract and split into mutating vs. read-only, with modifier badges, call-tree depth, and unresolved-target counts per entry: the whole audit surface on one page. A filter box narrows the list as you type, by signature or contract name, so protocols with hundreds of entry points stay navigable.
- **Real source, not boxes.** Every node renders the target function's actual code, syntax-highlighted and line-numbered. Click a call site and the callee expands beside it, the edge anchored to the exact line that makes the call.
- **Progressive or bird's-eye.** Flows open at the root and expand on demand, so you reveal only the paths you're tracing. **Expand all** and **Collapse all** controls in the Flow header flip the whole tree at once, or start with `--expand-all` for the full tree at page load. Your expanded call sites are remembered per Flow, so returning to one restores exactly what you had open.
- **Light or dark.** A header toggle sets the palette — Auto (follows your OS), Light, or Dark — so long audit sessions stay easy on the eyes. The choice is server-rendered: no flash on load, and your preference never leaves the machine.
- **Bookmarks & recently-viewed.** Bookmark the entry points (and contracts) you keep returning to — they collect in a *Bookmarked* section at the top of the index, reachable from any scroll position via a floating shortcut. Toggling a bookmark happens in place, so the page doesn't lose your spot. Entry points you've already opened are tinted on the index so you can see your progress at a glance. It's all a first-party localhost cookie — no accounts, nothing leaves the machine — and the toggles still work with JavaScript disabled.
- **It never silently lies.** When a call target can't be resolved statically (an interface with no bound implementation, `addr.call(...)`, computed-target Yul), the node is explicitly marked unresolved, with the reason. No guessing, no silent omissions. The auditor's trust in a Flow's completeness within its declared scope is the load-bearing property.
- **Scope you control.** Inline a dependency, stub a dense in-tree math library, or exclude test and mock contracts, via `solflow.toml` or CLI flags (see [Configuration](#configuration)).
- **Local and private.** Analysis runs entirely on your machine and the server binds only to `127.0.0.1`. Audit code under NDA is never uploaded anywhere.

## Prerequisites

- **Python 3.11+**
- **A `solc` matching your target**, via [solc-select](https://github.com/crytic/solc-select). Slither needs it to compile the project.

## Installation

```bash
pipx install solflow
pipx install solc-select
solc-select install <version> && solc-select use <version>
```

For the latest development version: `pipx install git+https://github.com/norah1499/solidity-flow-navigator`.

To uninstall, `pipx uninstall solflow` removes the tool and all its bundled dependencies; SolFlow writes nothing outside its own install directory. `solc-select` and its downloaded compilers are a separate install, removable with `pipx uninstall solc-select`.

### Updating

Check your installed version with `solflow --version`, and upgrade to the latest release with:

```bash
pipx upgrade solflow
```

SolFlow does not check for or notify you about new versions. It makes no network calls of any kind, the same local-and-private property that keeps audited code on your machine, so there is nothing to phone home for an update check. To hear about new releases, watch the [GitHub releases](https://github.com/norah1499/solidity-flow-navigator/releases) or the [PyPI project page](https://pypi.org/project/solflow/), or simply run `pipx upgrade solflow` now and then.

## Usage

```bash
solflow path/to/your/solidity/project
```

Point it at the repository root, where Slither can resolve dependencies. SolFlow compiles the project, binds `127.0.0.1:8080` (or the next free port), and prints the URL. Open it in your browser to navigate the Flows.

Compilation goes through [crytic-compile](https://github.com/crytic/crytic-compile), so any build system it detects should work (Foundry, Hardhat, Truffle, Brownie, plain solc). Foundry projects are what SolFlow is tested against.

## Configuration

SolFlow runs with sensible defaults. By default it excludes common test and mock directories (`**/*.t.sol`, `**/test/**`, `**/tests/**`, `**/mocks/**`) and `*Mock*` contracts. Refine scope through a config file, CLI flags, or both. CLI flags override file values; file values override defaults.

### CLI flags

Run `solflow --help` for the full reference, grouped into **Scope**, **Resolution**, **Rendering**, and **Server**.

| Flag | What it does |
|------|--------------|
| `--exclude-path GLOB` | Drop contracts whose file path matches the glob (repeatable) |
| `--exclude-contract PATTERN` | Drop contracts whose name matches the pattern (repeatable) |
| `--inline-library NAME` | Recurse into a `lib/<NAME>/` dependency instead of stubbing it (repeatable) |
| `--stub-path GLOB` | Stop at a terminal node for matching in-tree call targets, e.g. dense math libraries (repeatable) |
| `--expand-all` | Open every Flow fully expanded, for a bird's-eye view |
| `--port N` | Bind a specific port (the default auto-selects from 8080 upward) |
| `--config PATH` | Use a config file other than `solflow.toml` in the working directory |
| `--version` | Print the installed SolFlow version and exit |

### Config file

SolFlow looks for `solflow.toml` in the directory you invoke it from. All keys are optional; a missing key uses its default, and setting a key to `[]` clears the default.

```toml
[scope]
exclude_paths = ["**/*.t.sol", "**/test/**", "**/tests/**", "**/mocks/**"]
exclude_contracts = ["*Mock*"]
inline_libraries = []
stub_paths = []
```

## How it works

SolFlow is a thin pipeline over Slither's analysis model:

1. **Compile** the repository via crytic-compile, then extract raw facts from Slither: contracts, functions, modifiers, inheritance order, call edges, and source locations.
2. **Build a Flow per entry point**, applying your scope rules: modifiers folded into the call tree, virtual dispatch resolved through the C3 chain, cross-contract calls resolved against bindings, and every unresolvable branch explicitly marked.
3. **Serve** the Flows as progressive HTML+SVG graphs from a local Flask server, with source highlighted server-side by [Pygments](https://pygments.org/).

## When the first run fails

SolFlow is built on Slither and is contingent on it: anything Slither cannot compile and analyze, SolFlow cannot visualize. When that happens, SolFlow prints the underlying error verbatim and exits without producing anything.

> [!IMPORTANT]
> There is no partial output and no auto-remediation, by design. SolFlow never installs dependencies or modifies the target repository to make a build pass; a half-compiled picture would silently mislead an audit. The rule of thumb: if the project does not build on its own in that directory, SolFlow will not build it either.

The two most common first-run failures are environment issues, not tool bugs:

1. **solc version mismatch.** The active `solc` does not match the project's pragma. Fix: `solc-select install <version> && solc-select use <version>`.
2. **Missing dependencies.** The target was cloned fresh and its libraries were never fetched. Fix: run the project's own setup (`forge install`, `npm install`, or equivalent) and confirm its build succeeds before pointing SolFlow at it.

If the project builds cleanly and Slither itself still fails on it (this happens on some large protocols), that is an upstream Slither limitation rather than a SolFlow defect; an [issue](https://github.com/norah1499/solidity-flow-navigator/issues) is still welcome so it can be tracked.

## Screenshots

<details>
<summary>Stills from the demo above (Morpho Blue)</summary>

The index lists every external entry point of the analyzed repository:

![Index view listing the entry points of Morpho Blue](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/index-morpho.png)

Opening an entry point renders its full call flow; every callee panel shows the real source:

![Expanded call-flow graph of Morpho.liquidate](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/flow-liquidate-overview.png)

![Zoomed view of the liquidate flow with library functions](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/flow-liquidate-detail.png)

</details>

## Development

Issues and pull requests are welcome. Before opening a PR, make sure these pass:

```bash
pytest
black --check .
ruff check
```
