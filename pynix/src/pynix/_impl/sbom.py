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
from pynix import _evaluate
from pynix._impl import _sbom_cyclonedx, _sbom_spdx
from pynix._impl._sbom_model import inventory, parse_walk, serial_uuid
from pynix._impl.env import output_paths
from pynix._store_layout import StoreLayout
from pynix._util import huggorm_build_session, print_json, report_and_exit
from pynix.target import EvaluationTarget, EvaluationTargetError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Iterable, Mapping

    from huggorm import AsyncStore

    from nanopynix.models import JsonValue
    from pynix._impl._sbom_model import Inventory
    from pynix.sbom import Sbom

_WALK = Path(__file__).with_name("sbom_walk.nix")


async def runtime_closure(store: AsyncStore, roots: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Each path of the closure of *roots*, with its references."""
    parsed = [await store.parse_store_path(root) for root in roots]
    closure = {await store.print_store_path(path): path for path in await store.compute_fs_closure(parsed)}
    return {
        printed: tuple(
            sorted(
                [
                    await store.print_store_path(ref)
                    for ref in (await store.query_path_info(closure[printed])).references()
                ]
            )
        )
        for printed in sorted(closure)
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
        async with huggorm_build_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_session, store, _eval_store, state):
            value = await _evaluate.evaluate_target(
                EvaluationTarget.from_command(command),
                state,
                auto_call_file=True,
                attr_search=_evaluate.base_attr_search(),
            )
            walk = await state.eval_file(str(_WALK))
            await state.force(walk)
            records = parse_walk(await (await walk(value)).string_value())
            await state.force(value)
            out_path = await value.get("outPath")
            await state.force(out_path)
            target = await out_path.string_value()
            roots = await output_paths(state, value)
            listed = inventory(records, target, await runtime_closure(store, roots), buildtime=command.buildtime)
            return listed, await _patch_texts(listed, (await StoreLayout.of(store)).to_disk)
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
