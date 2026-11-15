"""CycloneDX 1.7 JSON for ``pynix sbom``.

The component mapping is bombon's (``rust/transformer/src/cyclonedx.rs``),
field for field. bombon writes 1.5 and converts; this module writes 1.7, so
``evidence.identity`` is a list.
"""

from __future__ import annotations

from collections.abc import Mapping

from pynix._evaluate import JsonValue
from pynix._impl._sbom_model import Inventory, License, Maintainer, Origin, Record, sri_digest

SPEC_VERSION = "1.7"


def _hashes(sri: str | None) -> list[JsonValue] | None:
    digest = sri_digest(sri)
    return [{"alg": digest.algorithm, "content": digest.hex}] if digest else None


def _license(entry: License) -> JsonValue:
    if entry.spdx_id:
        return {"license": {"id": entry.spdx_id}}
    return {"license": {"name": entry.name or "NOASSERTION"}}


def _licenses(record: Record) -> list[JsonValue]:
    if record.license_expression:
        # CycloneDX allows one expression, alone in the list.
        return [{"expression": record.license_expression}]
    return [_license(entry) for entry in record.licenses]


def _external_references(record: Record) -> list[JsonValue]:
    references: list[JsonValue] = []
    if record.source is not None:
        hashes = _hashes(record.source.hash)
        for url in record.source.urls:
            # A fetch with a revision checks out a repository; any other fetches an archive.
            reference: dict[str, JsonValue] = {
                "type": "vcs" if record.source.rev else "source-distribution",
                "url": url,
            }
            if record.source.rev:
                reference["comment"] = f"revision {record.source.rev}"
            if hashes:
                reference["hashes"] = hashes
            references.append(reference)
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
    if record.purls:
        # Declared by the package (`meta.identifiers.purls`), not guessed.
        purls: list[JsonValue] = [
            {"technique": "manifest-analysis", "confidence": 1.0, "value": purl} for purl in record.purls
        ]
        identity.append({"field": "purl", "methods": purls})
    return {"identity": identity} if identity else None


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
    if record.long_description:
        pairs.append(("nix:longDescription", record.long_description))
    flags = {"nix:unfree": record.unfree, "nix:insecure": record.insecure, "nix:broken": record.broken}
    pairs.extend((name, "true") for name, value in flags.items() if value)
    pairs.extend(("nix:maintainer", _maintainer(person)) for person in record.maintainers)
    return [{"name": name, "value": value} for name, value in pairs]


def component(record: Record, patches: Mapping[str, str]) -> dict[str, JsonValue]:
    """The CycloneDX component of *record*.

    *patches* holds the text of each patch file that could be read. A patch
    that is not there is left out, as bombon leaves it out.
    """
    result: dict[str, JsonValue] = {
        "type": "application",
        "bom-ref": record.ref,
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
    texts = [patches[path] for path in record.patches if path in patches]
    if texts:
        diffs: list[JsonValue] = [
            {"type": "unofficial", "diff": {"text": {"contentType": "text/plain", "content": text}}} for text in texts
        ]
        result["pedigree"] = {"patches": diffs}
    if properties := _properties(record):
        result["properties"] = properties
    return result


def document(
    inventory: Inventory,
    *,
    serial_number: str,
    tool_version: str,
    patches: Mapping[str, str],
) -> dict[str, JsonValue]:
    """The CycloneDX document of *inventory*."""
    refs = {record.path: record.ref for record in (inventory.target, *inventory.components)}
    dependencies: list[JsonValue] = []
    for path in sorted(inventory.dependencies, key=refs.__getitem__):
        depends_on: list[JsonValue] = [refs[ref] for ref in sorted(inventory.dependencies[path], key=refs.__getitem__)]
        dependencies.append({"ref": refs[path], "dependsOn": depends_on})
    tool: dict[str, JsonValue] = {
        "type": "application",
        "name": "pynix",
        "version": tool_version,
        "description": "Nix CLI built on nanopynix",
        "licenses": [{"license": {"id": "Apache-2.0"}}],
        "externalReferences": [{"type": "website", "url": "https://github.com/nixidae/nanopynix"}],
    }
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": serial_number,
        "version": 1,
        "metadata": {
            "tools": {"components": [tool]},
            "component": component(inventory.target, patches),
        },
        # A `bom-ref` names one object, and the target is `metadata.component`.
        "components": [
            component(record, patches) for record in inventory.components if record.path != inventory.target.path
        ],
        "dependencies": dependencies,
    }
