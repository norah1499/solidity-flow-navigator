"""TOML config loading for v0.1 scope rules (spec §11.2).

Loads ``solflow.toml`` (or the path passed to ``--config``) into a
``PartialScope`` record, which the CLI then merges with the default Scope
and any CLI-flag appends to produce the final ``Scope`` handed to
``build_flows``.

The loader stays separate from ``scope.py`` because the tri-state partial-
scope shape (``None`` vs. ``()`` vs. populated) is config-loader-local —
``Scope`` itself only ever holds concrete tuples. Keeping ``PartialScope``
sealed here means cli.py imports a single merge function (``apply_partial``)
and never sees the tri-state shape directly.

File-not-found is reported via the stdlib ``FileNotFoundError`` so callers
(specifically cli.py in Stage 3) can distinguish "the default
solflow.toml isn't present" (skip silently) from "the user passed
--config <path> and we can't read it" (fail loudly) with a clean
``try/except FileNotFoundError`` path. Malformed TOML and value-type
errors raise ``ConfigError`` with a message naming the path or key.
"""

import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .scope import Scope

# The keys recognized under ``[scope]``. Anything else under that table is a
# typo of one of these (the realistic failure mode) and warrants a stderr
# warning, not silent acceptance.
_KNOWN_SCOPE_KEYS: frozenset[str] = frozenset(
    {
        "exclude_paths",
        "exclude_contracts",
        "inline_libraries",
        "stub_paths",
    }
)


class ConfigError(Exception):
    """Raised when ``solflow.toml`` is malformed or contains invalid values.

    Distinct from ``FileNotFoundError`` (the file simply isn't there — a
    caller-side decision whether to skip or fail) and from
    ``tomllib.TOMLDecodeError`` (re-raised as ``ConfigError`` so callers
    have a single exception class to catch for "the file exists but we
    can't use it").
    """


@dataclass(frozen=True, slots=True)
class PartialScope:
    """A tri-state record of what the TOML config specified.

    Each field carries one of three values:

    - ``None``: the key was absent from ``[scope]`` in the file. The default
      survives the file-replace step of §11.2's resolution order.
    - ``()``: the key was present with an empty list. Per spec, this
      explicitly clears the default for that key. CLI flag appends may
      still add values on top.
    - populated tuple: the key was present with one or more values. Per
      spec, this replaces (not augments) the default for that key.

    ``apply_partial`` consumes this tri-state to perform step 3 of §11.2's
    resolution order. The shape is intentionally not exposed to cli.py.
    """

    exclude_paths: tuple[str, ...] | None = None
    exclude_contracts: tuple[str, ...] | None = None
    inline_libraries: tuple[str, ...] | None = None
    stub_paths: tuple[str, ...] | None = None
    # v0.17.0 (§13.2): the [bindings] table — interface→contract pairs. None
    # when the table is absent, () when present-but-empty, populated otherwise.
    # Sourced from a top-level [bindings] table, not a [scope] key.
    interface_bindings: tuple[tuple[str, str], ...] | None = None


