"""Layer 1 unit tests for the Slither fact extractor (analysis/slither_facts.py).

These run without the Solmate integration fixture: ``Slither`` is replaced by
a minimal stub, so the tests can exercise ``extract_facts`` mechanics that a
single-compilation-unit repo (Foundry, i.e. the whole integration test folder)
can never reach. Regression coverage for the v0.25.2 contract de-dup (spec
§11.5, "Contract de-duplication across compilation units"): Hardhat-style
builds hand Slither one compilation unit per source file, so
``slither.contracts`` yields the same contract once per unit that includes it,
and every duplicate passed through built an identical Flow per entry point.
"""

from pathlib import Path
from types import SimpleNamespace

from solidity_flow_navigator.analysis import slither_facts
from solidity_flow_navigator.analysis.types import RepoFacts


def _source_mapping(absolute: str) -> SimpleNamespace:
    """The attribute subset of a Slither source mapping ``_source_location`` reads."""

    return SimpleNamespace(
        filename=SimpleNamespace(absolute=absolute),
        start=0,
        length=0,
        lines=(1,),
        starting_column=1,
        ending_column=1,
    )


def _stub_contract(
    name: str, absolute_path: str, *, is_abstract: bool = False
) -> SimpleNamespace:
    """A minimal object satisfying every contract attribute ``extract_facts`` reads."""

    return SimpleNamespace(
        name=name,
        contract_kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=is_abstract,
        inheritance=[],
        immediate_inheritance=[],
        functions_declared=[],
        state_variables_declared=[],
        modifiers_declared=[],
        structures_declared=[],
        enums_declared=[],
        source_mapping=_source_mapping(absolute_path),
    )


def _extract(monkeypatch, repo_root: Path, contracts: list) -> RepoFacts:
    """Run ``extract_facts`` against a stub Slither exposing ``contracts``."""

    stub = SimpleNamespace(contracts=contracts, compilation_units=[])
    monkeypatch.setattr(slither_facts, "Slither", lambda _cc: stub)
    return slither_facts.extract_facts(object(), repo_root)


def test_same_contract_repeated_across_units_collapses_keep_first(
    monkeypatch, tmp_path
):
    repo = tmp_path.resolve()
    pool = str(repo / "contracts" / "Pool.sol")
    # Real cross-unit duplicates are byte-identical; the differing is_abstract
    # flag is a marker to observe WHICH copy survives, not a realistic shape.
    first = _stub_contract("Pool", pool, is_abstract=True)
    duplicates = [_stub_contract("Pool", pool) for _ in range(2)]

    facts = _extract(monkeypatch, repo, [first, *duplicates])

    assert len(facts.contracts) == 1
    kept = facts.contracts[0]
    assert kept.uid == ("contracts/Pool.sol", "Pool")
    assert kept.is_abstract is True


def test_distinct_same_name_contracts_in_different_files_both_survive(
    monkeypatch, tmp_path
):
    # The v0.25.0 name-collision case (two files each declaring IERC20) must
    # NOT be collapsed by the de-dup: identity is the uid, never the bare name.
    repo = tmp_path.resolve()
    a = _stub_contract("IERC20", str(repo / "a" / "IERC20.sol"))
    b = _stub_contract("IERC20", str(repo / "b" / "IERC20.sol"))

    facts = _extract(monkeypatch, repo, [a, b])

    assert [c.uid for c in facts.contracts] == [
        ("a/IERC20.sol", "IERC20"),
        ("b/IERC20.sol", "IERC20"),
    ]
