"""CPEs that a dictionary of NVD product names suggests."""

from __future__ import annotations

import json
from typing import cast

import pytest
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from pynix._impl._sbom_cpe import CpeDictionary, bind, generated_cpe, with_generated_cpes
from pynix._impl._sbom_cyclonedx import document
from pynix._impl._sbom_model import Origin, Record, inventory, parse_walk

_CSV = '"vendor","product"\n"gnu","glibc"\n"haxx","curl"\n"curl_project","curl"\n"1c","1c\\:enterprise"\n'
_DICTIONARY = CpeDictionary.parse(_CSV)


def test_a_product_with_one_vendor_gives_it_and_one_with_two_gives_none() -> None:
    assert _DICTIONARY.vendors == {"glibc": "gnu", "1c\\:enterprise": "1c"}


def test_a_dictionary_without_the_columns_is_refused() -> None:
    with pytest.raises(ValueError, match="vendor and product"):
        CpeDictionary.parse('"name","version"\n"glibc","2.40"\n')


@pytest.mark.parametrize(
    ("value", "bound"),
    [
        ("2.40", "2.40"),
        ("x.org", "x.org"),
        ("man-db", "man-db"),
        ("1.0+git", "1.0\\+git"),
        ("1c:enterprise", "1c\\:enterprise"),
    ],
)
def test_a_value_binds_as_a_formatted_string(value: str, bound: str) -> None:
    assert bind(value) == bound


def test_a_package_gets_the_vendor_of_its_product() -> None:
    record = Record(path="/nix/store/a-glibc-2.40", name="glibc", version="2.40")
    assert generated_cpe(record, _DICTIONARY) == "cpe:2.3:a:gnu:glibc:2.40:*:*:*:*:*:*:*"


def test_the_product_and_part_come_from_cpe_parts() -> None:
    record = Record(path="/p", name="enterprise", version="8", cpe_part="o", cpe_product="1c:enterprise")
    assert generated_cpe(record, _DICTIONARY) == "cpe:2.3:o:1c:1c\\:enterprise:8:*:*:*:*:*:*:*"


@pytest.mark.parametrize(
    "record",
    [
        Record(path="/p", name="glibc", version="2.40", cpe="cpe:2.3:a:gnu:glibc:2.40:*:*:*:*:*:*:*"),
        Record(path="/p", name="glibc", version="2.40", possible_cpes=("cpe:2.3:a:gnu:glibc:2.40:*:*:*:*:*:*:*",)),
        Record(path="/p", name="glibc", version=None),
        Record(path="/p", name="glibc", version="2.40", language="perl"),
        Record(path="/p", name="curl", version="8.0"),
        Record(path="/p", name="hello", version="2.12"),
        Record(path="/p", name="glibc", version="2.40-lib", origin=Origin.NAME),
        Record(path="/p", name="glibc", version="2.40", origin=Origin.DERIVATION),
    ],
    ids=["states-a-cpe", "states-candidates", "no-version", "language", "two-vendors", "not-listed", "name", "drv"],
)
def test_a_package_gets_no_generated_cpe(record: Record) -> None:
    assert generated_cpe(record, _DICTIONARY) is None


def test_the_walk_reads_cpe_parts_and_languages() -> None:
    raw = [
        {
            "path": "/nix/store/a-glibc-2.40",
            "pname": "glibc",
            "version": "2.40",
            "meta": {"cpeParts": {"part": "a", "product": "glibc"}},
        },
        {"path": "/nix/store/b-idna-3.18", "pname": "idna", "version": "3.18", "language": "python", "meta": {}},
    ]
    records = parse_walk(json.dumps(raw))
    glibc, idna = records["/nix/store/a-glibc-2.40"], records["/nix/store/b-idna-3.18"]
    assert (glibc.cpe_part, glibc.cpe_product, glibc.language) == ("a", "glibc", None)
    assert idna.language == "python"


def test_the_document_lists_a_generated_cpe_as_evidence_alone() -> None:
    target = Record(path=f"/nix/store/{'a' * 32}-app-1.0", name="app", version="1.0")
    glibc = Record(path=f"/nix/store/{'b' * 32}-glibc-2.40", name="glibc", version="2.40")
    records = {target.path: target, glibc.path: glibc}
    listed = inventory(records, target.path, {target.path: (glibc.path,), glibc.path: ()}, buildtime=False)
    bom = document(
        with_generated_cpes(listed, _DICTIONARY),
        serial_number="urn:uuid:00000000-0000-4000-8000-000000000000",
        tool_version="0",
        patches={},
    )
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(bom)) is None
    (component,) = cast("list[dict[str, object]]", bom["components"])
    assert "cpe" not in component
    assert component["evidence"] == {
        "identity": [
            {
                "field": "cpe",
                "methods": [
                    {"technique": "other", "confidence": 0.5, "value": "cpe:2.3:a:gnu:glibc:2.40:*:*:*:*:*:*:*"}
                ],
            }
        ]
    }
