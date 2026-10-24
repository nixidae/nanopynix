"""Tests for the resolver that finds `options`, `pkgs` and `lib` in one target.

Every fixture is a real module system or a real package set, built from this
repository. The resolver has to meet the shapes that people really point at,
and a double cannot show that `specialArgs` never reaches the result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from huggorm import AsyncEvalState, AsyncValue
from huggorm.attrpath import AttrPathNotFoundError, select_attr
from huggorm.testing.environment import NixTestEnvironment

import pynix._util as util_module
from pynix._evaluate import evaluate_target
from pynix._search_target import TARGET_PATH, SearchTarget, _is_package_set, resolve
from pynix.target import EvaluationTarget, EvaluationTargetError

_FIXTURES = Path(__file__).parent / "test_search_target"
_NIXOS_SHAPE = Path(__file__).parent / "test_search" / "system.nix"


@pytest.fixture
async def state(shared_nix_environment: NixTestEnvironment) -> AsyncIterator[AsyncEvalState]:
    """One evaluator for every fixture of this module."""
    async with util_module.huggorm_eval_session(shared_nix_environment.store_uri) as (_session, _store, opened):
        yield opened


async def _target(state: AsyncEvalState, path: Path) -> AsyncValue:
    target = EvaluationTarget(file=str(path), attr=None, flake=None)
    return await evaluate_target(target, state, auto_call_file=True)


async def _resolved(state: AsyncEvalState, path: Path) -> SearchTarget:
    return await resolve(state, await _target(state, path))


def _paths(found: SearchTarget) -> tuple[str | None, str | None, str | None]:
    """The three attribute paths that answered, for a compact comparison."""
    return (
        found.options.path if found.options else None,
        found.pkgs.path if found.pkgs else None,
        found.lib.path if found.lib else None,
    )


async def test_a_nixos_shaped_target_needs_no_override(state: AsyncEvalState) -> None:
    """`eval-config.nix` re-exports `pkgs`, so the first link answers.

    `lib` is the one that falls through: the wrapper re-exports `pkgs` and not
    `lib`, which is why the old default of this program was `pkgs.lib`.
    """
    found = await _resolved(state, _NIXOS_SHAPE)
    assert _paths(found) == ("options", "pkgs", "pkgs.lib")


async def test_module_args_gives_the_package_set_back(state: AsyncEvalState) -> None:
    found = await _resolved(state, _FIXTURES / "module_args.nix")
    assert _paths(found) == ("options", "_module.args.pkgs", "_module.args.pkgs.lib")


async def test_special_args_hides_the_package_set(state: AsyncEvalState) -> None:
    """The measured limit: `specialArgs` reaches a module and not the result.

    The options tree is still there, so option search answers and package
    search does not. Only `--pkgs` can fill this in.
    """
    found = await _resolved(state, _FIXTURES / "special_args.nix")
    assert _paths(found) == ("options", None, None)


async def test_config_underscore_module_resolves_nowhere(state: AsyncEvalState) -> None:
    """`evalModules` removes `_module` from `config` and re-exports it above.

    This is the path a reader writes first, and it is wrong. The resolver
    documents `_module.args.pkgs`, so the wrong one must stay wrong.
    """
    target = await _target(state, _FIXTURES / "module_args.nix")
    with pytest.raises(AttrPathNotFoundError):
        await select_attr(state, target, "config._module.args.pkgs")
    assert await select_attr(state, target, "_module.args.pkgs") is not None


async def test_a_bare_package_set_is_its_own_package_set(state: AsyncEvalState) -> None:
    """A target that is nixpkgs has no options, and package search still runs."""
    found = await _resolved(state, _FIXTURES / "bare_pkgs.nix")
    assert _paths(found) == (None, TARGET_PATH, "lib")


async def test_an_override_names_itself_in_the_result(state: AsyncEvalState) -> None:
    target = await _target(state, _NIXOS_SHAPE)
    found = await resolve(state, target, pkgs_attr="pkgs", lib_attr="pkgs.lib", options_attr="options")
    assert _paths(found) == ("options", "pkgs", "pkgs.lib")


@pytest.mark.parametrize("keyword", ["options_attr", "pkgs_attr", "lib_attr"])
async def test_an_override_that_resolves_nowhere_raises(state: AsyncEvalState, keyword: str) -> None:
    """A person who names a path asked for that path, so a miss is an error."""
    target = await _target(state, _NIXOS_SHAPE)
    with pytest.raises(EvaluationTargetError, match="noSuchAttribute"):
        await resolve(state, target, **{keyword: "noSuchAttribute"})


async def test_a_value_that_is_not_an_attribute_set_is_not_a_package_set(state: AsyncEvalState) -> None:
    """`nix` treats a type error on a candidate path as a miss, and so does this."""
    assert not await _is_package_set(state, await state.eval_expr('"a string"'))


async def test_an_attribute_set_without_the_markers_is_not_a_package_set(state: AsyncEvalState) -> None:
    assert not await _is_package_set(state, await state.eval_expr("{ path = 1; }"))
