from __future__ import annotations

from typing import override

from libpynix import opt
from pynix import _impl
from pynix._nix_options import (
    arg_option,
    argjson_option,
    args_json_option,
    argstr_option,
    attr_option,
    file_option,
    flake_option,
)
from pynix._settings import ConfiguredCommand, store_option


class Eval(ConfiguredCommand):
    """Evaluate a Nix expression and print the result as JSON"""

    expr: str | None = opt(None, help="Nix expression to evaluate. Reads from stdin if not provided.")

    file: str | None = file_option()

    arg: list[tuple[str, str]] = arg_option()

    argstr: list[tuple[str, str]] = argstr_option()

    argjson: list[tuple[str, str]] = argjson_option()

    args_json: str | None = args_json_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to evaluate with.")

    apply: str | None = opt(None, help="Apply the function EXPR to the result before it is printed.")

    raw: bool = opt(
        False,
        help="Print the result as a string, with no quotes and no newline. A value that does not coerce to a string is an error.",
    )

    # pynix evaluates impurely already (nixidae/nixidae#1109). The flag makes a
    # `nix eval` command line parse.
    impure: bool = opt(False, help="Allow access to mutable paths and the environment, as nix eval --impure does.")

    @override
    async def run(self) -> None:
        await _impl.eval.run_eval(self)
