"""Direct unit tests for pynix.flake's private rendering helpers.

test_flake_show.py only exercises these through real flake evaluation, which
never produces Nix INT/FLOAT/BOOL/NULL attrs, list values, or unresolved
values at the top level of the test fixture flake. These tests evaluate a
small expression for each branch instead of contriving a flake to expose it.
"""

from __future__ import annotations

from huggorm.testing.environment import NixTestEnvironment
from rich.tree import Tree

import pynix._util as util_module
from pynix._impl.flake import (
    _build_tree,
    _format_attr,
    _TreeBudget,
)


async def _labels(environment: NixTestEnvironment, expr: str, budget: _TreeBudget | None = None) -> list[str]:
    async with util_module.huggorm_eval_session(environment.store_uri) as (_session, _store, state):
        value = await (await state.eval_expr(f"{{ v = {expr}; }}")).get("v")
        tree = Tree("root")
        await _build_tree(tree, state, value, budget=budget or _TreeBudget())
    return [str(child.label) for child in tree.children]


async def test_build_tree_marks_unresolved_when_the_value_throws(shared_nix_environment: NixTestEnvironment) -> None:
    assert await _labels(shared_nix_environment, 'throw "evaluation failed"') == ["[dim]<unresolved>[/dim]"]


async def test_build_tree_labels_a_throwing_attribute_and_marks_it_unresolved(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with util_module.huggorm_eval_session(shared_nix_environment.store_uri) as (_session, _store, state):
        value = await state.eval_expr('{ v = { broken = throw "x"; }; }')
        tree = Tree("root")
        await _build_tree(tree, state, await value.get("v"), budget=_TreeBudget())

    [broken] = tree.children
    assert str(broken.label) == "[green]broken[/green]: [dim]<unspecified>[/dim]"
    assert [str(child.label) for child in broken.children] == ["[dim]<unresolved>[/dim]"]


async def test_build_tree_renders_list_values_and_truncates_past_ten(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    labels = await _labels(shared_nix_environment, 'builtins.genList (i: "s") 12')

    assert labels[0] == "[green][0][/green]: [dim]<string>[/dim]"
    assert len(labels) == 11
    assert labels[-1] == "[dim]... 2 more items[/dim]"


async def test_build_tree_renders_a_short_list_without_a_more_items_message(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    labels = await _labels(shared_nix_environment, '[ "a" "b" "c" ]')

    assert len(labels) == 3
    assert not any("more items" in label for label in labels)


async def test_build_tree_stops_list_rendering_once_the_budget_is_exhausted(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    labels = await _labels(shared_nix_environment, '[ "a" "b" "c" ]', _TreeBudget(remaining=0))

    assert labels == ["[dim]<...>[/dim]"]


def test_format_attr_covers_the_remaining_scalar_types() -> None:
    assert "<int>" in _format_attr("n", "int")
    assert "<float>" in _format_attr("n", "float")
    assert "<bool>" in _format_attr("n", "bool")
    assert "<null>" in _format_attr("n", "null")
    assert _format_attr("n", "attrs") == "[cyan]n[/cyan]"
    assert _format_attr("n", "function") == "[yellow]n[/yellow]: [dim]<function>[/dim]"
