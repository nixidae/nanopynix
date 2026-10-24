"""``pynix flake lock`` and ``pynix flake update`` against the linked flakes.

The oracle is the lock file that ``nix flake lock`` writes for the same
fixture, byte for byte. The other tests name what the oracle cannot: which
node moved, and which node stayed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pygit2
import pytest
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse
from support.nix_oracle import require_matching_nix_cli
from test_support.git_fixtures import LinkedFlakes, commit_files, init_linked_flakes
from test_support.subprocess_output import run_process


async def _pynix(environment: NixTestEnvironment, *argv: str) -> None:
    await parse([*argv, *environment.pynix_store_args()]).run()


def _locks(flakes: LinkedFlakes) -> dict[str, Any]:
    return json.loads((flakes.root / "flake.lock").read_text(encoding="utf-8"))


def _locked_rev(flakes: LinkedFlakes, name: str) -> str:
    nodes = _locks(flakes)["nodes"]
    return nodes[nodes["root"]["inputs"][name]]["locked"]["rev"]


def _new_commit(repo_dir: Path, marker: str) -> str:
    """Give *repo_dir* a new commit, and return its revision."""
    flake = repo_dir / "flake.nix"
    flake.write_text(flake.read_text(encoding="utf-8").replace('marker = "', f'marker = "{marker}-'), encoding="utf-8")
    return str(commit_files(pygit2.Repository(str(repo_dir)), flake, message=marker))


async def test_lock_writes_every_node(shared_nix_environment: NixTestEnvironment, tmp_path: Path) -> None:
    flakes = init_linked_flakes(tmp_path)

    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))

    nodes = _locks(flakes)["nodes"]
    assert set(nodes["root"]["inputs"]) == {"leaf", "mid"}
    assert nodes[nodes["root"]["inputs"]["mid"]]["inputs"]["leaf"] == ["leaf"]


async def test_lock_leaves_a_complete_lock_file_alone(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path
) -> None:
    """A new upstream commit is not a missing entry, so ``lock`` keeps the old one."""
    flakes = init_linked_flakes(tmp_path)
    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))
    lock_file = flakes.root / "flake.lock"
    before = lock_file.read_bytes()
    before_mtime = lock_file.stat().st_mtime_ns
    _new_commit(flakes.leaf, "moved")

    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))

    assert lock_file.read_bytes() == before
    assert lock_file.stat().st_mtime_ns == before_mtime


async def test_update_one_input_leaves_the_other(shared_nix_environment: NixTestEnvironment, tmp_path: Path) -> None:
    flakes = init_linked_flakes(tmp_path)
    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))
    old_mid = _locked_rev(flakes, "mid")
    new_leaf = _new_commit(flakes.leaf, "moved")
    _new_commit(flakes.mid, "moved")

    await _pynix(shared_nix_environment, "flake", "update", "leaf", "--flake", str(flakes.root))

    assert _locked_rev(flakes, "leaf") == new_leaf
    assert _locked_rev(flakes, "mid") == old_mid


async def test_update_with_no_input_updates_every_input(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path
) -> None:
    flakes = init_linked_flakes(tmp_path)
    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))
    new_leaf = _new_commit(flakes.leaf, "moved")
    new_mid = _new_commit(flakes.mid, "moved")

    await _pynix(shared_nix_environment, "flake", "update", "--flake", str(flakes.root))

    assert _locked_rev(flakes, "leaf") == new_leaf
    assert _locked_rev(flakes, "mid") == new_mid


async def test_lock_file_matches_nix(shared_nix_environment: NixTestEnvironment, tmp_path: Path) -> None:
    """The acceptance test: the bytes that ``nix flake lock`` writes, for the same flakes."""
    await require_matching_nix_cli()
    flakes = init_linked_flakes(tmp_path)
    lock_file = flakes.root / "flake.lock"
    result = await run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "lock",
            "--store",
            shared_nix_environment.store_uri,
            str(flakes.root),
        ],
    )
    if result.returncode != 0:
        pytest.fail(f"the oracle failed: nix flake lock {result.describe()}")
    theirs = lock_file.read_bytes()
    lock_file.unlink()

    await _pynix(shared_nix_environment, "flake", "lock", str(flakes.root))

    assert lock_file.read_bytes() == theirs
