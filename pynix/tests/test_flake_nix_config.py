"""A flake's ``nixConfig``, applied as Nix applies it (nixidae/nixidae#1284).

``InstallableFlake::getLockedFlake``, ``CmdFlakeLock`` and ``CmdFlakeUpdate``
set ``applyNixConfig``. The flake here sets ``max-call-depth``, which no
whitelist trusts, so it needs ``accept-flake-config``, and ten calls are too
deep for it.

Each test has its own store, and so its own evaluator: a setting stays on
the evaluator that applied it, and the local fast path shares one per store.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import huggorm
import pytest
from huggorm.errors import NixError
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse

_FLAKE = """{
  nixConfig.max-call-depth = 5;
  outputs = _: { deep = let f = n: if n == 0 then 0 else f (n - 1); in f 10; };
}
"""


@pytest.fixture
def shallow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The flake, with the host's ``trusted-settings.json`` kept out."""
    monkeypatch.setenv("NIX_DATA_HOME", str(tmp_path / "data"))
    flake = tmp_path / "shallow"
    flake.mkdir()
    (flake / "flake.nix").write_text(_FLAKE, encoding="utf-8")
    return flake


@pytest.fixture
def accept() -> Iterator[None]:
    before = huggorm.get_setting("accept-flake-config")
    assert before is not None
    huggorm.set_setting("accept-flake-config", "true")
    yield
    huggorm.set_setting("accept-flake-config", before)


async def _eval_deep(environment: NixTestEnvironment, flake: Path) -> None:
    await parse(["eval", "--flake", f"path:{flake}#deep", *environment.pynix_store_args()]).run()


@pytest.mark.usefixtures("accept")
async def test_an_accepted_nix_config_applies_to_the_evaluation(
    isolated_nix_environment: NixTestEnvironment, shallow: Path
) -> None:
    with pytest.raises(NixError, match="max-call-depth exceeded"):
        await _eval_deep(isolated_nix_environment, shallow)


async def test_an_untrusted_nix_config_is_ignored_with_nix_s_warning(
    isolated_nix_environment: NixTestEnvironment, shallow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await _eval_deep(isolated_nix_environment, shallow)
    captured = capsys.readouterr()
    assert captured.out.strip() == "0"
    assert "ignoring untrusted flake configuration setting 'max-call-depth'" in captured.err


@pytest.mark.parametrize("command", [["flake", "lock"], ["flake", "update", "--flake"]], ids=["lock", "update"])
async def test_flake_lock_and_update_read_the_nix_config(
    isolated_nix_environment: NixTestEnvironment,
    shallow: Path,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    """The warning shows that the command read ``nixConfig``; untrusted, it sets nothing."""
    await parse([*command, str(shallow), *isolated_nix_environment.pynix_store_args()]).run()
    assert "ignoring untrusted flake configuration setting 'max-call-depth'" in capsys.readouterr().err
