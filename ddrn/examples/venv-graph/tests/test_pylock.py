"""`../pylock.nix`, over a PEP 751 export of each project of `../projects`.

**This module measures a spike, and it does not gate a supported reader.**
`../pylock.nix` says what the spike is for: how much of the Nix half survives a
move from `uv.lock` to the standard lock format. These tests keep the answer
honest, so that the numbers in `ddrn/PACKAGING-FACTS.md` stay true when
something moves.

**The export is offline, and so is every test here.** `uv export --frozen`
reads `uv.lock` and resolves nothing, and `builtins.fromTOML` reads the result.
Nothing builds and nothing reaches an index, so this module carries no
`network` marker.

`--python` is necessary, because uv finds no interpreter and offers to
download one that NixOS cannot start. `ddrn/PACKAGING-FACTS.md` records that.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from toolchain import EXAMPLE

if TYPE_CHECKING:
    from toolchain import Toolchain

pytestmark = pytest.mark.nix

PYLOCK = EXAMPLE / "pylock.nix"
PROJECTS = EXAMPLE / "projects"

#: Every project of the example. Each one exports, and each export reads.
NAMES = [
    "pure-app",
    "packaged-app",
    "workspace",
    "binary-app",
    "git-app",
    "vendored-app",
    "local-index",
]

#: The feature that `builtins.fromTOML` needs for a PEP 751 file. That format
#: writes `upload-time` as a TOML datetime, and Nix refuses one without this.
TIMESTAMPS = "parse-toml-timestamps"

#: One lock file, as `tomllib` reads it. `uv.lock` writes `package` and PEP 751
#: writes `packages`, and every value under either key is a table.
type LockDocument = dict[str, list[dict[str, Any]]]


def menu_of(document: LockDocument) -> int:
    """How many artefacts a lock document offers.

    A wheel row, a source distribution and an archive are each one artefact.
    `uv.lock` writes the first two, and PEP 751 writes all three.
    """
    packages = document["package"] if "package" in document else document["packages"]
    total = 0
    for package in packages:
        total += len(package.get("wheels", []))
        total += 1 if "sdist" in package else 0
        total += 1 if "archive" in package else 0
    return total


async def export(project: str, into: Path) -> Path:
    """Copy `project`, write a `pylock.toml` beside its `pyproject.toml`.

    **The export goes into the project, and PEP 751 is the reason.** Every
    relative path of the file resolves against the directory that holds it, so
    an export somewhere else names a directory that is not there.
    """
    tree = into / project
    shutil.copytree(PROJECTS / project, tree)
    result = await anyio.run_process(
        [
            "uv",
            "export",
            "--format",
            "pylock.toml",
            "--frozen",
            "--all-packages",
            "--all-groups",
            "--python",
            sys.executable,
            "-o",
            "pylock.toml",
        ],
        cwd=tree,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace")
        raise AssertionError(f"uv refused to export {project}:\n{message}")
    return tree


async def read(toolchain: Toolchain, tree: Path, expression: str) -> str:
    """Evaluate one expression of `../pylock.nix` over `tree`.

    `expression` names `r`, which is the result of the reader, and it must give
    a string: :meth:`Toolchain.evaluate` reads one.

    The evaluator of the machine serves this, because nothing here builds and
    the private store would take a copy of every source for no reason.
    """
    source = f'(let r = import {PYLOCK} {{ project = "{tree}"; }}; in {expression})'
    return await toolchain.evaluate(source, evaluator=toolchain.host_evaluator)


async def count(toolchain: Toolchain, tree: Path, expression: str) -> int:
    """How many entries `expression` holds, as a number."""
    return int(await read(toolchain, tree, f"toString (builtins.length ({expression}))"))


@pytest.fixture(scope="session")
async def timestamps(toolchain: Toolchain) -> None:
    """Refuse this module when the evaluator cannot read a TOML datetime.

    **This asks the evaluator, and it does not read a list of features.** A
    name in `toolchain.FEATURES` says what the session requested, and an older
    Nix that does not know the name gives the same refusal as a Nix that never
    got the request. The message names the feature, because a reader who meets
    a failure here would otherwise look for a defect of `../pylock.nix`.
    """
    probe = '(builtins.toJSON (builtins.fromTOML "t = 1979-05-27T07:32:00Z"))'
    try:
        await toolchain.evaluate(probe, evaluator=toolchain.host_evaluator)
    # The probe reads one TOML datetime and nothing else, so every refusal of
    # it has the same cause, and the message of the refusal goes to the reader.
    except Exception as refusal:
        pytest.skip(f"{TIMESTAMPS} does not read a TOML datetime here: {refusal}")


class TestEveryProjectReads:
    """The seven projects, through the standard format."""

    @pytest.mark.parametrize("name", NAMES)
    async def test_the_menu_is_the_menu_that_uv_lock_gives(
        self, toolchain: Toolchain, timestamps: None, name: str, tmp_path: Path
    ) -> None:
        # **The two menus are equal, and that is the finding.** An export
        # resolves which packages the target installs, and it keeps every
        # wheel of each one. So the planner still chooses an artefact by the
        # PEP 425 tags of the target, and no choice moved to the machine that
        # exported.
        tree = await export(name, tmp_path)
        expected = menu_of(tomllib.loads((PROJECTS / name / "uv.lock").read_text(encoding="utf-8")))
        assert await count(toolchain, tree, "r.artifacts") == expected

    @pytest.mark.parametrize("name", NAMES)
    async def test_the_export_drops_the_virtual_root_and_nothing_else(
        self, toolchain: Toolchain, timestamps: None, name: str, tmp_path: Path
    ) -> None:
        # A virtual package takes part in the resolution and installs nothing,
        # so an export of what to install leaves it out. Every other name of
        # the lock file survives.
        tree = await export(name, tmp_path)
        document = tomllib.loads((PROJECTS / name / "uv.lock").read_text(encoding="utf-8"))
        locked = {package["name"] for package in document["package"]}
        virtual = {
            package["name"] for package in document["package"] if package.get("source", {}).get("virtual") is not None
        }
        names = 'builtins.concatStringsSep " " r.names'
        exported = set((await read(toolchain, tree, names)).split())
        assert exported == locked - virtual


class TestTheSourceKindsThatUvNixParses:
    """The four shapes that `../uv.nix` reads out of a url or a registry."""

    async def test_a_git_source_arrives_as_three_fields(
        self, toolchain: Toolchain, timestamps: None, tmp_path: Path
    ) -> None:
        # **This is what `../uv-git.nix` exists for.** uv writes one url,
        # `git+https://host/repo?subdirectory=x#<rev>`, and that file parses
        # it. PEP 751 writes the url, the commit and the subdirectory apart,
        # so the reader parses nothing.
        tree = await export("git-app", tmp_path)
        assert await read(toolchain, tree, "builtins.head (builtins.attrNames r.editables)") == "flit-core"
        assert await read(toolchain, tree, 'r.editables."flit-core".subdirectory') == "flit_core"

    async def test_a_member_of_a_workspace_is_an_editable(
        self, toolchain: Toolchain, timestamps: None, tmp_path: Path
    ) -> None:
        tree = await export("workspace", tmp_path)
        assert await read(toolchain, tree, 'r.editables."ws-cli".mode') == "editable"
        assert await read(toolchain, tree, 'r.editables."ws-cli".root') == "$DDRN_EDITABLE_ROOT/packages/ws-cli"

    async def test_a_vendored_archive_and_a_directory_registry(
        self, toolchain: Toolchain, timestamps: None, tmp_path: Path
    ) -> None:
        # `vendored-app` holds a tarball of the project, which PEP 751 writes
        # as `archive`. `local-index` holds a registry that is a directory,
        # which it writes as an absolute `file://` url. Both reach the store as
        # a path, and neither is fetched.
        for name in ("vendored-app", "local-index"):
            tree = await export(name, tmp_path / name)
            paths = await count(toolchain, tree, "builtins.filter (a: a ? path) r.artifacts")
            assert paths >= 1, name


class TestWhatTheReaderRefuses:
    """The one assertion that gates the whole file."""

    async def test_a_lock_version_that_this_reader_does_not_accept(
        self, toolchain: Toolchain, timestamps: None, tmp_path: Path
    ) -> None:
        tree = await export("pure-app", tmp_path)
        lock = anyio.Path(tree / "pylock.toml")
        await lock.write_text(
            (await lock.read_text(encoding="utf-8")).replace('lock-version = "1.0"', 'lock-version = "2.0"'),
            encoding="utf-8",
        )
        source = f'(import {PYLOCK} {{ project = "{tree}"; }}).artifacts'
        refusal = await toolchain.evaluate_failing(source)
        assert "and this reader accepts" in refusal
