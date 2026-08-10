"""Write a source distribution from a source tree, with no network.

A source distribution is a `.tar.gz` whose members all live under one
directory named `{name}-{version}`. That layout is what `plan-sdist.py` reads:
it looks for `pyproject.toml` directly under that one directory.

**This is the fixture that makes the sealed test exercise the interesting
path.** The archive declares a build backend, and nothing outside the archive
knows which one, so the planner has to open the file.

The environment gives `out`, `src`, `distName` and `distVersion`.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tarfile
from pathlib import Path

#: Every member carries this mtime, so two builds write the same bytes.
EPOCH = 315532800


def escape(name: str) -> str:
    """The name of a distribution, as it appears in an archive member."""
    return re.sub(r"[^\w\d.]+", "_", name, flags=re.UNICODE)


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: the Nix half must set it")
    return value


def main() -> None:
    out = Path(env("out"))
    src = Path(env("src"))
    root = f"{escape(env('distName'))}-{escape(env('distVersion'))}"

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{root}/{path.relative_to(src)}")
            info.size = len(data)
            info.mtime = EPOCH
            info.mode = 0o644
            # A tar records the owner by name and by number. Both are fixed,
            # so the archive does not carry the account that built it.
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))

    # `gzip` writes an mtime into its own header, which `mtime=0` removes.
    out.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    print(f"mk-sdist: wrote {out.name} under {root}/")


if __name__ == "__main__":
    main()
