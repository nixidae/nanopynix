"""Render ci/workflows/*.nix into .github/workflows/*.yml with huggorm.

Run with::

    direnv exec . python ci/render.py

You do not have to remember that command.
``tests/test_ci_workflows.py`` calls :func:`render_workflows` on
every pytest run, compares the result against the checked-in YAML, and writes
the fresh render when the two differ -- so an edit under ``ci/workflows/`` that
was never rendered fails the suite *and* arrives already corrected.

That test is why this module separates rendering from writing.
:func:`render_workflows` returns the text of each output file and touches no
disk, because the packaged CI runner runs from a read-only store copy of this
repository (``nix/tests.nix``), where the comparison is still meaningful
and the write is not possible.
"""

# ruff: noqa: T201
# This script's entire purpose is emitting the rendered CI matrix on
# stdout for the caller to redirect; print is the interface.

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import huggorm
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / "ci" / "workflows"
OUTPUT_DIR = REPO_ROOT / ".github" / "workflows"

HEADER = "# GENERATED FILE -- do not edit by hand.\n# Edit ci/workflows/{name}.nix; a pytest run renders this file again.\n\n"

#: The keys that come first in a mapping, in this order. Everything else
#: follows in alphabetical order.
#:
#: One list for every level of the document, and not one per level. A key name
#: means the same thing wherever it appears -- ``name`` labels the thing,
#: ``if`` gates it, ``steps`` is the body of a job -- so a single ordering
#: reads correctly for a workflow, a job and a step alike. A key that is not
#: here sorts alphabetically after these, which is why the list holds only the
#: keys whose position a reader relies on.
KEY_ORDER = (
    "name",
    "on",
    "id",
    "uses",
    "run",
    "runs-on",
    "needs",
    "if",
    "with",
    "permissions",
    "concurrency",
    "defaults",
    "strategy",
    "env",
    "outputs",
    "timeout-minutes",
    "steps",
    "jobs",
)

_KEY_RANK = {key: rank for rank, key in enumerate(KEY_ORDER)}


def order_keys(value: Any) -> Any:
    """Order every mapping in *value* by :data:`KEY_ORDER`, then by name.

    **This exists because a Nix attribute set has no order to preserve.**
    Nix keys an attribute set by ``Symbol``, and interns a symbol the first
    time it parses the name, so ``builtins.toYAML`` rendered the members in the
    order the names were first read anywhere -- not in the order the source
    writes them, and not alphabetically.

    Issue #121 measured what that cost. A probe attribute near the top of
    ``ci/workflows/lib.nix``, naming ``steps``, ``if``, ``timeout-minutes``,
    ``runs-on`` and ``name`` and reaching no output, moved 150 lines across 32
    hunks of ``on_commit.yml`` and 58 lines across 14 hunks of
    ``on_schedule.yml``. The same probe against this function changes nothing.
    ``tests/test_ci_workflows.py`` holds the invariant.

    A Python dict does have an order, and this function is where the rendering
    picks one. ``builtins.toJSON`` sorts the names on the way out, so the input
    here is already deterministic; this only promotes the keys a reader looks
    for first.
    """
    # `cast`, and not an annotated assignment. `isinstance` narrows an `Any` to
    # `dict[Unknown, Unknown]`, and that narrowed type wins over a declared one,
    # so pyright reports `reportUnknownVariableType` either way.
    if isinstance(value, dict):
        items = cast("dict[str, Any]", value)
        return {
            key: order_keys(items[key])
            for key in sorted(items, key=lambda name: (_KEY_RANK.get(name, len(KEY_ORDER)), name))
        }
    if isinstance(value, list):
        entries = cast("list[Any]", value)
        return [order_keys(entry) for entry in entries]
    return value


class _BlockStyleDumper(yaml.CSafeDumper):
    """Writes a string with a newline as a literal block (``|``), so a shell
    script in a step stays readable."""


def _represent_str(dumper: _BlockStyleDumper, data: str) -> yaml.Node:
    return dumper.represent_scalar(  # type: ignore[reportUnknownMemberType]  # PyYAML's stubs leave `value` untyped
        "tag:yaml.org,2002:str", data, style="|" if "\n" in data else None
    )


