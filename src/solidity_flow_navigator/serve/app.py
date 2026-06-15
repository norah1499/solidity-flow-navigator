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
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import flask.cli
from flask import (
    Flask,
    Response,
    abort,
    make_response,
    redirect,
    render_template,
    request,
)
from markupsafe import Markup

from ..analysis.types import RepoFacts
from ..flow.builder import build_flows
from ..flow.scope import DEFAULT_SCOPE, Scope
from ..flow.types import Flow, FunctionNode
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
    # v0.11.0 per-entry modifier chips (spec §8.3): names of the Flow root's
    # modifier children, in application order. Populated for every entry;
    # the template renders chips on Mutating rows only (the deliberate
    # asymmetry recorded in §8.3 — no chips on a mutating row IS the
    # unprotected-entry-point signal).
    modifier_names: tuple[str, ...]


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
                    modifier_names=_root_modifier_names(flow),
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


def _root_modifier_names(flow: Flow) -> tuple[str, ...]:
    """Names of the Flow root's modifier children, in application order.

    Modifier children are the leading FunctionNode children with
    ``is_modifier=True`` (§11.6 folds them in before body-call children).
    Unresolved modifiers (``ABSTRACT_NO_IMPLEMENTATION``) have no resolved
    name and are skipped — the Flow page itself shows the unresolved pill.
    """
    return tuple(
        c.name
        for c in flow.root.children
        if isinstance(c, FunctionNode) and c.is_modifier
    )


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

# v0.14.0 theme control (spec §8.3). The header's Auto / Light / Dark links hit
# ``/theme/<value>``: "light"/"dark" set this cookie, "auto" clears it. The
# value is read back per-request in the context processor and stamped as
# ``data-theme`` on ``<html>``, so the choice survives navigation with no
# client-side JavaScript (the index stays static). The dark palette applies
# under both ``prefers-color-scheme`` (Auto) and ``[data-theme="dark"]``
# (forced) in main.css.
THEME_COOKIE = "solflow_theme"
THEME_VALUES = ("auto", "light", "dark")
THEME_MAX_AGE = 60 * 60 * 24 * 365  # one year

# v0.15.0 bookmarks (spec §8.3). The per-row / per-heading ribbon toggles hit
# ``/bookmark/<kind>/<id>``, which adds or removes the id from this first-party,
# localhost-only cookie. The value is a JSON list of prefixed ids — ``e:<url_id>``
# for an entry point, ``c:<contract>`` for a contract — read back per request and
# exposed to templates as ``bookmarked_ids`` (filled vs outline state) and, on the
# index, materialized into the ``Bookmarked`` section. Like the theme cookie the
# whole mechanism is server-side, so the index stays JavaScript-free.
BOOKMARK_COOKIE = "solflow_bookmarks"
BOOKMARK_MAX_AGE = 60 * 60 * 24 * 365  # one year
# Defensive ceiling so a runaway cookie can't grow without bound. Browsers cap a
# single cookie near 4 KB; a few dozen real bookmarks sit comfortably under that,
# and the cap simply drops the oldest ids past the limit.
BOOKMARK_MAX = 100

# v0.15.0 "recently viewed" (spec §8.3). Opening a Flow page records its entry id
# in this cookie; the index renders those rows with a distinct tint so the auditor
# sees at a glance which entry points they've already opened. Server-side (no JS)
# and chosen over the browser's `:visited` state because :visited can't be reliably
# rendered/verified in every browser and its CSS is restricted to a faint wash —
# this gives a clear, controllable cue. Capped to the most-recent ids to stay well
# under the ~4 KB cookie limit ("recently viewed", oldest drop off).
VIEWED_COOKIE = "solflow_viewed"
VIEWED_MAX_AGE = 60 * 60 * 24 * 365  # one year
VIEWED_MAX = 60


# ---------------------------------------------------------------------------
# Interface binding candidates (spec §13.2) — feed the Flow-page node dropdown
# ---------------------------------------------------------------------------


