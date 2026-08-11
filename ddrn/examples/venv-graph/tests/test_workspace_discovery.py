"""Which directories of a workspace are members, which `ddrn_venv._workspace`
decides.

**A member that `uv.lock` never saw reaches no comparison of
`[package.metadata]`**, because `uv.nix` builds one declaration for each
package that the lock file holds. `workspace.nix` walks the tree instead, and
this module tests what the walker hands over.

**The walk is in Nix and every rule is here.** `workspace.nix` lists the
`pyproject.toml` of each directory and decides nothing, so every case below is
a plain Python test with no evaluator and no store. One test at the end runs
the walk itself, because only the evaluator reads a directory.

`collect_members_only` of `crates/uv-workspace/src/workspace.rs` states each
rule, and `Pattern::new` of `glob-0.3.4/src/lib.rs` states what a glob means.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ddrn_venv import LockError
from ddrn_venv._workspace import ALL, ROOT, matches, members, readable, selection, split

if TYPE_CHECKING:
    from ddrn_venv._workspace import Members

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent

#: A `pyproject.toml` that declares a project and nothing else.
PROJECT = """
[project]
name = "{name}"
version = "0.1.0"
"""


def listing(tmp_path: Path, tree: dict[str, str]) -> dict[str, str]:
    """The shape that `workspace.nix` hands over, written to `tmp_path`.

    `tree` maps the path of a directory, relative to the root, to the text of
    the `pyproject.toml` that the directory holds.
    """
    written: dict[str, str] = {}
    for relative, text in tree.items():
        directory = tmp_path / relative
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "pyproject.toml"
        path.write_text(text, encoding="utf-8")
        written[relative] = str(path)
    return written


def root(name: str | None, member_globs: list[str], exclude: list[str] | None = None) -> str:
    """The `pyproject.toml` of a workspace root."""
    text = "" if name is None else PROJECT.format(name=name)
    text += "\n[tool.uv.workspace]\n"
    text += f"members = {json.dumps(member_globs)}\n"
    if exclude is not None:
        text += f"exclude = {json.dumps(exclude)}\n"
    return text


def names(tmp_path: Path, tree: dict[str, str]) -> set[str] | None:
    found = members(listing(tmp_path, tree))
    return None if found is None else set(found.names)


class TestOneGlob:
    """`matches`, which reads one pattern against one directory."""

    @pytest.mark.parametrize(
        ("pattern", "directory"),
        [
            ("packages/ws-core", "packages/ws-core"),
            ("./packages/ws-core", "packages/ws-core"),
            ("packages/", "packages"),
            ("packages/*", "packages/ws-core"),
            ("packages/pkg-?", "packages/pkg-a"),
            ("packages/[abc]*", "packages/apps"),
            ("packages/[!x]*", "packages/apps"),
            ("packages/[a-c]ore", "packages/bore"),
            # `{a,b}` is not alternation. The token set of `Pattern::new` holds
            # no such token, so both braces are literal characters.
            ("packages/{a,b}", "packages/{a,b}"),
        ],
    )
    def test_it_matches(self, pattern: str, directory: str) -> None:
        assert matches(split(pattern), split(directory))

    @pytest.mark.parametrize(
        ("pattern", "directory"),
        [
            ("packages/*", "packages/ws-core/nested"),
            ("packages/pkg-?", "packages/pkg-bb"),
            ("packages/[!a]*", "packages/apps"),
            ("packages/{a,b}", "packages/a"),
            ("packages/*", "apps/ws-core"),
        ],
    )
    def test_it_does_not_match(self, pattern: str, directory: str) -> None:
        assert not matches(split(pattern), split(directory))


class TestTheRecursiveGlob:
    """`**`, which the `glob` crate calls `AnyRecursiveSequence`."""

    @pytest.mark.parametrize(
        "directory",
        [
            # **A pattern that ends in `**` names its own base directory.**
            # `Pattern::matches_from` takes the empty branch first, and
            # `Paths::next` returns the directory itself when the pattern ends
            # in the recursive token.
            "packages",
            "packages/one",
            "packages/one/two",
            "packages/one/two/three",
        ],
    )
    def test_it_crosses_any_number_of_separators(self, directory: str) -> None:
        assert matches(split("packages/**"), split(directory))

    def test_it_matches_in_the_middle_of_a_pattern(self) -> None:
        assert matches(split("packages/**/src"), split("packages/one/two/src"))
        assert matches(split("packages/**/src"), split("packages/src"))

    def test_it_matches_at_the_start(self) -> None:
        assert matches(split("**/src"), split("one/two/src"))

    def test_it_does_not_reach_past_the_pattern(self) -> None:
        assert not matches(split("packages/**/src"), split("packages/one/src/two"))

    @pytest.mark.parametrize("pattern", ["a/**b", "a/bc**", "a/*****", "a**b"])
    def test_uv_refuses_a_pattern_that_holds_it_inside_a_component(self, pattern: str) -> None:
        """`recursive wildcards must form a single path component`.

        The `glob` crate raises `PatternError` for each of these, and its own
        tests name all four. A pattern that uv refuses is one that this reader
        must not answer.
        """
        assert not readable(split(pattern))

    def test_it_reads_a_workspace_that_uv_itself_refuses(self, tmp_path: Path) -> None:
        """**`packages/**` is a pattern that uv refuses for most trees.**

        `**` names every directory at every depth, so the `src` of a member is
        a member with no `pyproject.toml`, and uv raises
        `Workspace member ... is missing a pyproject.toml (matches:
        packages/**)`. Measured against `projects/workspace` with that one
        edit, and `uv lock --check` named `packages/ws-cli/src`.

        This reader accepts it, because such a directory is not in the listing
        at all. uv cannot write that lock file, so a build of it fails for its
        own reason, and this is no false alarm.
        """
        assert names(
            tmp_path,
            {
                ROOT: root("rec-root", ["packages/**"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
            },
        ) == {"rec-root", "ws-core"}


class TestWhatTheRulesCount:
    """Each rule of `collect_members_only`, one test each."""

    def test_a_literal_member_path(self, tmp_path: Path) -> None:
        assert names(
            tmp_path,
            {
                ROOT: root("lit-root", ["packages/ws-core", "packages/ws-cli"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "packages/ws-cli": PROJECT.format(name="ws-cli"),
            },
        ) == {"lit-root", "ws-core", "ws-cli"}

    def test_a_star_takes_every_directory_of_one_level(self, tmp_path: Path) -> None:
        assert names(
            tmp_path,
            {
                ROOT: root("star-root", ["packages/*"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "packages/ws-cli": PROJECT.format(name="ws-cli"),
            },
        ) == {"star-root", "ws-core", "ws-cli"}

    def test_a_recursive_glob_takes_every_depth(self, tmp_path: Path) -> None:
        assert names(
            tmp_path,
            {
                ROOT: root("rec-root", ["packages/**"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "packages/deep/ws-cli": PROJECT.format(name="ws-cli"),
            },
        ) == {"rec-root", "ws-core", "ws-cli"}

    def test_the_root_is_a_member_when_it_declares_a_project(self, tmp_path: Path) -> None:
        found = members(listing(tmp_path, {ROOT: root("solo", []), "extra": PROJECT.format(name="extra")}))
        assert found is not None
        assert found.names == ("solo",)
        assert found.root == "solo"

    def test_a_virtual_root_is_no_member_of_itself(self, tmp_path: Path) -> None:
        """A root with no `[project]` is `Workspace::is_non_project` of uv."""
        found = members(
            listing(
                tmp_path,
                {
                    ROOT: root(None, ["packages/*"]),
                    "packages/ws-core": PROJECT.format(name="ws-core"),
                },
            )
        )
        assert found is not None
        assert found.names == ("ws-core",)
        assert found.root is None

    def test_an_excluded_directory_is_no_member(self, tmp_path: Path) -> None:
        """uv builds the same list in `WorkspaceExclusions::new`."""
        assert names(
            tmp_path,
            {
                ROOT: root("ex-root", ["packages/*"], ["packages/skip-me"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "packages/skip-me": PROJECT.format(name="skip-me"),
            },
        ) == {"ex-root", "ws-core"}

    def test_a_directory_that_uv_does_not_manage_is_no_member(self, tmp_path: Path) -> None:
        assert names(
            tmp_path,
            {
                ROOT: root("un-root", ["packages/*"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "packages/tool": PROJECT.format(name="tool") + "\n[tool.uv]\nmanaged = false\n",
            },
        ) == {"un-root", "ws-core"}

    def test_a_directory_that_no_glob_names_is_no_member(self, tmp_path: Path) -> None:
        """The walk lists the whole tree, and the globs decide."""
        assert names(
            tmp_path,
            {
                ROOT: root("abs-root", ["packages/*"]),
                "packages/ws-core": PROJECT.format(name="ws-core"),
                "apps/other": PROJECT.format(name="other"),
            },
        ) == {"abs-root", "ws-core"}

    def test_two_globs_that_name_one_directory_count_it_once(self, tmp_path: Path) -> None:
        found = members(
            listing(
                tmp_path,
                {
                    ROOT: root("dup-root", ["packages/*", "packages/ws-core"]),
                    "packages/ws-core": PROJECT.format(name="ws-core"),
                },
            )
        )
        assert found is not None
        assert found.names == ("ws-core", "dup-root")

    def test_no_workspace_table_gives_the_root_alone(self, tmp_path: Path) -> None:
        # This is every single project. `check_members` clears a list of one
        # member whose root is a project, so it compares an empty set with the
        # empty `[manifest]` that uv wrote.
        assert names(
            tmp_path,
            {ROOT: PROJECT.format(name="solo"), "vendor/thing": PROJECT.format(name="thing")},
        ) == {"solo"}


class TestWhatItRefusesToAnswer:
    """Each case that gives `None`, which turns the comparison off.

    **`None` leaves the gap that this module closes, and a wrong answer would
    stop a build that is correct.**
    """

    def test_a_pattern_that_uv_refuses(self, tmp_path: Path) -> None:
        assert (
            names(
                tmp_path,
                {
                    ROOT: root("bad-root", ["packages/**core"]),
                    "packages/ws-core": PROJECT.format(name="ws-core"),
                },
            )
            is None
        )

    def test_a_project_file_with_no_project_table(self, tmp_path: Path) -> None:
        # uv raises `MissingProject` here. The build that follows fails on its
        # own, and a message about the lock file would name the wrong cause.
        assert (
            names(
                tmp_path,
                {ROOT: root("npt-root", ["packages/*"]), "packages/broken": "[tool.uv]\n"},
            )
            is None
        )

    def test_a_project_file_that_toml_cannot_read(self, tmp_path: Path) -> None:
        assert names(tmp_path, {ROOT: root("bad", ["packages/*"]), "packages/x": "[project"}) is None

    def test_an_empty_listing(self, tmp_path: Path) -> None:
        """`tests/sealed.nix` builds a lock document with no project behind it."""
        assert members(listing(tmp_path, {})) is None


class TestTheWalk:
    """`../workspace.nix`, which lists and decides nothing.

    This is the one case that needs the evaluator, because only the evaluator
    reads a directory that is not in the store.
    """

    @pytest.fixture(scope="class")
    def walked(self) -> dict[str, str]:
        expression = f"""
        import {EXAMPLE / "workspace.nix"} {{
          lib = (import <nixpkgs> {{ }}).lib;
          root = {EXAMPLE / "projects/workspace"};
        }}
        """
        # **This fixture is synchronous, and `subprocess.run` is correct
        # here.** The rule of the repository asks for an async call inside an
        # async function, and a class-scoped fixture of anyio needs an event
        # loop of the same scope. One `nix eval` runs for the whole class.
        result = subprocess.run(  # noqa: S603 -- the argument list is a literal, and `nix` comes from PATH.
            ["nix", "eval", "--impure", "--json", "--expr", expression],  # noqa: S607 -- the example itself calls `nix` this way.
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"nix eval failed:\n{result.stderr}")
        walked: dict[str, str] = json.loads(result.stdout)
        return walked

    def test_it_lists_each_directory_that_holds_a_project_file(self, walked: dict[str, str]) -> None:
        assert sorted(walked) == ["", "packages/ws-cli", "packages/ws-core"]

    def test_each_value_is_a_store_path_of_that_file(self, walked: dict[str, str]) -> None:
        for path in walked.values():
            assert path.startswith("/nix/store/")
            assert path.endswith("-pyproject.toml")

    def test_the_rules_read_that_listing(self, walked: dict[str, str]) -> None:
        """The two halves, joined: the real fixture through the real walk."""
        found = members(walked)
        assert found is not None
        assert set(found.names) == {"workspace-root", "ws-core", "ws-cli"}
        assert found.root == "workspace-root"


def test_a_pattern_splits_the_way_uv_normalises_it() -> None:
    """uv calls `normalize_path` on each glob before it expands it."""
    assert split("./packages//ws-core/") == ["packages", "ws-core"]


class TestTheSelection:
    """Which extras and which groups each member installs.

    **`uv.lock` records none of this**, so `uv sync` takes it on its command
    line and `uv.nix` takes it as two arguments. `selection` gives what
    `uv sync` gives when a caller states nothing.
    """

    DOCUMENT = """
