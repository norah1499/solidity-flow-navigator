# Security Policy

## What SolFlow is

SolFlow (Solidity Flow Navigator) is a read-only call-graph visualizer for Solidity repositories, built to speed up navigation during Web3 security audits. It compiles a project with `crytic-compile` + Slither, extracts call-graph facts, and serves one interactive Flow per externally callable entry point on `127.0.0.1` so an auditor can read code in a navigable graph instead of file-by-file.

**SolFlow is not a vulnerability scanner.** It detects no vulnerabilities, makes no security claims about the code it visualizes, and emits no findings. When a call target cannot be resolved statically — interface calls without binding, `addr.call(...)`, computed-target Yul, abstract functions with no implementation in the C3 chain — the node is marked **unresolved** with an explicit reason rather than guessed at or silently omitted (Principle P4 in the spec: the picture never silently lies). Verify any conclusion you draw from a SolFlow visualization against the underlying source yourself.

## Supported versions

Only the latest tagged release is supported. Older versions receive no fixes; upgrade to the latest tag.

## Reporting a bug

Bugs in SolFlow itself — incorrect call resolution, crashes, layout defects, missing or misattributed edges, anything where SolFlow shows you something demonstrably wrong about your code — should be reported as a GitHub issue: <https://github.com/norah1499/solidity-flow-navigator/issues>.

## Reporting something sensitive

If you believe you've found a security-sensitive bug in SolFlow itself (for example, a way for a hostile Solidity repository to escape the local visualization sandbox and execute code on the auditor's machine, or to exfiltrate audit code despite the `127.0.0.1`-only bind), please use GitHub's private vulnerability reporting on this repository instead of opening a public issue: <https://github.com/norah1499/solidity-flow-navigator/security/advisories/new>.

SolFlow does not handle vulnerabilities in the Solidity code it visualizes. If you are reporting a vulnerability in a smart contract project, contact that project's maintainers — SolFlow has no role in that disclosure path.
