"""A value at an attribute path under a root, selected when it is awaited.

An editor asks about paths that often do not exist. A selection reports its
failure where a hover or a completion awaits it, as one of `EVAL_FAILURES`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from huggorm.attrpath import AttrPathError, select_attr_path
from huggorm.errors import NixError

from nanopynix._typechecking import BEARTYPING
from pynix._evaluate import to_python

if TYPE_CHECKING or BEARTYPING:
    from huggorm import AsyncEvalState, AsyncValue

    from pynix._evaluate import JsonValue

EVAL_FAILURES = (NixError, AttrPathError)

type EvalFailure = NixError | AttrPathError


@dataclass(frozen=True, slots=True)
class LazyValue:
    state: AsyncEvalState
    base: AsyncValue
    path: tuple[str, ...] = ()

    def attr(self, name: str) -> LazyValue:
        return LazyValue(self.state, self.base, (*self.path, name))

    async def force(self) -> AsyncValue:
        value = await select_attr_path(self.state, self.base, self.path)
        await self.state.force(value)
        return value

    async def type_name(self) -> str:
        return await (await self.force()).type_name()

    async def to_python(self) -> JsonValue:
        return await to_python(await self.force())

    async def attr_names(self) -> list[str]:
        value = await self.force()
        kind = await value.type_name()
        if kind != "attrs":
            raise AttrPathError(f"value is {kind}, not a set")
        return await value.names()

    async def edit_location(self) -> tuple[str, int]:
        location = await (await self.force()).edit_location()
        return str(location.path()), location.line()
