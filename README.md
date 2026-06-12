# SolFlow (Solidity Flow Navigator)

[![CI](https://github.com/norah1499/solidity-flow-navigator/actions/workflows/ci.yml/badge.svg)](https://github.com/norah1499/solidity-flow-navigator/actions/workflows/ci.yml)

**Read a contract the way it executes, not the way its files are organized.**

SolFlow compiles a Solidity repository, extracts call-graph facts with [Slither](https://github.com/crytic/slither), and serves one interactive call-flow visualization per external entry point. Every panel shows the function's real source. Built for smart contract auditing.

![SolFlow navigating Morpho Blue: the entry-point index, the fully expanded liquidate flow, and the same flow zoomed to source level](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/demo.gif)

*SolFlow on [Morpho Blue](https://github.com/morpho-org/morpho-blue): the entry-point index, the fully expanded `liquidate` flow, and the same flow zoomed to source level. Pausable stills are under [Screenshots](#screenshots).*

## Why

The first job in any audit is reconstructing what actually happens when someone calls an entry point. The answer is scattered across base contracts, libraries, and modifiers in a dozen files. SolFlow lays it out as a graph you can read:

- **One Flow per entry point.** The index lists every externally callable function, grouped by contract and split into mutating vs read-only, with modifier badges, call-tree depth, and unresolved-target counts per entry: the whole audit surface on one page.
- **Real source, not boxes.** Every node renders the target function's actual code, syntax-highlighted and line-numbered. Click a call site and the callee expands beside it, the edge anchored to the exact line that makes the call.
- **It never silently lies.** When a call target can't be resolved statically (an interface with no bound implementation, `addr.call(...)`, computed-target Yul), the node is explicitly marked unresolved, with the reason. No guessing, no silent omissions.
- **Local and private.** Analysis runs entirely on your machine and the server binds only to `127.0.0.1`. Audit code is never uploaded anywhere.

SolFlow is **not a vulnerability scanner**. It emits no findings and makes no security claims. It shows you the code's shape so you can find the problems faster.

## Install

Requires Python 3.11+ and a `solc` matching your target, via [solc-select](https://github.com/crytic/solc-select) (Slither needs it to compile):

```bash
pipx install solflow
pipx install solc-select
solc-select install <version> && solc-select use <version>
```

For the latest development version instead: `pipx install git+https://github.com/norah1499/solidity-flow-navigator`.

To uninstall: `pipx uninstall solflow` removes the tool and all its bundled dependencies; SolFlow writes nothing outside its own install directory, so nothing else is left behind. `solc-select` and its downloaded compilers are a separate install, removable with `pipx uninstall solc-select`.

## Use

```bash
solflow path/to/your/solidity/project
```

Point it at the repository root, where Slither can resolve dependencies. SolFlow compiles the project, binds `127.0.0.1:8080` (or the next free port), and prints the URL.

Compilation goes through [crytic-compile](https://github.com/crytic/crytic-compile), so any build system it detects should work (Foundry, Hardhat, Truffle, Brownie, plain solc); Foundry projects are what SolFlow is tested against. If compilation fails, SolFlow prints the compiler error verbatim and exits without producing anything. No partial Flows, by design: a half-compiled picture would silently mislead. The usual fix is matching the project's pragma with `solc-select use <version>`.

Useful flags (run `solflow --help` for the full reference, grouped into **Scope**, **Resolution**, **Rendering**, and **Server**):

| Flag | What it does |
|------|--------------|
| `--expand-all` | Open every Flow fully expanded, for a bird's-eye view |
| `--exclude-path GLOB`, `--exclude-contract PATTERN` | Narrow which contracts produce Flows |
| `--inline-library NAME` | Recurse into a `lib/<NAME>/` dependency instead of stubbing it |
| `--port N` | Bind a specific port |

## Screenshots

<details>
<summary>Stills from the demo above (Morpho Blue)</summary>

The index lists every external entry point of the analyzed repository:

![Index view listing the entry points of Morpho Blue](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/index-morpho.png)

Opening an entry point renders its full call flow; every callee panel shows the real source:

![Expanded call-flow graph of Morpho.liquidate](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/flow-liquidate-overview.png)

![Zoomed view of the liquidate flow with inherited library functions](https://raw.githubusercontent.com/norah1499/solidity-flow-navigator/main/docs/flow-liquidate-detail.png)

</details>

## Contributing

Issues and pull requests are welcome. Before opening a PR, make sure these pass:

```bash
pytest
black --check .
ruff check
```

## License

AGPL-3.0, and not a free choice: SolFlow builds on Slither and crytic-compile, both AGPL-3.0. If you host an instance for others, the license requires offering them the source; the index footer links back here.
