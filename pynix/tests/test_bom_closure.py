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
from pynix._impl._sbom_model import parse_walk
from pynix._nix_helpers import helper
from pynix.bom import (
    Origin,
    build_bom,
    closure_bom,
    context_derivations,
    context_roots,
    cyclonedx,
    cyclonedx_compositions,
    cyclonedx_formulation,
)

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
    assert [entry.spdx_id for entry in lib.licenses] == ["MIT"]
    assert lib.source is not None
    assert lib.source.urls == ("https://example.org/pynix-bom-lib-2.0.tar.gz",)
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(cyclonedx(bom))) is None


#: `split` has two outputs, and `dev` names `out`, as a pkg-config file does.
#: `user`'s build coerces `split.dev` alone, as stdenv's `chooseDevOutputs` does,
#: and keeps only the `out` path it reads there: the runtime closure holds
#: `split`'s `out` and not `dev`, and the state recorded only `dev`.
_SPLIT = """
with import <nixpkgs> { };
rec {
  split = runCommand "pynix-bom-split-3.0" {
    pname = "pynix-bom-split";
    version = "3.0";
    outputs = [ "out" "dev" ];
    meta.license = pkgs.lib.licenses.mit;
  } "mkdir $out $dev; echo $out > $dev/out";
  user = runCommand "pynix-bom-split-user" { } "mkdir $out; cat ${split.dev}/out > $out/lib";
}
"""


@LINUX_CHROOT_BUILD
async def test_an_output_takes_the_value_recorded_for_its_sibling(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, nixpkgs_path: str
) -> None:
    """Only `split.dev` was coerced, so only it was recorded; `split`'s `out` takes that value through its deriver."""
    path = tmp_path / "split.nix"
    path.write_text(with_nixpkgs(_SPLIT, nixpkgs_path), encoding="utf-8")
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, store, eval_store, state = opened
        await state.set_record_derivations(True)
        manifest = await state.eval_expr(f'builtins.toJSON {{ command = "${{(import {path}).user}}/lib"; }}')
        await state.force(manifest)
        await manifest.realise_string()
        roots = await context_roots(eval_store, manifest)
        recorded = set(await state.recorded_out_paths())
        bom = await closure_bom(store, roots, name="cluster", drv_store=eval_store, state=state)

    (split,) = [record for record in bom.inventory.components if record.name == "pynix-bom-split"]
    assert split.output_name == "out"
    assert not any(record.output_name == "dev" for record in bom.inventory.components)
    assert split.path not in recorded
    assert split.origin == Origin.VALUE
    assert [entry.spdx_id for entry in split.licenses] == ["MIT"]


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
    assert [entry.spdx_id for entry in by_name["pynix-bom-lib"].licenses] == ["MIT"]


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
    assert [entry.spdx_id for entry in lib.licenses] == ["MIT"]
    assert lib.path in bom.inventory.dependencies[wrapper.path]
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(cyclonedx(bom))) is None


async def test_a_git_fetch_in_a_drv_names_its_repository(shared_nix_environment: NixTestEnvironment) -> None:
    """fetchgit writes `url` and `rev` into its `.drv`; the build-time record keeps both, and the `.drv` path."""
    expr = """
      let
        src = derivation {
          name = "pynix-bom-git-src"; system = "x86_64-linux"; builder = "/bin/sh";
          url = "https://example.org/git.git"; rev = "abc123";
          outputHashMode = "recursive"; outputHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
        };
        pkg = derivation {
          name = "pynix-bom-git-1.0"; version = "1.0"; system = "x86_64-linux"; builder = "/bin/sh"; inherit src;
        };
      in builtins.toJSON { command = "${pkg}"; }
    """
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, _store, eval_store, state = opened
        manifest = await state.eval_expr(expr)
        await state.force(manifest)
        roots = await context_derivations(eval_store, manifest)
        bom = await build_bom(eval_store, roots, name="cluster")

    (pkg,) = [record for record in bom.inventory.components if record.name == "pynix-bom-git"]
    assert pkg.source is not None
    assert (pkg.source.urls, pkg.source.repository, pkg.source.rev) == ((), "https://example.org/git.git", "abc123")
    assert pkg.drv_path is not None
    assert pkg.drv_path == roots[pkg.path]


