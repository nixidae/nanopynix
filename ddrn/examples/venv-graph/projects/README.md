# The uv projects that this example builds

Each project here comes from `uv` itself, and not from a hand-written file.
`ddrn/examples/venv-graph/uv.nix` reads the `uv.lock` of one project and builds
the environment of it as a graph of derivations.

**Use the `uv` of this repository.** `pkgs.uv` is pinned by the lock file of
the repository, so every reader gets the same `uv` and the same output. The dev
shell holds it, so `uv` on the path is already that one:

```console
$ direnv exec . uv --version
uv 0.12.1 (x86_64-unknown-linux-gnu)
```

Each command below writes `nix run --file . pkgs.uv --`, which names the same
program from outside the dev shell. Inside the shell, `uv` alone is enough.

## Edit a `pyproject.toml`, and run `uv lock` again

**The build refuses a lock file that a project has grown past.** `uv.nix` puts
the `pyproject.toml` of each member of the workspace into the store, and the
planner compares each one with the `[package.metadata]` that `uv.lock`
recorded. A dependency that one file states and the other does not stops the
build:

```
ddrn_venv._uvlock.LockError: uv.lock is stale: 'pure-app' declares different
dependencies than the lock file resolved. pyproject.toml adds
['typing-extensions']. Run `uv lock` and build again.
```

Run `uv lock` in the directory of the project, and build again.
`ddrn/PACKAGING-FACTS.md` gives what the comparison covers, and the one edit
that it lets through.

## Two settings that this machine needs

`uv` downloads its own CPython by default, and that interpreter is a
dynamically linked executable for a generic Linux. NixOS refuses to start it.
So name an interpreter of nixpkgs, and stop the download:

```console
$ py=$(nix build --file . pkgs.python313 --no-link --print-out-paths)
$ export UV_PYTHON_DOWNLOADS=never UV_PYTHON="$py/bin/python3.13"
```

## What a project declares for `uv.nix`

**`[tool.ddrn]` holds one key for each decision that `uv.lock` does not
record and that the project answers the same way every time.** `[tool.uv]
no-binary-package` is the model: uv keeps a setting of the project in
`pyproject.toml`, and `uv.nix` reads it there rather than take the same
decision twice.

```toml
[tool.ddrn]
from-sdist = ["ws-core"]

[tool.ddrn.build-tools]
psutil = ["stdenv.cc"]

[tool.ddrn.from-nixpkgs]
pyzmq = "python3Packages.pyzmq"
```

| key | what it decides |
| --- | --- |
| `build-tools` | what one package needs to build. `native-app` below uses it |
| `from-nixpkgs` | which package nixpkgs supplies rather than the index |
| `from-sdist` | which member ships an archive rather than a redirect |

Each value of the first two tables names an attribute of nixpkgs, and a dot
walks into it. The argument of `uv.nix` with the same name overrides the
table, so a build takes a different decision without an edit of the project.

**Only `native-app` declares anything**, because two tests build the same
project both ways and a declaration would decide for both.

## `pure-app`

The first project. Every artefact of it is a pure Python wheel, so no node of
the graph runs `auto-patchelf`, and the dependency chain has three levels:
`pure-app` -> `rich` -> `markdown-it-py` -> `mdurl`.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name pure-app \
    --python 3.13 --no-workspace --vcs none pure-app
$ cd pure-app
$ nix run --file . pkgs.uv -- add --no-sync rich
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer
```

Three parts of those commands matter:

- **`--no-package` makes the root virtual.** `uv.lock` writes
  `source = { virtual = "." }` for it, so the project takes part in the
  resolution and no node of the graph builds it. The environment is the closure
  of the dependencies.
- **`--group ddrn` pins the wheel installer.** Every graph installs its wheels
  with `pypa/installer`, and that is a tool of this build system and not a
  dependency of the project. PEP 735 dependency groups are what uv offers for
  exactly that, so one lock file pins the whole build. The selection does not
  enable the group, so `installer` never enters the environment.
- **`--no-sync` writes the lock file and creates no `.venv`.** The graph is the
  environment here, so there is nothing to sync.

**`.python-version` says 3.13, and the graph plans for `pkgs.python3`, which is
3.14.** That is not a mistake, and it is a small proof: `uv.lock` says
`requires-python = ">=3.13"`, the planner checks the target against that
specifier, and 3.14 satisfies it. A lock file that pinned `==3.13.*` would make
the planner refuse this target, with the message that
`ddrn_venv._uvlock.Lock.check_environment` writes.

## `packaged-app`

The second project. It installs itself, `hatchling` builds it, and one of its
dependencies builds from a source distribution.

```console
$ nix run --file . pkgs.uv -- init --lib --package --build-backend hatch \
    --name packaged-app --python 3.13 --no-workspace --vcs none packaged-app
