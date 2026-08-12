"""The check that tells a stale `uv.lock` from a current one.

**Every case here is a rule of uv, and each test names the file of uv that
states the rule.** `ddrn_venv._freshness` compares `[package.metadata]` of the
lock file with the `pyproject.toml` that the workspace holds now. uv makes the
same comparison in `Lock::satisfies`, and it makes it over a list that it
builds out of more than the table of the same name. A test that only checked
the easy case would pass while every real project raised a false alarm.

The lock files come from `./fixtures/locks`, and uv wrote each one. The
`pyproject.toml` of each test is written here, because the point of the check
is a declaration that the lock file does not match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ddrn_venv import Lock, LockError, Pin, check_freshness
from ddrn_venv._freshness import from_declaration, from_metadata, groups_of

LOCKS = Path(__file__).resolve().parent / "fixtures/locks"

#: The workspace of two members, which `test_workspace.py` also reads. Its lock
#: file is the one with a `[manifest] members` table.
WORKSPACE = Path(__file__).resolve().parent.parent / "projects/workspace"


#: What `trivial.lock` resolved from. Its one member declares one dependency.
TRIVIAL = """
[project]
name = "trivial"
version = "0.1.0"
dependencies = ["arpeggio>=2.0.2"]
"""


def write(tmp_path: Path, name: str, text: str) -> dict[str, str]:
    """One declaration, in the shape that `check_freshness` takes."""
    path = tmp_path / f"{name}-pyproject.toml"
    path.write_text(text, encoding="utf-8")
    return {name: str(path)}


class TestAFreshLock:
    """A declaration that the lock file resolved raises nothing."""

    def test_a_matching_declaration_passes(self, tmp_path: Path) -> None:
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", TRIVIAL))

    def test_no_declaration_turns_the_check_off(self) -> None:
        """`tests/sealed.nix` builds a lock document with no project behind it."""
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, {})

    def test_the_specifier_is_compared_after_it_is_normalised(self, tmp_path: Path) -> None:
        """`>= 2.0.2` and `>=2.0.2` are one specifier, and `SpecifierSet` says so."""
        text = TRIVIAL.replace('"arpeggio>=2.0.2"', '"Arpeggio >= 2.0.2"')
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", text))

    def test_a_marker_only_edit_passes(self, tmp_path: Path) -> None:
        """The documented gap, stated as a test.

        uv calls `requires_python.simplify_markers` before it compares, so a
        marker that `requires-python` already implies is not in the lock file.
        This module drops every marker rather than report each such file.
        """
        text = TRIVIAL.replace('"arpeggio>=2.0.2"', "\"arpeggio>=2.0.2 ; python_version >= '3.11'\"")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", text))


class TestAStaleLock:
    """A declaration that the lock file did not resolve raises `LockError`."""

    def test_an_added_dependency(self, tmp_path: Path) -> None:
        text = TRIVIAL.replace('dependencies = ["arpeggio>=2.0.2"]', 'dependencies = ["arpeggio>=2.0.2", "rich"]')
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("uv.lock is stale")) as error:
            check_freshness(lock, write(tmp_path, "trivial", text))
        assert "adds ['rich']" in str(error.value)
        assert "dependencies" in str(error.value)

    def test_a_removed_dependency(self, tmp_path: Path) -> None:
        text = TRIVIAL.replace('dependencies = ["arpeggio>=2.0.2"]', "dependencies = []")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("uv.lock is stale")) as error:
            check_freshness(lock, write(tmp_path, "trivial", text))
        assert "still holds ['arpeggio>=2.0.2']" in str(error.value)

    def test_a_changed_specifier(self, tmp_path: Path) -> None:
        text = TRIVIAL.replace(">=2.0.2", ">=3")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("uv.lock is stale")) as error:
            check_freshness(lock, write(tmp_path, "trivial", text))
        assert "adds ['arpeggio>=3']" in str(error.value)
        assert "still holds ['arpeggio>=2.0.2']" in str(error.value)

    def test_a_member_that_the_lock_does_not_hold(self, tmp_path: Path) -> None:
        """A new member of the workspace is the other way a lock file goes stale."""
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("uv.lock holds no package of that name")):
            check_freshness(lock, write(tmp_path, "newcomer", TRIVIAL))

    def test_the_message_names_the_command_to_run(self, tmp_path: Path) -> None:
        text = TRIVIAL.replace(">=2.0.2", ">=3")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match="Run `uv lock`"):
            check_freshness(lock, write(tmp_path, "trivial", text))


class TestAnExtra:
    """**An extra is a row of `requires-dist`, and not a table of its own.**

    `RequiresDist::from_project_maybe_dynamic` of
    `crates/uv-pypi-types/src/metadata/requires_dist.rs` chains every
    `optional-dependencies` list onto `dependencies`, and it calls
    `with_extra_marker` on each row that came from an extra. So
    `optional-deps.lock` holds one row and no separate table:

        requires-dist = [{ name = "arpeggio", marker = "extra == 'haxx'" }]
        provides-extras = ["haxx"]

    A comparison against `[project].dependencies` alone would call every
    project that has an extra stale.
    """

    DECLARATION = """
