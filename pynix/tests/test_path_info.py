from __future__ import annotations

import json

import huggorm
import pytest
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse
from pynix._util import init_nix


@pytest.fixture
async def seeded_store_path(shared_nix_environment: NixTestEnvironment) -> str:
    """A content-addressed path of this suite's own, never one borrowed from the host store."""
    init_nix(load_config=False)
    async with huggorm.AsyncSession() as session:
        store = session.store(shared_nix_environment.store_uri)
        added = await store.add_to_store(
            "pynix-test-fixture", b"pynix hermetic fixture\n", method=huggorm.ContentAddressMethod.FLAT
        )
        return await store.print_store_path(added)


async def test_path_info(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = seeded_store_path

    cmd = parse(["path-info", path, *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["path"] == path
    assert isinstance(result["narHash"], str)
    assert isinstance(result["narSize"], int)
    assert result["narSize"] > 0
    assert isinstance(result["references"], list)


async def test_path_info_nonexistent(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure goes to stderr, and stdout stays empty.

    The output of this command is JSON, so a caller writes
    ``pynix path-info ... | jq``. This test asserted ``captured.out`` until
    the error moved, and the message it asserted was the one that reached
    ``jq`` instead of the JSON. ``_util.error_console`` carries the rule.
    """
    cmd = parse(
        ["path-info", "/nix/store/deadbeef-nonexistent", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()

    assert "Error" in captured.err
    assert captured.out == ""
    # The message of Nix arrives whole. `error_exit` turns it into a `Text`,
    # which keeps the colour of Nix for a terminal and drops it for a pipe.
    # Interpolating the same text into a markup string instead breaks it: the
    # highlighter of rich styles the `[` and the number of each escape, which
    # orphans the escape byte. `_util.error_exit` carries the measurement.
    assert "deadbeef-nonexistent" in captured.err
    assert "error:" in captured.err
