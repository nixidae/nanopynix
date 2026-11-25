"""Packages inside store paths that are not on this host's disk: read through the store, and cached.

A ``dummy://`` store holds its paths in memory, as a remote store does for
the host that evaluates against it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from huggorm import AsyncStore, SourceAccessor, StorePath
from test_sbom_embedded import _program

from pynix._impl._sbom_embedded import Package, scan
from pynix._impl._sbom_model import Inventory, Record
from pynix.bom import _patch_texts, embedded_packages, package_json, package_of_json

DUMMY = "dummy://?read-only=false"


def _tools(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    shutil.copy(_program("go-md2man"), root / "bin" / "go-md2man")
    shutil.copy(_program("rust-audit-info"), root / "bin" / "rust-audit-info")
    (root / "bin" / "link").symlink_to("go-md2man")
    package = root / "lib" / "node_modules" / "semver"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "semver", "version": "7.7.2"}))
    dist = root / "lib" / "python3.14" / "site-packages" / "demo-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n")
    # A dotted directory: its name is kept whole.
    hidden = root / ".node_modules" / "x"
    hidden.mkdir(parents=True)
    (hidden / "package.json").write_text(json.dumps({"name": "x", "version": "1"}))
    return root


class Memory:
    def __init__(self, held: Mapping[str, tuple[Package, ...]] | None = None) -> None:
        self.held = dict(held or {})
        self.kept: dict[str, tuple[Package, ...]] = {}

    async def known(self, paths: Sequence[str]) -> Mapping[str, tuple[Package, ...]]:
        return {p: self.held[p] for p in paths if p in self.held}

    async def keep(self, scans: Mapping[str, tuple[Package, ...]]) -> None:
        self.kept.update(scans)
        self.held.update(scans)


async def test_a_path_only_the_store_holds_names_what_it_names_on_disk(tmp_path: Path) -> None:
    store = AsyncStore(DUMMY)
    tree = _tools(tmp_path / "tools")
    printed = await store.print_store_path(await store.add_path_to_store("tools-1.0", str(tree)))
    assert not Path(printed).exists()
    expected = scan(tree, printed)
    assert len(expected) > 3
    assert await embedded_packages(store, [printed]) == {printed: expected}


async def test_a_cache_keeps_every_scan_and_answers_for_the_paths_it_holds(tmp_path: Path) -> None:
    store = AsyncStore(DUMMY)
    tools = await store.print_store_path(await store.add_path_to_store("tools-1.0", str(_tools(tmp_path / "t"))))
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "README").write_text("nothing here")
    empty = await store.print_store_path(await store.add_path_to_store("empty", str(tmp_path / "empty")))
    cache = Memory()
    found = await embedded_packages(store, [tools, empty], cache=cache)
    assert set(found) == {tools}
    # A path that names nothing is an answer too.
    assert cache.kept == {tools: found[tools], empty: ()}
    # A store that holds neither path: the cache answers alone.
    again = Memory(cache.held)
    assert await embedded_packages(AsyncStore(DUMMY), [tools, empty], cache=again) == found
    assert again.kept == {}


async def test_a_path_no_store_holds_is_not_kept(tmp_path: Path) -> None:
    cache = Memory()
    missing = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-gone"
    assert await embedded_packages(AsyncStore(DUMMY), [missing], cache=cache) == {}
    assert cache.kept == {}


class Counting(AsyncStore):
    """A store that counts the accessors it opens: on a remote store each one fetches a NAR."""

    opened = 0

    async def get_fs_accessor(self, path: StorePath, require_valid_path: bool = True) -> SourceAccessor | None:
        self.opened += 1
        return await super().get_fs_accessor(path, require_valid_path)


async def test_patches_inside_one_store_path_fetch_it_once(tmp_path: Path) -> None:
    store = Counting(DUMMY)
    source = tmp_path / "source"
    (source / "pkgs").mkdir(parents=True)
    (source / "pkgs" / "a.patch").write_text("--- a\n")
    (source / "pkgs" / "b.patch").write_text("--- b\n")
    root = await store.print_store_path(await store.add_path_to_store("source", str(source)))
    patches = (f"{root}/pkgs/a.patch", f"{root}/pkgs/b.patch", f"{root}/pkgs/gone.patch")
    listed = Inventory(target=Record(path="env", name="env", patches=patches), components=())
    assert await _patch_texts(store, listed) == {patches[0]: "--- a\n", patches[1]: "--- b\n"}
    assert store.opened == 1


def test_a_package_survives_its_json(tmp_path: Path) -> None:
    for package in scan(_tools(tmp_path), "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-tools-1.0"):
        assert package_of_json(json.loads(json.dumps(package_json(package)))) == package
