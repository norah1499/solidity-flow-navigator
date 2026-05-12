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
    assert "/static/js/flow.js" in body
    # Graph container the JS targets.
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
