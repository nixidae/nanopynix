"""The two source kinds that name a place, and not an index.

`uv.lock` writes seven source kinds. Four of them are a registry or a directory
of the workspace, and one is a file of this machine, which
`test_vendored_app.py` covers. The other two name a place on the network:

    git   a repository and the commit that the resolution pinned. The package
          carries no `wheels` and no `sdist` at all, so the checkout is the
          whole input and one PEP 517 `build_wheel` call gives the install.
    url   an archive that a URL names directly. The `sdist` table then carries
          the hash alone, and `source` carries the address and the
          subdirectory.

**The two kinds take two different routes, and neither is a new node kind for
its own sake.** A git source becomes a tree, which is the route that a local
project already takes. A URL source becomes an ordinary source distribution,
which is the route that an artefact of an index already takes. What each one
adds is a subdirectory, because one archive and one repository may hold more
than one project.

`tests/fixtures/locks/git-subdirectory.lock` and `url-subdirectory.lock` are
the two files that uv wrote for these shapes. This module reads the references
out of them, so the strings under test are the strings that uv writes.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from ddrn_venv import LocalNode, Lock, Plan, is_pyproject, read_pyproject

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
LOCKS = HERE / "fixtures/locks"

#: The parse that `uv.nix` uses for a git reference. It takes no `lib`, so this
#: evaluation needs no nixpkgs.
UV_GIT = EXAMPLE / "uv-git.nix"


def source_of(lock_name: str, package_name: str) -> Mapping[str, Any]:
    """The `source` table of one package of one fixture lock file."""
    parsed = Lock.read(LOCKS / f"{lock_name}.lock")
    return next(package.source for package in parsed.packages if package.name == package_name)


async def nix_eval(expression: str) -> object:
    """Evaluate one Nix expression, and give back what it says.

    `--strict` forces every attribute, so a `throw` inside one of them reaches
    this caller rather than travel out as an unevaluated thunk.
    """
    result = await anyio.run_process(
        ["nix-instantiate", "--eval", "--strict", "--json", "--expr", expression],
        env=os.environ,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"nix-instantiate failed with {result.returncode}: {result.stderr.decode()}")
    return json.loads(result.stdout)


async def git_parts(name: str, spec: str) -> object:
    return await nix_eval(f"import {UV_GIT} {json.dumps(name)} {json.dumps(spec)}")


async def git_error(name: str, spec: str) -> str:
    """The message that `uv-git.nix` raises for a reference it cannot read."""
    result = await anyio.run_process(
        [
            "nix-instantiate",
            "--eval",
            "--strict",
            "--json",
            "--expr",
            f"import {UV_GIT} {json.dumps(name)} {json.dumps(spec)}",
        ],
        env=os.environ,
        check=False,
    )
    if result.returncode == 0:
        raise AssertionError(f"{spec!r} was expected to fail, and it parsed")
    return result.stderr.decode()


def archive_with(tmp_path: Path, files: Sequence[tuple[str, str]]) -> Path:
    """A `.tar.gz` whose one top directory holds each of `files`."""
    path = tmp_path / "many-projects-1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files:
            data = text.encode()
            info = tarfile.TarInfo(f"many-projects-1.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


class TestTheGitReference:
    """`uv-git.nix`, over the strings that uv writes."""

    async def test_it_takes_the_url_the_commit_and_the_subdirectory(self) -> None:
        # The one reference of `git-subdirectory.lock`. uv put three things in
        # it, and the fragment is the commit that the resolution pinned.
        spec = source_of("git-subdirectory", "hatchling")["git"]
        assert await git_parts("hatchling", spec) == {
            "url": "https://github.com/pypa/hatch.git",
            "rev": "cbf6598e5cbce3ba9097023c5bf783001ebbcbcb",
            "subdirectory": "backend",
        }

    async def test_the_name_that_the_user_gave_the_commit_is_dropped(self) -> None:
        # `kitchen-sink.lock` asks for a tag, and uv wrote `?tag=20.3.1`
        # beside the commit. A tag moves and a commit does not, so the fetch
        # names the commit alone.
        spec = source_of("kitchen-sink", "pip")["git"]
        assert await git_parts("pip", spec) == {
            "url": "https://github.com/pypa/pip.git",
            "rev": "f94a429e17b450ac2d3432f46492416ac2cf58ad",
            "subdirectory": "",
        }

    async def test_a_reference_with_no_query_gives_no_subdirectory(self) -> None:
        parts = await git_parts("x", "https://example.com/x.git#" + "0" * 40)
        assert parts == {"url": "https://example.com/x.git", "rev": "0" * 40, "subdirectory": ""}

    async def test_a_reference_with_no_commit_says_so(self) -> None:
        # uv always writes the commit. A reference without one comes from a
        # hand edit, and the failure has to name what is missing rather than
        # fetch the default branch of the repository.
        message = await git_error("x", "https://example.com/x.git?tag=v1")
        assert "names no commit" in message


class TestTheKeysThatThisReaderRefuses:
    """The two query keys of `locked_git_url` that no node of this graph answers.

    **`locked_git_url` of uv writes five keys, and this reader takes three.**
    `subdirectory` it uses, and `tag`, `branch` and `rev` it drops. The other
    two change what the package *is*, so a reader that ignored either one would
    build the wrong thing and say nothing.
    """

    async def test_a_reference_that_names_a_file_of_the_repository(self) -> None:
        # `from_git_path_source_dist` of uv writes `path=`, and the package is
        # then an archive or a wheel that lives in the checkout. Building the
        # root of the repository instead would install another project.
        message = await git_error(
            "x", "https://example.com/x.git?path=dist%2Fx-1.0-py3-none-any.whl&rev=v1#" + "0" * 40
        )
        assert "is a file" in message

    async def test_a_reference_that_needs_git_lfs(self) -> None:
        # `builtins.fetchGit` fetches no LFS object, so the checkout would hold
        # a pointer file of a hundred bytes in place of each large file, and
        # the build would read that pointer as if it were the file.
        message = await git_error("x", "https://example.com/x.git?lfs=true&rev=v1#" + "0" * 40)
        assert "Git LFS" in message

    async def test_lfs_that_is_not_enabled_is_not_a_refusal(self) -> None:
        # uv appends the key only when LFS is on, and `GitLfs::from` compares
        # the value against `true` with no regard for case. So a value that is
        # not `true` leaves the checkout ordinary.
        parts = await git_parts("x", "https://example.com/x.git?lfs=false&rev=v1#" + "0" * 40)
        assert parts == {"url": "https://example.com/x.git", "rev": "0" * 40, "subdirectory": ""}


class TestThePlanOfAGitSource:
    """One node, and no archive between the tree and the wheel."""

    def test_a_checkout_becomes_one_wheel_node(self) -> None:
        plan = Plan()
        name = plan.local_wheel_node(
            LocalNode(
                name="hatchling",
                source="/nix/store/aaaa-hatch",
                backend="hatchling.ouroboros",
                backend_path=["pathspec-0.12.1"],
                subdirectory="backend",
            )
        )
        assert name == "hatchling-from-tree"
        assert [node["kind"] for node in plan.nodes] == ["local-wheel"]

    def test_the_node_carries_the_subdirectory(self) -> None:
        # The backend runs where `pyproject.toml` is. `pypa/hatch` holds
        # `hatchling` in `backend`, so the top of the checkout declares
        # another project.
        plan = Plan()
        plan.local_wheel_node(
            LocalNode(
                name="hatchling",
                source="/nix/store/aaaa-hatch",
                backend="hatchling.ouroboros",
                backend_path=[],
                subdirectory="backend",
            )
        )
        assert plan.nodes[0]["subdirectory"] == "backend"

    def test_a_tree_at_the_top_carries_an_empty_subdirectory(self) -> None:
        plan = Plan()
        plan.local_wheel_node(
            LocalNode(name="b", source="/nix/store/aaaa-b", backend="flit_core.buildapi", backend_path=[])
        )
        assert plan.nodes[0]["subdirectory"] == ""

    def test_the_node_names_no_root(self) -> None:
        # A `local-wheel` install holds the code, so nothing redirects to a
        # tree of the user. That is the whole difference from an editable.
        plan = Plan()
        plan.local_wheel_node(
            LocalNode(name="b", source="/nix/store/aaaa-b", backend="flit_core.buildapi", backend_path=[])
        )
        assert "root" not in plan.nodes[0]


class TestTheArchiveOfAUrlSource:
    """Which `pyproject.toml` of an archive is the one that PEP 517 reads."""

    def test_the_subdirectory_moves_the_answer_down_by_exactly_that_much(self) -> None:
        assert is_pyproject("hatch-1.27.0/backend/pyproject.toml", "backend")
        assert not is_pyproject("hatch-1.27.0/pyproject.toml", "backend")
        assert not is_pyproject("hatch-1.27.0/backend/src/pyproject.toml", "backend")

    def test_a_source_with_no_subdirectory_reads_the_top(self) -> None:
        assert is_pyproject("demo-1.0/pyproject.toml")
        assert not is_pyproject("demo-1.0/backend/pyproject.toml")

    def test_the_declaration_of_the_named_project_is_the_one_that_is_read(self, tmp_path: Path) -> None:
        # **One archive holds two projects, and each one declares a backend.**
        # A reader that took the first `pyproject.toml` it found would build
        # the wrong project with the wrong backend, and nothing downstream
        # would say so.
        archive = archive_with(
            tmp_path,
            [
                ("pyproject.toml", '[build-system]\nbuild-backend = "outer"\nrequires = []\n'),
                ("backend/pyproject.toml", '[build-system]\nbuild-backend = "inner"\nrequires = []\n'),
            ],
        )
        assert read_pyproject(archive, "backend")["build-system"]["build-backend"] == "inner"
        assert read_pyproject(archive)["build-system"]["build-backend"] == "outer"

    def test_an_archive_with_nothing_at_that_subdirectory_gives_an_empty_table(self, tmp_path: Path) -> None:
        # PEP 517 gives a fallback backend for a distribution that declares
        # none, and `build_system` applies it. An empty table is that case,
        # and it is not an error here.
        archive = archive_with(tmp_path, [("pyproject.toml", "[build-system]\n")])
        assert read_pyproject(archive, "backend") == {}


class TestTheReaderOfTheLockFile:
    """What `ddrn_venv._uvlock` says about each of the two kinds."""

    def test_a_git_package_carries_no_artefact(self) -> None:
        parsed = Lock.read(LOCKS / "git-subdirectory.lock")
        package = next(item for item in parsed.packages if item.name == "hatchling")
        assert package.is_git
        assert not package.is_local
        assert package.wheels == ()
        assert package.sdist is None

    def test_a_url_package_carries_a_hash_and_no_url(self) -> None:
        # This is the shape that `uv.nix` joins: the address is on the source,
        # and the hash is on the `sdist` table.
        source = source_of("url-subdirectory", "hatchling")
        assert source["subdirectory"] == "backend"
        assert source["url"].endswith("hatchling-v1.27.0.tar.gz")


@pytest.mark.parametrize("name", ["git-subdirectory", "url-subdirectory"])
def test_the_fixture_lock_files_parse(name: str) -> None:
    # A guard on the fixtures themselves. Every test above reads one of them,
    # and a lock file that stopped parsing would make each one fail for a
    # reason that is not the subject of that test.
    assert Lock.read(LOCKS / f"{name}.lock").packages


def test_the_two_fixtures_describe_the_same_project() -> None:
    # `pypa/hatch` is the repository that both fixtures name, and `backend` is
    # where `hatchling` lives in it. The two lock files therefore describe one
    # project through two source kinds, which is what makes them a pair.
    assert source_of("git-subdirectory", "hatchling")["git"].startswith("https://github.com/pypa/hatch.git")
    assert "pypa/hatch" in source_of("url-subdirectory", "hatchling")["url"]
