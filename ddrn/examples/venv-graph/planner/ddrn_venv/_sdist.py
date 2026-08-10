"""Reading a source distribution, which is the one file no evaluation holds.

`scripts/plan-sdist.py` is the only caller today. The code lives here, and not
there, because it is a decision about packaging and not about one planner: the
same question — what does this archive declare — belongs beside `build_system`
in `_resolve.py`.

PEP 517 says a source distribution holds one top directory, named
`{name}-{version}`, and reads `pyproject.toml` inside it.
"""

from __future__ import annotations

import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

#: The name of the file that PEP 517 reads, inside the one top directory that
#: a source distribution must have.
PYPROJECT = "pyproject.toml"


def is_pyproject(name: str, subdirectory: str = "") -> bool:
    """Whether `name` names the `pyproject.toml` that PEP 517 reads.

    **A member name is not a path, and two archives spell the same member two
    ways.** `tar` accepts `./demo-1.0/pyproject.toml`, and `zip` writes
    `demo-1.0/pyproject.toml`, so a count of the separators answers one and
    not the other. This drops the leading `.` and each empty part, and then
    compares what remains.

    A file deeper in the tree is a file of the project, and not the
    declaration that PEP 517 reads. `subdirectory` moves the answer down by
    exactly that many parts, and no more: **an archive of a URL source may
    hold more than one project.** `uv.lock` writes
    `source = { url = "…/hatch.tar.gz", subdirectory = "backend" }`, so the
    declaration to read is `hatch-1.27.0/backend/pyproject.toml` and the one
    at the top of that archive belongs to another project.
    """
    parts = [part for part in PurePosixPath(name).parts if part not in {".", ""}]
    wanted = [*PurePosixPath(subdirectory).parts, PYPROJECT] if subdirectory else [PYPROJECT]
    # The top directory is the one part that the archive names and no caller
    # can know, so the comparison starts after it.
    return len(parts) == len(wanted) + 1 and parts[1:] == wanted


def read_pyproject(archive: str | Path, subdirectory: str = "") -> dict[str, Any]:
    """The `pyproject.toml` of a source distribution, read from the archive.

    A distribution that carries no such file is not an error. PEP 517 gives
    the fallback backend for exactly that case, and `build_system` applies it,
    so an empty table is the answer.
    """
    path = Path(archive)
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive_file:
            for name in archive_file.namelist():
                if is_pyproject(name, subdirectory):
                    return tomllib.loads(archive_file.read(name).decode())
        return {}

    with tarfile.open(path) as archive_file:
        for member in archive_file.getmembers():
            if is_pyproject(member.name, subdirectory):
                handle = archive_file.extractfile(member)
                if handle is None:
                    raise RuntimeError(f"{path}: '{member.name}' is not a regular file")
                with handle:
                    return tomllib.loads(handle.read().decode())
    return {}
