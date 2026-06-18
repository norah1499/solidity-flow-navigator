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

from solidity_flow_navigator.analysis.types import (
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import Flow
from solidity_flow_navigator.serve.app import (
    BOOKMARK_COOKIE,
    THEME_COOKIE,
    _build_binding_entries,
    _candidate_contracts,
    _flow_node_count,
    _implemented_signatures,
    _parse_bookmarks,
    _safe_json,
    _safe_next,
    build_index,
    create_app,
)


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
    assert ">Project<" in body
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

    The index template emits one ``<article class="contract-block" ...
    data-name="<contract>">`` per contract. v0.20.0 (the two-pane redesign)
    restructured the ``<h3>`` header, so we locate the article by its
    ``data-name`` attribute — stable across markup changes and unique to the
    article (the navigator rows carry ``data-nav``, entry rows ``data-fn``) —
    then slice to the next ``</article>``. Avoids pulling in an HTML parser for
    what is a simple boundary problem at this scale.
    """
    needle = f'data-name="{contract_name}"'
    idx = body.find(needle)
    assert idx != -1, f"no contract block found for {contract_name}"
    article_start = body.rfind("<article", 0, idx)
    assert article_start != -1, f"no <article> wrapping {contract_name}"
    article_end = body.find("</article>", article_start)
    assert article_end != -1, "unterminated <article> in rendered index"
    return body[article_start:article_end]


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
    # v0.8.0: section headings carry a "· N" count suffix (spec §8.3). v0.20.0:
    # the Reads heading leads with a collapse chevron, so match the heading text
    # without requiring a literal ">" immediately before it.
    assert "Writes · " in block, "Writes section missing from ERC4626 block"
    assert "Reads · " in block, "Reads section missing from ERC4626 block"


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
                single_kind = (contract.name, "Writes")
                break
            if r and not m:
                single_kind = (contract.name, "Reads")
                break
        if single_kind is not None:
            break
    assert single_kind is not None, (
        "expected at least one Solmate contract with a single-kind entry-point "
        "list; index data shape may have changed"
    )
    contract_name, present = single_kind
    absent = "Reads" if present == "Writes" else "Writes"

    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    block = _extract_contract_block(body, contract_name)
    # v0.8.0: section heading is `Present · N`; v0.20.0 leads the Reads heading
    # with a chevron, so match the heading text without a literal ">" prefix.
    assert (
        f"{present} · " in block
    ), f"{present} section missing from {contract_name} block"
    assert (
        f"{absent} · " not in block
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


def test_index_overview_block_renders_summary(client: FlaskClient) -> None:
    """v0.20.0 redesign (spec §8.3): the three-count header is replaced by a
    sidebar Overview block — ``N contracts · M entry points`` and a
    ``R/M visited`` line (relabelled from ``reviewed`` in v0.23.0). The global
    red ``untraced`` trust-budget count is intentionally dropped: the redesign
    reframes unresolved as a neutral per-contract fact in the navigator (never a
    risk/priority cue), so a global red figure would contradict that stance.
    """
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert 'class="index-overview"' in body
    text = re.sub(r"<[^>]+>", "", body)
    assert "contract" in text and "entry point" in text
    assert "visited" in text
    # The old three-count header (and its global untraced count) is gone.
    assert 'class="index-header"' not in body
    assert ">untraced<" not in body


def test_index_facet_bar_pills(client: FlaskClient) -> None:
    """v0.23.0 (spec §8.3): the kind-filter bar carries six pills —
    ``All · Writes · Reads · No modifiers · With modifiers · Unresolved``.
    ``Unresolved`` is new (its backing data, ``Flow.unresolved_count``, already
    existed); the modifier pills are relabelled from ``Unguarded``/``Guarded``
    while keeping the internal ``data-facet`` values stable. (``Payable`` was
    trialled in v0.23.0 development and dropped before release.)
    """
    body = client.get("/").get_data(as_text=True)
    # New facet machine value is wired; the dropped Payable one is absent.
    assert 'data-facet="unresolved"' in body
    assert 'data-facet="payable"' not in body
    # New / relabelled display text is present; the old modifier labels are gone.
    for label in ("No modifiers", "With modifiers", "Unresolved"):
        assert f">{label} <b>" in body, f"facet pill {label!r} missing"
    assert ">Payable <b>" not in body
    assert ">Unguarded <b>" not in body and ">Guarded <b>" not in body
    # Internal modifier facet values are preserved despite the relabel.
    assert 'data-facet="unguarded"' in body and 'data-facet="guarded"' in body
    # Requested tooltips on the relevant pills.
    assert 'title="storage write detected in this flow."' in body
    assert (
        'title="function has no Solidity modifiers. Inline checks may still exist."'
        in body
    )
    assert 'title="call target could not be resolved statically."' in body


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
    # v0.20.0: counts live in the sidebar Overview line `1 contract · 1 entry
    # point`. Strip tags so the <b>1</b> bolding doesn't split the phrase.
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", body))
    assert "1 contract " in text, "singular count must read 'contract' (pluralization)."
    assert (
        "1 entry point " in text
    ), "singular count must read 'entry point' (pluralization)."
    assert "1 contracts" not in text and "1 entry points" not in text


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
    # No chips inside any Reads section, anywhere on the page. v0.20.0 marks the
    # read sub-section with data-section="read"; slice to its closing </section>.
    for m in re.finditer(r'data-section="read"', body):
        section_start = m.start()
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


def test_index_overview_counts_match_build_index_totals(
    client: FlaskClient,
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """The numeric values in the sidebar Overview block match the totals
    build_index returned. Walks build_index directly to avoid pinning concrete
    numbers that drift with Solmate (v0.20.0 redesign)."""
    _, total_eps, total_contracts, _ = build_index(solmate_facts, solmate_flows)
    rv = client.get("/")
    body = rv.get_data(as_text=True)
    overview = body.split('class="index-overview"', 1)[1].split("</div>", 1)[0]
    assert (
        f"<b>{total_contracts}</b>" in overview
    ), f"contracts count {total_contracts} missing from Overview"
    assert (
        f"<b>{total_eps}</b>" in overview
    ), f"entry points count {total_eps} missing from Overview"


def test_index_scope_line_renders_excluded_and_stub_counts(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """v0.20.0 redesign (spec §8.3): the compact scope line reports the COUNT of
    excluded path globs and stub paths — a neutral verification fact in the
    Bindings card footer (or standalone when nothing is bindable), replacing the
    verbose per-glob list."""
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
    assert "Scope · excluded 2 paths · stub 1 path" in body


def test_index_scope_line_renders_none_when_no_stubs(
    solmate_facts: RepoFacts,
    solmate_flows: tuple[Flow, ...],
) -> None:
    """Spec §8.3: when no stubs are active, the compact scope line reads
    ``stub none`` (v0.20.0)."""
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
    assert "Scope · excluded 1 path · stub none" in body


def test_index_renders_chips_legend(client: FlaskClient) -> None:
    """v0.12.0 (spec §8.3): a ``Legend`` chrome line renders unconditionally at
    the foot of the main column, with each key rendered through the SAME classes
    as the live chips (entry-mod-chip / meta-unresolved / meta-depth) so the
    legend stays self-verifying against the rows. v0.20.0 moved the scope line
    into the sidebar, so the legend no longer sits directly below it."""
    rv = client.get("/")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)

    assert 'class="index-legend"' in body
    legend = body.split('class="index-legend"', 1)[1].split("</p>", 1)[0]

    # Literal "Legend" prefix in muted chrome.
    assert '<span class="scope-chrome">Legend</span>' in legend
    # The three keys, each in its live chip class with its sample text.
    assert "entry-mod-chip" in legend
    assert ">modifier</span>" in legend
    assert '<span class="meta-unresolved">2 unr</span>' in legend
    assert '<span class="meta-depth">d3</span>' in legend
    # The three glosses.
    assert "function with a modifier, none = no modifier" in legend
    assert "calls that couldn't be traced" in legend
    assert "max call depth" in legend


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
    asserted_at_least_one_of_each = {"Writes": False, "Reads": False}
    for group in groups:
        for contract in group.contracts:
            if contract.mutating_count:
                expected = f"Writes · {contract.mutating_count}"
                assert expected in body, (
                    f"section heading {expected!r} missing for " f"{contract.name}"
                )
                asserted_at_least_one_of_each["Writes"] = True
            if contract.read_only_count:
                expected = f"Reads · {contract.read_only_count}"
                assert expected in body, (
                    f"section heading {expected!r} missing for " f"{contract.name}"
                )
                asserted_at_least_one_of_each["Reads"] = True
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
                            f"title=\"{ep.unresolved_count} call(s) that couldn't "
                            f'be traced in this Flow — shown as red pills">'
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


def test_safe_json_defangs_script_breakouts_and_stays_valid_json() -> None:
    """``_safe_json`` must prevent ``</script>`` tag breakout AND remain
    parseable JSON. A previous implementation backslash-escaped the
    raw characters (``<\\!--``, ``<\\script``), which are invalid JSON
    escapes — any analyzed source containing ``<!--`` or ``<script`` (e.g.
    in a NatSpec comment) made the client's ``JSON.parse`` throw and the
    flow page render blank. Escaping ``<`` as ``\\u003c`` covers every
    breakout substring while round-tripping cleanly."""
    payload = {"src": "a </script><script>alert(1)</script> <!-- b"}
    encoded = _safe_json(payload)
    assert "</" not in encoded, "raw </ would terminate the embedding tag"
    assert "<script" not in encoded
    assert "<!--" not in encoded
    assert json.loads(encoded) == payload, "defanged output must stay valid JSON"


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
    # ``_safe_json`` output is valid JSON (``<`` is emitted as the standard
    # escape ``\u003c``), so the embedded payload parses directly.
    owned_data = json.loads(owned_match.group(1))
    mock_data = json.loads(mock_match.group(1))

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
        "/static/js/index.js",
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


def test_flow_progressive_anchors_single_node_ranks(client: FlaskClient) -> None:
    """The within-rank reorder pass (``reorderRanksBySourceLine``) must NOT
    short-circuit ranks that hold a single node — a lone child still has to
    be anchored to its parent's call-site line (spec §10.3 per-line anchor).

    Earlier the per-rank loop began ``if (rankNodes.length < 2) return;``,
    skipping any node alone in its column. The pass mutates each parent's Y
    in place (forward-separation sweep, which only pushes DOWN) and relies on
    re-anchoring the next depth to pull descendants down to follow. A
    single-call chain hanging off a parent that got pushed down therefore
    never caught up: each deeper rank held one node, hit the guard, and kept
    dagre's original Y — so the deep node floated far ABOVE its shifted
    parent with a long stretched connector edge. Reproduced at depth 3-4 in
    v4-core (``applyDelta`` → ``NonzeroDeltaCount.decrement``, ``Hooks.callHook``
    → ``CustomRevert.bubbleUpAndRevertWith``).

    pytest cannot run the JS; pin that the rank-level short-circuit is gone
    and that lone ranks still flow into the same per-line anchor used for
    multi-node ranks (and for edge rendering).
    """
    rv = client.get("/static/js/flow-progressive.js")
    js = rv.get_data(as_text=True)
    assert "reorderRanksBySourceLine" in js, (
        "flow-progressive.js must define the within-rank reorder pass "
        "reorderRanksBySourceLine (spec §10.3)."
    )
    assert "if (rankNodes.length < 2) return;" not in js, (
        "reorderRanksBySourceLine must not short-circuit single-node ranks — "
        "re-adding the rank-level guard regresses the 'deep node flies way "
        "up, long stretched edge' artifact (single-call chains detach from a "
        "parent the forward-separation sweep pushed down)."
    )
    # Lone ranks must reach the same call-site-line anchor (idealCenter) the
    # multi-node path and edge rendering use, so the child tracks its parent.
    assert "const off = lineOffsetInParent(pDom, rel);" in js, (
        "anchor-then-separate must compute the parent call-site-line offset "
        "via lineOffsetInParent (spec §10.3 per-line anchor)."
    )
    assert "idealCenter = pDagre.y - pDagre.height / 2 + off.y;" in js, (
        "anchor-then-separate must center each child on its parent's "
        "call-site line (idealCenter), the rule lone ranks now also obey."
    )


def test_flow_progressive_colors_argument_nested_calls(client: FlaskClient) -> None:
    """Calls nested inside another call's argument list (the ``bar`` in
    ``foo(bar(x))``) render in a distinct color so they stand out — with no
    change to interaction (the whole line stays the single expand target, and
    a nested call still opens together with its line).

    The renderer locates real call sites in the function's source text
    (``scanCallSites`` — an identifier followed by ``(``, including on the
    continuation lines of a multi-line call statement) and tags the nested ones
    with ``src-call-name--arg``; main.css colors that class with the teal
    ``--tok-call-arg``, defined once per theme block (light / auto-dark @media /
    forced ``:root[data-theme="dark"]``), matching the other tokenized call
    colors. pytest cannot run the JS; pin the helper, the class tag, and the
    three-block token.
    """
    js = client.get("/static/js/flow-progressive.js").get_data(as_text=True)
    assert "function scanCallSites(" in js, (
        "flow-progressive.js must define scanCallSites to locate call sites "
        "(including on continuation lines of multi-line statements) and detect "
        "argument-nesting from source text."
    )
    assert "src-call-name--arg" in js, (
        "the renderer must tag argument-nested call-name tokens with "
        "src-call-name--arg (distinct color, behavior unchanged)."
    )
    css = client.get("/static/css/main.css").get_data(as_text=True)
    assert css.count("--tok-call-arg:") == 3, (
        "--tok-call-arg must be defined once per theme block (light, auto-dark "
        "@media, forced :root[data-theme=dark]), like the other tokenized call "
        "colors."
    )
    assert (
        ".src-call-name--arg" in css
    ), "main.css must color the .src-call-name--arg class with --tok-call-arg."


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
    branch, the v0.22.0 call-tree sidebar's legitimate reuse of it (a
    sidebar row reveals its node), and the collapse-never-pans invariant.
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
    # v0.22.0: the call-tree sidebar legitimately pans too (ctOpen reveals a
    # node when its row's chevron is clicked), so the call is no longer globally
    # unique. The load-bearing invariant is that the call-line COLLAPSE branch
    # never pans — check that branch directly.
    collapse_marker = "expanded.forEach((cid) => collapse(cid));"
    assert collapse_marker in js
    collapse_branch = js[
        js.index(collapse_marker) : js.index("} else {", js.index(collapse_marker))
    ]
    assert (
        "panNewNodesIntoView" not in collapse_branch
    ), "the call-line collapse branch must never pan (spec §10.2 item 2)."
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
    """v0.10.4: the d{N} / 'N unr' shorthand carry native title tooltips —
    previously unexplained anywhere in the UI. (v0.20.0 dropped the three-count
    header, and with it the header-stat tooltips.)"""
    body = client.get("/").get_data(as_text=True)
    assert 'class="meta-depth" title="max call depth ' in body, (
        "meta-depth badges must carry a title tooltip explaining d{N} " "(v0.10.4)."
    )
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
    (defined in every palette) instead of hardcoded light-mode values —
    the hardcoded cream bar rendered with unreadable light text in dark
    mode (pre-existing since v0.7.0).

    v0.14.0: with the three-state theme control, each dark token is now
    defined in THREE places — the light ``:root`` default, the auto-dark
    ``@media (prefers-color-scheme: dark)`` block, and the forced-dark
    ``:root[data-theme="dark"]`` mirror — so the count is 3, not 2."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    assert "background: var(--node-title-bg)" in css, (
        ".node-title-bar background must come from --node-title-bg "
        "(v0.10.4 dark-mode fix)."
    )
    assert (
        "border-bottom: 0.5px solid var(--node-title-rule)" in css
    ), ".node-title-bar rule must come from --node-title-rule (v0.10.4)."
    assert css.count("--node-title-bg:") == 3, (
        "--node-title-bg must be defined in the light :root, the auto-dark "
        "prefers-color-scheme block, and the forced-dark [data-theme] mirror "
        "(v0.14.0 three-state theme)."
    )
    assert (
        css.count("--node-title-rule:") == 3
    ), "--node-title-rule must be defined in all three palette blocks (v0.14.0)."


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
    """v0.10.4 branding: the site-header brand anchor is the CLI name; browser
    <title> tags keep 'Solidity Flow Navigator' (header-only branding decision).
    v0.23.0 renders the brand as the `solFlow` wordmark (`Flow` in its own span
    for blue coloring); the CLI name and the <title> are unchanged."""
    body = client.get("/").get_data(as_text=True)
    m = re.search(r'<a class="site-title"[^>]*>(.*?)</a>', body, re.S)
    assert m, "site-header brand anchor (class=site-title) must be present."
    brand_text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    assert (
        "solFlow" in brand_text
    ), "site-header brand must render the 'solFlow' wordmark (v0.23.0)."
    assert (
        'class="site-title-flow">Flow</span>' in body
    ), "the 'Flow' half of the wordmark must be in its own span for coloring."
    assert "<title>Overview — Solidity Flow Navigator</title>" in body, (
        "browser <title> must keep the full project name — the brand "
        "change is header-only."
    )


# -- theme control (v0.14.0, spec §8.3) -------------------------------------


@pytest.fixture
def themed_client(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> FlaskClient:
    """A function-scoped client for cookie tests.

    Cookie-reading tests use the test client's cookie jar (``set_cookie``)
    rather than a manual ``Cookie:`` header — Werkzeug's client rebuilds the
    request's Cookie header from its jar, so a manual header is dropped when
    the jar is empty. A fresh per-test client keeps jar state from leaking
    across tests (the module-scoped ``client`` is shared and must stay clean).
    """
    app = create_app(solmate_facts, solmate_flows)
    app.config.update(TESTING=True)
    return app.test_client()


def test_theme_default_is_auto_no_attribute(client: FlaskClient) -> None:
    """No cookie → <html> carries no data-theme (Auto follows the OS via
    prefers-color-scheme) and the Auto segment is the active one."""
    body = client.get("/").get_data(as_text=True)
    assert '<html lang="en">' in body, (
        "Auto (no cookie) must stamp no data-theme so prefers-color-scheme "
        "governs the palette."
    )
    assert "data-theme=" not in body
    assert 'aria-current="true">Auto</a>' in body
    assert 'aria-current="true">Dark</a>' not in body


def test_dark_cookie_stamps_attribute_and_marks_dark(
    themed_client: FlaskClient,
) -> None:
    """A solflow_theme=dark cookie makes the server stamp data-theme="dark"
    on <html> (so the forced palette renders with no theme-flash) and marks
    the Dark segment active."""
    themed_client.set_cookie(THEME_COOKIE, "dark")
    body = themed_client.get("/").get_data(as_text=True)
    assert '<html lang="en" data-theme="dark">' in body
    assert 'aria-current="true">Dark</a>' in body
    assert 'aria-current="true">Auto</a>' not in body


def test_light_cookie_stamps_attribute(themed_client: FlaskClient) -> None:
    """A solflow_theme=light cookie forces light even where the OS is dark —
    the data-theme="light" attribute is what suppresses the prefers-color-scheme
    rule in main.css (the :not([data-theme="light"]) guard)."""
    themed_client.set_cookie(THEME_COOKIE, "light")
    body = themed_client.get("/").get_data(as_text=True)
    assert '<html lang="en" data-theme="light">' in body
    assert 'aria-current="true">Light</a>' in body


def test_unknown_cookie_value_falls_back_to_auto(themed_client: FlaskClient) -> None:
    """A junk cookie value is treated as Auto: no attribute, Auto active."""
    themed_client.set_cookie(THEME_COOKIE, "chartreuse")
    body = themed_client.get("/").get_data(as_text=True)
    assert "data-theme=" not in body
    assert 'aria-current="true">Auto</a>' in body


def test_set_theme_dark_sets_cookie_and_redirects(client: FlaskClient) -> None:
    """GET /theme/dark sets the cookie and redirects back to ``next``."""
    rv = client.get("/theme/dark?next=/")
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/")
    set_cookie = rv.headers.get("Set-Cookie", "")
    assert f"{THEME_COOKIE}=dark" in set_cookie
    assert "SameSite=Lax" in set_cookie


def test_set_theme_auto_clears_cookie(client: FlaskClient) -> None:
    """GET /theme/auto clears the cookie (immediate-expiry Set-Cookie) so the
    OS preference governs again."""
    rv = client.get("/theme/auto?next=/")
    assert rv.status_code == 302
    set_cookie = rv.headers.get("Set-Cookie", "")
    assert f"{THEME_COOKIE}=" in set_cookie
    assert "Max-Age=0" in set_cookie or "Expires=Thu, 01 Jan 1970" in set_cookie


def test_set_theme_invalid_value_is_404(client: FlaskClient) -> None:
    """An unrecognized theme value is a bad URL, not a silent no-op."""
    assert client.get("/theme/bogus").status_code == 404


def test_set_theme_blocks_open_redirect(client: FlaskClient) -> None:
    """A non-local ``next`` is refused — the route redirects to the index
    instead of bouncing to an attacker-supplied URL."""
    rv = client.get("/theme/dark?next=https://evil.example/x")
    assert rv.status_code == 302
    loc = rv.headers["Location"]
    assert "evil.example" not in loc
    assert loc.endswith("/")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/flow/Foo.bar(uint256)", "/flow/Foo.bar(uint256)"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_safe_next_only_allows_local_paths(raw: str | None, expected: str) -> None:
    """``_safe_next`` honors only same-origin absolute paths; everything else
    (external, protocol-relative, scheme) collapses to the index."""
    assert _safe_next(raw) == expected


def test_theme_control_is_server_side_links(client: FlaskClient) -> None:
    """The header carries the three theme links, server-side (plain links + a
    cookie, no script of their own). The index does load index.js (v0.15.0
    bookmark enhancement), but the theme control itself needs no JavaScript."""
    body = client.get("/").get_data(as_text=True)
    assert 'class="theme-control"' in body
    for value in ("auto", "light", "dark"):
        assert f"/theme/{value}" in body
    # The only script on the index is the bookmark progressive-enhancement file;
    # there are no inline scripts and no theme-control script.
    assert "js/index.js" in body
    assert "<script>" not in body, "the index carries no INLINE scripts."


# -- bookmarks (v0.15.0, spec §8.3) -----------------------------------------

# A real Solmate entry/contract that exist under default scope (used by the
# flow-page test above too), so the bookmark round-trips have something to match.
_BM_ENTRY = "ERC4626.deposit(uint256,address)"
_BM_CONTRACT = "Owned"


@pytest.fixture
def bookmark_client(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> FlaskClient:
    """Function-scoped client with its own cookie jar for bookmark round-trips.

    Same rationale as ``themed_client``: cookie state must not leak across tests
    and the jar must rebuild the request Cookie header from server-set cookies.
    """
    app = create_app(solmate_facts, solmate_flows)
    app.config.update(TESTING=True)
    return app.test_client()


def test_parse_bookmarks_is_defensive() -> None:
    """``_parse_bookmarks`` degrades junk to an empty list, and preserves order
    while dropping duplicates and unprefixed entries."""
    assert _parse_bookmarks(None) == []
    assert _parse_bookmarks("") == []
    assert _parse_bookmarks("not json") == []
    assert _parse_bookmarks(json.dumps({"x": 1})) == []  # not a list
    assert _parse_bookmarks(json.dumps(["e:a", "c:b", "junk", "e:a", 5])) == [
        "e:a",
        "c:b",
    ]


def test_bookmark_entry_sets_cookie_and_redirects(client: FlaskClient) -> None:
    """GET /bookmark/entry/<id> sets the bookmarks cookie and redirects to
    ``next``."""
    ident = urllib.parse.quote(_BM_ENTRY, safe="")
    rv = client.get(f"/bookmark/entry/{ident}?next=/")
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/")
    set_cookie = rv.headers.get("Set-Cookie", "")
    assert f"{BOOKMARK_COOKIE}=" in set_cookie
    assert "SameSite=Lax" in set_cookie


def test_bookmark_toggle_off_clears_when_empty(bookmark_client: FlaskClient) -> None:
    """Toggling the same id twice removes it; when no bookmarks remain the cookie
    is cleared. The round-trip also confirms the jar resends the server-set
    JSON cookie (otherwise the second toggle would re-add rather than remove)."""
    ident = urllib.parse.quote(_BM_ENTRY, safe="")
    bookmark_client.get(f"/bookmark/entry/{ident}")  # on
    rv = bookmark_client.get(f"/bookmark/entry/{ident}")  # off
    set_cookie = rv.headers.get("Set-Cookie", "")
    assert "Max-Age=0" in set_cookie or "Expires=Thu, 01 Jan 1970" in set_cookie


def test_bookmark_invalid_kind_is_404(client: FlaskClient) -> None:
    """An unrecognized kind is a bad URL, not a silent no-op."""
    assert client.get("/bookmark/bogus/Whatever").status_code == 404


def test_bookmark_blocks_open_redirect(client: FlaskClient) -> None:
    """A non-local ``next`` is refused — redirect collapses to the index."""
    ident = urllib.parse.quote(_BM_ENTRY, safe="")
    rv = client.get(f"/bookmark/entry/{ident}?next=https://evil.example/x")
    assert rv.status_code == 302
    assert "evil.example" not in rv.headers["Location"]
    assert rv.headers["Location"].endswith("/")


def test_index_bookmarked_section_renders_for_known_ids(
    bookmark_client: FlaskClient,
) -> None:
    """With an entry and a contract pinned, the index renders a `Pinned`
    section and the corresponding row toggles show the filled (is-on) state."""
    bookmark_client.get(f"/bookmark/entry/{urllib.parse.quote(_BM_ENTRY, safe='')}")
    bookmark_client.get(f"/bookmark/contract/{_BM_CONTRACT}")
    body = bookmark_client.get("/").get_data(as_text=True)
    assert 'class="index-bookmarked"' in body
    assert ">Pinned</h2>" in body
    assert body.count('class="bookmark-link"') >= 2
    assert "bookmark-toggle is-on" in body


def test_index_ignores_stale_bookmark_ids(bookmark_client: FlaskClient) -> None:
    """Ids that match nothing in the current analysis are ignored: no section,
    no error (bookmarks set against a different codebase)."""
    bookmark_client.get(
        f"/bookmark/entry/{urllib.parse.quote('Nope.ghost(uint256)', safe='')}"
    )
    bookmark_client.get("/bookmark/contract/DoesNotExist")
    body = bookmark_client.get("/").get_data(as_text=True)
    assert 'class="index-bookmarked"' not in body


def test_index_bookmarks_degrade_without_js(bookmark_client: FlaskClient) -> None:
    """Progressive enhancement: the index loads index.js for in-place toggling,
    but the bookmark toggles remain real server-side links (a working no-JS
    fallback) and the Bookmarked section is server-rendered (spec §8.3)."""
    bookmark_client.get(f"/bookmark/entry/{urllib.parse.quote(_BM_ENTRY, safe='')}")
    body = bookmark_client.get("/").get_data(as_text=True)
    assert "js/index.js" in body, "index must load the enhancement script."
    assert 'class="bookmark-toggle' in body
    # Every toggle is a server-side link to the /bookmark route (no-JS fallback).
    assert 'href="/bookmark/' in body
    assert 'class="index-bookmarked"' in body, "section is server-rendered."


def test_flow_page_carries_bookmark_toggle(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """The Flow-page header carries a bookmark toggle for the entry it shows."""
    url_id = urllib.parse.quote(_BM_ENTRY, safe="")
    body = client.get(f"/flow/{url_id}").get_data(as_text=True)
    # The site-header closes first; find the flow-header's own close after it.
    header_start = body.find('<header class="flow-header">')
    header = body[header_start : body.find("</header>", header_start)]
    assert 'class="bookmark-toggle' in header, (
        "the flow header must carry a bookmark toggle for the current entry "
        "(v0.15.0 §8.3)."
    )


def test_flow_progressive_back_link_restores_scroll_via_history(
    client: FlaskClient,
) -> None:
    """v0.15.0: the in-app back link reuses history navigation (so the browser
    restores the index scroll position) when the referrer is the index. pytest
    can't run the JS; pin the handler shape so a refactor that drops it trips."""
    js = client.get("/static/js/flow-progressive.js").get_data(as_text=True)
    assert ".back-link" in js, "the back-link handler must select the link."
    assert "document.referrer" in js, (
        "the handler must gate on the referrer so it only intercepts when the "
        "user arrived from the index (v0.15.0)."
    )
    assert "history.back()" in js, (
        "the handler must call history.back() to reuse the browser's scroll "
        "restoration instead of a fresh navigation (v0.15.0)."
    )


def test_main_css_bookmark_toggle_uses_tok_number_when_on(
    client: FlaskClient,
) -> None:
    """The filled bookmark reuses the existing --tok-number palette token (no new
    accent); the outline state uses muted chrome. Pins the color contract."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    start = css.find(".bookmark-toggle.is-on .bookmark-ico {")
    assert start != -1, "main.css must style the on-state bookmark icon (v0.15.0)."
    block = css[start : css.find("}", start)]
    assert "var(--tok-number)" in block, (
        ".bookmark-toggle.is-on must fill with --tok-number (reused palette "
        "color, no new accent — v0.15.0 §8.3)."
    )


def test_index_bookmark_jump_shortcut_present_with_count(
    bookmark_client: FlaskClient,
) -> None:
    """v0.15.0 round 2: a persistent shortcut anchors to the Bookmarked section
    from anywhere, with a server-rendered count. The section carries id=bookmarked."""
    bookmark_client.get(f"/bookmark/entry/{urllib.parse.quote(_BM_ENTRY, safe='')}")
    bookmark_client.get(f"/bookmark/contract/{_BM_CONTRACT}")
    body = bookmark_client.get("/").get_data(as_text=True)
    assert 'class="bookmark-jump"' in body, "persistent bookmarks shortcut missing."
    assert 'href="#bookmarked"' in body, "shortcut must anchor to the section."
    assert 'id="bookmarked"' in body, "Bookmarked section must carry the anchor id."
    assert (
        '<span class="bookmark-jump-count">2</span>' in body
    ), "shortcut must show the server-rendered bookmark count (entry + contract)."


def test_index_no_bookmark_jump_without_bookmarks(client: FlaskClient) -> None:
    """With no bookmarks, neither the shortcut nor the section renders."""
    body = client.get("/").get_data(as_text=True)
    assert 'class="bookmark-jump"' not in body
    assert 'id="bookmarked"' not in body


def test_main_css_viewed_row_dims_signature(client: FlaskClient) -> None:
    """v0.20.0 redesign (spec §8.3): a reviewed (opened) row dims its signature
    to opacity .5 — the redesign's neutral 'already-looked-at' cue, paired with a
    ✓ in the meta column — replacing the v0.15.0 --seen-bg pill. Server-tracked
    via the solflow_viewed cookie."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    start = css.find(".entry-row.viewed .entry-link {")
    assert start != -1, "main.css must style .entry-row.viewed (the viewed cue)."
    block = css[start : css.find("}", start)]
    assert "opacity" in block, ".entry-row.viewed must dim the signature (opacity)."


def test_flow_page_records_view_in_cookie(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """Opening a Flow page records its entry id in the solflow_viewed cookie."""
    from solidity_flow_navigator.serve.app import VIEWED_COOKIE

    rv = client.get(f"/flow/{urllib.parse.quote(_BM_ENTRY, safe='')}")
    assert rv.status_code == 200
    set_cookie = rv.headers.get("Set-Cookie", "")
    assert f"{VIEWED_COOKIE}=" in set_cookie, "flow view must set solflow_viewed."
    assert "SameSite=Lax" in set_cookie


def test_index_marks_viewed_row(bookmark_client: FlaskClient) -> None:
    """After opening a Flow, that entry's row on the index carries the `viewed`
    class (the cookie round-trips through the client jar)."""
    enc = urllib.parse.quote(_BM_ENTRY, safe="")
    bookmark_client.get(f"/flow/{enc}")  # records the view in the cookie jar
    body = bookmark_client.get("/").get_data(as_text=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", _BM_ENTRY).strip("-")
    row_id = f'id="e-{slug}"'
    idx = body.find(row_id)
    assert idx != -1, "viewed entry row must be present on the index."
    li_start = body.rfind("<li", 0, idx)
    assert (
        "viewed" in body[li_start:idx]
    ), "the opened entry's row must carry the `viewed` class (v0.15.0)."


def test_main_css_anchor_scroll_margin(client: FlaskClient) -> None:
    """Bookmark redirects land on row/contract anchors with breathing room from
    the top, not flush against it."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    start = css.find(".entry-row,\n.contract-block {")
    assert start != -1, "main.css must give anchor targets scroll-margin (v0.15.0)."
    block = css[start : css.find("}", start)]
    assert "scroll-margin-top" in block


def test_bookmark_toggle_fallback_targets_row(client: FlaskClient) -> None:
    """The no-JS fallback returns to the clicked row anchor (e-/c-), not the page
    top — JavaScript intercepts the click for the in-place path, but the link is
    what runs with JS disabled."""
    body = client.get("/").get_data(as_text=True)
    m = re.search(r'<a class="bookmark-toggle[^"]*" href="([^"]+)"', body)
    assert m is not None, "expected a bookmark toggle on the index."
    href = m.group(1)
    assert "/bookmark/" in href, "toggle must hit the /bookmark route."
    assert (
        "%23e-" in href or "%23c-" in href
    ), "fallback next must return to the row anchor (e-/c-), not the top."


def test_main_css_smooth_scroll_guarded_by_reduced_motion(client: FlaskClient) -> None:
    """Anchor navigation glides (scroll-behavior: smooth), only when the user has
    not requested reduced motion."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    assert "scroll-behavior: smooth" in css
    assert "prefers-reduced-motion: no-preference" in css


def test_index_js_toggles_in_place_and_preserves_scroll(client: FlaskClient) -> None:
    """index.js intercepts bookmark clicks, persists via fetch, and compensates
    scroll so the auditor stays put. pytest can't run the JS; pin the shape so a
    refactor that drops the in-place behavior trips this test."""
    js = client.get("/static/js/index.js").get_data(as_text=True)
    assert ".bookmark-toggle" in js, "must intercept bookmark toggle clicks."
    assert "preventDefault" in js, "must suppress the default navigation."
    assert "fetch(" in js, "must persist the toggle via fetch (no reload)."
    assert "scrollBy" in js, "must compensate scroll so the view stays fixed."


def test_flow_progressive_toggles_bookmark_in_place(client: FlaskClient) -> None:
    """The flow page toggles its header bookmark via fetch rather than reloading
    (a reload would re-run the whole graph layout)."""
    js = client.get("/static/js/flow-progressive.js").get_data(as_text=True)
    assert ".flow-nav .bookmark-toggle" in js, "must target the flow header toggle."
    assert "fetch(" in js, "flow-page bookmark must persist via fetch, not reload."


# ----- v0.16.0 Feature 1: per-flow Expand all / Collapse all (spec §10.2) ----


def test_flow_page_has_expand_collapse_controls(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """The flow header carries whole-tree Expand all / Collapse all controls plus
    the relocated Reset view, grouped in .flow-controls at the right of the header
    (v0.16.0, spec §10.2). They are <button>s, not links — the graph is
    JS-rendered, so there is no no-JS fallback to preserve."""
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    body = client.get(f"/flow/{target.entry_point_invoker_canonical_name}").get_data(
        as_text=True
    )
    assert (
        'id="expand-all-btn"' in body
    ), "flow header must carry the Expand all control."
    assert (
        'id="collapse-all-btn"' in body
    ), "flow header must carry the Collapse all control."
    assert "Expand all" in body and "Collapse all" in body
    # Buttons, not links (no href, type=button).
    assert '<button class="flow-control" id="expand-all-btn" type="button"' in body
    # v0.16.0: the three controls are grouped in .flow-controls, and Reset view
    # moved out of #graph-frame into that group (id preserved for the JS wiring).
    start = body.find('class="flow-controls"')
    assert start != -1, "controls must be grouped in .flow-controls (right of header)."
    group = body[start : body.find("</div>", start)]
    for ctrl in ('id="expand-all-btn"', 'id="collapse-all-btn"', 'id="reset-view"'):
        assert ctrl in group, f"{ctrl} must live in the .flow-controls group."
    # Reset view is no longer a floating button inside the graph frame.
    frame_start = body.find('id="graph-frame"')
    assert (
        'id="reset-view"' not in body[frame_start:]
    ), "Reset view must be in the header group, not inside #graph-frame."


def test_flow_progressive_wires_expand_collapse_all(client: FlaskClient) -> None:
    """v0.16.0 (spec §10.2): the renderer wires the whole-tree controls and the
    e / c keyboard shortcuts. pytest can't run the JS; pin the handler shape so a
    refactor that drops it trips this test."""
    js = client.get("/static/js/flow-progressive.js").get_data(as_text=True)
    assert (
        "expand-all-btn" in js and "collapse-all-btn" in js
    ), "must wire both header buttons by id."
    assert "function expandAll(" in js and "function collapseAll(" in js
    # Expand all reuses the existing recursive expander + the expand-all balancing.
    assert "expandAllRecursive(flow.root.__id)" in js
    # e / c keyboard shortcuts alongside the existing R / 0 fit keys.
    assert '=== "e"' in js and '=== "c"' in js, "must bind the e / c shortcut keys."


# ----- v0.16.0 Feature 3: persisted per-flow expansion state (spec §10.2) -----


def test_flow_data_carries_flow_id(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """v0.16.0 (spec §10.2): #flow-data carries data-flow-id (the entry url id) so
    the renderer can key persisted expansion state per Flow."""
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    body = client.get(f"/flow/{target.entry_point_invoker_canonical_name}").get_data(
        as_text=True
    )
    assert (
        'data-flow-id="ERC4626.deposit(' in body
    ), "#flow-data must expose the entry url id as data-flow-id (v0.16.0)."


def test_flow_progressive_persists_expansion_to_localstorage(
    client: FlaskClient,
) -> None:
    """v0.16.0 (spec §10.2 'Persisted expansion state'): the renderer remembers
    expanded call sites per Flow in localStorage and restores them on the default
    initial-render path. pytest can't run the JS; pin the shape."""
    js = client.get("/static/js/flow-progressive.js").get_data(as_text=True)
    assert "localStorage" in js, "expansion state must persist to localStorage."
    assert "solflow:expanded:" in js, "the storage key must be namespaced per Flow."
    assert "function persistExpansion(" in js and "function restoreExpansion(" in js
    assert "dataset.flowId" in js, "the key must read data-flow-id (the entry url id)."
    assert "restoreExpansion()" in js, "must restore on init."


# ----- v0.16.0 Feature 2: index filter (spec §8.3) ---------------------------


def test_index_has_filter_box(client: FlaskClient) -> None:
    """v0.16.0 (spec §8.3): the index carries a filter box, server-rendered but
    `hidden` so no-JS users never see a dead control (index.js reveals it). Contract
    blocks carry a clean data-name for contract-name matching."""
    body = client.get("/").get_data(as_text=True)
    assert 'id="entry-filter"' in body, "index must carry the filter input."
    start = body.find('class="index-filter"')
    assert start != -1, "filter container must be present."
    container = body[start : body.find(">", start) + 1]
    assert "hidden" in container, "the filter box must be hidden for the no-JS path."
    assert 'data-name="ERC4626"' in body, "contract blocks must carry data-name."


def test_index_js_filters_entry_rows(client: FlaskClient) -> None:
    """index.js implements the filter (an input listener that narrows .entry-row by
    signature/contract name and reveals the box); the index stays free of inline
    scripts (spec §8.3)."""
    body = client.get("/").get_data(as_text=True)
    assert "<script>" not in body, "the filter adds no inline script (spec §8.3)."
    js = client.get("/static/js/index.js").get_data(as_text=True)
    assert "entry-filter" in js, "index.js must wire the filter input."
    assert (
        "applyFilter" in js and "initFilter" in js
    ), "must implement + reveal the filter."
    assert ".entry-row" in js, "must narrow the entry rows."
    assert "data-name" in js, "must match against the contract name too."
    # The filter must hide rows via inline display, NOT the `hidden` attribute:
    # `.entry-row { display: flex }` overrides the UA `[hidden]` rule, so a hidden
    # attribute would leave rows visible (the v0.16.0 filter bug).
    assert (
        "style.display" in js
    ), "rows must be hidden via inline display, not [hidden]."


def test_main_css_filter_hidden_for_no_js(client: FlaskClient) -> None:
    """The filter's `display` is scoped to :not([hidden]) so the UA
    `[hidden] { display: none }` rule still wins for no-JS users — a bare
    `.index-filter { display: flex }` would override it and leak the dead box."""
    css = client.get("/static/css/main.css").get_data(as_text=True)
    assert (
        ".index-filter:not([hidden])" in css
    ), "filter display must be gated on :not([hidden]) so it stays hidden with JS off."


# ---------------------------------------------------------------------------
# Interface bindings (v0.17.0, spec §10.2, §13.2)
# ---------------------------------------------------------------------------


def test_bind_route_redirects_and_index_still_renders(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> None:
    app = create_app(solmate_facts, solmate_flows, scope=Scope())
    app.config.update(TESTING=True)
    c = app.test_client()
    resp = c.get("/bind/IFoo?contract=Bar&next=/")
    assert resp.status_code == 302
    # the rebuild succeeded and the index renders with the new state
    assert c.get("/").status_code == 200


_SL = SourceLocation(
    filename_absolute="/abs/x.sol",
    filename_relative="src/x.sol",
    start=0,
    length=1,
    lines=(1,),
    starting_column=1,
    ending_column=2,
)


def _ct(
    name: str,
    *,
    kind: str = "contract",
    is_interface: bool = False,
    is_abstract: bool = False,
    bases: tuple[str, ...] = (),
    funcs: tuple[Function, ...] = (),
) -> Contract:
    return Contract(
        name=name,
        kind=kind,
        is_interface=is_interface,
        is_library=False,
        is_abstract=is_abstract,
        linearized_base_contract_names=bases,
        immediate_base_contract_names=bases[:1] if bases else (),
        source_location=_SL,
        functions=funcs,
        modifiers=(),
    )


def _fn(declarer: str, full_name: str, *, is_implemented: bool = True) -> Function:
    return Function(
        canonical_name=f"{declarer}.{full_name}",
        name=full_name.split("(")[0],
        full_name=full_name,
        contract_declarer_name=declarer,
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=is_implemented,
        is_virtual=False,
        is_entry_point=False,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_SL,
        source_code="",
        calls=(),
    )


def test_candidate_contracts_excludes_unrelated_contracts() -> None:
    """Candidates are explicit + structural implementers only — never every
    contract in the repo (the bug that dumped all contracts into the dropdown)."""
    iface = _ct(
        "IFoo",
        kind="interface",
        is_interface=True,
        funcs=(_fn("IFoo", "foo()", is_implemented=False),),
    )
    structural = _ct("FooImpl", funcs=(_fn("FooImpl", "foo()"),))
    explicit = _ct("FooChild", bases=("IFoo",), funcs=(_fn("FooChild", "foo()"),))
    unrelated = _ct("Bar", funcs=(_fn("Bar", "bar()"),))
    facts = RepoFacts(
        repo_path="/abs",
        contracts=(iface, structural, explicit, unrelated),
        free_functions=(),
    )
    cands = _candidate_contracts(facts, "IFoo", _implemented_signatures(facts))
    assert "FooImpl" in cands  # structural implementer
    assert "FooChild" in cands  # explicit implementer
    assert "Bar" not in cands  # unrelated — the noise we removed


def test_candidate_contracts_empty_when_nothing_implements() -> None:
    """An interface nothing implements (e.g. a cheatcode interface) yields an
    empty candidate list rather than every contract."""
    iface = _ct(
        "Vm",
        kind="interface",
        is_interface=True,
        funcs=(_fn("Vm", "warp(uint256)", is_implemented=False),),
    )
    other = _ct("Bar", funcs=(_fn("Bar", "bar()"),))
    facts = RepoFacts(repo_path="/abs", contracts=(iface, other), free_functions=())
    cands = _candidate_contracts(facts, "Vm", _implemented_signatures(facts))
    assert cands == ()


# ---------------------------------------------------------------------------
# Index Bindings panel + Save-to-TOML (v0.18.0, spec §8.3, §13.2)
# ---------------------------------------------------------------------------


def test_build_binding_entries_filters_and_sorts() -> None:
    """The panel lists bindable interfaces (≥1 candidate OR already bound),
    drops candidate-less unbound ones, and sorts by call-site count descending
    then name (highest-impact first, regardless of bound state)."""
    candidates = {"IA": ("X", "Y"), "IB": (), "IC": ("Z",), "ID": ()}
    sites = {"IA": 5, "IB": 2, "IC": 9, "ID": 1}
    bound = {"IB": "BImpl"}  # IB has no candidates but is bound → kept + clearable
    entries = _build_binding_entries(candidates, sites, bound)
    # ID (no candidates, not bound) is omitted; the rest sort by call-site count
    # descending: IC(9), IA(5), IB(2) — bound IB is NOT pulled to the front.
    assert [e.interface for e in entries] == ["IC", "IA", "IB"]
    assert entries[1].interface == "IA"
    assert entries[1].call_sites == 5
    ib = entries[2]
    assert ib.bound_to == "BImpl"
    assert ib.candidates == ()  # bound to an out-of-candidate contract, still shown


def test_bindings_panel_renders_with_seeded_binding(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> None:
    """A seeded binding makes the panel render its row with the bound contract
    selected (even when the interface has no detected candidate, so the bound
    value stays visible and clearable), the Save control, and the no-JS Set
    button."""
    app = create_app(
        solmate_facts,
        solmate_flows,
        scope=Scope(interface_bindings=(("IFoo", "Bar"),)),
    )
    app.config.update(TESTING=True)
    html = app.test_client().get("/").get_data(as_text=True)
    assert 'id="bindings"' in html
    assert "Save to solflow.toml" in html
    assert "IFoo" in html
    assert 'value="Bar" selected' in html  # out-of-candidate bound value kept
    assert "bindings-set" in html  # the no-JS fallback submit


def test_save_bindings_route_writes_and_redirects(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...], tmp_path
) -> None:
    target = tmp_path / "solflow.toml"
    app = create_app(
        solmate_facts,
        solmate_flows,
        scope=Scope(interface_bindings=(("IFoo", "Bar"),)),
        config_path=target,
    )
    app.config.update(TESTING=True)
    resp = app.test_client().post("/bindings/save")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/?saved=ok#bindings"
    text = target.read_text()
    assert "[bindings]" in text
    assert 'IFoo = "Bar"' in text


def test_save_bindings_route_is_surgical(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...], tmp_path
) -> None:
    target = tmp_path / "solflow.toml"
    target.write_text("# hand-written\n[scope]\nexclude_paths = " '["**/*.t.sol"]\n')
    app = create_app(
        solmate_facts,
        solmate_flows,
        scope=Scope(interface_bindings=(("IFoo", "Bar"),)),
        config_path=target,
    )
    app.config.update(TESTING=True)
    app.test_client().post("/bindings/save")
    text = target.read_text()
    assert "# hand-written" in text  # comment preserved
    assert "[scope]" in text  # other table preserved
    assert 'IFoo = "Bar"' in text  # bindings written


def test_save_bindings_route_error_is_loud(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...], tmp_path
) -> None:
    # An unwritable target (parent directory does not exist) is reported as a
    # ?saved=error note, not a 500, and no file is created.
    target = tmp_path / "nope" / "solflow.toml"
    app = create_app(
        solmate_facts,
        solmate_flows,
        scope=Scope(interface_bindings=(("IFoo", "Bar"),)),
        config_path=target,
    )
    app.config.update(TESTING=True)
    resp = app.test_client().post("/bindings/save")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/?saved=error#bindings"
    assert not target.exists()


def test_bind_from_index_redirects_to_panel_fragment(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> None:
    """The index panel's per-row form points /bind/ back at /#bindings; the
    fragment survives _safe_next so the page returns to the panel."""
    app = create_app(solmate_facts, solmate_flows, scope=Scope())
    app.config.update(TESTING=True)
    resp = app.test_client().get("/bind/IFoo?contract=Bar&next=/%23bindings")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/#bindings"


# ---------------------------------------------------------------------------
# Index listing order by Flow node count (v0.18.0, spec §8.3)
# ---------------------------------------------------------------------------


class _FakeNode:
    """Minimal stand-in for ``_flow_node_count`` (it only reads ``.children``)."""

    def __init__(self, *children: _FakeNode) -> None:
        self.children = children


def test_flow_node_count_counts_root_plus_all_subnodes() -> None:
    assert _flow_node_count(_FakeNode()) == 1  # lone root
    # root + two children, the second with its own child → 4 nodes total.
    assert _flow_node_count(_FakeNode(_FakeNode(), _FakeNode(_FakeNode()))) == 4
    # A terminal node type (no ``children`` attribute at all) counts as 1.
    assert _flow_node_count(object()) == 1  # type: ignore[arg-type]


def test_index_orders_contracts_and_entries_by_node_count(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> None:
    """Contracts within a group are ordered by total Flow node count descending;
    within a contract each mutability bucket is likewise ordered descending; and
    a contract's ``node_count`` equals the sum across its entry points."""
    groups, _, _, _ = build_index(solmate_facts, solmate_flows)
    for group in groups:
        contract_counts = [c.node_count for c in group.contracts]
        assert contract_counts == sorted(
            contract_counts, reverse=True
        ), f"contracts in {group.label} must be heaviest-first by node count"
        for contract in group.contracts:
            summed = sum(
                ep.node_count
                for ep in (
                    *contract.mutating_entry_points,
                    *contract.read_only_entry_points,
                )
            )
            assert contract.node_count == summed
            for bucket in (
                contract.mutating_entry_points,
                contract.read_only_entry_points,
            ):
                counts = [ep.node_count for ep in bucket]
                assert counts == sorted(
                    counts, reverse=True
                ), f"{contract.name} entries must be heaviest-first by node count"
