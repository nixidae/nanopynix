"""The packaging decisions of `ddrn_venv`, tested without Nix.

**Every decision of this example is a Python decision.** `packaging` is the
reference implementation of the PEPs, and `ddrn_venv` is the code that asks it
the questions: which artefact satisfies this requirement, does this target
accept that wheel, and what does this archive declare as its build system.

`test_sealed_graph.py` proves the same code through a build. This module
proves it in a second, and it names the case that each rule exists for.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest
from ddrn_venv import (
    FALLBACK_BACKEND,
    Artifact,
    Target,
    build_system,
    choose,
    is_pyproject,
    read_pyproject,
    target_from_environ,
    target_platforms,
    target_tags,
    wheel_rank,
)
from packaging.requirements import Requirement
from packaging.tags import Tag

if TYPE_CHECKING:
    from pathlib import Path

#: The target that the example plans for: CPython 3.13 on x86_64 Linux.
PYTHON_VERSION = "3.13"
PLATFORM = "linux_x86_64"

#: Every key of PEP 508. A short set would let `packaging` read the
#: interpreter that runs this test, and `PEP508_KEYS` exists to stop that.
MARKERS = {
    "implementation_name": "cpython",
    "implementation_version": f"{PYTHON_VERSION}.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": f"{PYTHON_VERSION}.0",
    "python_version": PYTHON_VERSION,
    "sys_platform": "linux",
    "extra": "",
}

#: The document that `default.nix` puts in `DDRN_TARGET`, as JSON.
TARGET_DOCUMENT = {"markers": MARKERS, "pythonVersion": PYTHON_VERSION, "platform": PLATFORM}

#: One row of `DDRN_INPUTS`, as `uv.nix` writes it for a wheel to fetch.
ROW = {
    "package": "demo",
    "version": "1.0",
    "filename": "demo-1.0-py3-none-any.whl",
    "kind": "wheel",
    "drv": "/nix/store/aaa-demo.drv",
    "out": "/nix/store/bbb-demo",
}

PYPROJECT = b"""
[build-system]
requires = ["demo-backend >=1.0"]
build-backend = "demoback"
"""


def wheel(package: str, version: str, tag: str = "py3-none-any", **rest: str) -> Artifact:
    """One row of a lock file, as a wheel of `tag`."""
    filename = f"{package.replace('-', '_')}-{version}-{tag}.whl"
    return Artifact(package=package, version=version, filename=filename, kind="wheel", **rest)


def sdist(package: str, version: str, **rest: str) -> Artifact:
    """One row of a lock file, as a source distribution."""
    filename = f"{package.replace('-', '_')}-{version}.tar.gz"
    return Artifact(package=package, version=version, filename=filename, kind="sdist", **rest)


@pytest.fixture
def target() -> Target:
    return Target(
        tags=tuple(target_tags(PYTHON_VERSION, PLATFORM)),
        markers=MARKERS,
        python_version=PYTHON_VERSION,
        platform=PLATFORM,
    )


class TestTags:
    """PEP 425 and PEP 600, which say what this target accepts."""

    def test_a_pure_wheel_fits(self, target: Target) -> None:
        # **The rank is a position in the list that `packaging` orders**, so a
        # test asserts the order and not the number. A number here would
        # change whenever `packaging` adds a tag, and it would say nothing.
        assert wheel_rank(wheel("demo", "1.0"), target.tags) is not None

    def test_a_wheel_of_this_interpreter_outranks_a_pure_one(self, target: Target) -> None:
        binary = wheel_rank(wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64"), target.tags)
        pure = wheel_rank(wheel("demo", "1.0"), target.tags)
        assert binary is not None
        assert pure is not None
        assert binary < pure

    def test_a_newer_manylinux_outranks_an_older_one(self, target: Target) -> None:
        # PEP 600: both run here, and the one that names the newer glibc is
        # the closer fit.
        newer = wheel_rank(wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64"), target.tags)
        older = wheel_rank(wheel("demo", "1.0", "cp313-cp313-manylinux_2_17_x86_64"), target.tags)
        assert newer is not None
        assert older is not None
        assert newer < older

    def test_a_wheel_of_another_interpreter_does_not_fit(self, target: Target) -> None:
        assert wheel_rank(wheel("demo", "1.0", "cp314-cp314-linux_x86_64"), target.tags) is None

    def test_a_wheel_of_another_platform_does_not_fit(self, target: Target) -> None:
        assert wheel_rank(wheel("demo", "1.0", "cp313-cp313-macosx_10_13_x86_64"), target.tags) is None

    def test_a_manylinux_older_than_the_floor_does_not_fit(self, target: Target) -> None:
        # PEP 600 names the oldest glibc that the wheel runs against, and
        # `GLIBC_MINORS` starts at 2.17, which is `manylinux2014`.
        assert wheel_rank(wheel("demo", "1.0", "cp313-cp313-manylinux_2_5_x86_64"), target.tags) is None

    def test_abi3_fits(self, target: Target) -> None:
        assert wheel_rank(wheel("demo", "1.0", "cp313-abi3-manylinux_2_28_x86_64"), target.tags) is not None

    def test_abi3_of_an_older_cpython_fits(self, target: Target) -> None:
        # **PEP 384: a stable-ABI wheel runs on every CPython at or above the
        # version that its tag names.** `pyzmq` 27.1.0 ships one `cp312-abi3`
        # wheel for Linux and no `cp313` or `cp314` wheel, so a planner that
        # took the tag literally found no wheel and built the source
        # distribution instead.
        assert wheel_rank(wheel("demo", "1.0", "cp312-abi3-manylinux_2_28_x86_64"), target.tags) is not None

    def test_abi3_of_a_newer_cpython_does_not_fit(self, target: Target) -> None:
        # The rule runs one way only. A wheel for the 3.14 stable ABI may use
        # what 3.14 added, so it does not run on 3.13.
        assert wheel_rank(wheel("demo", "1.0", "cp314-abi3-manylinux_2_28_x86_64"), target.tags) is None

    def test_the_three_grades_rank_in_order(self, target: Target) -> None:
        exact = wheel_rank(wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64"), target.tags)
        stable = wheel_rank(wheel("demo", "1.0", "cp312-abi3-manylinux_2_28_x86_64"), target.tags)
        pure = wheel_rank(wheel("demo", "1.0"), target.tags)
        assert exact is not None
        assert stable is not None
        assert pure is not None
        assert exact < stable < pure

    def test_the_set_holds_each_form_that_an_index_serves(self) -> None:
        # **This is the whole reason to call `packaging` rather than write the
        # rule again.** `cpython_tags` takes the target as an argument, so the
        # planner states what it plans for and `packaging` answers. A tag set
        # written by hand missed the third of these four, which is the one that
        # `pyzmq` ships.
        accepted = set(target_tags(PYTHON_VERSION, PLATFORM))
        assert Tag("cp313", "cp313", "manylinux_2_28_x86_64") in accepted
        assert Tag("cp313", "abi3", "manylinux_2_28_x86_64") in accepted
        assert Tag("cp312", "abi3", "manylinux_2_28_x86_64") in accepted
        assert Tag("py3", "none", "any") in accepted

    def test_a_linux_target_takes_every_manylinux_at_or_below_it(self) -> None:
        # PEP 600: a `manylinux_2_Y` wheel runs against every glibc at or above
        # 2.Y, so each version at or below the target fits and the newest one
        # is the closest fit. `manylinux2014` is the legacy alias of 2.17.
        platforms = target_platforms("linux_x86_64")
        assert platforms[-1] == "linux_x86_64"
        assert "manylinux2014_x86_64" in platforms
        assert platforms.index("manylinux_2_28_x86_64") < platforms.index("manylinux_2_17_x86_64")

    @pytest.mark.parametrize("platform", ["macosx_11_0_arm64", "win_amd64", "any"])
    def test_a_platform_that_is_not_linux_travels_alone(self, platform: str) -> None:
        # **`manylinux` is a Linux rule**, and this function states the target
        # rather than describe the machine that runs it. A platform with no
        # such rule needs no expansion.
        assert target_platforms(platform) == [platform]


class TestChoose:
    """Which one artefact of the lock file answers a requirement."""

    def test_a_name_is_normalised_before_it_is_matched(self, target: Target) -> None:
        # PEP 503: a project asks for `flit_core`, and the index calls it
        # `flit-core`. This is the case that made `idna` build.
        chosen = choose([wheel("flit-core", "3.12.0")], Requirement("flit_core >=3.11,<5"), target)
        assert chosen is not None
        assert chosen.package == "flit-core"

    def test_a_version_outside_the_specifier_is_refused(self, target: Target) -> None:
        assert choose([wheel("demo", "5.0")], Requirement("demo <2"), target) is None

    def test_a_wheel_wins_over_a_source_distribution(self, target: Target) -> None:
        chosen = choose([sdist("demo", "1.0"), wheel("demo", "1.0")], Requirement("demo"), target)
        assert chosen is not None
        assert chosen.kind == "wheel"

    def test_a_source_distribution_is_the_answer_when_no_wheel_fits(self, target: Target) -> None:
        # The path that motivates the whole lab: no wheel of this target, so
        # the node becomes a planner that reads the archive.
        artifacts = [sdist("demo", "1.0"), wheel("demo", "1.0", "cp314-cp314-linux_x86_64")]
        chosen = choose(artifacts, Requirement("demo"), target)
        assert chosen is not None
        assert chosen.kind == "sdist"

    def test_the_better_tag_wins_among_wheels(self, target: Target) -> None:
        artifacts = [wheel("demo", "1.0"), wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64")]
        chosen = choose(artifacts, Requirement("demo"), target)
        assert chosen is not None
        assert chosen.is_platform_wheel

    def test_the_build_tag_separates_two_wheels_of_one_tag(self, target: Target) -> None:
        # **Two wheels of one distribution may carry the same tags.** PEP 427
        # gives the build tag to separate them, and it "acts as a tie-breaker
        # if two wheel file names are the same in all other respects". uv
        # breaks the tie with the same value, in `Package::find_best_wheel`.
        # The order of the list must not decide, so this asserts both orders.
        first = wheel("demo", "1.0", "py3-none-any")
        second = wheel("demo", "1.0", "2-py3-none-any")
        for artifacts in ([first, second], [second, first]):
            chosen = choose(artifacts, Requirement("demo"), target)
            assert chosen is not None
            assert chosen.filename == "demo-1.0-2-py3-none-any.whl"

    def test_a_closer_tag_beats_a_higher_build_tag(self, target: Target) -> None:
        # The build tag is the second step and never the first. uv looks at it
        # only when the tag priority of the two wheels is equal.
        artifacts = [
            wheel("demo", "1.0", "9-py3-none-any"),
            wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64"),
        ]
        chosen = choose(artifacts, Requirement("demo"), target)
        assert chosen is not None
        assert chosen.is_platform_wheel

    def test_a_marker_that_excludes_this_target_is_refused(self, target: Target) -> None:
        artifacts = [wheel("colorama", "0.4.6", marker="sys_platform == 'win32'")]
        assert choose(artifacts, Requirement("colorama"), target) is None

    def test_a_marker_that_holds_is_accepted(self, target: Target) -> None:
        artifacts = [wheel("demo", "1.0", marker="sys_platform == 'linux'")]
        assert choose(artifacts, Requirement("demo"), target) is not None

    def test_a_name_that_the_lock_does_not_hold_gives_nothing(self, target: Target) -> None:
        assert choose([wheel("demo", "1.0")], Requirement("absent"), target) is None

    def test_no_binary_sends_a_named_package_to_its_source_distribution(self, target: Target) -> None:
        # This is `tool.uv.no-binary-package`, and it is the one setting that
        # reverses the rule above. **uv keeps it in `pyproject.toml` and writes
        # it into no lock file**, so `uv.nix` reads it where uv put it.
        artifacts = [sdist("demo", "1.0"), wheel("demo", "1.0")]
        chosen = choose(artifacts, Requirement("demo"), target, no_binary={"demo"})
        assert chosen is not None
        assert chosen.kind == "sdist"

    def test_no_binary_names_one_package_and_not_another(self, target: Target) -> None:
        artifacts = [sdist("other", "1.0"), wheel("other", "1.0")]
        chosen = choose(artifacts, Requirement("other"), target, no_binary={"demo"})
        assert chosen is not None
        assert chosen.kind == "wheel"

    def test_no_binary_with_no_source_distribution_gives_nothing(self, target: Target) -> None:
        # A package that ships wheels alone cannot answer the setting, and the
        # caller learns that from an empty answer rather than a wrong one.
        assert choose([wheel("demo", "1.0")], Requirement("demo"), target, no_binary={"demo"}) is None

    def test_the_name_of_no_binary_is_normalised_too(self, target: Target) -> None:
        # `choose` compares the canonical name, so a setting that writes
        # `demo_pkg` reaches the package that the index calls `demo-pkg`.
        artifacts = [sdist("demo-pkg", "1.0"), wheel("demo-pkg", "1.0")]
        chosen = choose(artifacts, Requirement("demo-pkg"), target, no_binary={"demo-pkg"})
        assert chosen is not None
        assert chosen.kind == "sdist"


class TestTheTargetComesFromTheNixHalf:
    """`DDRN_TARGET` carries the whole target, and a planner reads it once.

    **A planner plans for a machine that it is not running on.** Every value
    below therefore travels, and none of it is read from the interpreter that
    runs the planner. `PEP508_KEYS` is what makes a short document an error
    rather than a silent fall back to this machine.
    """

    def test_the_document_becomes_the_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DDRN_TARGET", json.dumps(TARGET_DOCUMENT))
        read = target_from_environ()
        assert read.python_version == PYTHON_VERSION
        assert read.platform == PLATFORM
        assert read.tags == tuple(target_tags(PYTHON_VERSION, PLATFORM))

    def test_the_extra_is_empty_whatever_the_document_says(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An edge of an optional dependency carries `extra == '...'`, and the
        # lock file already resolved it. An empty value makes each such marker
        # false, which is what a resolved edge needs.
        document = {**TARGET_DOCUMENT, "markers": {**MARKERS, "extra": "docs"}}
        monkeypatch.setenv("DDRN_TARGET", json.dumps(document))
        assert target_from_environ().markers["extra"] == ""

    def test_a_document_that_leaves_out_a_pep_508_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        markers = {name: value for name, value in MARKERS.items() if name != "sys_platform"}
        monkeypatch.setenv("DDRN_TARGET", json.dumps({**TARGET_DOCUMENT, "markers": markers}))
        with pytest.raises(RuntimeError, match="sys_platform"):
            target_from_environ()

    def test_an_unset_variable_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DDRN_TARGET", raising=False)
        with pytest.raises(RuntimeError, match="DDRN_TARGET"):
            target_from_environ()


class TestArtifact:
    """What travels from the planner to `graph.nix`."""

    def test_a_pure_wheel_is_not_a_platform_wheel(self) -> None:
        assert not wheel("demo", "1.0").is_platform_wheel

    def test_a_tagged_wheel_is_a_platform_wheel(self) -> None:
        assert wheel("demo", "1.0", "cp313-cp313-manylinux_2_28_x86_64").is_platform_wheel

    def test_a_source_distribution_is_not_a_platform_wheel(self) -> None:
        # `parse_wheel_filename` would raise on this name, so the guard runs
        # before it.
        assert not sdist("demo", "1.0").is_platform_wheel

    def test_a_present_file_travels_as_a_path_and_not_as_a_pair(self) -> None:
        # The planner of a source distribution receives its own archive as an
        # input of its derivation, so there is nothing left to build.
        plan = sdist("demo", "1.0", path="/nix/store/aaa-demo-1.0.tar.gz").as_plan()
        assert plan["path"] == "/nix/store/aaa-demo-1.0.tar.gz"
        assert "drv" not in plan

    def test_an_artefact_to_build_travels_as_a_pair(self) -> None:
        plan = wheel("demo", "1.0", drv="/nix/store/aaa-demo.drv", out="/nix/store/bbb-demo").as_plan()
        assert plan["drv"] == "/nix/store/aaa-demo.drv"
        assert plan["out"] == "/nix/store/bbb-demo"

    def test_a_planner_travels_beside_the_artefact(self) -> None:
        plan = sdist("demo", "1.0", planner_drv="/nix/store/aaa-demo.drv", planner_out="/nix/store/bbb").as_plan()
        assert plan["plannerDrv"] == "/nix/store/aaa-demo.drv"
        assert plan["plannerOut"] == "/nix/store/bbb"

    def test_a_subdirectory_travels_and_an_absent_one_does_not(self) -> None:
        # `uv.lock` writes a subdirectory for a URL source, so one archive of
        # the index may hold more than one project.
        assert "subdirectory" not in sdist("demo", "1.0").as_plan()
        assert sdist("demo", "1.0", subdirectory="packages/demo").as_plan()["subdirectory"] == "packages/demo"


class TestTheRowThatTheNixHalfWrote:
    """`Artifact.from_json` reads one row of `DDRN_INPUTS`.

    **This is the boundary between the two halves.** `uv.nix` walks the lock
    file and writes one row for each artefact, and the planner reads that row
    here. A key that this method drops is a decision of the Nix half that never
    reaches the plan, so each key is named rather than copied in bulk.

    The Nix half writes `plannerDrv` and `plannerOut` in camel case, and this
    method is where those two names become `planner_drv` and `planner_out`.
    """

    def test_the_four_required_keys_are_read(self) -> None:
        read = Artifact.from_json({key: ROW[key] for key in ("package", "version", "filename", "kind")})
        assert (read.package, read.version, read.kind) == ("demo", "1.0", "wheel")
        assert read.drv == ""
        assert read.marker is None

    def test_every_optional_key_is_read(self) -> None:
        row = {
            **ROW,
            "path": "/nix/store/ccc-demo.tar.gz",
            "marker": "sys_platform == 'linux'",
            "subdirectory": "packages/demo",
            "plannerDrv": "/nix/store/ddd-planner.drv",
            "plannerOut": "/nix/store/eee-planner",
        }
        read = Artifact.from_json(row)
        assert read.path == "/nix/store/ccc-demo.tar.gz"
        assert read.marker == "sys_platform == 'linux'"
        assert read.subdirectory == "packages/demo"
        assert read.planner_drv == "/nix/store/ddd-planner.drv"
        assert read.planner_out == "/nix/store/eee-planner"

    def test_a_row_becomes_a_plan_and_keeps_what_graph_nix_needs(self) -> None:
        # The round trip is the contract: what `uv.nix` wrote reaches
        # `graph.nix` again, under the names that `graph.nix` reads.
        plan = Artifact.from_json(ROW).as_plan()
        assert plan == {
            "filename": "demo-1.0-py3-none-any.whl",
            "package": "demo",
            "version": "1.0",
            "drv": "/nix/store/aaa-demo.drv",
            "out": "/nix/store/bbb-demo",
        }

    def test_a_key_that_the_row_does_not_carry_is_not_an_error(self) -> None:
        # `uv.nix` writes `marker` for an artefact of a fork alone, so most
        # rows carry neither it nor `subdirectory`.
        read = Artifact.from_json(ROW)
        assert (read.subdirectory, read.planner_drv, read.planner_out, read.path) == ("", "", "", "")

    def test_the_node_name_is_the_package_and_the_version(self) -> None:
        # `graph.nix` keys a node by this name, and `test_plan_names.py` holds
        # what happens when two different nodes claim one of them.
        assert Artifact.from_json(ROW).node_name == "demo-1.0"


class TestBuildSystem:
    """PEP 517, which says what builds a project."""

    def test_a_declared_backend_is_the_answer(self) -> None:
        table = {"build-system": {"requires": ["demo-backend >=1.0"], "build-backend": "demoback"}}
        backend, requirements = build_system(table)
        assert backend == "demoback"
        assert [str(item) for item in requirements] == ["demo-backend>=1.0"]

    def test_a_project_with_no_table_gets_the_fallback(self) -> None:
        backend, requirements = build_system({})
        assert backend == FALLBACK_BACKEND
        assert requirements == []

    def test_a_table_with_requires_and_no_backend_gets_the_fallback(self) -> None:
        backend, requirements = build_system({"build-system": {"requires": ["setuptools"]}})
        assert backend == FALLBACK_BACKEND
        assert len(requirements) == 1


class TestReadPyproject:
    """The one file that no evaluation holds, read out of the archive."""

    @pytest.mark.parametrize(
        "name",
        [
            "demo_tool-3.0.0/pyproject.toml",
            "./demo_tool-3.0.0/pyproject.toml",
        ],
    )
    def test_the_two_spellings_of_the_same_member(self, name: str) -> None:
        # `tar` accepts the second one, and a count of the separators would
        # answer the first one only.
        assert is_pyproject(name)

    @pytest.mark.parametrize(
        "name",
        [
            "pyproject.toml",
            "demo_tool-3.0.0/src/demotool/pyproject.toml",
            "demo_tool-3.0.0/PKG-INFO",
        ],
    )
    def test_what_is_not_the_declaration(self, name: str) -> None:
        assert not is_pyproject(name)

    def test_a_tar_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "demo_tool-3.0.0.tar.gz"
        archive.write_bytes(gzip.compress(tar_bytes("demo_tool-3.0.0/pyproject.toml")))
        assert build_system(read_pyproject(archive))[0] == "demoback"

    def test_a_tar_archive_with_a_leading_dot(self, tmp_path: Path) -> None:
        archive = tmp_path / "demo_tool-3.0.0.tar.gz"
        archive.write_bytes(gzip.compress(tar_bytes("./demo_tool-3.0.0/pyproject.toml")))
        assert build_system(read_pyproject(archive))[0] == "demoback"

    def test_a_zip_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "demo_tool-3.0.0.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("demo_tool-3.0.0/pyproject.toml", PYPROJECT)
        assert build_system(read_pyproject(archive))[0] == "demoback"

    def test_an_archive_with_no_declaration_gets_the_fallback(self, tmp_path: Path) -> None:
        # PEP 517 gives the fallback for exactly this, so it is not an error.
        archive = tmp_path / "demo_tool-3.0.0.tar.gz"
        archive.write_bytes(gzip.compress(tar_bytes("demo_tool-3.0.0/setup.py")))
        assert read_pyproject(archive) == {}
        assert build_system(read_pyproject(archive))[0] == FALLBACK_BACKEND


def tar_bytes(member: str) -> bytes:
    """One tar archive that holds `member`, with `PYPROJECT` as its content."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(PYPROJECT)
        archive.addfile(info, io.BytesIO(PYPROJECT))
    return buffer.getvalue()
