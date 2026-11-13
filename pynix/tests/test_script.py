"""``pynix`` as the interpreter of a script (``#!/usr/bin/env pynix``). nixidae/nixidae#1039.

The parser cases are Nix's own, from ``src/libutil-tests/args.cc``; the script
cases follow ``tests/functional/flakes/shebang.sh``.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import anyio
import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, with_nixpkgs

from pynix._script import ShebangError, command_base_dir_of, expand_script, parse_shebang_content
from pynix.target import resolve_file_reference
from support.child_env import child_environment

_CLI = "import sys, pynix; sys.argv[0] = 'pynix'; pynix.main()"


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("hi there", ["hi", "there"]),
        ("", []),
        ('``"ain\'t that nice"``', ['"ain\'t that nice"']),
        ("````", [""]),
        ("``# I'm markdown section about `coolFunction` ``", ["# I'm markdown section about `coolFunction`"]),
        ("``Example 1\n```nix\na: a\n``` ``", ["Example 1\n``nix\na: a\n``"]),
        ("``Example 1\n````nix\na: a\n```` ``", ["Example 1\n```nix\na: a\n```"]),
        (
            "``Example 1\n````nix\na: a\n````\nExample 2\n````nix\na: a\n```` ``",
            ["Example 1\n```nix\na: a\n```\nExample 2\n```nix\na: a\n```"],
        ),
        ("``` ``", ["`"]),
        ("```  ``", ["` "]),
        ("````` ``", ["``"]),
        ("```` ``` `` ````` `` `````` ``", ["", "`", "``", "```"]),
        ("--no-write-lock-file ``--command`` bash", ["--no-write-lock-file", "--command", "bash"]),
        ("a#b\n``two words``\n", ["a#b", "two words"]),
    ],
)
def test_parse_shebang_content_reads_as_nix(text: str, words: list[str]) -> None:
    assert parse_shebang_content(text) == words


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("shell *", "unsupported unquoted character in nix shebang: *. Use double backticks to escape?"),
        ("`x`", "single backtick is not a supported syntax in the nix shebang."),
        ("#x", "unquoted nix shebang argument cannot start with #. Use double backticks to escape?"),
        ("``open", "unterminated quoted string in nix shebang"),
    ],
)
def test_parse_shebang_content_refuses_as_nix(text: str, message: str) -> None:
    with pytest.raises(ShebangError) as caught:
        parse_shebang_content(text)
    assert str(caught.value) == message


def _escape(text: str) -> str:
    """The `escape` of Nix's round-trip property test, `src/libutil-tests/args.cc`."""
    out = "``"
    index = 0
    while index < len(text):
        if text.startswith("``", index):
            out += "`"
            while index < len(text) and text[index] == "`":
                out += "`"
                index += 1
        else:
            out += text[index]
            index += 1
    if out[-1] in "` ":
        out += " "
    return f"{out}``"


@pytest.mark.parametrize("text", ["", "`", "``", "a ``b`` c", " lead", "trail ", "x\ny", "```", "` `` ```"])
def test_parse_shebang_content_reads_back_what_escape_wrote(text: str) -> None:
    assert parse_shebang_content(_escape(text)) == [text]
    assert parse_shebang_content(f"{_escape(text)} {_escape('two')}") == [text, "two"]


def test_expand_script_leaves_a_command_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "build").mkdir()
    (tmp_path / "binary").write_bytes(b"\x7fELF")
    monkeypatch.chdir(tmp_path)

    for argv in (["build", "--file", "x"], ["binary"], ["absent", "a"], []):
        assert expand_script(argv).argv == argv
        assert expand_script(argv).base_dir is None


