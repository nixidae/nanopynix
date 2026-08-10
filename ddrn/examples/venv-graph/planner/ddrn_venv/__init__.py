"""What every planner of `ddrn/examples/venv-graph` shares.

The example has more than one planner. The outer one resolves the environment,
and the planner of a source distribution resolves the backend that the archive
asks for. Both make packaging decisions with `packaging`, both write a plan
document, and both submit the root that `graph.nix` wrote.

This package is not installed. A planner reaches it through `PYTHONPATH`
inside its build sandbox, which `default.nix` sets from a store path.
"""

from __future__ import annotations

from ._env import env as env
from ._plan import (
    Plan as Plan,
    nix_string as nix_string,
    open_restricted_store as open_restricted_store,
    submit as submit,
)
from ._resolve import (
    FALLBACK_BACKEND as FALLBACK_BACKEND,
    Artifact as Artifact,
    Target as Target,
    build_system as build_system,
    choose as choose,
    target_from_environ as target_from_environ,
    target_tags as target_tags,
    wheel_rank as wheel_rank,
)

__all__ = [
    "FALLBACK_BACKEND",
    "Artifact",
    "Plan",
    "Target",
    "build_system",
    "choose",
    "env",
    "nix_string",
    "open_restricted_store",
    "submit",
    "target_from_environ",
    "target_tags",
    "wheel_rank",
]
