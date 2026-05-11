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

import pytest
from flask.testing import FlaskClient

from solidity_flow_navigator.analysis.types import RepoFacts
from solidity_flow_navigator.flow.types import Flow
from solidity_flow_navigator.serve.app import create_app, flow_url_id


@pytest.fixture(scope="module")
def client(solmate_facts: RepoFacts, solmate_flows: tuple[Flow, ...]) -> FlaskClient:
    """Build the app once per module; reuse a single test client across tests."""
    app = create_app(solmate_facts, solmate_flows)
    app.config.update(TESTING=True)
    return app.test_client()


# -- index ------------------------------------------------------------------


def test_index_status_and_groups(client: FlaskClient) -> None:
    rv = client.get("/")
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


# -- flow page --------------------------------------------------------------


def test_flow_page_known_url_id(
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    target = next(
        f
        for f in solmate_flows
        if f.entry_point_canonical_name == "ERC4626.deposit(uint256,address)"
        and f.entry_point_contract_name == "ERC4626"
    )
    rv = client.get(f"/flow/{flow_url_id(target)}")
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
    client: FlaskClient, solmate_flows: tuple[Flow, ...]
) -> None:
    """The Stage 2 collision fix: MockOwned and Owned both have canonical
    ``Owned.transferOwnership(address)`` but distinct flow_url_ids."""
    bare = next(
        f
        for f in solmate_flows
        if f.entry_point_contract_name == "Owned"
        and f.entry_point_function_name == "transferOwnership"
    )
    inherited = next(
        f
        for f in solmate_flows
        if f.entry_point_contract_name == "MockOwned"
        and f.entry_point_function_name == "transferOwnership"
    )
    bare_rv = client.get(f"/flow/{flow_url_id(bare)}")
    inh_rv = client.get(f"/flow/{flow_url_id(inherited)}")
    assert bare_rv.status_code == 200
    assert inh_rv.status_code == 200
    # The inherited flow's page header carries the subtitle.
    assert "Inherited entry point" in inh_rv.get_data(as_text=True)
    assert "Inherited entry point" not in bare_rv.get_data(as_text=True)


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
