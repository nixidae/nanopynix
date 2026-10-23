"""CycloneDX 1.7 JSON for ``pynix sbom``.

The component mapping is bombon's (``rust/transformer/src/cyclonedx.rs``),
field for field. bombon writes 1.5 and converts; this module writes 1.7, so
``evidence.identity`` is a list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._impl._sbom_model import sri_digest

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping

    from nanopynix.models import JsonValue
    from pynix._impl._sbom_model import Inventory, License, Record

SPEC_VERSION = "1.7"


def _hashes(sri: str | None) -> list[JsonValue] | None:
    digest = sri_digest(sri)
    return [{"alg": digest.algorithm, "content": digest.hex}] if digest else None


def _license(entry: License) -> JsonValue:
    if entry.spdx_id:
        return {"license": {"id": entry.spdx_id}}
    return {"license": {"name": entry.name or "NOASSERTION"}}


def _external_references(record: Record) -> list[JsonValue]:
    references: list[JsonValue] = []
    if record.source is not None:
        hashes = _hashes(record.source.hash)
        for url in record.source.urls:
            reference: dict[str, JsonValue] = {"type": "vcs", "url": url}
            if hashes:
                reference["hashes"] = hashes
            references.append(reference)
    if record.homepage:
        references.append({"type": "website", "url": record.homepage})
    return references


def _evidence(possible_cpes: tuple[str, ...]) -> JsonValue:
    confidence = 1.0 / len(possible_cpes)
    methods: list[JsonValue] = [
        {"technique": "manifest-analysis", "confidence": confidence, "value": cpe} for cpe in possible_cpes
    ]
    return {"identity": [{"field": "cpe", "methods": methods}]}


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
    if hashes := _hashes(record.output_hash):
        result["hashes"] = hashes
    if record.description:
        result["description"] = record.description
    if record.licenses:
        result["licenses"] = [_license(entry) for entry in record.licenses]
    if record.cpe:
        result["cpe"] = record.cpe
    elif record.possible_cpes:
        result["evidence"] = _evidence(record.possible_cpes)
    if references := _external_references(record):
        result["externalReferences"] = references
    texts = [patches[path] for path in record.patches if path in patches]
    if texts:
        diffs: list[JsonValue] = [
            {"type": "unofficial", "diff": {"text": {"contentType": "text/plain", "content": text}}} for text in texts
        ]
        result["pedigree"] = {"patches": diffs}
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
