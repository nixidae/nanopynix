from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import huggorm
import pytest
from anyio import Path as AnyioPath
from huggorm.errors import BuildError, NixError
from huggorm.fod import replace_fod_hash as _real_replace_fod_hash
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, with_nixpkgs

from pynix import _util, parse
from pynix._impl import build as build_impl
from pynix._impl.build import (
    BuildSession,
    BuildTargetError,
    _build_target,
)
from pynix.target import EvaluationTarget
from support.ansi import strip_ansi
from support.nix_oracle import require_matching_nix_cli
from test_support.git_fixtures import init_flake_repo


@LINUX_CHROOT_BUILD
async def test_build_file_derivation(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-file-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-file > "$out"
        '';
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(["build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-file-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-file\n"


@LINUX_CHROOT_BUILD
async def test_build_nom_draws_the_build_and_its_totals(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--nom`` draws the dependency graph and the sum row, and stdout stays JSON."""
    nix_file = tmp_path / "nom.nix"
    nix_file.write_text(
        f"""
let
  dep = derivation {{
    name = "pynix-nom-dep";
    system = builtins.currentSystem;
    builder = "/bin/sh";
    args = [ "-c" "echo {tmp_path.name} > $out" ];
  }};
in derivation {{
  name = "pynix-nom-test";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "read x < ${{dep}}; echo $x > $out" ];
}}
"""
    )
    cmd = parse(["build", "--nom", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    captured = capsys.readouterr()
    assert "pynix-nom-test" in json.loads(captured.out)["outputs"]["out"]
    # The input above its root, as nom draws the graph.
    assert re.search(r"┃ ┌─ ✔ pynix-nom-dep.*\n┃ ✔ pynix-nom-test", captured.err), captured.err
    assert re.search(r"┗━ ∑ ⏵ 0 │ ✔ 2 │ ⏸ 0 │ .*Finished at \d\d:\d\d:\d\d", captured.err), captured.err


@LINUX_CHROOT_BUILD
async def test_build_nom_marks_a_failed_build(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix logs no error for the build a caller asked for, and --nom still shows it failed."""
    nix_file = tmp_path / "nom-fail.nix"
    nix_file.write_text(
        f"""
derivation {{
  name = "pynix-nom-fails";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo {tmp_path.name}; exit 1" ];
}}
"""
    )
    cmd = parse(["build", "--nom", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    # `main` prints the error after the command returns, so after the last frame.
    with pytest.raises(BuildError, match="Cannot build"):
        await cmd.run()

    err = capsys.readouterr().err
    assert "┃ ⚠ pynix-nom-fails failed after" in err, err
    assert "⚠ Exited after 1 build failures at" in err, err


@LINUX_CHROOT_BUILD
async def test_build_file_derivation_attr(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-attr > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        [
            "build",
            "--file",
            str(nix_file),
            "--attr",
            "package",
            *shared_nix_environment.pynix_store_args(),
            "--print-build-logs",
        ],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-attr-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-attr\n"


@LINUX_CHROOT_BUILD
async def test_build_file_auto_calls_defaulted_lambda_before_attr(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "default.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    { pkgs ? import <nixpkgs> {}, name ? "pynix-build-autocall-test" }:
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = name;
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-from-autocall > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "package", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-autocall-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-from-autocall\n"


async def test_build_missing_attr_errors_before_build(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    {
      package = pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-attr-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo should-not-build > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "missing", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


async def test_build_names_the_toplevel_of_a_nixos_configuration(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix answers `selected value is not a derivation`, which is the end of the road.

    That message says neither what the value is nor which path would work. The
    module system stamps `_type` and `class` on the result of `nixosSystem`, so
    two attribute reads answer both.
    """
    nix_file = tmp_path / "configuration.nix"
    nix_file.write_text(
        """
    {
      hetztop = {
        _type = "configuration";
        class = "nixos";
        config = { system.build.toplevel = "unused"; };
        options = { };
      };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", "hetztop", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "hetztop is a NixOS configuration, not a derivation" in error
    assert "hetztop.config.system.build.toplevel" in error


async def test_build_names_the_activation_package_of_a_home_manager_configuration(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """home-manager sets `class = "homeManager"`, and holds its derivation elsewhere."""
    nix_file = tmp_path / "home.nix"
    nix_file.write_text(
        """
    {
      me = {
        _type = "configuration";
        class = "homeManager";
        config = { home.activationPackage = "unused"; };
        options = { };
      };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", "me", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "me is a home-manager configuration, not a derivation" in error
    assert "me.config.home.activationPackage" in error


@pytest.mark.parametrize("attribute", ["unknown_class", "plain_set"])
async def test_build_builds_nothing_for_a_set_that_holds_no_derivation(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    attribute: str,
) -> None:
    """A `class` that is not one of the two named gets no guess.

    A set that holds `config` and `options` is any evaluation of the module
    system, and there are more of those than the two that this knows. Such a
    set is an attribute set like any other, and `nix build --file other.nix
    plain_set` builds the derivations in it: none, with `[]` and exit 0
    (nixidae/nixidae#1002).
    """
    nix_file = tmp_path / "other.nix"
    nix_file.write_text(
        """
    {
      unknown_class = {
        _type = "configuration";
        class = "someOtherModuleSystem";
        config = { };
        options = { };
      };
      plain_set = { a = 1; };
    }
    """
    )
    cmd = parse(["build", "--file", str(nix_file), "--attr", attribute, *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    assert json.loads(capsys.readouterr().out)["outputs"] == {}


async def test_build_refuses_a_value_that_holds_no_derivation(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "number.nix"
    nix_file.write_text("1")
    cmd = parse(["build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    assert "does not evaluate to a derivation (or a set or list of those)" in strip_ansi(capsys.readouterr().err)


def _trivial(name: str) -> str:
    return (
        f'derivation {{ name = "{name}"; system = builtins.currentSystem; builder = "/bin/sh"; '
        f'args = [ "-c" "echo {name} > $out" ]; }}'
    )


async def _build_json(environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], *arguments: str) -> Any:
    await parse(["build", *arguments, *environment.pynix_store_args()]).run()
    return json.loads(capsys.readouterr().out)["outputs"]


@LINUX_CHROOT_BUILD
async def test_build_every_attr_and_key_the_outputs_by_attr(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`nix build --file two.nix a b` builds both (nixidae/nixidae#891)."""
    nix_file = tmp_path / "two.nix"
    nix_file.write_text(f"{{ a = {_trivial('pynix-attr-a')}; b = {_trivial('pynix-attr-b')}; }}")

    outputs = await _build_json(shared_nix_environment, capsys, "--file", str(nix_file), "--attr", "a", "--attr", "b")

    assert sorted(outputs) == ["a", "b"]
    assert outputs["a"]["out"].endswith("-pynix-attr-a")
    assert outputs["b"]["out"].endswith("-pynix-attr-b")


def test_out_links_are_named_as_nix_names_them() -> None:
    """``createOutLinks`` (``libcmd/command.cc``): ``-N`` from the second
    result on, and ``-OUTPUT`` for every output but ``out``."""
    built = [{"out": "/nix/store/a", "dev": "/nix/store/a-dev"}, {"out": "/nix/store/b"}]
    assert _util.out_links(Path("res"), built) == [
        (Path("res-dev"), "/nix/store/a-dev"),
        (Path("res"), "/nix/store/a"),
        (Path("res-1"), "/nix/store/b"),
    ]


@LINUX_CHROOT_BUILD
async def test_build_out_link_links_each_output(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "two.nix"
    await AnyioPath(nix_file).write_text(f"{{ a = {_trivial('pynix-link-a')}; b = {_trivial('pynix-link-b')}; }}")
    prefix = tmp_path / "res"

    outputs = await _build_json(
        shared_nix_environment, capsys, "--file", str(nix_file), "--attr", "a", "--attr", "b", "--out-link", str(prefix)
    )

    assert await AnyioPath(prefix).readlink() == Path(outputs["a"]["out"])
    assert await AnyioPath(f"{prefix}-1").readlink() == Path(outputs["b"]["out"])


@LINUX_CHROOT_BUILD
async def test_build_links_nothing_by_default(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nix build`` links ``./result``, and pynix links only when asked."""
    nix_file = tmp_path / "one.nix"
    await AnyioPath(nix_file).write_text(_trivial("pynix-no-link"))
    monkeypatch.chdir(tmp_path)

    await _build_json(shared_nix_environment, capsys, "--file", str(nix_file))

    assert [p.name async for p in AnyioPath(tmp_path).iterdir()] == ["one.nix"]


@LINUX_CHROOT_BUILD
async def test_build_takes_no_link_and_links_nothing(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-link`` is what pynix does already; a command written for nix runs unchanged."""
    nix_file = tmp_path / "one.nix"
    await AnyioPath(nix_file).write_text(_trivial("pynix-no-link"))
    monkeypatch.chdir(tmp_path)

    await _build_json(shared_nix_environment, capsys, "--file", str(nix_file), "--no-link")

    assert [p.name async for p in AnyioPath(tmp_path).iterdir()] == ["one.nix"]


async def test_build_refuses_no_link_with_an_out_link(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """In Nix the later flag wins; pynix refuses the pair."""
    argv = ["build", "--file", str(tmp_path / "x.nix"), "--no-link", "--out-link", str(tmp_path / "r")]
    with pytest.raises(SystemExit):
        await parse([*argv, *shared_nix_environment.pynix_store_args()]).run()
    assert "--no-link and --out-link contradict each other" in capsys.readouterr().err


@LINUX_CHROOT_BUILD
async def test_build_print_out_paths_prints_what_nix_prints(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await require_matching_nix_cli()
    nix_file = tmp_path / "two.nix"
    await AnyioPath(nix_file).write_text(f"{{ a = {_trivial('pynix-print-a')}; b = {_trivial('pynix-print-b')}; }}")
    store = shared_nix_environment.pynix_store_args()
    await parse(["build", "--file", str(nix_file), "--attr", "b", "--attr", "a", "--print-out-paths", *store]).run()
    ours = capsys.readouterr().out
    theirs = await anyio.run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "build",
            "--no-link",
            "--print-out-paths",
            "--file",
            str(nix_file),
            "b",
            "a",
            *store,
        ]
    )
    assert ours == theirs.stdout.decode()
    assert len(ours.splitlines()) == 2


@LINUX_CHROOT_BUILD
async def test_build_expr_builds_what_nix_build_expr_builds(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """The expression is called with the arguments, as a ``--file`` is."""
    await require_matching_nix_cli()
    expr = (
        '{ name }: { a = derivation { name = "pynix-expr-${name}"; system = builtins.currentSystem; '
        'builder = "/bin/sh"; args = [ "-c" "echo > $out" ]; }; }'
    )
    store = shared_nix_environment.pynix_store_args()
    # `--impure`: an expression is pure as Nix has it, and `currentSystem` is impure.
    argv = ["--expr", expr, "--argstr", "name", "x", "--impure"]
    await parse(["build", *argv, "--attr", "a", "--print-out-paths", *store]).run()
    ours = capsys.readouterr().out
    theirs = await anyio.run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "build",
            "--no-link",
            "--print-out-paths",
            *argv,
            "a",
            *store,
        ]
    )
    assert ours == theirs.stdout.decode()
    assert ours.strip().endswith("-pynix-expr-x")


async def test_build_refuses_expr_with_a_file(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["build", "--expr", "{}", "--file", str(tmp_path / "x.nix"), *shared_nix_environment.pynix_store_args()]
    with pytest.raises(SystemExit):
        await parse(argv).run()
    assert "--expr cannot be combined with --file or --flake" in capsys.readouterr().err


@LINUX_CHROOT_BUILD
@pytest.mark.parametrize("keep_going", [False, True], ids=["stop", "keep-going"])
async def test_build_keep_going_builds_past_a_failure_as_nix_does(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    keep_going: bool,
) -> None:
    """Two failing builds, one job: Nix stops after the first, unless it keeps going."""
    await require_matching_nix_cli()
    nix_file = tmp_path / "two.nix"
    failing = (
        'derivation {{ name = "{0}"; system = builtins.currentSystem; builder = "/bin/sh"; args = [ "-c" "exit 1" ]; }}'
    )
    await AnyioPath(nix_file).write_text(
        f"{{ a = {failing.format('pynix-keep-going-a')}; b = {failing.format('pynix-keep-going-b')}; }}"
    )
    monkeypatch.setenv("PYNIX_NIX_MAX_JOBS", "1")
    store = shared_nix_environment.pynix_store_args()
    flag = ["--keep-going"] if keep_going else []
    with pytest.raises(NixError) as raised:
        await parse(["build", "--file", str(nix_file), "--attr", "a", "--attr", "b", *flag, *store]).run()
    # What a run prints: the logged failures, then `main` prints the raised error.
    ours = strip_ansi(capsys.readouterr().err + raised.value.colored)
    theirs = await anyio.run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "build",
            "--no-link",
            *flag,
            "--file",
            str(nix_file),
            "a",
            "b",
            *store,
        ],
        check=False,
        # `use-cgroups` from a host's nix.conf fails every local build of a user (nixidae/nixidae#1037).
        env={**os.environ, "NIX_CONFIG": "max-jobs = 1\nuse-cgroups = false"},
    )
    nix_err = strip_ansi(theirs.stderr.decode())
    assert ours.count("Cannot build '") == nix_err.count("Cannot build '") == (2 if keep_going else 1)
    # `throwBuildErrors` names every failed request once more than one failed.
    assert ("error: build of '" in ours) == ("error: build of '" in nix_err) == keep_going


@LINUX_CHROOT_BUILD
async def test_build_one_attr_twice_keeps_the_flat_outputs(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "one.nix"
    nix_file.write_text(f"{{ a = {_trivial('pynix-attr-twice')}; }}")

    outputs = await _build_json(shared_nix_environment, capsys, "--file", str(nix_file), "--attr", "a", "--attr", "a")

    assert outputs["out"].endswith("-pynix-attr-twice")


@LINUX_CHROOT_BUILD
async def test_build_a_set_builds_each_derivation_in_it(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`nix build --file . checks` (nixidae/nixidae#1002): one level, a member
    set only with `recurseForDerivations`, and every other value skipped."""
    nix_file = tmp_path / "checks.nix"
    nix_file.write_text(
        f"""{{
  checks = {{
    x = {_trivial("pynix-check-x")};
    y = {_trivial("pynix-check-y")};
    deeper = {{ recurseForDerivations = true; z = {_trivial("pynix-check-z")}; }};
    hidden = {{ w = throw "a set without recurseForDerivations is not entered"; }};
    number = 1;
  }};
}}"""
    )

    outputs = await _build_json(shared_nix_environment, capsys, "--file", str(nix_file), "--attr", "checks")

    assert sorted(outputs) == ["checks.deeper.z", "checks.x", "checks.y"]
    assert outputs["checks.deeper.z"]["out"].endswith("-pynix-check-z")


@LINUX_CHROOT_BUILD
async def test_build_flake_derivation(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(["build", "--flake", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "test-hello" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "hi\n"


async def test_build_missing_input_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["build"])

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "either --file or --flake is required" in captured.err


async def test_build_update_fod_without_file_errors(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(
        ["build", "--flake", f"{git_flake}#hello", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--update-fod requires --file to name a local file" in captured.err


async def test_build_update_fod_with_a_fetched_file_errors(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--file now accepts a reference, and --update-fod still needs a local file.

    ``<nixpkgs>`` resolves through the lookup path into the store, which is
    read-only. The guard refuses it before any evaluation starts, so this test
    needs no network and no lookup path that exists.
    """
    cmd = parse(
        ["build", "--file", "<nixpkgs>", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--update-fod requires --file to name a local file" in captured.err


async def test_build_dry_run_without_update_fod_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text("1\n")
    cmd = parse(["build", "--file", str(nix_file), "--dry-run", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--dry-run requires --update-fod" in captured.err


@LINUX_CHROOT_BUILD
async def test_build_propagates_a_non_fod_build_failure(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A build failure unrelated to a FOD hash mismatch must surface as the
    original BuildError, not be swallowed or turned into a FOD update."""
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-fails-test";
        version = "1";
        dontUnpack = true;
        installPhase = "exit 1";
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(["build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])

    with pytest.raises(BuildError):
        await cmd.run()

    assert capsys.readouterr().out == ""


@LINUX_CHROOT_BUILD
async def test_build_with_separate_eval_store(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "build-test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
      pkgs.stdenvNoCC.mkDerivation {
        pname = "pynix-build-eval-store-test";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo built-with-eval-store > "$out"
        '';
      }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        [
            "build",
            "--file",
            str(nix_file),
            *shared_nix_environment.pynix_store_args(),
            "--eval-store",
            shared_nix_environment.store_uri,
        ],
    )

    await cmd.run()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    out_path = data["outputs"]["out"]
    assert "pynix-build-eval-store-test" in out_path
    assert await AnyioPath(shared_nix_environment.physical_path(out_path)).read_text() == "built-with-eval-store\n"


async def test_build_update_fod_dry_run_reports_local_fetchurl_diff(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    nix_file = tmp_path / "source.nix"
    source = f'builtins.fetchurl {{ url = "file://{payload}"; sha256 = ""; }}\n'
    nix_file.write_text(source)
    cmd = parse(
        ["build", "--file", str(nix_file), "--update-fod", "--dry-run", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"dryRun": True, "outputs": {}, "updatedFods": 1}
    assert "-builtins.fetchurl" in strip_ansi(captured.err)
    assert "+builtins.fetchurl" in strip_ansi(captured.err)
    assert nix_file.read_text() == source


async def test_build_update_fod_dry_run_updates_a_lib_fakehash_binding(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`lib.fakeHash` is how nixpkgs says "I have not computed this hash".

    The updater read a plain string and nothing else, so the convention an
    author actually writes reported that the file holds no hash literal.
    Issue #109.
    """
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    nix_file = tmp_path / "source.nix"
    source = with_nixpkgs(
        f'with import <nixpkgs> {{}};\nbuiltins.fetchurl {{ url = "file://{payload}"; sha256 = lib.fakeHash; }}\n',
        nixpkgs_path,
    )
    nix_file.write_text(source)
    cmd = parse(
        ["build", "--file", str(nix_file), "--update-fod", "--dry-run", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"dryRun": True, "outputs": {}, "updatedFods": 1}
    # The diff is wrapped for a terminal, and the wrap point moves with the
    # length of the temporary path, so it lands inside `sha256 = "sha256:...`
    # under one root and not under another. Match the text with every run of
    # whitespace removed: the rendering then cannot decide whether the
    # assertion holds.
    flattened = re.sub(r"\s+", "", strip_ansi(captured.err))
    # The symbol leaves and a quoted hash takes its place. Nix reports the
    # computed hash in base32 here and in SRI elsewhere, so the assertion
    # names the attribute and the algorithm and not the encoding.
    assert "lib.fakeHash" in flattened
    assert re.search(r'sha256="sha256[:-][A-Za-z0-9+/=]+"', flattened)
    assert nix_file.read_text() == source


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_a_lib_fakehash_binding_and_rebuilds(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The symbol leaves the file, a quoted hash takes its place, and the build runs.

    `builtins.fetchurl` cannot carry this case: it yields a path rather than a
    derivation, so the rebuild after the rewrite has nothing to build. Issue #109.
    """
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = lib.fakeHash;
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    updated = nix_file.read_text()
    assert "lib.fakeHash" not in updated
    assert re.search(r'outputHash = "sha256[:-][A-Za-z0-9+/=]+"', updated)


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_and_rebuilds(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    assert 'outputHash = "sha256-' in nix_file.read_text()


@LINUX_CHROOT_BUILD
async def test_build_update_fod_finds_the_mismatch_after_a_long_build_log(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mismatch of a dependency comes last, after every line of the build log.

    A capture that holds a fixed number of records keeps the first ones and
    drops the mismatch. Measured on a cold store: 22 327 records dropped.
    """
    nix_file = tmp_path / "source.nix"
    await AnyioPath(nix_file).write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
let
  payload = runCommand "payload" {
    outputHash = lib.fakeHash;
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "for i in $(seq 3000); do echo line $i; done; printf '%s\\n' fixed-output-payload > $out";
in
runCommand "consumer" { } "cat ${payload} > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert "lib.fakeHash" not in await AnyioPath(nix_file).read_text()


@pytest.fixture
def ca_derivations() -> Iterator[None]:
    """`ca-derivations` for the process, put back after the test."""
    before = huggorm.get_setting("experimental-features") or ""
    huggorm.set_setting("experimental-features", f"{before} ca-derivations")
    yield
    huggorm.set_setting("experimental-features", before)


@LINUX_CHROOT_BUILD
@pytest.mark.usefixtures("ca_derivations")
async def test_build_update_fod_rewrites_a_dependency_that_builds_resolved(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fixed-output derivation with a floating content-addressed input builds resolved.

    Nix then reports the mismatch on the resolved derivation, a path that is
    in no closure of the target (`derivation-resolution-goal.cc:138`). A
    second fixed-output `payload` stops a match by name, as many `source`
    fetchers do in one nixpkgs closure. Issue nixidae/nixidae#1157.
    """
    nix_file = tmp_path / "source.nix"
    await AnyioPath(nix_file).write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
let
  input = runCommand "floating-input" { __contentAddressed = true; } "echo input > $out";
  payload = runCommand "payload" {
    inherit input;
    outputHash = lib.fakeHash;
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' fixed-output-payload > $out";
  other = runCommand "payload" {
    outputHash = "sha256-99smzF56LF3VXKUUxc3ITc/4WQpqcUrLQ0PCMyiKv+g=";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' other-payload > $out";
in
runCommand "consumer" { } "cat ${payload} ${other} > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 1
    assert data["outputs"]["out"]
    assert "lib.fakeHash" not in await AnyioPath(nix_file).read_text()


@LINUX_CHROOT_BUILD
async def test_build_update_fod_rewrites_each_named_run_command_dependency(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
let
  first = runCommand "first" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' first > $out";
  second = runCommand "second" {
    outputHash = "";
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  } "printf '%s\\n' second > $out";
in runCommand "combined" {} "cat ${first} ${second} > $out"
""",
            nixpkgs_path,
        ),
    )
    cmd = parse(["build", "--file", str(nix_file), "--update-fod", *shared_nix_environment.pynix_store_args()])

    await cmd.run()

    data = json.loads(capsys.readouterr().out)
    assert data["updatedFods"] == 2
    assert data["outputs"]["out"]
    assert nix_file.read_text().count('outputHash = "sha256-') == 2


async def test_build_update_fod_reports_an_ambiguous_hash_literal(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two identical empty hash literals with no derivation_name to
    disambiguate them (a fetchurl mismatch carries no drv_path) must refuse
    to guess, surfacing find_fod_hash_literal's ambiguity error."""
    payload_a = tmp_path / "payload-a"
    payload_a.write_text("payload a\n")
    payload_b = tmp_path / "payload-b"
    payload_b.write_text("payload b\n")
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        f'{{ a = builtins.fetchurl {{ url = "file://{payload_a}"; sha256 = ""; }};'
        f' b = builtins.fetchurl {{ url = "file://{payload_b}"; sha256 = ""; }}; }}\n',
    )
    cmd = parse(
        ["build", "--file", str(nix_file), "--attr", "a", "--update-fod", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot update fixed-output hash" in strip_ansi(captured.err)


@LINUX_CHROOT_BUILD
async def test_build_target_raises_when_mismatch_is_not_in_the_target_closure(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mismatch_is_target_fod returning False must abort the update instead
    of patching a hash literal for a derivation outside the evaluated
    target's own closure."""
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    target = EvaluationTarget(file=str(nix_file), attr=None, flake=None)

    async def _always_false(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(build_impl, "mismatch_is_target_fod", _always_false)

    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        with pytest.raises(BuildTargetError, match="not found in the evaluated target derivation closure"):
            await _build_target([target], BuildSession(*opened), update_fod=True, dry_run=False)


async def test_build_target_requires_a_file_target_to_update_a_fod(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """The CLI already refuses --update-fod without --file before evaluation
    (see test_build_update_fod_without_file_errors); this pins
    _build_target's own defense-in-depth check by calling it directly with a
    flake target, bypassing that earlier CLI guard."""
    payload = tmp_path / "payload"
    payload.write_text("fixed-output payload\n")
    flake_dir = tmp_path / "flake"
    flake_dir.mkdir()
    init_flake_repo(flake_dir, f'default = builtins.fetchurl {{ url = "file://{payload}"; sha256 = ""; }};')
    target = EvaluationTarget(file=None, attr=None, flake=f"{flake_dir}#default")

    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        with pytest.raises(BuildTargetError, match="--update-fod currently requires --file"):
            await _build_target([target], BuildSession(*opened), update_fod=True, dry_run=False)


@LINUX_CHROOT_BUILD
async def test_build_target_stops_after_ten_fixed_output_hash_updates(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replace_fod_hash that always writes back the same wrong hash
    simulates a FOD that never actually gets fixed, exercising the
    update-count cap instead of looping forever."""
    nix_file = tmp_path / "source.nix"
    nix_file.write_text(
        with_nixpkgs(
            """with import <nixpkgs> {};
runCommand "payload" {
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
} "printf '%s\\n' fixed-output-payload > $out"
""",
            nixpkgs_path,
        ),
    )
    target = EvaluationTarget(file=str(nix_file), attr=None, flake=None)

    def _rewrite_with_still_wrong_hash(source: str, literal: Any, _got: str) -> str:
        return _real_replace_fod_hash(source, literal, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    monkeypatch.setattr(build_impl, "replace_fod_hash", _rewrite_with_still_wrong_hash)

    async with _util.huggorm_build_session(shared_nix_environment.store_uri) as opened:
        with pytest.raises(BuildTargetError, match="stopped after 10 fixed-output hash updates"):
            await _build_target([target], BuildSession(*opened), update_fod=True, dry_run=False)
