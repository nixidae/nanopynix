"""The packages a store path names inside itself: Go build info, cargo-auditable data and Python metadata.

The binaries are nixpkgs builds on the suite's PATH (``nix/suite-runtime.nix``).
cargo-auditable's own reader, ``rust-audit-info``, is the oracle for Rust, and
``importlib.metadata`` for Python.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import subprocess
import zlib
from pathlib import Path
from typing import cast

import pytest
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from pynix._impl._sbom_cyclonedx import document
from pynix._impl._sbom_embedded import (
    DeclaredLicenses,
    Ecosystem,
    Package,
    parse_cargo,
    parse_composer,
    parse_gemspec,
    parse_go,
    parse_npm,
    parse_python,
    read_binary,
    scan,
)
from pynix._impl._sbom_model import Record, inherited_license, inventory, parse_walk, with_embedded

#: ``cmd/go/internal/modload``'s ``infoStart`` and ``infoEnd``.
_GO_START = bytes.fromhex("3077af0c9274080241e1c107e6d618e6")
_GO_END = bytes.fromhex("f932433186182072008242104116d8f2")


def _program(name: str) -> Path:
    found = shutil.which(name)
    assert found is not None, f"{name} is not on PATH: nix/suite-runtime.nix provides it"
    return Path(found).resolve()


def _read(path: Path) -> tuple[Package, ...] | None:
    with path.open("rb") as stream:
        return read_binary(stream)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _go_section(version: str, lines: str) -> bytes:
    """A ``.go.buildinfo`` section as Go 1.18 and later lay it out."""
    header = b"\xff Go buildinf:" + bytes([8, 2]) + bytes(16)
    modules = _GO_START + lines.encode() + _GO_END
    return header + _varint(len(version)) + version.encode() + _varint(len(modules)) + modules


def test_a_rust_binary_names_the_crates_cargo_auditable_reads() -> None:
    binary = _program("rust-audit-info")
    oracle = json.loads(
        subprocess.run([binary, binary], capture_output=True, text=True, check=True).stdout  # noqa: S603 -- a nixpkgs program on itself
    )
    raw = cast("list[dict[str, object]]", oracle["packages"])
    runtime = {(str(p["name"]), str(p["version"])) for p in raw if p.get("kind") != "build"}
    found = _read(binary)
    assert found is not None
    assert {(p.name, p.version) for p in found} == runtime
    root = next(p for p in found if p.root)
    edges = cast("list[int]", next(p for p in raw if p.get("root")).get("dependencies") or [])
    assert set(root.dependencies) == {f"pkg:cargo/{raw[i]['name']}@{raw[i]['version']}" for i in edges}


def test_a_go_binary_names_its_modules_and_its_standard_library() -> None:
    found = _read(_program("go-md2man"))
    assert found is not None
    by_name = {p.name: p for p in found}
    assert by_name["github.com/russross/blackfriday/v2"].purl == "pkg:golang/github.com/russross/blackfriday/v2@v2.1.0"
    assert by_name["stdlib"].version is not None
    assert by_name["stdlib"].version.startswith("v1.")
    main = by_name["github.com/cpuguy83/go-md2man/v2"]
    assert main.root
    assert set(main.dependencies) == {p.purl for p in found if not p.root}
    # Go states no edges between modules, nor which ones the main module imports directly.
    assert {p.complete for p in found} == {None}
    # nixpkgs builds it for this machine.
    goarch = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
    for p in found:
        assert ("go:goos", "linux") in p.properties
        assert ("go:goarch", goarch) in p.properties


def test_a_c_binary_names_nothing() -> None:
    assert _read(_program("bash")) is None


def test_a_replace_line_names_what_was_compiled_in() -> None:
    lines = (
        "path\texample.org/app\n"
        "mod\texample.org/app\t(devel)\t\n"
        "dep\texample.org/old\tv1.0.0\th1:abc=\n"
        "=>\texample.org/fork\tv1.0.1\th1:def=\n"
        "dep\texample.org/local\tv0.1.0\th1:ghi=\n"
        "=>\t../local\t\t\n"
        "build\tCGO_ENABLED=0\n"
    )
    found = parse_go(_go_section("go1.26.8", lines))
    assert found is not None
    by_name = {p.name: p for p in found}
    assert "example.org/old" not in by_name
    assert by_name["example.org/fork"].version == "v1.0.1"
    assert ("go:replaces", "example.org/old v1.0.0") in by_name["example.org/fork"].properties
    # A local directory keeps the module's name, and has no version.
    assert by_name["example.org/local"].version is None
    assert by_name["example.org/app"].version is None
    assert by_name["stdlib"].version == "v1.26.8"


def test_each_go_module_names_the_platform_it_was_built_for() -> None:
    lines = (
        "mod\texample.org/app\tv1.0.0\t\n"
        "dep\texample.org/lib\tv1.0.0\th1:abc=\n"
        "build\tGOARCH=ppc64le\n"
        "build\tGOOS=windows\n"
        "build\tGOAMD64=v1\n"
    )
    found = parse_go(_go_section("go1.26.8", lines))
    assert found is not None
    platforms = {p.name: {(k, v) for k, v in p.properties if k in ("go:goos", "go:goarch")} for p in found}
    expected = {("go:goos", "windows"), ("go:goarch", "ppc64le")}
    assert platforms == {"stdlib": expected, "example.org/lib": expected, "example.org/app": expected}


def test_go_build_info_without_its_sentinels_names_no_module() -> None:
    header = b"\xff Go buildinf:" + bytes([8, 2]) + bytes(16)
    section = header + _varint(8) + b"go1.26.8" + _varint(4) + b"mod\t"
    assert parse_go(section) == (Package(Ecosystem.GOLANG, "stdlib", "v1.26.8"),)


def _audit(*packages: dict[str, object]) -> bytes:
    return zlib.compress(json.dumps({"packages": list(packages)}).encode())


def test_a_build_dependency_is_not_compiled_in_and_leaves_its_dependent_incomplete() -> None:
    found = parse_cargo(
        _audit(
            {"name": "app", "version": "1.0.0", "source": "local", "root": True, "dependencies": [1, 2]},
            {"name": "serde", "version": "1.0.0", "source": "crates.io", "dependencies": [2]},
            {"name": "serde_derive", "version": "1.0.0", "source": "crates.io", "kind": "build"},
        )
    )
    assert found is not None
    by_name = {p.name: p for p in found}
    assert set(by_name) == {"app", "serde"}
    assert by_name["app"].dependencies == ("pkg:cargo/serde@1.0.0",)
    assert by_name["app"].complete is False
    assert by_name["serde"].complete is False
    assert parse_cargo(_audit({"name": "leaf", "version": "1.0.0", "source": "crates.io"})) == (
        Package(Ecosystem.CARGO, "leaf", "1.0.0", complete=True, properties=(("cargo:source", "crates.io"),)),
    )


def test_audit_data_that_is_not_zlib_names_nothing() -> None:
    assert parse_cargo(b"not zlib") is None


def test_a_scan_finds_the_executables_of_a_store_path(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-tools-1.0"
    (tmp_path / "bin").mkdir()
    shutil.copy(_program("go-md2man"), tmp_path / "bin" / "go-md2man")
    # Not executable: a store file with no execute bit is no program.
    shutil.copy(_program("rust-audit-info"), tmp_path / "rust-audit-info")
    (tmp_path / "rust-audit-info").chmod(0o444)
    found = scan(tmp_path, store_path)
    assert {p.ecosystem for p in found} == {Ecosystem.GOLANG}
    assert {file for p in found for file in p.files} == {f"{store_path}/bin/go-md2man"}


def test_a_scan_reads_the_python_distribution_importlib_reads() -> None:
    oracle = importlib.metadata.distribution("packageurl-python")
    # The test environment links each distribution; the link resolves into the store path that holds it.
    metadata = Path(str(oracle.locate_file(next(f for f in oracle.files or () if f.name == "METADATA")))).resolve()
    store_path = str(Path(*metadata.parts[:4]))
    found = scan(Path(store_path), store_path)
    own = next(p for p in found if p.ecosystem is Ecosystem.PYPI and p.name == oracle.metadata["Name"])
    assert own.version == oracle.version
    assert own.purl == f"pkg:pypi/packageurl-python@{oracle.version}"
    assert [file.rsplit("/", 2)[-2] for file in own.files] == [f"packageurl_python-{oracle.version}.dist-info"]


def test_a_scan_finds_each_python_distribution_once_and_follows_no_link(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-python3-tools-1.0"
    site = tmp_path / "lib" / "python3.13" / "site-packages"

    def install(directory: Path, name: str, text: str) -> None:
        directory.mkdir(parents=True)
        (directory / name).write_text(text)

    install(
        site / "Typing_Extensions-4.15.0.dist-info",
        "METADATA",
        "Metadata-Version: 2.4\nName: Typing_Extensions\nVersion: 4.15.0\n\nbody\nName: not-a-header\n",
    )
    install(site / "tool" / "_vendor" / "packaging-26.0.dist-info", "METADATA", "Name: packaging\nVersion: 26.0\n")
    install(site / "LibAppArmor-5.0.2-py3.13.egg-info", "PKG-INFO", "Name: LibAppArmor\nVersion: 5.0.2\n")
    (site / "legacy-1.0-py3.13.egg-info").write_text("Name: legacy\nVersion: 1.0\n")
    install(site / "nameless-1.0.dist-info", "METADATA", "Version: 1.0\n")
    install(site / "elsewhere", "METADATA", "Name: not-in-a-dist-info\nVersion: 1.0\n")
    # An environment links the distributions of other store paths; those name themselves.
    (site / "linked-1.0.dist-info").symlink_to(site / "Typing_Extensions-4.15.0.dist-info")
    found = scan(tmp_path, store_path)
    assert {(p.purl, p.files) for p in found} == {
        (
            "pkg:pypi/typing-extensions@4.15.0",
            (f"{store_path}/lib/python3.13/site-packages/Typing_Extensions-4.15.0.dist-info/METADATA",),
        ),
        (
            "pkg:pypi/packaging@26.0",
            (f"{store_path}/lib/python3.13/site-packages/tool/_vendor/packaging-26.0.dist-info/METADATA",),
        ),
        (
            "pkg:pypi/libapparmor@5.0.2",
            (f"{store_path}/lib/python3.13/site-packages/LibAppArmor-5.0.2-py3.13.egg-info/PKG-INFO",),
        ),
        ("pkg:pypi/legacy@1.0", (f"{store_path}/lib/python3.13/site-packages/legacy-1.0-py3.13.egg-info",)),
    }
    assert {p.name for p in found} >= {"Typing_Extensions", "LibAppArmor"}
    assert {(p.complete, p.root, p.dependencies) for p in found} == {(None, False, ())}


def test_a_scan_finds_each_installed_npm_package_and_no_other_manifest(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodejs-24.0.0"
    top = tmp_path / "lib" / "node_modules"

    def install(directory: Path, manifest: object) -> None:
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps(manifest))

    install(top / "npm", {"name": "npm", "version": "11.0.0", "dependencies": {"semver": "^7"}})
    install(top / "npm" / "node_modules" / "semver", {"name": "semver", "version": "7.7.2"})
    install(top / "npm" / "node_modules" / "@npmcli" / "Config", {"name": "@npmcli/Config", "version": "10.0.0"})
    # A package's own files and the project root are no installed package.
    install(top / "npm" / "node_modules" / "semver" / "lib", {"name": "inner", "version": "1.0.0"})
    install(tmp_path / "app", {"name": "app", "version": "1.0.0"})
    install(top / "nameless", {"version": "1.0.0"})
    install(top / "array", [{"name": "x"}])
    (top / "broken").mkdir()
    (top / "broken" / "package.json").write_text("{")
    install(top / "private", {"name": "private"})
    found = scan(tmp_path, store_path)
    assert {(p.purl, p.files) for p in found} == {
        ("pkg:npm/npm@11.0.0", (f"{store_path}/lib/node_modules/npm/package.json",)),
        ("pkg:npm/semver@7.7.2", (f"{store_path}/lib/node_modules/npm/node_modules/semver/package.json",)),
        (
            "pkg:npm/%40npmcli/config@10.0.0",
            (f"{store_path}/lib/node_modules/npm/node_modules/@npmcli/Config/package.json",),
        ),
        ("pkg:npm/private", (f"{store_path}/lib/node_modules/private/package.json",)),
    }
    assert {(p.ecosystem, p.complete, p.root, p.dependencies) for p in found} == {(Ecosystem.NPM, None, False, ())}


def test_a_scan_reads_each_composer_vendor_directory_and_its_edges(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nextcloud-34.0.0"
    installed = {
        "packages": [
            {"name": "Guzzle/Psr7", "version": "2.7.0", "require": {"php": "^7.2", "psr/http-message": "^1"}},
            {"name": "psr/http-message", "version": "1.1", "require": {}},
            {"name": "symfony/polyfill-mbstring", "version": "v1.31.0", "require": {"ext-mbstring": "*"}},
            {
                "name": "nextcloud/lib",
                "version": "dev-master",
                "require": {"symfony/polyfill-php80": "^1", "missing/package": "^1", "guzzle/psr7": "^2"},
            },
            {"name": "mine/shim", "version": "1.0.0", "replace": {"symfony/polyfill-php80": "*"}},
            {"version": "1.0.0"},
        ],
        "dev": False,
        "dev-package-names": [],
    }
    for directory in (tmp_path / "3rdparty" / "composer", tmp_path / "apps" / "x" / "vendor" / "composer"):
        directory.mkdir(parents=True)
    (tmp_path / "3rdparty" / "composer" / "installed.json").write_text(json.dumps(installed))
    # Composer 1's list alone, in an app's own vendor directory.
    (tmp_path / "apps" / "x" / "vendor" / "composer" / "installed.json").write_text(
        json.dumps([{"name": "psr/http-message", "version": "1.1"}])
    )
    # An installed.json outside a composer directory is no Composer list.
    (tmp_path / "apps" / "x" / "installed.json").write_text(json.dumps(installed))
    (tmp_path / "3rdparty" / "composer" / "broken").mkdir()
    (tmp_path / "3rdparty" / "composer" / "broken" / "installed.json").write_text("{")
    found = {p.purl: p for p in scan(tmp_path, store_path)}
    assert {purl: (p.dependencies, p.complete) for purl, p in found.items()} == {
        "pkg:composer/guzzle/psr7@2.7.0": (("pkg:composer/psr/http-message@1.1",), True),
        "pkg:composer/psr/http-message@1.1": ((), True),
        "pkg:composer/symfony/polyfill-mbstring@v1.31.0": ((), True),
        "pkg:composer/nextcloud/lib@dev-master": (
            ("pkg:composer/guzzle/psr7@2.7.0", "pkg:composer/mine/shim@1.0.0"),
            False,
        ),
        "pkg:composer/mine/shim@1.0.0": ((), True),
    }
    assert found["pkg:composer/psr/http-message@1.1"].files == (
        f"{store_path}/3rdparty/composer/installed.json",
        f"{store_path}/apps/x/vendor/composer/installed.json",
    )


def _gemspec(name: str, version: str, *extra: str) -> str:
    """A spec as ``Gem::Specification#to_ruby`` writes it on install."""
    lines = "\n".join(f"  {line}" for line in extra)
    return f'''# -*- encoding: utf-8 -*-
# stub: {name} {version} ruby lib

Gem::Specification.new do |s|
  s.name = "{name}".freeze
  s.version = "{version}".freeze
{lines}
  s.add_runtime_dependency(%q<ffi>.freeze, ["~> 1.9".freeze])
end
'''


