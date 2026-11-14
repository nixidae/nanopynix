"""A Software Bill of Materials for a derivation, as a library.

``pynix sbom`` is the command over these functions. A caller with its own
evaluator and store, or its own components to add, calls them directly:
:func:`inventory_of` lists what the SBOM holds, and :func:`cyclonedx` and
:func:`spdx` render it. Both documents are deterministic for one inventory.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from anyio import Path as AnyioPath
from huggorm import AsyncEvalState, AsyncStore, AsyncValue

from pynix._evaluate import JsonValue
from pynix._impl import _sbom_cyclonedx, _sbom_spdx
from pynix._impl._sbom_model import (
    Inventory as Inventory,
    License as License,
    Record as Record,
    Source as Source,
    inventory,
    parse_walk,
    serial_uuid,
)
from pynix._impl.env import output_paths
from pynix._nix_helpers import helper
from pynix._store_layout import StoreLayout


@dataclass(frozen=True, slots=True)
class Bom:
    """What an SBOM of one derivation holds, and the text of each patch this host can read, by path."""

    inventory: Inventory
    patches: dict[str, str]
    buildtime: bool


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
    texts: dict[str, str] = {}
    for record in (listed.target, *listed.components):
        for patch in record.patches:
            path = AnyioPath(to_disk(patch))
            if patch not in texts and await path.is_file():
                texts[patch] = await path.read_text(errors="replace")
    return texts


async def inventory_of(state: AsyncEvalState, store: AsyncStore, value: AsyncValue, *, buildtime: bool = False) -> Bom:
    """Build the default outputs of the derivation *value*, and list what its SBOM holds.

    The runtime closure of those outputs, and with *buildtime* the build
    closure too. *store* is the store *state* builds in.
    """
    walk = await helper(state, "sbom")
    await state.force(walk)
    records = parse_walk(await (await walk(value)).string_value())
    await state.force(value)
    out_path = await value.get("outPath")
    await state.force(out_path)
    target = await out_path.string_value()
    roots = await output_paths(state, value)
    listed = inventory(records, target, await runtime_closure(store, roots), buildtime=buildtime)
    return Bom(listed, await _patch_texts(listed, (await StoreLayout.of(store)).to_disk), buildtime)


def _serial(bom: Bom, kind: str) -> str:
    return serial_uuid(f"{bom.inventory.target.path}:{kind}:{bom.buildtime}").urn


def cyclonedx(bom: Bom) -> dict[str, JsonValue]:
    """The CycloneDX 1.7 document of *bom*. Its serial number follows from the target."""
    return _sbom_cyclonedx.document(
        bom.inventory,
        serial_number=_serial(bom, "cyclonedx"),
        tool_version=importlib.metadata.version("pynix"),
        patches=bom.patches,
    )


def cyclonedx_component(bom: Bom, record: Record) -> dict[str, JsonValue]:
    """The CycloneDX component of one *record* of *bom*, for a caller that writes its own document."""
    return _sbom_cyclonedx.component(record, bom.patches)


def spdx(bom: Bom, *, created: str) -> dict[str, JsonValue]:
    """The SPDX 2.3 document of *bom*. *created* is the time of the document, in ``YYYY-MM-DDThh:mm:ssZ``."""
    return _sbom_spdx.document(
        bom.inventory,
        namespace=_serial(bom, "spdx"),
        created=created,
        tool_version=importlib.metadata.version("pynix"),
    )
