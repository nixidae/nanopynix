"""The four layers that give a repeated ``pynix`` option its default.

The flag, then the environment, then ``$XDG_CONFIG_HOME/pynix/config.toml``,
then the built-in default. One test for each boundary, and the tests that can
go through ``Pynix.parse`` do: ``ConfiguredCommand.__init__`` is what carries a
flag into the model, so a test that only built the model would pass while the
command line ignored the file.

``PYNIX_CONFIG`` points every test at its own ``tmp_path``. Without it a test
would read the configuration file of whoever runs the suite, and pass or fail
by that.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import anyio
import huggorm
import pytest

import nanopynix
from pynix import parse
from pynix._impl.build import (
    _resolve_namespaced,  # pyright: ignore[reportPrivateUsage] -- the refusal under test is inside this function
    _server_arguments,  # pyright: ignore[reportPrivateUsage] -- the argument list under test is private
)
from pynix._impl.settings import (
    DEFAULT_STORE,
    DEFAULTS,
    ConfigFileError,
    SettingError,
    configured_fields,
    nix_settings as resolve_nix_settings,
    pynix_defaults,
)
from pynix._util import nix_session
from pynix.build import Build

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from subprocess import CompletedProcess


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("PYNIX_CONFIG", str(path))
    return path


def no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at a file that does not exist, which is the ordinary case."""
    monkeypatch.setenv("PYNIX_CONFIG", str(tmp_path / "absent.toml"))


KNOWN = frozenset(huggorm.list_settings())


def nix_settings(**flags: str | None) -> dict[str, str]:
    return resolve_nix_settings(KNOWN, **flags)


def store() -> object:
    return pynix_defaults()["store"]


def build_command(*arguments: str) -> Build:
    """The `Build` that *arguments* names, resolved the way `main` resolves it.

    Narrowed to `Build`, because `parse` answers with whatever command the
    caller named and every test here names this one.
    """
    command = parse(["build", *arguments])
    assert isinstance(command, Build)
    return command


# ── one test for each boundary of the precedence order ───────────────


def test_the_built_in_default_applies_when_nothing_else_speaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_config(tmp_path, monkeypatch)

    assert store() == DEFAULT_STORE
    # `pynix` holds no Nix setting of its own. `nix build` holds none either,
    # and a value here would replace what the `nix.conf` of the host says --
    # which is issue #96.
    assert nix_settings() == {}


def test_the_file_beats_the_built_in_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\nverbosity = "notice"\nprint-build-logs = true\n'
        '\n[nix]\nsubstituters = ["https://file.example/"]\nmax-jobs = 8\n',
    )

    assert pynix_defaults() == {
        "store": "daemon",
        "eval_store": None,
        "verbosity": "notice",
        "print_build_logs": True,
    }
    assert nix_settings() == {"substituters": "https://file.example/", "max-jobs": "8"}


def test_the_environment_beats_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\n\n[nix]\nsubstituters = ["https://file.example/"]\n',
    )
    monkeypatch.setenv("PYNIX_STORE", "local")
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", "https://env.example/")

    assert store() == "local"
    assert nix_settings() == {"substituters": "https://env.example/"}


def test_the_flag_beats_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real parser, because clypi is what orders these two."""
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_STORE", "local")

    command = build_command("--store", "daemon")

    assert command.store == "daemon"
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", "https://env.example/")
    assert nix_settings(substituters="https://flag.example/") == {"substituters": "https://flag.example/"}


# ── the file reaches the parser, and not only the model ──────────────


def test_a_configured_store_becomes_the_default_of_a_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, '[defaults]\nstore = "daemon"\n')

    command = parse(["path-info", "/nix/store/x"])

    assert command.store == "daemon"  # type: ignore[attr-defined] -- see test_the_flag_beats_the_environment


def test_a_configured_flag_that_is_true_can_still_be_turned_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason ``--print-build-logs`` grew a negative.

    A boolean whose default a file can set to true is unreachable from the
    command line without one.
    """
    write_config(tmp_path, monkeypatch, "[defaults]\nprint-build-logs = true\n")

    assert build_command().print_build_logs is True  # type: ignore[attr-defined] -- see above
    assert build_command("--no-print-build-logs").print_build_logs is False  # type: ignore[attr-defined] -- see above


