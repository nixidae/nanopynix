"""The `uv.lock` reader, against the lock files that uv itself wrote.

**uv2nix resolves the same files, and it holds the answers.** Its
`lib/expected/lock1.resolveDependencies.*.json` gives the resolution of seven
of the fixtures in `./fixtures/locks`. `TestAgreesWithUv2nix` carries those
seven answers, so this reader and uv2nix must give the same result for the
same lock file and the same environment. A reader that only its own author
tested proves nothing.

The environment below is what `pyproject-nix`'s `pep508.mkEnviron
pkgs.python312` builds, which is what uv2nix passed when it recorded those
answers. Two keys are empty on purpose, and `ddrn/PACKAGING-FACTS.md` gives
the reason: no build knows the kernel of the machine that will run the
environment.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ddrn_venv import (
    Lock,
    LockError,
    Resolution,
    build_requirements,
    filter_conflicts,
    resolve_lock,
)
from packaging.requirements import Requirement

LOCKS = Path(__file__).resolve().parent / "fixtures/locks"

#: `pep508.mkEnviron pkgs.python312`, as every key of PEP 508.
ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.12",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.12",
    "python_version": "3.12",
    "sys_platform": "linux",
    "extra": "",
}

#: The answers of uv2nix, from `lib/expected/lock1.resolveDependencies.*.json`.
#: The key is the fixture, and the value is the name of each resolved package
#: with its version.
UV2NIX_ANSWERS = {
    "trivial": {"arpeggio": "arpeggio-2.0.2", "trivial": "trivial-0.1.0"},
    "multi-choice-package": {"multi-choice-package": "multi-choice-package-0.1.0"},
    "optional-deps": {"arpeggio": "arpeggio-2.0.2", "optional-deps": "optional-deps-0.1.0"},
    "with-tool-uv-devdeps": {
        "coverage": "coverage-7.6.1",
        "with-tool-uv-devdeps": "with-tool-uv-devdeps-0.1.0",
    },
    "multi-pythons": {
        "arpeggio": "arpeggio-2.0.2",
        "multi-depends": "multi-depends-0.1.0",
        "multi-pythons": "multi-pythons-0.1.0",
    },
    "kitchen-sink": {
        "a": "a-0.1.0",
        "arpeggio": "arpeggio-2.0.2",
        "attrs": "attrs-23.1.0",
        "b": "b-0.1.0",
        "blinker": "blinker-1.6.2",
        "c-editable": "c-editable-0.1.0",
        "certifi": "certifi-2024.7.4",
        "charset-normalizer": "charset-normalizer-3.3.2",
        "idna": "idna-3.7",
        "pip": "pip-20.3.1",
        "requests": "requests-2.32.3",
        "urllib3": "urllib3-2.2.3",
    },
}


def lock(name: str) -> Lock:
    """One fixture lock file, parsed."""
    return Lock.read(LOCKS / f"{name}.lock")


def deps_all(parsed: Lock) -> dict[str, list[str]]:
    """Every extra and every group of every local package.

    This is `deps.all` of uv2nix (`lib/workspace.nix:274`), and it is the
    selection that uv2nix uses by default. A package that a `directory`
    source pulled in is left out, the same way.
    """
    return {
        package.name: sorted(set(package.optional_dependencies) | set(package.dev_dependencies))
        for package in parsed.packages
        if package.is_local and "directory" not in package.source
    }


def labels(resolution: Resolution) -> dict[str, str]:
    return {package.name: package.label for package in resolution.packages}


class TestAgreesWithUv2nix:
    """The same lock file and the same environment give the same resolution."""

    @pytest.mark.parametrize("name", sorted(UV2NIX_ANSWERS))
    def test_the_resolution_matches(self, name: str) -> None:
        parsed = lock(name)
        resolved = resolve_lock(parsed, ENVIRONMENT, deps_all(parsed))
        assert labels(resolved) == UV2NIX_ANSWERS[name]

    def test_a_fork_that_the_environment_does_not_take_drops_the_package(self) -> None:
        # `multi-choice-package` pins arpeggio 2.0.1 for one kernel and 2.0.2
        # for another. `platform_release` is empty, so neither edge holds and
        # arpeggio is not in the environment at all. uv2nix answers the same,
        # and this is what the two empty keys cost.
        parsed = lock("multi-choice-package")
        assert "arpeggio" not in labels(resolve_lock(parsed, ENVIRONMENT, deps_all(parsed)))

    def test_naming_the_kernel_takes_one_branch_of_the_fork(self) -> None:
        parsed = lock("multi-choice-package")
        for release, version in (("5.10.65", "2.0.1"), ("5.10.104", "2.0.2")):
            environment = {**ENVIRONMENT, "platform_release": release}
            resolved = resolve_lock(parsed, environment, deps_all(parsed))
            assert labels(resolved)["arpeggio"] == f"arpeggio-{version}"


class TestSelection:
    """An extra and a group are followed only when the selection asks.

    **This is where the reader is stricter than uv2nix.** The resolver of
    uv2nix follows every extra and every group of every package
    (`lib/lock1.nix:133`), so its own `testResolveWithOptionals` and
    `testResolveNoOptionals` expect the same answer over the same fixture.
    """

    def test_an_extra_that_nothing_asks_for_is_not_installed(self) -> None:
        resolved = resolve_lock(lock("optional-deps"), ENVIRONMENT, {"optional-deps": []})
        assert sorted(labels(resolved)) == ["optional-deps"]

    def test_an_extra_that_the_selection_names_is_installed(self) -> None:
        resolved = resolve_lock(lock("optional-deps"), ENVIRONMENT, {"optional-deps": ["haxx"]})
        assert labels(resolved)["arpeggio"] == "arpeggio-2.0.2"

    def test_a_group_that_nothing_asks_for_is_not_installed(self) -> None:
        resolved = resolve_lock(lock("with-tool-uv-devdeps"), ENVIRONMENT, {"with-tool-uv-devdeps": []})
        assert sorted(labels(resolved)) == ["with-tool-uv-devdeps"]

    def test_a_group_that_the_selection_names_is_installed(self) -> None:
        resolved = resolve_lock(lock("with-tool-uv-devdeps"), ENVIRONMENT, {"with-tool-uv-devdeps": ["dev"]})
        assert labels(resolved)["coverage"] == "coverage-7.6.1"


class TestConflicts:
    """A `conflicts` declaration, and the branch that the caller takes."""

    @pytest.mark.parametrize(
        ("choice", "extras", "groups"),
        [
            (["extra-a"], ["extra-a"], []),
            (["extra-b"], ["extra-b"], []),
            (["group-c"], [], ["group-c"]),
            # A selection that takes no branch leaves the declaration alone,
            # because the declaration then constrains nothing. uv2nix does the
            # same (`lib/lock1.nix:282`).
            ([], ["extra-a", "extra-b"], ["group-c"]),
        ],
    )
    def test_the_other_branches_are_removed(self, choice: list[str], extras: list[str], groups: list[str]) -> None:
        filtered = filter_conflicts(lock("conflicts"), {"conflicts": choice})
        package = next(item for item in filtered.packages if item.name == "conflicts")
        assert sorted(package.optional_dependencies) == extras
        assert sorted(package.dev_dependencies) == groups
        assert filtered.conflicts == ()

    @pytest.mark.parametrize(
        ("choice", "version"),
        [(["extra-a"], "2.0.0"), (["extra-b"], "2.0.1"), (["group-c"], "2.0.0")],
    )
    def test_each_branch_resolves_to_its_own_version(self, choice: list[str], version: str) -> None:
        resolved = resolve_lock(lock("conflicts"), ENVIRONMENT, {"conflicts": choice})
        assert labels(resolved)["arpeggio"] == f"arpeggio-{version}"

    def test_taking_two_branches_at_once_is_refused(self) -> None:
        # This is what `deps.all` asks for, and it is what a conflict
        # declaration forbids. uv2nix fails here too, and the caller has to
        # pass a narrower selection.
        with pytest.raises(LockError, match="more than one branch"):
            resolve_lock(lock("conflicts"), ENVIRONMENT, {"conflicts": ["extra-a", "extra-b"]})


class TestTheBuildSystemClosure:
    """`build_requirements` pins what a PEP 517 backend needs to import.

    **A backend needs its own dependencies, and PEP 517 says so.** The
    frontend installs `requires` and what those need, so
    `build-system.requires = ["hatchling"]` names one package of five.
    """

    def test_a_project_that_requires_nothing_needs_nothing(self) -> None:
        # PEP 517 gives `setuptools` as the fallback for a project with no
        # `[build-system]`, and `build_system` answers that. A project whose
        # table is present and empty asks for nothing at all, and the closure
        # of nothing is nothing.
        assert build_requirements(lock("trivial"), ENVIRONMENT, []) == []

    def test_a_backend_that_the_lock_file_does_not_hold_names_the_command(self) -> None:
        # **`uv.lock` records no build system**, so a backend reaches it only
        # as an ordinary package. The message gives the command that puts one
        # there, because a reader who meets this has no other way to know.
        with pytest.raises(LockError, match="uv add --group ddrn"):
            build_requirements(lock("trivial"), ENVIRONMENT, [Requirement("hatchling")])


class TestAWorkspaceMemberThatIsNotSelected:
    """A conflict of a member that `select` left out constrains nothing.

    **`select` names the members to install, and a member it leaves out may
    declare conflicting extras of its own.** Those extras reach no
    environment, so they can decide nothing about the members that `select`
    did name. A reader that asked the selection about every package of every
    declaration refused such a workspace, and it named the selection as the
    fault.
    """

    HEAD = (
        'version = 1\nrequires-python = ">=3.12"\n'
        'conflicts = [[ { package = "a", extra = "x" }, { package = "a", extra = "y" } ]]\n'
        '\n[manifest]\nmembers = ["a", "b"]\n'
    )
    BODY = (
        '\n[[package]]\nname = "a"\nversion = "0.1.0"\nsource = { editable = "a" }\n'
        "\n[package.optional-dependencies]\nx = []\ny = []\n"
        '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "b" }\n'
    )

    def workspace(self) -> Lock:
        return Lock.parse(tomllib.loads(self.HEAD + self.BODY))

    def test_selecting_the_other_member_alone_resolves(self) -> None:
        resolved = resolve_lock(self.workspace(), ENVIRONMENT, {"b": []})
        assert sorted(labels(resolved)) == ["b"]

    def test_selecting_both_members_still_takes_one_branch(self) -> None:
        resolved = resolve_lock(self.workspace(), ENVIRONMENT, {"a": ["x"], "b": []})
        assert sorted(labels(resolved)) == ["a", "b"]

    def test_selecting_both_branches_of_the_named_member_is_still_refused(self) -> None:
        # The declaration constrains the member that the selection names, and
        # this change does not reach that.
        with pytest.raises(LockError, match="more than one branch"):
            resolve_lock(self.workspace(), ENVIRONMENT, {"a": ["x", "y"], "b": []})


class TestEnvironment:
    """The three assertions that a lock file makes about its target."""

    def test_a_python_that_is_too_old_is_refused(self) -> None:
        environment = {**ENVIRONMENT, "python_full_version": "3.10.0", "python_version": "3.10"}
        with pytest.raises(LockError, match="needs a Python of"):
            resolve_lock(lock("trivial"), environment, {"trivial": []})

    def test_a_required_environment_that_does_not_hold_is_refused(self) -> None:
        environment = {**ENVIRONMENT, "sys_platform": "win32", "platform_machine": "AMD64"}
        with pytest.raises(LockError, match="required environment"):
            resolve_lock(lock("with-required-markers"), environment, {"with-required-environments": []})

    def test_a_required_environment_that_holds_is_accepted(self) -> None:
        resolved = resolve_lock(lock("with-required-markers"), ENVIRONMENT, {"with-required-environments": []})
        assert sorted(labels(resolved)) == ["with-required-environments"]

    def test_a_supported_environment_that_does_not_hold_is_refused(self) -> None:
        environment = {**ENVIRONMENT, "sys_platform": "win32"}
        with pytest.raises(LockError, match="supported environment"):
            resolve_lock(lock("with-supported-environments"), environment, {"with-supported-environments": []})


class TestParse:
    """What the format says, read back."""

    def test_a_lock_of_another_version_is_refused(self) -> None:
        with pytest.raises(LockError, match="version"):
            Lock.parse({"version": 2})

    def test_the_revision_is_read_and_nothing_reads_it(self) -> None:
        assert lock("trivial").revision == 3

    def test_a_registry_package_carries_its_artefacts(self) -> None:
        arpeggio = next(item for item in lock("trivial").packages if item.name == "arpeggio")
        assert arpeggio.sdist is not None
        assert arpeggio.sdist.name == "Arpeggio-2.0.2.tar.gz"
        assert [wheel.name for wheel in arpeggio.wheels] == ["Arpeggio-2.0.2-py2.py3-none-any.whl"]
        # The URL is what identifies an artefact to the Nix half.
        assert arpeggio.wheels[0].key == arpeggio.wheels[0].url

    def test_a_package_with_no_source_distribution(self) -> None:
        hgtk = next(item for item in lock("only-wheels").packages if item.name == "hgtk")
        assert hgtk.sdist is None
        assert len(hgtk.wheels) == 1

    def test_a_local_package_is_local(self) -> None:
        trivial = next(item for item in lock("trivial").packages if item.name == "trivial")
        assert trivial.is_local
        assert not trivial.is_virtual
        assert trivial.source == {"editable": "."}

    def test_a_virtual_package_is_not_a_member(self) -> None:
        # `tool.uv.package = false`. It carries edges, and no node builds it.
        resolved = resolve_lock(lock("virtual"), ENVIRONMENT, {"virtual": []})
        assert sorted(labels(resolved)) == ["arpeggio", "virtual"]
        assert sorted(package.name for package in resolved.members) == ["arpeggio"]

    def test_a_package_with_a_dynamic_version(self) -> None:
        # uv writes no `version` when the version is dynamic. uv2nix puts
        # `0.0.0` there; this keeps the absence, because such a package is a
        # local project and its name alone identifies it.
        parsed = lock("dynamic-version")
        assert parsed.packages[0].version is None
        assert parsed.packages[0].label == "dynamic-version"

    def test_a_name_is_normalised_by_pep_503(self) -> None:
        names = {package.name for package in lock("kitchen-sink").packages}
        assert "charset-normalizer" in names

    @pytest.mark.parametrize(("name", "kind"), [("git-subdirectory", "git"), ("url-subdirectory", "url")])
    def test_a_source_that_names_a_place_parses(self, name: str, kind: str) -> None:
        # `git` and `url` sources are 2 of the 1911 packages that
        # `ddrn/PACKAGING-FACTS.md` measured. `test_git_and_url_sources.py`
        # covers what the Nix half then does with each one.
        assert any(kind in package.source for package in lock(name).packages)


class TestASourceThatNamesAFile:
    """The `path` kind, over the file that uv2nix carries.

    `../projects/vendored-app` holds the same two shapes, and
    `test_vendored_app.py` reads them. This class reads a second file, written
    by a second person, so the shapes are not a property of one project.
    """

    def test_an_archive_gives_the_hash_and_no_name(self) -> None:
        attrs = next(item for item in lock("kitchen-sink").packages if item.name == "attrs")
        assert attrs.is_path
        assert attrs.source == {"path": "../attrs-23.1.0.tar.gz"}
        assert attrs.sdist is not None
        assert attrs.sdist.url == ""
        assert attrs.sdist.path == ""
        # No key of the row names the file, so the name comes off the source.
        assert attrs.sdist.filename == ""

    def test_a_wheel_gives_the_name_and_no_address(self) -> None:
        urllib3 = next(item for item in lock("kitchen-sink").packages if item.name == "urllib3")
        assert urllib3.is_path
        assert urllib3.source == {"path": "../urllib3-2.2.3-py3-none-any.whl"}
        assert urllib3.sdist is None
        assert [wheel.name for wheel in urllib3.wheels] == ["urllib3-2.2.3-py3-none-any.whl"]
        assert urllib3.wheels[0].url == ""

    def test_such_a_package_is_not_a_directory_of_the_workspace(self) -> None:
        # **A `path` source takes the artefact route, and not the tree
        # route.** So `uv.nix` copies the file into the store, and no node of
        # the graph reads a directory that the user holds.
        kitchen = lock("kitchen-sink")
        for name in ("attrs", "urllib3"):
            package = next(item for item in kitchen.packages if item.name == name)
            assert not package.is_local
            assert not package.is_virtual


class TestErrors:
    """What the reader says when it cannot answer.

    **Each message here is what a user reads when a lock file is wrong**, and a
    message that no test reads is a message that nothing keeps true. A reader
    of a lock file meets these in one of two ways: the file disagrees with
    itself, or the selection asks for something that the file does not hold.

    The documents below are the smallest ones that reach each answer. A
    fixture from uv cannot reach most of them, because uv does not write a file
    that disagrees with itself.
    """

    HEAD = 'version = 1\nrequires-python = ">=3.12"\n'
    REGISTRY = '{ registry = "https://pypi.org/simple" }'

    def entry(self, name: str, version: str, source: str | None = None) -> str:
        """One `[[package]]` entry, of the registry unless another source is given."""
        return f'\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source or self.REGISTRY}\n'

    def document(self, body: str, head: str | None = None) -> Lock:
        return Lock.parse(tomllib.loads((self.HEAD if head is None else head) + body))

    def test_a_selection_of_a_name_that_the_lock_does_not_hold(self) -> None:
        with pytest.raises(LockError, match="no such package"):
            resolve_lock(lock("trivial"), ENVIRONMENT, {"absent": []})

    def test_a_selection_of_nothing_reaches_nothing(self) -> None:
        # A caller that passes no root asks for an empty environment. The walk
        # starts nowhere and ends there, and an empty answer is more likely a
        # caller that built its selection wrongly than a request.
        with pytest.raises(LockError, match="reached no package at all"):
            resolve_lock(lock("trivial"), ENVIRONMENT, {})

    def test_a_version_that_pep_440_does_not_read(self) -> None:
        parsed = self.document(self.entry("a", "not a version"))
        with pytest.raises(LockError, match="PEP 440 does not read it"):
            _ = parsed.packages[0].id

    def test_an_edge_that_names_an_entry_the_file_does_not_hold(self) -> None:
        # Two entries of `a` make the name ambiguous, so the edge has to name a
        # version and a source. It names a version that neither entry carries,
        # and no entry answers it.
        body = (
            self.entry("a", "0.1.0")
            + self.entry("a", "0.1.1")
            + '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "." }\n'
            + f'\n[[package.dependencies]]\nname = "a"\nversion = "0.9.9"\nsource = {self.REGISTRY}\n'
        )
        with pytest.raises(LockError, match="and the lock file holds no such package"):
            resolve_lock(self.document(body), ENVIRONMENT, {"b": []})

    def test_a_selection_that_reaches_two_entries_of_one_name(self) -> None:
        # An environment installs one distribution for each name. Two edges of
        # one name that both hold ask for a directory that cannot exist, and
        # the edges decided this, so the reader states it rather than choose.
        body = (
            self.entry("a", "0.1.0")
            + self.entry("a", "0.1.1")
            + '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "." }\n'
            + f'\n[[package.dependencies]]\nname = "a"\nversion = "0.1.0"\nsource = {self.REGISTRY}\n'
            + f'\n[[package.dependencies]]\nname = "a"\nversion = "0.1.1"\nsource = {self.REGISTRY}\n'
        )
        with pytest.raises(LockError, match="more than one entry of one name"):
            resolve_lock(self.document(body), ENVIRONMENT, {"b": []})

    def test_a_resolution_marker_decides_between_two_entries_of_one_name(self) -> None:
        # The other half of the answer below. A fork writes a
        # `resolution-markers` on each entry, and the fork whose marker holds
        # is the one that this environment takes.
        body = (
            '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "." }\n'
            "resolution-markers = [ \"sys_platform == 'linux'\" ]\n"
            '\n[[package]]\nname = "b"\nversion = "0.2.0"\nsource = { editable = "." }\n'
            "resolution-markers = [ \"sys_platform == 'win32'\" ]\n"
        )
        resolved = resolve_lock(self.document(body), ENVIRONMENT, {"b": []})
        assert labels(resolved) == {"b": "b-0.1.0"}

    def test_a_root_of_a_name_that_two_entries_carry(self) -> None:
        # `Lock.find` answers a name and no version, which is what a root is.
        # Neither entry carries a resolution marker, so both take part in every
        # fork and nothing decides. This is `Lock::find_by_markers` of uv, and
        # uv raises here as well.
        body = (
            '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "." }\n'
            '\n[[package]]\nname = "b"\nversion = "0.2.0"\nsource = { editable = "." }\n'
        )
        with pytest.raises(LockError, match="do not decide between them"):
            resolve_lock(self.document(body), ENVIRONMENT, {"b": []})

    def test_a_conflict_item_that_names_neither_an_extra_nor_a_group(self) -> None:
        # Each item of a conflict declaration names a package and one of the
        # two. An item that names the package alone constrains nothing, and it
        # is more likely a file that was written by hand.
        head = (
            'version = 1\nrequires-python = ">=3.12"\n'
            'conflicts = [[ { package = "a" }, { package = "a", extra = "x" } ]]\n'
        )
        parsed = self.document(self.entry("a", "0.1.0"), head=head)
        with pytest.raises(LockError, match="names neither an extra nor a group"):
            filter_conflicts(parsed, {"a": ["x"]})

    def test_a_conflict_of_a_package_that_the_selection_leaves_out(self) -> None:
        # A package that the selection does not name enables no extra and no
        # group, so its conflict declaration constrains nothing and the
        # declaration is left alone. `TestAWorkspaceMemberThatIsNotSelected`
        # holds the case that this exists for.
        filtered = filter_conflicts(lock("conflicts"), {"other": []})
        package = next(item for item in filtered.packages if item.name == "conflicts")
        assert sorted(package.optional_dependencies) == ["extra-a", "extra-b"]
        assert sorted(package.dev_dependencies) == ["group-c"]


class TestThePackageIdentity:
    """The name, the version and the source together identify one entry.

    **The source is a part of the identity, and a reader that drops it takes
    the wrong file.** `two-indexes.lock` is the output of
    `lock_multiple_sources_index_disjoint_markers`, a test of uv itself
    (`crates/uv/tests/lock/lock.rs`). It pins `jinja2` 3.1.3 to one index for
    Windows and to another index for every other system, and the two entries
    differ in nothing else. This is the shape that a project takes when it
    pulls `torch` from the CPU index and the CUDA index of PyTorch, which is
    the common reason to hold two indexes at all.
    """

    def test_two_entries_of_one_name_and_one_version_stay_two(self) -> None:
        packages = [item for item in lock("two-indexes").packages if item.name == "jinja2"]
        assert [item.version for item in packages] == ["3.1.3", "3.1.3"]
        assert len({item.id for item in packages}) == 2

    def test_the_environment_takes_the_index_that_its_marker_names(self) -> None:
        parsed = lock("two-indexes")
        resolved = resolve_lock(parsed, ENVIRONMENT, {"project": []})
        jinja = resolved.by_name()["jinja2"]
        assert jinja.source == {"registry": "https://astral-sh.github.io/pytorch-mirror/whl/cu124"}

    def test_another_environment_takes_the_other_index(self) -> None:
        # The one edge that holds decides, and the two edges differ in the
        # source alone. A reader that compared the name and the version would
        # find the two entries equal and install whichever it met first.
        parsed = lock("two-indexes")
        environment = {**ENVIRONMENT, "sys_platform": "win32", "os_name": "nt", "platform_system": "Windows"}
        resolved = resolve_lock(parsed, environment, {"project": []})
        jinja = resolved.by_name()["jinja2"]
        assert jinja.source == {"registry": "https://astral-sh.github.io/pytorch-mirror/whl/cu118"}

    def test_an_edge_of_an_unambiguous_name_takes_the_source_of_that_entry(self) -> None:
        # Both `jinja2` entries depend on `{ name = "markupsafe" }`, with no
        # version and no source. One entry of that name exists, so uv omits
        # both fields as redundant and this reader puts them back.
        parsed = lock("two-indexes")
        jinja = next(item for item in parsed.packages if item.name == "jinja2")
        edge = jinja.dependencies[0]
        assert edge.name == "markupsafe"
        assert str(edge.target.version) == "2.1.5"
        assert "cu118" in edge.target.source


class TestAnEdgeNamesOneEntry:
    """An edge that omits a field which no entry can supply is refused.

    **These five documents are the unit tests of uv itself**, from
    `crates/uv-resolver/src/lock/mod.rs`. uv writes the version and the source
    of an edge only when the name is ambiguous, so an edge that omits either
    one of them for an ambiguous name names no single entry.
    `PackageIdForDependency::unwire` refuses to read such a file, and this
    reader gives the same answer.
    """

    HEAD = 'version = 1\nrequires-python = ">=3.12"\n'
    ENTRY = (
        '\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    )

    def document(self, versions: list[str], edge: str) -> Lock:
        """A lock file of `a` at each version, and of `b` that depends on `a`."""
        text = self.HEAD
        for version in versions:
            text += self.ENTRY.format(name="a", version=version)
        text += self.ENTRY.format(name="b", version="0.1.0")
        text += f'\n[[package.dependencies]]\nname = "a"\n{edge}'
        return Lock.parse(tomllib.loads(text))

    #: The three edges of the tests of uv: no source, no version, and neither.
    SOURCE = 'source = { registry = "https://pypi.org/simple" }\n'
    VERSION = 'version = "0.1.0"\n'

    @pytest.mark.parametrize("edge", [VERSION, SOURCE, ""])
    def test_one_entry_of_the_name_supplies_what_the_edge_omits(self, edge: str) -> None:
        # `missing_dependency_source_unambiguous`, and the two beside it. One
        # entry carries the name, so uv reads the file and so does this.
        parsed = self.document(["0.1.0"], edge)
        target = next(item for item in parsed.packages if item.name == "b").dependencies[0].target
        assert str(target.version) == "0.1.0"
        assert "pypi.org" in target.source

    def test_two_entries_and_an_edge_with_no_source(self) -> None:
        # `missing_dependency_source_ambiguous`. uv says: "Dependency `a` has
        # missing `source` field but has more than one matching package".
        with pytest.raises(LockError, match="names no source"):
            self.document(["0.1.0", "0.1.1"], self.VERSION)

    def test_two_entries_and_an_edge_with_no_version(self) -> None:
        # `missing_dependency_version_ambiguous`. The version of a registry
        # package is never dynamic, so nothing supplies the missing field.
        with pytest.raises(LockError, match="names no version"):
            self.document(["0.1.0", "0.1.1"], self.SOURCE)

    def test_two_entries_and_an_edge_with_neither(self) -> None:
        # `missing_dependency_source_version_ambiguous`. uv reports the source
        # first, and the order matters to nobody but the reader of the message.
        with pytest.raises(LockError, match="names no source"):
            self.document(["0.1.0", "0.1.1"], "")

    def test_a_directory_may_omit_the_version(self) -> None:
        # `missing_dependency_version_dynamic`. `Source::is_source_tree` names
        # the three kinds that may, and a project of a directory declares a
        # dynamic version. `dynamic-version.lock` is that shape from uv.
        parsed = lock("dynamic-version")
        assert parsed.packages[0].id.version is None


class TestAConflictMarkerIsNotPep508:
    """uv encodes a conflict into the `extra` of a marker, and says so.

    The doc comment of `UniversalMarker`
    (`crates/uv-resolver/src/universal_marker.rs`) gives the reason: "we
    technically preserve PEP 508 compatibility but abuse the `extra` attribute
    to encode conflicts". `packaging` reads such a marker as PEP 508 and
    compares it against the `extra` of the environment, which is empty here.
    Every such edge would then disappear with no message.
    """

    def document(self, marker: str) -> Lock:
        text = (
            'version = 1\nrequires-python = ">=3.12"\n'
            '\n[[package]]\nname = "a"\nversion = "0.1.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            '\n[[package]]\nname = "b"\nversion = "0.1.0"\nsource = { editable = "." }\n'
            f'\n[[package.dependencies]]\nname = "a"\nmarker = "{marker}"\n'
        )
        return Lock.parse(tomllib.loads(text))

    def test_an_extra_of_a_package_is_refused_and_decoded(self) -> None:
        with pytest.raises(LockError, match="the extra 'foo' of the package 'example'"):
            self.document("extra == 'extra-7-example-foo'")

    def test_a_group_of_a_package_is_refused_and_decoded(self) -> None:
        with pytest.raises(LockError, match="the group 'dev' of the package 'example'"):
            self.document("extra == 'group-7-example-dev'")

    def test_a_project_is_refused_and_decoded(self) -> None:
        # `project-7-example` means that the production dependencies of the
        # workspace member `example` are enabled. The number is the length of
        # the package name, which is how a reader finds where that name ends.
        with pytest.raises(LockError, match="the package 'example'"):
            self.document("extra == 'project-7-example'")

    def test_an_ordinary_marker_still_reads(self) -> None:
        parsed = self.document("sys_platform == 'linux'")
        edge = next(item for item in parsed.packages if item.name == "b").dependencies[0]
        assert edge.holds(ENVIRONMENT)