def _interface_call_sites(facts: RepoFacts) -> dict[str, int]:
    """Interface type → number of high-level call sites that target it (§13.2).

    A call site counts when a ``kind="high_level"`` edge's resolved declarer —
    or its static call-site type — is an interface contract. These are the
    interfaces a binding can resolve, i.e. the ones a Flow node can offer a
    bind dropdown for.
    """
    by_name = {c.name: c for c in facts.contracts}
    funcs = {f.canonical_name: f for c in facts.contracts for f in c.functions}
    counts: dict[str, int] = {}
    for contract in facts.contracts:
        for f in contract.functions:
            for e in f.calls:
                if e.kind != "high_level":
                    continue
                iface: str | None = None
                if e.is_resolved and e.target_canonical_name:
                    target = funcs.get(e.target_canonical_name)
                    if target is not None:
                        decl = by_name.get(target.contract_declarer_name)
                        if decl is not None and decl.is_interface:
                            iface = target.contract_declarer_name
                if iface is None and e.target_contract_name:
                    decl = by_name.get(e.target_contract_name)
                    if decl is not None and decl.is_interface:
                        iface = e.target_contract_name
                if iface:
                    counts[iface] = counts.get(iface, 0) + 1
    return counts


def _explicit_implementers(facts: RepoFacts, iface: str) -> list[str]:
    """Concrete contracts that explicitly inherit ``iface`` (the high-confidence
    binding candidates), sorted by name."""
    return sorted(
        c.name
        for c in facts.contracts
        if c.kind == "contract"
        and not c.is_abstract
        and iface in c.linearized_base_contract_names
    )


def _implemented_signatures(facts: RepoFacts) -> dict[str, frozenset[str]]:
    """For each concrete contract, the set of function ``full_name``s it
    implements across its C3 chain. Precomputed once so structural candidate
    matching (§13.2) is a cheap subset test rather than a repeated walk."""
    by_name = {c.name: c for c in facts.contracts}
    out: dict[str, frozenset[str]] = {}
    for contract in facts.contracts:
        if contract.kind != "contract" or contract.is_abstract:
            continue
        sigs: set[str] = set()
        for cn in (contract.name,) + contract.linearized_base_contract_names:
            base = by_name.get(cn)
            if base is None:
                continue
            sigs.update(f.full_name for f in base.functions if f.is_implemented)
        out[contract.name] = frozenset(sigs)
    return out


def _interface_methods(facts: RepoFacts, iface: str) -> frozenset[str]:
    """The function signatures ``iface`` declares, including those inherited
    from base interfaces. A concrete contract is a structural implementer when
    it implements all of these."""
    by_name = {c.name: c for c in facts.contracts}
    contract = by_name.get(iface)
    if contract is None:
        return frozenset()
    methods: set[str] = set()
    for cn in (iface,) + contract.linearized_base_contract_names:
        base = by_name.get(cn)
        if base is None or not base.is_interface:
            continue
        methods.update(f.full_name for f in base.functions)
    return frozenset(methods)


