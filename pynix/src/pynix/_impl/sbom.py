"""The implementation of ``pynix sbom``.

The build closure and what each package says of itself come from one
evaluation of ``sbom_walk.nix``. The runtime closure comes from the store,
after the build.
"""

from __future__ import annotations

import importlib.metadata
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from anyio import Path as AnyioPath

from nanopynix._typechecking import BEARTYPING
from pynix._impl import _sbom_cyclonedx, _sbom_spdx
from pynix._impl._sbom_model import inventory, parse_walk, serial_uuid
from pynix._impl.develop import physical_path_mapper
from pynix._impl.env import output_paths
from pynix._util import eval_session, print_json, report_and_exit
from pynix.target import EvaluationTarget, EvaluationTargetError, base_attr_search, evaluate_target

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Iterable, Mapping

    from nanopynix.models import JsonValue
    from nanopynix.protocols import AsyncStore
    from pynix._impl._sbom_model import Inventory
    from pynix.sbom import Sbom

_WALK = Path(__file__).with_name("sbom_walk.nix")


async def runtime_closure(store: AsyncStore, roots: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Each path of the closure of *roots*, with its references."""
    closure: set[str] = set()
    for root in roots:
        closure.update(str(path) for path in await store.compute_fs_closure(root))
    return {
        path: tuple(sorted(str(ref) for ref in (await store.query_path_info(path)).references))
        for path in sorted(closure)
    }


async def _patch_texts(listed: Inventory, to_disk: Callable[[str], str]) -> dict[str, str]:
    """The text of each patch of a listed package that this host can read."""
    texts: dict[str, str] = {}
    for record in (listed.target, *listed.components):
        for patch in record.patches:
            path = AnyioPath(to_disk(patch))
            if patch not in texts and await path.is_file():
                texts[patch] = await path.read_text(errors="replace")
    return texts


async def build_inventory(command: Sbom) -> tuple[Inventory, dict[str, str]]:
    """Evaluate and build the target of *command*, and list what its SBOM holds.

    The second value holds the text of each patch, by path.
    """
    try:
        async with eval_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_nix, store, session):
            value = await evaluate_target(
                EvaluationTarget.from_command(command), session, auto_call_file=True, attr_search=base_attr_search()
            )
            walk = await session.file(str(_WALK))
            records = parse_walk(await (await value.apply(walk)).as_string())
            target = str(await (await value.attr("outPath").apply("builtins.unsafeDiscardStringContext")).as_string())
            roots = await output_paths(value)
            listed = inventory(records, target, await runtime_closure(store, roots), buildtime=command.buildtime)
            return listed, await _patch_texts(listed, await physical_path_mapper(store))
    except EvaluationTargetError as exc:
        report_and_exit(exc)


def created_at(environ: Mapping[str, str], now: datetime) -> str:
    """The SPDX creation time: ``SOURCE_DATE_EPOCH`` when it is set, else *now*."""
    epoch = environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), UTC) if epoch and epoch.isdigit() else now.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def render(command: Sbom, listed: Inventory, patches: dict[str, str]) -> dict[str, JsonValue]:
    """The document that *command* asks for."""
    unique = serial_uuid(f"{listed.target.path}:{command.format}:{command.buildtime}").urn
    tool_version = importlib.metadata.version("pynix")
    if command.format == "spdx":
        return _sbom_spdx.document(
            listed,
            namespace=unique,
            created=created_at(os.environ, datetime.now(UTC)),
            tool_version=tool_version,
        )
    return _sbom_cyclonedx.document(listed, serial_number=unique, tool_version=tool_version, patches=patches)


async def run_sbom(command: Sbom) -> None:
    """The body of :meth:`pynix.sbom.Sbom.run`."""
    listed, patches = await build_inventory(command)
    print_json(render(command, listed, patches))
