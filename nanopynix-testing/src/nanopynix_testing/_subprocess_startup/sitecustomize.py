"""Reproduce the pytest process's startup side effects in exec'd subprocesses.

Python's `site` module imports `sitecustomize` at interpreter startup if this
directory is on `sys.path` (via the `PYTHONPATH` environment variable). A
`pynix` command a test runs as a subprocess, or a `multiprocessing` child, is a
freshly exec'd interpreter, so it inherits neither of the two things set up
below. Getting this directory onto `PYTHONPATH` is handled by
`nanopynix_testing.beartype_hook`, which is loaded before anything else (see
its docstring).

Both entries are no-ops unless their environment variable is set, so importing
this module is always safe -- including in the many subprocesses tests spawn
that have nothing to do with either concern.
"""

from __future__ import annotations

import os

import coverage

# `coverage.process_startup()` checks COVERAGE_PROCESS_START itself and is a
# no-op when that variable is unset. pytest-cov sets it when --cov is active.
coverage.process_startup()

# A child the suite starts gets the same hook as the pytest process.
if os.environ.get("BEARTYPING") == "1":
    import beartype_bootstrap

    beartype_bootstrap.install()