def test_every_sandbox_path_is_kept() -> None:
    """``--sandbox-path`` is repeatable, so a second one adds to the first."""
    command = build_command("--namespaced", "--sandbox-path", "/a=/x", "--sandbox-path", "/b=/y")

    assert command.sandbox_path == ["/a=/x", "/b=/y"]  # type: ignore[attr-defined] -- see above


def test_a_sandbox_path_adds_to_what_nix_conf_set() -> None:
    """Replacing the list took ``/bin/sh`` out of every such build, so a
    sandbox path goes to the ``extra-`` setting, after nix.conf and the
    configured settings."""
    arguments = _server_arguments("/r", {"cores": "2"}, ["/ccache=/home/me/cc", "/run/binfmt"])

    assert arguments == [
        "--overlay",
        "/r",
        "--load-config",
        "--option",
        "cores",
        "2",
        "--option",
        "extra-sandbox-paths",
        "/ccache=/home/me/cc /run/binfmt",
    ]


def test_a_configured_store_does_not_refuse_a_namespaced_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--namespaced`` owns its store, so naming one is a contradiction.

    A configured default is not a request about this build, and must not be
    read as one. ``_resolve_namespaced`` asks ``explicit_options``, which holds only
    what the command line named.
    """
    write_config(tmp_path, monkeypatch, '[defaults]\nstore = "daemon"\n')

    command = build_command("--namespaced")

    assert command.store == "daemon"  # type: ignore[attr-defined] -- see above
    assert _resolve_namespaced(command) is True  # type: ignore[arg-type] -- build_command returns the parsed Build

    named = build_command("--namespaced", "--store", "daemon")
    with pytest.raises(SystemExit):
        _resolve_namespaced(named)  # type: ignore[arg-type] -- see above


# ── what the command line named ──────────────────────────────────────


def test_every_configured_option_holds_a_value_after_the_command_is_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ConfiguredCommand.__init__`` is what fills in a configured option.

    A command that reached ``run()`` without it would pass ``None`` to the
    library. clypi needed an ``UNSET`` sentinel to tell "absent" from
    "explicitly false"; argparse says it with ``SUPPRESS``, so the sentinel is
    gone and this asks the question the other way round. Issue #214.
    """
    no_config(tmp_path, monkeypatch)

    command = build_command()

    # Compared against the defaults, and not against ``None``: ``--eval-store``
    # and ``--verbosity`` resolve to ``None`` on purpose, so "is not None" would
    # be false for two options that are perfectly resolved.
    fields = configured_fields(Build)
    assert fields
    for field in fields:
        assert getattr(command, field) == DEFAULTS[field], field


def test_explicit_holds_the_flags_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the environment nor the file puts a name in ``explicit_options``."""
    write_config(tmp_path, monkeypatch, '[defaults]\nverbosity = "notice"\n')
    monkeypatch.setenv("PYNIX_STORE", "local")

    assert build_command().explicit_options == frozenset()
    assert build_command("--store", "daemon").explicit_options == frozenset({"store"})


def test_the_nix_setting_called_store_does_not_resolve_the_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--store`` is the store a command opens, and Nix has a global of the
    same name, which holds a store directory. ``PYNIX_NIX_STORE`` names the
    second, and must not move the first.
    """
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_NIX_STORE", "/elsewhere")

    assert build_command().store == DEFAULT_STORE  # type: ignore[attr-defined] -- see above
    assert nix_settings() == {"store": "/elsewhere"}


# ── the regression that this work exists to fix ──────────────────────


