"""User-configured scope rules: include/exclude lists, library inlining,
and in-tree stubbing preferences (spec §11.2).

In v0 the Scope was always ``DEFAULT_SCOPE`` and the dataclass carried no
fields. v0.1 introduced three rule sets; v0.2 adds a fourth:

- ``exclude_paths``: gitignore-style globs matched against each contract's
  ``source_location.filename_relative``. Matching contracts produce no Flows.
- ``exclude_contracts``: gitignore-style globs matched against contract names
  (treated as a single path segment — no ``/`` involved).
- ``inline_libraries``: library name prefixes. A dependency under
  ``lib/{name}/**`` recurses into Layer 2 instead of stubbing as
  ``ExternalNode`` when ``{name}`` appears in this tuple.
- ``stub_paths`` (v0.2): gitignore-style globs matched against each call
  target's ``source_location.filename_relative``. Matching call targets emit
  an ``ExternalNode`` (terminal) instead of recursing — the auditor uses
  this to compress dense in-tree libraries whose internals aren't
  load-bearing for the Flow at hand.

``DEFAULT_SCOPE`` carries the hard-coded defaults from §11.2 so first-run
output on real codebases is usable without configuration ceremony (P3). To
drop a default key, set it to an empty list in ``solflow.toml`` (the
config-file ``[]``-clears-default rule, §11.2). The previous CLI shortcut
``--no-default-excludes`` was retired in v0.10.0 Stage 2; the
config-file path is now the only way to clear a built-in default.

Glob matching uses `pathspec` with gitignore syntax (the ``gitwildmatch``
factory was deprecated in pathspec 1.x in favor of ``gitignore``). Compiled
PathSpec instances are cached per Scope so repeated matches against many
filenames don't re-parse the patterns.
"""

from dataclasses import dataclass
from functools import lru_cache

import pathspec


@dataclass(frozen=True, slots=True)
class Scope:
    """Container for user-configured scope rules.

    All fields are tuples (frozen, hashable) so a Scope can key an
    ``lru_cache``. CLI resolution constructs the final Scope after layering
    defaults, file values, and CLI flag values per spec §11.2.

    ``interface_bindings`` (v0.17.0, spec §13.2) is a tuple of
    ``(interface_name, contract_name)`` pairs rather than a dict — a dict is
    unhashable and would break the frozen/cacheable contract. Each pair binds
    one interface type to one concrete contract globally. Unlike the four
    glob/name sets this one is exact-name-keyed and may be replaced at runtime
    by the index Bindings panel (§8.3), which re-runs ``build_flows`` against
    the server-held facts (§11.1). ``binding_for`` resolves the bound contract
    name for an interface, applying last-wins on duplicate pairs.
    """

    exclude_paths: tuple[str, ...] = ()
    exclude_contracts: tuple[str, ...] = ()
    inline_libraries: tuple[str, ...] = ()
    stub_paths: tuple[str, ...] = ()
    interface_bindings: tuple[tuple[str, str], ...] = ()


# Default tuple values track spec §11.2's "Default scope" block. v0.2 broadens
# the v0.1 defaults to cover Morpho-style mock conventions (suffix ``Mock``
# rather than prefix; mocks living under ``src/mocks/`` rather than under a
# test directory) without retreating from the Solmate-style coverage v0.1
# already provided. ``stub_paths`` defaults empty — in-tree library stubbing
# is the auditor's call, never assumed.
DEFAULT_SCOPE: Scope = Scope(
    exclude_paths=("**/*.t.sol", "**/test/**", "**/tests/**", "**/mocks/**"),
    exclude_contracts=("*Mock*",),
    inline_libraries=(),
    stub_paths=(),
    interface_bindings=(),
)


# ---------------------------------------------------------------------------
# Matcher helpers
# ---------------------------------------------------------------------------
#
# Compiled PathSpec instances are cached by the patterns tuple. Two Scopes
# that share an exclude_paths value reuse the same compiled spec. The cache is
# unbounded by default (lru_cache default of maxsize=128 is sufficient since
# the number of distinct pattern tuples seen during a single run is small —
# typically 1 for a default-scope run, 2 if CLI flags append).


@lru_cache(maxsize=128)
def _compile_spec(patterns: tuple[str, ...]) -> pathspec.PathSpec:
    """Compile a tuple of gitignore patterns into a PathSpec.

    An empty tuple compiles to a spec that matches nothing.
    """

    return pathspec.PathSpec.from_lines("gitignore", patterns)


def path_excluded(scope: Scope, filename_relative: str) -> bool:
    """True if ``filename_relative`` matches any ``scope.exclude_paths`` glob.

    Empty pattern tuple → always False.
    """

    if not scope.exclude_paths:
        return False
    return _compile_spec(scope.exclude_paths).match_file(filename_relative)


def contract_excluded(scope: Scope, contract_name: str) -> bool:
    """True if ``contract_name`` matches any ``scope.exclude_contracts`` glob.

    Contract names are treated as a single path segment — patterns should not
    contain ``/``. ``Mock*`` matches ``MockERC20``; ``*`` matches anything.
    Empty pattern tuple → always False.
    """

    if not scope.exclude_contracts:
        return False
    return _compile_spec(scope.exclude_contracts).match_file(contract_name)


def library_inlined(scope: Scope, filename_relative: str) -> bool:
    """True if ``filename_relative`` is under ``lib/{name}/**`` for a name
    listed in ``scope.inline_libraries``.

    Files outside ``lib/`` never qualify, regardless of scope. ``node_modules/``
    has no inlining mechanism in v0.1 (spec §11.2 / §11.8).
    """

    if not scope.inline_libraries:
        return False
    if not filename_relative.startswith("lib/"):
        return False
    # Extract the {name} segment: "lib/forge-std/src/Vm.sol" → "forge-std".
    remainder = filename_relative[len("lib/") :]
    slash = remainder.find("/")
    if slash <= 0:
        # "lib/foo" with no trailing slash is malformed for this check; treat
        # as not inlinable (an actual library directory always has children).
        return False
    name = remainder[:slash]
    return name in scope.inline_libraries


def target_stubbed(scope: Scope, filename_relative: str) -> bool:
    """True if ``filename_relative`` matches any ``scope.stub_paths`` glob.

    Semantically identical to ``path_excluded`` but applied to call targets
    (in Layer 2's dispatch) rather than to invoker contracts (in Layer 2's
    outer enumeration). The two matchers are kept separate because they live
    on different scope fields with different defaults — sharing the helper
    would obscure that distinction. Empty pattern tuple → always False.
    """

    if not scope.stub_paths:
        return False
    return _compile_spec(scope.stub_paths).match_file(filename_relative)


def binding_for(scope: Scope, interface_name: str) -> str | None:
    """Return the concrete contract bound to ``interface_name``, or None.

    Reads ``scope.interface_bindings`` (spec §13.2), the exact-name-keyed map
    of interface → concrete contract. Last-wins on duplicate entries for the
    same interface, matching the §11.2 resolution rule that a later ``--bind``
    or panel selection overrides an earlier one. Empty tuple / no match →
    None (the interface stays at its §13.1 default rendering).
    """

    if not scope.interface_bindings:
        return None
    result: str | None = None
    for iface, contract in scope.interface_bindings:
        if iface == interface_name:
            result = contract
    return result
