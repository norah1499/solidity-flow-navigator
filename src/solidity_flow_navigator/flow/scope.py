"""User-configured scope rules: include/exclude lists and library inlining
preferences (spec §11.2).

In v0 the Scope was always ``DEFAULT_SCOPE`` and the dataclass carried no
fields. v0.1 introduces three rule sets:

- ``exclude_paths``: gitignore-style globs matched against each contract's
  ``source_location.filename_relative``. Matching contracts produce no Flows.
- ``exclude_contracts``: gitignore-style globs matched against contract names
  (treated as a single path segment — no ``/`` involved).
- ``inline_libraries``: library name prefixes. A dependency under
  ``lib/{name}/**`` recurses into Layer 2 instead of stubbing as
  ``ExternalNode`` when ``{name}`` appears in this tuple.

``DEFAULT_SCOPE`` carries the hard-coded defaults from §11.2 so first-run
output on real codebases is usable without configuration ceremony (P3).
``--no-default-excludes`` (in the CLI) clears ``exclude_paths`` and
``exclude_contracts`` before file/CLI values apply; ``inline_libraries`` has
no default to clear.

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

    All three fields are tuples (frozen, hashable) so a Scope can key an
    ``lru_cache``. CLI resolution constructs the final Scope after layering
    defaults, file values, and CLI flag values per spec §11.2.
    """

    exclude_paths: tuple[str, ...] = ()
    exclude_contracts: tuple[str, ...] = ()
    inline_libraries: tuple[str, ...] = ()


DEFAULT_SCOPE: Scope = Scope(
    exclude_paths=("**/*.t.sol", "**/test/**", "**/tests/**"),
    exclude_contracts=("Mock*",),
    inline_libraries=(),
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
