"""A PEP 517 build backend, small enough to read in one sitting.

**The sealed test needs a backend that no index serves.** `flit_core` is what
the real lock file uses, and it comes from PyPI. This one is a fixture, and it
is built into a wheel by `mk-wheel.py` like any other artefact.

It implements the three hooks that this lab calls:

- `build_wheel`, which PEP 517 defines, and which a node of kind `sdist-build`
  runs over an unpacked archive.
- `build_editable`, which PEP 660 defines, and which the editable node runs
  over a local project.
- `build_sdist`, which PEP 517 defines, and which a node of kind `local-sdist`
  runs over a local project. This is the hook that `uv build` calls, and the
  archive that it writes is the one that an index serves.

The first two write a wheel whose `RECORD` agrees with the archive, because
`pypa/installer` refuses one that does not.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from io import BytesIO
from pathlib import Path

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: The one time that every member of a source distribution carries. A node of
#: this graph floats, so a build that wrote the time of the day would take a
#: new output path on every run, and every node above it would build again.
TAR_EPOCH = 315532800


def escape(name: str) -> str:
    return re.sub(r"[^\w\d.]+", "_", name, flags=re.UNICODE)


def project() -> dict[str, str]:
    """The name and the version, from the `pyproject.toml` beside us."""
    with Path("pyproject.toml").open("rb") as handle:
        table = tomllib.load(handle)["project"]
    return {"name": table["name"], "version": table["version"]}


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def _platform_tag() -> str:
    """The tag of a wheel that holds a binary of this interpreter.

    **A wheel that carries a binary may not say `py3-none-any`.** That tag
    means "any interpreter, any platform", and a compiled module runs on
    neither. `sysconfig` names the interpreter and the platform of the build,
    and `packaging` is not on the path of a node that runs this backend.
    """
    version = f"{sys.version_info.major}{sys.version_info.minor}"
    platform = re.sub(r"[^\w\d]+", "_", sysconfig.get_platform())
    return f"cp{version}-cp{version}-{platform}"


def _write(directory: str, members: dict[str, bytes], dist: dict[str, str]) -> str:
    binary = any(item.endswith(".so") for item in members)
    tag = _platform_tag() if binary else "py3-none-any"
    purelib = "false" if binary else "true"

    dist_info = f"{escape(dist['name'])}-{escape(dist['version'])}.dist-info"
    members[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {dist['name']}\nVersion: {dist['version']}\n\nBuilt by demoback.\n"
    ).encode()
    members[f"{dist_info}/WHEEL"] = (
        f"Wheel-Version: 1.0\nGenerator: demoback\nRoot-Is-Purelib: {purelib}\nTag: {tag}\n"
    ).encode()
    record = "\n".join(_record_line(item, data) for item, data in sorted(members.items()))
    members[f"{dist_info}/RECORD"] = f"{record}\n{dist_info}/RECORD,,\n".encode()

    name = f"{escape(dist['name'])}-{escape(dist['version'])}-{tag}.whl"
    with zipfile.ZipFile(Path(directory, name), "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item, data in sorted(members.items()):
            info = zipfile.ZipInfo(item, date_time=ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return name


def _compile() -> dict[str, bytes]:
    """Compile every `.c` of the project, and give back what the wheel holds.

    **A project with no C file compiles nothing, and needs no compiler.** Each
    fixture beside this one is that case, so this hook costs them nothing.

    `cc` comes from the `buildTools` argument of `../../../default.nix`, and
    from nowhere else. A node of the graph carries no compiler by default, so
    the error below is the one that a caller who forgot the argument reads.
    """
    root = Path("src")
    sources = sorted(root.rglob("*.c"))
    if not sources:
        return {}

    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    include = sysconfig.get_paths()["include"]
    built: dict[str, bytes] = {}
    for source in sources:
        target = source.with_name(f"{source.stem}{suffix}")
        command = ["cc", "-shared", "-fPIC", f"-I{include}", str(source), "-o", str(target)]
        print(f"demoback: {' '.join(command)}", file=sys.stderr)
        subprocess.run(command, check=True)  # noqa: S603 -- every argument is a path of this build
        built[str(target.relative_to(root))] = target.read_bytes()
    return built


def _sources() -> dict[str, bytes]:
    """Every module of the project, keyed by its path inside the wheel.

    The layout is `src/`, which is the one that the fixtures use.
    """
    root = Path("src")
    # A `.pyi` is the interface of a compiled module, which a type checker
    # reads and an interpreter does not. It ships beside the module.
    found = sorted(path for pattern in ("*.py", "*.pyi") for path in root.rglob(pattern))
    modules = {str(path.relative_to(root)): path.read_bytes() for path in found}
    return modules | _compile()


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, str] | None = None,  # noqa: ARG001 -- PEP 517 names it, and this backend reads none
    metadata_directory: str | None = None,  # noqa: ARG001 -- same
) -> str:
    """PEP 517: write a wheel of this project, and return its file name."""
    return _write(wheel_directory, _sources(), project())


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, str] | None = None,  # noqa: ARG001 -- PEP 517 names it, and this backend reads none
) -> str:
    """PEP 517: write the archive that an index serves, and return its name.

    **The name follows PEP 625, and it is not the name of the project.** The
    project is `demo-app`, and the archive is `demo_app-0.1.0.tar.gz`. So the
    node that builds a wheel out of this archive finds it by its extension,
    rather than predict the name.

    The archive holds one directory, and the name of that directory is the name
    of the archive with no suffix. A consumer strips that one level, which is
    what `scripts/build-sdist.sh` does with `--strip-components=1`.

    **What the archive holds is what the user gets, and it is not the tree.**
    This one takes `pyproject.toml` and every module under `src`, so a file
    that a build needs and that this list leaves out is absent for every user
    who installs from the index. `PKG-INFO` is the metadata that PEP 643
    names.
    """
    dist = project()
    root = f"{escape(dist['name'])}-{escape(dist['version'])}"

    members = {
        "pyproject.toml": Path("pyproject.toml").read_bytes(),
        "PKG-INFO": (
            f"Metadata-Version: 2.1\nName: {dist['name']}\nVersion: {dist['version']}\n\nBuilt by demoback.\n"
        ).encode(),
    }
    # **The archive ships the source, and not the build of it.** A `.c` file
    # is a member and a `.so` file is not: the wheel that a consumer builds
    # out of this archive compiles its own.
    for pattern in ("*.py", "*.pyi", "*.c"):
        for path in sorted(Path("src").rglob(pattern)):
            members[str(path)] = path.read_bytes()

    name = f"{root}.tar.gz"
    with (
        Path(sdist_directory, name).open("wb") as raw,
        # `mtime=0` keeps the time of the day out of the gzip header, and
        # `TAR_EPOCH` keeps it out of every member.
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.TarFile(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for item, data in sorted(members.items()):
            info = tarfile.TarInfo(f"{root}/{item}")
            info.size = len(data)
            info.mtime = TAR_EPOCH
            info.mode = 0o644
            archive.addfile(info, BytesIO(data))
    return name


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, str] | None = None,  # noqa: ARG001 -- PEP 660 names it, and this backend reads none
    metadata_directory: str | None = None,  # noqa: ARG001 -- same
) -> str:
    """PEP 660: write a wheel that points at the project rather than copying it.

    The `.pth` names the `src` of the tree that this build ran in.
    `build-local.py` of the graph is what redirects that path at the tree
    the environment must read.
    """
    dist = project()
    members = {f"{escape(dist['name'])}.pth": f"{Path('src').resolve()}\n".encode()}
    return _write(wheel_directory, members, dist)