def test_a_scan_finds_each_installed_gem_and_no_other_gemspec(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-ruby3.4-nokogiri-1.18.0"
    gems = tmp_path / "lib" / "ruby" / "gems" / "3.4.0"
    for directory in (gems / "specifications" / "default", gems / "gems" / "rake-13.0.0"):
        directory.mkdir(parents=True)
    (gems / "specifications" / "nokogiri-1.18.0-x86_64-linux-gnu.gemspec").write_text(
        _gemspec("nokogiri", "1.18.0", 's.platform = "x86_64-linux-gnu".freeze', 's.licenses = ["MIT".freeze]')
    )
    (gems / "specifications" / "default" / "json-2.9.1.gemspec").write_text(
        _gemspec("json", "2.9.1", 's.licenses = ["Ruby".freeze, "BSD-2-Clause".freeze]')
    )
    # A gem's own source carries its gemspec too; only RubyGems' installed copy counts.
    (gems / "gems" / "rake-13.0.0" / "rake.gemspec").write_text(_gemspec("rake", "13.0.0"))
    (gems / "specifications" / "nameless.gemspec").write_text("Gem::Specification.new do |s|\nend\n")
    found = {p.purl: p for p in scan(tmp_path, store_path)}
    assert set(found) == {"pkg:gem/nokogiri@1.18.0", "pkg:gem/json@2.9.1"}
    nokogiri = found["pkg:gem/nokogiri@1.18.0"]
    assert nokogiri.properties == (("gem:platform", "x86_64-linux-gnu"),)
    assert nokogiri.files == (
        f"{store_path}/lib/ruby/gems/3.4.0/specifications/nokogiri-1.18.0-x86_64-linux-gnu.gemspec",
    )
    assert (nokogiri.ecosystem, nokogiri.dependencies, nokogiri.complete) == (Ecosystem.GEM, (), None)
    assert found["pkg:gem/json@2.9.1"].properties == ()


@pytest.mark.parametrize(
    ("line", "declared"),
    [
        ('s.licenses = ["MIT".freeze]', DeclaredLicenses(expression="MIT")),
        ('s.licenses = ["Ruby".freeze, "BSD-2-Clause".freeze]', DeclaredLicenses(expression="(Ruby OR BSD-2-Clause)")),
        ('s.license = "Apache-2.0".freeze', DeclaredLicenses(expression="Apache-2.0")),
        ('s.licenses = ["Apache License (2.0)".freeze]', DeclaredLicenses(names=("Apache License (2.0)",))),
        (
            's.licenses = ["Nonstandard".freeze, "GPL-2.0-only".freeze]',
            DeclaredLicenses(names=("Nonstandard", "GPL-2.0-only")),
        ),
        ('s.licenses = ["".freeze]', None),
        ('s.summary = "no license".freeze', None),
    ],
)
def test_a_gem_declares_any_of_its_licenses(line: str, declared: DeclaredLicenses | None) -> None:
    package = parse_gemspec(_gemspec("demo", "1.0.0", line).encode())
    assert package is not None
    assert package.licenses == declared


def test_a_gemspec_string_is_read_as_ruby_dumped_it() -> None:
    package = parse_gemspec(_gemspec('q\\"uote', "1.0.0", 's.licenses = ["caf\\u00E9 \\#{x}".freeze]').encode())
    assert package is not None
    assert package.name == 'q"uote'
    assert package.licenses == DeclaredLicenses(names=("café #{x}",))


_GEM = "/nix/store/dddddddddddddddddddddddddddddddd-ruby3.4-mini_suffix-0.3.3"
_MIT = DeclaredLicenses(expression="MIT")


@pytest.mark.parametrize(
    ("record", "packages", "inherits"),
    [
        (Record(_GEM, "mini_suffix", "0.3.3"), (Package(Ecosystem.GEM, "mini_suffix", "0.3.3", licenses=_MIT),), True),
        # A record from the store name alone carries the ruby prefix.
        (
            Record(_GEM, "ruby3.4-mini_suffix", "0.3.3"),
            (Package(Ecosystem.GEM, "mini_suffix", "0.3.3", licenses=_MIT),),
            True,
        ),
        # nixpkgs' own license wins.
        (
            Record(_GEM, "mini_suffix", "0.3.3", license_expression="Apache-2.0"),
            (Package(Ecosystem.GEM, "mini_suffix", "0.3.3", licenses=_MIT),),
            False,
        ),
        # Another gem's license, or one of several, is not the record's own.
        (Record(_GEM, "mini_suffix", "0.3.3"), (Package(Ecosystem.GEM, "ffi", "1.0", licenses=_MIT),), False),
        (
            Record(_GEM, "mini_suffix", "0.3.3"),
            (
                Package(Ecosystem.GEM, "mini_suffix", "0.3.3", licenses=_MIT),
                Package(Ecosystem.GEM, "ffi", "1.0", licenses=_MIT),
            ),
            False,
        ),
        (
            Record(_GEM, "mini_suffix", "0.3.3"),
            (Package(Ecosystem.PYPI, "mini_suffix", "0.3.3", licenses=_MIT),),
            False,
        ),
        (Record(_GEM, "mini_suffix", "0.3.3"), (Package(Ecosystem.GEM, "mini_suffix", "0.3.3"),), False),
    ],
)
def test_a_gem_derivation_inherits_the_license_its_gem_declares(
    record: Record, packages: tuple[Package, ...], inherits: bool
) -> None:
    result = inherited_license(record, packages)
    assert (result.license_declared_by is not None) == inherits
    if inherits:
        assert (result.license_expression, result.license_declared_by) == ("MIT", "pkg:gem/mini_suffix@0.3.3")
    else:
        assert result == record


def test_an_inherited_license_is_declared_in_the_document() -> None:
    records = parse_walk(json.dumps([{"path": _GEM, "pname": "mini_suffix", "version": "0.3.3", "outputName": "out"}]))
    listed = inventory(records, _GEM, {_GEM: ()}, buildtime=False)
    gem = Package(Ecosystem.GEM, "mini_suffix", "0.3.3", licenses=DeclaredLicenses(names=("Nonstandard",)))
    bom = document(
        with_embedded(listed, {_GEM: (gem,)}),
        serial_number="urn:uuid:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
        tool_version="0",
        patches={},
    )
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(bom)) is None
    target = cast("dict[str, object]", cast("dict[str, object]", bom["metadata"])["component"])
    assert target["licenses"] == [{"license": {"name": "Nonstandard", "acknowledgement": "declared"}}]
    assert {"name": "nix:licenseDeclaredBy", "value": "pkg:gem/mini_suffix@0.3.3"} in cast(
        "list[object]", target["properties"]
    )


