"""``pynix flake check`` (nixidae/nixidae#111), held to ``nix flake check``.

The oracle is the set of failures each command reports under
``--keep-going``: the "while checking" frames and the error messages. Each
command visits the outputs in its own order, so without ``--keep-going`` the
two can stop at different failures; those tests ask for one error and a
failed exit.
"""

from __future__ import annotations

import re
from pathlib import Path

import huggorm
import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment
from structlog.testing import capture_logs

from pynix import parse
from pynix._impl.flake_check import CheckError
from support.ansi import strip_ansi
from support.nix_oracle import require_matching_nix_cli
from test_support.subprocess_output import run_process

_SUMMARY = "error: some errors were encountered during the evaluation"


def _system() -> str:
    system = huggorm.get_setting("system")
    assert system is not None
    return system


def _drv(name: str, script: str) -> str:
    return f'derivation {{ name = "{name}"; system = "{_system()}"; builder = "/bin/sh"; args = [ "-c" "{script}" ]; }}'


def _flake(tmp_path: Path, name: str, outputs: str) -> Path:
    flake = tmp_path / name
    flake.mkdir()
    (flake / "flake.nix").write_text(f"{{ outputs = _: {outputs}; }}\n", encoding="utf-8")
    return flake


@pytest.fixture
def good(tmp_path: Path) -> Path:
    system = _system()
    return _flake(
        tmp_path,
        "good",
        f"""{{
  checks.{system}.ok = {_drv(f"ok-{tmp_path.name}", "echo > $out")};
  packages.{system}.default = {_drv("pkg", "echo > $out")};
  packages.no-such-system.default = 1;
  overlays.default = final: prev: {{ }};
  apps.{system}.default = {{ type = "app"; program = "/bin/sh"; meta.description = "a shell"; }};
}}""",
    )


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    system = _system()
    return _flake(
        tmp_path,
        "broken",
        f"""{{
  packages.{system} = {{ broken = throw "boom"; notdrv = 1; }};
  overlays.default = x: x;
  checks.nosystem = {{ }};
}}""",
    )


@pytest.fixture
def failing(tmp_path: Path) -> Path:
    return _flake(
        tmp_path,
        "failing",
        f"{{ checks.{_system()}.bad = {_drv(f'bad-{tmp_path.name}', 'echo failing; exit 3')}; }}",
    )


async def _check(environment: NixTestEnvironment, flake: Path, *flags: str) -> int:
    try:
        await parse(["flake", "check", *flags, f"path:{flake}", *environment.pynix_store_args()]).run()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _failures(stderr: str) -> set[str]:
    """Each "while checking" frame and each error message, with no position."""
    found: set[str] = set()
    for line in strip_ansi(stderr).splitlines():
        if frame := re.match(r"\s*… (while checking .*)$", line):
            found.add(frame[1])
        elif (message := re.match(r"\s*error: (.+?)(?:, at /.*)?$", line)) and line.strip() != _SUMMARY:
            found.add(message[1])
    return found


_BROKEN_FAILURES = {
    "while checking the derivation 'packages.{system}.broken'",
    "boom",
    "while checking the derivation 'packages.{system}.notdrv'",
    "flake attribute 'packages.{system}.notdrv' is not a derivation",
    "while checking the overlay 'overlays.default'",
    "overlay does not take an argument named 'final'",
    "'nosystem' is not a valid system type",
}


