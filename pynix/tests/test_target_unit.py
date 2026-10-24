"""Direct unit tests for pynix.target and the evaluation in pynix._evaluate.

Real CLI integration tests (test_build.py, test_eval.py, test_flake_show.py)
only ever exercise evaluate_target on their respective happy paths. These
tests pin down the auto_call_file=False branch, the fragment and --attr
together, and evaluate_target's already-validate()-guarded "no target" branch
(kept as defense in depth, so still worth pinning even though it's unreachable
through the public validate()-first call path).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import huggorm
import pytest
from huggorm.errors import ThrownError

from pynix._evaluate import (
    app_attr_search,
    base_attr_search,
    dev_shell_attr_search,
    evaluate_target,
    formatter_attr_search,
    open_file_reference,
    repl_attr_search,
    to_python,
)
from pynix.target import (
    CONFIGURATION_KINDS,
    EvaluationTarget,
    EvaluationTargetError,
    FileReference,
    configuration_message,
    resolve_file_reference,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from huggorm import AsyncEvalState, AsyncValue


@pytest.fixture
async def state() -> AsyncGenerator[AsyncEvalState]:
    """An evaluator over `dummy://`: these tests read local files and copy nothing."""
    async with huggorm.AsyncSession("dummy://") as session:
        yield session.eval(session.store())


async def test_evaluate_target_with_a_file_skips_auto_call_by_default(
    state: AsyncEvalState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.nix").write_text("{ x ? 1 }: { a = x; }")
    target = EvaluationTarget(file="./x.nix", attr=None, flake=None)

    result = await evaluate_target(target, state)

    assert await result.is_lambda()


async def test_evaluate_target_raises_if_flake_is_missing_after_bypassing_validate(
    state: AsyncEvalState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate(required=True) always runs first in practice and would
    already raise "either --file or --flake is required" before this branch;
    bypass it (on the frozen dataclass's class, since instances can't be
    patched) to pin the defensive fallback directly."""

    def _no_op_validate(_self: EvaluationTarget, *, required: bool = False) -> None:
        del required

    target = EvaluationTarget(file=None, attr=None, flake=None)
    monkeypatch.setattr(EvaluationTarget, "validate", _no_op_validate)

    with pytest.raises(EvaluationTargetError, match="either --file or --flake is required"):
        await evaluate_target(target, state)


# --- resolve_file_reference -------------------------------------------------
#
# The six rules of the docstring, one test each, plus the two errors. The rule
# that decides an argument is not visible in the value that comes back, so each
# test names the rule it covers.


async def test_a_local_file_keeps_its_whole_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 1: a name that exists is a path, '#' and all."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "weird#name.nix").write_text("1")

    reference = await resolve_file_reference("weird#name.nix")

    assert reference.arguments == ("weird#name.nix",)
    assert reference.fragment is None
    assert reference.local_path == Path("weird#name.nix")


async def test_a_local_file_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2: the part before the first '#' exists, so the rest is a fragment."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")

    reference = await resolve_file_reference("default.nix#packages.hello")

    assert reference.arguments == ("default.nix",)
    assert reference.fragment == "packages.hello"
    assert reference.local_path == Path("default.nix")


async def test_a_local_directory_wins_over_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 2 beats rule 6: 'nixpkgs' here is the directory, not the flake."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nixpkgs").mkdir()

    reference = await resolve_file_reference("nixpkgs")

    assert reference.arguments == ("nixpkgs",)
    assert reference.local_path == Path("nixpkgs")


