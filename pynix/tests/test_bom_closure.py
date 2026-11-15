"""``pynix.bom.closure_bom``: an SBOM of store paths, from recorded values or from their derivers."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, with_nixpkgs

from pynix import _util
from pynix.bom import Origin, build_bom, closure_bom, context_derivations, context_roots, cyclonedx

PAYLOAD = "pynix-bom-closure-source\n"
SRI = "sha256-" + base64.b64encode(hashlib.sha256(PAYLOAD.encode()).digest()).decode()

#: `src` is a fixed-output derivation with structured attributes, as fetchurl
#: writes one; its URLs live outside the environment. `wrapper` has no version.
#: `lib`'s license is in `meta`, which no `.drv` holds.
_PACKAGES = """
with import <nixpkgs> { };
rec {
  src = runCommand "pynix-bom-lib-2.0.tar.gz" {
    __structuredAttrs = true;
    urls = [ "https://example.org/pynix-bom-lib-2.0.tar.gz" ];
    outputHash = "@SRI@";
    outputHashMode = "flat";
  } "printf 'pynix-bom-closure-source\\n' > $out";
  lib = runCommand "pynix-bom-lib-2.0" {
    pname = "pynix-bom-lib";
    version = "2.0";
    inherit src;
    patches = [ ./fix.patch ];
    meta.license = pkgs.lib.licenses.mit;
  } "mkdir $out; cat $src > $out/src";
  wrapper = runCommand "pynix-bom-wrapper" { } "mkdir $out; echo ${lib} > $out/lib";
}
"""


@pytest.fixture
def packages(tmp_path: Path, nixpkgs_path: str) -> Path:
    (tmp_path / "fix.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    path = tmp_path / "packages.nix"
    path.write_text(with_nixpkgs(_PACKAGES.replace("@SRI@", SRI), nixpkgs_path), encoding="utf-8")
    return path


@LINUX_CHROOT_BUILD
async def test_a_closure_lists_each_path_by_its_deriver(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, store, _eval_store, state = opened
        value = await state.eval_file(str(packages))
        await state.force(value)
        wrapper = await value.get("wrapper")
        await state.force(wrapper)
        root = await (await wrapper.get("outPath")).realise_string()
        bom = await closure_bom(store, [root], name="cluster", version="1")

    # `components` holds the target too; the renderers put it in metadata.
    by_name = {record.name: record for record in bom.inventory.components if record.path != "cluster"}
    assert bom.inventory.target.name == "cluster"
    assert set(by_name) == {"pynix-bom-lib", "pynix-bom-wrapper"}
    lib = by_name["pynix-bom-lib"]
    assert lib.version == "2.0"
    assert (lib.origin, lib.licenses) == (Origin.DERIVATION, ())
    assert lib.output_name == "out"
    assert lib.source is not None
    assert lib.source.urls == ("https://example.org/pynix-bom-lib-2.0.tar.gz",)
    assert lib.source.hash == SRI
    assert len(lib.patches) == 1
    assert bom.patches == {lib.patches[0]: "--- a\n+++ b\n"}
    assert by_name["pynix-bom-wrapper"].version is None
    assert bom.inventory.dependencies["cluster"] == frozenset({root})
    assert bom.inventory.dependencies[root] == frozenset({lib.path})
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(cyclonedx(bom))) is None


@LINUX_CHROOT_BUILD
async def test_a_recorded_value_gives_its_path_the_meta(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    """A manifest names `wrapper`; `wrapper`'s build names `lib`. Both were coerced, so both were recorded."""
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, store, eval_store, state = opened
        await state.set_record_derivations(True)
        manifest = await state.eval_expr(f'builtins.toJSON {{ command = "${{(import {packages}).wrapper}}/lib"; }}')
        await state.force(manifest)
        await manifest.realise_string()
        roots = await context_roots(eval_store, manifest)
        bom = await closure_bom(store, roots, name="cluster", drv_store=eval_store, state=state)

    (root,) = roots
    by_name = {record.name: record for record in bom.inventory.components if record.path != "cluster"}
    lib = by_name["pynix-bom-lib"]
    assert root == by_name["pynix-bom-wrapper"].path
    assert {record.origin for record in by_name.values()} == {Origin.VALUE}
    assert [license.spdx_id for license in lib.licenses] == ["MIT"]
    assert lib.source is not None
    assert lib.source.urls == ("https://example.org/pynix-bom-lib-2.0.tar.gz",)
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(cyclonedx(bom))) is None


@LINUX_CHROOT_BUILD
async def test_a_path_named_without_context_still_finds_its_value(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    """A manifest whose context was discarded names `wrapper` in its text alone; the record still holds it."""
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, store, eval_store, state = opened
        await state.set_record_derivations(True)
        built = await state.eval_expr(f"(import {packages}).wrapper")
        await state.force(built)
        root = await (await built.get("outPath")).realise_string()
        manifest = await state.eval_expr(f'builtins.unsafeDiscardStringContext "${{(import {packages}).wrapper}}/lib"')
        await state.force(manifest)
        assert await context_roots(eval_store, manifest) == ()
        bom = await closure_bom(store, [root], name="cluster", drv_store=eval_store, state=state)

    by_name = {record.name: record for record in bom.inventory.components if record.path != "cluster"}
    assert {record.origin for record in by_name.values()} == {Origin.VALUE}
    assert [license.spdx_id for license in by_name["pynix-bom-lib"].licenses] == ["MIT"]


@LINUX_CHROOT_BUILD
async def test_the_build_closure_is_its_own_document(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    """What builds `wrapper`, read from `.drv` files alone: the test builds nothing."""
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, _store, eval_store, state = opened
        await state.set_record_derivations(True)
        manifest = await state.eval_expr(f'builtins.toJSON {{ command = "${{(import {packages}).wrapper}}/lib"; }}')
        await state.force(manifest)
        roots = await context_derivations(eval_store, manifest)
        bom = await build_bom(eval_store, roots, name="cluster", state=state)

    assert bom.buildtime
    by_name = {record.name: record for record in bom.inventory.components if record.path != "cluster"}
    lib, wrapper = by_name["pynix-bom-lib"], by_name["pynix-bom-wrapper"]
    # The runtime closure is these two; the build closure holds the toolchain too.
    assert "bash" in by_name
    assert [license.spdx_id for license in lib.licenses] == ["MIT"]
    assert lib.path in bom.inventory.dependencies[wrapper.path]
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(cyclonedx(bom))) is None


@LINUX_CHROOT_BUILD
async def test_a_path_with_no_readable_deriver_keeps_the_name_it_spells(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path
) -> None:
    added = tmp_path / "pynix-bom-added-3.1"
    added.write_text("x", encoding="utf-8")
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, store, _eval_store, state = opened
        root = await (
            await state.eval_expr(f"builtins.toString (builtins.path {{ path = {added}; }})")
        ).realise_string()
        bom = await closure_bom(store, [root], name="cluster")

    (record,) = [record for record in bom.inventory.components if record.path != "cluster"]
    assert (record.name, record.version, record.source) == ("pynix-bom-added", "3.1", None)
    assert record.origin is Origin.NAME
