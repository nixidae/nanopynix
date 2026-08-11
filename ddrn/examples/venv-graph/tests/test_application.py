"""The two shapes that a planner submits: an environment, and an application.

**A virtual environment is not a program.** `make-venv.py` writes a real one,
so the result carries `bin/python`, `bin/activate` and the console script of
every member. A NixOS configuration that puts such a path in
`environment.systemPackages` shadows the interpreter of the system.

`Plan.root` gives the other shape when the caller names one member: the tree of
that member, with each entry a link into the same environment. `pyproject.nix`
calls this `util.mkApplication`, and `scripts/make-application.py` gives the
two skip rules that both implement.

`../check-application.nix` builds the shape and runs it, and `../run-uv.sh`
reads that report. This module holds the decisions that need no build.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from ddrn_venv import Artifact, Plan

if TYPE_CHECKING:
    from collections.abc import Mapping

EXAMPLE = Path(__file__).resolve().parent.parent

#: The interpreter of the environment decides this, and `graph.nix` gives it to
#: the node. The value below is any version, because no test here runs Python
#: out of the tree that it builds.
SITE = Path("lib/python3.14/site-packages")


def make_application() -> Any:
    """`../scripts/make-application.py`, as a module.

    **A build step of this example is a plain script, and no package holds
    it.** `graph.nix` gives each one to an interpreter by path, so a test
    reaches one the same way. The alternative is a build for each case, and the
    decisions below need none.
    """
    path = EXAMPLE / "scripts/make-application.py"
    spec = importlib.util.spec_from_file_location("make_application", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path} is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(root: Path, paths: Mapping[str, str]) -> Path:
    """Write a store path of files, and give it back."""
    for name, text in paths.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def wheel(package: str, version: str) -> Artifact:
    """One row of a lock file, as a pure wheel."""
    filename = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
    return Artifact(package=package, version=version, filename=filename, kind="wheel")


def with_members(*packages: str) -> Plan:
    """A plan whose environment holds one node for each of `packages`."""
    plan = Plan()
    for package in packages:
        name = plan.artifact_node(wheel(package, "1.0"), "wheel")
        plan.member(name, f"{package}==1.0", package=package)
    return plan


class TestTheEnvironment:
    """What every build gives when the caller names no application."""

    def test_the_root_is_the_environment_itself(self) -> None:
        plan = with_members("rich")
        assert plan.root("demo-venv") == "demo-venv"
        assert [node["kind"] for node in plan.nodes] == ["wheel", "venv"]


class TestTheApplication:
    """The shape that one member takes over that same environment."""

    def test_the_application_takes_the_name_and_the_environment_the_suffix(self) -> None:
        # The caller builds the application, so the application carries the
        # name of the project. A reader of the store path of the environment
        # then sees that it is the environment of that same project.
        plan = with_members("ws-cli")
        assert plan.root("ws-cli-app", application="ws-cli") == "ws-cli-app"
        assert [node["name"] for node in plan.nodes] == ["ws-cli-1.0", "ws-cli-app-env", "ws-cli-app"]

    def test_the_environment_is_still_a_node_of_the_graph(self) -> None:
        # The application links into the environment, so both are built. This
        # is the reason the closure does not shrink.
        plan = with_members("ws-cli", "rich")
        plan.root("app", application="ws-cli")
        application = plan.nodes[-1]
        assert application["kind"] == "application"
        assert application["venv"] == "app-env"
        assert application["bases"] == ["ws-cli-1.0"]

        environment = next(node for node in plan.nodes if node["kind"] == "venv")
        assert environment["members"] == ["ws-cli-1.0", "rich-1.0"]

    def test_the_name_is_read_the_way_pep_503_reads_it(self) -> None:
        # `Ws_CLI` and `ws-cli` are one name, so a caller who spells the
        # argument the way `pyproject.toml` spells it gets the same node.
        plan = with_members("ws-cli")
        plan.root("app", application="Ws_CLI")
        assert plan.nodes[-1]["bases"] == ["ws-cli-1.0"]

    def test_a_list_of_members_is_one_output(self) -> None:
        # A project that ships three programs ships them together. Three
        # applications would each hold a link into the same environment.
        plan = with_members("ws-cli", "ws-web", "rich")
        plan.root("app", application=["ws-cli", "ws-web"])
        assert plan.nodes[-1]["bases"] == ["ws-cli-1.0", "ws-web-1.0"]
        assert len([node for node in plan.nodes if node["kind"] == "application"]) == 1

    def test_a_repeated_name_is_one_template(self) -> None:
        plan = with_members("ws-cli")
        plan.root("app", application=["ws-cli", "Ws_CLI"])
        assert plan.nodes[-1]["bases"] == ["ws-cli-1.0"]

    def test_an_empty_list_says_what_to_pass_instead(self) -> None:
        plan = with_members("ws-cli")
        with pytest.raises(ValueError, match="application is an empty list"):
            plan.root("app", application=[])

    def test_every_name_that_no_member_answers_is_named(self) -> None:
        # One message for the whole list, because a caller who mistyped two
        # names should not have to build twice to learn the second.
        plan = with_members("ws-cli")
        with pytest.raises(ValueError, match="'ws-web', 'ws-core'"):
            plan.root("app", application=["ws-cli", "ws-web", "ws-core"])

    def test_a_package_that_the_environment_does_not_hold_is_refused(self) -> None:
        # A silent empty output is the failure to avoid: an application of a
        # package that nothing installed would build and hold nothing.
        plan = with_members("rich", "ws-core")
        with pytest.raises(ValueError, match="application names 'ws-cli'") as raised:
            plan.root("app", application="ws-cli")
        assert "rich, ws-core" in str(raised.value)

    def test_an_empty_environment_says_so(self) -> None:
        plan = Plan()
        with pytest.raises(ValueError, match="no member at all"):
            plan.root("app", application="ws-cli")


class TestWhatTheLinkerWrites:
    """`../scripts/make-application.py`, over trees that this test writes.

    The node runs under the interpreter of the environment, so these cases
    exercise the two rules and the union with no build at all.
    """

    def build(self, tmp_path: Path, templates: Mapping[str, Mapping[str, str]], venv: Mapping[str, str]) -> Path:
        """Run the linker over `templates`, against an environment of `venv`."""
        module = make_application()
        out = tmp_path / "out"
        environment = tree(tmp_path / "venv", venv)
        bases = [str(tree(tmp_path / name, paths)) for name, paths in templates.items()]

        code = module.main.__globals__["os"].environ
        for key, value in {
            "out": str(out),
            "venv": str(environment),
            "bases": " ".join(bases),
            "site": str(SITE),
        }.items():
            code[key] = value
        return out if module.main() == 0 else Path()

    def test_a_program_becomes_a_link_into_the_environment(self, tmp_path: Path) -> None:
        out = self.build(
            tmp_path,
            {"cli": {"bin/ws-cli": "script", str(SITE / "ws_cli/__init__.py"): "code"}},
            {"bin/ws-cli": "script", "bin/python": "interpreter"},
        )
        assert sorted(path.name for path in out.rglob("*")) == ["bin", "ws-cli"]
        assert (out / "bin/ws-cli").is_symlink()
        assert (out / "bin/ws-cli").resolve() == (tmp_path / "venv/bin/ws-cli").resolve()

    def test_site_packages_and_nix_support_do_not_reach_the_output(self, tmp_path: Path) -> None:
        # The environment holds the code already, and a second copy of the tree
        # of one member would shadow it. `pyproject.nix` skips the same two.
        out = self.build(
            tmp_path,
            {"cli": {"bin/ws-cli": "s", str(SITE / "ws_cli/__init__.py"): "c", "nix-support/propagated": "x"}},
            {"bin/ws-cli": "s", str(SITE / "ws_cli/__init__.py"): "c", "nix-support/propagated": "x"},
        )
        assert [str(path.relative_to(out)) for path in sorted(out.rglob("*"))] == ["bin", "bin/ws-cli"]

    def test_two_templates_that_share_a_path_write_one_link(self, tmp_path: Path) -> None:
        # Two members of one project ship one `share/man/man1` between them,
        # and the union writes that link once.
        out = self.build(
            tmp_path,
            {
                "cli": {"bin/ws-cli": "s", "share/man/man1/ws.1": "m"},
                "web": {"bin/ws-web": "s", "share/man/man1/ws.1": "m"},
            },
            {"bin/ws-cli": "s", "bin/ws-web": "s", "share/man/man1/ws.1": "m"},
        )
        assert [str(path.relative_to(out)) for path in sorted(out.rglob("*")) if path.is_symlink()] == [
            "bin/ws-cli",
            "bin/ws-web",
            "share/man/man1/ws.1",
        ]

    def test_a_path_that_the_environment_dropped_is_left_out(self, tmp_path: Path) -> None:
        # `make-venv.py` drops the `.dist-info` of a member, and a link to a
        # path that no build wrote would be a broken link in the output.
        out = self.build(
            tmp_path,
            {"cli": {"bin/ws-cli": "s", str(SITE / "ws_cli-1.0.dist-info/RECORD"): "r"}},
            {"bin/ws-cli": "s"},
        )
        assert [str(path.relative_to(out)) for path in sorted(out.rglob("*"))] == ["bin", "bin/ws-cli"]

    def test_a_library_among_the_templates_is_refused(self, tmp_path: Path) -> None:
        # An application that built and held nothing is the failure to avoid,
        # and one library among three programs would otherwise pass unnoticed.
        assert (
            self.build(
                tmp_path,
                {
                    "cli": {"bin/ws-cli": "s"},
                    "core": {str(SITE / "ws_core/__init__.py"): "c"},
                },
                {"bin/ws-cli": "s"},
            )
            == Path()
        )
