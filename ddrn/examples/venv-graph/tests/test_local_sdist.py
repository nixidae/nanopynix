"""A local project that ships, rather than one that links.

`../run-uv.sh` builds `projects/packaged-app` both ways, and
`test_sealed_graph.py` builds the fixture both ways with no index. This module
covers the two halves that need neither: the shape of the plan, and the shape
of the archive.

**These are the two forms that uv itself writes.**

    uv sync    installs a member as a PEP 660 editable, so the environment
               follows the tree of the user.
    uv build   writes a source distribution. An index serves that archive, and
               a user who installs from it runs `build_wheel` over what the
               archive holds.

**The second form is two PEP 517 hooks, and the graph makes each one a node.**
`build_sdist` writes the archive, and `build_wheel` reads it. The archive
between them is a store path, so a reader can open the thing that a release
would upload.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import demoback
import pytest
from ddrn_venv import LocalNode, Plan

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The tree of the local project that every test below builds.
DEMO_APP = FIXTURES / "demo-app"

#: What PEP 625 makes of `demo-app` 0.1.0. The name of the archive is not the
#: name of the project, so a consumer that predicted the name would be wrong.
ARCHIVE = "demo_app-0.1.0.tar.gz"


@pytest.fixture
def plan() -> Plan:
    """A plan that already holds the two nodes of a shipped local project."""
    plan = Plan()
    archive = plan.local_sdist_node(
        LocalNode(
            name="demo-app",
            source="/nix/store/aaaa-demo-app-source",
            backend="demoback",
            backend_path=["demo-backend-1.0"],
        )
    )
    plan.local_build_node(
        name="demo-app",
        archive=archive,
        backend="demoback",
        backend_path=["demo-backend-1.0"],
    )
    return plan


def node_of(plan: Plan, kind: str) -> dict[str, object]:
    return next(node for node in plan.nodes if node["kind"] == kind)


class TestThePlan:
    def test_shipping_a_project_takes_two_nodes(self, plan: Plan) -> None:
        # One hook for each node. Neither can exist before the plan runs: the
        # backend is another node of the same graph.
        assert [node["kind"] for node in plan.nodes] == ["local-sdist", "sdist-build"]

    def test_the_wheel_node_reads_the_archive_node(self, plan: Plan) -> None:
        archive = node_of(plan, "local-sdist")
        build = node_of(plan, "sdist-build")
        assert build["from"] == archive["name"]

    def test_the_archive_node_carries_no_artifact(self, plan: Plan) -> None:
        # An artefact of the menu comes with a `drv` and an `out` that
        # `default.nix` discarded the dependency between. A local project has
        # neither: its archive does not exist until this graph builds it.
        assert "artifact" not in node_of(plan, "local-sdist")
        assert "artifact" not in node_of(plan, "sdist-build")

    def test_neither_node_takes_the_name_of_the_project(self, plan: Plan) -> None:
        # **A node name is the name of a store path.** The project may also be
        # the name of the environment, which is a node too, so both of these
        # carry a suffix.
        names = [node["name"] for node in plan.nodes]
        assert names == ["demo-app-sdist", "demo-app-from-sdist"]
        assert "demo-app" not in names

    def test_the_two_nodes_run_the_same_backend(self, plan: Plan) -> None:
        # PEP 517 gives one build environment for every hook of a project, and
        # `build-system.requires` is that environment. So one resolution serves
        # both nodes.
        assert node_of(plan, "local-sdist")["backendPath"] == node_of(plan, "sdist-build")["backendPath"]


class TestTheArchive:
    """What `build_sdist` writes, against the backend of the sealed test."""

    @pytest.fixture
    def archive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # The hook reads `pyproject.toml` and `src` of the working directory,
        # which is what PEP 517 says a backend does.
        monkeypatch.chdir(DEMO_APP)
        name = demoback.build_sdist(str(tmp_path))
        return tmp_path / name

    def test_the_name_is_the_one_that_pep_625_gives(self, archive: Path) -> None:
        assert archive.name == ARCHIVE

    def test_the_archive_holds_one_directory(self, archive: Path) -> None:
        # `scripts/build-sdist.sh` unpacks with `--strip-components=1`, so an
        # archive that held two directories would lose one of them.
        with tarfile.open(archive) as tar:
            roots = {name.split("/")[0] for name in tar.getnames()}
        assert roots == {"demo_app-0.1.0"}

    def test_the_archive_holds_what_a_build_needs(self, archive: Path) -> None:
        # **The archive is what the user gets, and it is not the tree.** A file
        # that a build needs and that the backend leaves out is absent for
        # every user who installs from the index.
        with tarfile.open(archive) as tar:
            members = {name.removeprefix("demo_app-0.1.0/") for name in tar.getnames()}
        assert members == {"pyproject.toml", "PKG-INFO", "src/demoapp/__init__.py"}

    def test_the_archive_is_the_same_bytes_every_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A node of this graph floats, so its output path is a hash of what it
        # built. An archive that carried the time of the day would take a new
        # path on every run, and every node above it would build again.
        monkeypatch.chdir(DEMO_APP)
        first = Path(tmp_path / "a")
        second = Path(tmp_path / "b")
        first.mkdir()
        second.mkdir()
        assert (first / demoback.build_sdist(str(first))).read_bytes() == (
            second / demoback.build_sdist(str(second))
        ).read_bytes()