$ cd packaged-app
$ nix run --file . pkgs.uv -- add --no-sync rich
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn \
    hatchling editables installer flit-core
```

Then two hand edits to `pyproject.toml`, which `uv` has no command for:

```toml
[build-system]
requires = ["hatchling", "editables"]

[tool.uv]
no-binary-package = ["mdurl"]
```

### Why the `ddrn` group holds four packages

**`uv.lock` records no build system.** The `[build-system]` table of the
project is not in it, and neither is the build system of any source
distribution that the lock file names. A backend therefore reaches the graph
only as an ordinary package, and a dependency group is what puts one there.
One entry is needed for each backend that any build of this project reaches:

| package | what needs it |
| --- | --- |
| `hatchling` | the `[build-system]` of this project |
| `editables` | `hatchling`, for a PEP 660 build, undeclared |
| `flit-core` | the `[build-system]` inside the `mdurl` source distribution |
| `installer` | every node of the graph, to install a wheel |

`ddrn_venv.build_requirements` walks the edges of the lock file from each of
those names, so the group pins the top of each closure and the lock file gives
the rest. `hatchling` alone expands to six packages.

**`hatchling` needs `editables` and does not say so.** Its
`build_editable_detection` reads `from editables import EditableProject`, and
the metadata of hatchling 1.31.0 names `packaging`, `pathspec`, `pluggy` and
`trove-classifiers` only. So `build-system.requires` names it instead, and
`tests/test_packaged_app.py` records both halves of that measurement.

hatchling states the same need in one more place, and it is a place that this
lab does not read: `get_requires_for_build_editable` of `hatchling/build.py`
returns `editables` beside every other dependency of the build.
`ddrn/PACKAGING-FACTS.md` says why no hook of that family runs here, under
"`get_requires_for_build_*` is the hook that this lab does not call".

### The same project, shipped rather than linked

`uv sync` installs a member as a PEP 660 editable, and that is what every
paragraph above describes. `uv build` writes the other form: a source
distribution, which is the file that an index serves and the only form that
reaches a user who does not have the tree.

```console
$ nix run --file . pkgs.uv -- build --sdist
Successfully built dist/packaged_app-0.1.0.tar.gz
```

`fromSdist` of `uv.nix` names each member to install that way, and
`check-sdist.nix` builds this project with it. Two PEP 517 hooks then run as
two nodes of one graph:

| node | hook | output |
| --- | --- | --- |
| `packaged-app-sdist` | `build_sdist` over the tree | `packaged_app-0.1.0.tar.gz` |
| `packaged-app-from-sdist` | `build_wheel` over that archive | a wheel, installed |

**The second node is of the same kind that `mdurl` uses.** An archive that a
node of this graph wrote and an archive that PyPI served are one thing to the
build that reads it, so one script builds both.

The two reports of `run-uv.sh` differ in one line, and that line is the whole
difference between the two forms:

```text
editable   loaded from $DDRN_EDITABLE_ROOT/src/packaged_app/__init__.py
sdist      loaded from <the environment>/lib/python3.14/site-packages/packaged_app/...
```

The editable follows an edit of the tree, and the release does not. `run-uv.sh`
builds the release check twice, with two trees, and both reports name the same
environment and the same module.

### The cost, stated plainly

A project that only installs wheels needs one line in a dependency group. A
project that builds anything from source needs one line for each backend that
any of those builds reaches, and nothing checks that the list is complete until
a build fails. `uv2nix` pays the same cost differently: `pyproject.nix` ships a
set of build systems of its own, so the version of a backend comes from
nixpkgs rather than from the lock file of the project.

## `workspace`

The third project. It is a uv workspace: one lock file, one virtual root, and
two members that install.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name workspace-root \
    --python 3.13 --no-workspace --vcs none workspace
$ cd workspace
$ rm main.py
$ nix run --file . pkgs.uv -- init --lib --package --build-backend hatch \
    --name ws-core --python 3.13 --vcs none packages/ws-core
$ nix run --file . pkgs.uv -- init --lib --package --build-backend hatch \
    --name ws-cli --python 3.13 --vcs none packages/ws-cli
$ nix run --file . pkgs.uv -- add --no-sync --package ws-cli rich ws-core
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn \
    hatchling editables installer
```

