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
from ._freshness import (
    Pin as Pin,
    # `check` alone says nothing about what it checks, and this package holds
    # more than one check.
    check as check_freshness,
)
from ._plan import (
    LocalNode as LocalNode,
    Plan as Plan,
    build_tools_from as build_tools_from,
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
    target_platforms as target_platforms,
    target_tags as target_tags,
    wheel_rank as wheel_rank,
)
from ._sdist import (
    PYPROJECT as PYPROJECT,
    is_pyproject as is_pyproject,
    read_pyproject as read_pyproject,
)
from ._uvlock import (
    Edge as Edge,
    File as File,
    Lock as Lock,
    LockError as LockError,
    Package as Package,
    PackageId as PackageId,
    Resolution as Resolution,
    build_requirements as build_requirements,
    filter_conflicts as filter_conflicts,
    # `plan.py` has a `resolve` of its own, which turns a resolution into a
    # graph. This one turns a lock file into a resolution.
    resolve as resolve_lock,
)

__all__ = [
    "FALLBACK_BACKEND",
    "PYPROJECT",
    "Artifact",
    "Edge",
    "File",
    "LocalNode",
    "Lock",
    "LockError",
    "Package",
    "PackageId",
    "Pin",
    "Plan",
    "Resolution",
    "Target",
    "build_requirements",
    "build_system",
    "build_tools_from",
    "check_freshness",
    "choose",
    "env",
    "filter_conflicts",
    "is_pyproject",
    "nix_string",
    "open_restricted_store",
    "read_pyproject",
    "resolve_lock",
    "submit",
    "target_from_environ",
    "target_platforms",
    "target_tags",
    "wheel_rank",
]
