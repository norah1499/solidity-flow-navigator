"""Smoke tests for the Flask app — Layer 3 routes and static assets.

We use the Flask test client (no real server, no port binding) on top of
the session-scoped solmate fixtures from ``tests/conftest.py``. The tests
cover the v0 success-criteria checks the spec calls out:

  * ``GET /`` returns 200 with the three index group headings.
  * ``GET /flow/<known url_id>`` returns 200 and embeds ``<script id="flow-data">``.
  * ``GET /flow/<unknown>`` returns 404.
  * Static assets (main.css, generated pygments.css, vendored dagre and d3)
    all return 200.

We don't try to run flow-progressive.js in a headless browser here — the
Stage 1b / 1c Playwright probes under ``docs/probes/`` cover the
rendering behavior we'd otherwise want to assert on JS-rendered geometry.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from flask.testing import FlaskClient

from solidity_flow_navigator.analysis.types import RepoFacts
from solidity_flow_navigator.flow.types import Flow
from solidity_flow_navigator.serve.app import build_index, create_app


@pytest.fixture(scope="module")
def client(solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]) -> FlaskClient:
    """Build the app once per module; reuse a single test client across tests."""
    app = create_app(solmate_facts, solmate_flows)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(scope="module")
def client_unfiltered(
    solmate_facts: RepoFacts, solmate_flows_unfiltered: tuple[Flow, ...]
) -> FlaskClient:
    """Variant of ``client`` built from ``solmate_flows_unfiltered``.

    Used by tests whose subject is content the default scope filters out
    (Mock contracts, the Tests group's existence, ``src/test/**`` flows).
    Equivalent to running ``solflow`` against a config that clears the
    built-in path and contract excludes (``exclude_paths = []`` and
    ``exclude_contracts = []`` in ``solflow.toml``) — the v0.10.0
    Stage 2 replacement for the retired ``--no-default-excludes`` flag.
    """
    app = create_app(solmate_facts, solmate_flows_unfiltered)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(scope="module")
def client_expand_all(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> FlaskClient:
    """Variant of ``client`` built with ``expand_all=True``.

    Equivalent to running ``solflow --expand-all``: per-Flow pages still
    use the progressive renderer, but the embedded ``data-expand-all``
    attribute on ``#flow-data`` flips to ``"true"`` so the renderer
    expands the full call tree at page load. See spec §10.2 "Full
    expansion".
    """
    app = create_app(solmate_facts, solmate_flows, expand_all=True)
    app.config.update(TESTING=True)
    return app.test_client()


# -- index ------------------------------------------------------------------


def test_index_status_and_groups(client_unfiltered: FlaskClient) -> None:
    """All three group headings render when content exists in each.

    Uses the unfiltered client because the ``Tests`` group is populated
    only by ``src/test/**`` and ``Mock*`` content — both excluded by
    default scope, which would omit the heading entirely (the index
    template skips groups with zero contracts).
    """
    rv = client_unfiltered.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert ">Application<" in body
    assert ">Tests<" in body
    assert ">Dependencies<" in body


def test_index_lists_solmate_application_contracts(client: FlaskClient) -> None:
    """Sanity: a few canonical Solmate contracts appear in the rendered index."""
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    # Application contracts (real protocol code, not under src/test/ or lib/)
    for name in ("ERC20", "ERC4626", "Owned", "WETH"):
        assert (
            f">{name}\n" in body or f">{name}<" in body or f"  {name}\n" in body
        ), f"contract {name} missing from index"


# -- index sub-grouping (spec §8.3): Mutating vs Read-only -------------------


def _extract_contract_block(body: str, contract_name: str) -> str:
    """Return the substring of ``body`` covering one contract's ``<article>``.

    The index template emits one ``<article class="contract-block">`` per
    contract. We locate the article whose ``<h3>`` opens with ``contract_name``
    followed by a newline (the template formatting), then slice to the next
    ``</article>``. Avoids pulling in an HTML parser for what is a simple
    boundary problem at this scale.
    """
    needle = '<article class="contract-block">'
    start = 0
    while True:
        article_start = body.find(needle, start)
        assert article_start != -1, f"no contract block found for {contract_name}"
        article_end = body.find("</article>", article_start)
        assert article_end != -1, "unterminated <article> in rendered index"
        block = body[article_start:article_end]
        # The <h3> renders the name followed by a newline before the path span.
        if f">\n        {contract_name}\n" in block or f">{contract_name}\n" in block:
            return block
        start = article_end


def test_index_sub_groups_both_sections_for_mixed_contract(
    client: FlaskClient,
) -> None:
    """Spec §8.3: a contract with both mutating and read-only entry points
    renders both section headings inside its ``<article>``.

    ERC4626 is the canonical mixed case in Solmate: ``deposit``/``mint``/
    ``withdraw``/``redeem``/``transfer``/``approve``/``permit``/
    ``transferFrom`` are mutating; ``previewX``, ``convertToX``, ``maxX``,
    ``totalAssets``, ``DOMAIN_SEPARATOR``, etc. are view.
    """
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    block = _extract_contract_block(body, "ERC4626")
    # v0.8.0: section headings now carry a "· N" count suffix per spec §8.3,
    # so the literal `>Mutating<` shape no longer appears.
    assert ">Mutating · " in block, "Mutating section missing from ERC4626 block"
    assert ">Read-only · " in block, "Read-only section missing from ERC4626 block"


def test_index_omits_empty_section_for_single_kind_contract(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3: sections containing zero entry points are omitted entirely
    (no ``Read-only`` placeholder header on a contract with only mutating
    entry points, and vice versa).

    Discovered dynamically rather than hardcoded: most Solmate contracts have
    a public-state-variable getter that auto-creates a view entry point, so a
    naive "pick a contract you think is mutating-only" can become mixed if
    Solmate's source shifts. Walking ``build_index``'s output is the
    authoritative source of truth.
    """
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    single_kind: tuple[str, str] | None = None  # (contract_name, present_section)
    for group in groups:
        for contract in group.contracts:
            m = len(contract.mutating_entry_points)
            r = len(contract.read_only_entry_points)
            if m and not r:
                single_kind = (contract.name, "Mutating")
                break
            if r and not m:
                single_kind = (contract.name, "Read-only")
                break
        if single_kind is not None:
            break
    assert single_kind is not None, (
        "expected at least one Solmate contract with a single-kind entry-point "
        "list; index data shape may have changed"
    )
    contract_name, present = single_kind
    absent = "Read-only" if present == "Mutating" else "Mutating"

    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    block = _extract_contract_block(body, contract_name)
    # v0.8.0: section heading is `<heading>Present · N</heading>`; check both
    # the leading marker and the section-count suffix together.
    assert (
        f">{present} · " in block
    ), f"{present} section missing from {contract_name} block"
    assert (
        f">{absent} · " not in block
    ), f"{absent} placeholder header should be omitted for {contract_name}"


# -- index signature highlighting (v0.7.0, spec §8.3) -----------------------


def test_index_entry_signatures_render_through_pygments(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3 (v0.7.0): each index entry's signature lands under a
    ``<code class="src">`` and contains Pygments token spans.

    Probes the rendered HTML for one known mutating entry (``ERC4626.deposit``)
    and one known read-only entry (``ERC4626.totalAssets``) to confirm:
      * the ``<code class="src">`` wrapper is present so the existing
        ``.src .X`` palette in ``main.css`` colours the signature;
      * the function name lands in the ``.nv`` token slot (purple per
        ``--tok-name``);
      * the synthetic ``function`` keyword and ``external`` modifier land in
        the ``.kt`` token slot (dark blue per ``--tok-keyword``);
      * mutating vs read-only entries render with the expected mutability
        suffix (``external`` vs ``external view``).
    """
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)

    erc4626_block = _extract_contract_block(body, "ERC4626")

    # The wrapper class is what activates the existing .src .X palette.
    assert '<code class="src">' in erc4626_block

    # Function names in the .nv slot (purple), one for each entry rendered
    # via the Pygments pipeline. Solmate's ERC4626 has many entries; we just
    # need a sample of each kind.
    assert (
        '<span class="nv">deposit</span>' in erc4626_block
    ), "mutating entry-point function name should land in the .nv token slot"
    assert (
        '<span class="nv">totalAssets</span>' in erc4626_block
    ), "read-only entry-point function name should land in the .nv token slot"

    # The synthetic `function` keyword lexes as .kt under the Solidity lexer.
    assert '<span class="kt">function</span>' in erc4626_block

    # Mutability suffix differs by section: mutating → `external`,
    # read-only → `external view`. We search the whole body rather than
    # the ERC4626 block alone to allow either to land in either; the
    # presence of both strings somewhere in the index is the contract.
    assert '<span class="kt">external</span>' in body
    # The literal `view` text appears in read-only entries; the Solidity
    # lexer leaves it as plain text rather than tagging it as a keyword,
    # so we look for the bare substring after `external`.
    assert 'external</span><span class="w"> </span>view' in body


def test_index_signature_html_set_on_entry_points(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Sanity: every ``_EntryPointEntry`` carries a non-empty ``signature_html``
    that contains the function name inside a ``.nv`` span.

    Walks ``build_index``'s output directly rather than scraping HTML, so a
    template refactor can't silently mask a regression in the underlying
    data shape.
    """
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    seen_at_least_one = False
    for group in groups:
        for contract in group.contracts:
            for ep in (
                *contract.mutating_entry_points,
                *contract.read_only_entry_points,
            ):
                seen_at_least_one = True
                # Function name is the part of full_name before the first `(`.
                name = ep.function_full_name.split("(", 1)[0]
                if not name:
                    # receive/fallback / no-paren shapes — skip name assert.
                    continue
                assert (
                    ep.signature_html
                ), f"signature_html empty for {ep.contract_name}.{name}"
                assert f'<span class="nv">{name}</span>' in ep.signature_html, (
                    f"function name {name!r} missing from .nv slot in "
                    f"{ep.signature_html!r}"
                )
    assert seen_at_least_one, "no entry points walked; fixture may be broken"


# -- v0.8.0 index metadata (data shape) -------------------------------------


def test_build_index_returns_total_unresolved(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """build_index returns ``(groups, total_eps, total_contracts, total_unresolved)``.

    ``total_unresolved`` is the sum of ``Flow.unresolved_count`` across every
    Flow in the input — the v0.8.0 header "trust budget" figure (spec §8.3).
    """
    groups, _, _, total_unresolved = build_index(solmate_facts, solmate_flows)
    expected = sum(f.unresolved_count for f in solmate_flows)
    assert total_unresolved == expected
    # Sanity: Solmate has Unresolved descendants somewhere (low-level calls in
    # SafeTransferLib, Yul dynamic dispatch in CREATE3/SSTORE2). If this is 0
    # the fixture has drifted in a way that hides the v0.8.0 metric.
    assert total_unresolved > 0
    # Every group's entry points should expose unresolved_count and max_depth.
    saw_one = False
    for group in groups:
        for contract in group.contracts:
            for ep in (
                *contract.mutating_entry_points,
                *contract.read_only_entry_points,
            ):
                saw_one = True
                assert ep.unresolved_count >= 0
                assert ep.max_depth >= 0
    assert saw_one


def test_build_index_contract_entry_section_counts(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Each ``_ContractEntry`` exposes ``mutating_count`` and ``read_only_count``
    equal to the length of the corresponding entry-point tuple."""
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    for group in groups:
        for contract in group.contracts:
            assert contract.mutating_count == len(contract.mutating_entry_points)
            assert contract.read_only_count == len(contract.read_only_entry_points)


def test_build_index_entry_metadata_matches_source_flow(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Each ``_EntryPointEntry`` carries the unresolved_count and max_depth
    of its source Flow."""
    by_url_id = {f.entry_point_invoker_canonical_name: f for f in solmate_flows}
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    saw_one = False
    for group in groups:
        for contract in group.contracts:
            for ep in (
                *contract.mutating_entry_points,
                *contract.read_only_entry_points,
            ):
                src = by_url_id[ep.url_id]
                assert ep.unresolved_count == src.unresolved_count
                assert ep.max_depth == src.max_depth
                saw_one = True
    assert saw_one


def test_index_route_passes_scope_and_total_unresolved(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """The ``/`` route exposes ``total_unresolved`` and the active ``Scope``
    to the template's render context.

    We probe the context via Flask's ``context_processor`` mechanism: a
    test-installed processor captures the keys it sees, then the test
    asserts on that capture. This keeps the test focused on data-flow
    plumbing (Stage 2's contract) rather than on rendered HTML (Stage 3's).
    """
    from solidity_flow_navigator.flow.scope import Scope

    custom_scope = Scope(
        exclude_paths=("**/*.t.sol",),
        exclude_contracts=("*Mock*",),
        inline_libraries=(),
        stub_paths=("src/utils/dense/**",),
    )
    app = create_app(solmate_facts, solmate_flows, scope=custom_scope)
    app.config.update(TESTING=True)

    captured: dict[str, object] = {}

    @app.context_processor
    def _capture() -> dict[str, object]:
        # No-op processor; capture happens via before_render hook below.
        return {}

    # Flask doesn't have a "before_render" hook; the cleanest way to peek at
    # what the route passed is to monkeypatch render_template inside this
    # module. Done locally to keep the rest of the suite untouched.
    import solidity_flow_navigator.serve.app as app_mod

    original = app_mod.render_template

    def _spy(template_name: str, **ctx: object) -> str:
        captured.update(ctx)
        return original(template_name, **ctx)

    app_mod.render_template = _spy
    try:
        rv = app.test_client().get("/")
    finally:
        app_mod.render_template = original

    assert rv.status_code == 200
    assert "total_unresolved" in captured
    assert isinstance(captured["total_unresolved"], int)
    assert "scope" in captured
    assert captured["scope"] is custom_scope


