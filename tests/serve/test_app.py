"""Smoke tests for the Flask app — Layer 3 routes and static assets.

We use the Flask test client (no real server, no port binding) on top of
the session-scoped solmate fixtures from ``tests/conftest.py``. The tests
cover the v0 success-criteria checks the spec calls out:

  * ``GET /`` returns 200 with the three index group headings.
  * ``GET /flow/<known url_id>`` returns 200 and embeds ``<script id="flow-data">``.
  * ``GET /flow/<unknown>`` returns 404.
  * Static assets (main.css, generated pygments.css, vendored dagre and d3)
    all return 200.

We don't try to run flow.js in a headless browser here — that's covered
by the manual §15.4 walk-through in the Stage 4 commit message.
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
    Equivalent to running ``solflow --no-default-excludes`` against the
    test fixture.
    """
    app = create_app(solmate_facts, solmate_flows_unfiltered)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(scope="module")
def client_legacy(
    solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]
) -> FlaskClient:
    """Variant of ``client`` built with ``legacy=True``.

    Equivalent to running ``solflow --legacy``: the per-Flow page loads
    the all-at-once renderer (``flow.js``) instead of the progressive
    renderer (``flow-progressive.js``). See spec §10.2.
    """
    app = create_app(solmate_facts, solmate_flows, legacy=True)
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
    assert ">Mutating<" in block, "Mutating section missing from ERC4626 block"
    assert ">Read-only<" in block, "Read-only section missing from ERC4626 block"


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
    groups, _, _ = build_index(solmate_facts, solmate_flows)
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
    assert (
        f">{present}<" in block
    ), f"{present} section missing from {contract_name} block"
    assert (
        f">{absent}<" not in block
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
    groups, _, _ = build_index(solmate_facts, solmate_flows)
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
    assert '<script type="application/json" id="flow-data">' in body
    # Vendored libs and frontend script tags.
    assert "/static/vendor/dagre.min.js" in body
    assert "/static/vendor/d3.min.js" in body
    # v0.5 default: the progressive renderer loads instead of flow.js.
    # The legacy renderer is opt-in via solflow --legacy (covered in
    # test_flow_page_legacy_loads_flow_js below).
    assert "/static/js/flow-progressive.js" in body
    assert "/static/js/flow.js" not in body
    # Graph container the JS targets.
    assert 'id="graph-frame"' in body


def test_flow_page_legacy_loads_flow_js(
    client_legacy: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """``solflow --legacy`` swaps the renderer script tag to flow.js.

    The page still embeds the same flow JSON and references the same
    vendored libs — only the renderer choice differs.
    """
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_invoker_canonical_name == "ERC4626.deposit(uint256,address)"
    )
    rv = client_legacy.get(f"/flow/{target.entry_point_invoker_canonical_name}")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "/static/js/flow.js" in body
    assert "/static/js/flow-progressive.js" not in body
    # Sanity: the rest of the page chrome still renders.
    assert '<script type="application/json" id="flow-data">' in body
    assert 'id="graph-frame"' in body


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
    r'<script type="application/json" id="flow-data">(.*?)</script>',
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
    assert '<script type="application/json" id="flow-data">' in owned_body
    assert '<script type="application/json" id="flow-data">' in mock_body

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
    rv = client.get("/flow/Bogus.nonexistent(uint256)")
    assert rv.status_code == 404


# -- static assets ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/static/css/main.css",
        "/static/css/pygments.css",
        "/static/vendor/dagre.min.js",
        "/static/vendor/d3.min.js",
        "/static/js/flow.js",
        "/static/js/flow-progressive.js",
    ],
)
def test_static_assets_served(client: FlaskClient, path: str) -> None:
    rv = client.get(path)
    assert rv.status_code == 200, f"{path} did not return 200"
    assert int(rv.headers.get("Content-Length", "0")) > 0


def test_pygments_css_contains_token_classes(client: FlaskClient) -> None:
    """``write_pygments_css`` ran at app construction; the file should
    define styles for the ``.src`` namespace's token classes."""
    rv = client.get("/static/css/pygments.css")
    body = rv.get_data(as_text=True)
    assert ".src" in body
    # A couple of token classes we know Pygments emits for Solidity output.
    assert ".k" in body or ".kt" in body
