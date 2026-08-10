"""Write a PEP 427 wheel from a source tree, with no network and no backend.

**This exists so that the sealed test needs no index.** Every artefact of
`ddrn/examples/venv-graph/lock.json` comes from PyPI, so a test over that lock
needs a network. The fixtures of this directory are derivations instead, and
this script is what turns one source tree into the `.whl` file that a graph
node installs.

It is deliberately the smallest thing that `pypa/installer` accepts:

- PEP 427 names the file `{name}-{version}-{python}-{abi}-{platform}.whl`, and
  it escapes the name and the version by replacing each run of unsafe
  characters with `_`.
- PEP 376 puts `METADATA`, `WHEEL` and `RECORD` in the `.dist-info` directory.
  `installer` reads `RECORD` and refuses a wheel whose `RECORD` disagrees with
  what the archive holds.
- PEP 517 console scripts live in `entry_points.txt`, and `installer` is what
  turns each one into a file in `bin`.

The environment gives `out`, `src`, `distName`, `distVersion`, and optionally
`entryPoints`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import zipfile
from pathlib import Path

#: The timestamp that every member of the archive carries. A zip records the
#: mtime of each file, so a build that used the real one would write different
#: bytes every time. This is what `SOURCE_DATE_EPOCH` means for a zip, and
#: 1980-01-01 is the earliest that the format can hold.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def escape(name: str) -> str:
    """The name of a distribution, as PEP 427 spells it inside a file name."""
    return re.sub(r"[^\w\d.]+", "_", name, flags=re.UNICODE)


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: the Nix half must set it")
    return value


def record_line(name: str, data: bytes) -> str:
    """One row of `RECORD`: the path, the hash and the size.

    PEP 376 spells the hash as `sha256=` followed by url-safe base64 with the
    padding removed.
    """
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def main() -> None:
    out = Path(env("out"))
    src = Path(env("src"))
    name = env("distName")
    version = env("distVersion")

    dist_info = f"{escape(name)}-{escape(version)}.dist-info"

    # Every file of the wheel, in the order that the archive holds them. A
    # dict keeps that order, and the sort below makes it independent of how
    # the file system returned the tree.
    members: dict[str, bytes] = {}

    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        members[str(path.relative_to(src))] = path.read_bytes()

    # **`Requires-Dist` is absent, and that is not an omission.** The graph
    # resolves every dependency from the lock file, and no node of it reads the
    # metadata of a wheel. A field that nothing reads would be a fixture that
    # nothing tests.
    members[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nA fixture of ddrn/examples/venv-graph/tests.\n"
    ).encode()

    members[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: ddrn-mk-wheel\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )

    scripts = env_list("entryPoints")
    if scripts:
        members[f"{dist_info}/entry_points.txt"] = ("[console_scripts]\n" + "\n".join(scripts) + "\n").encode()

    # `RECORD` names itself with no hash and no size, which is what PEP 376
    # says, because it cannot hash the file it is.
    record = "\n".join(record_line(item, data) for item, data in sorted(members.items()))
    members[f"{dist_info}/RECORD"] = f"{record}\n{dist_info}/RECORD,,\n".encode()

    # **`$out` is the file, and not a directory that holds it.** `fetchurl`
    # produces a file, and `install-wheel.sh` copies `$wheel` under the name
    # that the lock gave it. A fixture has to arrive the same way.
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item, data in sorted(members.items()):
            info = zipfile.ZipInfo(item, date_time=ZIP_EPOCH)
            # A zip carries the mode in the top 16 bits of this field.
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)

    print(f"mk-wheel: wrote {out.name} with {len(members)} members")


def env_list(name: str) -> list[str]:
    """One value for each line of `name`, with the blank lines dropped."""
    return [line.strip() for line in os.environ.get(name, "").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