# -- v0.8.0 index rendering (Stage 3 HTML output) ---------------------------


def test_index_header_renders_three_counts(client: FlaskClient) -> None:
    """Spec §8.3 index header block: three right-aligned counts with the
    numeric values in default text color and labels in --fg-muted chrome.
    We assert each count is present in the rendered HTML alongside its
    label. Exact numeric values come from the fixture state — we only
    check that each ``.count-value`` / ``.count-label`` pair exists.
    """
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    # Header block present with the three labels in the canonical order.
    assert '<header class="index-header">' in body
    assert '<span class="count-label">contracts</span>' in body
    assert '<span class="count-label">entry points</span>' in body
    assert '<span class="count-label">unresolved</span>' in body


def test_index_drops_repo_path_subtitle_and_contract_prefix(
    client: FlaskClient,
) -> None:
    """v0.11.0 (spec §8.3): the repo path renders once (site header chrome) —
    the index-local subtitle is gone — and entry rows drop the `Contract.`
    prefix (the contract block heading already names the contract)."""
    body = client.get("/").get_data(as_text=True)
    assert 'class="index-subtitle"' not in body, (
        "the index-local repo-path subtitle must be gone (v0.11.0 — it "
        "duplicated the site header's path)."
    )
    # The declaration-form signature now starts the link content directly:
    # `<code class="src">function transfer(...)`, never `Owned.function ...`.
    assert (
        re.search(r'<code class="src">\s*Owned\.', body) is None
    ), "entry rows must not carry the Contract. prefix (v0.11.0 §8.3)."
    assert "function transferOwnership" in re.sub(r"<[^>]+>", "", body), (
        "declaration-form signatures must still render after the prefix " "removal."
    )