def test_pynix_build_honours_the_substituters_of_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pynix build`` passed its own clypi defaults into the settings model.

    ``pydantic-settings`` ranks the init source above the environment, so
    ``PYNIX_NIX_SUBSTITUTERS`` never took effect once, for any value.
    """
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", "https://env.example/")
    command = build_command()

    settings = nix_settings(
        substituters=command.substituters,  # type: ignore[attr-defined] -- see above
        trusted_public_keys=command.trusted_public_keys,  # type: ignore[attr-defined] -- see above
    )

    assert settings == {"substituters": "https://env.example/"}


@pytest.mark.parametrize(
    ("variable", "expected"),
    [("PYNIX_NIX_CORES", {"cores": "9"}), ("cores", {})],
)
def test_only_the_prefixed_spelling_reaches_a_nix_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    expected: dict[str, str],
) -> None:
    """``PYNIX_NIX_`` is the documented prefix, and the only one that works."""
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, "9")

    assert nix_settings() == expected


def test_a_common_word_in_the_environment_changes_no_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every single-word Nix setting, set at once under its bare name, and none is read.

    ``stdenv`` exports ``system``, so the shell of Nix that runs this suite
    already sets one of these names.
    """
    no_config(tmp_path, monkeypatch)
    single_word = sorted(key for key in KNOWN if "-" not in key)
    for key in single_word:
        monkeypatch.setenv(key, "0")

    assert single_word, "no single-word key found; the scan is broken, not the resolution"
    assert nix_settings() == {}


def test_the_defaults_also_refuse_an_unprefixed_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``store`` is a common word, and it names which store a command opens."""
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("store", "/tmp/not-the-store")

    assert store() == DEFAULT_STORE


def test_both_tables_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        '[defaults]\nstore = "daemon"\n\n[nix]\nmax-jobs = 8\n',
    )

    assert store() == "daemon"
    assert nix_settings() == {"max-jobs": "8"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("Yes", True), ("on", True), ("0", False), ("false", False), ("off", False)],
)
def test_a_boolean_option_reads_the_spellings_of_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_PRINT_BUILD_LOGS", value)

    assert pynix_defaults()["print_build_logs"] is expected


def test_a_boolean_option_refuses_another_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_config(tmp_path, monkeypatch)
    monkeypatch.setenv("PYNIX_PRINT_BUILD_LOGS", "maybe")

    with pytest.raises(SettingError, match="PYNIX_PRINT_BUILD_LOGS"):
        pynix_defaults()


def test_a_string_option_refuses_another_kind_in_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, "[defaults]\nstore = 8\n")

    with pytest.raises(SettingError, match="store"):
        pynix_defaults()


@pytest.mark.parametrize(
    ("layer", "source"),
    [("file", "[nix]"), ("environment", "PYNIX_NIX_MAX_JOBZ"), ("flag", "--max-jobz")],
)
def test_an_unknown_nix_setting_is_refused_and_names_its_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    source: str,
) -> None:
    """A typo that pynix passes on is a setting a user believes is applied."""
    no_config(tmp_path, monkeypatch)
    flags: dict[str, str | None] = {}
    if layer == "file":
        write_config(tmp_path, monkeypatch, "[nix]\nmax-jobz = 8\n")
    elif layer == "environment":
        monkeypatch.setenv("PYNIX_NIX_MAX_JOBZ", "8")
    else:
        flags["max_jobz"] = "8"

    with pytest.raises(SettingError, match=rf"max-jobz \({re.escape(source)}"):
        nix_settings(**flags)


async def _pynix(tmp_path: Path, *arguments: str, **environment: str) -> CompletedProcess[bytes]:
    script = f"import sys; from pynix import main; sys.argv = ['pynix', *{list(arguments)!r}]; main()"
    env = {**os.environ, "PYNIX_CONFIG": str(tmp_path / "absent.toml"), **environment}
    return await anyio.run_process([sys.executable, "-c", script], env=env, cwd=tmp_path, check=False)


async def test_a_nix_setting_from_the_environment_reaches_an_evaluation(tmp_path: Path) -> None:
    """``PYNIX_NIX_*`` reached ``pynix build`` only (nixidae/nixidae#1055)."""
    result = await _pynix(tmp_path, "eval", "--expr", "builtins.currentSystem", PYNIX_NIX_SYSTEM="riscv64-linux")

    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == "riscv64-linux"


@pytest.mark.parametrize(
    "arguments",
    [
        ("eval", "--expr", "1"),
        ("path-info", "/nix/store/00000000000000000000000000000000-absent"),
        ("run", "--file", "./x.nix"),
        ("develop", "--file", "./x.nix", "--", "true"),
        ("print-dev-env", "--file", "./x.nix"),
        ("sbom", "--file", "./x.nix"),
        ("config", "show"),
        ("flake", "metadata", "./flake"),
    ],
    ids=lambda arguments: arguments[0],
)
async def test_every_command_refuses_an_unknown_nix_setting(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    """Each command reads the configured Nix settings, so each one refuses a typo."""
    await anyio.Path(tmp_path / "x.nix").write_text("1\n")

    result = await _pynix(tmp_path, *arguments, PYNIX_NIX_MAX_JOBZ="8")

    assert result.returncode != 0
    assert "no such Nix setting: max-jobz (PYNIX_NIX_MAX_JOBZ)" in result.stderr.decode()


# ── what the file may say, and what it may not ───────────────────────


def test_a_missing_configuration_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_config(tmp_path, monkeypatch)

    assert store() == DEFAULT_STORE


def test_a_file_with_neither_table_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '[something-else]\nkey = "value"\n')

    assert store() == DEFAULT_STORE


def test_a_file_that_does_not_parse_names_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, monkeypatch, "[defaults\nstore =\n")

    with pytest.raises(ConfigFileError, match=str(path)):
        pynix_defaults()


