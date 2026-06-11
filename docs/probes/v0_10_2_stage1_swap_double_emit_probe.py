"""v0.10.2 Stage 1 probe — diagnose PoolManager._swap double-emit.

Symptom: PoolManager.swap (../test-repos/v4-core) renders two identical
PoolManager._swap nodes from a single call site (PoolManager.sol:206,
``swapDelta = _swap(...)``). The call statement spans lines 206-218 and has
both a struct-literal argument and a ternary argument; both shapes can fork
Slither's CFG and surface the same call as multiple SlithIR ops.

This probe answers, without speculation, where the duplication enters:

  1. Compiles ../test-repos/v4-core via Layer 1 (analysis.compile.compile_repo)
     and builds the per-entry-point Flow tuple via Layer 2
     (analysis.slither_facts.extract_facts + flow.builder.build_flows with a
     default Scope).
  2. Locates the swap-keyed Flow for invoker contract PoolManager. Prints
     swap's root FunctionNode children: index, node_type, target/name,
     call_site_line, source_location. Counts the ``_swap`` children and their
     call_site_line values.
  3. At the Layer 1 boundary, counts the CallEdges in PoolManager.swap.calls
     whose target_function_name is ``_swap``, and prints the full
     source_location of each (so we can see whether the duplicates carry
     identical offset+length, identical line-only mappings, or distinct
     mappings).
  4. At the Slither level (re-runs Slither directly to inspect the raw IR),
     iterates the SlitherFunction.nodes for swap and, for every IR op whose
     target name is ``_swap``, prints the op's source_mapping (start, length,
     lines). Reports how many distinct ``_swap`` InternalCall ops exist and
     whether their source_mappings are identical or distinct.

Run from the project root:

    .venv/bin/python docs/probes/v0_10_2_stage1_swap_double_emit_probe.py

This file is a development artifact; it is intentionally not part of the
pytest suite. Per spec/CLAUDE.md, probes live under docs/probes/ and are
preserved across releases as historical reference.
"""

from __future__ import annotations

import sys
from pathlib import Path

from crytic_compile import CryticCompile
from slither import Slither
from slither.slithir.operations import InternalCall

from solidity_flow_navigator.analysis.compile import compile_repo
from solidity_flow_navigator.analysis.slither_facts import extract_facts
from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import FunctionNode

REPO = Path(__file__).resolve().parents[2] / ".." / "test-repos" / "v4-core"
REPO = REPO.resolve()

TARGET_CONTRACT = "PoolManager"
TARGET_FULL_NAME = "swap(PoolKey,SwapParams,bytes)"
TARGET_CALLEE_NAME = "_swap"


