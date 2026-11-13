"""The Nix helpers pynix ships, imported through a mount of their directory."""

from __future__ import annotations

import pytest
from huggorm import AsyncNativeError
from huggorm.errors import NixError
from huggorm.testing.environment import NixTestEnvironment

import pynix._util as util_module
from pynix._evaluate import call
from pynix._nix_helpers import helper


async def test_a_helper_loads_under_pure_evaluation(shared_nix_environment: NixTestEnvironment) -> None:
    """Pure evaluation refuses `import` of any other absolute path, a store
    path included: the mount is what allows this one."""
    async with util_module.huggorm_eval_session(shared_nix_environment.store_uri, pure=True) as (_s, _store, state):
        walk = await helper(state, "packages")
        assert await walk.type_name() == "function"


async def test_an_error_inside_a_helper_names_its_file(shared_nix_environment: NixTestEnvironment) -> None:
    async with util_module.huggorm_eval_session(shared_nix_environment.store_uri) as (_s, _store, state):
        lib = await state.eval_expr("{ concatStringsSep = builtins.concatStringsSep; }")
        top = await (await call(await helper(state, "packages"), lib)).get("top")
        with pytest.raises(NixError) as raised:
            await state.force(await (await call(top, await state.eval_expr("1"))).get("packages"))
        native = raised.value.native
        assert isinstance(native, AsyncNativeError)
        assert "packages.nix" in await state.render_error(native, show_trace=True)