`uv init` inside the workspace adds each member to
`[tool.uv.workspace] members` of the root, and `uv add --package ws-cli ws-core`
writes `[tool.uv.sources] ws-core = { workspace = true }`. Then the same hand
edit as `packaged-app`, to the `pyproject.toml` of **each member**:

```toml
[build-system]
requires = ["hatchling", "editables"]
```

`ws-cli` also gets a `[project.scripts]` entry, and each `src/` module is hand
written.

### Two lists of members, and they do not agree

`[manifest] members` of `uv.lock` names the **packages**:

```toml
[manifest]
members = ["workspace-root", "ws-cli", "ws-core"]
```

`[tool.uv.workspace] members` of `pyproject.toml` names the **directories**:
`packages/ws-core` and `packages/ws-cli`. `uv.nix` reads the first list,
because a selection names packages. uv writes `[manifest]` for a workspace
only, so a lock file of one project holds none, and `uv.nix` falls back to the
name of the project.

### One lock file, and two environments

**`uv.lock` records no selection.** So `uv.nix` takes the `select` argument,
and `null` means every member, which is what `uv sync --all-packages` takes.
`run-uv.sh` builds two selections of this one lock file:

| selection | what the environment holds |
| --- | --- |
| every member | `ws-cli`, `ws-core`, `rich`, `markdown-it-py`, `mdurl`, `pygments` |
| `{ ws-core = [ ]; }` | `ws-core` |

`ws-core` depends on nothing, so the second selection reaches no artefact of
the index at all. The two runs share the `ws-core-editable` node, because the
node is the same derivation in both graphs.

### A member redirects at its own directory

**This is the defect that the workspace found.** A PEP 660 install redirects at
`$DDRN_EDITABLE_ROOT`, and `uv.nix` gave every local package that same value.
That is correct for a project whose source is `.`, and wrong for a member: the
`.pth` of `ws-core` then reads `$DDRN_EDITABLE_ROOT/src`, and the environment
raises `ModuleNotFoundError: No module named 'ws_core'`.

`uv.nix` now joins the relative directory of the lock file to the redirect, so
the `.pth` reads `$DDRN_EDITABLE_ROOT/packages/ws-core/src`. `packaged-app`
proves the `.` case, and its environment is the same store path as before the
correction.

### One member ships, and the other one still links

`fromSdist = [ "ws-core" ]` installs one member from its own archive and leaves
the other one editable. `run-uv.sh` builds that against the **edited** copy of
the workspace, and the two members then disagree about what they read:
`ws-core` reports the tree of this repository, which reached the store through
its archive, and `ws-cli` reads the edited copy and imports `ws_core` from the
store.

**The archive of a member carries neither the workspace nor the lock file.**
Measured with `uv build --all-packages --sdist`:

```text
ws_cli-0.1.0.tar.gz
  PKG-INFO
  README.md
  pyproject.toml
  src/ws_cli/__init__.py
  src/ws_cli/py.typed
```

`[tool.uv.sources] ws-core = { workspace = true }` is a table of uv, and no
index reads it. So a user who takes that archive from an index sees
`Requires-Dist: ws-core` and resolves it against PyPI. The graph does not meet
that problem, because it resolves every member out of the one lock file, and
this is a thing to know before a workspace member is published.

## `binary-app`

The fourth project. `pyzmq` ships wheels that hold compiled code, and this is
the only project here whose graph runs `auto-patchelf`.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name binary-app \
    --python 3.13 --no-workspace --vcs none binary-app
