"""User-configured scope rules: include/exclude lists, library inlining, and
global interface-to-implementation bindings (spec §11.2, §13).

In v0 the Scope is always ``DEFAULT_SCOPE``: no user rules are read, the CLI
accepts only ``<repo_path>``, and Layer 2 applies its built-in defaults
(everything under ``lib/`` and ``node_modules/`` becomes ``ExternalNode``;
unbound interface calls become ``UnresolvedNode``). The dataclass exists now
as a stable parameter slot in ``build_flows(facts, scope)`` so v0.1's TOML
config and CLI flags can land without changing the Layer 2 → CLI contract.

The dataclass is intentionally empty in v0. The spec (§11.2) describes the
v0.1 surface in prose but does not yet fix field names; adding speculative
fields now (even commented-out) would either decay or constrain the v0.1
design pass. When v0.1 lands, fields are added here in one place.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Scope:
    """Container for user-configured scope rules. Empty in v0; populated in v0.1+."""


DEFAULT_SCOPE: Scope = Scope()