def test_a_pypi_purl_folds_case_and_underscores_but_not_dots() -> None:
    assert Package(Ecosystem.PYPI, "ruamel.yaml_Clib", "0.2").purl == "pkg:pypi/ruamel.yaml-clib@0.2"
    assert Package(Ecosystem.GOLANG, "example.org/Some_Module", "v1").purl == "pkg:golang/example.org/Some_Module@v1"


_APP = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-app-1.0"
_LIB = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-lib-2.0"


def test_the_document_nests_each_package_in_the_component_that_holds_it() -> None:
    records = parse_walk(
        json.dumps(
            [
                {"path": _APP, "pname": "app", "version": "1.0", "outputName": "out"},
                {"path": _LIB, "pname": "lib", "version": "2.0", "outputName": "out"},
            ]
        )
    )
    listed = inventory(records, _APP, {_APP: (_LIB,), _LIB: ()}, buildtime=False)
    found = parse_cargo(
        _audit(
            {"name": "lib", "version": "2.0.0", "source": "local", "root": True, "dependencies": [1]},
            {"name": "itoa", "version": "1.0.0", "source": "crates.io"},
        )
    )
    assert found is not None
    listed = with_embedded(listed, {_LIB: found, "/nix/store/cccccccccccccccccccccccccccccccc-unlisted": found})
    assert set(listed.embedded) == {_LIB}
    bom = document(
        listed, serial_number="urn:uuid:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12, tool_version="0", patches={}
    )
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(bom)) is None
    components = cast("list[dict[str, object]]", bom["components"])
    lib = next(c for c in components if c["name"] == "lib")
    inside = cast("list[dict[str, object]]", lib["components"])
    refs = {str(c["bom-ref"]) for c in inside}
    assert refs == {f"{lib['bom-ref']}#pkg:cargo/lib@2.0.0", f"{lib['bom-ref']}#pkg:cargo/itoa@1.0.0"}
    edges = {str(d["ref"]): d["dependsOn"] for d in cast("list[dict[str, object]]", bom["dependencies"])}
    assert edges[f"{lib['bom-ref']}#pkg:cargo/lib@2.0.0"] == [f"{lib['bom-ref']}#pkg:cargo/itoa@1.0.0"]
    claims = {
        (str(c["aggregate"]), kind): set(cast("list[str]", c[kind]))
        for c in cast("list[dict[str, object]]", bom["compositions"])
        for kind in ("assemblies", "dependencies")
        if kind in c
    }
    assert claims[("incomplete", "assemblies")] == {lib["bom-ref"]}
    assert refs <= claims[("complete", "dependencies")]