$ cd binary-app
$ rm main.py
$ nix run --file . pkgs.uv -- add --no-sync pyzmq
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer
```

`pyproject.nix` uses `pyzmq` for the same purpose, which is why this project
uses it too: the wheel carries a compiled extension **and** a copy of `libzmq`
and `libsodium` beside it, in `pyzmq.libs`.

### The stable ABI, which a hand-written tag set missed

`pyzmq` 27.1.0 ships six wheels for Linux on x86-64, and **none of them names
this interpreter**:

```
pyzmq-27.1.0-cp312-abi3-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl
pyzmq-27.1.0-cp313-cp313t-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl
pyzmq-27.1.0-cp314-cp314t-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl
```

`cp313t` and `cp314t` are the free-threaded builds, which are a different
interpreter. The wheel that fits is the first one, and PEP 384 is the reason: a
`cp312-abi3` wheel runs on every CPython at or above 3.12.

The tag set of `_resolve.py` was written by hand and held `cp314-abi3` alone,
so it matched nothing and the planner fell back to the source distribution.
`target_tags` now calls `packaging.tags.cpython_tags`, which takes the target
as an argument. `ddrn/PACKAGING-FACTS.md` gives the whole finding.

### The same project, with `pyzmq` from nixpkgs

`run-uv.sh` builds this project twice. The second build passes an override:

```console
$ nix build --file check-binary.nix \
    --arg fromNixpkgs '{ pyzmq = (import <nixpkgs> {}).python3Packages.pyzmq; }'
```

Both environments run the same code, and the two reports differ in one line:

| | the wheel of the index | `pkgs.python3Packages.pyzmq` |
| --- | --- | --- |
| extension | `_zmq.abi3.so` | `_zmq.cpython-314-x86_64-linux-gnu.so` |
| links against | `site-packages/pyzmq.libs` | `zeromq`, `libsodium`, `glibc` of the store |
| `auto-patchelf` | runs | does not run |
| `libzmq` | 4.3.5, a copy inside the wheel | 4.3.5, the one that the store holds |

**The override builds nothing during the run.** nixpkgs instantiates the
package outside the sandbox and hands the planner the `.drv` and the output
path, and the outer store substitutes the output from `cache.nixos.org`.

**Nothing reconciles the two versions.** nixpkgs holds `pyzmq` 27.1.0 here and
the lock file pins 27.1.0, so they agree by luck. The planner prints both, and
that line is the only warning there is.

## `git-app`

The fifth project. Its one dependency comes from a git repository, and from a
subdirectory of that repository.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name git-app \
    --python 3.13 --no-workspace --vcs none git-app
$ cd git-app
$ rm main.py
$ nix run --file . pkgs.uv -- add --no-sync \
    "flit-core @ git+https://github.com/pypa/flit@3.12.0#subdirectory=flit_core"
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer
```

`main.py` goes, for the reason that `binary-app` drops its own: this project
imports a package that only the graph installs, so the import belongs in
`../check-git.nix` and not in a file that the type checker of the repository
reads.

uv writes the whole reference as one string, and it resolves the tag to a
commit:

```toml
[[package]]
name = "flit-core"
version = "3.12.0"
source = { git = "https://github.com/pypa/flit?subdirectory=flit_core&rev=3.12.0#1c8141722066ffe1497e511982feeea0f091ddb6" }
```

**That entry names no file, and this is the whole point.** Every other package
of every project here gives a `wheels` table, an `sdist` table or both, and
`uv.nix` makes one `pkgs.fetchurl` for each row. This one gives neither, so the
menu holds nothing to download. The environment can hold `flit_core` only
because `uv.nix` fetched the repository and the graph ran one PEP 517
`build_wheel` call over the `flit_core` directory of the checkout.

Three things follow from the entry above, and `../check-git.nix` reports each
one:

1. **The fetch happens during the evaluation, and it is the one input of this
   lab that is not lazy.** A commit identifies a tree and is not a hash of one,
   so there is no fixed-output derivation to instantiate.
   `ddrn/PACKAGING-FACTS.md` gives the cost, and the one line of `uv.lock` that
   would remove it.
2. **The install holds the code.** A git checkout is not a tree that the user
   edits, so nothing redirects to it. The report names a module inside the
   environment, and the editable projects above name one under
   `$DDRN_EDITABLE_ROOT`.
3. **`uv.nix` fetches the commit, and not the tag.** The query of the reference
   says `rev=3.12.0`, and the fragment says `1c814172…`. A tag moves and a
   commit does not, so `../uv-git.nix` drops the name that the user gave.

### `flit_core` carries its own backend, and that is why it is here

