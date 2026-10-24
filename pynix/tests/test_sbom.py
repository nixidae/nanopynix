"""``pynix sbom``.

The record and inventory rules are unit tests. The end-to-end tests run
``pynix`` in a subprocess against a small package set, and a validator of each
format reads what it prints.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
from huggorm.testing.environment import with_nixpkgs
from spdx_tools.spdx.parser.json.json_parser import parse_from_file
from spdx_tools.spdx.validation.document_validator import validate_full_spdx_document

from pynix._impl._sbom_cyclonedx import document
from pynix._impl._sbom_model import (
    License,
    Record,
    Source,
    from_store_path,
    inventory,
    parse_walk,
    serial_uuid,
    sri_digest,
)
from pynix._impl._sbom_spdx import package, spdx_id
from pynix._impl.sbom import created_at

if TYPE_CHECKING:
    from pathlib import Path

    from huggorm.testing.environment import NixTestEnvironment


@pytest.mark.parametrize(
    ("path", "name", "version"),
    [
        ("/nix/store/bqwmxjkrkmn1kqivq4pr053j68biq4k4-mailcap-2.1.54", "mailcap", "2.1.54"),
        ("/nix/store/bqwmxjkrkmn1kqivq4pr053j68biq4k4-mailcap-extra-2.1.54", "mailcap-extra", "2.1.54"),
        ("/nix/store/lcxn67gbjdcr6bjf1rcs03ywa7gcslr2-system-units", "system-units", None),
        ("/nix/store/13cll4sxzxxi2lxbpsg5nvfjyv7qsznr-groupdel.pam", "groupdel.pam", None),
        ("/nix/store/h4wfwic161kxrr74jlzla5lsm28hgary-glibc-2.44-25", "glibc", "2.44-25"),
    ],
)
def test_a_bare_store_path_names_itself_as_nix_does(path: str, name: str, version: str | None) -> None:
    record = from_store_path(path)
    assert (record.name, record.version) == (name, version)


def test_an_sri_hash_becomes_hex() -> None:
    digest = sri_digest("sha256-DV9gFUOC/uELEUocNOeF2LH0kgc64tOm97FHaHs2aqA=")
    assert digest is not None
    assert (digest.algorithm, digest.hex[:8]) == ("SHA-256", "0d5f6015")
    assert sri_digest("0d5f6015abc") is None
    assert sri_digest("blake3-AAAA") is None


def test_the_serial_number_follows_its_seed() -> None:
    assert serial_uuid("a") == serial_uuid("a")
    assert serial_uuid("a") != serial_uuid("b")


def _walk(*records: dict[str, object]) -> dict[str, Record]:
    return parse_walk(json.dumps(list(records)))


_APP = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-app-1.0"
_LIB = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-lib-2.0"
_MAN = "/nix/store/cccccccccccccccccccccccccccccccc-lib-2.0-man"
_CC = "/nix/store/dddddddddddddddddddddddddddddddd-cc-3.0"


def _records() -> dict[str, Record]:
    return _walk(
        {"path": _APP, "pname": "app", "version": "1.0", "outputName": "out", "buildReferences": [_LIB, _CC]},
        {"path": _LIB, "pname": "lib", "version": "2.0", "outputName": "out", "buildReferences": [_CC]},
        {"path": _MAN, "pname": "lib", "version": "2.0", "outputName": "man", "buildReferences": [_CC]},
        {"path": _CC, "pname": "cc", "version": "3.0", "outputName": "out"},
    )


def test_the_runtime_inventory_holds_the_runtime_closure_alone() -> None:
    listed = inventory(_records(), _APP, {_APP: (_APP, _LIB), _LIB: ()}, buildtime=False)
    assert {record.name for record in listed.components} == {"app", "lib"}
    assert listed.dependencies == {_APP: frozenset({_LIB}), _LIB: frozenset()}


def test_buildtime_adds_the_build_closure_without_documentation() -> None:
    listed = inventory(_records(), _APP, {_APP: (_LIB,), _LIB: ()}, buildtime=True)
    assert {record.path for record in listed.components} == {_APP, _LIB, _CC}
    assert listed.dependencies[_APP] == frozenset({_LIB, _CC})


def test_a_path_with_no_version_is_not_a_component() -> None:
    unversioned = "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-etc"
    listed = inventory(_records(), _APP, {_APP: (unversioned,), unversioned: ()}, buildtime=False)
    assert [record.path for record in listed.components] == [_APP]
    assert listed.dependencies[_APP] == frozenset()


def test_an_edge_to_a_merged_output_goes_to_the_output_that_stays() -> None:
    lib_bin = "/nix/store/ffffffffffffffffffffffffffffffff-lib-2.0-bin"
    records = _records() | _walk(
        {"path": lib_bin, "pname": "lib", "version": "2.0", "outputName": "bin"},
    )
    listed = inventory(records, _APP, {_APP: (lib_bin,), _LIB: (), lib_bin: ()}, buildtime=False)
    assert [record.path for record in listed.components if record.name == "lib"] == [_LIB]
    assert listed.dependencies[_APP] == frozenset({_LIB})


def test_every_dependency_names_an_object_of_the_document() -> None:
    listed = inventory(_records(), _APP, {_APP: (_LIB,), _LIB: ()}, buildtime=True)
    bom = document(listed, serial_number=serial_uuid("x").urn, tool_version="0", patches={})
    components: list[dict[str, object]] = bom["components"]  # pyright: ignore[reportAssignmentType] -- the document holds this shape
    refs = {str(c["bom-ref"]) for c in components} | {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-app-1.0"}
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-app-1.0" not in {str(c["bom-ref"]) for c in components}
    dependencies: list[dict[str, object]] = bom["dependencies"]  # pyright: ignore[reportAssignmentType] -- as above
    for dependency in dependencies:
        assert dependency["ref"] in refs
        assert set(dependency["dependsOn"]) <= refs  # pyright: ignore[reportArgumentType] -- a list of strings


def test_an_spdx_id_keeps_only_the_allowed_characters() -> None:
    record = Record(path="/nix/store/aaaa-gtk+3_x-1.0", name="gtk+3")
    assert spdx_id(record) == "SPDXRef-aaaa-gtk-3-x-1.0"


@pytest.mark.parametrize(
    ("licenses", "declared"),
    [
        ((License(spdx_id="MIT", name="MIT License"),), "MIT"),
        ((License(spdx_id="MIT", name=None), License(spdx_id="Apache-2.0", name=None)), "NOASSERTION"),
        ((License(spdx_id=None, name="Unfree"),), "NOASSERTION"),
        ((), "NOASSERTION"),
    ],
)
def test_spdx_declares_one_license_and_no_list(licenses: tuple[License, ...], declared: str) -> None:
    assert package(Record(path="/nix/store/aaaa-x-1", name="x", licenses=licenses))["licenseDeclared"] == declared


def test_spdx_downloads_only_from_a_scheme_it_knows() -> None:
    mirror = Source(urls=("mirror://gnu/hello.tar.gz", "https://ftp.gnu.org/hello.tar.gz"), hash=None)
    assert package(Record(path="/nix/store/a-x", name="x", source=mirror))["downloadLocation"] == (
        "https://ftp.gnu.org/hello.tar.gz"
    )
    only_mirror = Source(urls=("mirror://gnu/hello.tar.gz",), hash=None)
    assert package(Record(path="/nix/store/a-x", name="x", source=only_mirror))["downloadLocation"] == "NOASSERTION"


def test_the_creation_time_follows_source_date_epoch() -> None:
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    assert created_at({"SOURCE_DATE_EPOCH": "0"}, now) == "1970-01-01T00:00:00Z"
    assert created_at({}, now) == "2026-10-23T12:00:00Z"


# --- end to end, in a subprocess --------------------------------------------

#: `app` references `libfoo` at runtime and its `man` not at all; `cc` is a build
#: input alone. `broken` has a `meta` that throws, and the SBOM still lists it.
_PACKAGES = """
with import <nixpkgs> { };
rec {
  cc = runCommand "pynix-sbom-cc-3.0" { pname = "pynix-sbom-cc"; version = "3.0"; } "mkdir $out";
  libfoo = runCommand "pynix-sbom-lib-2.0" {
    pname = "pynix-sbom-lib";
    version = "2.0";
    outputs = [ "out" "man" ];
    nativeBuildInputs = [ cc ];
    passthru.src = {
      urls = [ "https://example.org/lib-2.0.tar.gz" ];
      outputHash = "sha256-DV9gFUOC/uELEUocNOeF2LH0kgc64tOm97FHaHs2aqA=";
    };
    passthru.patches = [ ./fix.patch ];
    meta.license = [ lib.licenses.mit lib.licenses.asl20 ];
    meta.homepage = "https://example.org/lib";
  } "mkdir $out $man";
  raw = pname: version: meta: (derivation {
    name = "${pname}-${version}";
    inherit pname version;
    system = builtins.currentSystem;
    builder = runtimeShell;
    args = [ "-c" "echo > $out" ];
  }) // { inherit meta; };
  broken = raw "pynix-sbom-broken" "4.0" (throw "this meta throws");
  deep = raw "pynix-sbom-deep" "5.0" {
    license = [ lib.licenses.mit (throw "this license throws") ];
    homepage = [ ];
  };
  app = runCommand "pynix-sbom-app-1.0" {
    pname = "pynix-sbom-app";
    version = "1.0";
    nativeBuildInputs = [ cc broken deep ];
    buildInputs = [ libfoo ];
    meta.license = lib.licenses.gpl3Plus;
    meta.description = "The target";
  } "mkdir $out; echo ${libfoo} > $out/lib";
}
"""


def _sbom(store_uri: str, nix_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "sbom",
        "--file",
        str(nix_file),
        "--attr",
        "app",
        "--store",
        store_uri,
        *args,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv, capture_output=True, text=True, check=False, env=os.environ.copy()
    )


@pytest.fixture
def packages(tmp_path: Path, nixpkgs_path: str) -> Path:
    (tmp_path / "fix.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    path = tmp_path / "packages.nix"
    path.write_text(with_nixpkgs(_PACKAGES, nixpkgs_path), encoding="utf-8")
    return path


def _components(bom: dict[str, object]) -> dict[str, dict[str, object]]:
    listed: list[dict[str, object]] = bom["components"]  # pyright: ignore[reportAssignmentType] -- the CycloneDX shape
    return {str(component["name"]): component for component in listed}


def test_cyclonedx_lists_the_runtime_closure(shared_nix_environment: NixTestEnvironment, packages: Path) -> None:
    result = _sbom(shared_nix_environment.store_uri, packages)
    assert result.returncode == 0, result.stderr
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(result.stdout) is None
    bom = json.loads(result.stdout)
    assert bom["metadata"]["component"]["name"] == "pynix-sbom-app"
    assert bom["metadata"]["component"]["licenses"] == [{"license": {"id": "GPL-3.0-or-later"}}]
    lib = _components(bom)["pynix-sbom-lib"]
    assert lib["licenses"] == [{"license": {"id": "MIT"}}, {"license": {"id": "Apache-2.0"}}]
    assert lib["externalReferences"] == [
        {
            "type": "vcs",
            "url": "https://example.org/lib-2.0.tar.gz",
            "hashes": [
                {"alg": "SHA-256", "content": "0d5f60154382fee10b114a1c34e785d8b1f492073ae2d3a6f7b147687b366aa0"}
            ],
        },
        {"type": "website", "url": "https://example.org/lib"},
    ]
    assert lib["pedigree"] == {
        "patches": [
            {"type": "unofficial", "diff": {"text": {"contentType": "text/plain", "content": "--- a\n+++ b\n"}}}
        ]
    }
    assert set(_components(bom)) == {"pynix-sbom-lib"}
    app_ref = bom["metadata"]["component"]["bom-ref"]
    assert {d["ref"]: d["dependsOn"] for d in bom["dependencies"]}[app_ref] == [lib["bom-ref"]]


def test_cyclonedx_buildtime_adds_build_inputs_and_survives_a_throwing_meta(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    result = _sbom(shared_nix_environment.store_uri, packages, "--buildtime")
    assert result.returncode == 0, result.stderr
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(result.stdout) is None
    components = _components(json.loads(result.stdout))
    assert {"pynix-sbom-lib", "pynix-sbom-cc", "pynix-sbom-broken", "pynix-sbom-deep"} <= set(components)
    assert "licenses" not in components["pynix-sbom-broken"]
    assert "licenses" not in components["pynix-sbom-deep"]
    assert all(not str(c["bom-ref"]).endswith("-man") for c in components.values())


def test_spdx_is_valid_and_states_the_graph(
    shared_nix_environment: NixTestEnvironment, packages: Path, tmp_path: Path
) -> None:
    result = _sbom(shared_nix_environment.store_uri, packages, "--format", "spdx", "--buildtime")
    assert result.returncode == 0, result.stderr
    written = tmp_path / "sbom.spdx.json"
    written.write_text(result.stdout, encoding="utf-8")
    assert validate_full_spdx_document(parse_from_file(str(written))) == []
    spdx = json.loads(result.stdout)
    ids = {p["name"]: p["SPDXID"] for p in spdx["packages"]}
    lib = next(p for p in spdx["packages"] if p["name"] == "pynix-sbom-lib")
    assert lib["downloadLocation"] == "https://example.org/lib-2.0.tar.gz"
    assert lib["licenseDeclared"] == "NOASSERTION"
    edges = {(r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"]) for r in spdx["relationships"]}
    assert ("SPDXRef-DOCUMENT", "DESCRIBES", ids["pynix-sbom-app"]) in edges
    assert (ids["pynix-sbom-app"], "DEPENDS_ON", ids["pynix-sbom-lib"]) in edges
    assert (ids["pynix-sbom-app"], "DEPENDS_ON", ids["pynix-sbom-cc"]) in edges