_LONG_TEXT = "Permission is hereby granted, free of charge, to any person obtaining a copy" * 3


@pytest.mark.parametrize(
    ("headers", "declared"),
    [
        ("License-Expression: MIT OR Apache-2.0\nLicense: ignored", DeclaredLicenses(expression="MIT OR Apache-2.0")),
        ("License: BSD", DeclaredLicenses(names=("BSD",))),
        ("License: UNKNOWN", None),
        (
            f"License: {_LONG_TEXT}\nClassifier: License :: OSI Approved :: MIT License\nClassifier: License :: OSI Approved",
            DeclaredLicenses(names=("MIT License",)),
        ),
        ("Classifier: Programming Language :: Python", None),
    ],
)
def test_a_python_distribution_declares_its_license(headers: str, declared: DeclaredLicenses | None) -> None:
    package = parse_python(f"Metadata-Version: 2.4\nName: demo\nVersion: 1\n{headers}\n".encode())
    assert package is not None
    assert package.licenses == declared


@pytest.mark.parametrize(
    ("fields", "declared"),
    [
        ({"license": "(MIT OR Apache-2.0)"}, DeclaredLicenses(expression="(MIT OR Apache-2.0)")),
        ({"license": "UNLICENSED"}, DeclaredLicenses(names=("UNLICENSED",))),
        ({"license": "SEE LICENSE IN LICENSE.md"}, DeclaredLicenses(names=("SEE LICENSE IN LICENSE.md",))),
        ({"license": {"type": "MIT"}}, DeclaredLicenses(names=("MIT",))),
        ({"licenses": [{"type": "MIT"}, {"type": "GPL-2.0"}]}, DeclaredLicenses(names=("MIT", "GPL-2.0"))),
        ({}, None),
    ],
)
def test_an_npm_package_declares_its_license(fields: dict[str, object], declared: DeclaredLicenses | None) -> None:
    package = parse_npm(json.dumps({"name": "demo", "version": "1.0.0", **fields}).encode())
    assert package is not None
    assert package.licenses == declared


