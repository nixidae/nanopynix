from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from huggorm.errors import NixTypeError
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse
from support.ansi import strip_ansi


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


async def test_eval_expr(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--expr", "1 + 1", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == 2


async def test_eval_string(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--expr", '"hello"', *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == "hello"


async def test_eval_file(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; b = true; c = [ 1 2 3 ]; }")
    cmd = parse(["eval", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"a": 1, "b": True, "c": [1, 2, 3]}


async def test_eval_file_attr(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ nested = { answer = 42; }; }")
    cmd = parse(
        ["eval", "--file", str(nix_file), "--attr", "nested", *shared_nix_environment.pynix_store_args()],
    )

    await cmd.run()

    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == {"answer": 42}


async def test_eval_json_sorted_keys(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--expr", "{ z = 1; a = 2; }", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    result = _parse_json_output(captured.out)
    assert json.dumps(result, sort_keys=True, indent=2) + "\n" in captured.out


async def test_eval_attr_without_file_or_flake_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["eval", "--expr", "1", "--attr", "x"])
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "--attr requires --file or --flake" in captured.err


async def test_eval_expr_combined_with_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("1")
    cmd = parse(["eval", "--expr", "1", "--file", str(nix_file)])
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "expression argument cannot be combined with --file or --flake" in captured.err


async def test_eval_reads_expression_from_stdin(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("21 + 21"))
    cmd = parse(["eval", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert _parse_json_output(captured.out) == 42


async def test_eval_file_missing_attr_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ present = 1; }")
    cmd = parse(
        ["eval", "--file", str(nix_file), "--attr", "missing", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


async def test_eval_raw_prints_string_unquoted(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--raw", "--expr", '"a b"', *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    assert capsys.readouterr().out == "a b"


async def test_eval_raw_coerces_a_set_through_outpath(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--raw", "--expr", '{ outPath = "x"; }', *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    assert capsys.readouterr().out == "x"


@pytest.mark.parametrize(("expr", "type_name"), [("{ }", "a set"), ("1", "an integer")])
async def test_eval_raw_refuses_a_value_that_does_not_coerce(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    expr: str,
    type_name: str,
) -> None:
    cmd = parse(["eval", "--raw", "--expr", expr, *shared_nix_environment.pynix_store_args()])
    with pytest.raises(NixTypeError, match=f"cannot coerce {type_name} to a string"):
        await cmd.run()
    assert capsys.readouterr().out == ""


async def test_eval_apply_calls_the_function_on_the_result(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ b = 1; a = 2; }")
    cmd = parse(
        [
            "eval",
            "--file",
            str(nix_file),
            "--apply",
            "builtins.attrNames",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    assert _parse_json_output(capsys.readouterr().out) == ["a", "b"]


async def test_eval_apply_with_raw(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "eval",
            "--raw",
            "--expr",
            "[ 1 2 ]",
            "--apply",
            'l: builtins.concatStringsSep "," (map toString l)',
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    assert capsys.readouterr().out == "1,2"


async def test_eval_accepts_impure(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(["eval", "--impure", "--expr", "builtins ? currentSystem", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    assert _parse_json_output(capsys.readouterr().out) is True


_FUNCTION = '{ a ? "da", b ? "db", c }: { inherit a b c; }'


async def _eval_file(
    environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    nix_file: Path,
    *arguments: str,
) -> object:
    await parse(["eval", "--file", str(nix_file), *arguments, *environment.pynix_store_args()]).run()
    return _parse_json_output(capsys.readouterr().out)


async def test_eval_arg_and_argstr_call_the_file_with_defaults_for_the_rest(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--arg", "c", "1 + 1", "--argstr", "a", "s")
    assert got == {"a": "s", "b": "db", "c": 2}


async def test_eval_a_repeated_arg_keeps_the_last(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--argstr", "c", "1", "--argstr", "c", "2")
    assert got == {"a": "da", "b": "db", "c": "2"}


async def test_eval_arg_applies_before_attr_selects(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    assert await _eval_file(shared_nix_environment, capsys, nix_file, "--argstr", "c", "x", "--attr", "c") == "x"


async def test_eval_an_arg_the_function_does_not_take_is_not_evaluated(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix passes the thunk only to a formal that names it, so a `throw` stays unforced."""
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--argstr", "c", "x", "--arg", "z", 'throw "z"')
    assert got == {"a": "da", "b": "db", "c": "x"}


async def test_eval_arg_to_a_file_that_is_not_a_function_is_unused(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`nix eval --file f.nix --argstr a x` prints the set unchanged, with exit 0."""
    nix_file = tmp_path / "f.nix"
    nix_file.write_text("{ a = 1; }")
    assert await _eval_file(shared_nix_environment, capsys, nix_file, "--argstr", "a", "x") == {"a": 1}


async def test_eval_argstr_is_a_string_and_never_nix_source(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--argstr", "c", '"; throw "x"; "')
    assert got == {"a": "da", "b": "db", "c": '"; throw "x"; "'}


async def test_eval_argjson_passes_a_structured_value(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(
        shared_nix_environment, capsys, nix_file, "--argjson", "c", '{"x": [1, true, null], "y": 1.5}'
    )
    assert got == {"a": "da", "b": "db", "c": {"x": [1, True, None], "y": 1.5}}


async def test_eval_argjson_is_a_value_and_never_nix_source(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spliced into Nix source, the string would interpolate and throw (nixidae/nixidae#1255)."""
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--argjson", "c", r'"${throw \"x\"}"')
    assert got == {"a": "da", "b": "db", "c": '${throw "x"}'}


async def test_eval_args_json_gives_each_key_of_the_object(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--args-json", '{"c": [1], "a": "s"}')
    assert got == {"a": "s", "b": "db", "c": [1]}


async def test_eval_args_json_reads_the_file_after_an_at_sign(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "f.nix"
    nix_file.write_text(_FUNCTION)
    arguments = tmp_path / "arguments.json"
    arguments.write_text('{"c": {"n": 1}, "b": "${throw \\"b\\"}"}')
    got = await _eval_file(shared_nix_environment, capsys, nix_file, "--args-json", f"@{arguments}")
    assert got == {"a": "da", "b": '${throw "b"}', "c": {"n": 1}}