def _candidate_contracts(
    facts: RepoFacts, iface: str, impl_sigs: dict[str, frozenset[str]]
) -> tuple[str, ...]:
    """Concrete contracts that could implement ``iface`` (§13.2): those that
    explicitly inherit it, plus those that structurally implement ALL of its
    declared methods.

    Deliberately NOT every contract in the repo — an interface with no real
    in-scope implementer (a cheatcode interface like ``Vm``, a callback
    receiver nothing implements) shows an empty list, and the auditor binds an
    unlisted contract via ``--bind`` / the config table if they really need to.
    """
    explicit = set(_explicit_implementers(facts, iface))
    methods = _interface_methods(facts, iface)
    structural = (
        {name for name, sigs in impl_sigs.items() if methods <= sigs}
        if methods
        else set()
    )
    return tuple(sorted(explicit | structural))


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

    # v0.15.0 bookmarks: register the anchor-slug filter the templates use for
    # row ``id``s and the ``next`` fragment.
    app.jinja_env.filters["anchor_slug"] = _anchor_slug

    # v0.17.0 (§13.2): candidate contracts per interface for the Flow-page bind
    # dropdown. Depends only on the (immutable) facts, so compute once.
    _impl_sigs = _implemented_signatures(facts)
    candidates_by_iface: dict[str, tuple[str, ...]] = {
        iface: _candidate_contracts(facts, iface, _impl_sigs)
        for iface in _interface_call_sites(facts)
    }

    # v0.17.0 (§13.2): the binding map is the one Scope field editable at
    # runtime. The server retains the Layer 1 facts and, when a binding
    # changes, re-runs Layer 2 (build_flows) — sub-second, no recompile — then
    # rebuilds the index and per-Flow lookups in place. These are reassigned
    # via ``nonlocal`` in ``_rebuild`` so the route closures below always read
    # the current values. The other four scope rules stay fixed for the
    # process lifetime (§12).
    current_scope: Scope = scope
    flow_by_url_id: dict[str, Flow] = {}
    groups: tuple[_GroupEntry, ...] = ()
    total_eps = total_contracts = total_unresolved = 0
    entry_by_url_id: dict[str, _EntryPointEntry] = {}
    contract_by_name: dict[str, _ContractEntry] = {}

    def _rebuild(active_scope: Scope, prebuilt: tuple[Flow, ...] | None = None) -> None:
        nonlocal current_scope, flow_by_url_id, groups
        nonlocal total_eps, total_contracts, total_unresolved
        nonlocal entry_by_url_id, contract_by_name
        these = prebuilt if prebuilt is not None else build_flows(facts, active_scope)
        current_scope = active_scope
        flow_by_url_id = {f.entry_point_invoker_canonical_name: f for f in these}
        groups, total_eps, total_contracts, total_unresolved = build_index(facts, these)
        entry_by_url_id = {}
        contract_by_name = {}
        for _group in groups:
            for _contract in _group.contracts:
                contract_by_name[_contract.name] = _contract
                for _ep in (
                    *_contract.mutating_entry_points,
                    *_contract.read_only_entry_points,
                ):
                    entry_by_url_id[_ep.url_id] = _ep

    # Seed from the flows the CLI already built with this same scope (avoids a
    # redundant startup build_flows). Subsequent binding changes rebuild live.
    _rebuild(scope, prebuilt=flows)

    # Common context for every rendered template.
    @app.context_processor
    def _inject_globals() -> dict[str, Any]:
        # v0.14.0: theme is read per-request from the cookie the set_theme
        # route writes. ``theme`` (None | "light" | "dark") drives the
        # data-theme attribute on <html>; ``current_theme`` (always one of
        # "auto"/"light"/"dark") marks the active segment in the header
        # control. An absent or unrecognized cookie falls back to Auto.
        cookie = request.cookies.get(THEME_COOKIE)
        forced = cookie if cookie in ("light", "dark") else None
        # v0.15.0: the set of bookmarked ids drives the filled-vs-outline ribbon
        # state on every contract heading, entry row, and the Flow-page toggle.
        # A frozenset so templates can do O(1) ``("e:" + url_id) in bookmarked_ids``.
        bookmarked_ids = frozenset(
            _parse_bookmarks(request.cookies.get(BOOKMARK_COOKIE))
        )
        return {
            "repo_path": facts.repo_path,
            "expand_all": expand_all,
            "theme": forced,
            "current_theme": forced or "auto",
            "bookmarked_ids": bookmarked_ids,
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
        # v0.15.0: resolve the cookie's bookmark ids (in bookmark order) against
        # this analysis. Ids that match nothing in the current repo are skipped,
        # so bookmarks set against a different codebase neither error nor appear.
        ids = _parse_bookmarks(request.cookies.get(BOOKMARK_COOKIE))
        bookmarked_entries = [
            entry_by_url_id[i[2:]]
            for i in ids
            if i.startswith("e:") and i[2:] in entry_by_url_id
        ]
        bookmarked_contracts = [
            contract_by_name[i[2:]]
            for i in ids
            if i.startswith("c:") and i[2:] in contract_by_name
        ]
        # v0.15.0: ids of entry points opened recently (the Flow route records
        # them). Used to tint already-viewed rows. A frozenset for O(1) lookup.
        viewed_ids = frozenset(_parse_viewed(request.cookies.get(VIEWED_COOKIE)))
        return render_template(
            "index.html",
            groups=groups,
            total_entry_points=total_eps,
            total_contracts=total_contracts,
            total_unresolved=total_unresolved,
            scope=current_scope,
            bookmarked_entries=bookmarked_entries,
            bookmarked_contracts=bookmarked_contracts,
            viewed_ids=viewed_ids,
        )

    @app.route("/theme/<value>")
    def set_theme(value: str) -> Response:
        # v0.14.0 (spec §8.3). "auto" clears the cookie so the OS preference
        # governs again (prefers-color-scheme); "light"/"dark" force a palette.
        # Any other value is a bad URL → 404. The redirect returns to the page
        # the control was on (``next``), validated to a local path so the
        # endpoint can't be used as an open redirect.
        if value not in THEME_VALUES:
            abort(404)
        response = redirect(_safe_next(request.args.get("next")))
        if value == "auto":
            response.delete_cookie(THEME_COOKIE)
        else:
            response.set_cookie(
                THEME_COOKIE, value, max_age=THEME_MAX_AGE, samesite="Lax"
            )
        return response

    @app.route("/bookmark/<kind>/<path:ident>")
    def toggle_bookmark(kind: str, ident: str) -> Response:
        # v0.15.0 (spec §8.3). ``kind`` is ``entry`` or ``contract``; anything
        # else is a bad URL → 404 (never a silent no-op). The id is toggled in
        # the cookie's JSON list and the route redirects to the originating row
        # (``next``, validated to a local path by ``_safe_next`` exactly like the
        # theme route, with the row's fragment anchor so the page returns to it).
        if kind not in ("entry", "contract"):
            abort(404)
        decoded = urllib.parse.unquote(ident)
        key = ("e:" if kind == "entry" else "c:") + decoded
        current = _parse_bookmarks(request.cookies.get(BOOKMARK_COOKIE))
        if key in current:
            current = [c for c in current if c != key]
        else:
            # Append (preserve bookmark order); bound to the last BOOKMARK_MAX.
            current = (current + [key])[-BOOKMARK_MAX:]
        response = redirect(_safe_next(request.args.get("next")))
        if current:
            response.set_cookie(
                BOOKMARK_COOKIE,
                json.dumps(current),
                max_age=BOOKMARK_MAX_AGE,
                samesite="Lax",
            )
        else:
            response.delete_cookie(BOOKMARK_COOKIE)
        return response

    @app.route("/bind/<path:iface>")
    def set_binding(iface: str) -> Response:
        # v0.17.0 (spec §10.2, §13.2). The Flow-page node dropdown GETs here
        # with ?contract=<name> (or the "__none__" sentinel to clear). We
        # replace this interface's binding, re-run Layer 2 against the retained
        # facts (no recompile), and redirect back (``next``, validated to a
        # local path) — the Flow reloads with the node resolved.
        iface = urllib.parse.unquote(iface)
        contract = request.args.get("contract", "")
        pairs = [(i, c) for (i, c) in current_scope.interface_bindings if i != iface]
        if contract and contract != "__none__":
            pairs.append((iface, contract))
        _rebuild(replace(current_scope, interface_bindings=tuple(pairs)))
        return redirect(_safe_next(request.args.get("next")))

    @app.route("/flow/<path:url_id>")
    def flow_page(url_id: str) -> Response:
        decoded = urllib.parse.unquote(url_id)
        flow = flow_by_url_id.get(decoded)
        if flow is None:
            abort(404)
        # v0.17.0 (§10.2, §13.2): give the serializer the candidates and current
        # bindings so interface-call nodes carry the inline bind dropdown.
        bindings_ctx = {
            "candidates": candidates_by_iface,
            "bound": dict(current_scope.interface_bindings),
        }
        flow_dict = serialize_flow(flow, bindings_ctx)
        response = make_response(
            render_template(
                "flow.html",
                flow=flow_dict,
                flow_json=Markup(_safe_json(flow_dict)),
                # v0.15.0: the entry's url_id keys the flow-header bookmark toggle.
                entry_url_id=decoded,
            )
        )
        # v0.15.0: record this entry as recently viewed (moved to most-recent),
        # so the index can tint already-opened rows. Server-side, no JS.
        prior = _parse_viewed(request.cookies.get(VIEWED_COOKIE))
        viewed = [v for v in prior if v != decoded]
        viewed = (viewed + [decoded])[-VIEWED_MAX:]
        response.set_cookie(
            VIEWED_COOKIE,
            json.dumps(viewed),
            max_age=VIEWED_MAX_AGE,
            samesite="Lax",
        )
        return response

    # v0.10.4: render 404s in the tool's own chrome instead of Flask's bare
    # default page. Status code stays 404; only the body changes.
    @app.errorhandler(404)
    def _not_found(_error):  # type: ignore[no-untyped-def]
        return render_template("not_found.html"), 404

    return app


def _safe_json(obj: Any) -> str:
    """JSON-encode for inline ``<script type="application/json">`` embedding.

    The script type is non-executable, but a stray ``</script>`` substring
    inside a string literal would still terminate the tag, and ``<!--`` /
    ``<script`` confuse HTML-comment-eating browsers. Emitting every ``<``
    as the JSON escape ``\\u003c`` defangs all three at once. ``<`` only
    occurs inside string literals (JSON syntax itself never contains it),
    and ``\\u003c`` is *valid* JSON — unlike backslash-escaping the raw
    characters — so the client's ``JSON.parse`` round-trips the payload
    unchanged even when the analyzed source contains those substrings.
    """
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def _safe_next(raw: str | None) -> str:
    """Validate a theme-toggle ``next`` redirect target to a local path.

    The header control passes the current page as ``?next=`` so toggling
    returns you where you were. Only same-origin absolute paths are honored —
    the value must start with a single ``/``; external URLs and
    protocol-relative ``//host`` targets fall back to the index. solflow only
    ever binds localhost, but refusing to bounce to an attacker-supplied URL
    is cheap hygiene and keeps the endpoint from being a generic open redirect.
    """
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


def _anchor_slug(value: str) -> str:
    """Sanitize an id into an HTML-``id`` / URL-fragment safe slug.

    Entry ``url_id``s carry parens and commas (``ERC721.safeTransferFrom(address,
    ...)``); contract names are plain identifiers. Collapsing every run of
    non-alphanumerics to a single ``-`` yields a stable, fragment-safe anchor —
    used both for the row's ``id`` and for the ``next`` fragment the bookmark
    toggle redirects to, so the page returns to the row rather than the top.
    Overloaded entries differ in their parameter list, so their slugs differ.
    Exposed as the ``anchor_slug`` Jinja filter (registered in ``create_app``).
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")


def _parse_bookmarks(raw: str | None) -> list[str]:
    """Decode the ``solflow_bookmarks`` cookie into an ordered id list.

    The cookie holds a JSON array of prefixed ids (``e:`` / ``c:``). Anything
    malformed — not JSON, not a list, junk entries — degrades to an empty list
    rather than raising, so a corrupt or hand-edited cookie can never 500 a
    request. Order (bookmark order) and uniqueness are preserved; the list is
    capped at ``BOOKMARK_MAX`` so a runaway cookie can't grow without bound.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if (
            isinstance(item, str)
            and (item.startswith("e:") or item.startswith("c:"))
            and item not in seen
        ):
            seen.add(item)
            out.append(item)
            if len(out) >= BOOKMARK_MAX:
                break
    return out


def _parse_viewed(raw: str | None) -> list[str]:
    """Decode the ``solflow_viewed`` cookie into an ordered url_id list.

    A JSON array of entry url_ids, oldest first. Malformed input degrades to an
    empty list (a corrupt cookie never 500s a request). Duplicates are dropped and
    the list is capped at ``VIEWED_MAX`` ("recently viewed").
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
    return out[-VIEWED_MAX:]


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