_BlockStyleDumper.add_representer(str, _represent_str)


def to_yaml(document: Any) -> str:
    """*document* as YAML, in the key order it already has."""
    return yaml.dump(document, sort_keys=False, Dumper=_BlockStyleDumper)


async def render_workflow(state: huggorm.AsyncEvalState, nix_file: Path) -> str:
    """Render one workflow, through JSON.

    ``builtins.toJSON`` sorts the names. This reads the workflow as a Python
    value, orders it with :func:`order_keys`, and writes it as YAML.
    """
    value = await state.eval_expr(f"builtins.toJSON (import {nix_file})")
    await state.force(value)
    document = json.loads(await value.string_value())
    return to_yaml(order_keys(document))


def workflow_sources() -> list[Path]:
    """Every workflow entry point, in a stable order.

    Only ``on_*.nix``: ``lib.nix`` is a library of job builders that each
    entry point imports, and it renders to nothing on its own.
    """
    return sorted(WORKFLOWS_DIR.glob("on_*.nix"))


def output_path(nix_file: Path) -> Path:
    return OUTPUT_DIR / f"{nix_file.stem}.yml"


#: What nixpkgs reads to stop refusing a package that this platform cannot
#: build. nixpkgs reads it with ``builtins.getEnv``, so it belongs to the
#: environment of the worker and not to the settings of Nix.
_UNSUPPORTED_SYSTEM_ENV = "NIXPKGS_ALLOW_UNSUPPORTED_SYSTEM"


@contextlib.contextmanager
def _allow_unsupported_system() -> Generator[None]:
    """Let the render name a package that this platform cannot build.

    **The render reads names, and it builds nothing.** ``checkAttrs`` in
    ``ci/workflows/lib.nix`` asks ``isDerivation`` of each check, which forces
    each one, and ``checks.nixos-module`` evaluates a NixOS module. That module
    reaches ``nixos-enter``, whose ``meta.platforms`` is Linux, so nixpkgs
    refuses on macOS and the whole render fails.

    **The output does not change, and it must not.** The step of
    ``static-checks`` names every gate, so a render that dropped one would
    quietly drop it from CI. This makes macOS evaluate the same names that
    Linux evaluates, rather than fewer.

    Set around the session, because the worker takes the environment when it
    starts. Restored after, because
    ``tests/test_ci_workflows.py`` calls this from the pytest
    process, which other tests share.
    """
    previous = os.environ.get(_UNSUPPORTED_SYSTEM_ENV)
    os.environ[_UNSUPPORTED_SYSTEM_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            del os.environ[_UNSUPPORTED_SYSTEM_ENV]
        else:
            os.environ[_UNSUPPORTED_SYSTEM_ENV] = previous


async def render_workflows() -> dict[Path, str]:
    """Render every workflow, and return the whole text of each output file.

    Writes nothing. The keys are the paths that :func:`main` writes to.
    """
    with _allow_unsupported_system():
        return await _render_workflows()


async def _render_workflows() -> dict[Path, str]:
    # `nix/compat.nix` reads the lock through `builtins.fetchTree`.
    # A Nix that made one stable refuses to enable it.
    for feature in ("nix-command", "flakes", "fetch-tree"):
        if huggorm.is_experimental_feature(feature):
            huggorm.enable_experimental_feature(feature)
    async with huggorm.AsyncSession() as session:
        state = session.eval(session.store())
        rendered: dict[Path, str] = {}
        for nix_file in workflow_sources():
            body = await render_workflow(state, nix_file)
            rendered[output_path(nix_file)] = HEADER.format(name=nix_file.stem) + body
        return rendered


def main() -> None:
    # Sync, so that the writes below are plain `pathlib` calls. Blocking file
    # I/O inside an async function is banned repository-wide, and there is
    # nothing to overlap it with here: the render is already finished.
    rendered = asyncio.run(render_workflows())
    for out_path in sorted(OUTPUT_DIR.glob("*.yml")):
        if out_path not in rendered and out_path.read_text().startswith("# GENERATED FILE --"):
            out_path.unlink()
            print(f"removed stale {out_path}")

    for out_path, body in sorted(rendered.items()):
        out_path.write_text(body)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