def load_partial_scope_from_toml(path: Path) -> PartialScope:
    """Load ``path`` as TOML and return its ``[scope]`` table as a PartialScope.

    Raises:
        FileNotFoundError: if ``path`` does not exist. Caller decides
            whether to skip silently (default ``solflow.toml`` lookup) or
            propagate as a user-facing error (explicit ``--config`` flag).
        ConfigError: if the file is malformed TOML, or if a ``[scope]`` key
            has the wrong value type (e.g. a string instead of a list, or
            a list containing non-string elements).

    Unknown keys under ``[scope]`` produce a stderr warning but do not raise;
    forward compatibility for new v0.2+ keys would otherwise require a
    coordinated cross-version release. Unknown top-level sections are
    silently ignored on the principle that ``solflow.toml`` may be shared
    with other tooling in future versions.
    """

    with open(path, "rb") as fp:
        try:
            data = tomllib.load(fp)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"malformed TOML in {path}: {exc}") from exc

    # The [bindings] table (v0.17.0, §13.2) is a top-level sibling of [scope],
    # so parse it independently — a file may carry [bindings] with no [scope].
    bindings = _extract_string_map(data.get("bindings"), path)

    scope_table = data.get("scope")
    if scope_table is None:
        # No [scope] section: only [bindings] (if any) contributes. Other
        # unrecognized top-level sections are silently ignored — other tools
        # may share this file in future versions.
        return PartialScope(interface_bindings=bindings)

    if not isinstance(scope_table, dict):
        raise ConfigError(
            f"[scope] in {path} must be a table, got {type(scope_table).__name__}"
        )

    # Warn about unknown keys under [scope] before pulling out the known
    # ones, so a typo'd key produces feedback even if the rest of the file
    # is well-formed.
    for key in scope_table:
        if key not in _KNOWN_SCOPE_KEYS:
            print(
                f"solflow: warning: ignoring unknown key '[scope].{key}' in {path}",
                file=sys.stderr,
            )

    return PartialScope(
        exclude_paths=_extract_string_list(scope_table, "exclude_paths", path),
        exclude_contracts=_extract_string_list(scope_table, "exclude_contracts", path),
        inline_libraries=_extract_string_list(scope_table, "inline_libraries", path),
        stub_paths=_extract_string_list(scope_table, "stub_paths", path),
        interface_bindings=bindings,
    )


def apply_partial(base: Scope, partial: PartialScope) -> Scope:
    """Apply ``partial`` over ``base`` per step 3 of §11.2's resolution order.

    For each of the three scope keys: if ``partial`` has ``None`` for that
    key, ``base``'s value survives; otherwise ``partial``'s value (which may
    be an empty tuple — an explicit clear — or populated) replaces it.

    Returns a new ``Scope``; ``base`` is not mutated.
    """

    return replace(
        base,
        exclude_paths=(
            base.exclude_paths
            if partial.exclude_paths is None
            else partial.exclude_paths
        ),
        exclude_contracts=(
            base.exclude_contracts
            if partial.exclude_contracts is None
            else partial.exclude_contracts
        ),
        inline_libraries=(
            base.inline_libraries
            if partial.inline_libraries is None
            else partial.inline_libraries
        ),
        stub_paths=(
            base.stub_paths if partial.stub_paths is None else partial.stub_paths
        ),
        interface_bindings=(
            base.interface_bindings
            if partial.interface_bindings is None
            else partial.interface_bindings
        ),
    )


def _extract_string_list(
    table: dict[str, object], key: str, path: Path
) -> tuple[str, ...] | None:
    """Pull ``key`` from ``table`` as a tuple-of-strings, or None if absent.

    An empty list returns ``()`` — that's the explicit-clear sentinel per
    the PartialScope docstring, distinct from absence (``None``).
    """

    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, list):
        raise ConfigError(
            f"[scope].{key} in {path} must be a list of strings, "
            f"got {type(value).__name__}"
        )
    for i, element in enumerate(value):
        if not isinstance(element, str):
            raise ConfigError(
                f"[scope].{key}[{i}] in {path} must be a string, "
                f"got {type(element).__name__}"
            )
    return tuple(value)


def _extract_string_map(
    table: object, path: Path
) -> tuple[tuple[str, str], ...] | None:
    """Parse a ``[bindings]``-style TOML table into a tuple of string pairs.

    Returns ``None`` when ``table`` is absent (the section wasn't in the file),
    ``()`` for an empty table, or a tuple of ``(key, value)`` pairs preserving
    file order. Raises ``ConfigError`` if ``table`` is not a table or any value
    is not a string — the §11.2 validation style. Used for the v0.17.0
    ``[bindings]`` table (§13.2): interface type name → concrete contract name.
    TOML guarantees keys are strings, so only values are type-checked.
    """

    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError(
            f"[bindings] in {path} must be a table, got {type(table).__name__}"
        )
    pairs: list[tuple[str, str]] = []
    for key, value in table.items():
        if not isinstance(value, str):
            raise ConfigError(
                f"[bindings].{key} in {path} must be a string "
                f"(a concrete contract name), got {type(value).__name__}"
            )
        pairs.append((key, value))
    return tuple(pairs)
