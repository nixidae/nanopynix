"""A Software Bill of Materials for a derivation, as a library.

``pynix sbom`` is the command over these functions. A caller with its own
evaluator and store, or its own components to add, calls them directly:
:func:`inventory_of` lists what the SBOM holds, and :func:`cyclonedx` and
:func:`spdx` render it. Both documents are deterministic for one inventory.

:func:`closure_bom` is for a caller that holds store paths and no value,
such as the paths a Kubernetes manifest names. It builds nothing. Each
record comes from the path's deriver, so it has no license and no CPE:
those live in ``meta``, which no ``.drv`` holds.
"""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from anyio import Path as AnyioPath
from huggorm import AsyncDerivation, AsyncEvalState, AsyncStore, AsyncValue
from huggorm.errors import NixError

from pynix._evaluate import JsonValue
from pynix._impl import _sbom_cyclonedx, _sbom_spdx
from pynix._impl._sbom_model import (
    Inventory as Inventory,
    License as License,
    Record as Record,
    Source as Source,
    from_store_path,
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


def _store_name(path: str) -> str:
    """The name of a store path, after its hash."""
    return path.rsplit("/", 1)[-1].partition("-")[2]


def _sri(value: str | None) -> str | None:
    """*value* when it is an SRI hash; a `.drv` may hold any of Nix's hash formats."""
    return value if value and "-" in value and value.split("-", 1)[0] in {"sha1", "sha256", "sha512"} else None


async def _attrs(derivation: AsyncDerivation) -> dict[str, str]:
    """The attributes a derivation was written from, each as one string.

    With ``__structuredAttrs`` the environment holds only the outputs, and
    the rest (``pname``, ``version``, ``src``, ``urls``) is JSON beside it.
    fetchurl writes its derivations that way. A list joins with spaces, as
    Nix puts one into the environment.
    """
    attrs = dict(await derivation.env())
    structured = await derivation.structured_attrs()
    if structured is not None:
        for name, value in json.loads(structured).items():
            if isinstance(value, str):
                attrs[name] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                attrs[name] = " ".join(value)
    return attrs


async def _source(drv_store: AsyncStore, derivation: AsyncDerivation, src: str | None) -> Source | None:
    """The fetch that made *src*: the input derivation with the same name, which names URLs."""
    if not src:
        return None
    store_dir = src.rsplit("/", 1)[0]
    wanted = _store_name(src) + ".drv"
    for input_drv in await derivation.input_drvs():
        if _store_name(input_drv) != wanted:
            continue
        fetch = await drv_store.read_derivation(await drv_store.parse_store_path(f"{store_dir}/{input_drv}"))
        env = await _attrs(fetch)
        urls = tuple((env.get("urls") or env.get("url") or "").split())
        if urls:
            return Source(urls=urls, hash=_sri(env.get("outputHash")))
    return None


async def record_of_path(store: AsyncStore, drv_store: AsyncStore, path: str) -> Record:
    """The record of one store path, from its deriver in *drv_store*.

    A path with no deriver, or one *drv_store* cannot read, keeps only the
    name and version its path spells.
    """
    named = from_store_path(path)
    deriver = (await store.query_path_info(await store.parse_store_path(path))).deriver()
    if deriver is None:
        return named
    try:
        derivation = await drv_store.read_derivation(deriver)
    except NixError:
        return named
    env = await _attrs(derivation)
    output_name = next((name for name in sorted(env) if env[name] == path), None)
    return Record(
        path=path,
        name=env.get("pname") or named.name,
        version=env.get("version") or named.version,
        output_name=output_name,
        output_hash=env.get("outputHash"),
        source=await _source(drv_store, derivation, env.get("src")),
        patches=tuple(env.get("patches", "").split()),
    )


async def closure_bom(
    store: AsyncStore,
    roots: Iterable[str],
    *,
    name: str,
    version: str | None = None,
    drv_store: AsyncStore | None = None,
) -> Bom:
    """The SBOM of the runtime closure of *roots*, valid paths in *store*. Builds nothing.

    *name* and *version* name the target, which depends on every root.
    *drv_store* holds the derivers, when that is not *store*: the local
    store after evaluation, for paths that live in a remote one.
    """
    wanted = sorted(set(roots))
    runtime = await runtime_closure(store, wanted)
    readers = drv_store or store
    records = {path: await record_of_path(store, readers, path) for path in runtime}
    target = Record(path=name, name=name, version=version)
    records[name] = target
    # A root is what the caller deploys: listed though it has no version.
    listed = inventory(records, name, {**runtime, name: tuple(wanted)}, buildtime=False, always=frozenset(wanted))
    return Bom(listed, await _patch_texts(listed, (await StoreLayout.of(store)).to_disk), buildtime=False)


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