[project]
name = "optional-deps"
version = "0.1.0"
dependencies = []

[project.optional-dependencies]
haxx = ["arpeggio"]
"""

    def test_an_extra_raises_no_alarm(self, tmp_path: Path) -> None:
        lock = Lock.read(str(LOCKS / "optional-deps.lock"))
        check_freshness(lock, write(tmp_path, "optional-deps", self.DECLARATION))

    def test_the_lock_file_records_the_extra_in_one_list(self) -> None:
        """The fact that the test above rests on, read from the lock file."""
        lock = Lock.read(str(LOCKS / "optional-deps.lock"))
        package = next(one for one in lock.packages if one.name == "optional-deps")
        assert package.metadata["requires-dist"] == [{"name": "arpeggio", "marker": "extra == 'haxx'"}]
        assert "optional-dependencies" not in package.metadata

    def test_a_removed_extra_is_stale(self, tmp_path: Path) -> None:
        text = self.DECLARATION.replace('haxx = ["arpeggio"]', "haxx = []")
        lock = Lock.read(str(LOCKS / "optional-deps.lock"))
        with pytest.raises(LockError, match=re.escape("still holds ['arpeggio']")):
            check_freshness(lock, write(tmp_path, "optional-deps", text))


class TestADependencyGroup:
    """`[package.metadata.requires-dev]`, and the two things uv does to it."""

    def test_the_legacy_table_is_the_dev_group(self, tmp_path: Path) -> None:
        """**`tool.uv.dev-dependencies` is a group, and its name is `dev`.**

        `FlatDependencyGroups::from_pyproject_toml` of
        `crates/uv-workspace/src/dependency_groups.rs` adds that table to the
        group `DEV_DEPENDENCIES`, which
        `crates/uv-normalize/src/group_name.rs` gives as `"dev"`. A project
        that uses the older spelling has no `[dependency-groups]` at all, so a
        reader of the standard table alone would call it stale.
        """
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[tool.uv]
dev-dependencies = ["coverage[toml]"]
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))

    def test_the_standard_table_gives_the_same_group(self, tmp_path: Path) -> None:
        """The newer spelling of the same group compares against the same lock."""
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["coverage[toml]"]
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))

    def test_a_cycle_is_an_error_and_not_a_hang(self, tmp_path: Path) -> None:
        """uv refuses such a file with `DependencyGroupCycle`, and so does this."""
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = [{ include-group = "other" }]
other = [{ include-group = "dev" }]
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        with pytest.raises(LockError, match="cycle in dependency-groups"):
            check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))

    def test_a_dependency_object_specifier_is_refused(self, tmp_path: Path) -> None:
        """uv gives `DependencyObjectSpecifierNotSupported` for the same entry."""
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = [{ name = "coverage" }]
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        with pytest.raises(LockError, match="include-group entry only"):
            check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))

    def test_an_empty_group_raises_no_alarm(self, tmp_path: Path) -> None:
        """uv drops an empty group from both sides before it compares them.

        `Lock::satisfies` filters each map with
        `self.includes_empty_groups() || !requirements.is_empty()`, so a group
        that states nothing is not a difference.
        """
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["coverage[toml]"]
lint = []
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))

    def test_a_group_that_the_lock_does_not_hold_is_stale(self, tmp_path: Path) -> None:
        text = """
