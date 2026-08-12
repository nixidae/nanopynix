"""Report what the two compiled packages give, from inside the environment.

`check-native.nix` runs this with the `bin/python` of the environment that the
graph built. Both packages arrived as a source distribution, so each line below
exists only because a compiler ran inside the build of that distribution.

**The file name of each extension is a part of the report.** `sysconfig` gives
a build the suffix of the interpreter it builds for, so a name that carries
another version means the wheel was built for another target.
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import cffi
import psutil
from _cffi_backend import __file__ as backend_file
from psutil import _psutil_linux


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")

    # **The import is the proof, and the call is the second one.** A wheel that
    # holds no extension fails at the import, and one that holds an extension
    # of another interpreter fails there too.
    print(f"psutil       {psutil.__version__} cpus={psutil.cpu_count()}")
    print(f"  {Path(_psutil_linux.__file__).name}")

    # `cffi` reads `ffi.h` of `libffi`, which reaches the build through
    # `CPPFLAGS`, and links `libffi`, which reaches it through `LDFLAGS`.
    # `sizeof` runs that library.
    print(f"cffi         {cffi.__version__} sizeof(long)={cffi.FFI().sizeof('long')}")
    print(f"  {Path(backend_file).name}")

    found = sorted((dist.name or "?", dist.version) for dist in metadata.distributions())
    print("distributions")
    for name, version in found:
        print(f"  {name}=={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
