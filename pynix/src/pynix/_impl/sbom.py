"""The implementation of ``pynix sbom``.

The build closure and what each package says of itself come from one
evaluation of ``nix/sbom.nix``. The runtime closure comes from the store,
after the build.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime

import anyio

from pynix import _evaluate
from pynix._evaluate import JsonValue
from pynix._util import huggorm_build_session, print_json, report_and_exit
from pynix.bom import Bom, CpeDictionary, cyclonedx, inventory_of, spdx, with_cpe_dictionary
from pynix.sbom import Sbom
from pynix.target import EvaluationTarget, EvaluationTargetError


async def build_inventory(command: Sbom) -> Bom:
    """Evaluate and build the target of *command*, and list what its SBOM holds."""
    try:
        evaluation = EvaluationTarget.from_command(command)
        async with huggorm_build_session(
            command.store,
            verbosity=command.verbosity,
            print_build_logs=command.print_build_logs,
            pure=evaluation.pure(impure=command.impure),
        ) as (_session, store, _eval_store, state):
            value = await _evaluate.evaluate_target(
                evaluation,
                state,
                auto_call_file=True,
                attr_search=_evaluate.base_attr_search(),
            )
            found = await inventory_of(state, store, value, buildtime=command.buildtime)
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if command.cpe_dictionary is None:
        return found
    text = await anyio.Path(command.cpe_dictionary).read_text()
    return with_cpe_dictionary(found, CpeDictionary.parse(text))


def created_at(environ: Mapping[str, str], now: datetime) -> str:
    """The SPDX creation time: ``SOURCE_DATE_EPOCH`` when it is set, else *now*."""
    epoch = environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), UTC) if epoch and epoch.isdigit() else now.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def render(command: Sbom, found: Bom) -> dict[str, JsonValue]:
    """The document that *command* asks for."""
    if command.format == "spdx":
        return spdx(found, created=created_at(os.environ, datetime.now(UTC)))
    return cyclonedx(found)


async def run_sbom(command: Sbom) -> None:
    """The body of :meth:`pynix.sbom.Sbom.run`."""
    print_json(render(command, await build_inventory(command)))