def test_a_composer_package_declares_any_of_its_licenses() -> None:
    installed = {
        "packages": [
            {"name": "a/one", "version": "1.0.0", "license": ["MIT"]},
            {"name": "a/two", "version": "1.0.0", "license": ["LGPL-2.1-only", "GPL-3.0-or-later"]},
            {"name": "a/own", "version": "1.0.0", "license": ["proprietary"]},
            {"name": "a/none", "version": "1.0.0"},
        ]
    }
    found = parse_composer(json.dumps(installed).encode())
    assert found is not None
    assert {p.name: p.licenses for p in found} == {
        "a/one": DeclaredLicenses(expression="MIT"),
        "a/two": DeclaredLicenses(expression="(LGPL-2.1-only OR GPL-3.0-or-later)"),
        "a/own": DeclaredLicenses(names=("proprietary",)),
        "a/none": None,
    }


def test_a_declared_license_is_a_valid_cyclonedx_license() -> None:
    records = parse_walk(json.dumps([{"path": _APP, "pname": "app", "version": "1.0", "outputName": "out"}]))
    listed = inventory(records, _APP, {_APP: ()}, buildtime=False)
    packages = (
        Package(Ecosystem.NPM, "a", "1", licenses=DeclaredLicenses(expression="MIT OR Apache-2.0")),
        Package(Ecosystem.PYPI, "b", "1", licenses=DeclaredLicenses(names=("BSD", "Zope Public License"))),
        Package(Ecosystem.GOLANG, "c", "v1"),
    )
    bom = document(
        with_embedded(listed, {_APP: packages}),
        serial_number="urn:uuid:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
        tool_version="0",
        patches={},
    )
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(bom)) is None
    app = cast("dict[str, object]", cast("dict[str, object]", bom["metadata"])["component"])
    inside = {str(c["name"]): c.get("licenses") for c in cast("list[dict[str, object]]", app["components"])}
    assert inside == {
        "a": [{"expression": "MIT OR Apache-2.0", "acknowledgement": "declared"}],
        "b": [
            {"license": {"name": "BSD", "acknowledgement": "declared"}},
            {"license": {"name": "Zope Public License", "acknowledgement": "declared"}},
        ],
        "c": None,
    }