[project]
name = "app"
version = "0.1.0"

[project.optional-dependencies]
docs = ["sphinx"]
test = ["pytest"]

[dependency-groups]
dev = ["ruff"]
ddrn = ["installer"]
"""

    def found(self, tmp_path: Path, text: str | None = None) -> Members:
        answer = members(listing(tmp_path, {ROOT: text or self.DOCUMENT}))
        assert answer is not None
        return answer

    def test_no_flag_takes_the_dev_group_and_no_extra(self, tmp_path: Path) -> None:
        """**`uv sync` with no flag takes the group `dev`, and no extra.**

        `default_dependency_groups` of
        `crates/uv/src/commands/project/mod.rs` gives `["dev"]` when
        `tool.uv.default-groups` says nothing, and `sync.rs` reads
        `DefaultExtras::default()` for the extras, which is empty.
        """
        assert selection(self.found(tmp_path), None, None) == {"app": ["dev"]}

    def test_all_takes_every_extra_and_every_group(self, tmp_path: Path) -> None:
        """`uv sync --all-extras --all-groups`."""
        assert selection(self.found(tmp_path), ALL, ALL) == {"app": ["ddrn", "dev", "docs", "test"]}

    def test_a_list_names_them(self, tmp_path: Path) -> None:
        assert selection(self.found(tmp_path), ["docs"], ["ddrn"]) == {"app": ["ddrn", "docs"]}

    def test_default_groups_replaces_the_dev_default(self, tmp_path: Path) -> None:
        text = self.DOCUMENT + '\n[tool.uv]\ndefault-groups = ["ddrn"]\n'
        assert selection(self.found(tmp_path, text), None, None) == {"app": ["ddrn"]}

    def test_default_groups_takes_the_literal_all(self, tmp_path: Path) -> None:
        """`DefaultGroups::All` of `crates/uv-normalize/src/group_name.rs`."""
        text = self.DOCUMENT + '\n[tool.uv]\ndefault-groups = "all"\n'
        assert selection(self.found(tmp_path, text), None, None) == {"app": ["ddrn", "dev"]}

    def test_a_default_group_that_is_not_declared_is_refused(self, tmp_path: Path) -> None:
        """uv gives `MissingDefaultGroup` for the same file."""
        text = self.DOCUMENT + '\n[tool.uv]\ndefault-groups = ["absent"]\n'
        with pytest.raises(LockError, match=re.escape("declares no ['absent']")):
            selection(self.found(tmp_path, text), None, None)

    def test_the_legacy_table_counts_as_the_dev_group(self, tmp_path: Path) -> None:
        """`tool.uv.dev-dependencies` fills the group `dev`, so `all` holds it."""
        text = '[project]\nname = "app"\nversion = "0.1.0"\n\n[tool.uv]\ndev-dependencies = ["ruff"]\n'
        assert selection(self.found(tmp_path, text), None, ALL) == {"app": ["dev"]}

    def test_a_project_with_no_group_at_all_still_asks_for_dev(self, tmp_path: Path) -> None:
        # A name that is neither an extra nor a group contributes no edge, so
        # this costs nothing. `Package.edges` reads `dev_dependencies.get`.
        text = '[project]\nname = "app"\nversion = "0.1.0"\n'
        assert selection(self.found(tmp_path, text), None, None) == {"app": ["dev"]}

    def test_every_member_takes_the_same_answer(self, tmp_path: Path) -> None:
        answer = members(
            listing(
                tmp_path,
                {
                    ROOT: root("ws-root", ["packages/*"]),
                    "packages/one": PROJECT.format(name="one") + "\n[dependency-groups]\ndev = []\n",
                },
            )
        )
        assert answer is not None
        assert selection(answer, None, None) == {"one": ["dev"], "ws-root": ["dev"]}
