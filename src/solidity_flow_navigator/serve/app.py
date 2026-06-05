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
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flask.cli
from flask import Flask, abort, render_template
from markupsafe import Markup

from ..analysis.types import RepoFacts
from ..flow.scope import DEFAULT_SCOPE, Scope
from ..flow.types import Flow
from .highlight import highlight_signature, write_pygments_css
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
    # Pygments-rendered HTML for the declaration form
    # ``function name(types) external[ view]``. The template inlines this
    # under a ``<code class="src">`` so the existing ``.src .X`` palette
    # in ``main.css`` colours the function name purple and keywords/types
    # dark blue — same visual language used inside Flow-page source bodies.
    # See spec §8.3.
    signature_html: str
    # v0.8.0 per-entry metadata (spec §8.3 per-entry metadata column).
    # ``unresolved_count``: number of UnresolvedNode descendants in this
    # Flow; the template renders ``N unr`` only when > 0.
    # ``max_depth``: longest edge-distance from root to any leaf; the
    # template renders ``dN`` unconditionally.
    unresolved_count: int
    max_depth: int


@dataclass(frozen=True, slots=True)
class _ContractEntry:
    name: str
    source_path: str
    # Partitioned per spec §8.3: non-view, non-pure roots vs view-or-pure roots.
    # Empty tuples render nothing — the template omits the section header rather
    # than emitting a "Read-only (none)" placeholder.
    mutating_entry_points: tuple[_EntryPointEntry, ...]
    read_only_entry_points: tuple[_EntryPointEntry, ...]
    # v0.8.0 pre-computed cardinalities so the template doesn't have to call
    # ``|length`` filters on each render; the per-section count suffix
    # (``Mutating · N``) reads these directly. Spec §8.3 per-section totals.
    mutating_count: int
    read_only_count: int


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
) -> tuple[tuple[_GroupEntry, ...], int, int, int]:
    """Group flows by category → contract → entry point, sub-grouped by mutability.

    Returns ``(groups, total_entry_points, total_contracts, total_unresolved)``.
    ``total_unresolved`` is the sum of ``Flow.unresolved_count`` across every
    Flow — the index header's "trust budget" figure per spec §8.3.

    Empty groups are still returned (so the template can decide whether to
    show them). Within a group, contracts are sorted alphabetically; within
    a contract, each mutability bucket is sorted by full_name independently
    (spec §8.3).
    """
    contract_paths = _contract_source_paths(facts)

    # group_label -> contract_name -> list[(flow, full_name)]
    # The _EntryPointEntry itself is built later, at partition time, because
    # ``signature_html`` depends on the bucket the entry lands in (mutating
    # entries render with ``external``; read-only entries render with
    # ``external view`` — see §8.3 and the v0.7.0 spec patch).
    by_group: dict[str, dict[str, list[tuple[Flow, str]]]] = {
        label: {} for label in GROUP_ORDER
    }

    total = 0
    total_unresolved = 0
    for flow in flows:
        cn = flow.entry_point_contract_name
        # Fall back to the root function's source path if we somehow can't
        # resolve a contract path (e.g. synthetic contract). Keeps the index
        # honest rather than silently dropping entries.
        path = contract_paths.get(cn, flow.root.source_location.filename_relative)
        label = categorize_path(path)
        bucket = by_group[label].setdefault(cn, [])
        full_name = flow.entry_point_function_name + _signature_suffix(flow)
        bucket.append((flow, full_name))
        total += 1
        total_unresolved += flow.unresolved_count

    groups: list[_GroupEntry] = []
    contract_count = 0
    for label in GROUP_ORDER:
        contracts: list[_ContractEntry] = []
        for cn in sorted(by_group[label]):
            pairs = sorted(by_group[label][cn], key=lambda fn: fn[1])
            mutating: list[_EntryPointEntry] = []
            read_only: list[_EntryPointEntry] = []
            for flow, full_name in pairs:
                is_read_only = flow.root.view or flow.root.pure
                mutability = "external view" if is_read_only else "external"
                sig_html = highlight_signature(
                    flow.entry_point_function_name,
                    _signature_suffix(flow),
                    mutability,
                )
                ep = _EntryPointEntry(
                    url_id=flow.entry_point_invoker_canonical_name,
                    contract_name=cn,
                    function_full_name=full_name,
                    signature_html=sig_html,
                    unresolved_count=flow.unresolved_count,
                    max_depth=flow.max_depth,
                )
                if is_read_only:
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
                    mutating_count=len(mutating),
                    read_only_count=len(read_only),
                )
            )
            contract_count += 1
        groups.append(_GroupEntry(label=label, contracts=tuple(contracts)))

    return tuple(groups), total, contract_count, total_unresolved


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


def create_app(
    facts: RepoFacts,
    flows: tuple[Flow, ...],
    *,
    expand_all: bool = False,
    scope: Scope | None = None,
) -> Flask:
    """Build the Flask application.

    ``expand_all=True`` causes every per-Flow page to render with its full
    call tree expanded at page load — the progressive renderer's initial
    expansion state is set to "everything" instead of root-only (spec
    §10.2 "Full expansion"). The flag is set from the CLI's
    ``--expand-all`` switch and is session-wide: every Flow page reflects
    it. The index page is unaffected. This is not a separate renderer:
    per-line edge anchoring, the §10.3 direction model, and all v0.9
    reorder / anchor passes apply identically. v0.10.0 Stage 2 removed
    the ``legacy`` parameter and the all-at-once renderer it switched to;
    ``--expand-all`` is now the only path to the bird's-eye view.

    ``scope`` is the active ``Scope`` whose raw glob strings drive the
    v0.8.0 index scope summary line (spec §8.3). It is optional — tests
    constructing an app without going through the CLI resolution path can
    omit it; the index then falls back to ``DEFAULT_SCOPE`` so the line
    still reflects something real rather than rendering empty. The CLI
    always passes the resolved Scope explicitly.
    """
    if scope is None:
        scope = DEFAULT_SCOPE
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
    groups, total_eps, total_contracts, total_unresolved = build_index(facts, flows)

    # Common context for every rendered template.
    @app.context_processor
    def _inject_globals() -> dict[str, Any]:
        return {
            "repo_path": facts.repo_path,
            "expand_all": expand_all,
        }

    # solflow is a localhost dev tool; an auditor swapping a JS/CSS file mid-
    # session (or rerunning the CLI after a code change) must see the new
    # bytes on the next reload, not Chrome's cached copy. Blanket no-store on
    # every response is simpler and safer than scoping to /static/* — none of
    # solflow's responses benefit from caching anyway (HTML embeds the Flow
    # data inline; JS/CSS change with every release).
    @app.after_request
    def _no_cache(response):  # type: ignore[no-untyped-def]
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/")
    def index() -> str:
        return render_template(
            "index.html",
            groups=groups,
            total_entry_points=total_eps,
            total_contracts=total_contracts,
            total_unresolved=total_unresolved,
            scope=scope,
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
#
# The pre-bind probe (``_check_port_available``) moved to ``cli._bind_probe``
# as of v0.10.0 Stage 2, alongside the new ``_select_port`` auto-select loop.
# This module is now responsible only for constructing the Flask app and
# running it once the port has been chosen.


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