def test_a_table_that_is_not_a_table_names_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, 'defaults = "daemon"\n')

    with pytest.raises(ConfigFileError, match=r"\[defaults\]"):
        pynix_defaults()


def test_an_unknown_key_is_reported_and_not_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo that the file ignores is a setting a user believes is applied."""
    write_config(tmp_path, monkeypatch, '[defaults]\nstor = "daemon"\n')

    with pytest.raises(SettingError, match="stor"):
        pynix_defaults()


def test_the_file_takes_the_nix_conf_spelling_of_a_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A TOML array is the natural spelling here, and the other one still works."""
    write_config(tmp_path, monkeypatch, '[nix]\nsubstituters = "https://a.example/ https://b.example/"\n')

    assert nix_settings() == {"substituters": "https://a.example/ https://b.example/"}


def test_a_toml_array_becomes_the_nix_conf_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        tmp_path, monkeypatch, '[nix]\nsubstituters = ["https://a.example/", "https://b.example/"]\nsandbox = false\n'
    )

    assert nix_settings() == {"substituters": "https://a.example/ https://b.example/", "sandbox": "false"}


# ── nix_session engine selection ─────────────────────────────────────


@pytest.mark.anyio
async def test_nix_session_defaults_to_inproc_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI command defaults to in-process evaluation without worker overhead."""
    recorded: list[str] = []

    class FakeInproc:
        def __init__(self, **kwargs: object) -> None:
            recorded.append("inproc")

        async def __aenter__(self) -> FakeInproc:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def log_stream(self) -> AsyncIterator[nanopynix.models.LogEvent]:
            async def _empty() -> AsyncIterator[nanopynix.models.LogEvent]:
                if False:
                    yield  # type: ignore[unreachable] -- generator typing helper

            return _empty()

    monkeypatch.setattr(nanopynix.inproc, "Session", FakeInproc)
    async with nix_session():
        pass
    assert recorded == ["inproc"]


@pytest.mark.anyio
async def test_nix_session_uses_rpc_when_namespace_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlay namespace requires process isolation, so it uses RPC."""
    recorded: list[str] = []

    class FakeRpc:
        def __init__(self, **kwargs: object) -> None:
            recorded.append("rpc")

        async def __aenter__(self) -> FakeRpc:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def log_stream(self) -> AsyncIterator[nanopynix.models.LogEvent]:
            async def _empty() -> AsyncIterator[nanopynix.models.LogEvent]:
                if False:
                    yield  # type: ignore[unreachable] -- generator typing helper

            return _empty()

    monkeypatch.setattr(nanopynix.rpc, "Session", FakeRpc)
    namespace = MagicMock(spec=nanopynix.OverlayNamespace)
    async with nix_session(namespace=namespace):
        pass
    assert recorded == ["rpc"]
