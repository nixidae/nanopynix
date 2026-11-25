"""A Software Bill of Materials for a derivation, as a library.

``pynix sbom`` is the command over these functions. A caller with its own
evaluator and store, or its own components to add, calls them directly:
:func:`inventory_of` lists what the SBOM holds, and :func:`cyclonedx` and
:func:`spdx` render it. Both documents are deterministic for one inventory.

:func:`closure_bom` is for a caller that holds store paths, not one
derivation: the paths a Kubernetes manifest names, read from its string
context by :func:`context_roots`. It builds nothing. A state that recorded
its derivations (huggorm ``set_record_derivations``) gives each path the
value it was coerced from, with ``meta``. A path with no recorded value
gets its record from its ``.drv``, with no license and no CPE, and says so
in :attr:`Record.origin`.
"""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol

import anyio
from anyio import Path as AnyioPath
from huggorm import AsyncDerivation, AsyncEvalState, AsyncStore, AsyncValue, DerivationOutputCAFixed, FileType
from huggorm.errors import InvalidPath, NixError

from pynix._evaluate import JsonValue
from pynix._impl import _sbom_cyclonedx, _sbom_embedded, _sbom_spdx
from pynix._impl._sbom_cpe import CpeDictionary as CpeDictionary, with_generated_cpes
from pynix._impl._sbom_embedded import (
    Ecosystem as Ecosystem,
    Package as Package,
    package_json as package_json,
    package_of_json as package_of_json,
)
from pynix._impl._sbom_model import (
    Aggregate as Aggregate,
    Build as Build,
    Inventory as Inventory,
    License as License,
    Maintainer as Maintainer,
    Origin as Origin,
    Record as Record,
    Source as Source,
    from_deriver_name,
    from_store_path,
    inventory,
    parse_walk,
    serial_uuid,
    with_embedded,
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
    #: Each ``.drv`` of the build closure, from :func:`build_bom`; empty for any other SBOM.
    builds: tuple[Build, ...] = ()


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


async def with_store_facts(store: AsyncStore, records: Mapping[str, Record], paths: Iterable[str]) -> dict[str, Record]:
    """*records*, each of *paths* with the NAR hash, NAR size and deriver *store* holds for it.

    *paths* must be valid in *store*. A record that names its ``.drv`` keeps it.
    """
    result = dict(records)
    for path in paths:
        record = result.get(path)
        if record is None:
            continue
        info = await store.query_path_info(await store.parse_store_path(path))
        deriver = info.deriver()
        result[path] = replace(
            record,
            nar_hash=info.nar_hash().sri(),
            nar_size=info.nar_size(),
            drv_path=record.drv_path or (await store.print_store_path(deriver) if deriver else None),
        )
    return result


class EmbeddedCache(Protocol):
    """Scans kept between calls. A store path never changes, so neither does its scan.

    An empty tuple is an answer: the path names no package.
    """

    async def known(self, paths: Sequence[str]) -> Mapping[str, tuple[Package, ...]]:
        """The scans held for any of *paths*."""
        ...

    async def keep(self, scans: Mapping[str, tuple[Package, ...]]) -> None: ...


#: NARs read at once from a store whose paths are not on this host's disk.
#: Each is held in memory while it is scanned.
NAR_READS: Final = 4


async def _scan(
    store: AsyncStore, to_disk: Callable[[str], str], nar_reads: anyio.CapacityLimiter, path: str
) -> tuple[Package, ...] | None:
    """The scan of *path*, from this host's disk or else through *store*; None when neither holds it."""
    disk = Path(to_disk(path))
    if await AnyioPath(disk).exists():
        return await anyio.to_thread.run_sync(_sbom_embedded.scan, disk, path)
    async with nar_reads:
        try:
            accessor = await store.get_fs_accessor(await store.parse_store_path(path))
        except InvalidPath:
            return None
        if accessor is None:
            return None
        return await anyio.to_thread.run_sync(_sbom_embedded.scan_tree, _sbom_embedded.AccessorTree(accessor, path))


async def embedded_packages(
    store: AsyncStore, paths: Iterable[str], *, cache: EmbeddedCache | None = None
) -> dict[str, tuple[Package, ...]]:
    """The packages that each of *paths* names inside itself: Go modules, Rust crates, Python distributions.

    A path not on this host's disk is read through *store*: from a remote
    store, its NAR. *cache* answers for the paths it holds and keeps the rest.
    """
    wanted = sorted(set(paths))
    known = dict(await cache.known(wanted)) if cache is not None else {}
    to_disk = (await StoreLayout.of(store)).to_disk
    nar_reads = anyio.CapacityLimiter(NAR_READS)
    scanned: dict[str, tuple[Package, ...]] = {}

    async def read(path: str) -> None:
        packages = await _scan(store, to_disk, nar_reads, path)
        if packages is not None:
            scanned[path] = packages

    async with anyio.create_task_group() as group:
        for path in wanted:
            if path not in known:
                group.start_soon(read, path)
    if cache is not None and scanned:
        await cache.keep(scanned)
    return {path: packages for path, packages in sorted({**known, **scanned}.items()) if packages}


async def _store_files(store: AsyncStore, root: str, files: Mapping[str, str]) -> dict[str, bytes]:
    """The bytes of each regular file in *files* (a path, by where it sits inside the store path *root*).

    One accessor for the store path: on a remote store it fetches the NAR
    once. Patches often sit inside a large source, such as nixpkgs.
    """
    try:
        accessor = await store.get_fs_accessor(await store.parse_store_path(root))
    except NixError:
        return {}
    if accessor is None:
        return {}
    found: dict[str, bytes] = {}
    for path, inside in files.items():
        stat = await anyio.to_thread.run_sync(accessor.maybe_lstat, inside)
        if stat is not None and stat.type() == FileType.REGULAR:
            found[path] = await anyio.to_thread.run_sync(accessor.read_file, inside)
    return found


async def _patch_texts(store: AsyncStore, listed: Inventory) -> dict[str, str]:
    """The text of each patch, from this host's disk or else through *store*."""
    to_disk = (await StoreLayout.of(store)).to_disk
    store_dir = (await store.store_dir()).rstrip("/")
    texts: dict[str, str] = {}
    elsewhere: dict[str, dict[str, str]] = {}
    for patch in sorted({p for record in (listed.target, *listed.components) for p in record.patches}):
        path = AnyioPath(to_disk(patch))
        if await path.is_file():
            texts[patch] = await path.read_text(errors="replace")
            continue
        name, _, rest = patch.removeprefix(store_dir + "/").partition("/")
        elsewhere.setdefault(f"{store_dir}/{name}", {})[patch] = "/" + rest
    for root, files in elsewhere.items():
        for patch, data in (await _store_files(store, root, files)).items():
            texts[patch] = data.decode(errors="replace")
    return texts


async def inventory_of(state: AsyncEvalState, store: AsyncStore, value: AsyncValue, *, buildtime: bool = False) -> Bom:
    """Build the default outputs of the derivation *value*, and list what its SBOM holds.

    The runtime closure of those outputs, and with *buildtime* the build
    closure too. *store* is the store *state* builds in.
    """
    walk = await (await helper(state, "sbom")).get("closure")
    await state.force(walk)
    records = parse_walk(await (await walk(value)).string_value())
    await state.force(value)
    out_path = await value.get("outPath")
    await state.force(out_path)
    target = await out_path.string_value()
    roots = await output_paths(state, value)
    runtime = await runtime_closure(store, roots)
    records = await with_store_facts(store, records, runtime)
    listed = with_embedded(
        inventory(records, target, runtime, buildtime=buildtime), await embedded_packages(store, runtime)
    )
    return Bom(listed, await _patch_texts(store, listed), buildtime)


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
        hash_ = _sri(env.get("outputHash"))
        if "rev" in env and "urls" not in env and env.get("url"):
            # fetchgit: `url` names the repository, and `rev` what it checks out.
            return Source(urls=(), hash=hash_, repository=env["url"], rev=env["rev"])
        urls = tuple((env.get("urls") or env.get("url") or "").split())
        if urls:
            return Source(urls=urls, hash=hash_)
    return None


async def record_of_path(store: AsyncStore, drv_store: AsyncStore, path: str) -> Record:
    """The record of one store path, from its deriver in *drv_store*.

    A path with no deriver keeps only the name and version its path
    spells. One whose deriver *drv_store* cannot read takes them from the
    deriver's name, which holds no output name.
    """
    deriver = (await store.query_path_info(await store.parse_store_path(path))).deriver()
    if deriver is None:
        return from_store_path(path)
    try:
        derivation = await drv_store.read_derivation(deriver)
    except NixError:
        return from_deriver_name(path, await store.print_store_path(deriver))
    return await _record_of_derivation(drv_store, derivation, await store.print_store_path(deriver), path)


async def _record_of_derivation(drv_store: AsyncStore, derivation: AsyncDerivation, drv_path: str, path: str) -> Record:
    named = from_store_path(path)
    env = await _attrs(derivation)
    output_name = next((name for name in sorted(env) if env[name] == path), None)
    return Record(
        path=path,
        name=env.get("pname") or named.name,
        version=env.get("version") or named.version,
        output_name=output_name,
        output_hash=env.get("outputHash"),
        drv_path=drv_path,
        source=await _source(drv_store, derivation, env.get("src")),
        patches=tuple(env.get("patches", "").split()),
        origin=Origin.DERIVATION,
    )


async def context_derivations(store: AsyncStore, value: AsyncValue) -> dict[str, str | None]:
    """The store paths the string context of *value* names, each with the ``.drv`` that builds it.

    Forced all the way down. A plain path names itself, built by nothing.
    A built output names the path its ``.drv`` in *store* gives it, and a
    floating content-addressed one, which has no path yet, is left out. A
    ``.drv`` with its whole closure (``addDrvOutputDependencies``) names the
    ``.drv``, built by nothing.
    """
    # Each element names its paths by base name, with no store directory.
    store_dir = await store.store_dir()
    roots: dict[str, str | None] = {}
    for element in await value.string_context():
        if element.startswith("!"):
            output, drv = element[1:].split("!", 1)
            drv = f"{store_dir}/{drv}"
            env = await (await store.read_derivation(await store.parse_store_path(drv))).env()
            path = env.get(output, "")
            if path.startswith(store_dir + "/"):
                roots[path] = drv
        else:
            roots.setdefault(f"{store_dir}/{element.removeprefix('=')}", None)
    return dict(sorted(roots.items()))


async def context_roots(store: AsyncStore, value: AsyncValue) -> tuple[str, ...]:
    """The store paths the string context of *value* names: :func:`context_derivations` without the ``.drv``."""
    return tuple(await context_derivations(store, value))


@dataclass(frozen=True, slots=True)
class _BuildGraph:
    #: Each output and input source of the build closure, and what its build reads.
    reads: dict[str, tuple[str, ...]]
    #: The ``.drv`` path and file of each output in *reads*; an input source has none.
    derivations: dict[str, tuple[str, AsyncDerivation]]
    builds: tuple[Build, ...]


async def _read(drv_store: AsyncStore, parsed: dict[str, AsyncDerivation], drv: str) -> AsyncDerivation:
    if drv not in parsed:
        parsed[drv] = await drv_store.read_derivation(await drv_store.parse_store_path(drv))
    return parsed[drv]


async def _output_path(drv_store: AsyncStore, parsed: dict[str, AsyncDerivation], drv: str, output: str) -> str | None:
    path = (await (await _read(drv_store, parsed, drv)).env()).get(output, "")
    # A floating content-addressed output has a placeholder, no path yet.
    return path if path.startswith(await drv_store.store_dir() + "/") else None


async def _inputs(
    drv_store: AsyncStore, parsed: dict[str, AsyncDerivation], derivation: AsyncDerivation, store_dir: str
) -> tuple[list[str], set[str]]:
    """The input ``.drv`` files of *derivation*, and the output paths it reads of them."""
    children: list[str] = []
    needed: set[str] = set()
    for base, node in (await derivation.input_drvs()).items():
        child = f"{store_dir}/{base}"
        children.append(child)
        # A wire value: its accessors answer at once.
        for output in node.outputs():
            if (found := await _output_path(drv_store, parsed, child, output)) is not None:
                needed.add(found)
    return children, needed


async def _build_graph(drv_store: AsyncStore, roots: Mapping[str, str | None]) -> _BuildGraph:
    """The build closure of *roots* from the ``.drv`` files in *drv_store*. Reads, builds nothing.

    An output's build reads the outputs it names of each input derivation, and
    each input source. Every output of one ``.drv`` reads the same.
    """
    store_dir = await drv_store.store_dir()
    parsed: dict[str, AsyncDerivation] = {}
    reads: dict[str, tuple[str, ...]] = {}
    derivations: dict[str, tuple[str, AsyncDerivation]] = {}
    builds: list[Build] = []
    done: set[str] = set()
    pending = [drv for drv in roots.values() if drv is not None]
    for path, drv in roots.items():
        if drv is None:
            reads.setdefault(path, ())
    while pending:
        drv = pending.pop()
        if drv in done:
            continue
        done.add(drv)
        derivation = await _read(drv_store, parsed, drv)
        children, needed = await _inputs(drv_store, parsed, derivation, store_dir)
        pending.extend(children)
        sources = [await drv_store.print_store_path(src) for src in await derivation.input_srcs()]
        for src in sources:
            reads.setdefault(src, ())
        built = tuple(sorted(needed.union(sources)))
        outputs = await derivation.outputs()
        written: list[str] = []
        for output in sorted(outputs):
            if (out := await _output_path(drv_store, parsed, drv, output)) is not None:
                reads[out] = built
                derivations[out] = (drv, derivation)
                written.append(out)
        fixed = any(isinstance(output, DerivationOutputCAFixed) for output in outputs.values())
        builds.append(
            Build(
                drv_path=drv,
                name=await derivation.name(),
                system=await derivation.system(),
                builder=await derivation.builder(),
                args=tuple(await derivation.args()),
                outputs=tuple(written),
                reads=built,
                input_drvs=tuple(sorted(children)),
                fixed_output=fixed,
                rev=(await _attrs(derivation)).get("rev") if fixed else None,
            )
        )
    return _BuildGraph(dict(sorted(reads.items())), derivations, tuple(sorted(builds, key=lambda b: b.drv_path)))


async def build_bom(
    drv_store: AsyncStore,
    roots: Mapping[str, str | None],
    *,
    name: str,
    version: str | None = None,
    state: AsyncEvalState | None = None,
) -> Bom:
    """The SBOM of what builds *roots*: every output and source their ``.drv`` files read, recursively.

    A separate document from :func:`closure_bom`'s: a Nix build runs ahead
    of time in a sandbox, so what it reads is not what a deployment runs.
    *roots* maps each path to the ``.drv`` that builds it, or None for a
    source (:func:`context_derivations`). Builds nothing, and needs no path
    of the closure to be valid. *state*, when it recorded its derivations,
    gives each output the value it coerced to it; any other output's
    record comes from its ``.drv``, and a source's from its name. Each
    ``.drv`` is a :class:`Build`, which CycloneDX writes as ``formulation``.
    """
    graph = await _build_graph(drv_store, roots)
    records = await recorded_records(state, graph.reads) if state is not None else {}
    for path in graph.reads:
        if path not in records:
            built = graph.derivations.get(path)
            records[path] = (
                await _record_of_derivation(drv_store, built[1], built[0], path) if built else from_store_path(path)
            )
    records[name] = Record(path=name, name=name, version=version)
    wanted = tuple(sorted(roots))
    # The build edges stand where `closure_bom` puts references.
    listed = inventory(records, name, {**graph.reads, name: wanted}, buildtime=False, always=frozenset(wanted))
    texts = await _patch_texts(drv_store, listed)
    return Bom(listed, texts, buildtime=True, builds=graph.builds)


async def recorded_records(state: AsyncEvalState, paths: Iterable[str]) -> dict[str, Record]:
    """The records of the values *state* recorded for *paths*, and of their sibling outputs.

    Empty unless *state* recorded its derivations as it evaluated.
    """
    recorded = set(await state.recorded_out_paths())
    wanted = sorted(recorded.intersection(paths))
    if not wanted:
        return {}
    values = await state.make_list()
    for path in wanted:
        found = await state.recorded_derivation(path)
        if found is not None:
            await state.list_append(values, found)
    walk = await (await helper(state, "sbom")).get("values")
    await state.force(walk)
    return parse_walk(await (await walk(values)).string_value())


async def closure_bom(  # noqa: PLR0913 -- keyword-only, one per source of what the SBOM knows
    store: AsyncStore,
    roots: Iterable[str],
    *,
    name: str,
    version: str | None = None,
    drv_store: AsyncStore | None = None,
    state: AsyncEvalState | None = None,
    embedded_cache: EmbeddedCache | None = None,
) -> Bom:
    """The SBOM of the runtime closure of *roots*, valid paths in *store*. Builds nothing.

    *name* and *version* name the target, which depends on every root.
    *state*, when it recorded its derivations, gives each path the value it
    coerced to it. Any other path's record comes from its deriver, which
    *drv_store* holds when *store* does not: the local store after
    evaluation, for paths that live in a remote one. *embedded_cache* keeps
    what each path names inside itself between calls.
    """
    wanted = sorted(set(roots))
    runtime = await runtime_closure(store, wanted)
    readers = drv_store or store
    records = await recorded_records(state, runtime) if state is not None else {}
    for path in runtime:
        if path not in records:
            records[path] = await record_of_path(store, readers, path)
    records = await with_store_facts(store, records, runtime)
    target = Record(path=name, name=name, version=version)
    records[name] = target
    # A root is what the caller deploys: listed though it has no version.
    listed = inventory(records, name, {**runtime, name: tuple(wanted)}, buildtime=False, always=frozenset(wanted))
    listed = with_embedded(listed, await embedded_packages(store, runtime, cache=embedded_cache))
    return Bom(listed, await _patch_texts(store, listed), buildtime=False)


def _serial(bom: Bom, kind: str) -> str:
    return serial_uuid(f"{bom.inventory.target.path}:{kind}:{bom.buildtime}").urn


def with_cpe_dictionary(bom: Bom, dictionary: CpeDictionary) -> Bom:
    """*bom*, where each package that states no CPE lists the one *dictionary* suggests, as evidence."""
    return replace(bom, inventory=with_generated_cpes(bom.inventory, dictionary))


def cyclonedx(bom: Bom) -> dict[str, JsonValue]:
    """The CycloneDX 1.7 document of *bom*. Its serial number follows from the target."""
    return _sbom_cyclonedx.document(
        bom.inventory,
        serial_number=_serial(bom, "cyclonedx"),
        tool_version=importlib.metadata.version("pynix"),
        patches=bom.patches,
        builds=bom.builds,
    )


def cyclonedx_component(bom: Bom, record: Record, ref: str | None = None) -> dict[str, JsonValue]:
    """The CycloneDX component of one *record* of *bom*, for a caller that writes its own document.

    *ref* is its ``bom-ref``, by default the record's own; the packages its binaries name sit inside it.
    """
    return _sbom_cyclonedx.component(record, bom.patches, ref=ref, embedded=bom.inventory.embedded.get(record.path, ()))


def cyclonedx_embedded_dependencies(bom: Bom, refs: Mapping[str, str] | None = None) -> list[JsonValue]:
    """The ``dependencies`` entries of the packages inside *bom*'s components; *refs* as for :func:`cyclonedx_formulation`."""
    return _sbom_cyclonedx.embedded_dependencies(bom.inventory, refs)


def cyclonedx_formulation(bom: Bom, refs: Mapping[str, str] | None = None) -> list[JsonValue]:
    """How *bom*'s components were built, as CycloneDX ``formulation``; empty when *bom* holds no builds.

    *refs* gives the ``bom-ref`` of each listed path, for a caller that names
    its components its own way; by default each record's own.
    """
    return _sbom_cyclonedx.formulation(bom.inventory, bom.builds, refs)


def cyclonedx_compositions(bom: Bom, refs: Mapping[str, str] | None = None) -> list[JsonValue]:
    """How complete each component's ``dependsOn`` is, and its parts, as CycloneDX ``compositions``.

    *refs* gives the ``bom-ref`` of each listed path, as for :func:`cyclonedx_formulation`.
    """
    return _sbom_cyclonedx.compositions(bom.inventory, refs)


def cyclonedx_vulnerabilities(bom: Bom) -> list[JsonValue]:
    """The vulnerabilities Nix states for *bom* (CVE patches, ``meta.knownVulnerabilities``), for a caller that writes its own document."""
    listed = (bom.inventory.target, *bom.inventory.components)
    return _sbom_cyclonedx.vulnerabilities({record.path: record for record in listed}.values(), bom.patches)


def spdx(bom: Bom, *, created: str) -> dict[str, JsonValue]:
    """The SPDX 2.3 document of *bom*. *created* is the time of the document, in ``YYYY-MM-DDThh:mm:ssZ``."""
    return _sbom_spdx.document(
        bom.inventory,
        namespace=_serial(bom, "spdx"),
        created=created,
        tool_version=importlib.metadata.version("pynix"),
    )