def test_index_singular_count_labels() -> None:
    """v0.11.0: count labels pluralize with their values — a one-contract,
    one-entry-point repo reads '1 contract · 1 entry point', not
    '1 contracts'. Built from a minimal synthetic repo so the singular
    branch is actually exercised (the Solmate fixture is always plural)."""
    from solidity_flow_navigator.analysis.types import (
        Contract,
        Function,
        SourceLocation,
    )
    from solidity_flow_navigator.flow.builder import build_flows
    from solidity_flow_navigator.flow.scope import Scope

    sl = SourceLocation(
        filename_absolute="/abs/src/App.sol",
        filename_relative="src/App.sol",
        start=0,
        length=1,
        lines=(1,),
        starting_column=1,
        ending_column=2,
    )
    entry = Function(
        canonical_name="App.go()",
        name="go",
        full_name="go()",
        contract_declarer_name="App",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=sl,
        source_code="function go() external {}",
        calls=(),
    )
    app_contract = Contract(
        name="App",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=sl,
        functions=(entry,),
        modifiers=(),
    )
    facts = RepoFacts(repo_path="/abs", contracts=(app_contract,), free_functions=())
    flows = build_flows(facts, Scope())
    assert len(flows) == 1
    app = create_app(facts, flows)
    app.config.update(TESTING=True)
    body = app.test_client().get("/").get_data(as_text=True)
    assert (
        '<span class="count-label">contract</span>' in body
    ), "singular count must read 'contract' (v0.11.0 pluralization)."
    assert (
        '<span class="count-label">entry point</span>' in body
    ), "singular count must read 'entry point' (v0.11.0 pluralization)."
    assert "1 contracts" not in body and "1 entry points" not in body


def test_index_modifier_chips_on_mutating_rows_only(client: FlaskClient) -> None:
    """v0.11.0 (spec §8.3): mutating rows render one chip per root modifier
    (Owned.transferOwnership → onlyOwner); read-only sections render no
    chips — the deliberate asymmetry where a chipless mutating row IS the
    unprotected-entry-point signal."""
    body = client.get("/").get_data(as_text=True)
    owned_block = _extract_contract_block(body, "Owned")
    assert "entry-mod-chip" in owned_block, (
        "Owned's mutating rows must carry modifier chips (transferOwnership "
        "has onlyOwner)."
    )
    assert (
        ">onlyOwner</span>" in owned_block
    ), "the onlyOwner chip must carry the modifier's name."
    # No chips inside any Read-only section, anywhere on the page.
    for section_start in [m.start() for m in re.finditer(r">Read-only · ", body)]:
        section_end = body.find("</section>", section_start)
        assert "entry-mod-chip" not in body[section_start:section_end], (
            "read-only rows must not render modifier chips (v0.11.0 §8.3 "
            "deliberate asymmetry)."
        )