[project]
name = "with-tool-uv-devdeps"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["coverage[toml]"]
lint = ["ruff"]
"""
        lock = Lock.read(str(LOCKS / "with-tool-uv-devdeps.lock"))
        with pytest.raises(LockError, match=re.escape("dependency-groups.lint")):
            check_freshness(lock, write(tmp_path, "with-tool-uv-devdeps", text))


class TestTheGroupsOfADeclaration:
    """`groups_of`, against `FlatDependencyGroups::from_pyproject_toml`.

    **The lock file holds the flat list, and not the table that the user
    wrote.** `resolve_group` of
    `crates/uv-workspace/src/dependency_groups.rs` replaces each
    `include-group` entry with the requirements of the group that it names,
    and it does so before uv writes `requires-dev`. These tests read the
    result directly, because an end-to-end check against a lock file that
    holds one group cannot tell a flattened group from an absent one.
    """

    def test_a_table_with_no_include_group_passes_through(self) -> None:
        assert groups_of({"dependency-groups": {"dev": ["coverage"]}}) == {"dev": ["coverage"]}

    def test_an_include_group_entry_is_resolved(self) -> None:
        document = {"dependency-groups": {"dev": [{"include-group": "leaf"}], "leaf": ["coverage[toml]"]}}
        assert groups_of(document) == {"dev": ["coverage[toml]"], "leaf": ["coverage[toml]"]}

    def test_an_include_group_entry_is_resolved_through_two_levels(self) -> None:
        document = {
            "dependency-groups": {
                "dev": [{"include-group": "middle"}, "ruff"],
                "middle": [{"include-group": "leaf"}],
                "leaf": ["coverage[toml]"],
            }
        }
        assert groups_of(document)["dev"] == ["coverage[toml]", "ruff"]

    def test_the_name_of_a_group_is_normalised(self) -> None:
        """`GroupName` takes `validate_and_normalize_ref`, as a package name does."""
        assert groups_of({"dependency-groups": {"Dev_Tools": ["ruff"]}}) == {"dev-tools": ["ruff"]}

    def test_the_legacy_table_extends_the_dev_group(self) -> None:
        document = {
            "dependency-groups": {"dev": ["ruff"]},
            "tool": {"uv": {"dev-dependencies": ["coverage"]}},
        }
        assert sorted(groups_of(document)["dev"]) == ["coverage", "ruff"]

    def test_the_legacy_table_alone_makes_the_dev_group(self) -> None:
        assert groups_of({"tool": {"uv": {"dev-dependencies": ["coverage"]}}}) == {"dev": ["coverage"]}

    def test_a_document_with_no_group_gives_nothing(self) -> None:
        assert groups_of({"project": {"name": "trivial"}}) == {}

    @pytest.mark.parametrize("entry", [123, 1.5, True, ["coverage"], None])
    def test_an_entry_that_is_neither_a_string_nor_a_table_is_refused(self, entry: object) -> None:
        # `tomllib` reads a file that the user wrote, so `_include_group` tests
        # every step rather than assume the shape. An entry of any other TOML
        # type takes the same route as a Dependency Object Specifier, and uv
        # refuses both.
        with pytest.raises(LockError, match="include-group entry only"):
            groups_of({"dependency-groups": {"dev": [entry]}})


class TestWhatTheCheckSkips:
    """The two cases where a comparison is not possible, and passes."""

    def test_a_lock_with_no_metadata_is_skipped(self, tmp_path: Path) -> None:
        """**An older uv writes no `[package.metadata]`.**

        uv has the same case, and it calls it
        `allow_missing_package_metadata`. A reader cannot tell such a file from
        a member that declares nothing, so it compares neither.
        """
        text = (LOCKS / "trivial.lock").read_text(encoding="utf-8")
        stripped = text.replace('requires-dist = [{ name = "arpeggio", specifier = ">=2.0.2" }]', "")
        stripped = stripped.replace("[package.metadata]", "")
        path = tmp_path / "old.lock"
        path.write_text(stripped, encoding="utf-8")

        lock = Lock.read(str(path))
        assert not next(one for one in lock.packages if one.name == "trivial").metadata
        # The declaration below states nothing, and the lock file resolved one
        # dependency. A comparison would call this stale, and the skip does not.
        check_freshness(lock, write(tmp_path, "trivial", '[project]\nname = "trivial"\nversion = "0.1.0"\n'))

    def test_a_dynamic_dependencies_table_is_skipped(self, tmp_path: Path) -> None:
        """**A dynamic field is not in the file, so no reader can compare it.**

        `RequiresDist::from_project_maybe_dynamic` gives
        `MetadataError::DynamicField("dependencies")` for such a file, and uv
        then builds the package to learn the list. The lock file therefore
        holds requirements that `pyproject.toml` never stated.
        """
        text = """