def test_expand_script_reads_the_pynix_lines(tmp_path: Path) -> None:
    script = tmp_path / "dir" / "script.sh"
    script.parent.mkdir()
    script.write_text(
        "#!/usr/bin/env pynix\n"
        "# a comment\n"
        "#! pynix run --file ./prog.nix\n"
        "#! nix --ignored\n"
        "-- another comment\n"
        "#!pynix: ``two words`` --\n"
        "echo body\n"
        "#! pynix --after-the-body\n"
    )

    expanded = expand_script([str(script), "a", "--b"])

    assert expanded.argv == ["run", "--file", "./prog.nix", "two words", "--", str(script), "a", "--b"]
    assert expanded.base_dir == script.parent


def test_expand_script_stops_at_an_empty_line(tmp_path: Path) -> None:
    script = tmp_path / "script"
    script.write_text("#!/usr/bin/env pynix\n#! pynix run\n\n#! pynix --late\n")

    assert expand_script([str(script)]).argv == ["run", str(script)]


def test_expand_script_with_no_pynix_line_runs_the_script_as_a_command(tmp_path: Path) -> None:
    script = tmp_path / "script"
    script.write_text("#!/usr/bin/env pynix\necho\n")

    assert expand_script([str(script), "a"]).argv == [str(script), "a"]


def test_expand_script_names_the_directory_of_a_relative_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "s").write_text("#!/usr/bin/env pynix\n")
    monkeypatch.chdir(tmp_path)

    assert expand_script(["s"]).base_dir == tmp_path


async def test_a_relative_file_names_a_file_from_the_base_directory(tmp_path: Path) -> None:
    await anyio.Path(tmp_path / "default.nix").write_text("1")

    with command_base_dir_of(tmp_path):
        found = await resolve_file_reference("./default.nix#x")
        absent = await resolve_file_reference("./absent.nix")
        rooted = await resolve_file_reference("/elsewhere.nix")

    assert found.arguments == (str(tmp_path / "default.nix"),)
    assert found.local_path == tmp_path / "default.nix"
    assert found.fragment == "x"
    assert absent.arguments == (str(tmp_path / "absent.nix"),)
    assert rooted.arguments == ("/elsewhere.nix",)


@LINUX_CHROOT_BUILD
async def test_a_script_runs_its_command_with_its_arguments(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
) -> None:
    """The script and its arguments reach the program, its status reaches the caller.

    The run starts in another directory, so ``./prog.nix`` names a file from
    the directory of the script.
    """
    scripts = tmp_path / "scripts"
    elsewhere = tmp_path / "elsewhere"
    await anyio.Path(scripts).mkdir()
    await anyio.Path(elsewhere).mkdir()
    await anyio.Path(scripts / "prog.nix").write_text(
        with_nixpkgs(
            f"""with import <nixpkgs> {{}};
writeShellScriptBin "prog" ''
  # {uuid.uuid4()}
  printf '%s\\n' "$@"
  exit 3
''
""",
            nixpkgs_path,
        )
    )
    store_args = " ".join(f"``{arg}``" for arg in shared_nix_environment.pynix_store_args())
    script = scripts / "script.sh"
    await anyio.Path(script).write_text(f"#!/usr/bin/env pynix\n#! pynix run --file ./prog.nix {store_args} --\necho\n")
    result = await anyio.run_process(
        [sys.executable, "-c", _CLI, str(script), "a", "b c"],
        cwd=elsewhere,
        env=child_environment(tmp_path),
        check=False,
    )

    assert result.returncode == 3, result.stderr.decode()
    assert result.stdout.decode().splitlines() == [str(script), "a", "b c"]


async def test_a_script_that_nix_refuses_fails_with_nix_words(tmp_path: Path) -> None:
    script = tmp_path / "script.sh"
    await anyio.Path(script).write_text("#!/usr/bin/env pynix\n#! pynix run *\n")

    result = await anyio.run_process([sys.executable, "-c", _CLI, str(script)], check=False)

    assert result.returncode == 1
    assert result.stderr.decode() == (
        "error: unsupported unquoted character in nix shebang: *. Use double backticks to escape?\n"
    )