def test_main_css_unresolved_badge_is_chip(client: FlaskClient) -> None:
    """v0.11.0: `N unr` renders as a chip (fill + border in the
    --node-unresolved-* family), not bare colored text."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    start = css.find(".meta-unresolved {")
    block = css[start : css.find("}", start)]
    assert (
        "background: var(--node-unresolved-bg)" in block
    ), "meta-unresolved must carry the unresolved family fill (v0.11.0)."
    assert (
        "border: 0.5px solid var(--node-unresolved-border)" in block
    ), "meta-unresolved must carry the unresolved family border (v0.11.0)."


def test_index_header_counts_match_build_index_totals(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """The numeric values in the header match the totals build_index
    returned. Walks build_index directly to avoid pinning concrete numbers
    that drift with Solmate."""
    _, total_eps, total_contracts, total_unresolved = build_index(
        solmate_facts, solmate_flows
    )
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    assert (
        f'<span class="count-value">{total_contracts}</span>' in body
    ), f"contracts count {total_contracts} missing from header"
    assert (
        f'<span class="count-value">{total_eps}</span>' in body
    ), f"entry points count {total_eps} missing from header"
    assert (
        f'<span class="count-value">{total_unresolved}</span>' in body
    ), f"unresolved count {total_unresolved} missing from header"


def test_index_scope_line_renders_excluded_and_stub_paths(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Scope summary line renders excluded path globs from the active Scope
    and the stub paths list, with the glob values themselves visible as
    ``<code class="scope-glob">`` so the CSS can color them default-on-muted
    (spec §8.3)."""
    from solidity_flow_navigator.flow.scope import Scope

    scope = Scope(
        exclude_paths=("**/*.t.sol", "**/mocks/**"),
        exclude_contracts=(),
        inline_libraries=(),
        stub_paths=("src/utils/dense/**",),
    )
    app = create_app(solmate_facts, solmate_flows, scope=scope)
    app.config.update(TESTING=True)
    rv = app.test_client().get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)

    # The literal "Scope" prefix appears in muted chrome.
    assert '<span class="scope-chrome">Scope</span>' in body
    # Both exclude globs render as scope-glob elements.
    assert '<code class="scope-glob">**/*.t.sol</code>' in body
    assert '<code class="scope-glob">**/mocks/**</code>' in body
    # Stub path renders as scope-glob too.
    assert '<code class="scope-glob">src/utils/dense/**</code>' in body
    # When stubs are configured, the "none" sentinel must NOT appear.
    assert '<span class="scope-none">none</span>' not in body


