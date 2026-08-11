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

import pytest
from ddrn_venv import Artifact, Plan


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
        assert application["base"] == "ws-cli-1.0"

        environment = next(node for node in plan.nodes if node["kind"] == "venv")
        assert environment["members"] == ["ws-cli-1.0", "rich-1.0"]

    def test_the_name_is_read_the_way_pep_503_reads_it(self) -> None:
        # `Ws_CLI` and `ws-cli` are one name, so a caller who spells the
        # argument the way `pyproject.toml` spells it gets the same node.
        plan = with_members("ws-cli")
        plan.root("app", application="Ws_CLI")
        assert plan.nodes[-1]["base"] == "ws-cli-1.0"

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