async def test_each_drv_of_the_build_closure_is_a_formulation_task(shared_nix_environment: NixTestEnvironment) -> None:
    """A hook with no version is a task with no component; a git fetch is a clone; two outputs are one component.

    Builds nothing.
    """
    expr = """
      let
        sh = { system = "x86_64-linux"; builder = "/bin/sh"; };
        hook = derivation (sh // { name = "pynix-bom-hook"; });
        src = derivation (sh // {
          name = "pynix-bom-src-1.0"; url = "https://example.org/git.git"; rev = "abc123";
          outputHashMode = "recursive"; outputHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
        });
        pkg = derivation (sh // {
          name = "pynix-bom-pkg-1.0"; version = "1.0"; args = [ "-c" "echo it's built" ]; outputs = [ "out" "dev" ];
          inherit src hook;
        });
        app = derivation (sh // { name = "pynix-bom-app-1.0"; headers = pkg.dev; });
      in builtins.toJSON { command = "${app}"; }
    """
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, _store, eval_store, state = opened
        manifest = await state.eval_expr(expr)
        await state.force(manifest)
        roots = await context_derivations(eval_store, manifest)
        bom = await build_bom(eval_store, roots, name="cluster")

    text = json.dumps(cyclonedx(bom))
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(text) is None
    doc = json.loads(text)
    by_name = {record.name: record for record in bom.inventory.components}
    app, pkg, src = by_name["pynix-bom-app"], by_name["pynix-bom-pkg"], by_name["pynix-bom-src"]
    (workflow,) = doc["formulation"][0]["workflows"]
    tasks = {task["name"]: task for task in workflow["tasks"]}
    assert set(tasks) == {"pynix-bom-hook", "pynix-bom-src-1.0", "pynix-bom-pkg-1.0", "pynix-bom-app-1.0"}
    built = tasks["pynix-bom-pkg-1.0"]
    assert built["uid"] == pkg.drv_path
    # `app` reads `pkg.dev`, and `dev` is part of the `pkg` component.
    assert tasks["pynix-bom-app-1.0"]["inputs"] == [{"resource": {"ref": pkg.ref}}]
    assert tasks["pynix-bom-app-1.0"]["outputs"] == [{"type": "artifact", "resource": {"ref": app.ref}}]
    assert built["steps"][0]["commands"] == [{"executed": "/bin/sh -c 'echo it'\"'\"'s built'"}]
    assert built["inputs"] == [{"resource": {"ref": src.ref}}]
    assert built["outputs"] == [{"type": "artifact", "resource": {"ref": pkg.ref}}]
    assert (tasks["pynix-bom-src-1.0"]["taskTypes"], built["taskTypes"]) == (["clone"], ["build"])
    assert "outputs" not in tasks["pynix-bom-hook"]
    depends = {entry["ref"]: entry["dependsOn"] for entry in workflow["taskDependencies"]}
    assert sorted(depends[built["bom-ref"]]) == sorted(
        [tasks["pynix-bom-hook"]["bom-ref"], tasks["pynix-bom-src-1.0"]["bom-ref"]]
    )
    (renamed,) = json.loads(json.dumps(cyclonedx_formulation(bom, {pkg.path: "nix:pynix-bom-pkg@1.0"})))[0]["workflows"]
    assert next(t for t in renamed["tasks"] if t["name"] == "pynix-bom-pkg-1.0")["outputs"][0]["resource"] == {
        "ref": "nix:pynix-bom-pkg@1.0"
    }


async def test_a_component_that_reads_a_path_with_no_version_has_incomplete_dependencies(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """`pkg` reads a hook that is no component, so its `dependsOn` lacks it. Builds nothing."""
    expr = """
      let
        sh = { system = "x86_64-linux"; builder = "/bin/sh"; };
        hook = derivation (sh // { name = "pynix-bom-hook"; });
        lib = derivation (sh // { name = "pynix-bom-lib-2.0"; });
        pkg = derivation (sh // { name = "pynix-bom-pkg-1.0"; inherit hook lib; });
        app = derivation (sh // { name = "pynix-bom-app-1.0"; inherit pkg; });
      in builtins.toJSON { command = "${app}"; }
    """
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, _store, eval_store, state = opened
        manifest = await state.eval_expr(expr)
        await state.force(manifest)
        roots = await context_derivations(eval_store, manifest)
        bom = await build_bom(eval_store, roots, name="cluster")

    text = json.dumps(cyclonedx(bom))
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(text) is None
    by_name = {record.name: record for record in bom.inventory.components}
    app, pkg, lib = by_name["pynix-bom-app"], by_name["pynix-bom-pkg"], by_name["pynix-bom-lib"]
    composed = {entry["aggregate"]: entry["dependencies"] for entry in json.loads(text)["compositions"]}
    assert composed == {"complete": sorted([app.ref, lib.ref, "cluster"]), "incomplete": [pkg.ref]}
    renamed = json.loads(json.dumps(cyclonedx_compositions(bom, {pkg.path: "nix:pynix-bom-pkg@1.0"})))
    assert {entry["aggregate"]: entry["dependencies"] for entry in renamed}["incomplete"] == ["nix:pynix-bom-pkg@1.0"]


async def test_the_walk_reads_past_patches_that_name_no_file(shared_nix_environment: NixTestEnvironment) -> None:
    """gnulib's `passthru.patches` is an attribute set; `tryEval` would not catch `toString` of it."""
    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        _session, _store, _eval_store, state = opened
        odd = await state.eval_expr(
            '[ (derivation { name = "odd-1.0"; system = "x86_64-linux"; builder = "/bin/sh"; }'
            ' // { patches = { fix = "/nix/store/x-fix.patch"; }; src = { urls = "https://a"; }; }) ]'
        )
        walk = await (await helper(state, "sbom")).get("values")
        await state.force(walk)
        (record,) = parse_walk(await (await walk(odd)).string_value()).values()
    assert (record.name, record.patches, record.source) == ("odd-1.0", (), None)


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
