"""Every import pynix reaches resolves under deferimport, as a release runs it.

The suites run with ``DEFERIMPORT=eager`` (the beartype hook needs real
objects), so no other test runs pynix the way a user does. An import that
works eagerly and fails lazily was invisible to all of them: ``pynix search``
failed on rapidfuzz in every release build, and every suite stayed green.

A fresh interpreter imports pynix, which installs deferimport with pynix's own
policy, imports every pynix module, then resolves every lazy proxy in
``sys.modules`` until none is left. Under eager imports a failing module-scope
import fails the import itself, so a proxy that fails here is a failure only
the lazy mode has.
"""

from __future__ import annotations

import json
import os
import sys

import anyio

_PROBE = """
import importlib, json, pkgutil, sys, types

import deferimport
import pynix
from deferimport.proxy import LazyProxy

if deferimport.installed() is None:
    raise SystemExit("deferimport is not installed: the probe would test nothing")

failures = {}
for info in pkgutil.walk_packages(pynix.__path__, "pynix."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as error:
        failures[info.name] = f"{type(error).__name__}: {error}"

seen = set()
while True:
    pending = [
        (module.__name__, name, value)
        for module in list(sys.modules.values())
        if isinstance(module, types.ModuleType)
        for name, value in list(vars(module).items())
        if type(value) is LazyProxy and id(value) not in seen
    ]
    if not pending:
        break
    for module, name, value in pending:
        seen.add(id(value))
        try:
            object.__getattribute__(value, "_reify")()
        except Exception as error:
            failures[f"{module}.{name}"] = f"{type(error).__name__}: {error}"

print(json.dumps({"proxies": len(seen), "failures": failures}))
"""

#: Lazy-only failures that pynix never reaches at run time, each with why.
_DORMANT = {
    # structlog's `try: from ._greenlets import GreenThreadLocal` stays eager,
    # and `_greenlets`' own `from greenlet import getcurrent` goes lazy, so the
    # `except ImportError` never fires. Only `structlog.threadlocal` reads it,
    # and pynix does not use that module.
    "structlog._greenlets.getcurrent",
}


async def test_every_lazy_import_pynix_reaches_resolves() -> None:
    env = {key: value for key, value in os.environ.items() if key not in ("BEARTYPING", "DEFERIMPORT")}
    result = await anyio.run_process([sys.executable, "-c", _PROBE], env=env, check=False)
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)

    assert report["proxies"] > 0, "no lazy proxy was made: the probe ran eager"
    failing = {name: error for name, error in report["failures"].items() if name not in _DORMANT}
    assert not failing, "these imports fail only under deferimport:\n" + "\n".join(
        f"  {name}: {error}" for name, error in sorted(failing.items())
    )
