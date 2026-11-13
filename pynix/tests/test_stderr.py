"""stdout holds the result, stderr everything else, and stderr says only what is news.

A caller can filter stderr only because nothing else is on stdout. A caller
stops filtering it only if it stays quiet: a build that changes nothing prints
nothing, as ``nix build`` prints nothing. Issue nixidae/nixidae#863.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
import pytest
import structlog
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment

import pynix
import pynix._impl.main as main_module
import pynix._util as util_module
from pynix import parse
from pynix._build_monitor import MonitorState
from support.child_env import child_environment

_CLI = "import sys, pynix; sys.argv[0] = 'pynix'; pynix.main()"


def _derivation(tmp_path: Path) -> Path:
    # `${./input}` copies a file at every evaluation, which Nix reports as a
    # chatty activity: 34 of the 36 lines that #863 counted were these.
    (tmp_path / "input").write_text(tmp_path.name)
    nix_file = tmp_path / "quiet.nix"
    nix_file.write_text(
        """
derivation {
  name = "pynix-quiet-stderr";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "read x < ${./input}; echo $x > $out" ];
}
"""
    )
    return nix_file


@LINUX_CHROOT_BUILD
async def test_a_build_that_changes_nothing_prints_only_the_result(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["build", "--file", str(_derivation(tmp_path)), *shared_nix_environment.pynix_store_args()]
    await parse(argv).run()
    capsys.readouterr()

    await parse(argv).run()

    captured = capsys.readouterr()
    assert "pynix-quiet-stderr" in json.loads(captured.out)["outputs"]["out"]
    assert captured.err == ""


@LINUX_CHROOT_BUILD
async def test_a_higher_verbosity_shows_the_activities(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control for the case above: the copy is there to hide."""
    argv = ["build", "--verbosity", "chatty", "--file", str(_derivation(tmp_path))]
    await parse([*argv, *shared_nix_environment.pynix_store_args()]).run()

    assert "to the store...\n" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "shown"), [([], False), (["--verbosity", "talkative"], True)], ids=["default", "talkative"]
)
def test_the_verbosity_gates_an_event_before_the_command_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    shown: bool,
) -> None:
    """`--verbosity` applies from the start of the command, and not only once
    an evaluator opens (nixidae/nixidae#1224)."""
    monkeypatch.setenv("PYNIX_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("PYNIX_VERBOSITY", raising=False)
    monkeypatch.setattr(sys, "argv", ["pynix", "build", *flags, "--expr", "1"])
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(main_module, "run", lambda _body: structlog.get_logger().info("an event before evaluation"))
    old_config = structlog.get_config()
    try:
        pynix.main()
    finally:
        structlog.configure(**old_config)

    assert ("an event before evaluation" in capsys.readouterr().err) is shown


async def test_a_warning_prints_as_nix_prints_it(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`SimpleLogger::logEI` prints `showErrorInfo`: the prefix, the position
    and the trace, and not the message alone (nixidae/nixidae#1313)."""
    argv = ["eval", "--expr", 'builtins.warn "hello there" 1', *shared_nix_environment.pynix_store_args()]
    await parse(argv).run()

    assert "evaluation warning: hello there\n" in capsys.readouterr().err


async def test_the_monitor_prints_a_warning_and_counts_no_error(shared_nix_environment: NixTestEnvironment) -> None:
    """A warning comes through `logEI` as an error does, with its parts in
    `info`. Only its level tells the two apart (nixidae/nixidae#1313)."""
    async with (
        util_module.huggorm_eval_session(shared_nix_environment.store_uri) as (session, _store, state),
        session.capture(state) as logs,
    ):
        await state.force(await state.eval_expr('builtins.warn "hello there" 1'))

    monitor = MonitorState(started=0.0)
    printed = [
        line.plain
        for record in logs.records
        if (event := util_module.monitor_event(record)) is not None
        for line in monitor.apply(event, 0.0)
    ]
    assert "evaluation warning: hello there" in printed
    assert monitor.errors == 0


@LINUX_CHROOT_BUILD
@pytest.mark.parametrize("print_build_logs", [False, True], ids=["quiet", "logs"])
async def test_the_command_prints_a_build_log_only_when_asked(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    print_build_logs: bool,
) -> None:
    """The real command, in a process of its own, with nothing shared.

    A build logs on huggorm's process stream alone. Unread, that stream goes to
    a fallback logger at CHATTY, which prints the builder's own output whether
    or not the caller asked for it. Issue nixidae/nixidae#1053.
    """
    # A name for each case, because the cases share a store.
    name = f"pynix-build-log-{print_build_logs}".lower()
    nix_file = tmp_path / "loud.nix"
    nix_file.write_text(
        f"""
derivation {{
  name = "{name}";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo from-the-builder; echo > $out" ];
}}
"""
    )
    argv = [sys.executable, "-c", _CLI, "build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()]
    if print_build_logs:
        argv.append("--print-build-logs")

    result = await anyio.run_process(argv, env=child_environment(tmp_path), check=False)

    err = result.stderr.decode()
    assert result.returncode == 0, err
    assert name in json.loads(result.stdout)["outputs"]["out"]
    assert re.search(rf"^building '/\S+-{name}\.drv'\.\.\.$", err, re.MULTILINE), err
    assert ("from-the-builder" in err) is print_build_logs, err