[project]
name = "trivial"
version = "0.1.0"
dynamic = ["dependencies"]
"""
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", text))

    def test_a_dynamic_version_is_not_skipped(self, tmp_path: Path) -> None:
        """The same function keeps the static path for a dynamic version."""
        text = """
[project]
name = "trivial"
dynamic = ["version"]
dependencies = ["arpeggio>=3"]
"""
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("uv.lock is stale")):
            check_freshness(lock, write(tmp_path, "trivial", text))

    def test_a_listing_that_the_reader_cannot_walk_makes_no_comparison(self, tmp_path: Path) -> None:
        """**A rule of uv that reaches further than this reader ends the check.**

        `ddrn_venv._workspace.members` gives `None` for a listing it cannot
        walk, and a `pyproject.toml` that `tomllib` refuses is one such
        listing. The member comparison then makes no claim, rather than claim
        that the workspace lost a member.
        """
        broken = tmp_path / "pyproject.toml"
        broken.write_text("this is not TOML = = =\n", encoding="utf-8")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", TRIVIAL), listing={".": str(broken)})

    def test_a_listing_with_no_root_makes_no_comparison(self, tmp_path: Path) -> None:
        # The root holds `[tool.uv.workspace]`, so a listing without it says
        # nothing about which directories are members.
        other = tmp_path / "member-pyproject.toml"
        other.write_text('[project]\nname = "other"\nversion = "0.1.0"\n', encoding="utf-8")
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        check_freshness(lock, write(tmp_path, "trivial", TRIVIAL), listing={"member": str(other)})


class TestTheMembersOfAWorkspace:
    """The other half of a stale lock file, which no declaration reaches.

    `uv.nix` builds one declaration for each package that the lock file holds,
    so a member directory that the lock file never saw is in no declaration.
    `workspace.nix` lists every directory of the tree that holds a
    `pyproject.toml`, `ddrn_venv._workspace` decides which of them are members,
    and `check_members` compares that set with `[manifest] members`. uv makes
    the same comparison, and it calls the difference
    `SatisfiesResult::MismatchedMembers`.

    `test_workspace_discovery.py` holds the rules that build the set. These
    tests are the comparison alone.
    """

    def workspace(self, tmp_path: Path, members: dict[str, str], root: str | None = "workspace-root") -> dict[str, str]:
        """A listing, in the shape that `workspace.nix` hands over."""
        text = "" if root is None else f'[project]\nname = "{root}"\nversion = "0.1.0"\n'
        text += '\n[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        tree = {"": text}
        tree.update({f"packages/{name}": f'[project]\nname = "{name}"\nversion = "0.1.0"\n' for name in members})
        written: dict[str, str] = {}
        for relative, body in tree.items():
            directory = tmp_path / relative
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "pyproject.toml"
            path.write_text(body, encoding="utf-8")
            written[relative] = str(path)
        return written

    def test_the_same_members_pass(self, tmp_path: Path) -> None:
        lock = Lock.read(str(WORKSPACE / "uv.lock"))
        check_freshness(lock, {}, self.workspace(tmp_path, {"ws-cli": "", "ws-core": ""}))

    def test_no_listing_turns_the_comparison_off(self) -> None:
        """`tests/sealed.nix` gives no project, so it gives no listing."""
        lock = Lock.read(str(WORKSPACE / "uv.lock"))
        check_freshness(lock, {}, {})

    def test_a_new_member_is_stale(self, tmp_path: Path) -> None:
        lock = Lock.read(str(WORKSPACE / "uv.lock"))
        listing = self.workspace(tmp_path, {"ws-cli": "", "ws-core": "", "ws-extra": ""})
        with pytest.raises(LockError, match=re.escape("the workspace adds ['ws-extra']")):
            check_freshness(lock, {}, listing)

    def test_a_member_that_the_workspace_dropped_is_stale(self, tmp_path: Path) -> None:
        lock = Lock.read(str(WORKSPACE / "uv.lock"))
        with pytest.raises(LockError, match=re.escape("the lock file still holds ['ws-cli']")):
            check_freshness(lock, {}, self.workspace(tmp_path, {"ws-core": ""}))

    def test_the_name_is_normalised_on_both_sides(self, tmp_path: Path) -> None:
        listing = self.workspace(tmp_path, {"WS.CLI": "", "ws_core": ""}, root="Workspace_Root")
        lock = Lock.read(str(WORKSPACE / "uv.lock"))
        check_freshness(lock, {}, listing)

    def test_one_member_that_is_a_project_compares_two_empty_sets(self, tmp_path: Path) -> None:
        """**uv writes no `[manifest] members` for a workspace of one project.**

        `LockTarget::members` of `crates/uv/src/commands/project/lock_target.rs`
        clears the list in that one case. A reader that did not clear the same
        list would call every single project stale.
        """
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        assert lock.members == ()
        check_freshness(lock, {}, self.workspace(tmp_path, {}, root="trivial"))

    def test_one_member_under_a_virtual_root_keeps_the_list(self, tmp_path: Path) -> None:
        """The same rule asks that the root be a project, and this one is not.

        So uv writes the member into `[manifest] members`, and a lock file that
        holds none of it is stale.
        """
        lock = Lock.read(str(LOCKS / "trivial.lock"))
        with pytest.raises(LockError, match=re.escape("the workspace adds ['solo']")):
            check_freshness(lock, {}, self.workspace(tmp_path, {"solo": ""}, root=None))


class TestAPin:
    """The three fields that both sides spell the same way."""

    def test_a_declaration_and_a_lock_row_give_one_pin(self) -> None:
        assert from_declaration("Flask[async] >= 2.0") == from_metadata(
            {"name": "flask", "extras": ["async"], "specifier": ">=2.0"}
        )

    def test_the_extras_of_a_pin_are_sorted(self) -> None:
        assert from_declaration("flask[b,a]").extras == ("a", "b")

    def test_the_text_of_a_pin_is_what_a_message_shows(self) -> None:
        assert Pin(name="flask", extras=("a", "b"), specifier=">=2.0").text == "flask[a,b]>=2.0"

    def test_a_declaration_that_pep_508_does_not_read_is_an_error(self) -> None:
        with pytest.raises(LockError, match="PEP 508 does not read it"):
            from_declaration("this is not a requirement ===")
