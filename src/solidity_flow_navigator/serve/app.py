"""Flask application factory and route definitions.

Layer 3 entry point. The app is constructed with already-built data — Layer 1
facts and the tuple of Flows from Layer 2 — so that compilation happens once
in the CLI before the server starts. The Flask process holds everything in
memory; there is no JSON API and no async re-fetching.

Routes:
    GET /                  — index page, entry points grouped by source path.
    GET /flow/<canonical>  — per-Flow page (Stage 1: placeholder; Stage 2: real).

Static assets (CSS, JS, vendored libraries) are served by Flask's default
static handler from ``serve/static/``.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flask.cli
from flask import Flask, abort, render_template
from markupsafe import Markup

from ..analysis.types import RepoFacts
from ..flow.types import Flow
from .highlight import write_pygments_css
from .serializer import serialize_flow

# ---------------------------------------------------------------------------
# Path categorization
# ---------------------------------------------------------------------------

# Group labels are exposed in the index template; keeping them here so the
# template doesn't hardcode strings that have meaning in the categorization.
GROUP_APPLICATION = "Application"
GROUP_TESTS = "Tests"
GROUP_DEPENDENCIES = "Dependencies"

# Order matters: this is the order groups render in. First match wins.
GROUP_ORDER = (GROUP_APPLICATION, GROUP_TESTS, GROUP_DEPENDENCIES)


def categorize_path(rel_path: str) -> str:
    """Classify a repo-relative source path into one of three index groups.

    Categorization is by *contract* source location, not function source
    location: a mock contract under ``src/test/utils/mocks/`` that inherits a
    function declared under ``src/tokens/`` belongs in Tests, because the
    auditor reasons about the deployed contract, not the inherited body.

    Rules (first match wins):
        * ``lib/...`` or ``node_modules/...``  → Dependencies
        * ``src/test/...``                      → Tests
        * everything else                       → Application
    """
    if rel_path.startswith("lib/") or rel_path.startswith("node_modules/"):
        return GROUP_DEPENDENCIES
    if rel_path.startswith("src/test/"):
        return GROUP_TESTS
    return GROUP_APPLICATION


# ---------------------------------------------------------------------------
# Index data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EntryPointEntry:
    # The URL-level identifier: ``flow.entry_point_invoker_canonical_name``.
    # Unique across all Flows (Layer 2 keys it on the invoking contract, so
    # MockOwned.transferOwnership and Owned.transferOwnership get distinct
    # canonicals).
    url_id: str
    contract_name: str
    function_full_name: str


@dataclass(frozen=True, slots=True)
class _ContractEntry:
    name: str
    source_path: str
    # Partitioned per spec §8.3: non-view, non-pure roots vs view-or-pure roots.
    # Empty tuples render nothing — the template omits the section header rather
    # than emitting a "Read-only (none)" placeholder.
    mutating_entry_points: tuple[_EntryPointEntry, ...]
    read_only_entry_points: tuple[_EntryPointEntry, ...]


@dataclass(frozen=True, slots=True)
class _GroupEntry:
    label: str
    contracts: tuple[_ContractEntry, ...]


def _contract_source_paths(facts: RepoFacts) -> dict[str, str]:
    """Map ``Contract.name`` → repo-relative source path.

    Used to categorize each entry point by the path of the contract it hangs
    off, which is more meaningful for an auditor than the path of the function
    body (which may live in a parent contract via inheritance).
    """
    return {c.name: c.source_location.filename_relative for c in facts.contracts}


def build_index(
    facts: RepoFacts, flows: Iterable[Flow]
) -> tuple[tuple[_GroupEntry, ...], int, int]:
    """Group flows by category → contract → entry point, sub-grouped by mutability.

    Returns ``(groups, total_entry_points, total_contracts)``. Empty groups
    are still returned (so the template can decide whether to show them).
    Within a group, contracts are sorted alphabetically; within a contract,
    each mutability bucket is sorted by full_name independently (spec §8.3).
    """
    contract_paths = _contract_source_paths(facts)

    # group_label -> contract_name -> list[(flow, _EntryPointEntry)]
    # We carry the Flow alongside the entry so the mutability partition below
    # can read ``flow.root.view`` / ``flow.root.pure`` without re-walking flows.
    by_group: dict[str, dict[str, list[tuple[Flow, _EntryPointEntry]]]] = {
        label: {} for label in GROUP_ORDER
    }

    total = 0
    for flow in flows:
        cn = flow.entry_point_contract_name
        # Fall back to the root function's source path if we somehow can't
        # resolve a contract path (e.g. synthetic contract). Keeps the index
        # honest rather than silently dropping entries.
        path = contract_paths.get(cn, flow.root.source_location.filename_relative)
        label = categorize_path(path)
        bucket = by_group[label].setdefault(cn, [])
        full_name = flow.entry_point_function_name + _signature_suffix(flow)
        bucket.append(
            (
                flow,
                _EntryPointEntry(
                    url_id=flow.entry_point_invoker_canonical_name,
                    contract_name=cn,
                    function_full_name=full_name,
                ),
            )
        )
        total += 1

    groups: list[_GroupEntry] = []
    contract_count = 0
    for label in GROUP_ORDER:
        contracts: list[_ContractEntry] = []
        for cn in sorted(by_group[label]):
            pairs = sorted(by_group[label][cn], key=lambda fe: fe[1].function_full_name)
            mutating: list[_EntryPointEntry] = []
            read_only: list[_EntryPointEntry] = []
            for flow, ep in pairs:
                if flow.root.view or flow.root.pure:
                    read_only.append(ep)
                else:
                    mutating.append(ep)
            path = contract_paths.get(cn, "")
            contracts.append(
                _ContractEntry(
                    name=cn,
                    source_path=path,
                    mutating_entry_points=tuple(mutating),
                    read_only_entry_points=tuple(read_only),
                )
            )
            contract_count += 1
        groups.append(_GroupEntry(label=label, contracts=tuple(contracts)))

    return tuple(groups), total, contract_count


def _signature_suffix(flow: Flow) -> str:
    """Extract the ``(...)`` part of the canonical name, if any.

    Falls back to empty if the canonical doesn't include parens (e.g. for
    receive/fallback, where Layer 2 may emit a different shape).
    """
    canon = flow.entry_point_invoker_canonical_name
    idx = canon.find("(")
    return canon[idx:] if idx != -1 else ""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(facts: RepoFacts, flows: tuple[Flow, ...]) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # Pygments CSS is regenerated each startup so it tracks the installed
    # Pygments version. Written into the same static dir Flask serves.
    write_pygments_css(Path(app.static_folder))

    # Indexed by ``entry_point_invoker_canonical_name`` — Layer 2 keys it on
    # the invoking contract, so it is unique across all Flows.
    flow_by_url_id: dict[str, Flow] = {
        f.entry_point_invoker_canonical_name: f for f in flows
    }
    groups, total_eps, total_contracts = build_index(facts, flows)

    # Common context for every rendered template.
    @app.context_processor
    def _inject_globals() -> dict[str, Any]:
        return {"repo_path": facts.repo_path}

    @app.route("/")
    def index() -> str:
        return render_template(
            "index.html",
            groups=groups,
            total_entry_points=total_eps,
            total_contracts=total_contracts,
        )

    @app.route("/flow/<path:url_id>")
    def flow_page(url_id: str) -> str:
        decoded = urllib.parse.unquote(url_id)
        flow = flow_by_url_id.get(decoded)
        if flow is None:
            abort(404)
        flow_dict = serialize_flow(flow)
        return render_template(
            "flow.html",
            flow=flow_dict,
            flow_json=Markup(_safe_json(flow_dict)),
        )

    return app


def _safe_json(obj: Any) -> str:
    """JSON-encode for inline ``<script type="application/json">`` embedding.

    The script type is non-executable, but we still defang ``</`` so a stray
    ``</script>`` substring inside a string literal can't terminate the tag.
    The other escapes (``<!--``, ``<script``) are belt-and-braces against
    HTML-comment-eating browsers.
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("<script", "<\\script")
    )


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------


def _check_port_available(host: str, port: int) -> None:
    """Raise OSError if the host:port pair is not bindable.

    A pre-bind is racy by definition (someone could grab the port between
    here and ``app.run``), but for a single-user local tool it converts
    the common case (port already taken by a stale solflow) into a clean
    error message instead of a Werkzeug traceback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


def run_server(app: Flask, host: str, port: int) -> None:
    """Start the Flask development server, suppressing chrome.

    We silence:
      * Werkzeug's "WARNING: This is a development server" banner — appropriate
        production guidance, but solflow IS a development server by design and
        the warning is just noise on every startup.
      * Werkzeug's per-request access logs — the use case is one user clicking
        around their own browser; access logs are clutter.
    """
    flask.cli.show_server_banner = lambda *_args, **_kwargs: None  # type: ignore[assignment]
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=host, port=port, debug=False, use_reloader=False)