```toml
[build-system]
requires = []
build-backend = "flit_core.buildapi"
backend-path = ["."]
```

PEP 517 calls that an in-tree backend. `requires` is empty and honest:
`flit_core` declares `dependencies = []`, so nothing has to be resolved and
nothing has to be installed before this build. `scripts/build-local.py` reads
`backend-path` out of the checkout and puts it first on the path.

**`hatchling` has the same shape and does not build here**, which is why this
project uses `flit_core` instead. `pypa/hatch` holds `hatchling` in `backend`,
and its `[build-system]` also says `requires = []` with an in-tree backend. But
that backend is `hatchling.ouroboros`, which reads the real list out of its own
`pyproject.toml` and returns it from `get_requires_for_build_wheel`. This lab
calls no hook of that family, and `ddrn/PACKAGING-FACTS.md` says why, so those
four packages never reach the path and the import of the backend fails.

`tests/fixtures/locks/git-subdirectory.lock` and `url-subdirectory.lock` still
name `hatchling`, because uv wrote them and they are the record of the shape.
`tests/test_git_app.py` reads this lock file, which the graph builds.

## `vendored-app`

The sixth project. Both dependencies are files of `vendor/`, and no index
serves either one.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name vendored-app \
    --python 3.13 --no-workspace --vcs none vendored-app
$ cd vendored-app
$ rm main.py
$ mkdir vendor
$ # write vendor/vendored-1.0.0-py3-none-any.whl and vendor/archived-1.0.0.tar.gz
$ nix run --file . pkgs.uv -- add --no-sync \
    vendor/vendored-1.0.0-py3-none-any.whl vendor/archived-1.0.0.tar.gz
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer "flit-core>=3.11,<4"
```

`main.py` goes, for the reason that `git-app` drops its own. `flit-core` joins
the `ddrn` group because the archive builds through `flit_core.buildapi`, and
the selection enables no group, so that backend stays out of the environment.

uv puts the location on the source, and writes the two shapes that the kind
takes:

```toml
[[package]]
name = "archived"
version = "1.0.0"
source = { path = "vendor/archived-1.0.0.tar.gz" }
sdist = { hash = "sha256:9207336232d4501be209c97c00472b0e34f1ef720b231274743ca2b9d21a76b2" }

