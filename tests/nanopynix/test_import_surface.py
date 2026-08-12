"""What ``import nanopynix`` loads, and what it must not load.

``nanopynix/__init__.py`` used to import its whole public surface, and that
cost 0.848 s of a fresh interpreter that started in 0.017 s. Every consumer
paid it to read one name. The package now maps each name to a module and
imports that module at the first read, so the cost belongs to the caller that
asks for the name.

**These tests assert what loaded, and not how long it took.** A ceiling in
milliseconds measures the machine as much as the library, and it fails on a
loaded runner for no defect. The set of modules is the same fact and it is
exact: a re-export that came back would put its module in ``sys.modules``, and
the timing follows from that.

Each test runs a fresh interpreter, because the pytest process has already
imported most of nanopynix by the time it collects this file.

Issue #122 holds the measurement, and :mod:`nanopynix.libstore` is the
subpackage that it produced.
"""

from __future__ import annotations

import json
import sys

from tests.support.subprocess_output import CompletedProcess, run_process

#: Import each module that the caller names, and report what the interpreter
#: ended up with. `json` and `sys` are already in a fresh interpreter, so
#: importing them here adds nothing to the report.
_REPORT_SCRIPT = """
import importlib, json, sys
before = set(sys.modules)
for name in sys.argv[1:]:
    importlib.import_module(name)
print(json.dumps(sorted(set(sys.modules) - before)))
"""

#: The modules that cost the 0.848 s, with what each one was measured at.
#:
#: ``nanopynix_proto`` is one third of the whole: three generated modules of
#: 6634 lines, each message a pydantic model built at import time. Five modules
#: of nanopynix import it, so no subset of the old surface could stay eager and
#: be cheap.
EXPENSIVE = {
    "nanopynix_proto": "274 ms, the generated protocol",
    "nanopynix.rpc": "183 ms, the RPC engine",
    "nanopynix.inproc": "190 ms, the in-process engine",
    "nanopynix.settings": "113 ms, the pydantic settings models",
    "nanopynix.stores": "33 ms, the store models",
    "pydantic": "37 ms, which only the two above need",
    "grpclib": "15 ms, which only the RPC engine needs",
}


async def loaded_by(*modules: str) -> list[str]:
    """Every module that a fresh interpreter loads to import `modules`."""
    result: CompletedProcess = await run_process([sys.executable, "-c", _REPORT_SCRIPT, *modules])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout)


def reached(loaded: list[str], expensive: str) -> bool:
    """Whether `loaded` holds `expensive`, or any module under it."""
    return any(name == expensive or name.startswith(f"{expensive}.") for name in loaded)


async def test_the_report_sees_the_expensive_modules_when_they_load() -> None:
    """Fail loudly if the scanner stops matching, so the gates below mean something.

    Both tests assert that a set does **not** hold a name, and a report that
    came back empty would satisfy each of them by checking nothing.

    **The two engines are siblings, so this imports both.** ``nanopynix.rpc``
    does not reach ``nanopynix.inproc``, and a caller picks one of them.
    """
    loaded = await loaded_by("nanopynix.rpc", "nanopynix.inproc", "nanopynix.stores")
    found = sorted(name for name in EXPENSIVE if reached(loaded, name))
    assert found == sorted(EXPENSIVE), f"these no longer load: {sorted(set(EXPENSIVE) - set(found))}"


async def test_importing_the_package_loads_no_engine() -> None:
    loaded = await loaded_by("nanopynix")
    found = [f"{name} ({EXPENSIVE[name]})" for name in sorted(EXPENSIVE) if reached(loaded, name)]
    assert not found, (
        "`import nanopynix` loaded a module that the lazy surface exists to defer. An eager "
        "import in nanopynix/__init__.py is what does this: put the name in _EXPORTS and in the "
        "`if TYPE_CHECKING` block instead.\n" + "\n".join(found)
    )


async def test_the_libstore_surface_loads_no_engine() -> None:
    """The subpackage that a consumer of the store alone imports.

    It imports ``nanopynix_bindings`` and no other module of nanopynix, so it
    reaches none of these whether or not the package stays lazy.
    """
    loaded = await loaded_by("nanopynix.libstore")
    found = [name for name in sorted(EXPENSIVE) if reached(loaded, name)]
    assert not found, f"nanopynix.libstore grew a dependency on {found}"
    assert "nanopynix_bindings.util" in loaded, "the libstore surface no longer reaches the bindings"


async def test_a_name_of_the_surface_loads_the_module_that_defines_it() -> None:
    """The other direction: the deferral defers and does not drop.

    ``NixError`` comes from ``nanopynix.exceptions``, which imports the
    generated protocol. So a caller that catches a Nix error pays for the
    protocol, and that is the cost belonging to the caller that asked.
    """
    result = await run_process(
        [
            sys.executable,
            "-c",
            "import nanopynix, sys; nanopynix.NixError; print('nanopynix.exceptions' in sys.modules)",
        ]
    )
    assert result.returncode == 0, result.describe()
    assert result.stdout.strip() == "True"
