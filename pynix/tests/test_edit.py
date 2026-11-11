"""``pynix edit`` (nixidae/nixidae#118), as ``CmdEdit`` opens a package.

``edit`` ends in ``exec``, so the end-to-end tests run ``pynix`` in a
subprocess, with a fake editor that writes its arguments to a file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from huggorm.testing.environment import NixTestEnvironment

from pynix._impl.edit import editor_argv


@pytest.mark.parametrize(
    ("editor", "expected"),
    [
        ("vim", ["vim", "+3", "/f.nix"]),
        ("emacsclient -t", ["emacsclient", "-t", "+3", "/f.nix"]),
        ("code --wait", ["code", "--wait", "/f.nix"]),
        ("", ["cat", "/f.nix"]),
    ],
)
def test_the_editor_gets_the_line_as_nix_passes_it(editor: str, expected: list[str]) -> None:
    assert editor_argv("/f.nix", 3, {"EDITOR": editor}) == expected


def test_no_editor_is_cat_and_line_zero_is_the_whole_file() -> None:
    assert editor_argv("/f.nix", 0, {}) == ["cat", "/f.nix"]
    assert editor_argv("/f.nix", 0, {"EDITOR": "vim"}) == ["vim", "/f.nix"]


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    source = tmp_path / "default.nix"
    source.write_text(
        "{\n"
        '  placed = { type = "derivation"; name = "placed"; meta.position = "' + str(tmp_path / "pkg.nix") + ':4"; };\n'
        '  nowhere = { type = "derivation"; name = "nowhere"; };\n'
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg.nix").write_text("\n\n\n{ }\n", encoding="utf-8")
    return source


def _edit(store_uri: str, editor: str, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "edit",
        "--store",
        store_uri,
        *args,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv, capture_output=True, text=True, check=False, env={**os.environ, "EDITOR": editor}
    )


def _fake_vim(tmp_path: Path) -> tuple[Path, Path]:
    """An editor named ``vim``, which writes its arguments to ``args``."""
    record = tmp_path / "args"
    editor = tmp_path / "fakevim"
    editor.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n', encoding="utf-8")
    editor.chmod(0o755)
    return editor, record


def test_edit_opens_the_file_at_the_line(
    shared_nix_environment: NixTestEnvironment, packages: Path, tmp_path: Path
) -> None:
    editor, record = _fake_vim(tmp_path)
    result = _edit(shared_nix_environment.store_uri, str(editor), "--file", str(packages), "--attr", "placed")
    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == ["+4", str(tmp_path / "pkg.nix")]


def test_edit_names_a_package_with_no_position(
    shared_nix_environment: NixTestEnvironment, packages: Path, tmp_path: Path
) -> None:
    editor, record = _fake_vim(tmp_path)
    result = _edit(shared_nix_environment.store_uri, str(editor), "--file", str(packages), "--attr", "nowhere")
    assert result.returncode == 1
    assert "cannot find position information for 'nowhere'" in " ".join(result.stderr.split())
    assert not record.exists()
