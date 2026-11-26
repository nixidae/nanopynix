"""CycloneDX 1.7 JSON for ``pynix sbom``.

The component mapping is bombon's (``rust/transformer/src/cyclonedx.rs``),
field for field. bombon writes 1.5 and converts; this module writes 1.7, so
``evidence.identity`` is a list.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from packageurl import PackageURL

from pynix._evaluate import JsonValue
from pynix._impl._sbom_embedded import Package
from pynix._impl._sbom_model import (
    Aggregate,
    Build,
    Inventory,
    License,
    Maintainer,
    Origin,
    Record,
    cve_ids,
    sri_digest,
)

SPEC_VERSION = "1.7"


def _hashes(sri: str | None) -> list[JsonValue] | None:
    digest = sri_digest(sri)
    return [{"alg": digest.algorithm, "content": digest.hex}] if digest else None


def _license(entry: License, acknowledged: dict[str, JsonValue]) -> JsonValue:
    if entry.spdx_id:
        return {"license": {"id": entry.spdx_id, **acknowledged}}
    return {"license": {"name": entry.name or "NOASSERTION", **acknowledged}}


def _licenses(record: Record) -> list[JsonValue]:
    # A license from the package's own metadata, not nixpkgs' `meta`, says so.
    acknowledged: dict[str, JsonValue] = {"acknowledgement": "declared"} if record.license_declared_by else {}
    if record.license_expression:
        # CycloneDX allows one expression, alone in the list.
        return [{"expression": record.license_expression, **acknowledged}]
    return [_license(entry, acknowledged) for entry in record.licenses]


def _external_references(record: Record) -> list[JsonValue]:
    references: list[JsonValue] = []
    if (source := record.source) is not None:
        # The fetch's hash is of what it fetched: the archive, or the checkout when there is none.
        hashes = _hashes(source.hash)
        for url in source.urls:
            reference: dict[str, JsonValue] = {"type": "source-distribution", "url": url}
            if hashes:
                reference["hashes"] = hashes
            references.append(reference)
        if source.repository:
            vcs: dict[str, JsonValue] = {"type": "vcs", "url": source.repository}
            if source.rev:
                vcs["comment"] = f"revision {source.rev}"
            if hashes and not source.urls:
                vcs["hashes"] = hashes
            references.append(vcs)
    references.extend({"type": "website", "url": url} for url in record.homepages)
    references.extend({"type": "release-notes", "url": url} for url in record.changelogs)
    if record.download_page:
        references.append({"type": "distribution", "url": record.download_page})
    return references


def _evidence(record: Record) -> JsonValue | None:
    identity: list[JsonValue] = []
    if not record.cpe and record.possible_cpes:
        confidence = 1.0 / len(record.possible_cpes)
        cpes: list[JsonValue] = [
            {"technique": "manifest-analysis", "confidence": confidence, "value": cpe} for cpe in record.possible_cpes
        ]
        identity.append({"field": "cpe", "methods": cpes})
    elif record.generated_cpe:
        # A product name looked up in the NVD CPE dictionary: no ecosystem, no vendor from the package.
        guess: JsonValue = {"technique": "other", "confidence": 0.5, "value": record.generated_cpe}
        identity.append({"field": "cpe", "methods": [guess]})
    if record.purls:
        # Declared by the package (`meta.identifiers.purls`), not guessed.
        purls: list[JsonValue] = [
            {"technique": "manifest-analysis", "confidence": 1.0, "value": purl} for purl in record.purls
        ]
        identity.append({"field": "purl", "methods": purls})
    return {"identity": identity} if identity else None


def _ancestor(purl: str) -> dict[str, JsonValue] | None:
    try:
        parsed = PackageURL.from_string(purl)
    except ValueError:
        return None
    result: dict[str, JsonValue] = {"type": "library"}
    if parsed.namespace:
        result["group"] = parsed.namespace
    result["name"] = parsed.name
    if parsed.version:
        result["version"] = parsed.version
    result["purl"] = purl
    return result


def _ancestors(record: Record) -> list[JsonValue]:
    """The upstream releases the source of *record* declares, as the components it derives from."""
    if record.source is None:
        return []
    found = (_ancestor(purl) for purl in record.source.purls if purl not in record.purls)
    return [entry for entry in found if entry is not None]


def _pedigree(record: Record, patches: Mapping[str, str]) -> dict[str, JsonValue]:
    pedigree: dict[str, JsonValue] = {}
    if ancestors := _ancestors(record):
        pedigree["ancestors"] = ancestors
    if applied := _patches(record, patches):
        pedigree["patches"] = applied
    return pedigree


def _nvd(cve: str) -> dict[str, JsonValue]:
    return {"name": "NVD", "url": f"https://nvd.nist.gov/vuln/detail/{cve}"}


def _patches(record: Record, texts: Mapping[str, str]) -> list[JsonValue]:
    """Each patch with its text, or with a CVE in its name; a patch with neither says nothing."""
    result: list[JsonValue] = []
    for path in record.patches:
        text, cves = texts.get(path), cve_ids(PurePosixPath(path).name)
        if text is None and not cves:
            continue
        entry: dict[str, JsonValue] = {"type": "unofficial"}
        if text is not None:
            entry["diff"] = {"text": {"contentType": "text/plain", "content": text}}
        if cves:
            entry["resolves"] = [{"type": "security", "id": cve, "source": _nvd(cve)} for cve in cves]
        result.append(entry)
    return result


def vulnerabilities(records: Iterable[Record], texts: Mapping[str, str]) -> list[JsonValue]:
    """What Nix itself states about vulnerabilities, as VEX.

    A CVE that a patch's name gives is resolved, with pedigree when the
    patch text is in the document. ``meta.knownVulnerabilities`` marks a
    package affected: one vulnerability for each CVE an entry names, and
    one with no identifier for an entry that names none.
    """
    # (id, description, state, detail) -> the components it affects
    found: dict[tuple[str, str, str, str], set[str]] = {}
    for record in records:
        for path in record.patches:
            name = PurePosixPath(path).name
            state = "resolved_with_pedigree" if path in texts else "resolved"
            for cve in cve_ids(name):
                found.setdefault((cve, "", state, f"the Nix build applies {name}"), set()).add(record.ref)
        for entry in record.known_vulnerabilities:
            for key in [(cve, "", "exploitable", entry) for cve in cve_ids(entry)] or [("", entry, "exploitable", "")]:
                found.setdefault(key, set()).add(record.ref)
    result: list[JsonValue] = []
    for (cve, description, state, detail), refs in sorted(found.items()):
        vulnerability: dict[str, JsonValue] = {}
        if cve:
            vulnerability["id"] = cve
            vulnerability["source"] = _nvd(cve)
        if description:
            vulnerability["description"] = description
        analysis: dict[str, JsonValue] = {"state": state}
        if detail:
            analysis["detail"] = detail
        vulnerability["analysis"] = analysis
        vulnerability["affects"] = [{"ref": ref} for ref in sorted(refs)]
        result.append(vulnerability)
    return result


def _maintainer(person: Maintainer) -> str:
    text = person.name or person.github or person.email or ""
    if person.email and person.email != text:
        text += f" <{person.email}>"
    if person.github and person.github != text:
        text += f" (github:{person.github})"
    return text


def _properties(record: Record) -> list[JsonValue]:
    pairs: list[tuple[str, str]] = []
    if record.origin is not Origin.VALUE:
        # No `meta` behind it: a missing license or CPE is unknown, not absent.
        pairs.append(("nix:recordOrigin", record.origin.value))
    if record.path.startswith("/"):
        pairs.append(("nix:outputPath", record.path))
    if record.output_name:
        pairs.append(("nix:outputName", record.output_name))
    if record.drv_path:
        pairs.append(("nix:drvPath", record.drv_path))
    if record.nar_size is not None:
        pairs.append(("nix:narSize", str(record.nar_size)))
    if record.position:
        pairs.append(("nix:position", record.position))
    if record.main_program:
        pairs.append(("nix:mainProgram", record.main_program))
    if record.license_declared_by:
        pairs.append(("nix:licenseDeclaredBy", record.license_declared_by))
    if record.long_description:
        pairs.append(("nix:longDescription", record.long_description))
    flags = {"nix:unfree": record.unfree, "nix:insecure": record.insecure, "nix:broken": record.broken}
    pairs.extend((name, "true") for name, value in flags.items() if value)
    pairs.extend(("nix:maintainer", _maintainer(person)) for person in record.maintainers)
    return [{"name": name, "value": value} for name, value in pairs]


def embedded_ref(parent: str, purl: str) -> str:
    """The ``bom-ref`` of the package *purl* inside the component *parent*: one package in two components is two."""
    return f"{parent}#{purl}"


def _embedded_component(package: Package, parent: str) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "type": "application" if package.root else "library",
        "bom-ref": embedded_ref(parent, package.purl),
        "name": package.name,
    }
    if package.version:
        result["version"] = package.version
    result["scope"] = "required"
    result["purl"] = package.purl
    if (declared := package.licenses) is not None:
        acknowledged: list[JsonValue] = (
            [{"expression": declared.expression, "acknowledgement": "declared"}]
            if declared.expression
            else [{"license": {"name": name, "acknowledgement": "declared"}} for name in declared.names]
        )
        result["licenses"] = acknowledged
    if package.files:
        result["evidence"] = {"occurrences": [{"location": file} for file in package.files]}
    if package.properties:
        result["properties"] = [{"name": name, "value": value} for name, value in package.properties]
    return result


def component(
    record: Record, patches: Mapping[str, str], *, ref: str | None = None, embedded: Iterable[Package] = ()
) -> dict[str, JsonValue]:
    """The CycloneDX component of *record*, with the packages its binaries name (*embedded*) inside it.

    *patches* holds the text of each patch file that could be read. A patch
    that is not there is left out, as bombon leaves it out. *ref* replaces
    the record's own ``bom-ref``.
    """
    own = ref or record.ref
    result: dict[str, JsonValue] = {
        "type": "application",
        "bom-ref": own,
        "name": record.name,
        "version": record.version or "",
        "scope": "required",
        "purl": record.purl,
    }
    # The NAR hash names the bytes a deployment holds; a fixed output's own hash is the fallback.
    if hashes := _hashes(record.nar_hash) or _hashes(record.output_hash):
        result["hashes"] = hashes
    if record.description:
        result["description"] = record.description
    if licenses := _licenses(record):
        result["licenses"] = licenses
    if record.cpe:
        result["cpe"] = record.cpe
    if evidence := _evidence(record):
        result["evidence"] = evidence
    if references := _external_references(record):
        result["externalReferences"] = references
    if pedigree := _pedigree(record, patches):
        result["pedigree"] = pedigree
    if properties := _properties(record):
        result["properties"] = properties
    inside: list[JsonValue] = [_embedded_component(package, own) for package in embedded]
    if inside:
        result["components"] = inside
    return result


def _task_ref(build: Build) -> str:
    # The store hash alone: `uid` and `name` hold the rest. With the full base
    # name, sg1's task graph is 13.6 MB of a 46 MB document. `task:` keeps it
    # apart from a component, as a `.drv` can itself be a root.
    return f"task:{PurePosixPath(build.drv_path).name.partition('-')[0]}"


def _task_types(build: Build) -> list[JsonValue]:
    if not build.fixed_output:
        return ["build"]
    return ["clone"] if build.rev else ["copy"]


def _task(build: Build, ref_of: Mapping[str, str]) -> dict[str, JsonValue]:
    inputs = sorted({ref_of[path] for path in build.reads if path in ref_of})
    outputs = sorted({ref_of[path] for path in build.outputs if path in ref_of})
    properties: list[JsonValue] = [{"name": "nix:system", "value": build.system}]
    if build.rev:
        properties.append({"name": "nix:rev", "value": build.rev})
    task: dict[str, JsonValue] = {
        "bom-ref": _task_ref(build),
        "uid": build.drv_path,
        "name": build.name,
        "taskTypes": _task_types(build),
        "steps": [{"name": "builder", "commands": [{"executed": shlex.join([build.builder, *build.args])}]}],
    }
    if inputs:
        task["inputs"] = [{"resource": {"ref": ref}} for ref in inputs]
    if outputs:
        task["outputs"] = [{"type": "artifact", "resource": {"ref": ref}} for ref in outputs]
    task["properties"] = properties
    return task


def formulation(
    inventory: Inventory, builds: Iterable[Build], refs: Mapping[str, str] | None = None
) -> list[JsonValue]:
    """How the components of *inventory* were built: one workflow, with one task for each ``.drv``.

    A task reads and writes components; a path with no component is left
    out of its inputs and outputs, and the task stays, so the task graph is
    the whole ``.drv`` graph. *refs* gives each listed path its ``bom-ref``.
    """
    ordered = list(builds)
    if not ordered:
        return []
    names = _names(inventory, refs)
    ref_of = {path: names[kept] for path, kept in inventory.aliases.items() if kept in names}
    task_of = {build.drv_path: _task_ref(build) for build in ordered}
    dependencies: list[JsonValue] = []
    for build in ordered:
        depends_on: list[JsonValue] = [task_of[drv] for drv in build.input_drvs if drv in task_of]
        dependencies.append({"ref": task_of[build.drv_path], "dependsOn": depends_on})
    target = inventory.target.ref
    workflow: dict[str, JsonValue] = {
        "bom-ref": f"workflow:{target}",
        "uid": target,
        "name": f"nix build of {inventory.target.name}",
        "taskTypes": ["build"],
        "tasks": [_task(build, ref_of) for build in ordered],
        "taskDependencies": dependencies,
    }
    return [{"bom-ref": f"formula:{target}", "workflows": [workflow]}]


def _names(inventory: Inventory, refs: Mapping[str, str] | None) -> dict[str, str]:
    listed = {record.path: record.ref for record in (inventory.target, *inventory.components)}
    return {**listed, **refs} if refs is not None else listed


def _aggregate(package: Package) -> Aggregate:
    if package.complete is None:
        return Aggregate.UNKNOWN
    return Aggregate.COMPLETE if package.complete else Aggregate.INCOMPLETE


def embedded_dependencies(inventory: Inventory, refs: Mapping[str, str] | None = None) -> list[JsonValue]:
    """The edges between the packages inside each component, one entry for each package.

    *refs* gives each listed path its ``bom-ref``.
    """
    names = _names(inventory, refs)
    result: list[JsonValue] = []
    for path, packages in inventory.embedded.items():
        if path not in names:
            continue
        parent, present = names[path], {package.purl for package in packages}
        for package in packages:
            depends_on: list[JsonValue] = [
                embedded_ref(parent, purl) for purl in package.dependencies if purl in present
            ]
            result.append({"ref": embedded_ref(parent, package.purl), "dependsOn": depends_on})
    return result


def _claims(inventory: Inventory, names: Mapping[str, str]) -> dict[Aggregate, dict[str, set[str]]]:
    """The refs each aggregate claims, by the kind of claim: ``assemblies`` or ``dependencies``."""
    grouped: dict[Aggregate, dict[str, set[str]]] = {}

    def claim(aggregate: Aggregate, kind: str, ref: str) -> None:
        grouped.setdefault(aggregate, {}).setdefault(kind, set()).add(ref)

    for path, aggregate in inventory.completeness.items():
        if path in names:
            claim(aggregate, "dependencies", names[path])
    for path, packages in inventory.embedded.items():
        if path in names:
            claim(Aggregate.INCOMPLETE, "assemblies", names[path])
            for package in packages:
                claim(_aggregate(package), "dependencies", embedded_ref(names[path], package.purl))
    return grouped


def compositions(inventory: Inventory, refs: Mapping[str, str] | None = None) -> list[JsonValue]:
    """How complete the ``dependsOn`` of each component is, and the parts of each one with packages inside.

    One composition for each aggregate. A claim about one component's own
    edges, not about the edges below it. A component with packages inside
    holds more than they name: C code, or Rust's standard library.
    *refs* gives each listed path its ``bom-ref``.
    """
    grouped = _claims(inventory, _names(inventory, refs))
    result: list[JsonValue] = []
    for aggregate in Aggregate:
        if aggregate in grouped:
            entry: dict[str, JsonValue] = {
                "bom-ref": f"composition:{aggregate}:{inventory.target.ref}",
                "aggregate": aggregate.value,
            }
            for kind in ("assemblies", "dependencies"):
                if kind in grouped[aggregate]:
                    entry[kind] = [*sorted(grouped[aggregate][kind])]
            result.append(entry)
    return result


def document(
    inventory: Inventory,
    *,
    serial_number: str,
    tool_version: str,
    patches: Mapping[str, str],
    builds: Iterable[Build] = (),
) -> dict[str, JsonValue]:
    """The CycloneDX document of *inventory*."""
    refs = {record.path: record.ref for record in (inventory.target, *inventory.components)}
    dependencies: list[JsonValue] = []
    for path in sorted(inventory.dependencies, key=refs.__getitem__):
        depends_on: list[JsonValue] = [refs[ref] for ref in sorted(inventory.dependencies[path], key=refs.__getitem__)]
        dependencies.append({"ref": refs[path], "dependsOn": depends_on})
    dependencies.extend(embedded_dependencies(inventory))
    tool: dict[str, JsonValue] = {
        "type": "application",
        "name": "pynix",
        "version": tool_version,
        "description": "Nix CLI built on nanopynix",
        "licenses": [{"license": {"id": "Apache-2.0"}}],
        "externalReferences": [{"type": "website", "url": "https://github.com/nixidae/nanopynix"}],
    }
    result: dict[str, JsonValue] = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": serial_number,
        "version": 1,
        "metadata": {
            "tools": {"components": [tool]},
            "component": component(
                inventory.target, patches, embedded=inventory.embedded.get(inventory.target.path, ())
            ),
        },
        # A `bom-ref` names one object, and the target is `metadata.component`.
        "components": [
            component(record, patches, embedded=inventory.embedded.get(record.path, ()))
            for record in inventory.components
            if record.path != inventory.target.path
        ],
        "dependencies": dependencies,
    }
    if composed := compositions(inventory):
        result["compositions"] = composed
    listed = {record.path: record for record in (inventory.target, *inventory.components)}
    if stated := vulnerabilities(listed.values(), patches):
        result["vulnerabilities"] = stated
    if formulas := formulation(inventory, builds):
        result["formulation"] = formulas
    return result
