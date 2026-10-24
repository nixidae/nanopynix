from __future__ import annotations

import json
from pathlib import Path

import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, with_nixpkgs

import pynix._util as util_module
from pynix import parse


def _store_path_basename(path: str) -> str:
    return path.split("/nix/store/", 1)[1]


@LINUX_CHROOT_BUILD
async def test_print_roots(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(["store", "gc", "print-roots", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "roots" in data
    assert isinstance(data["roots"], list)
    for root in data["roots"]:
        assert "link" in root
        assert "path" in root
        assert root["path"].startswith("/nix/store/")


@LINUX_CHROOT_BUILD
async def test_print_alive(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(["store", "gc", "print-alive", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert isinstance(data["paths"], list)
    for path in data["paths"]:
        assert path.startswith("/nix/store/")


@LINUX_CHROOT_BUILD
async def test_print_dead_dry_run(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(["store", "gc", "print-dead", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert "bytesFreed" in data
    assert isinstance(data["paths"], list)
    assert isinstance(data["bytesFreed"], int)


def test_print_dead_help(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        parse(["store", "gc", "print-dead", "--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--rip" in captured.out


@LINUX_CHROOT_BUILD
async def test_path_from_hash_part(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = _store_path_basename(populated_store["hello_path"])
    hash_part = store_path.split("-", 1)[0]
    cmd = parse(["store", "path-from-hash-part", hash_part, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["path"] == f"/nix/store/{store_path}"


@LINUX_CHROOT_BUILD
async def test_store_info(
    populated_store: dict[str, str],
    nix_backend: str,
    capsys: pytest.CaptureFixture[str],
):
    cmd = parse(["store", "info", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    if nix_backend == "local":
        assert data["uri"].startswith("local://")
    else:
        # An open unix:// store may report back as the bare "daemon"
        # shorthand (Nix's own collapse for a store whose socket matches the
        # process's default) or drop its query params on older Nix versions.
        assert data["uri"] == "daemon" or data["uri"].startswith(("daemon?", "unix://"))
    assert data["storeDir"] == "/nix/store"


@LINUX_CHROOT_BUILD
async def test_is_valid_path(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = populated_store["hello_path"]
    cmd = parse(["store", "is-valid-path", store_path, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": store_path, "valid": True}


@LINUX_CHROOT_BUILD
async def test_follow_links_to_store_path(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(
        ["store", "follow-links-to-store-path", populated_store["hello_path"], "--store", populated_store["store_url"]],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"]}


@LINUX_CHROOT_BUILD
async def test_compute_fs_closure(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = populated_store["hello_path"]
    cmd = parse(["store", "compute-fs-closure", store_path, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert store_path in data["paths"]


@LINUX_CHROOT_BUILD
async def test_query_missing(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(
        ["store", "query-missing", populated_store["hello_path"], "--store", populated_store["store_url"]],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["unknown"] == []
    assert data["willBuild"] == []
    assert data["willSubstitute"] == []
    assert isinstance(data["downloadSize"], int)
    assert isinstance(data["narSize"], int)


async def test_query_derivation_outputs(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "query-outputs";
      version = "1";
      dontUnpack = true;
      installPhase = ''
        echo hi > "$out"
      '';
    }
    """,
            nixpkgs_path,
        )
    )
    show = parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await show.run()
    captured = capsys.readouterr()
    # Nix's document keys each derivation by its base name.
    drv_path = f"/nix/store/{next(iter(json.loads(captured.out)['derivations']))}"

    cmd = parse(
        ["store", "query-derivation-outputs", drv_path, *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["paths"]) == 1
    assert data["paths"][0].startswith("/nix/store/")


@LINUX_CHROOT_BUILD
async def test_query_valid_derivers(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = populated_store["hello_path"]
    cmd = parse(["store", "query-valid-derivers", store_path, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


@LINUX_CHROOT_BUILD
async def test_list_valid_paths(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(["store", "list-valid-paths", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert populated_store["hello_path"] in data["paths"]


@LINUX_CHROOT_BUILD
async def test_query_referrers(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = populated_store["hello_path"]
    cmd = parse(["store", "query-referrers", store_path, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


@LINUX_CHROOT_BUILD
async def test_query_substitutable_paths(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(
        ["store", "query-substitutable-paths", populated_store["hello_path"], "--store", populated_store["store_url"]],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


@LINUX_CHROOT_BUILD
async def test_add_temp_root(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(
        ["store", "add-temp-root", populated_store["hello_path"], "--store", populated_store["store_url"]],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"], "added": True}


@LINUX_CHROOT_BUILD
async def test_add_perm_root_and_indirect_root(
    populated_store: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root_path = tmp_path / "pynix-gc-root"
    cmd = parse(
        [
            "store",
            "add-perm-root",
            populated_store["hello_path"],
            str(root_path),
            "--store",
            populated_store["store_url"],
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"], "gcRoot": str(root_path)}
    assert root_path.is_symlink()

    cmd = parse(["store", "add-indirect-root", str(root_path), "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": str(root_path), "added": True}


@LINUX_CHROOT_BUILD
async def test_ensure_path(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    store_path = populated_store["hello_path"]
    cmd = parse(["store", "ensure-path", store_path, "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": store_path, "valid": True}


@LINUX_CHROOT_BUILD
async def test_store_cat_reads_file_from_populated_store(
    populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]
):
    cmd = parse(["store", "cat", populated_store["text_path"], "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    assert captured.out == "temporary-store-message\n"


@LINUX_CHROOT_BUILD
async def test_store_ls_lists_populated_store_path(populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]):
    cmd = parse(["store", "ls", populated_store["hello_path"], "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    assert "bin" in captured.out.splitlines()

    cmd = parse(["store", "ls", populated_store["hello_path"], "--json", "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert {"name": "bin", "type": "directory"} in data["entries"]


async def _add_tree(environment: NixTestEnvironment, tree: Path, capsys: pytest.CaptureFixture[str]) -> str:
    await parse(["store", "add-path", str(tree), *environment.pynix_store_args()]).run()
    return json.loads(capsys.readouterr().out)["path"]


async def test_store_cat_reads_through_a_relocated_store(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    tree = tmp_path / "pynix-store-cat-test"
    (tree / "share").mkdir(parents=True)
    (tree / "share" / "message").write_text("cat-output\n")
    out_path = await _add_tree(isolated_nix_environment, tree, capsys)

    cmd = parse(["store", "cat", f"{out_path}/share/message", *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert captured.out == "cat-output\n"


async def test_store_ls_reads_through_a_relocated_store(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    tree = tmp_path / "pynix-store-ls-test"
    (tree / "bin").mkdir(parents=True)
    (tree / "share").mkdir()
    (tree / "bin" / "tool").touch()
    (tree / "share" / "message").touch()
    out_path = await _add_tree(isolated_nix_environment, tree, capsys)

    cmd = parse(["store", "ls", out_path, *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["bin", "share"]

    cmd = parse(["store", "ls", out_path, "--json", *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "entries": [
            {"name": "bin", "type": "directory"},
            {"name": "share", "type": "directory"},
        ],
    }


async def test_store_diff_closures_reports_added_removed_and_size_delta(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
):
    async with util_module.huggorm_store_session(isolated_nix_environment.store_uri) as (_session, store):
        common = await store.add_to_store("common", b"common")
        old = await store.add_to_store("old", b"old")
        new = await store.add_to_store("new", b"new!")
        before = await store.add_to_store("before", b"before", references=[common, old])
        after = await store.add_to_store("after", b"after", references=[common, new])
        sizes = {
            await store.print_store_path(path): (await store.query_path_info(path)).nar_size()
            for path in (common, old, new, before, after)
        }
        common_path, old_path, new_path, before_path, after_path = sizes

    cmd = parse(["store", "diff-closures", before_path, after_path, *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    before_size = sizes[common_path] + sizes[old_path] + sizes[before_path]
    after_size = sizes[common_path] + sizes[new_path] + sizes[after_path]
    assert data == {
        "added": [{"narSize": sizes[path], "path": path} for path in sorted([after_path, new_path])],
        "after": after_path,
        "afterNarSize": after_size,
        "before": before_path,
        "beforeNarSize": before_size,
        "narSizeDelta": after_size - before_size,
        "removed": [{"narSize": sizes[path], "path": path} for path in sorted([before_path, old_path])],
    }


async def test_optimise_empty_store(
    isolated_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["store", "optimise", *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"optimised": True}


async def test_verify_empty_store(
    isolated_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["store", "verify", *isolated_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"errors": False}
