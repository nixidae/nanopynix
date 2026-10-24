"""SPDX 2.3 JSON for ``pynix sbom``.

The package mapping is sbomnix's (``src/sbomnix/exporters.py``), from the same
inventory that the CycloneDX document renders.
"""

from __future__ import annotations

import re

from pynix._evaluate import JsonValue
from pynix._impl._sbom_model import Inventory, Record, sri_digest

SPEC_VERSION = "SPDX-2.3"

_NOASSERTION = "NOASSERTION"

#: The schemes that an SPDX `downloadLocation` takes. `mirror://` of nixpkgs is
#: not one of them, so such a source has no assertion.
_DOWNLOAD_SCHEMES = ("http://", "https://", "ftp://", "git://", "ssh://", "svn://", "sftp://")


def spdx_id(record: Record) -> str:
    """The `SPDXRef-` identifier of *record*: its store path name, in the allowed characters."""
    return "SPDXRef-" + re.sub(r"[^-.a-zA-Z0-9]", "-", record.ref)


def _download_location(record: Record) -> str:
    urls = record.source.urls if record.source else ()
    return next((url for url in urls if url.startswith(_DOWNLOAD_SCHEMES)), _NOASSERTION)


def _declared_license(record: Record) -> str:
    """The license of *record* when it has exactly one SPDX identifier.

    A list in `meta.license` does not say whether the licenses apply together
    or as a choice, so more than one gives no assertion, as in sbomnix.
    """
    if len(record.licenses) == 1 and record.licenses[0].spdx_id:
        return record.licenses[0].spdx_id
    return _NOASSERTION


def package(record: Record) -> dict[str, JsonValue]:
    """The SPDX package of *record*."""
    result: dict[str, JsonValue] = {
        "SPDXID": spdx_id(record),
        "name": record.name,
        "versionInfo": record.version or "",
        "downloadLocation": _download_location(record),
        "filesAnalyzed": False,
        "licenseConcluded": _NOASSERTION,
        "licenseDeclared": _declared_license(record),
        "copyrightText": _NOASSERTION,
    }
    if record.homepage:
        result["homepage"] = record.homepage
    if record.description:
        result["summary"] = record.description
    if digest := sri_digest(record.output_hash):
        result["checksums"] = [{"algorithm": digest.algorithm.replace("-", ""), "checksumValue": digest.hex}]
    references: list[JsonValue] = [
        {"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": record.purl}
    ]
    if record.cpe:
        references.append(
            {"referenceCategory": "SECURITY", "referenceType": "cpe23Type", "referenceLocator": record.cpe}
        )
    result["externalRefs"] = references
    return result


def document(
    inventory: Inventory,
    *,
    namespace: str,
    created: str,
    tool_version: str,
) -> dict[str, JsonValue]:
    """The SPDX document of *inventory*.

    *created* is the time of the document, in `YYYY-MM-DDThh:mm:ssZ`.
    """
    records = {record.path: record for record in (inventory.target, *inventory.components)}
    target_id = spdx_id(inventory.target)
    relationships: list[JsonValue] = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": target_id}
    ]
    for path in sorted(inventory.dependencies, key=lambda p: records[p].ref):
        relationships.extend(
            {
                "spdxElementId": spdx_id(records[path]),
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": spdx_id(records[ref]),
            }
            for ref in sorted(inventory.dependencies[path], key=lambda p: records[p].ref)
        )
    target = inventory.target
    name = f"{target.name}-{target.version}" if target.version else target.name
    return {
        "spdxVersion": SPEC_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": namespace,
        "creationInfo": {"created": created, "creators": [f"Tool: pynix-{tool_version}"]},
        "packages": [package(record) for record in records.values()],
        "relationships": relationships,
    }