[[package]]
name = "vendored"
version = "1.0.0"
source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
wheels = [
    { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:73535645a05f874809b1654dba489ab0f5726e32b341e60be40e9526d3dc59f2" },
]
```

**Neither artefact carries an address, and that is the whole point.** Every
package of an index gives a `url` on each row, and `uv.nix` makes one
`pkgs.fetchurl` for it. Here the wheel row gives the file name and the hash,
and the `sdist` table gives the hash alone. So `uv.nix` joins the path of the
source to this directory and calls `builtins.path`, which copies the file into
the store with no fetch.

Three things follow, and `../check-vendored.nix` reports each one:

1. **The wheel installs, and the archive builds.** One file needs no build, and
   the other runs one PEP 517 `build_wheel` call through `flit_core.buildapi`.
   The two are the two routes that an artefact of an index already takes.
2. **Both modules import from inside the environment.** A `path` source is not
   a tree that the user edits, so nothing redirects to it, and the report names
   a module under the store path of the environment.
3. **`flit_core` is not in the environment.** It builds `archived`, and the
   graph installs it into that build alone.

The two files of `vendor/` are in this repository, because a lock file that
names a path needs that path to exist. Both are small.
`tests/test_vendored_app.py` reads this lock file, which the graph builds.

## `local-index`

The seventh project. Its registry is a directory of the project, and `uv`
resolves both dependencies out of it.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name local-index \
    --python 3.13 --no-workspace --vcs none local-index
$ cd local-index
$ rm main.py
$ mkdir index
$ # write index/stocked-1.0.0-py3-none-any.whl and index/crated-1.0.0.tar.gz
$ # add the two index tables below to pyproject.toml
$ nix run --file . pkgs.uv -- add --no-sync stocked crated
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer "flit-core>=3.11,<4"
```

```toml
[[tool.uv.index]]
name = "local"
url = "index"
format = "flat"
default = true

[[tool.uv.index]]
name = "pypi"
url = "https://pypi.org/simple"
```

**`default = true` is what keeps the lock file portable.** A `tool.uv.sources`
entry that names the index by name makes uv write the absolute path of the
index into `[package.metadata]`, and that path is the machine that ran the
command. The local index as the default writes no such key, and the second
table keeps PyPI available for the `ddrn` group.

uv puts the directory of the registry on the source and the name of the file on
the row:

```toml
[[package]]
name = "crated"
version = "1.0.0"
source = { registry = "index" }
sdist = { path = "crated-1.0.0.tar.gz" }

[[package]]
name = "stocked"
version = "1.0.0"
source = { registry = "index" }
wheels = [
    { path = "stocked-1.0.0-py3-none-any.whl" },
]
```

**The location has three parts, and this is what the project proves.** uv joins
the root of the workspace, the path of the registry and the path of the row. A
reader that joined the row to the project would look for
`crated-1.0.0.tar.gz` at the top of this directory, where there is no such
file. `ddrn/PACKAGING-FACTS.md` names the two lines of uv that join the three.

Neither row carries a hash, because a flat registry of a directory serves the
file itself. That costs nothing: `uv.nix` copies the file with `builtins.path`.

`tests/test_local_index.py` reads this lock file, and `../check-index.nix`
builds the environment and reports what it holds.

## `native-app`

The eighth project, and the one that needs a compiler. `psutil` and `cffi` both
build from their source distribution, so a node of the graph runs `gcc`.

```console
$ nix run --file . pkgs.uv -- init --app --no-package --name native-app \
    --python 3.13 --no-workspace --vcs none native-app
$ cd native-app
$ rm main.py
$ nix run --file . pkgs.uv -- add --no-sync psutil cffi
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer setuptools
```

Then `tool.uv.no-binary-package` names both, so the planner takes the source
distribution of each and not the wheel that PyPI serves:

```toml
[tool.uv]
no-binary-package = ["cffi", "psutil"]
```

**`setuptools` is in the `ddrn` group for the reason that `installer` is.** The
backend of both distributions is `setuptools`, and no lock file records the
build system of a package. The planner resolves the backend out of the same
menu, so `setuptools` has to be a package of this lock file.

### What each package needs, and where the project says it

**The project declares its build tools, beside `[tool.uv]`:**

```toml
[tool.ddrn.build-tools]
psutil = ["stdenv.cc"]
cffi = ["stdenv.cc", "pkg-config", "libffi.dev", "libffi"]
```

Each value names an attribute of nixpkgs, and a dot walks into it. So
`../check-native.nix` names no build tool, and one file describes the whole
project. The `buildTools` argument of `../uv.nix` writes the same thing in
Nix, and it replaces the entry of each package that it names.

The two entries differ:

| package | tools | why |
| --- | --- | --- |
| `psutil` | `stdenv.cc` | it holds C code and links nothing outside the interpreter |
| `cffi` | `stdenv.cc`, `pkg-config`, `libffi.dev`, `libffi` | it reads `ffi.h` and links `libffi` |

**Name each output that the build reads.** A node of the graph runs no
`stdenv`, so no setup hook follows `.dev`: `libffi.dev` holds the header and
`libffi` holds the library, and the build needs both.

**`pkg-config` is not necessary here, and the entry is honest about that.**
`cffi` asks `pkg-config` first and falls back to `CPPFLAGS` and `LDFLAGS`, so
the build succeeds with no `pkg-config` at all.
`ddrn/PACKAGING-FACTS.md` records that measurement.

## The two distributions that a project vendors

`vendored-app/vendor` and `local-index/index` hold four small files, and no
index serves any of them. `tests/fixtures/mk-wheel.py` and
`tests/fixtures/mk-sdist.py` wrote all four, from a source tree of one module.
Each script takes its input in the environment:

```console
$ mkdir -p /tmp/src/stocked
$ printf '__version__ = "1.0.0"\n' > /tmp/src/stocked/__init__.py
$ out=index/stocked-1.0.0-py3-none-any.whl src=/tmp/src distName=stocked \
    distVersion=1.0.0 python tests/fixtures/mk-wheel.py
```

`mk-sdist.py` takes the same four values, and its source tree also holds the
`pyproject.toml` that names the backend. Both scripts write the same bytes
every time: a zip carries an mtime for each member, and a tar carries the owner
as well, so each script fixes both.