@LINUX_CHROOT_BUILD
@pytest.mark.parametrize("flags", [[], ["--no-build"]], ids=["build", "no-build"])
async def test_a_good_flake_passes(
    shared_nix_environment: NixTestEnvironment, good: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    with capture_logs() as logs:
        assert await _check(shared_nix_environment, good, *flags) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error" not in captured.err
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warnings == [
        {
            "event": "the check omitted these incompatible systems; use --all-systems to check all",
            "systems": ["no-such-system"],
            "log_level": "warning",
        }
    ]


@pytest.mark.parametrize("flags", [[], ["--no-build"]], ids=["build", "no-build"])
async def test_a_broken_output_reports_every_failure_with_keep_going(
    shared_nix_environment: NixTestEnvironment, broken: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    assert await _check(shared_nix_environment, broken, "--keep-going", *flags) == 1
    err = capsys.readouterr().err
    assert _failures(err) == {failure.format(system=_system()) for failure in _BROKEN_FAILURES}
    assert strip_ansi(err).rstrip().endswith(_SUMMARY)


@pytest.mark.parametrize("flags", [[], ["--no-build"]], ids=["build", "no-build"])
async def test_a_broken_output_ends_the_check_without_keep_going(
    shared_nix_environment: NixTestEnvironment, broken: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """The first failure carries the frame of its output too, as Nix's does."""
    assert await _check(shared_nix_environment, broken, *flags) == 1
    err = strip_ansi(capsys.readouterr().err)
    assert err.count("error:") == 2, err
    assert re.search(r"… while checking flake output '\w+'", err), err
    assert _SUMMARY not in err


@LINUX_CHROOT_BUILD
@pytest.mark.parametrize("flags", [[], ["--keep-going"]], ids=["first", "keep-going"])
async def test_a_failing_check_fails_with_its_attribute_path(
    shared_nix_environment: NixTestEnvironment, failing: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    assert await _check(shared_nix_environment, failing, *flags) == 1
    err = strip_ansi(capsys.readouterr().err)
    assert re.search(
        rf"^error: failed to build attribute 'checks\.{re.escape(_system())}\.bad', build of '/\S+-bad-\S+\.drv\^\*' "
        r"failed: Cannot build '",
        err,
        re.MULTILINE,
    ), err


async def test_no_build_passes_a_failing_check(shared_nix_environment: NixTestEnvironment, failing: Path) -> None:
    assert await _check(shared_nix_environment, failing, "--no-build") == 0


async def test_no_build_writes_no_derivation(shared_nix_environment: NixTestEnvironment, failing: Path) -> None:
    with capture_logs() as logs:
        assert await _check(shared_nix_environment, failing, "--no-build", "--verbosity", "talkative") == 0
    evaluated = [entry["drv_path"] for entry in logs if entry["event"] == "pynix flake check: derivation evaluated"]
    assert len(evaluated) == 1, logs
    drv = evaluated[0]
    assert not shared_nix_environment.physical_path(drv).exists()
    assert huggorm.read_only_mode() is False


async def test_all_systems_checks_the_other_systems(
    shared_nix_environment: NixTestEnvironment, good: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other system's package is checked, and it is not a derivation."""
    assert await _check(shared_nix_environment, good, "--all-systems", "--no-build", "--keep-going") == 1
    assert "flake attribute 'packages.no-such-system.default' is not a derivation" in _failures(capsys.readouterr().err)


async def test_the_failures_are_the_ones_nix_reports(
    shared_nix_environment: NixTestEnvironment, broken: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await require_matching_nix_cli()
    result = await run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "check",
            "--keep-going",
            "--no-build",
            "--store",
            shared_nix_environment.store_uri,
            f"path:{broken}",
        ],
    )
    assert await _check(shared_nix_environment, broken, "--keep-going", "--no-build") == result.returncode == 1
    assert _failures(capsys.readouterr().err) == _failures(result.stderr)


def test_a_frame_goes_in_front_of_nix_s_frames() -> None:
    error = CheckError("\x1b[31;1merror:\x1b[0m\n       … while calling the 'throw' builtin\n\n       error: boom")
    error.add_trace("inner")
    error.add_trace("outer")
    assert strip_ansi(error.render()) == (
        "error:\n       … outer\n\n       … inner\n\n       … while calling the 'throw' builtin\n\n       error: boom"
    )


def test_a_frame_goes_above_a_one_line_error() -> None:
    error = CheckError.raised("overlay does not take an argument named 'final'")
    error.add_trace("while checking the overlay 'o'")
    assert strip_ansi(error.render()) == (
        "error:\n       … while checking the overlay 'o'\n\n       error: overlay does not take an argument named 'final'"
    )