def test_index_scope_line_renders_none_when_no_stubs(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3: when no stubs are active, the literal word ``none``
    renders in default text color in place of the stub list."""
    from solidity_flow_navigator.flow.scope import Scope

    scope = Scope(
        exclude_paths=("**/*.t.sol",),
        exclude_contracts=(),
        inline_libraries=(),
        stub_paths=(),
    )
    app = create_app(solmate_facts, solmate_flows, scope=scope)
    app.config.update(TESTING=True)
    rv = app.test_client().get("/")
    body = rv.get_data(as_text=True)
    # The "none" sentinel is wrapped in .scope-none so the CSS can paint it
    # in default text color rather than the surrounding muted chrome.
    assert '<span class="scope-none">none</span>' in body


def test_index_section_count_suffix_matches_entry_count(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3: each ``Mutating`` and ``Read-only`` section heading carries
    a count suffix in the form ``Mutating · 14`` matching that section's
    entry-point cardinality. Walks build_index for the authoritative counts
    and verifies the rendered headings include them."""
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    asserted_at_least_one_of_each = {"Mutating": False, "Read-only": False}
    for group in groups:
        for contract in group.contracts:
            if contract.mutating_count:
                expected = f"Mutating · {contract.mutating_count}"
                assert expected in body, (
                    f"section heading {expected!r} missing for " f"{contract.name}"
                )
                asserted_at_least_one_of_each["Mutating"] = True
            if contract.read_only_count:
                expected = f"Read-only · {contract.read_only_count}"
                assert expected in body, (
                    f"section heading {expected!r} missing for " f"{contract.name}"
                )
                asserted_at_least_one_of_each["Read-only"] = True
    assert all(asserted_at_least_one_of_each.values()), (
        "fixture exercised only one bucket; coverage of the section count "
        "suffix is incomplete"
    )


def test_index_per_entry_metadata_renders_in_all_four_cases(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3 per-entry metadata column. For each bucket (mutating /
    read-only) and each branch (unresolved-present / unresolved-absent),
    assert the rendered HTML matches the rule:

      - unresolved_count > 0 → ``N unr · dN``
      - unresolved_count == 0 → ``dN``

    Symmetric across mutability. Walks every entry in build_index's output
    so the rule is verified at scale, not just on cherry-picked cases."""
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    rv = client.get("/")
    body = rv.get_data(as_text=True)

    covered = {
        ("mutating", "with_unr"): False,
        ("mutating", "without_unr"): False,
        ("read-only", "with_unr"): False,
        ("read-only", "without_unr"): False,
    }
    for group in groups:
        for contract in group.contracts:
            for bucket_label, entries in (
                ("mutating", contract.mutating_entry_points),
                ("read-only", contract.read_only_entry_points),
            ):
                for ep in entries:
                    # v0.10.4: badges carry a title tooltip between the class
                    # and the closing bracket — match through it.
                    depth_tag = (
                        f'<span class="meta-depth" '
                        f'title="max call depth {ep.max_depth}">'
                        f"d{ep.max_depth}</span>"
                    )
                    assert (
                        depth_tag in body
                    ), f"depth tag {depth_tag!r} missing for {ep.url_id}"
                    if ep.unresolved_count > 0:
                        unr_tag = (
                            f'<span class="meta-unresolved" '
                            f'title="{ep.unresolved_count} unresolved call '
                            f'site(s) in this flow — rendered as red pills">'
                            f"{ep.unresolved_count} unr</span>"
                        )
                        assert (
                            unr_tag in body
                        ), f"unresolved tag {unr_tag!r} missing for {ep.url_id}"
                        covered[(bucket_label, "with_unr")] = True
                    else:
                        covered[(bucket_label, "without_unr")] = True
    missing = [case for case, hit in covered.items() if not hit]
    assert not missing, (
        f"fixture did not exercise these (bucket, unr-state) cases: {missing}; "
        f"the symmetric-across-mutability rule cannot be fully validated"
    )


def test_index_per_entry_metadata_omits_unresolved_when_zero(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Negative-case sanity for the previous test: entries with
    ``unresolved_count == 0`` must NOT render a ``0 unr`` annotation
    anywhere (the conditional in the template is ``> 0``, not ``>= 0``)."""
    # The literal string "0 unr" wrapped in .meta-unresolved should never
    # appear — that would indicate the conditional flipped.
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    assert '<span class="meta-unresolved">0 unr</span>' not in body


def test_index_privacy_footer_present(client: FlaskClient) -> None:
    """Spec §8.3: privacy footer at the bottom of the page documents the
    localhost-only execution model AND links to the public repository so a
    party running a hosted instance can reach the AGPL-3.0 Corresponding
    Source (section 13 of the license). Exact text is required so the auditor
    sees the canonical assurance phrasing on every render."""
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    assert '<p class="privacy-footer">' in body
    assert (
        "All analysis local · code never uploaded · server bound to 127.0.0.1 · "
        in body
    )
    assert (
        '<a class="source-link" href="https://github.com/norah1499/solidity-flow-navigator">source</a>'
        in body
    )


# -- flow page --------------------------------------------------------------


def test_flow_page_known_url_id(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    rv = client.get(f"/flow/{target.entry_point_invoker_canonical_name}")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    # The embedded JSON the frontend reads.
    assert '<script type="application/json" id="flow-data"' in body
    # Vendored libs and frontend script tags.
    assert "/static/vendor/dagre.min.js" in body
    assert "/static/vendor/d3.min.js" in body
    # v0.10.0 Stage 2: the progressive renderer is the only renderer.
    # ``--legacy`` and ``flow.js`` are gone; both the script tag and the
    # static-file path are pinned absent.
    assert "/static/js/flow-progressive.js" in body
    assert "flow.js" not in body
    # Graph container the JS targets.
    assert 'id="graph-frame"' in body


def test_flow_page_default_carries_expand_all_false(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """The default app (no ``--expand-all``) emits
    ``data-expand-all="false"`` on the ``#flow-data`` script tag, so the
    progressive renderer takes the root-only initial-render path.

    Pins the default-path-preserved invariant: v0.10.0 Stage 1 must not
    change behavior when the flag is off.
    """
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    rv = client.get(f"/flow/{target.entry_point_invoker_canonical_name}")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert 'data-expand-all="false"' in body
    assert 'data-expand-all="true"' not in body
    # Progressive renderer is the only renderer post-v0.10.0 Stage 2.
    assert "/static/js/flow-progressive.js" in body
    assert "flow.js" not in body


def test_flow_page_expand_all_propagates_to_data_attribute(
    client_expand_all: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """``solflow --expand-all`` propagates to the rendered Flow page as
    ``data-expand-all="true"`` on the ``#flow-data`` script tag.

    The renderer reads that attribute on init and switches to the
    full-tree initial expansion. Same progressive renderer, same script
    tag — only the embedded flag differs. The Flow JSON itself is
    unchanged.
    """
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    rv = client_expand_all.get(f"/flow/{target.entry_point_invoker_canonical_name}")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert 'data-expand-all="true"' in body
    assert 'data-expand-all="false"' not in body
    # Progressive renderer is the only renderer post-v0.10.0 Stage 2;
    # ``--expand-all`` is a flag on it, not a renderer swap.
    assert "/static/js/flow-progressive.js" in body
    assert "flow.js" not in body
    # Same flow-data script structure as without the flag.
    assert '<script type="application/json" id="flow-data"' in body


def test_flow_page_inherited_url_does_not_collide(
    client_unfiltered: FlaskClient, solmate_flows_unfiltered: tuple[Flow, ...]
) -> None:
    """MockOwned and Owned each get their own URL: Layer 2's
    ``entry_point_invoker_canonical_name`` is keyed on the invoking contract,
    so the two flows carry distinct canonicals
    (``MockOwned.transferOwnership(...)`` vs ``Owned.transferOwnership(...)``)
    and route independently.

    Uses the unfiltered fixture pair because ``MockOwned`` is filtered by
    default scope's ``Mock*`` rule.
    """
    bare = next(
        f
        for f in solmate_flows_unfiltered
        if f.entry_point_contract_name == "Owned"
        and f.entry_point_function_name == "transferOwnership"
    )
    inherited = next(
        f
        for f in solmate_flows_unfiltered
        if f.entry_point_contract_name == "MockOwned"
        and f.entry_point_function_name == "transferOwnership"
    )
    assert (
        bare.entry_point_invoker_canonical_name
        != inherited.entry_point_invoker_canonical_name
    )
    bare_rv = client_unfiltered.get(f"/flow/{bare.entry_point_invoker_canonical_name}")
    inh_rv = client_unfiltered.get(
        f"/flow/{inherited.entry_point_invoker_canonical_name}"
    )
    assert bare_rv.status_code == 200
    assert inh_rv.status_code == 200
    # The inherited flow's page header carries the subtitle.
    assert "Inherited entry point" in inh_rv.get_data(as_text=True)
    assert "Inherited entry point" not in bare_rv.get_data(as_text=True)


_FLOW_DATA_RE = re.compile(
    r'<script type="application/json" id="flow-data"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _undefang(s: str) -> str:
    """Reverse ``serve.app._safe_json``'s defang substitutions."""
    return (
        s.replace("<\\/", "</")
        .replace("<\\!--", "<!--")
        .replace("<\\script", "<script")
    )


def test_flow_page_routes_colliding_canonicals_distinctly(
    client_unfiltered: FlaskClient,
) -> None:
    """Regression: previously-colliding ``Owned.transferOwnership(address)``
    and ``MockOwned.transferOwnership(address)`` route to distinct Flows.

    Hits both URL-encoded paths, asserts each returns 200 with an embedded
    ``<script id="flow-data">`` block, then parses the embedded JSON from
    each and asserts the payloads differ — minimum: distinct
    ``entry_point_contract_name``. Uses the unfiltered client because the
    ``MockOwned`` route doesn't exist under default scope.
    """
    owned_url = urllib.parse.quote("Owned.transferOwnership(address)", safe="")
    mock_url = urllib.parse.quote("MockOwned.transferOwnership(address)", safe="")

    owned_rv = client_unfiltered.get(f"/flow/{owned_url}")
    mock_rv = client_unfiltered.get(f"/flow/{mock_url}")
    assert owned_rv.status_code == 200, f"Owned route returned {owned_rv.status_code}"
    assert mock_rv.status_code == 200, f"MockOwned route returned {mock_rv.status_code}"

    owned_body = owned_rv.get_data(as_text=True)
    mock_body = mock_rv.get_data(as_text=True)
    assert '<script type="application/json" id="flow-data"' in owned_body
    assert '<script type="application/json" id="flow-data"' in mock_body

    owned_match = _FLOW_DATA_RE.search(owned_body)
    mock_match = _FLOW_DATA_RE.search(mock_body)
    assert owned_match is not None and mock_match is not None
    # Reverse the ``_safe_json`` defang substitutions before parsing.
    owned_data = json.loads(_undefang(owned_match.group(1)))
    mock_data = json.loads(_undefang(mock_match.group(1)))

    assert owned_data != mock_data, "colliding routes returned identical payloads"
    assert owned_data["entry_point_contract_name"] == "Owned"
    assert mock_data["entry_point_contract_name"] == "MockOwned"


def test_flow_page_unknown_returns_404(client: FlaskClient) -> None:
    """Unknown flow ids 404 — and since v0.10.4 the body is the tool's own
    styled error page (site header + back-link), not Flask's bare default."""
    rv = client.get("/flow/Bogus.nonexistent(uint256)")
    assert rv.status_code == 404
    body = rv.get_data(as_text=True)
    assert 'class="site-header"' in body, (
        "404 page must render in the tool's chrome (base.html), not "
        "Flask's bare default (v0.10.4)."
    )
    assert "No Flow at this path." in body
    assert 'class="back-link"' in body, "404 page must offer a way back to the index"


def test_unknown_top_level_path_uses_styled_404(client: FlaskClient) -> None:
    """The errorhandler covers ALL 404s, not just /flow/ misses."""
    rv = client.get("/definitely-not-a-route")
    assert rv.status_code == 404
    assert "No Flow at this path." in rv.get_data(as_text=True)


# -- static assets ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/static/css/main.css",
        "/static/css/pygments.css",
        "/static/vendor/dagre.min.js",
        "/static/vendor/d3.min.js",
        "/static/js/flow-progressive.js",
    ],
)
def test_static_assets_served(client: FlaskClient, path: str) -> None:
    rv = client.get(path)
    assert rv.status_code == 200, f"{path} did not return 200"
    assert int(rv.headers.get("Content-Length", "0")) > 0


def test_legacy_flow_js_is_no_longer_served(client: FlaskClient) -> None:
    """v0.10.0 Stage 2 deleted ``flow.js`` from the static tree. A request
    for the old path must 404 — not silently fall through to another
    file or serve a stale copy.
    """
    rv = client.get("/static/js/flow.js")
    assert rv.status_code == 404


def test_pygments_css_contains_token_classes(client: FlaskClient) -> None:
    """``write_pygments_css`` ran at app construction; the file should
    define styles for the ``.src`` namespace's token classes."""
    rv = client.get("/static/css/pygments.css")
    body = rv.get_data(as_text=True)
    assert ".src" in body
    # A couple of token classes we know Pygments emits for Solidity output.
    assert ".k" in body or ".kt" in body


# -- v0.10.0 Stage 1b: flow-page sizing pins --------------------------------
# These tests pin the CSS rules that prevent --expand-all on a large tree from
# inflating the flex chain (body → .site-main → #graph-frame) to match the
# rendered #graph's intrinsic dimensions. The Stage 1b probe
# (docs/probes/v0_10_0_stage1b_expand_all_probe.py) showed that without these
# rules, Uniswap V4 PoolManager.swap rendered #graph-frame at 9645 × 133773
# (matching the graph) instead of viewport-fit dimensions, leaving every node
# placed off-screen and producing the "graph flashes once and vanishes"
# symptom. pytest cannot assert browser-rendered geometry, but it can pin the
# CSS string so a future refactor that drops these rules trips the test rather
# than silently regressing.


_FLOW_PAGE_BODY_BLOCK_RE = re.compile(
    r"body\.flow-page\s*\{[^}]*\}",
    re.DOTALL,
)
_FLOW_PAGE_SITE_MAIN_BLOCK_RE = re.compile(
    r"body\.flow-page\s+\.site-main\s*\{[^}]*\}",
    re.DOTALL,
)
_GRAPH_FRAME_BLOCK_RE = re.compile(
    r"#graph-frame\s*\{[^}]*\}",
    re.DOTALL,
)


def _block_from(css: str, regex: re.Pattern[str], label: str) -> str:
    match = regex.search(css)
    assert match is not None, f"did not find {label} rule in main.css"
    return match.group(0)


def test_flow_page_body_caps_to_viewport(client: FlaskClient) -> None:
    """``body.flow-page`` must be ``height: 100vh`` and ``overflow: hidden``.

    Without this, --expand-all on a large tree lets the body grow to the
    full rendered-graph height (~133k px on v4-core swap), which lets the
    frame inflate vertically and breaks ``fitTransform``. v0.10.0 Stage 1b.
    """
    rv = client.get("/static/css/main.css")
    css = rv.get_data(as_text=True)
    block = _block_from(css, _FLOW_PAGE_BODY_BLOCK_RE, "body.flow-page")
    assert "height: 100vh" in block, (
        "body.flow-page must set height: 100vh so the page locks to the "
        "viewport. Without it, --expand-all on huge trees blows up the "
        "vertical layout (see v0.10.0 Stage 1b commit)."
    )
    assert "overflow: hidden" in block, (
        "body.flow-page must set overflow: hidden so content overflows are "
        "clipped instead of expanding the body (see v0.10.0 Stage 1b commit)."
    )


def test_flow_page_site_main_caps_to_viewport_width(client: FlaskClient) -> None:
    """``body.flow-page .site-main`` must set ``width: 100%`` and
    ``min-width: 0``.

    Without ``width: 100%``, ``align-items: stretch`` on the body flex
    container doesn't override the item's content-based width and
    .site-main grows to match #graph's intrinsic dimensions. Without
    ``min-width: 0``, the flex automatic-minimum-size rule keeps the item
    at min-content. Both are needed. v0.10.0 Stage 1b.
    """
    rv = client.get("/static/css/main.css")
    css = rv.get_data(as_text=True)
    block = _block_from(css, _FLOW_PAGE_SITE_MAIN_BLOCK_RE, "body.flow-page .site-main")
    assert "width: 100%" in block, (
        "body.flow-page .site-main must set width: 100% so it tracks the "
        "body width instead of its min-content (see v0.10.0 Stage 1b commit)."
    )
    assert "min-width: 0" in block, (
        "body.flow-page .site-main must set min-width: 0 to escape the "
        "flex automatic-minimum-size rule (see v0.10.0 Stage 1b commit)."
    )


def test_graph_frame_caps_to_viewport_width(client: FlaskClient) -> None:
    """``#graph-frame`` must set ``min-width: 0`` to escape the same flex
    automatic-minimum-size trap (mirrors the .site-main pin above)."""
    rv = client.get("/static/css/main.css")
    css = rv.get_data(as_text=True)
    block = _block_from(css, _GRAPH_FRAME_BLOCK_RE, "#graph-frame")
    assert "min-width: 0" in block, (
        "#graph-frame must set min-width: 0 — without it, the frame "
        "inflates to #graph's intrinsic width on huge layouts and "
        "fitTransform() can never scale to fit (see v0.10.0 Stage 1b)."
    )


def test_flow_progressive_fit_lowers_scale_extent_for_huge_layouts(
    client: FlaskClient,
) -> None:
    """The progressive renderer's ``fitTransform`` must lower the d3-zoom
    floor when the required fit scale dips below the default 0.05.

    Without this, even after the CSS pins above bring the frame back to
    viewport dimensions, the computed fit scale (~0.0065 on v4-core
    PoolManager.swap) is clamped back up to 0.05 and nodes at style.top
    67k+ remain off-screen. v0.10.0 Stage 1b.
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    # Pin both the new constant and the lazy zoom.scaleExtent update inside
    # fitTransform — either alone is insufficient.
    assert "ZOOM_MIN_DEFAULT" in js, (
        "flow-progressive.js must define a ZOOM_MIN_DEFAULT constant so the "
        "v0.10.0 Stage 1b dynamic-floor adjustment can reference it."
    )
    assert "zoom.scaleExtent(" in js, (
        "fitTransform must call zoom.scaleExtent(...) to widen the lower "
        "bound when the required fit dips below the default floor."
    )


def test_flow_progressive_balances_expand_all_first_level_sides(
    client: FlaskClient,
) -> None:
    """The progressive renderer must run a global balancing pass before the
    visible relayout under ``--expand-all`` (spec §10.3 "Expand-all
    balancing").

    Without this pass, Rule 1's auto-balance reads an empty
    ``lastNodeRects`` for every first-level branch and ties resolve right,
    so the whole tree piles on one side. Stage 1c adds three helpers —
    ``measureFirstLevelExtents`` (no-DOM dagre run that returns per-branch
    vertical extent), ``balanceFirstLevelSides`` (greedy partition: longest
    first to the lighter side), and ``applyBalancedSides`` (rewrites
    sideById so first-level branches reflect the partition and deeper
    nodes inherit, matching Rule 2; modifier-subtree descendants stay
    "left" per Rule 3). All three must be present and the EXPAND_ALL init
    block must call them in order. pytest cannot run the JS, but pinning
    the helper names and the call shape catches accidental deletion.
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    for helper in (
        "measureFirstLevelExtents",
        "balanceFirstLevelSides",
        "applyBalancedSides",
    ):
        assert helper in js, (
            f"flow-progressive.js must define {helper} for the v0.10.0 "
            f"Stage 1c expand-all balancing pass (spec §10.3 'Expand-all "
            f"balancing')."
        )
    # The EXPAND_ALL init block must wire all three helpers together: the
    # measurement feeds the balancer, whose result feeds the side rewrite.
    # Pin the exact call shape so a refactor that swaps the order or
    # silently drops a step trips this test rather than regressing recall.
    expected = (
        "const extents = measureFirstLevelExtents();\n"
        "    const balanced = balanceFirstLevelSides(extents);\n"
        "    applyBalancedSides(balanced);"
    )
    assert expected in js, (
        "EXPAND_ALL init block must call measureFirstLevelExtents() → "
        "balanceFirstLevelSides() → applyBalancedSides() in that order "
        "before the visible relayout (v0.10.0 Stage 1c)."
    )


def test_flow_progressive_relation_badges_and_title_qualification(
    client: FlaskClient,
) -> None:
    """v0.10.4 relation labeling (spec §10.2): the renderer must key the
    title's qualifying contract and the relation badge off ``call_kind``.

    Pre-v0.10.4 the renderer titled every node by the Flow invoker and
    badged 'inherited from {declarer}' on ANY declarer/invoker mismatch,
    which presented Morpho Blue's IOracle.price() external call as
    'Morpho.price(...), inherited from IOracle' — an inverted trust-boundary
    claim. pytest cannot run the JS; pin the discriminator, the two badge
    labels, and that the inherited badge no longer keys off the bare
    mismatch alone.
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    assert 'node.call_kind === "library" || node.call_kind === "external"' in js, (
        "renderFunctionNode must derive declarer-qualified titles from "
        "call_kind (spec §10.2 v0.10.4 title qualification)."
    )
    assert '"external call"' in js, (
        "external relation badge label 'external call' missing (spec §10.2 "
        "'Relation badges')."
    )
    assert (
        "badge-library" in js and '"library"' in js
    ), "library relation badge missing (spec §10.2 'Relation badges')."
    assert 'node.call_kind === "external"' in js, (
        "relation badge selection must branch on call_kind, not on the "
        "declarer/invoker mismatch alone."
    )


def test_main_css_styles_relation_badges(client: FlaskClient) -> None:
    """The two v0.10.4 relation badge classes must be styled: `external call`
    reuses the dashed --node-external-border outline (same visual language
    as ExternalNode pills), `library` the muted outline of badge-inherited."""
    rv = client.get("/static/css/main.css")
    css = rv.get_data(as_text=True)
    assert (
        ".badge-external-call" in css
    ), "main.css must style .badge-external-call (v0.10.4 relation badges)."
    assert (
        ".badge-library" in css
    ), "main.css must style .badge-library (v0.10.4 relation badges)."


def test_flow_progressive_relayout_restores_opacity_on_existing_nodes(
    client: FlaskClient,
) -> None:
    """v0.10.4 stuck-fade fix: relayout's existing-node branch must
    transition opacity back to 1, not only left/top.

    The existing-node branch calls .interrupt(), which kills any in-flight
    entrance fade from a previous relayout. Before the fix, two expansions
    within ANIM_MS of each other froze the first child at its mid-fade
    opacity forever (reproduced at 150 ms click spacing on Morpho Blue
    withdraw during the v0.10.3 eval) — the graph drew edges pointing at
    invisible nodes. pytest cannot run the JS; pin the transition shape:
    the position transition must also restore opacity.
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    expected = (
        '.style("left", targetLeft + "px")\n'
        '          .style("top", targetTop + "px")\n'
        '          .style("opacity", "1");'
    )
    assert expected in js, (
        "relayout's existing-node (old-position) transition must include "
        '.style("opacity", "1") — without it, an interrupted entrance fade '
        "leaves the node frozen below full opacity forever (v0.10.4 fix)."
    )


def test_flow_progressive_pans_new_nodes_into_view(client: FlaskClient) -> None:
    """v0.10.4 minimal pan (spec §10.2 item 2): after an expansion's
    relayout, newly materialized nodes outside the frame are panned into
    view — minimal translation, animated, zoom untouched; collapse never
    pans.

    pytest cannot run the JS; pin the helper, its wiring in the expand
    branch (and only there), and the zoom-preserving primitive it uses.
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    assert "function panNewNodesIntoView" in js, (
        "flow-progressive.js must define panNewNodesIntoView (v0.10.4 "
        "minimal pan, spec §10.2 item 2)."
    )
    assert "panNewNodesIntoView(fresh);" in js, (
        "the call-line click handler's EXPAND branch must invoke "
        "panNewNodesIntoView with the freshly expanded ids."
    )
    assert js.count("panNewNodesIntoView(") == 2, (
        "panNewNodesIntoView must be invoked exactly once (expand branch) "
        "— collapse never pans (spec §10.2 item 2)."
    )
    assert "zoom.translateBy" in js, (
        "the pan must use zoom.translateBy — translation only, zoom level "
        "untouched (spec §10.2 item 2)."
    )


def test_flow_progressive_dedups_builtins_strip(client: FlaskClient) -> None:
    """v0.10.4 builtins display (spec §11.7): the strip shows each builtin
    once with an occurrence count ('require(bool,string) × 6') instead of
    verbatim repeats. The underlying tuple keeps order and duplicates —
    display-only change."""
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    assert 'b + " × " + n' in js, (
        "builtins strip must render occurrence counts (spec §11.7 dedup "
        "display, v0.10.4)."
    )
    assert 'node.builtins_used.join(", ")' not in js, (
        "the verbatim builtins join must be gone — six require(bool,string) "
        "repeats carried zero information (v0.10.4)."
    )


def test_index_badges_carry_tooltips(client: FlaskClient) -> None:
    """v0.10.4: the d{N} / 'N unr' shorthand and the header stats carry
    native title tooltips — previously unexplained anywhere in the UI."""
    body = client.get("/").get_data(as_text=True)
    assert 'class="meta-depth" title="max call depth ' in body, (
        "meta-depth badges must carry a title tooltip explaining d{N} " "(v0.10.4)."
    )
    assert (
        'class="count-item" title="' in body
    ), "index header count items must carry title tooltips (v0.10.4)."
    # meta-unresolved only renders when a flow has unresolved calls; Solmate's
    # default-scope index has at least one (brutalized/low-level shapes), but
    # don't hard-require it — pin the template instead via the unfiltered
    # client if absent here.
    if 'class="meta-unresolved"' in body:
        assert (
            'class="meta-unresolved" title="' in body
        ), "meta-unresolved badges must carry a title tooltip (v0.10.4)."


def test_node_title_bar_uses_palette_tokens(client: FlaskClient) -> None:
    """v0.10.4: .node-title-bar must use --node-title-bg / --node-title-rule
    (defined in BOTH palettes) instead of hardcoded light-mode values —
    the hardcoded cream bar rendered with unreadable light text in dark
    mode (pre-existing since v0.7.0)."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    assert "background: var(--node-title-bg)" in css, (
        ".node-title-bar background must come from --node-title-bg "
        "(v0.10.4 dark-mode fix)."
    )
    assert (
        "border-bottom: 0.5px solid var(--node-title-rule)" in css
    ), ".node-title-bar rule must come from --node-title-rule (v0.10.4)."
    assert css.count("--node-title-bg:") == 2, (
        "--node-title-bg must be defined in both the light :root and the "
        "dark prefers-color-scheme blocks."
    )
    assert (
        css.count("--node-title-rule:") == 2
    ), "--node-title-rule must be defined in both palettes."


def test_flow_page_back_link_inside_header_row(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """v0.10.4 overlap fix: the back-link renders INSIDE .flow-header (flex
    row), and .flow-nav is no longer a position:fixed overlay.

    The fixed overlay sat on top of the flow title's first characters on
    every flow page (visible even in the README screenshots), hiding the
    contract name. The flow page never scrolls since v0.10.0 (viewport
    lock + d3-zoom pan), so the v0.6 motivation for fixing it is gone.
    """
    url_id = urllib.parse.quote(solmate_flows[0].entry_point_invoker_canonical_name)
    rv = client.get(f"/flow/{url_id}")
    body = rv.get_data(as_text=True)
    header_start = body.find('<header class="flow-header">')
    header_end = body.find("</header>", header_start)
    assert header_start != -1, "flow-header missing from flow page"
    header = body[header_start:header_end]
    assert 'class="back-link"' in header, (
        "the ← index back-link must render inside .flow-header so it "
        "occupies the title row instead of overlaying the title (v0.10.4)."
    )

    css = client.get("/static/css/main.css").get_data(as_text=True)
    nav_block_start = css.find(".flow-nav {")
    nav_block_end = css.find("}", nav_block_start)
    nav_block = css[nav_block_start:nav_block_end]
    assert "position: fixed" not in nav_block, (
        ".flow-nav must not be position:fixed — the overlay occluded the "
        "flow title's first characters (v0.10.4 overlap fix)."
    )


def test_site_header_brand_is_solflow_title_tag_keeps_full_name(
    client: FlaskClient,
) -> None:
    """v0.10.4 branding: the site-header brand anchor reads 'solflow' (the
    CLI name); browser <title> tags keep 'Solidity Flow Navigator'
    (header-only branding decision)."""
    body = client.get("/").get_data(as_text=True)
    assert re.search(r'<a class="site-title"[^>]*>solflow</a>', body), (
        "site-header brand must read 'solflow' (v0.10.4 header-only " "branding)."
    )
    assert "<title>Index — Solidity Flow Navigator</title>" in body, (
        "browser <title> must keep the full project name — the brand "
        "change is header-only."
    )