@pytest.mark.parametrize("raw", ["./missing.nix", "../missing.nix", "/etc/missing.nix", "~/missing.nix"])
async def test_a_written_path_stays_a_path_when_it_is_absent(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 3: the evaluator reports a missing file, and not a missing flake."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (raw,)
    assert reference.local_path is None


async def test_a_written_path_still_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("./missing.nix#a.b")

    assert reference.arguments == ("./missing.nix",)
    assert reference.fragment == "a.b"


@pytest.mark.parametrize(
    "raw",
    [
        "<nixpkgs>",
        "channel:nixos-unstable",
        "https://example.com/x.tar.gz",
        "flake:nixpkgs",
    ],
)
async def test_the_evaluator_keeps_what_it_resolves_itself(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules 4 and 5: lookup_file_arg has a branch for each of these."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (raw,)
    assert reference.local_path is None


async def test_a_pseudo_url_survives_its_double_slash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect that a `Path` annotation caused: 'https://' became 'https:/'."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("https://example.com/x.tar.gz")

    assert reference.arguments == ("https://example.com/x.tar.gz",)


@pytest.mark.parametrize(
    ("raw", "argument"),
    [
        ("github:NixOS/nixpkgs", "flake:github:NixOS/nixpkgs"),
        ("git+https://example.com/x", "flake:git+https://example.com/x"),
        ("path:/tmp/tree", "flake:path:/tmp/tree"),
    ],
)
async def test_a_reference_that_carries_a_scheme_goes_straight_to_the_registry(
    raw: str, argument: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 6: the lookup path never holds a name such as `github:owner/repo`."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (argument,)
    assert reference.fragment is None
    assert reference.local_path is None


@pytest.mark.parametrize("raw", ["nixpkgs", "nixpkgs/nixos-25.05"])
async def test_a_bare_name_asks_the_lookup_path_before_the_registry(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 7: `--file` is the old-style door, and NIX_PATH is how a name became
    a tree before flakes existed."""
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference(raw)

    assert reference.arguments == (f"<{raw}>", f"flake:{raw}")
    assert reference.local_path is None


async def test_a_reference_splits_its_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("github:NixOS/nixpkgs/nixos-25.05#lib.version")

    assert reference.arguments == ("flake:github:NixOS/nixpkgs/nixos-25.05",)
    assert reference.fragment == "lib.version"


async def test_an_empty_fragment_is_no_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    reference = await resolve_file_reference("nixpkgs#")

    assert reference.arguments == ("<nixpkgs>", "flake:nixpkgs")
    assert reference.fragment is None


async def test_an_empty_argument_is_an_error() -> None:
    with pytest.raises(EvaluationTargetError, match="needs a value"):
        await resolve_file_reference("")


async def test_a_bare_fragment_is_an_error() -> None:
    with pytest.raises(EvaluationTargetError, match="no reference before"):
        await resolve_file_reference("#lib.version")


async def test_a_dash_is_standard_input_even_beside_a_file_named_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'-f -' reads an expression from stdin, as in Nix, and never a file named '-'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-").write_text("{ }")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{ a = 1; }"))

    reference = await resolve_file_reference("-#a")

    assert (reference.expression, reference.fragment, reference.arguments) == ("{ a = 1; }", "a", ())


async def test_standard_input_is_read_once_for_every_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build`` resolves ``--file`` twice, and a second read of a pipe is empty."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("1 + 1"))

    first = await resolve_file_reference("-")
    second = await resolve_file_reference("-")

    assert first.expression == second.expression == "1 + 1"


async def test_the_target_resolves_its_own_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ }")

    target = EvaluationTarget(file="default.nix#a", attr=None, flake=None)
    reference = await target.file_reference()

    assert reference is not None
    assert reference.fragment == "a"
    assert await EvaluationTarget(file=None, attr=None, flake="x").file_reference() is None


async def test_a_fragment_selects_after_the_file(
    state: AsyncEvalState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragment selects first, and --attr still applies on top."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "default.nix").write_text("{ a.b = 42; }")

    target = EvaluationTarget(file="default.nix#a", attr="b", flake=None)
    result = await evaluate_target(target, state)

    assert await to_python(result) == 42


# --- the attribute-path search of each command ------------------------------
#
# One test for each command that copies a `nix` subcommand. The lists that
# `nix` uses are in `src/libcmd/installables.cc`, `src/nix/develop.cc`,
# `src/nix/run.cc` and `src/nix/formatter.cc`, and each test names the order
# that the matching file decides.


def test_the_base_search_matches_source_expr_command() -> None:
    system = huggorm.current_system()
    search = base_attr_search()

    assert search.prefixes == (f"packages.{system}.", f"legacyPackages.{system}.")
    assert search.defaults == (f"packages.{system}.default", f"defaultPackage.{system}")


def test_the_dev_shell_search_puts_dev_shells_in_front() -> None:
    system = huggorm.current_system()
    search = dev_shell_attr_search()

    assert search.prefixes == (
        f"devShells.{system}.",
        f"packages.{system}.",
        f"legacyPackages.{system}.",
    )
    assert search.defaults == (
        f"devShells.{system}.default",
        f"devShell.{system}",
        f"packages.{system}.default",
        f"defaultPackage.{system}",
    )


def test_the_app_search_puts_apps_in_front() -> None:
    system = huggorm.current_system()
    search = app_attr_search()

    assert search.prefixes[0] == f"apps.{system}."
    assert search.prefixes[1:] == base_attr_search().prefixes
    assert search.defaults[:2] == (f"apps.{system}.default", f"defaultApp.{system}")
    assert search.defaults[2:] == base_attr_search().defaults


def test_the_formatter_search_has_no_prefix() -> None:
    system = huggorm.current_system()
    search = formatter_attr_search()

    assert search.prefixes == ()
    assert search.defaults == (f"formatter.{system}",)


def test_the_repl_search_defaults_to_the_root() -> None:
    """`CmdRepl` overrides its defaults to one empty path, and keeps the prefixes.

    An empty path selects the outputs themselves, so `pynix repl --flake <ref>`
    puts every output into scope. The prefixes still apply to a fragment.
    """
    search = repl_attr_search()

    assert search.defaults == ("",)
    assert search.prefixes == base_attr_search().prefixes
    assert search.candidates(None) == ("",)
    assert search.candidates("hello")[-1] == "hello"


# --- open_file_reference ----------------------------------------------------
#
# The candidate list of a bare name reaches the evaluator one at a time, and
# only a miss in the lookup path moves to the next one. These tests drive the
# opener with a double, so each branch is reachable without a lookup path that
# the test machine happens to hold.


def _search_path_miss(name: str) -> ThrownError:
    """The error `EvalState::findFile` raises when the lookup path has no *name*."""
    return ThrownError(f"error: file '{name}' was not found in the Nix search path (add it using $NIX_PATH or -I)")


class _RecordingOpener:
    """An opener that answers from a table, and remembers what it was asked."""

    def __init__(self, answers: dict[str, AsyncValue | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def __call__(self, candidate: str) -> AsyncValue:
        self.asked.append(candidate)
        answer = self.answers.get(candidate)
        if answer is None:
            raise _search_path_miss(candidate.strip("<>"))
        if isinstance(answer, Exception):
            raise answer
        return answer


_BARE_NAME = FileReference(arguments=("<nixpkgs>", "flake:nixpkgs"), fragment=None, local_path=None)


async def test_the_lookup_path_answers_before_the_registry(state: AsyncEvalState) -> None:
    value = await state.eval_expr("1")
    opener = _RecordingOpener({"<nixpkgs>": value})

    assert await open_file_reference(_BARE_NAME, opener) is value
    assert opener.asked == ["<nixpkgs>"]


async def test_the_registry_answers_when_the_lookup_path_has_no_such_name(state: AsyncEvalState) -> None:
    value = await state.eval_expr("1")
    opener = _RecordingOpener({"flake:nixpkgs": value})

    assert await open_file_reference(_BARE_NAME, opener) is value
    assert opener.asked == ["<nixpkgs>", "flake:nixpkgs"]


async def test_a_found_name_that_fails_to_evaluate_keeps_its_own_error() -> None:
    """The reason the class alone is not enough to decide the fallback.

    `builtins.throw` raises `ThrownError` too, so a `<nixpkgs>` that the lookup
    path holds and whose file then rejects its arguments must report that, and
    not a message about a flake.
    """
    opener = _RecordingOpener({"<nixpkgs>": ThrownError("error: this expression refuses to evaluate")})

    with pytest.raises(ThrownError, match="refuses to evaluate"):
        await open_file_reference(_BARE_NAME, opener)

    assert opener.asked == ["<nixpkgs>"]


async def test_the_last_error_survives_when_no_candidate_answers() -> None:
    opener = _RecordingOpener({})

    with pytest.raises(ThrownError, match="Nix search path"):
        await open_file_reference(_BARE_NAME, opener)

    assert opener.asked == ["<nixpkgs>", "flake:nixpkgs"]


def test_the_selected_attribute_joins_a_fragment_and_an_attr() -> None:
    """The path can arrive in two places at once, and the hint names the whole of it."""
    assert EvaluationTarget(file="dir#hetztop", attr=None, flake=None).selected_attr() == "hetztop"
    assert EvaluationTarget(file=None, attr="hetztop", flake=None).selected_attr() == "hetztop"
    assert EvaluationTarget(file="dir#hetztop", attr="config", flake=None).selected_attr() == "hetztop.config"
    assert EvaluationTarget(file=None, attr=None, flake="ref#hello").selected_attr() == "hello"
    assert EvaluationTarget(file="default.nix", attr=None, flake=None).selected_attr() is None
    assert EvaluationTarget(file=None, attr=None, flake=None).selected_attr() is None


def test_the_configuration_message_names_the_kind_and_the_path() -> None:
    """Without a selected attribute it still names the path, because that is the answer."""
    nixos = CONFIGURATION_KINDS["nixos"]
    assert configuration_message(nixos, "hetztop") == (
        "hetztop is a NixOS configuration, not a derivation.\nTry --attr hetztop.config.system.build.toplevel"
    )
    assert configuration_message(nixos, None) == (
        "the selected value is a NixOS configuration, not a derivation.\nTry --attr config.system.build.toplevel"
    )
    home = CONFIGURATION_KINDS["homeManager"]
    assert configuration_message(home, "me").endswith("Try --attr me.config.home.activationPackage")
