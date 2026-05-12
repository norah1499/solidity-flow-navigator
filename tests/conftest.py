"""Shared pytest fixtures for the integration tests.

The Solmate test repository lives outside this codebase as a sibling
``test-repos/`` directory (CLAUDE.md). Discovery walks up from this file's
parents to find that sibling, so the fixtures work both from the primary
checkout and from a linked git worktree (e.g. under ``.claude/worktrees/``).

``solmate_facts`` (Layer 1) and ``solmate_flows`` (Layer 2) are
session-scoped — the expensive forge build runs once per pytest session,
and Flow construction runs once on top of it. Both fixtures live here so
either layer's tests find them via pytest's automatic conftest discovery.
"""

from pathlib import Path

import pytest

from solidity_flow_navigator.analysis.compile import compile_repo
from solidity_flow_navigator.analysis.slither_facts import extract_facts
from solidity_flow_navigator.analysis.types import RepoFacts
from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope
from solidity_flow_navigator.flow.types import Flow


def _discover_solmate() -> Path | None:
    """Return the path to the sibling ``test-repos/solmate``, or None.

    Walks several ancestor levels so this works in both the primary
    checkout (sibling at parents[2]) and in linked git worktrees that
    nest the project a few levels deeper (e.g. parents[5] when checked
    out under ``.claude/worktrees/<id>/``).
    """

    here = Path(__file__).resolve()
    for n in range(2, 7):
        if n >= len(here.parents):
            break
        candidate = here.parents[n] / "test-repos" / "solmate"
        if candidate.is_dir():
            return candidate
    return None


SOLMATE_PATH: Path | None = _discover_solmate()


@pytest.fixture(scope="session")
def solmate_facts() -> RepoFacts:
    """Compile Solmate via crytic-compile and extract Layer 1 facts."""

    if SOLMATE_PATH is None:
        pytest.skip(
            "Solmate test repo not present (looked for sibling test-repos/solmate)"
        )
    cc = compile_repo(SOLMATE_PATH)
    return extract_facts(cc, SOLMATE_PATH)


@pytest.fixture(scope="session")
def solmate_flows(solmate_facts: RepoFacts) -> tuple[Flow, ...]:
    """Build one Flow per entry point on the Solmate facts under DEFAULT_SCOPE.

    This is the production-behavior fixture: v0.1's default scope filters
    out ``Mock*`` contracts and anything under ``src/test/**``, so any test
    asserting on those names belongs on ``solmate_flows_unfiltered``
    instead.
    """

    return build_flows(solmate_facts, DEFAULT_SCOPE)


@pytest.fixture(scope="session")
def solmate_flows_unfiltered(solmate_facts: RepoFacts) -> tuple[Flow, ...]:
    """Build flows on the Solmate facts with NO scope filtering applied.

    Used by tests whose subject is content the default scope excludes —
    Mock contracts (``MockERC20``, ``MockOwned``, ...), helpers under
    ``src/test/**``, and the lib/ts-test reach that test contracts produce.
    Equivalent to running ``solflow --no-default-excludes`` with no config
    file.
    """

    return build_flows(solmate_facts, Scope())
