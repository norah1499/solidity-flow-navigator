"""Layer 1 integration test: full pipeline against ../test-repos/solmate.

Compiles Solmate via crytic-compile, runs the Slither fact extractor, and
asserts on the resulting RepoFacts tree. The session-scoped fixture
``solmate_facts`` lives in ``tests/conftest.py`` so both Layer 1 and
Layer 2 tests share one forge build per pytest session.

If the sibling test-repos/solmate directory is not present (e.g. a fresh
clone of just this project), the fixture skips and the whole module is
skipped with it.
"""

from pathlib import Path

from solidity_flow_navigator.analysis.types import Contract, Function, RepoFacts


def _contract_by_name(facts: RepoFacts, name: str) -> Contract:
    for c in facts.contracts:
        if c.name == name:
            return c
    raise AssertionError(f"contract {name!r} not found in repo")


def _function_by_full_name(contract: Contract, full_name: str) -> Function:
    for f in contract.functions:
        if f.full_name == full_name:
            return f
    declared = sorted(fn.full_name for fn in contract.functions)
    raise AssertionError(
        f"function {full_name!r} not declared in {contract.name!r}; "
        f"declared functions: {declared}"
    )


def test_compile_and_extract_succeed(solmate_facts: RepoFacts) -> None:
    """Stage 5 #1: end-to-end pipeline runs without raising."""
    assert isinstance(solmate_facts, RepoFacts)
    assert solmate_facts.repo_path
    assert Path(solmate_facts.repo_path).is_dir()
    assert len(solmate_facts.contracts) > 0


def test_expected_entry_points_exist(solmate_facts: RepoFacts) -> None:
    """Stage 5 #2: named Solmate entry points are present and flagged."""
    expected = [
        ("ERC20", "transferFrom(address,address,uint256)"),
        ("ERC20", "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"),
        ("ERC4626", "deposit(uint256,address)"),
        ("ERC4626", "redeem(uint256,address,address)"),
        ("Owned", "transferOwnership(address)"),
    ]
    for contract_name, fn_full_name in expected:
        c = _contract_by_name(solmate_facts, contract_name)
        f = _function_by_full_name(c, fn_full_name)
        assert (
            f.is_entry_point
        ), f"{contract_name}.{fn_full_name} found but is_entry_point=False"


def test_erc4626_deposit_has_internal_and_library_edges(
    solmate_facts: RepoFacts,
) -> None:
    """Stage 5 #3: ERC4626.deposit has at least one internal and one library edge."""
    erc4626 = _contract_by_name(solmate_facts, "ERC4626")
    deposit = _function_by_full_name(erc4626, "deposit(uint256,address)")
    kinds = [e.kind for e in deposit.calls]
    assert (
        kinds.count("internal") >= 1
    ), f"expected >=1 internal call edge, got kinds={kinds}"
    assert (
        kinds.count("library") >= 1
    ), f"expected >=1 library call edge, got kinds={kinds}"


def test_some_entry_point_has_zero_call_edges(solmate_facts: RepoFacts) -> None:
    """Stage 5 #4: empty calls is valid - guards against silently dropping them.

    ERC20.transfer has zero callable IR edges in Solmate (event emissions and
    storage manipulation do not produce CallEdges).
    """
    erc20 = _contract_by_name(solmate_facts, "ERC20")
    transfer = _function_by_full_name(erc20, "transfer(address,uint256)")
    assert transfer.is_entry_point
    assert len(transfer.calls) == 0, (
        f"expected 0 call edges on ERC20.transfer, got {len(transfer.calls)}: "
        f"{[(e.kind, e.target_function_name) for e in transfer.calls]}"
    )


def test_safetransferlib_assembly_yul_opcodes_preserved(
    solmate_facts: RepoFacts,
) -> None:
    """Stage 5 #5: Yul opcode lifting from inline assembly survives the pipeline.

    SafeTransferLib.safeTransferFrom is a pure-assembly library function;
    Slither lifts each Yul opcode (mstore, mload, gas, call, returndatasize,
    ...) as a SolidityCall IR op, which we map to kind="solidity".
    """
    stl = _contract_by_name(solmate_facts, "SafeTransferLib")
    fn = _function_by_full_name(stl, "safeTransferFrom(ERC20,address,address,uint256)")
    solidity_edges = [e for e in fn.calls if e.kind == "solidity"]
    assert len(solidity_edges) >= 2, (
        f"expected multiple solidity-kind edges from inline assembly, got "
        f"{len(solidity_edges)}; full edge list: "
        f"{[(e.kind, e.target_function_name) for e in fn.calls]}"
    )


def test_owned_modifier_only_owner_extracted(solmate_facts: RepoFacts) -> None:
    """Stage 5 #6: modifier extraction yields onlyOwner on Owned.

    Modifiers are a separate code path from regular functions in Slither.
    """
    owned = _contract_by_name(solmate_facts, "Owned")
    modifier_names = [m.name for m in owned.modifiers]
    assert (
        "onlyOwner" in modifier_names
    ), f"expected onlyOwner modifier on Owned; declared modifiers: {modifier_names}"
    only_owner = next(m for m in owned.modifiers if m.name == "onlyOwner")
    assert only_owner.is_modifier is True


def test_public_state_variable_getter_extracted(solmate_facts: RepoFacts) -> None:
    """Synthetic getters for public state variables surface as Function records.

    ERC20.balanceOf is declared as ``mapping(address => uint256) public
    balanceOf;``. Solidity auto-generates a ``balanceOf(address)`` getter;
    Slither resolves cross-contract calls to that synthetic function. Layer 1
    must surface it in ``Contract.functions`` so Layer 2's lookup by
    canonical_name finds it.
    """
    erc20 = _contract_by_name(solmate_facts, "ERC20")
    getter = _function_by_full_name(erc20, "balanceOf(address)")
    assert getter.canonical_name == "ERC20.balanceOf(address)"
    assert getter.contract_declarer_name == "ERC20"
    assert getter.visibility == "public"
    assert getter.view is True
    assert getter.is_implemented is True
    assert getter.is_entry_point is True
    assert getter.calls == ()


def test_free_functions_extracted(solmate_facts: RepoFacts) -> None:
    """Solidity top-level free functions populate RepoFacts.free_functions.

    Solmate declares free functions in src/test/LibString.t.sol (testing
    helpers) and src/utils/SignedWadMath.sol (math primitives). Both are
    invisible to ``slither.contracts`` and must be extracted via
    ``CompilationUnit.functions_top_level``.
    """
    canonical_names = {f.canonical_name for f in solmate_facts.free_functions}
    available = sorted(canonical_names)
    # Test-file free function (originally surfaced this gap during Layer 2 probing).
    assert (
        "toStringOZ(uint256)" in canonical_names
    ), f"expected toStringOZ(uint256) in free_functions; got {available}"
    # Source-file free function.
    assert (
        "wadMul(int256,int256)" in canonical_names
    ), f"expected wadMul(int256,int256) in free_functions; got {available}"
    # All free functions have empty contract_declarer_name and
    # is_entry_point=False (free functions can't be invoked on a contract).
    for f in solmate_facts.free_functions:
        assert f.contract_declarer_name == "", (
            f"free function {f.canonical_name!r} has non-empty declarer "
            f"{f.contract_declarer_name!r}"
        )
        assert (
            f.is_entry_point is False
        ), f"free function {f.canonical_name!r} marked as entry point"