def _hr(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    print(f"Repo: {REPO}")
    if not REPO.is_dir():
        print(f"FAIL: repo path is not a directory: {REPO}", file=sys.stderr)
        return 2

    _hr("Layer 1: compile_repo + extract_facts")
    cc: CryticCompile = compile_repo(REPO)
    facts = extract_facts(cc, REPO)
    print(
        f"  contracts: {len(facts.contracts)}  free_functions: {len(facts.free_functions)}"
    )

    # Locate PoolManager.swap in Layer 1 facts.
    swap_func = None
    for contract in facts.contracts:
        if contract.name != TARGET_CONTRACT:
            continue
        for f in contract.functions:
            if f.full_name == TARGET_FULL_NAME:
                swap_func = f
                break
        if swap_func is not None:
            break
    if swap_func is None:
        print(
            f"FAIL: could not find {TARGET_CONTRACT}.{TARGET_FULL_NAME} in Layer 1 facts",
            file=sys.stderr,
        )
        return 3

    _hr("(c) Layer 1: CallEdges from PoolManager.swap targeting _swap")
    swap_edges = [
        e for e in swap_func.calls if (e.target_function_name == TARGET_CALLEE_NAME)
    ]
    print(f"  total CallEdges in swap.calls:       {len(swap_func.calls)}")
    print(f"  CallEdges targeting _swap:           {len(swap_edges)}")
    for i, e in enumerate(swap_edges):
        sl = e.source_location
        has_offset = sl.start is not None and sl.length is not None
        granularity = "offset+length" if has_offset else "line-only"
        print(
            f"    [{i}] kind={e.kind!r} target={e.target_canonical_name!r}\n"
            f"        is_resolved={e.is_resolved}\n"
            f"        source_location: file={sl.filename_relative}\n"
            f"                         start={sl.start} length={sl.length}\n"
            f"                         lines={sl.lines}\n"
            f"                         starting_column={sl.starting_column}\n"
            f"                         ending_column={sl.ending_column}\n"
            f"                         granularity={granularity}"
        )

    _hr("Layer 2: build_flows + locate swap Flow")
    flows = build_flows(facts, Scope())
    swap_flow = None
    for flow in flows:
        if (
            flow.entry_point_contract_name == TARGET_CONTRACT
            and flow.entry_point_function_name == "swap"
        ):
            swap_flow = flow
            break
    if swap_flow is None:
        print("FAIL: could not locate PoolManager.swap Flow", file=sys.stderr)
        return 4
    print(f"  flows total: {len(flows)}")
    print(
        f"  swap_flow.entry_point_invoker_canonical_name = {swap_flow.entry_point_invoker_canonical_name}"
    )
    print(
        f"  swap_flow.root.canonical_name                = {swap_flow.root.canonical_name}"
    )
    print(f"  swap_flow.root has {len(swap_flow.root.children)} child(ren)")

    _hr("(a) Layer 2 Flow: swap root children")
    swap_children_idxs: list[int] = []
    for idx, child in enumerate(swap_flow.root.children):
        node_type = child.node_type
        if isinstance(child, FunctionNode):
            tgt = f"{child.declarer_contract_name}.{child.full_name}"
            csl = child.call_site_line
            sl = child.source_location
            sl_str = (
                f"start={sl.start} length={sl.length} lines={sl.lines}"
                if sl is not None
                else "<None>"
            )
            print(
                f"  [{idx}] node_type={node_type!r}\n"
                f"        target={tgt}\n"
                f"        name={child.name!r}\n"
                f"        call_site_line={csl}\n"
                f"        source_location: {sl_str}"
            )
            if child.name == TARGET_CALLEE_NAME:
                swap_children_idxs.append(idx)
        else:
            print(f"  [{idx}] node_type={node_type!r} (non-function)  {child}")
    print()
    print(f"  COUNT of '_swap' children:     {len(swap_children_idxs)}")
    print(
        "  call_site_line values of '_swap' children: "
        + str(
            [
                swap_flow.root.children[i].call_site_line  # type: ignore[union-attr]
                for i in swap_children_idxs
            ]
        )
    )

    _hr("(d) Slither raw IR: _swap InternalCall ops inside swap.nodes")
    slither = Slither(cc)
    sl_swap = None
    for sl_contract in slither.contracts:
        if sl_contract.name != TARGET_CONTRACT:
            continue
        for sl_fn in sl_contract.functions_declared:
            if sl_fn.full_name == TARGET_FULL_NAME:
                sl_swap = sl_fn
                break
        if sl_swap is not None:
            break
    if sl_swap is None:
        print("FAIL: could not locate swap in Slither", file=sys.stderr)
        return 5

    raw_ops: list[tuple[int, int, object]] = []  # (cfg_node_index, ir_index, op)
    for n_idx, node in enumerate(sl_swap.nodes):
        for ir_idx, op in enumerate(node.irs):
            tgt_name = None
            tgt_fn = getattr(op, "function", None)
            if tgt_fn is not None:
                tgt_name = getattr(tgt_fn, "name", None)
            if tgt_name is None:
                tgt_name = getattr(op, "function_name", None)
                if tgt_name is not None:
                    tgt_name = str(tgt_name)
            if tgt_name == TARGET_CALLEE_NAME:
                raw_ops.append((n_idx, ir_idx, op))

    print(
        f"  total CFG nodes in swap:                              {len(sl_swap.nodes)}"
    )
    print(f"  IR ops across all CFG nodes whose target is '_swap':  {len(raw_ops)}")
    internal_call_count = sum(1 for _, _, op in raw_ops if isinstance(op, InternalCall))
    print(
        f"    of which InternalCall instances:                    {internal_call_count}"
    )

    seen_keys: set[tuple] = set()
    for k, (n_idx, ir_idx, op) in enumerate(raw_ops):
        sm = op.node.source_mapping
        op_cls = type(op).__name__
        key = (sm.start, sm.length, tuple(sm.lines))
        seen_keys.add(key)
        print(
            f"  [{k}] cfg_node={n_idx} ir_idx={ir_idx} op_class={op_cls}\n"
            f"      cfg_node.type={op.node.type}\n"
            f"      op.node.source_mapping.start={sm.start}  length={sm.length}\n"
            f"      op.node.source_mapping.lines={tuple(sm.lines)}\n"
            f"      op.node.source_mapping.starting_column={sm.starting_column}\n"
            f"      op.node.source_mapping.ending_column={sm.ending_column}"
        )

    _hr("SUMMARY")
    print(f"(a) Flow _swap child count   : {len(swap_children_idxs)}")
    print(
        "    call_site_lines           : "
        + str(
            [
                swap_flow.root.children[i].call_site_line  # type: ignore[union-attr]
                for i in swap_children_idxs
            ]
        )
    )
    print(f"(c) Layer 1 _swap CallEdge cnt: {len(swap_edges)}")
    if swap_edges:
        e0 = swap_edges[0]
        has_offset = (
            e0.source_location.start is not None
            and e0.source_location.length is not None
        )
        print(
            f"    source_location granularity: {'offset+length' if has_offset else 'line-only'}"
        )
        all_identical = all(
            (
                e.source_location.start,
                e.source_location.length,
                tuple(e.source_location.lines),
            )
            == (
                e0.source_location.start,
                e0.source_location.length,
                tuple(e0.source_location.lines),
            )
            for e in swap_edges
        )
        print("    all CallEdge source_locations identical: " f"{all_identical}")
    print(f"(d) Slither _swap op total   : {len(raw_ops)}")
    print(f"    of which InternalCall    : {internal_call_count}")
    print(f"    distinct source_mappings : {len(seen_keys)}")
    print(
        "    mappings identical across ops: "
        f"{len(seen_keys) == 1 and len(raw_ops) > 1}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
