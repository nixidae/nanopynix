# Facts about Python packaging in Nix, and where each one comes from

`ddrn/NIX-FACTS.md` records what this lab proved about Nix. This file records
what it read in the two projects that already solve Python packaging in Nix,
and it is the reference for the work that remains.

Each fact names the file that proves it. A claim with no file is a guess, and
it does not belong here. Two checkouts are the source:

- `~/Code/pyproject.nix`, the builders and the pure Nix implementations of the
  packaging PEPs.
- `~/Code/uv2nix`, which reads `uv.lock` and renders a package set on top of
  those builders.

Sizes, measured with `wc -l` over each tree:

| tree | lines |
| --- | --- |
| `pyproject.nix/lib`, the PEPs in Nix | 3602 |
| `pyproject.nix/lib`, the tests of those | 4179 |
| `pyproject.nix/build`, the builders | 1578 |
| `pyproject.nix/build/hooks`, the shell and Python of the hooks | 921 |
| `uv2nix/lib` | 1947 |
| `ddrn/examples/venv-graph`, the planner and the build scripts | 990 |
| `ddrn/examples/venv-graph`, the Nix | 394 |

## The two halves of pyproject.nix

**`lib/` is the packaging PEPs, written in the Nix language.** `pep440.nix` is
402 lines of version comparison, `pep508.nix` is 826 lines of marker and
requirement parsing, and `pypa.nix` is 526 lines of tag selection. `poetry.nix`,
`pip.nix`, `eggs.nix`, `pep599.nix`, `pep600.nix`, `pep621.nix`, `pep656.nix`,
`pep723.nix`, `project.nix`, `scripts.nix` and `validators.nix` complete it.

**This lab needs none of that.** `ddrn/examples/venv-graph/plan.py` imports
`packaging.markers`, `packaging.requirements`, `packaging.tags` and
`packaging.utils`, and the reference implementation answers each question.

**`build/` is the builders, and this lab does need the equivalent of that.**
The two halves are separable, and the work that remains is all in the second
half.

## The hook pipeline of pyproject.nix

A package build is `stdenv.mkDerivation` with `pyprojectHook` in
`nativeBuildInputs`. `build/hooks/default.nix:191` composes it from four hooks,
and a fifth when the build is cross:

| hook | phase | what it does |
| --- | --- | --- |
| `pyprojectConfigureHook` | configure | overwrites `PYTHONPATH`, sets the `UV_*` variables |
| `pyprojectBuildHook` | build | PEP 517 `build_wheel` into `dist` |
| `pyprojectInstallHook` | install | installs each wheel of `dist` |
| `pyprojectOutputSetupHook` | — | writes the setup hook of the output |
| `pyprojectCrossShebangHook` | — | patches a shebang, when cross |

Three variants replace one hook of that set:

- `pyprojectWheelHook` replaces the build hook with `pyprojectWheelDistHook`,
  for a wheel that needs no build (`build/hooks/default.nix:236`).
- `pyprojectEditableHook` replaces it with `pyprojectBuildEditableHook`, and
  appends `pyprojectFixupEditableHook` (`build/hooks/default.nix:245`).
- `pyprojectDistHook` produces a `dist` output.

**The default installer of pyproject.nix is `uv`, and not `pypa/installer`.**
`build/hooks/pyproject-install-hook.sh` runs:

```sh
uv pip --offline --no-cache install --no-deps --link-mode=copy \
  --python=@pythonInterpreter@ --system --prefix "$out" "$wheel"
```

`pypa/installer` is the *cross* path only. The comment above
`pyprojectPypaInstallHook` says "Used instead of `pyprojectInstallHook` for
cross compilation support" (`build/hooks/default.nix:286`), and
`build/hooks/pypa-install-hook/wrapper.py` says "Ideally uv should also be used
for cross installs, but for now it's not compatible."

The wrapper exists because the two installers take different options: it turns
`UV_COMPILE_BYTECODE` into `--compile-bytecode 1` for `installer`.

**This lab uses `pypa/installer` for every node.** That is the cross path of
pyproject.nix, and not its default path. The reason is that `installer` is pure
Python and is one wheel of the same lock file, so it becomes a node of the
graph. `uv` is a compiled program, and a node that installs with `uv` would
take it from outside the plan.

**Both install from a `dist` directory.** `pushd dist` in each install hook.
That is the same reason this lab copies a wheel under its own name first, which
`ddrn/NIX-FACTS.md` records.

## Composing a virtual environment

`build/hooks/make-venv/make_venv.py` is the reference for
`ddrn/examples/venv-graph/scripts/make-venv.py`, and it is 350 lines against
130. Five differences matter.

**`merge_inputs` recurses into a directory, and this lab does not.** A
directory that several members provide is merged entry by entry, and only a
leaf regular file can collide (`make_venv.py:120`). `link_tree` of this lab
links a whole top level entry, and raises when a second member provides the
same name. **A namespace package therefore does not work in this lab**, because
two distributions that both ship `google/` or `zope/` collide.

**Bytecode always collides, and the comparison has to skip it.**
`is_bytecode` at `make_venv.py:84`: "Bytecode files contain fully resolved file
names, meaning that the byte code will always collide even if the original
sources are identical." pyproject.nix compares the sources instead. This lab
compares file content and would raise.

**`venvSkip` and `venvIgnoreCollisions` are the escape.** Both take an
`fnmatch` pattern, and `mkVirtualEnv` turns them into `--skip` and
`--ignore-collisions` (`build/packages.nix:54`). This lab has no equivalent.

**`pyvenv.cfg` names the interpreter that ran `venv`, which is the build
interpreter.** `fixup_pyvenv` at `make_venv.py:250` rewrites `sys.prefix` and
`sys.base_prefix` to the target Python. This lab does not, and the file is
correct only because the build interpreter and the target interpreter are the
same derivation. A cross build would write the wrong path.

**`venv` symlinks `bin/python`, and a symlink to the environment then breaks
it.** CPython derives `sys.prefix` from `argv[0]`, so a second symlink loses
the environment. `wrap_python_bin` at `make_venv.py:266` compiles a three line C
program that sets `argv[0]` and calls `execv`, and puts that in place of the
symlink. This lab meets the same trap from the other side: `default.nix` of the
planner enters the environment through its full path, and says so in a comment.
**The C wrapper is the fix, and the full path is the workaround.**

**The shebang test differs.** `write_bin` at `make_venv.py:185` compares the
whole shebang against the path of the build interpreter. This lab compares the
*name* of the interpreter, because `installer` writes the shebang from
`sys.executable`, which CPython reports after it resolves the symlink.

## Resolution is a walk, and not a solve

**`resolveNonCyclic` and `resolveCyclic` walk `passthru.dependencies`.**
`build/lib/resolvers.nix:26` and `:76`. Neither compares a version, and neither
reads a specifier. The edges come in already resolved, from `uv.lock` through
uv2nix, or by hand.

**`resolveCyclic` exists because Python packages have real cycles.** It uses
`builtins.genericClosure`. `resolveNonCyclic` is the faster memoised form, and
the comment says it "is intended to resolve build-system dependencies".

**So a lock file is the answer to resolution, and the Nix side only reads it.**
This is the fact that decides the shape of the work that remains here.

## Overrides in pyproject.nix

**The package set is a `lib.makeScope`** (`build/packages.nix:96`), so it
carries `overrideScope`, and each package carries `overrideAttrs`. An override
is an overlay of `final: prev: { name = prev.name.overrideAttrs ...; }`.

**`nixpkgsPrebuilt` adapts a nixpkgs Python package into that set.**
`build/hacks/default.nix:54`. It does three things, and
`build/hacks/write-nixpkgs-prebuilt.py` performs them:

1. **It drops `nix-support/propagated-build-inputs`**, so that the resolver of
   pyproject.nix owns dependencies rather than nixpkgs propagation
   (`write_nix_support`).
2. **It undoes `wrapPythonPrograms`.** Measured on
   `pkgs.python3Packages.pygments`: `bin/` holds `pygmentize`, a bash wrapper
   that prepends `PATH` and execs, and `.pygmentize-wrapped`, the real script.
   `write_bin` renames the second over the first, and drops any line matching
   `import sys.+sys.argv\[0\](.+)/nix/store`. That line is nixpkgs dependency
   injection:

   The block below is the line as nixpkgs writes it, so the fence says `text`.
   A `python` fence makes `ruff format` rewrite the quote, and a rewritten
   quote is no longer evidence.

   ```text
   import sys;import site;import functools;sys.argv[0] = '...';functools.reduce(
       lambda k, p: site.addsitedir(p, k),
       ['/nix/store/1j8cch7q…-pygments-2.20.0/lib/python3.14/site-packages'],
       site._init_pathinfo());
   ```

   Left in place, it puts the nixpkgs `site-packages` on `sys.path` and shadows
   every dependency that the environment resolved.
3. **It checks the interpreter.** It throws when `pythonVersion` differs, and
   warns when the Python derivation differs, because a compiled extension is
   built against one ABI.

**Everything else is a symlink.** `main` of `write-nixpkgs-prebuilt.py`
symlinks each top level entry of the input into the output.

**For this lab, only the second of the three is work.** Nothing here reads
`nix-support`, because `make-venv.py` reads `site-packages` and `bin` and
nothing else. The shebang is already rewritten. The interpreter check is a
comparison the planner can make.

Two more hacks exist: `importCargoLock` (`build/hacks/default.nix:155`), which
fetches Rust dependencies and uses import from derivation, and `toNixpkgs`
(`:240`), which renders a pyproject.nix set back into `buildPythonPackage`, and
adds `autoPatchelfHook` while it does so.

**`build/util/mkApplication`** builds an application from an environment by
symlinking only the files of one package, and dropping the interpreter, the
activation scripts and `pyvenv.cfg`.

## How much of the ecosystem pyproject.nix exercises

**`build/checks/build-systems.nix` bootstraps 27 build systems**, and builds
each one from the source that nixpkgs names:

```text
build calver cffi editables flit-core hatchling hatch-vcs installer libcst
maturin packaging pathspec pip pluggy pycparser pyproject-hooks pyyaml
semantic-version setuptools setuptools-rust setuptools-scm tomli tomli-w
trove-classifiers typing-extensions urllib3 wheel
```

**This lab has run one, `flit_core`.** Nothing in the design blocks the others.

## uv.lock, and what it does and does not hold

Measured over the 39 lock files in `~/Code/uv2nix/lib/fixtures`.

**A package entry carries the artefacts, and they are exactly the menu that
this lab already builds.** `sdist = { url, hash, size }` and `wheels = [ { url,
hash, size } ]`, with `hash` in the `sha256:<hex>` form that `fetchurl` takes.

**The dependency edges are in the lock, and they are already resolved.** A
`[[package]]` entry holds `dependencies = [ { name = "packaging" } ]`, with no
version and no specifier, because uv resolved them.

**`[package.metadata].requires-dist` is the declaration, not the resolution.**
That table holds `[{ name = "packaging", specifier = ">=24.1" }]`. It is what
the project asked for. The `dependencies` list is what it got.

**Six kinds of source appear, and this is how often:**

| `source = { ... }` | count | this lab |
| --- | --- | --- |
| `registry` | 59 | covered, wheel and sdist nodes |
| `editable` | 36 | covered, since the PEP 660 node |
| `virtual` | 5 | covered; a member with edges and no install |
| `url` | 1 | covered; an sdist that carries a subdirectory |
| `git` | 1 | covered; a checkout, fetched during evaluation |
| `directory` | 1 | covered; a tree, built as a wheel and not as an editable |

All six are covered now. The two that name a place get a section of their own
below, because each one costs something that a registry source does not.

**uv.lock records no build system.** No `build-system`, `build-backend` or
`requires` key appears in any of the 39 fixtures. uv2nix says so in its own
documentation, `doc/src/overriding/overrides-sdist.nix`:

> uv.lock does not contain build-system metadata. Meaning that for source
> builds, this needs to be provided by overriding.

The workaround it names has three parts: use
`pyproject-nix/build-system-pkgs`, add the build system to your own uv project,
or write an overlay by hand. The example adds six packages by hand to build
`pyzmq`.

**This is the gap that the lock file cannot close**, because the answer lives
in the `pyproject.toml` inside the source distribution, which nothing has
downloaded when the Nix evaluation runs.

**The whole override culture of Python in Nix comes from that one shape of
problem.** `doc/src/FAQ.md` of uv2nix states it plainly:

> `uv2nix` can only work with what it has, and `uv.lock` metadata is notably
> absent of important metadata.

> Overrides are required because Python tooling is lacking important metadata,
> complexity which surfaces when using Nix. Uv2nix focuses on getting the
> translation from `pyproject.toml` & `uv.lock` to Nix right, without trying to
> taper over deficiencies in metadata.

> In poetry2nix much of the requirement of overriding came from it's choice to
> build sdist's by default. […] Maintaining overrides was the biggest source of
> maintainer burnout for poetry2nix.

The reference is `nix-community/poetry2nix#1865`. So the reason uv2nix prefers
a wheel is not that a wheel is better. It is that **a wheel carries its
metadata on the outside, and a source distribution carries it on the inside**,
and a Nix evaluation cannot open the inside.

**A planner can open the inside.** That is the one structural difference this
lab has, and it is the difference that the burnout above measures.

**A lock can hold several resolutions at once.** `resolution-markers` appears
at the top level and again on a package, and two entries can then share a name
and differ in version:

```toml
resolution-markers = [
    "platform_release == '5.10.65'",
    "platform_release == '5.10.104'",
    "platform_release != '5.10.104' and platform_release != '5.10.65'",
]
```

A dependency of a forked resolution pins the whole coordinate:

```toml
dependencies = [
    { name = "arpeggio", version = "2.0.1", source = { registry = "..." }, marker = "platform_release == '5.10.65'" },
    { name = "arpeggio", version = "2.0.2", source = { registry = "..." }, marker = "platform_release == '5.10.104'" },
]
```

**Forked resolution is most of what `lock1.nix` costs.** That file is 568
lines, and the marker handling runs from `:62` to `:234`.

**uv has an open bug that makes a conflict marker unreadable.**
`lib/lock1.nix:85` names `astral-sh/uv#11513`: internal conflict markers are
not stripped, so "This makes it impossible to correctly consume a lock with
conflict markers", and the fallback parse costs the top level marker cache.

**A lock from an older uv can be ambiguous.** `lib/lock1.nix:166` throws "Non
disjoint install time resolution for package ... You are most likely using an
older version of uv to produce uv.lock which contains marker bugs."

**`sourcePreference` is mandatory in uv2nix.** `lib/workspace.nix:143` throws
unless `tool.uv.no-binary` or `tool.uv.no-build` implies one, and
`lib/build.nix:344` lets `no-binary-package` and `no-build-package` override it
for one package. This lab hardcodes the wheel preference, and has no per package
override.

**A lock also carries `optional-dependencies`, `dev-dependencies` and
`conflicts`**, which are extras, PEP 735 dependency groups, and declared
conflicts between them. `lib/build.nix:277` maps the first two onto
`passthru.optional-dependencies` and `passthru.dependency-groups`.

**A workspace gives four dependency presets**, and each one is an attribute set
of package name to a list of extras and groups (`lib/workspace.nix:253`):
`all`, `optionals`, `groups`, and `default`, where `default` reads
`tool.uv.default-groups` of the project. `mkVirtualEnv` takes one of them, or a
set written by hand.

## uv.lock in the wild, measured

The section above measured the 39 lock files of `~/Code/uv2nix/lib/fixtures`,
which a test suite wrote. This section measures 22 lock files of real projects,
under `~/Code/*/uv.lock`.

| what | count |
| --- | --- |
| lock files | 22 |
| packages | 1911 |
| artefacts (wheel entries and sdist entries) | 15223 |

The largest is `anthropic-sdk-python`: 103 packages and **1838 artefacts**. The
ratio holds across the sample, and it is the number that sizes a menu: a lock
of 100 packages carries about 1800 artefacts, because a package publishes one
wheel for each interpreter and each platform.

| field | count | note |
| --- | --- | --- |
| `source = { registry }` | 1858 | the common case |
| `source = { editable }` | 49 | a workspace member |
| `source = { virtual }` | 3 | edges, and nothing to install |
| `source = { git }` | 1 | needs a fetcher |
| package with `optional-dependencies` | 52 | extras |
| package with `dev-dependencies` | 40 | PEP 735 groups |
| package with `resolution-markers` | 60 | a forked resolution |
| package with no `version` | 6 | a dynamic version |
| `conflicts` declaration | 2 | over two lock files |

`version` is `1` in every one. `revision` is 3 in 18 of them, 2 in two, 1 in
one, and absent in one. So a reader asserts `version == 1`, and it accepts any
`revision` and no `revision`.

### What a uv.lock menu costs, measured

One `pkgs.fetchurl` for each of the 1838 artefacts of
`anthropic-sdk-python/uv.lock`, carried in a `writeText` file the way
`ddrn/NIX-FACTS.md` describes:

| what | value |
| --- | --- |
| the menu file | 632 KB |
| paths in its closure | 2599 |
| of which `.drv` | 2440 |
| closure size | 9.5 MiB |
| instantiation, warm | 1.2 s |
| first build, which writes each `.drv` | 12.5 s |

**Compare the pre-instantiated nixpkgs menu**, which the same document
measures at 37669 paths and 154.4 MiB for all of `python3Packages`. A whole
lock file costs one fortieth of that, so the menu needs no filter and no
budget. **The planner therefore reads a menu of every artefact of the lock,
and it makes the choice.**

### What the whole planner costs, measured and reproducible

The table above measures a `writeText` file of 1838 `fetchurl` derivations, and
that file is not the planner that this lab builds now. The planner also carries
one planner for each source distribution, at each level of the recursion, and
those did not exist when that measurement ran.

`ddrn/examples/venv-graph/bench/measure.sh` measures the planner itself. It
needs no network and it builds nothing, and `bench/README.md` gives the method.
The project is `anthropic`, `boto3`, `jupyterlab` and `pandas`, which `uv`
resolved to 111 packages:

```console
$ ddrn/examples/venv-graph/bench/measure.sh --cold
```

| what | value |
| --- | --- |
| packages in `uv.lock` | 111 |
| `uv.lock` | 244918 bytes |
| artefact rows | 715 (605 wheel, 110 sdist) |
| the menu, `DDRN_INPUTS` | 253860 bytes |
| instantiation into an empty store | 8.97 s |
| instantiation, warm | 1.74 s |
| paths in the closure of the `.drv` | 2043 |
| of which `.drv` | 1811 |
| closure size | 158730056 bytes |
| bytes of `.drv` text | 119169562 |
| `.drv` that carry a copy of the menu | 331, and 114646605 bytes of the above |

**96% of the derivation text is the same menu, written 331 times.** There is
one planner for each of the 110 source distributions at each of the three
levels, and one outer planner, so 331 derivations each hold the whole 253860
byte menu. That is what a menu of every artefact costs, and the cost is a
property of the design and not a defect: a planner names an artefact through
the string context of that text, so the text is what makes each `.drv` an input
source.

#### The row of an archive left the menu, and the menu is now one string

The measurement above found the menu serialised 331 times, and not 4 times. The
document of each planner carried the row of its own archive, so no two
documents were equal and `builtins.toJSON` ran again for each one.

`DDRN_SDIST_ROW` now carries that row, and `levelJson` is a list of the text of
each level. A list is a memo, so the menu is serialised one time for each
level. The row is a few hundred bytes.

| `sdistPlannerDepth` | warm, before | warm, after |
| --- | --- | --- |
| 0 | 1.09 s | 1.13 s |
| 1 | 1.97 s | 1.44 s |
| 2 | 2.97 s | 1.97 s |
| 3 | 3.83 s | 2.16 s |

Measured by setting `sdistPlannerDepth` in `default.nix` to each value and
instantiating twice, taking the second time. The cost of one level fell from
0.93 s to 0.34 s, and the whole warm instantiation fell 41%, from 2.95 s to
1.74 s. **The `.drv` files did not shrink**, because each planner still holds
the menu that names its inputs; what went away is 27.9 MB of JSON that Nix
wrote for each level and then threw away.

The 0.34 s that each level still costs is the construction of 110 derivations
and the hash of the text of each one. Nothing in that is redundant.

**A content-addressed output is what proved the change safe.** The planner of
each source distribution is a different derivation now, so its `.drv` path
changed, and the submitted root of every project that holds a source
distribution changed with it. The environment did not:

```console
$ ddrn/examples/venv-graph/run-uv.sh /tmp/ddrn-uv-compare   # before, and again after
$ diff <(grep '^prefix' before.log) <(grep '^prefix' after.log)
```

`run-uv.sh` built all seven projects on that date, and it builds two of them
now. `tests/test_projects.py` builds the seven, and it is the gate.

Every one of the seven projects built to the same store path. A floating output
takes its path from what the build wrote, so one path means one content. Under
input addressing the path would have moved with the `.drv` of the planner, and
a reader could not tell a rewritten plan from a rewritten environment.

### A planner node spends 97% of its time on one import

The measurements above are the cost of the evaluation, which writes the `.drv`
files. This one is the cost of a planner node while it runs.

`projects/workspace` builds in 4.320 s, and 0.441 s of that is the outer
evaluation. Temporary `time.monotonic()` prints inside `plan.py` gave the
breakdown of one planner node:

| phase | seconds |
| --- | --- |
| the imports of the module | 0.877 |
| open the restricted store | 0.003 |
| parse `DDRN_INPUTS` | 0.000 |
| resolve the lock file | 0.006 |
| build the plan | 0.005 |
| submit the graph | 0.011 |
| everything after the imports | 0.025 |

**The decision work is 25 ms, and the import is 35 times that.** The
instrumentation is gone now, and the import half needs none. It reproduces from
the environment of the planner alone, as the best of five runs:

```console
$ env=/nix/store/...-ddrn-planner-env
$ PYTHONPATH=$packaging:planner "$env/bin/python" -c pass                     # 17 ms
$ PYTHONPATH=$packaging:planner "$env/bin/python" -c 'import packaging.tags'  # 65 ms
$ PYTHONPATH=$packaging:planner "$env/bin/python" -c 'import nanopynix'       # 848 ms
$ PYTHONPATH=$packaging:planner "$env/bin/python" -c 'import ddrn_venv'       # 880 ms
```

So the interpreter starts in 17 ms, and `import nanopynix` is 848 ms of the
880. `python -X importtime` gives 778 ms of self time over 555 modules, by
top-level package:

| package | modules | self |
| --- | --- | --- |
| `nanopynix_proto` | 12 | 274.2 ms |
| `nanopynix` | 53 | 122.5 ms |
| `pydantic` | 41 | 44.9 ms |
| `anyio` | 31 | 29.3 ms |
| `grpclib_transports` | 15 | 20.8 ms |
| `nanopynix_bindings` | 10 | 20.2 ms |

**The planner uses one name of that package, and the name is
`init_libstore`.** Every other name it needs comes from `nanopynix_bindings`,
and `from nanopynix_bindings import expr, store, util` costs 46 ms. The 12
generated modules of `nanopynix_proto` alone are 274 ms, and a planner opens no
RPC session at all.

A submodule does not help, because Python runs the `__init__` of the package
first:

| import | seconds |
| --- | --- |
| `nanopynix.settings` | 0.785 |
| `nanopynix.stores` | 0.819 |
| `nanopynix.rpc` | 0.840 |
| `nanopynix` | 0.848 |

**The cost is the public surface of `nanopynix/__init__.py`, and every consumer
pays it.** That file re-exports `inproc`, `rpc`, `stores`, the settings models
and the generated protocol, so one import loads the whole library. The number
above is therefore not a fact about this lab. Issue #122 holds the measurement.

#### The surface became lazy, and the planner asks for the store alone

`nanopynix/__init__.py` now maps each public name to the module that defines
it, and PEP 562 gives a module a `__getattr__`, so the first read of a name
imports that one module. `nanopynix.libstore` holds `init_libstore` and the
store names, and it imports the compiled bindings and no other module of
nanopynix.

| import | before | after |
| --- | ---: | ---: |
| `python -c pass` | 0.017 s | 0.019 s |
| `import nanopynix` | 0.848 s | 0.030 s |
| `from nanopynix.libstore import init_libstore` | 0.848 s | 0.053 s |
| `import nanopynix.rpc` | 0.848 s | 0.801 s |

The planner reads `init_libstore` from `nanopynix.libstore`, so it pays 53 ms
in place of 848 ms. Its decision work is still 25 ms, and the import is no
longer 35 times that.

**Measured again from the environment of the planner**, with the same command
as above and the best of five runs:

| import | before | after |
| --- | ---: | ---: |
| `python -c pass` | 17 ms | 14 ms |
| `import packaging.tags` | 65 ms | 64 ms |
| `import nanopynix` | 848 ms | 23 ms |
| `from nanopynix.libstore import init_libstore` | 848 ms | 49 ms |
| **`import ddrn_venv`** | **880 ms** | **140 ms** |

`import ddrn_venv` loads 176 modules, against 555 for `import nanopynix`
alone before. `nanopynix_proto`, `pydantic`, `grpclib`, `anyio` and
`betterproto2` are absent from `sys.modules`, and the planner holds
`nanopynix`, `nanopynix.libstore` and the compiled bindings.

**`packaging` is now the largest import of a planner**, at 64 ms against 49 ms
for the store surface. The decision work is 25 ms, so the import is 5.6 times
the work rather than 35 times it. A planner runs one time for each source
distribution of a project, so this is a cost of each node and not of the
build.

**A consumer that reads a name from every module pays for every module.**
`pynix` is one, and its start did not change: 2.09 s, of which `lsprotocol` is
394 ms. Issue #107 covers what `pynix` itself asks for. The change moves the
cost to the caller that asks for the name, and it removes no cost that a
caller asked for.

`tests/nanopynix/test_import_surface.py` gates this by what loads and not by
how long it takes, because a ceiling in milliseconds measures the machine.

One planner runs for each source distribution of a project, at each level of
the recursion, so the cost multiplies with the count of source distributions
and not with the count of packages. `run-uv.sh` builds two projects, and four
planner nodes run in it.

### How uv2nix resolves, and what each step is for

`lib/lock1.nix:50`, `resolveDependencies`. It takes the lock, a PEP 508
environment, and the names to start from. Five steps:

1. **Drop each package that this environment cannot reach.** A package carries
   `resolution-markers`, and the package survives when one of those markers
   holds. `lock1.nix:79`.
2. **Drop each dependency edge that this environment cannot reach.** An edge
   carries a `marker`. `filterPackage`, `lock1.nix:231`.
3. **Walk the edges from the start set.** `genericClosure`, `lock1.nix:111`.
   An edge names a version only when the resolution is ambiguous, so an edge
   with no version admits every candidate of that name. `lock1.nix:127`.
4. **Reduce each name to one package.** `reduceDependencies`, `lock1.nix:144`.
   A name with more than one candidate is narrowed by the versions that the
   other reached packages pin for it, and the step repeats until each name has
   one. It gives up after 100 rounds.
5. **Assert.** `requires-python` against the interpreter, and
   `supported-markers` and `required-markers` against the environment.
   `overlays.nix:126`.

`filterConflicts` (`lock1.nix:250`) runs before all of this. A `conflicts`
declaration names a set of extras and groups that may not appear together, and
the caller passes the one it wants. The function removes the others from the
lock, so the resolver sees a lock with no conflict.

**Step 4 is the part that a lock of a current uv rarely needs.** uv writes
`resolution-markers` on each package of a fork, so step 1 already leaves one
candidate. Step 4 answers an older lock, where the edges carry the
disambiguation and the packages do not. `lock1.nix:166` says so in the message
that it throws.

### How uv resolves the same file, and why it needs no step 4

`Installable::to_resolution`, in `crates/uv-resolver/src/lock/installable.rs`.
It takes a marker environment, the tags, the extras and the groups, and the
roots. Three steps:

1. **Find the entry of each root.** `Lock::find_by_name`, which fails when more
   than one entry carries the name. `Lock::find_by_markers` answers the same
   question for a requirement of the manifest, and it uses
   `resolution-markers` to choose.
2. **Walk the edges from the roots.** The queue holds a package and one extra
   of it, and the marker of each edge decides whether uv follows that edge.
3. **Follow each edge to the entry that it names.** `Lock::find_by_id`, which
   is a lookup and not a search.

**Step 3 is a lookup because an edge names one entry.** uv identifies a package
by a `PackageId`, which is the name, the version and the source together. An
edge of the lock file carries all three whenever the name is ambiguous, and it
carries none of them when one entry holds the name. `TryFrom<LockWire> for Lock`
counts the entries of each name first, and its comment gives the reason: the
two fields are "strictly redundant" for a name that one entry carries.

**uv refuses a lock file whose edge is short for an ambiguous name.** The two
messages are `Dependency `a` has missing `source` field but has more than one
matching package`, and the same sentence for `version`.
`PackageIdForDependency::unwire` raises them. Only a directory may omit the
version, because the version of such a project may be dynamic;
`Source::is_source_tree` names the three kinds that count, and they are
`directory`, `editable` and `virtual`.

**uv applies no fork filter before the walk.** A package of a fork that this
environment does not take drops out because no live edge reaches it, and not
because a step removed it. `resolution-markers` decides a root, and it sets the
preferences of the next resolution. The field of `Package` says the second part
itself.

`ddrn_venv._uvlock` follows these three steps, and it therefore has no step 4.
`uv2nix` needs one because its resolver reads the version of an edge and not the
source (`lock1.nix:127`), so a name that two entries carry stays a choice for it.

#### A marker of a lock file is simplified, and two facts make it readable

**uv writes each marker in a simplified form, and the type says what that form
needs.** `SimplifiedMarkerTree`
(`crates/uv-distribution-types/src/requires_python.rs`) carries the rule:

> A simplified marker should only exist in contexts where a `requires-python`
> setting can be assumed. In order to get a "normal" marker out of a simplified
> marker, one must re-contextualize it by adding the `requires-python`
> constraint back to the marker.

uv adds the constraint back with `into_marker`, at the moment it reads the file.
This lab asserts the constraint instead, in `Lock.check_environment`, and it
reads each marker as written. The two are the same answer for an environment
that satisfies `requires-python`, and the assertion is what makes them the same.
So the order in `resolve` is load bearing: assert first, evaluate second.

**A second simplification applies to an edge, and the walk answers it.**
`simplify_dependency_marker` removes from an edge "the PEP 508 conditions that
must already hold to reach its parent node". An edge marker read alone is
therefore weaker than the condition it stands for. A walk evaluates the marker
of an edge only after it reaches the parent of that edge, so the removed
conditions already hold, and the two agree again. A reader that filtered every
edge of the file up front would still agree, and a reader that answered "does
this edge hold" for one edge in isolation would not.

#### The source is a part of the identity, and dropping it takes the wrong file

**Two entries of one lock file may carry the same name and the same version.**
`lock_multiple_sources_index_disjoint_markers`, a test of uv
(`crates/uv/tests/lock/lock.rs`), writes `jinja2` 3.1.3 twice: once from the
`cu118` index of PyTorch and once from `cu124`. The two entries differ in the
source and in nothing else, and the two edges of the root differ in the source
and the marker:

```toml
dependencies = [
    { name = "jinja2", version = "3.1.3", source = { registry = ".../cu118" }, marker = "sys_platform == 'win32'" },
    { name = "jinja2", version = "3.1.3", source = { registry = ".../cu124" }, marker = "sys_platform != 'win32'" },
]
```

A reader that identifies a package by the name and the version finds the two
entries equal, and it installs whichever one it met first. Both wheels are
named `Jinja2-3.1.3-py3-none-any.whl`, so nothing downstream says that the
answer is wrong. 13 lock files of the tests of uv hold such a pair. The common
one is `torch` from the CPU index beside `torch` from the CUDA index, which is
the usual reason to hold two indexes at all.

`tests/fixtures/locks/two-indexes.lock` is that file, copied from the test of
uv that produced it. `TestThePackageIdentity` resolves it for two environments,
and asserts the index of each answer.

### What a lock file pins, and what it leaves open

**uv.lock pins the hash of every artefact, and a reader trusts every one of
them.** It also pins the version of every package, and the edges between them,
because uv resolved them. Nothing in this lab solves a version or queries an
index. `[package.metadata].requires-dist` is the question that the project
asked, and the reader takes `dependencies`, which is the answer that uv gave.
The section below is the one place that reads the question, and it resolves
nothing there either: it only asks whether the project still asks the same
question.

**What a lock file does not pin is which of those apply to one machine**,
because the file covers many machines at once. Measured over
`anthropic-sdk-python/uv.lock`, which has 103 packages:

| what is open | the measurement |
| --- | --- |
| which wheel of a package | `multidict` ships **145** wheels for one version, and `pydantic-core` ships 120. PEP 425 tags choose. |
| which packages at all | **96** dependency edges carry a PEP 508 marker. |
| which fork | **20** packages carry `resolution-markers`, so the lock holds more than one version of them. |
| which extras and groups | the caller chooses, and a `conflicts` declaration says which choices are illegal. |
| how to build a source distribution | **98 of the 103** packages ship one, and the lock records no build system for any of them. |

So a reader takes every fact of the lock file, and it answers the five
questions above from the target. That is selection, and not resolution.

### A lock file records the declaration, and a project can grow past it

**A user who edits `pyproject.toml` and does not run `uv lock` has a lock file
that resolves the old declaration.** uv refuses such a file. `Lock::satisfies`
(`crates/uv-resolver/src/lock/mod.rs`) compares the two and gives one of 21
`SatisfiesResult` values, and `MismatchedPackageRequirements` is the one for
this case. A reader that skipped the comparison would build the old
environment and say nothing, which is the worst of the three answers.

`ddrn_venv._freshness` makes the part of that comparison that is exact.
`uv.nix` puts the `pyproject.toml` of each member into the store,
`default.nix` passes each path through as `uvDeclarations`, and
`UvProject.selection` compares before it resolves.

**It compares the name, the extras and the specifier, and nothing else.**
`normalize_requirement` of the same file says what uv rewrites before it
compares: it sorts the extras and the groups, it rebuilds a git or a path
source from its fields, and it calls `requires_python.simplify_markers` on the
marker. **It does not touch the specifier.** So those three fields are exactly
comparable, and the marker and the source are not. A marker-only edit
therefore passes this check, and `uv lock --check` is the command for the whole
answer.

**Four rules decide what each side holds, and none of them is the table of the
same name.** Each rule came from the source of uv, and not from the output of a
command:

| rule | where uv states it |
| --- | --- |
| An extra is a row of `requires-dist` under an `extra == '...'` marker, so the declared side is the union of `dependencies` and every `optional-dependencies` list. | `RequiresDist::from_project_maybe_dynamic`, `crates/uv-pypi-types/src/metadata/requires_dist.rs` |
| `tool.uv.dev-dependencies` is the group `dev`. | `FlatDependencyGroups::from_pyproject_toml`, `crates/uv-workspace/src/dependency_groups.rs`, with `DEV_DEPENDENCIES` in `crates/uv-normalize/src/group_name.rs` |
| `include-group` is resolved, through as many levels as it takes, before uv writes `requires-dev`. | `resolve_group` of the same file |
| A dynamic `dependencies` or `optional-dependencies` table is not in the file, so uv builds the package to learn it. | `MetadataError::DynamicField`, same file as the first rule |

A fifth rule costs nothing to follow: uv drops a group that states no
requirement from both sides, with
`self.includes_empty_groups() || !requirements.is_empty()`. An empty group
compares equal to an absent one, so this reader agrees without a special case.

**A lock file of an older uv writes no `[package.metadata]` at all.** uv has the
same case, and it calls it `allow_missing_package_metadata`. A reader cannot
tell such a file from a member that declares nothing, so the check compares
neither.

**A marker-only edit passes this check**, because uv simplifies every marker
before it compares. That is the one gap that stays, and `uv lock --check` is
the command for it.

`ddrn/examples/venv-graph/tests/test_freshness.py` holds one test for each rule
above, against the lock files that uv wrote in `tests/fixtures/locks`. The seam
was proved end to end by adding `typing-extensions` to
`projects/pure-app/pyproject.toml` and building `uv.nix` for that project:

```
ddrn_venv._uvlock.LockError: uv.lock is stale: 'pure-app' declares different
dependencies than the lock file resolved. pyproject.toml adds
['typing-extensions']. Run `uv lock` and build again.
```

### A workspace can grow a member, and no declaration reaches that one

**The comparison above reads one declaration for each package of the lock
file**, so a member directory that `uv.lock` never saw reaches no comparison at
all. uv finds that one, and it calls the difference
`SatisfiesResult::MismatchedMembers`. The two sides of it are
`[tool.uv.workspace] members` of the file system, and `[manifest] members` of
the lock file.

**Only the evaluator reads a directory that is not in the store, so the walk is
in Nix and every rule is in Python.**
`ddrn/examples/venv-graph/workspace.nix` lists the `pyproject.toml` of each
directory under the workspace and decides nothing: it reads no glob, it opens
no file, and it holds no rule of uv. `ddrn_venv._workspace` takes that listing
and answers which directories are members, with `fnmatch` of the standard
library for each glob component. `ddrn_venv._freshness.check_members` compares
the answer with `[manifest] members`.

That split is the argument of the lab applied once more, and it is the reason
`**`, `[a-z]` and `[!x]` all work: each one is a case of `fnmatch`, and not a
new branch of the Nix language.

**uv writes an empty `[manifest] members` for a workspace of one member that is
a project.** `LockTarget::members`
(`crates/uv/src/commands/project/lock_target.rs`) clears the list in that one
case, and the comment there gives the reason: such a workspace mismatches on
the member itself. So every single project has an empty list, and a reader that
did not clear the same list would call every single project stale.

**Five rules decide what counts as a member, and each one comes from
`collect_members_only` of `crates/uv-workspace/src/workspace.rs`:**

| rule | what this reader does |
| --- | --- |
| The root is a member when it declares `[project]`, and uv adds it before it expands one glob. | adds the root name; a root with no `[project]` is `Workspace::is_non_project` |
| An `exclude` glob takes a directory out. | expands `exclude`, and drops each match |
| `tool.uv.managed = false` takes a directory out. | skips it |
| A directory with no `pyproject.toml` is no member. | it is not in the listing, so it is skipped |
| A duplicate directory is read once. | takes the unique list |

**Two cases turn the comparison off, and each one gives `None`.** A false alarm
stops a build that is correct, which is worse than the gap that the comparison
closes:

| the case | why this reader cannot answer it |
| --- | --- |
| a glob that holds `**` inside a component | the `glob` crate raises `recursive wildcards must form a single path component`, so uv reads no such workspace |
| a `pyproject.toml` with no `[project]`, or one that TOML cannot read | uv raises `MissingProject`, and the build that follows fails for its own reason |

### What each glob of `[tool.uv.workspace] members` means

`Pattern::new` of `glob-0.3.4/src/lib.rs` gives the whole token set, and uv
calls that crate with `MatchOptions::new()`:

| the form | what it is |
| --- | --- |
| `?` | `AnyChar`, one character |
| `*` | `AnySequence`, any characters of one component |
| `**` | `AnyRecursiveSequence`, zero or more whole components |
| `[abc]`, `[a-c]` | `AnyWithin` |
| `[!abc]` | `AnyExcept` |
| `{a,b}` | **not alternation.** No token holds it, so both braces are literal characters |

`**` forms a whole component or the crate refuses the pattern: `a/**b`,
`a/bc**`, `a/*****` and `a**b` each give
`recursive wildcards must form a single path component`, and the tests of the
crate name all four. It matches zero or more levels, it matches directories
only, and `Paths::next` returns the base directory as well when the pattern
ends in it, so `packages/**` names `packages`.

**`packages/**` is a pattern that uv refuses for most trees, and that is a
measured fact and not an opinion.** `**` names every directory at every depth,
so the `src` of a member becomes a member with no `pyproject.toml`. Over
`projects/workspace` with that one edit:

```console
$ uv lock --check --offline --python <the interpreter>
error: Workspace member `.../packages/ws-cli/src` is missing a `pyproject.toml`
       (matches: `packages/**`)
```

This reader accepts that workspace, because such a directory is not in the
listing at all. It is a lock file that uv cannot write, so a build of it fails
for its own reason. **`packages/*` is the form that a workspace writes**, and
the reader answers it exactly.

### Two boundaries of the walk, and what each one costs

`workspace.nix` does not descend into `.git`, `.jj`, `node_modules`,
`__pycache__`, `.venv` and eight more names, and it follows no symbolic link.
uv reaches both, so a member behind one is not found here.

The reason is the cost of the walk, measured: this repository holds **1902
directories**, and a complete recursive `builtins.readDir` of all of them adds
**0.3 s** to an evaluation that costs 5.0 s to import `lib` at all.

```console
$ nix eval --impure --json --expr 'let walk = dir: ...; in walk /path'
```

The seam was proved end to end, and uv agreed on each tree. A copy of
`projects/workspace` took a fourth member directory and a fourth entry in
`[tool.uv.workspace] members`, with no `uv lock` after it:

```
ddrn_venv._uvlock.LockError: uv.lock is stale: it resolved a different set of
workspace members. the workspace adds ['ws-extra']. Run `uv lock` and build
again.
```

```console
$ uv lock --check --offline --python <the interpreter>
error: The lockfile at `uv.lock` needs to be updated, but `--check` was provided.
```

The same copy with `members = ["packages/*"]` in place of the two literal paths
built the same derivation, `workspace-root.drv`, and `uv lock --check` over it
resolved 14 packages and said nothing. The unchanged fixture did the same.

### uv itself is the backstop, and a sandbox can run it

The two comparisons above are the ones that a build reaches, and
`Lock::satisfies` holds 21. The `uvCheck` argument of `uv.nix` runs
`uv lock --check --offline` over the whole project, in a `runCommand` that the
planner waits for, so the other nineteen stop the build as well.

**A refusal with no network is still a refusal.** uv reaches the network only
when `Lock::satisfies` gave it a reason to resolve again, so a lock file that
satisfies the project downloads nothing. Measured over `projects/workspace`
with an empty cache directory:

| the tree | what uv did | how long |
| --- | --- | --- |
| unchanged | `Resolved 14 packages in 1ms`, exit 0 | 27 ms |
| one member added to `members` | `No solution found`, with the hint `Packages were unavailable because the network was disabled`, exit 1 | — |

**The message differs with the cache, and the verdict does not.** With a warm
cache uv resolves again and says `The lockfile at uv.lock needs to be updated`.
With the empty cache of a sandbox it cannot resolve at all, so it reports the
package it could not read. `uv.nix` prints its own paragraph after the failure,
because the uv message names the network and the cause is the lock file.

### `--check` is the one mode that a derivation can read

The verdict of a derivation is the exit code of its builder, so a mode that
exits 0 over a stale project is no gate. `LockMode`
(`crates/uv/src/commands/project/lock.rs:289`) holds four, and three of them
do:

| the mode | what it does | over a project that grew a member |
| --- | --- | --- |
| `uv lock` | resolves, and writes `uv.lock` | writes a new file, exits 0 |
| `--dry-run` | resolves, and writes nothing | prints the difference, exits 0 |
| `--frozen` | reads the lock file, and resolves nothing | exits 2 here, and 0 for a marker |
| `--check` | asserts the file would not change | exits 1 |

The `--dry-run` and `--check` rows are measured, over a copy of
`projects/workspace` with a fourth member and with the network up:

```console
$ uv lock --dry-run   # exit 0, uv.lock untouched
$ uv lock --check     # exit 1, uv.lock untouched
```

`uv lock` is the worst of the three in a sandbox: it rewrites the copy of the
lock file, so the build plans from the file that the graph came from and never
learns of the difference. The doc comment of `--check`
(`crates/uv-cli/src/lib.rs:3934`) states the behaviour that a gate needs:

> Asserts that the `uv.lock` would remain unchanged after a resolution. If the
> lockfile is missing or needs to be updated, uv will exit with an error.
> Equivalent to `--locked`.

**`--frozen` is a diagnostic, and uv says so itself.** It compares the lock
file with the project and resolves nothing, so it names a structural difference
exactly, and it needs no index:

```
error: The lockfile at `uv.lock` needs to be updated, but `--frozen` was
       provided: Missing workspace member `ws-extra`.
```

Over the marker edit it exits 0, with this warning:

```
warning: The lockfile at `uv.lock` was only checked for validity, not whether
it is up-to-date, because `--frozen` was provided; use `--check` instead
```

`uv.nix` runs `--frozen` after `--check` refused, and never before. The reason
is that warning: a successful check would otherwise print it on every build.

Two measurements say what the argument costs:

- **The check changes no output.** `projects/pure-app` submitted the same root
  derivation `s4k36ain6j2h1iz11q1615b4aykf2ibm-pure-app.drv` with the argument
  on and with it off, and built
  `/nix/store/2ppg8xs82055lxw8vg22a1zbbvjnmfjc-pure-app` either way.
- **It raises no false alarm.** All seven projects of
  `ddrn/examples/venv-graph/projects` pass it, and each of the seven holds a
  source kind that the ones before it do not.

The price is the input. The rest of `uv.nix` reads `pyproject.toml` and
`uv.lock`, so an edit of any other file changes no derivation. The check takes
the whole tree, so every edit makes a new store path and a new check. The
argument is off by default for that reason.

**It closes the marker gap, which is measured too.** A copy of
`projects/workspace` took `; sys_platform == 'darwin'` on the `rich` dependency
of `ws-cli`, with no `uv lock` after it. The planner accepted that tree and
built an environment of 14 nodes that still holds `rich` on Linux.
`uv lock --check` refused the same tree.

### A marker of an edge is not always a PEP 508 marker

**uv encodes a conflict into the `extra` of a marker, and it says so.** The doc
comment of `UniversalMarker` (`crates/uv-resolver/src/universal_marker.rs`)
gives the design and the reason for it:

> we technically preserve PEP 508 compatibility but abuse the `extra` attribute
> to encode conflicts

`encode_package_extra` of the same file writes the value, and it takes one of
three shapes:

| the value | what it means |
| --- | --- |
| `extra-7-example-foo` | the extra `foo` of the package `example` is enabled |
| `group-7-example-dev` | the group `dev` of the package `example` is enabled |
| `project-7-example` | the production dependencies of `example` are enabled |

The number is the length of the package name in bytes. uv puts it there so that
a reader can find where the package name ends, because an extra name may hold a
hyphen as well. uv evaluates such a marker against the extras and the groups
that the caller enabled, and `Installable::to_resolution` passes all three sets
to `UniversalMarker::evaluate`.

**A reader that gives `extra` an empty value answers a different question.** An
edge of `aiohttp` in `anthropic-sdk-python/uv.lock` reads:

```text
python_full_version < '3.11' or (extra == 'extra-9-anthropic-mcp' and extra
== 'group-9-anthropic-pydantic-v1') or (extra == 'group-9-anthropic-pydantic-v1'
and extra == 'group-9-anthropic-pydantic-v2')
```

An `extra` cannot hold two values at once, so each of those two clauses is
false for any environment. They are conflict markers that uv did not simplify
away, which is `astral-sh/uv#11513` and which `lib/lock1.nix:85` of uv2nix
names. An empty `extra` gives the right answer for this one marker, because the
marker then reduces to the version test that it already carried.

**It gives the wrong answer for a satisfiable one.** uv writes
`marker = "extra == 'project-7-example'"` on an edge that a workspace member
reaches, and 213 edges of the lock files in the tests of uv carry such a marker.
An empty `extra` makes that edge false, and the package leaves the environment
with no message. One `extra != '...'` fails the other way, and keeps a package
that the selection did not ask for.

`ddrn_venv._uvlock` therefore refuses an edge whose marker holds this encoding,
and the message decodes each value. `TestAConflictMarkerIsNotPep508` covers the
three shapes. The alternative is to implement `UniversalMarker::evaluate`, and
that needs the activated extras, groups and projects of the whole walk. uv
itself records two soundness gaps in how it accumulates those sets, in a comment
in `to_resolution_from_packages`.

### The marker environment, and the two keys that no build can know

`pep508.mkEnviron` of pyproject.nix (`lib/pep508.nix:683`) builds the PEP 508
environment from an interpreter derivation. It sets `platform_release` to `""`
and `platform_version` to an empty version, and the comment gives the reason:

> platform_release maps to platform.release() which returns the running kernel
> version on Linux. Because this field is not reproducible it's left empty.

**A build cannot know the kernel of the machine that will run the
environment.** So a marker that reads either key evaluates against an empty
value, and uv2nix lets the caller override the environment for that case.

### A key that a planner does not pass takes the value of the build machine

`Marker.evaluate(environment)` of `packaging` does not use `environment` alone.
It merges `environment` over `default_environment()`, and
`default_environment()` reads the interpreter that is running. **So a key that
a planner does not pass silently describes the machine that plans, and not the
target.**

Measured with `packaging` 26.2, on a host whose kernel is 7.1.4:

```text
host platform_release = 7.1.4
marker 'platform_release != ""' with {}   -> True
same with an explicit ''                  -> False
marker 'python_version >= "3.14"' with {} -> True
same with an explicit '3.11'              -> False
```

A planner plans for a target that is not the machine it runs on, so it must
pass **every** key of PEP 508:

```text
implementation_name  implementation_version  os_name
platform_machine     platform_python_implementation
platform_release     platform_system         platform_version
python_full_version  python_version          sys_platform
```

The present lab passes six of the eleven, so five of them describe the
builder. `packaging` raises no error for this, because it has a value for
each one.

### What a virtual environment needs from the edges, and what it does not

uv2nix builds a package **set**: one derivation for each package, and each one
carries its dependencies, because the builder of pyproject.nix composes a
`PYTHONPATH` for each build.

This lab builds one flat environment. A wheel installs on its own, and the
order does not matter, so the edges answer one question only: **which packages
are members**. The closure of the roots is the member list, and nothing else
reads an edge.

The one exception is a source distribution, and its build dependencies do not
come from the lock at all. They come from the archive, which is the gap that
this lab closes.

## How uv2nix layers onto pyproject.nix

**The base is a scope, and the layer is an overlay over that scope.**
`build/packages.nix:96` of pyproject.nix builds the package set with
`lib.makeScope`, so the set carries `overrideScope`. `lib/overlays.nix:71` of
uv2nix returns `final: prev: mapAttrs (name: package: callPackage ...)`, which
is one such overlay.

**The contract between the two halves is small.** A package of the layer is a
`stdenv.mkDerivation` that carries `pyprojectHook` in `nativeBuildInputs`, and
that sets three `passthru` attributes: `dependencies`,
`optional-dependencies` and `dependency-groups` (`uv2nix/lib/build.nix:272`).
Everything else, the resolvers and `mkVirtualEnv` and the hooks, belongs to the
base and never reads the lock file.

**The layer decides membership, and the base decides how one package builds.**
`lock1.resolveDependencies` walks the lock, and `build.remote` or `build.local`
turns each chosen package into a derivation.

**The canonical stack has four layers, and the third one exists only to patch
the second.** From `doc/src/usage/getting-started.md` of uv2nix:

```nix
pythonBase = pkgs.callPackage pyproject-nix.build.packages { inherit python; };
overlay    = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
pythonSet  = pythonBase.overrideScope (lib.composeManyExtensions [
               pyproject-build-systems.overlays.wheel
               overlay
             ]);
venv       = pythonSet.mkVirtualEnv "hello-world-env" workspace.deps.default;
```

`pythonBase` holds the hooks and the resolvers and no package at all.
`pyproject-build-systems` is a separate repository, and it is there because
**uv does not lock a build system**; uv2nix names the upstream issue,
`astral-sh/uv#5190`, and says "uv2nix doesn't try to hide this deficiency".

**An editable needs its own overlay and its own package set**, and the root it
takes is a reference to a variable:

```nix
editableOverlay = workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; };
editablePythonSet = pythonSet.overrideScope editableOverlay;
```

with `export REPO_ROOT=$(git rev-parse --show-toplevel)` in the shell hook.
**This lab arrived at the same interface**, `root = "$DDRN_EDITABLE_ROOT"`, and
for the same reason: a literal root would put the tree into what the
environment builds.

**pyproject.nix does not merely prefer that form. It refuses the other one.**
`build/lib/renderers.nix`, in `mkDerivationEditable`:

```nix
assert isString root;
assert assertMsg (!hasPrefix storeDir root) ''
  Editable root was passed as a Nix store path string.
  …
  Pass editable root either as a string pointing to an absolute path non-store
  path, or use environment variables for relative paths.
'';
```

The same message names the trap that catches everyone: a flake copies its
source into the store, so an inferred `projectRoot` **is** a store path, and
`--impure` is the workaround it offers. This lab bakes `$DDRN_EDITABLE_ROOT`
and never a store path, so it obeys the same rule. `check.nix` then points that
variable at a store path **at run time**, which is a different thing and is
what makes the two check derivations read one environment.

**uv2nix picks an interpreter from `requires-python`.**
`pyproject-nix.lib.util.filterPythonInterpreters` filters
`pkgs.pythonInterpreters` by the range that the workspace declares. This lab
takes `PYTHON_VERSION` from `default.nix` and asserts nothing.

**uv encodes a conflict as a synthetic marker.** `lib/overlays.nix:26` names
the encoding, from `crates/uv-resolver/src/universal_marker.rs` of uv:
`extra-{package_len}-{package}-{extra}` and
`group-{package_len}-{package}-{group}`, where the length disambiguates a name
that holds a hyphen. uv2nix has to synthesise those extras into the marker
environment for the resolution markers of a conflicted lock to evaluate.

**Poetry has no layer.** `pyproject.nix/lib/poetry.nix` is 323 lines, and it
translates `[tool.poetry]` metadata into the PEP 621 shape
(`translatePoetryProject`) and parses the caret and tilde version syntax of
poetry into PEP 440 (`parseVersionCond`). **It never reads `poetry.lock`**, no
fixture of that name exists in the tree, and nothing under `build/` mentions
poetry. So the pyproject.nix ecosystem stops at poetry metadata, and
`poetry2nix` is the separate project that consumes the lock.

### `poetry.lock`, and what it holds

Two checkouts on this machine give the two formats. `~/Code/pynixutil` is lock
version 1.1, with 15 packages. `~/Code/alpaca-py` is lock version 2.1, written
by Poetry 2.2.1, with 84 packages.

**Both hold the dependency edges, and a `uv.lock` reader and a `poetry.lock`
reader therefore walk the same way.** Each `[[package]]` carries `name`,
`version`, `python-versions`, a `[package.dependencies]` table of PEP 508
constraints with optional markers, and a `[package.extras]` table:

```toml
[package.dependencies]
click = ">=8.0.0"
tomli = {version = ">=1.1.0", markers = "python_full_version < \"3.11.0a7\""}
```

**The two versions differ in two places, and a reader must handle both.**

- Version 1.1 puts the artefacts in one `[metadata.files]` table at the end of
  the file, keyed by package name. Version 2.1 puts a `files = [ … ]` list
  inside each `[[package]]`. The measured counts are 1 `[metadata.files]`
  section against 84 inline `files = [` lists.
- Version 1.1 marks a package with `category = "dev"`. Version 2.1 replaces
  that with `groups = ["main"]`, which holds a list.

**Neither version records a URL.** Every artefact is a `{file, hash}` pair:

```toml
{file = "pytest-7.1.3-py3-none-any.whl", hash = "sha256:1377bda3466d…"},
```

A reader must therefore build the URL from the file name and an index, which
is what `lock.json` of this lab already holds directly. This is the one place
where `poetry.lock` gives less than `uv.lock`, which records the URL.

**Neither version records a build system**, exactly as `uv.lock` does not. So
the source distribution problem is identical under both, and step 4 below
serves both readers.

**`[metadata]` carries `lock-version`, `python-versions` and `content-hash`.**
The `content-hash` is the hash of the relevant part of `pyproject.toml`, and it
is how a reader tells that a lock file is stale.

**The same layering suits this lab, and the interface already exists.**
`plan.py` builds one document and hands it to `graph.nix`:

```json
{ "system": …, "pythonVersion": …, "tools": …, "scripts": …,
  "nodes": [ … ], "installer": …, "editables": …, "venv": { … } }
```

`graph.nix` is the base: it says how a node of each kind is built, and it reads
no lock file. A lock reader is anything that produces `nodes`. So a `uv.lock`
reader and a `poetry.lock` reader are two readers over one base, and the base
does not change when a second one arrives.

## Can the builders of pyproject.nix be passed into the planner?

Three tiers exist, and the probe of `ddrn/NIX-FACTS.md` measured the first two.

**Tier 1, the `lib` of nixpkgs: yes, and it is free.** `import (nixpkgs +
"/lib")` inside the sandbox costs 0.0 s and no store operation, because `lib`
is pure Nix. Any pure Nix library travels the same way, and that includes the
whole of `pyproject.nix/lib`, the 3602 lines of PEPs. **This lab needs none of
it**, but the mechanism is proved.

**Tier 2, nixpkgs itself: it imports, and it cannot yet instantiate.**
`import nixpkgs { ... }` returns a set in 0.1 s. `hello.drvPath` then fails on
`QueryPathInfo`, which the allowlist refuses. So the answer to "pass in
`${nixpkgs}` and get the complete shebang" is **not yet, and the gap is one
more allowlist entry or a few**.

**Tier 3, the built store paths: this is what the lab does now, and it needs no
nixpkgs inside the sandbox at all.** `default.nix` evaluates outside, and hands
the planner the output path of each tool and each script. `graph.nix` calls the
`derivation` primitive over those paths.

**The builders of pyproject.nix do not travel by tier 3 on their own, because
they are `stdenv.mkDerivation` and a set of setup hooks.** A setup hook is a
bash mechanism of `stdenv`, and `mkDerivation` is a Nix function of about a
thousand lines in `pkgs/stdenv/generic/make-derivation.nix`, with dozens of
optional arguments. Its result is a plain `derivation` call whose `builder` is
bash and whose `args` are `[ "-e" ./default-builder.sh ]`, and
`default-builder.sh` holds one word, `genericBuild`, which `$stdenv/setup`
defines. So the *runtime* half of `mkDerivation` is one store path, and the
*evaluation* half is the thousand lines.

### Tier 4, the pre-instantiated menu, and why it replaces tier 2

**Tier 2 is the wrong tier, and the reason is not the allowlist.** The sandbox
has no network and sees only the store paths that the derivation declares.
An evaluation of nixpkgs inside it would produce derivations whose sources
were never fetched and whose inputs are not present, so the allowlist entry
that tier 2 waits for buys a capability that still cannot be used. The choice
of what nixpkgs offers has to be made outside, where the network and the whole
store are available.

**Tier 4 makes that choice outside and hands in the result.** Ordinary Nix
evaluation instantiates the candidates, and the planner receives a menu of
`{ name, version, drv, out }`. `ddrn/NIX-FACTS.md` measures the mechanism, and
the two facts that make it work are:

- `builtins.unsafeDiscardOutputDependency value.drvPath` puts the `.drv` file
  and its whole `.drv` closure in the sandbox, and **builds no output**.
- `builtins.unsafeDiscardStringContext value.outPath` gives the output path as
  a plain name, so the planner can refer to a package it did not build.

**The worst case is affordable.** All of `python3Packages` is 10558 packages,
35994 `.drv` files, a 154.4 MiB closure and 17.7 s of evaluation, with nothing
built. The realistic case is far smaller, because the closures overlap: after
`numpy`, two more packages cost 28 more `.drv`.

**Carry the menu in a file, and not in the environment.** 2.2 MB of JSON in an
environment variable fails with `Argument list too long`. `pkgs.writeText`
holds it, and the reference scan of that output pulls the whole menu closure
in behind one input path.

**The return path is proved.** A planner took `hello` as a menu entry, wrote a
node whose `input_drvs` names that `.drv`, and submitted it. The outer store
then **substituted `hello` from `cache.nixos.org`** and ran the node, which
printed `Hello, world!`. `ddrn/NIX-FACTS.md` holds the log.

That substitution is the fact that decides the design. A menu entry keeps the
identity that nixpkgs gave it, so an override costs a download rather than a
compile. Tier 2 could never offer this: a derivation that the sandbox
instantiates itself is a new derivation, and no cache has ever seen it.

**So tier 4 answers the override question that tier 2 was asked for**, and it
needs no allowlist entry beyond what `builder-rpc-v0` already released.

## What is expensive there and cheap here

**Markers, tags, specifiers and name normalisation.** 3602 lines of Nix in
`pyproject.nix/lib`, against four import lines in `plan.py`.

**Forked resolution.** Most of `uv2nix/lib/lock1.nix`, against a filter over
the `Target` that `plan.py` already builds: a package is a candidate when it
carries no `resolution-markers`, or when one of them evaluates true.

**The build system of a source distribution.** uv2nix cannot close this, and
tells the user to write an overlay. A planner **can** close it, and the way is
already tested: `tests/functional/dyn-drv/eval-submit.sh` of the patched Nix
runs a three level chain, a planner that submits a planner that submits a
graph. So the outer planner can give each source distribution its own planner
node, with the source archive as a real input. That inner planner reads the
`pyproject.toml` of the archive, resolves the backend against the same lock,
and submits the build node.

**This is the strongest case for the whole feature that this lab has found**,
and it is stronger than the `idna` case, because uv2nix does not merely find it
awkward. uv2nix cannot express it at all: the answer is inside a file that no
evaluation has downloaded.

## What the environment costs at run time, measured

The measurements above are the cost of the **evaluation**. This one is the cost
of what the user runs. It is a different number, and the whole of it is the
interpreter.

The subject is `projects/pure-app`, whose environment holds `rich` and its two
dependencies. `tests/test_projects.py` builds it into a private store, and the
method is one command:

```console
$ nix --store "$store" path-info -r --json <the environment>
```

| path | NAR bytes | share |
| --- | --- | --- |
| `python3-3.14.6` | 141459368 | 59.4% |
| `glibc-2.42-67` | 35073096 | 14.7% |
| `pygments-2.20.0` | 13441424 | 5.6% |
| the 24 other paths | 48225088 | 20.2% |
| the environment itself | 21656 | 0.0% |
| **the closure** | **238220632** | **28 paths** |

**The environment holds nothing that it should not.** It is 21656 bytes,
because a virtual environment is a tree of symbolic links. The three
distributions that the user asked for are 19 MB of the 238 MB. Nothing that
built the environment is in it: neither `packaging` nor `installer` appears in
the closure, because each one is an input of a build step and not a member.

**So the question is the interpreter, and one file of it is 28% of everything.**
`du -sb` over the store path of `python3` gives the breakdown:

| inside `python3-3.14.6` | bytes |
| --- | --- |
| `lib/python3.14/config-3.14-x86_64-linux-gnu/libpython3.14.a` | 66397194 |
| `lib/python3.14/__pycache__` | 15449098 |
| `lib/libpython3.14.so.1.0` | 7474640 |
| `lib/python3.14/idlelib` | 7040073 |
| `lib/python3.14/lib-dynload` | 6884096 |
| `lib/python3.14/test` | 2743124 |
| `lib/python3.14/pydoc_data` | 2411476 |
| `lib/python3.14/tkinter` | 1458792 |

`libpython3.14.a` is the static archive that builds an extension module. No
file of the environment names it, and `grep -rl config-3.14` over the
environment finds nothing.

### The two interpreters that nixpkgs builds, and what each one costs

nixpkgs builds `python3` and `python3Minimal`, and a binary cache holds both.
Any other combination is an `.override`, which is a local build of CPython.

```console
$ nix path-info --store https://cache.nixos.org -S <the interpreter>
```

| interpreter | closure | paths |
| --- | --- | --- |
| `python3` | 219013232 | 23 |
| `python3Minimal` | 77948792 | 8 |

`python3Minimal` is 64% smaller, and `cpython/default.nix` says why: it takes
`withMinimalDeps = true`, and `stripConfig`, `stripIdlelib`, `stripTests` and
`stripTkinter` each default to that same value. So it drops the static archive
**and** the optional dependencies together.

**`python3Minimal` cannot open a wheel, so this lab cannot use it.** Every
wheel is a zip with deflate members, and the module that reads deflate is
absent. Measured by running each import against the store path:

```console
$ /nix/store/...-python3-minimal-3.14.6/bin/python3 -c 'import zlib'
```

| module | `python3Minimal` |
| --- | --- |
| `zlib`, `ssl`, `ctypes`, `sqlite3`, `readline`, `curses`, `bz2`, `lzma`, `gdbm`, `_zstd` | missing |
| `decimal`, `pyexpat`, `xml.etree.ElementTree`, `hashlib`, `zoneinfo`, `mimetypes` | present |

`zipfile` then raises
`RuntimeError: Compression requires the (missing) zlib module`. `ssl` and
`ctypes` rule the interpreter out for most projects as well.

### The third interpreter, which no cache holds

The two knobs are separate, so a third combination exists: keep every optional
dependency, and strip the dead weight only.

```console
$ nix build --expr '(import <nixpkgs> {}).python3.override {
    stripConfig = true; stripIdlelib = true; stripTests = true; stripTkinter = true;
  }' --impure --no-link --print-out-paths
```

| interpreter | closure | paths | on disk | a wheel opens |
| --- | --- | --- | --- | --- |
| `python3` | 219013232 | 23 | 141459368 | yes |
| `python3Minimal` | 77948792 | 8 | 27659480 | **no** |
| the stripped override | 140479456 | 23 | 62357942 | yes |

The override keeps `zlib`, `ssl`, `ctypes`, `sqlite3`, `readline`, `curses`,
`bz2`, `lzma`, `decimal` and `hashlib`, and its closure is 36% below the
closure of `python3`. **That comparison holds for an empty target only.** The
section below measures what each one adds to a target that already holds
`pkgs.python3`, and the order reverses there.

**The environment was built with it, and it runs.** `default.nix` takes a
`python` argument, so the whole graph builds against whichever interpreter the
caller names:

```console
$ nix build --file uv.nix --arg project ./projects/pure-app \
    --arg python '(import <nixpkgs> {}).python3.override {
      stripConfig = true; stripIdlelib = true; stripTests = true; stripTkinter = true;
    }' --argstr venvName pure-app-slim
```

| the environment of `pure-app` | closure |
| --- | --- |
| with `python3` | 238220632 |
| with the stripped override | 159686920 |

That is 33% less, for an empty target. A derivation that runs the second
environment reports
`sys.prefix` inside it, `rich` from its own `site-packages`, and an import of
`zlib`, `ssl`, `ctypes` and `sqlite3`. A deflate zip opens.

**It costs a build of CPython, and a cache does not answer for it.**
`nix path-info --store https://cache.nixos.org` gives `null` for the store
path, because an override is a derivation that nixpkgs never built. The build
above took 357 s on this machine, and that number holds the substitution of
each build input as well. Every package that `fromNixpkgs` supplies would also
need a build against the same interpreter.

### The total closure is the wrong number, and the marginal closure is right

**A total closure is what an empty target pays, and almost no target is
empty.** The store is shared. A machine that runs NixOS holds `pkgs.python3`
already, and so does every other package of nixpkgs that names an interpreter.
What an environment costs such a machine is what it **adds**: the paths of its
closure that the target does not hold yet.

The method subtracts one closure from the other, over the path sets and not
over the totals:

```console
$ nix --store "$store" path-info -r --json <the environment> > env.json
$ nix path-info -r --json <the interpreter of the target> > base.json
```

Read the `path` key of each entry into a set, subtract the second set from the
first, and add the `narSize` of what remains.

| the two interpreters | paths | NAR bytes |
| --- | --- | --- |
| the closure of `python3` | 23 | 219013232 |
| the closure of the stripped override | 23 | 140479456 |
| shared by both | 22 | 77553864 |
| only in `python3` | 1 | 141459368 |
| only in the stripped override | 1 | 62925592 |

**The two interpreters differ in one path, and share the other 22.** `glibc`,
`ncurses`, `sqlite`, `openssl` and every other dependency is the same store
path in both, because the override changes the build of CPython only.

| a target that already holds `pkgs.python3` | it adds | new paths |
| --- | --- | --- |
| the environment, with `pkgs.python3` | 19207400 | 5 |
| the environment, with the stripped override | 82133056 | 6 |

**So the stripped override makes the deployment larger by 62925592 bytes.** An
override is a new store path, so the target holds both interpreters: the
141459368 bytes of `pkgs.python3` stay, because everything else on that machine
still names them, and the 62925592 bytes of the override arrive beside them.
The 33% saving above is real for an empty target and for nothing else.

So the three options are a choice, and the target decides it:

| | an empty target | a target with `pkgs.python3` | the price |
| --- | --- | --- | --- |
| `python3` | 238220632 | 19207400 | none; this is the default of `default.nix` |
| `python3Minimal` | not usable | not usable | the interpreter cannot open a wheel |
| the stripped override | 159686920 | 82133056 | one CPython build, and no binary cache |

**Two deployments make the total closure the right number.** A container image
that carries this closure and nothing else pays the whole 238220632 bytes. A
`nix copy` to a machine that runs no other Nix package pays it as well. Every
other deployment shares the interpreter, and the override then costs bytes
rather than saving them.

**The lab takes neither decision for the user.** `python` is an argument, and
its default is `pkgs.python3`, which is the correct default for a shared store.
A caller that builds a single-purpose image passes one expression and pays one
build of CPython.

### The same environment from nixpkgs, measured beside it

`python3.withPackages` is the ordinary way to get this environment, so it is
the number that says what this lab costs. Measured on 2026-08-12, over
`projects/pure-app`, which resolves to `rich` and its three dependencies. Both
sides read the same `<nixpkgs>` and the same `pkgs.python3`:

| the closure | paths | NAR bytes |
| --- | --- | --- |
| `pkgs.python3` alone | 23 | 219013232 |
| `python3.withPackages (ps: [ ps.rich ])` | 28 | 238473264 |
| `uv.nix` of `projects/pure-app` | 28 | 238220632 |

**The interpreter is 91.9% of each total**, and each environment adds five
paths: the four packages, and the environment itself. The 23 paths of the
interpreter are a subset of both closures, and no path of it is missing from
either, so the subtraction is exact:

| what each one adds to a target that holds `pkgs.python3` | bytes |
| --- | --- |
| `python3.withPackages` | 19460032 |
| `uv.nix` | 19207400 |

**The difference is 252632 bytes, and 236488 of it is the environment path:**

| the path | nixpkgs | `uv.nix` |
| --- | --- | --- |
| pygments | 13450712 | 13441424 |
| rich | 4778984 | 4776248 |
| markdown-it-py | 902600 | 899248 |
| mdurl | 69592 | 68824 |
| the environment | 258144 | 21656 |

`python3.withPackages` writes a compiled wrapper for each program, and `bin`
holds twelve of them at about 16000 bytes each. A virtual environment writes a
symbolic link for the interpreter, and a Python shim of about 265 bytes for
each console script. The four packages differ by less than 0.1% each.

**So the runtime closure is no reason to choose either one.** The two agree to
0.1%, because both install the same code beside the same interpreter. The
reason to choose is the resolution: `uv.lock` decides the versions here, and
nixpkgs decides them there.

## A proof that reaches no index

`ddrn/examples/venv-graph/run.sh` is the proof over the real lock file, and
every artefact of that lock comes from PyPI. So that proof needs a network, and
it also needs the versions that PyPI serves today. A gate of this repository
can have neither.

`ddrn/examples/venv-graph/tests` is the same graph over artefacts that this
repository builds. The run reads:

```text
plan:   installer installer-1.0.1-py3-none-any.whl (unpacked to bootstrap)
plan: demo-lib: demo_lib-2.1.0-py3-none-any.whl (wheel)
plan: demo-tool: demo_tool-3.0.0.tar.gz (sdist)
plan:   backend demo_backend-1.0.0-py3-none-any.whl for demo-backend>=1.0
plan: demo-app: editable through demoback, reading $DDRN_EDITABLE_ROOT
==> 6 nodes, 3 of them in the environment, from 4 artefacts
```

and then, from the environment that the graph built:

```text
console script
  hello from demo-lib
from a source distribution
  demo-tool was built by demoback
editable
  hello from the tree that the sealed check named
```

The second line is the one that matters. `demo-tool` is a source distribution,
its `pyproject.toml` names `demoback`, and no row of the lock file records
that name. The line exists only because a planner opened the archive.

### The seam between a reader and the graph

`default.nix` takes an `artifacts` argument, and each entry holds `package`,
`version`, `filename`, `kind` and `drv`. **`drv` is a derivation, and not a
path.** `null` means "read `lock.json`, and make a `fetchurl` for each row",
which is what `run.sh` gets.

A reader of a different lock format fills that list, and so does a test that
builds its artefacts. The refactor changed no decision: the environment of
`run.sh` is the same store path as before,
`rfy15b0q3y3nmrld2wbw3j9arvjrf4wl-demo-venv`.

### What a fixture artefact costs

**A wheel and a source distribution each record a time, in three places.** A
derivation that writes one is not reproducible until all three are fixed, and
`ddrn/examples/venv-graph/tests/fixtures` fixes them:

| format | field | value |
| --- | --- | --- |
| zip | the date of each entry | 1980-01-01, the floor of the format |
| tar | `mtime`, `uid`, `gid`, `uname`, `gname` of each member | a fixed epoch, and `root` |
| gzip | the `mtime` of the header | `0`, through `gzip.compress(..., mtime=0)` |

A `.pyc` records a fourth one, and `SOURCE_DATE_EPOCH` is the fix for it.
`ddrn/NIX-FACTS.md` gives that measurement.

**nixpkgs ships the installed tree of `installer`, and not the wheel of it.**
The graph bootstraps by unzipping a wheel, because nothing can install the
installer, so the fixture repacks `python3Packages.installer` into a wheel
again.

**A member name is not a path, and two archives spell the same member two
ways.** `tar` accepts `./demo-1.0/pyproject.toml`, and `zip` writes
`demo-1.0/pyproject.toml`. A count of the separators answers one and not the
other, so `ddrn_venv.is_pyproject` drops the leading `.` and each empty part,
and then asks for exactly two parts.

## A real uv project, built end to end

`ddrn/examples/venv-graph/uv.nix` reads a `uv.lock` and hands it to the same
graph that `lock.json` uses. `projects/pure-app` is the first project, and
`uv` itself wrote every file of it. `projects/README.md` gives each command.

Two projects hold the comparison below. `pure-app` has a virtual root and every
artefact of it is a pure wheel. `packaged-app` installs itself through
`hatchling`, and one of its dependencies builds from a source distribution.

The graph of `pure-app` holds one node for each package and one for the
environment:

```console
==> the planner submitted /nix/store/yic8v7qq...-pure-app.drv
==> the root of the graph reads:
"7wz8dcib...-pygments-2.20.0.drv"
"dxvcwc56...-markdown-it-py-4.2.0.drv"
"vf58lwq9...-mdurl-0.1.2.drv"
"w9kllfhc...-rich-15.0.0.drv"
"yic8v7qq...-pure-app.drv"
```

**The Nix half of this reads the lock file and resolves nothing.** `uv.nix`
walks `package[].wheels[]` and `package[].sdist` and makes one `fetchurl` for
each, with no filter and no preference. The planner resolves inside the
sandbox, with `packaging`.

### Three decisions that `uv.lock` leaves open

uv already chose every version and every hash, and this lab trusts both
absolutely: the planner asks `choose` for `rich==15.0.0`, an exact pin, and
never for the range that the project wrote. **A lock file is a union over every
environment that the project supports**, so three decisions remain:

| decision | who answers it | why the lock cannot |
| --- | --- | --- |
| which packages apply | `ddrn_venv._uvlock` | an edge carries a PEP 508 marker, and a fork carries `resolution-markers` |
| which extras and groups | the caller, through `uv.nix` | `uv sync` takes it on its command line |
| which artefact of a package | `ddrn_venv.choose` | `multidict` ships 145 wheels for one version |

`uv export --format requirements.txt` moves the first decision into uv itself.
It still writes markers, so PEP 508 evaluation stays, and it costs a second
generated file to commit. This lab did not adopt it.

### The wheel installer comes out of the same lock file

The graph installs every wheel with `pypa/installer`, and that is a tool of
this build system and not a dependency of the project. PEP 735 dependency
groups are what uv offers for exactly that:

```console
$ uv add --no-sync --group ddrn installer
```

The selection of `uv.nix` enables no group, so the lock file pins the tool and
the environment does not carry it. Both directions are proved, by the manifest
of the environment that each selection builds:

| selection | the environment holds |
| --- | --- |
| `{ pure-app = [ ]; }` | `markdown-it-py`, `mdurl`, `pygments`, `rich` |
| `{ pure-app = [ "ddrn" ]; }` | the same four, and `installer==1.0.1` |

### A distribution does not spell its name the way the index does

`uv.lock` calls the package `pygments`. The `Name` of the metadata that the
installer wrote is `Pygments`:

```console
distributions
  Pygments==2.20.0
  markdown-it-py==4.2.0
  mdurl==0.1.2
  rich==15.0.0
```

PEP 503 normalisation is what makes the two agree, and `ddrn_venv` applies it
at every join. A check that compares the two spellings directly fails, and this
one did until the run above.

### `uv` writes a hash that `fetchurl` reads

`uv.lock` spells every hash `sha256:<hex>`, which is a format that the `hash`
argument of `fetchurl` accepts. So `uv.nix` passes it through with no
translation, and a mismatch is a mismatch of the lock file itself.

### The build system is the one thing that `uv.lock` does not hold

`projects/packaged-app` is the measurement. `uv init --package --build-backend
hatch` writes `requires = ["hatchling"]` into `pyproject.toml`, and `uv lock`
copies **nothing** of that table into `uv.lock`. The lock file holds
`hatchling` only after `uv add --group ddrn hatchling` puts it there as an
ordinary package.

The same hole covers every source distribution that the lock file names.
`mdurl` builds through `flit_core`, and no entry of `uv.lock` says so: the
declaration is inside the archive, which is why a source distribution becomes a
planner node here at all.

**So a dependency group is where a backend goes.** PEP 735 groups are what uv
offers for a tool that the project needs and does not ship, the selection does
not enable the group, and one lock file then pins the whole build. The cost is
one entry for each backend that any build reaches, and nothing checks that the
list is complete until a build fails:

| package | what needs it |
| --- | --- |
| `hatchling` | the `[build-system]` of the project |
| `editables` | `hatchling`, for a PEP 660 build, undeclared |
| `flit-core` | the `[build-system]` inside the `mdurl` source distribution |
| `installer` | every node of the graph, to install a wheel |

`uv2nix` pays the same cost differently. `pyproject.nix` ships a set of build
systems of its own, so the version of a backend comes from nixpkgs and not from
the lock file of the project.

### A backend needs its own closure, and `requires` names the top of it

**PEP 517 says the frontend installs `requires` and what those need.** The
`lock.json` example never met this rule, because `flit_core` depends on
nothing. `hatchling` depends on four packages, and a build that installs the
declaration alone fails at import.

`uv.lock` holds the edges of every package that it lists, so
`ddrn_venv.build_requirements` walks them from each name of `requires`. For
`packaged-app` that turns two declared names into six pinned ones:

```text
plan:   backend hatchling-1.31.0-py3-none-any.whl for hatchling==1.31.0
plan:   backend packaging-26.3-py3-none-any.whl for packaging==26.3
plan:   backend pathspec-1.1.1-py3-none-any.whl for pathspec==1.1.1
plan:   backend pluggy-1.6.0-py3-none-any.whl for pluggy==1.6.0
plan:   backend trove_classifiers-2026.6.1.19-py3-none-any.whl for trove-classifiers==2026.6.1.19
```

### hatchling needs `editables` and declares four other packages

Measured, and not assumed. The metadata of hatchling 1.31.0 reads:

```text
Requires-Dist: packaging>=24.2
Requires-Dist: pathspec>=0.10.1
Requires-Dist: pluggy>=1.0.0
Requires-Dist: tomli>=1.2.2; python_version < '3.11'
Requires-Dist: trove-classifiers
```

`hatchling/builders/wheel.py` reads `from editables import EditableProject` in
`build_editable_detection`, which is the default path of `build_editable`. So a
PEP 660 build of any hatchling project fails with `ModuleNotFoundError: No
module named 'editables'` until something adds it. This lab adds it to
`build-system.requires` of the project.

**hatchling does declare it, through the hook that this lab does not call.**
`hatchling/build.py` defines `get_requires_for_build_editable`, and that
function returns `[*builder.config.dependencies, EDITABLES_REQUIREMENT]`. PEP
517 and PEP 660 give a backend three such hooks, one for each of `build_sdist`,
`build_wheel` and `build_editable`, and a frontend calls the hook, resolves
what comes back, and adds it to the build environment. The next section says
why this lab cannot, and what it pays instead.

### `get_requires_for_build_*` is the hook that this lab does not call

A frontend of PEP 517 builds the environment of a backend in two steps: it
installs `build-system.requires`, and it then asks the backend what more it
needs. `ddrn` performs the first step only.

The reason is where each step would run. The plan is decided in the sandbox of
the planner, and the backend is not installed there. A hook could run in the
node that builds, which is where the backend does exist, but that node has no
index, no store connection and no way to add a member to a graph that is
already registered. So a requirement that arrives from the hook arrives after
the only moment that could act on it.

What the lab pays instead is one line for each such package in the `ddrn`
dependency group of the project, and `editables` is the one case that a project
here has met. What it costs is that nothing checks the list: a backend that
asks for a package through the hook, and a group that does not name it, fails
in the build with `ModuleNotFoundError`.

**A dependency group cannot pay it for a git source, and `hatchling` is that
case.** The group works because the `[build-system]` of the consumer project
names the backend, and the edges of `uv.lock` then expand it. A git source
brings its own `[build-system]`, out of the checkout, and `hatchling` writes
this one:

```toml
[build-system]
requires = []
build-backend = 'hatchling.ouroboros'
backend-path = ['src']
```

`requires` is empty and it is not honest. `hatchling/ouroboros.py` reads the
`dependencies` list out of its own `pyproject.toml` with a regular expression,
and returns it from `get_requires_for_build_wheel`. That list is `packaging`,
`pathspec`, `pluggy` and `trove-classifiers`, and `hatchling` imports all four.
So the plan holds no backend node, nothing puts those four on the path, and the
import of the backend fails.

`flit_core` has the same in-tree shape with an honest empty `requires`, because
it declares `dependencies = []`. `projects/git-app` therefore takes `flit_core`
from `pypa/flit`, and `projects/README.md` records why it is not `hatchling`.

**`editables` wrote a bare path, and not an import finder.** For a project of
one package under `src/`, `EditableProject` takes `add_to_path`, so the install
holds one `.pth` and no generated module:

```text
_editable_impl_packaged_app.pth
  import sys; import os.path; sys.path.append(os.path.expandvars('$DDRN_EDITABLE_ROOT/src'))
```

`scripts/build-local.py` rewrites that line, and it raises for the other
form. The other form needs the syntax tree rewrite that `pyproject.nix` does
with `libcst`, and no project of this lab has reached it yet.

### `backend-path`, which is the backend that no menu holds

PEP 517 lets a project carry its own build backend, and `backend-path` names
where it lives. `requires` is then often empty, because there is nothing to
resolve at all. `hatchling` is that shape, and it is the project that both
`*-subdirectory.lock` fixtures name:

```toml
[build-system]
requires = []
build-backend = 'hatchling.ouroboros'
backend-path = ['src']
```

**A reader that only resolves `requires` builds nothing here.** The list is
empty, so the plan holds no backend node, and the import of
`hatchling.ouroboros` then fails. So `scripts/build-local.py` and
`scripts/build-sdist.sh` both read `backend-path` out of the project and put
those directories first on the path, which is the order that PEP 517 states: a
backend that the project carries wins over a package of the same name.

The paths are relative to the directory that holds `pyproject.toml`, and PEP
517 says they must stay inside it. Both scripts refuse one that leaves.

### `tool.uv.no-binary-package` stays in `pyproject.toml`

uv writes this setting into no lock file, and the lock file still carries the
wheel of the package that the setting names. It is a decision of the consumer,
so `uv.nix` reads it from `pyproject.toml`, where uv keeps it, and passes it to
`ddrn_venv.choose`. That planner then takes the source distribution although a
wheel fits:

```text
plan: mdurl-0.1.2-py3-none-any.whl: no-binary-package names mdurl
plan: mdurl==0.1.2: mdurl-0.1.2.tar.gz (sdist)
```

### A node name is a store path name, so two nodes may not share one

`uv init --package` makes a project that is a member of its own environment
**and** the name of that environment. Both became a node called `packaged-app`,
and `Plan.add` de-duplicated by name, so the environment node replaced the
editable node and `builtins.outputOf` gave a directory with no `bin/python`.

`Plan.add` now refuses two different nodes under one name, and the editable node
carries an `-editable` suffix. A silent replacement is the failure mode to
avoid: the error it produced named neither node.

The wheel installer is the second case, and the `groups` argument of `uv.nix`
found it. `projects/pure-app` pins `installer` in the group `ddrn`, so
`groups = [ "ddrn" ]` puts that wheel in the environment. The graph already held
a node of the same name, which unpacks the wheel to bootstrap every install, and
the refusal read:

```
ValueError: two different nodes are both called 'installer-1.0.1': a node of
kind 'unpack' and a node of kind 'wheel'.
```

The bootstrap node now carries a `-bootstrap` suffix, and the environment then
holds five `.dist-info` directories rather than four.

**A rename of a node changes no output of this graph.** Every node is
content-addressing, so the name of a derivation reaches the output path of
nothing. The default `pure-app` built
`/nix/store/2ppg8xs82055lxw8vg22a1zbbvjnmfjc-pure-app` before this rename and
after it, although the root `.drv` moved from
`j99cp3qrc4spr30w077yp6qw8ljr01ja` to `s4k36ain6j2h1iz11q1615b4aykf2ibm`. An
input-addressed graph would have rebuilt every node and given a new environment.

### `uv.lock` records no extra and no group, so `uv.nix` takes both

`uv sync` decides on its command line which extras and which groups to install,
and it writes none of that decision into the lock file. `uv.nix` takes the same
decision as two arguments, and each one takes `null`, the literal `"all"`, or a
list of names. The rules come from uv, and each one has a source:

| what | uv says | `uv.nix` |
| --- | --- | --- |
| no extra | `sync.rs` reads `DefaultExtras::default()`, and it names the binding `_project` to say that it reads no project file | `extras = null` gives no extra |
| every extra | `uv sync --all-extras` | `extras = "all"` reads `[project.optional-dependencies]` |
| a default group | `default_dependency_groups` of `crates/uv/src/commands/project/mod.rs` reads `tool.uv.default-groups`, and gives `["dev"]` when the table says nothing | `groups = null` does the same |
| every group | `uv sync --all-groups`, and `tool.uv.default-groups = "all"` | `groups = "all"` |
| a named group | `uv sync --group ddrn` | `groups = [ "ddrn" ]` |

**There is no `tool.uv.default-extras` for a project.** `DefaultExtras` exists,
in `crates/uv-normalize/src/extra_name.rs`, and a PEP 723 script is what reads
it. So a project that wants an extra by default has no place to write that, and
`extras` of `uv.nix` is the only place.

**uv validates the default group list, and not the fallback.**
`default_dependency_groups` raises `MissingDefaultGroup` when
`tool.uv.default-groups` names a group that `[dependency-groups]` does not
declare. The `["dev"]` that it falls back to reaches no such check, so a project
with no dependency group at all still locks. `ddrn_venv._workspace.groups_of`
copies both halves of that behaviour.

### A workspace names its members twice, in two spellings

`uv.lock` and `pyproject.toml` each hold a list called `members`, and the two do
not agree entry for entry. The lock file names the packages, and the project
file names the directories:

```toml
# uv.lock
[manifest]
members = ["workspace-root", "ws-cli", "ws-core"]

# pyproject.toml
[tool.uv.workspace]
members = ["packages/ws-core", "packages/ws-cli"]
```

A selection names packages, so `uv.nix` reads the first list. **uv writes
`[manifest]` for a workspace only**, so the lock file of a single project holds
none of it, and `pure-app` and `packaged-app` have no such table. `uv.nix`
falls back to the name of the project, which keeps the default of a single
project the same.

`ddrn_venv._uvlock.Lock.members` parsed that table from the start and nothing
read it. `tests/test_workspace.py` reads it now.

### One lock file resolves to more than one environment

**`uv.lock` records no selection**, which is the first of the three open
decisions above, and a workspace is where that matters. Two selections of the
one lock file of `projects/workspace`:

| selection | what the environment holds |
| --- | --- |
| every member | `ws-cli`, `ws-core`, `rich`, `markdown-it-py`, `mdurl`, `pygments` |
| `{ ws-core = [ ]; }` | `ws-core` |

`ws-core` depends on nothing, so the second environment reaches no artefact of
the index. The two graphs share the `ws-core-editable` node, because that node
is the same derivation in both.

The default of `uv.nix` is every member, which is what `uv sync --all-packages`
takes. A default of "the root project" builds an empty environment for a
workspace, because the virtual root of a workspace usually depends on nothing.

### A PEP 660 redirect names the directory of the member

`scripts/build-local.py` rewrites the build directory of a backend to the
value of `editableRoot`, and the build directory is the tree of the package.
For a project whose source is `.`, that tree **is** the project, so one value
serves. For a member of a workspace it is a directory under the project, and
the redirect has to carry that directory too.

`uv.nix` gave every local package the same `editableRoot`, so the `.pth` of
`ws-core` read `$DDRN_EDITABLE_ROOT/src` and the environment raised
`ModuleNotFoundError: No module named 'ws_core'`. It now joins the relative
directory of `source = { editable = "packages/ws-core" }` to the redirect.

Two measurements come with the correction. The environment of `packaged-app`
is still `a851fa0dqrjd1bshd2x6v02ck3mmr1n1-packaged-app`, so the `.` case did
not move. A second copy of the whole workspace, with one edit to `ws-core`,
changes both reports of the **same** environment, because `ws-cli` reads
`ws-core` through the same redirect.

### Ask `packaging` for the tags, and do not write the rule again

`target_tags` of `_resolve.py` was written by hand, because
`packaging.tags.sys_tags` describes the machine that calls it and a planner
plans for a machine that it is not running on. That reason is right, and the
conclusion was wrong: `packaging.tags.cpython_tags` and `compatible_tags` take
`python_version` and `platforms` as arguments. They are the same computation
against a stated target, and they are the same escape as `Marker.evaluate` with
an explicit environment.

The hand-written set held `cp{major}{minor}-abi3` alone, and PEP 384 says a
stable-ABI wheel runs on every CPython **at or above** the version its tag
names. `pyzmq` 27.1.0 ships six wheels for Linux on x86-64 and none of them
names this interpreter:

| wheel | why it did not fit |
| --- | --- |
| `cp312-abi3-manylinux_2_26…` | the tag says 3.12, and the target is 3.14 |
| `cp313-cp313t-manylinux_2_26…` | `cp313t` is the free-threaded build |
| `cp314-cp314t-manylinux_2_26…` | `cp314t` is the free-threaded build |

So the planner matched nothing, fell back to the source distribution, and would
have needed a C compiler and `libzmq` to build it. The first wheel is the right
answer, and `cpython_tags` yields it.

**The order comes with it.** `cpython_tags` yields the tags in the order that
pip prefers, so `wheel_rank` is now the position of the best match rather than
a grade that this repository invents. A wheel for exactly this interpreter
beats a stable-ABI wheel, which beats a pure one, and a newer `manylinux_2_Y`
beats an older one.

**Only the platform list stays hand-written.** `packaging.tags.platform_tags`
reads the glibc of the running machine, and a target has its own. PEP 600 says
a `manylinux_2_Y` wheel runs against every glibc at or above 2.Y, so
`target_platforms` lists each one from the newest down.

#### The build tag separates two wheels that the tags do not

**The tag order leaves ties, and the order of the lock file must not break
them.** Two wheels of one distribution may carry the same tags, and PEP 427
gives the build tag for exactly this: it "acts as a tie-breaker if two wheel
file names are the same in all other respects", and it sorts "as an empty tuple
if unspecified, else as a two-item tuple". `packaging.utils.parse_wheel_filename`
returns that tuple, and `_resolve.py` discarded it.

uv compares the same two values in the same order. `Package::find_best_wheel`
keeps `(TagPriority, Option<&BuildTag>)` for each candidate and takes the
greatest, so the build tag decides only when the tag priority is equal.
`choose` now does the same, and `TestChoose` asserts both orders of the two
wheels, because a rule that the order of the list satisfies is not a rule.

### A wheel of nixpkgs, in place of a wheel of the index

Tier 4 answers the override question, and `projects/binary-app` is the proof.
`uv.nix` takes a `fromNixpkgs` argument, as
`{ pyzmq = pkgs.python3Packages.pyzmq; }`. The planner then makes no wheel node
for that name, and `Plan.nixpkgs_node` names the store path instead.

**That argument is not called `overrides`, and the name is a decision.** uv
gives `override-dependencies` to a table that forces a version, and uv2nix
gives `overrides` to a build fixup of a package that does not build. Neither
one is this, which replaces the artefact of a package with a store path that
nixpkgs already built. One word carries one meaning in this repository.

Both environments run the same code, and they differ in one line:

| | the wheel of the index | `pkgs.python3Packages.pyzmq` |
| --- | --- | --- |
| extension | `_zmq.abi3.so` | `_zmq.cpython-314-x86_64-linux-gnu.so` |
| links against | `site-packages/pyzmq.libs` | `zeromq`, `libsodium`, `glibc` of the store |
| `auto-patchelf` | runs | does not run |

**The substitution is the measurement that matters.** The build log of the
second one reads:

```
copying path '/nix/store/…-zeromq-4.3.5' from 'https://cache.nixos.org'...
copying path '/nix/store/…-python3.14-pyzmq-27.1.0' from 'https://cache.nixos.org'...
```

The derivation kept the identity that nixpkgs gave it, so the override cost a
download and not a compile. Tier 2 could never offer this, and the section
"Tier 4, the pre-instantiated menu" above gives the reason.

**Nothing reconciles the two versions.** nixpkgs holds what it holds, and the
lock file pins what uv resolved. The planner prints both on one line, and that
line is the whole of the warning:

```
plan: pyzmq==27.1.0: nixpkgs supplies pyzmq==27.1.0, and the lock file asked for ==27.1.0
```

### A local project installs in two forms, and the lock file records neither

`uv.lock` says that a member installs from its own directory. It does not say
which form that install takes, and uv itself writes two:

| command | hook | what the environment holds |
| --- | --- | --- |
| `uv sync` | PEP 660 `build_editable` | a redirect at the tree of the user |
| `uv build` | PEP 517 `build_sdist` | nothing yet; the archive that an index serves |

The second one is what reaches a user who does not have the tree. That user
runs `build_wheel` over the archive, and the wheel that comes out holds the
code. So `uv.nix` takes the form as an argument, `fromSdist`, and the graph
makes each hook a node:

```
demo-app-sdist       build_sdist over the tree      -> one .tar.gz
demo-app-from-sdist  build_wheel over that archive  -> a wheel, installed
```

**The two nodes are the same two nodes that an artefact of the index uses.**
The second one is of kind `sdist-build`, which is the kind that `mdurl` takes,
and the same `scripts/build-sdist.sh` builds both. An archive that a node of
the graph wrote and an archive that PyPI served are one thing to the build that
reads it.

The proof of the difference is the path that the module comes from:

```text
editable   loaded from $DDRN_EDITABLE_ROOT/src/packaged_app/__init__.py
sdist      loaded from <the environment>/lib/python3.14/site-packages/packaged_app/__init__.py
```

### The two sources that name a place, and what each one costs

Four of the seven source kinds name a registry or a directory of the workspace,
and one names a file of this machine. The other two name a place on the
network, and `uv.lock` writes them like this:

```toml
# tests/fixtures/locks/git-subdirectory.lock
source = { git = "https://github.com/pypa/hatch.git?subdirectory=backend&rev=hatchling-v1.27.0#cbf6598e5cbce3ba9097023c5bf783001ebbcbcb" }
# no wheels, and no sdist

# tests/fixtures/locks/url-subdirectory.lock
source = { url = "https://github.com/pypa/hatch/archive/hatchling-v1.27.0.tar.gz", subdirectory = "backend" }
sdist = { hash = "sha256:c5d576a04bbc3940c71dc899da35a9cfe644044a6e4e70549f41d964d6331625" }
```

Both files name one project, `hatchling`, and that project lives in `backend`
of `pypa/hatch`. So the two kinds are the same project through two doors, and
each door needs a different route:

| kind | what arrives | route | nodes |
| --- | --- | --- | --- |
| `url` | an archive, with the hash on `sdist` and the address on `source` | join the two, and it is an ordinary source distribution | a planner, then `sdist-build` |
| `git` | a checkout | a tree, like a `directory` source | one `local-wheel` |

**A URL source splits its artefact across two tables, and a reader has to join
them.** The `sdist` table carries the hash alone. `uv.nix` reads the address
from `source.url`, and what reaches the planner is a row of the menu that no
node treats specially.

**A git source carries no artefact at all.** The package has no `wheels` table
and no `sdist` table, so nothing of it reaches the menu. The checkout is the
whole input, and one PEP 517 `build_wheel` call over it gives the install.

**The commit is in the fragment, and the query holds what the user asked
for.** Measured against `locked_git_url` and `GitSource::from_url`, both in
`crates/uv-resolver/src/lock/mod.rs` of `astral-sh/uv`. That pair writes the
string and reads it back, so it is the whole grammar:

| key | what it means | this reader |
| --- | --- | --- |
| `subdirectory` | where the project lives in the repository | takes it |
| `path` | a **file** of the repository, a wheel or an archive | refuses |
| `lfs` | the checkout needs Git LFS | refuses |
| `tag`, `branch`, `rev` | what the user asked for | drops it |
| `#<fragment>` | the commit that uv resolved | fetches it |

A tag moves and a commit does not, so `uv-git.nix` takes the commit and drops
the name that the user gave it. uv also removes the credentials from the URL
before it writes the string, so no reference of a lock file carries one.

**The two refusals are refusals because each one changes what the package
is.** `path` comes from `from_git_path_source_dist`, and the package is then
an archive or a wheel that lives in the checkout, so a reader that ignored the
key would build the root of the repository and install another project.
`builtins.fetchGit` fetches no Git LFS object, so a checkout of an `lfs=true`
source holds a pointer file of a hundred bytes where each large file belongs,
and the build reads that pointer as if it were the file. Both fail with a
message rather than build the wrong thing.

**uv writes an artefact with no address in three shapes, and this reader takes
all three.** `WheelWireSource` gives `Url`, `Path` — "used for wheels that come
from local registries (like `--find-links`)" — and `Filename`, for a wheel that
a `path` source names. `SourceDist` gives the same three shapes, and its
`Metadata` variant is the hash-only row that a URL source and a `path` source
both write.

**A `path` source keeps the location on the source, and the row of the artefact
carries the file name or nothing at all.** So the reader takes the name from
the row when the row gives one, and from the path of the source when it does
not:

```toml
[[package]]
name = "vendored"
source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
wheels = [ { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:7353…" } ]

[[package]]
name = "archived"
source = { path = "vendor/archived-1.0.0.tar.gz" }
sdist = { hash = "sha256:9207…" }
```

`uv.nix` joins the path to the directory of the project and calls
`builtins.path`, so the file reaches the store with no fetch and with no hash
to state twice. Everything below that point is unchanged: the menu holds a row
for each file, the planner picks one out of the menu, and `graph.nix` names the
chosen file with `builtins.storePath` where a fetch would need
`builtins.appendContext`. `ddrn/examples/venv-graph/projects/vendored-app` is
the project that uv wrote for these two shapes.

**A registry may be a directory, and the location of an artefact then has three
parts.** The source names the directory, and the row of the artefact names the
file under it:

```toml
[[package]]
name = "stocked"
version = "1.0.0"
source = { registry = "index" }
wheels = [ { path = "stocked-1.0.0-py3-none-any.whl" } ]
```

uv joins the root of the workspace, the path of the registry and the path of
the row. `to_registry_wheel` writes `root.join(index_path).join(file_path)`,
and `to_source_dist` writes `workspace_root.join(path).join(file_path)`, both
in `crates/uv-resolver/src/lock/mod.rs`. A reader that took the row as a path
of the project names a file that does not exist, so this divergence fails
loudly. `ddrn/examples/venv-graph/projects/local-index` is the project, and
`tests/test_local_index.py` asserts that the file is under the registry and
nowhere else.

Three more facts of the same shape follow, and this reader takes each one:

- **A value with no scheme is a directory.** `RegistrySourceWire` reads the
  value of `registry` as a URL when `split_scheme` finds a scheme that
  `Scheme::parse` knows, and as a path otherwise.
- **Either path may be absolute.** uv makes the row relative only when the user
  gave the index as a relative path (`try_relative_to_if(…,
  !path.was_given_absolute())`).
- **Only a registry of a directory writes a `path` on a row.**
  `from_registry_wheel` writes it under `IndexUrl::Path` alone, and
  `to_registry_wheel` refuses the same row for a registry of the network with
  `MissingUrl`. `uv.nix` raises there rather than guess a directory.

**A row of such a registry carries no hash.** A flat registry serves the file
itself, so uv records no digest for it. That costs this reader nothing, because
`builtins.path` copies the file and needs none. A `pkgs.fetchurl` would have
had nothing to state.

**A git source is the one input of this lab that is not lazy, and `uv.lock` is
the reason.** Every other input is a `pkgs.fetchurl`, which is a fixed-output
derivation: instantiating one downloads nothing, so the menu holds every
artefact of the lock file and the build downloads the ones that the planner
chose. A git source records the commit and **no hash of the tree that the
commit gives**, so there is no fixed-output derivation to instantiate.
`builtins.fetchGit` therefore runs during evaluation, and a lock file that
holds a git source pays that fetch whether or not the resolution installs the
package.

**One line of `uv.lock` would remove the cost.** A NAR hash of the checkout, or
of the archive that the commit gives, would make the fetch a fixed-output
derivation like every other one. uv records the commit because the commit is
what pip and uv need, and neither of them defers a download.

uv now holds the machinery for such a line, and no lock file uses it yet.
`crates/uv-extract/src/dirhash.rs` hashes a directory tree with BLAKE3, and it
gives the property that a Nix consumer needs: "the dirhash of an archive is the
same as the dirhash of its unpacked files". Nothing in `uv-resolver` reads that
module, so a git source of a lock file written today still records the commit
alone.

**uv installs an `editable` source as an editable, and a `directory` source as
a wheel.** The `Source` enum of the lock says so itself: `Directory` is "a path
to a local directory", and `Editable` is "a path to a local directory that
should be installed as editable". So the two source kinds are the decision, and
`uv.nix` follows it rather than install every directory the same way.

**A subdirectory is what both kinds add, and one archive may hold two
projects.** `pypa/hatch` declares a project at its root and another in
`backend`. A reader that took the first `pyproject.toml` it found would build
the wrong project with the wrong backend, and nothing downstream would say so.
So `is_pyproject` moves the answer down by exactly the parts of the
subdirectory, and the sealed test carries a decoy above each project to prove
it: the top of the tree and the top of the archive both declare
`the_backend_of_the_wrong_project`, which no artefact of that menu supplies.

The section above records where uv states each of these, and this lab read
`astral-sh/uv` at `e39e0b21` to write it.

### An archive holds what the backend put in it, and not the tree

Measured with `uv build --sdist`, over the projects of
`ddrn/examples/venv-graph/projects`:

```text
packaged_app-0.1.0.tar.gz     ws_cli-0.1.0.tar.gz
  PKG-INFO                      PKG-INFO
  pyproject.toml                pyproject.toml
  README.md                     README.md
  src/packaged_app/__init__.py  src/ws_cli/__init__.py
  uv.lock                       src/ws_cli/py.typed
  .python-version
```

Three things follow, and each one is a difference from the tree:

1. **The name is not the name of the project.** PEP 625 normalises it, so
   `packaged-app` writes `packaged_app-0.1.0.tar.gz`. A consumer therefore
   finds the archive by its extension, and `scripts/build-sdist.sh` does that.
2. **The archive holds one directory**, named after itself, so a consumer
   strips one level.
3. **A file that the backend leaves out is absent for every user of the
   index.** `ws-cli` depends on `ws-core`, and its archive carries neither the
   other member nor `uv.lock`. `[tool.uv.sources]` is a table of uv and no
   index reads it, so the `Requires-Dist: ws-core` of that wheel resolves
   against PyPI and against nothing else.

### The archive is the same bytes, given the same `SOURCE_DATE_EPOCH`

The graph writes `packaged_app-0.1.0.tar.gz` with
`sha256:7cf7d79c5da32c1753de9717685799633745315284186a21972c0a4d3beaecb6`, and
so does `uv build --sdist` on the same tree. Change one variable, and the
archive changes:

```console
$ env -u SOURCE_DATE_EPOCH uv build --sdist
$ sha256sum dist/packaged_app-0.1.0.tar.gz
e4a131963bdbdf3cee4a22a951191a2ea788ad0a960bfc24f814d0a9860a1c1c
```

`hatchling/builders/utils.py` reads that variable, and it takes `1580601600`
when the variable is absent. `graph.nix` sets `315532800` for every node, for
the reason that the file gives at `SOURCE_DATE_EPOCH`: a node output floats, so
a build that recorded the time of the day would take a new path on every run.
The agreement with `uv build` is a result of that setting, and not of anything
that this lab does to the archive.

### `uv` downloads an interpreter that NixOS cannot start

`uv` fetches its own CPython by default, and that build is a dynamically linked
executable for a generic Linux. Name an interpreter of nixpkgs instead:

```console
$ py=$(nix build --file . pkgs.python313 --no-link --print-out-paths)
$ export UV_PYTHON_DOWNLOADS=never UV_PYTHON="$py/bin/python3.13"
```

### Two ways to seed the private store, and what each one costs

The build runs in a private chroot store, and that store starts empty. Two
forms fill it, and `README.md` and `run-uv.sh` do not use the same one.

- **`nix copy --to "$store" --no-check-sigs`** puts the closure of the patched
  Nix and of the planner environment in, before the first build.
- **`--extra-substituters daemon --option require-sigs false`** names the
  system daemon as a substituter, and copies nothing first.

Measured on 2026-08-12 over `projects/pure-app`, with one empty store for each
form and the store removed between the two runs:

| form | seeded from this machine | fetched from `cache.nixos.org` | built | store | total |
| --- | --- | --- | --- | --- | --- |
| `nix copy` | 142 paths | **78 paths** | 13 | 825 MB | 8 s |
| `--extra-substituters daemon` | 214 paths | **0 paths** | 13 | 822 MB | 9 s |

Both gave `/nix/store/90mx650ayjnhh0ivf92kmjgpbymskkzm-pure-app`.

**The substituter form removes the network, and it does not save time or
space.** `nix copy` seeds the two closures that the command names, so each
other path of the build still comes from the binary cache. The daemon serves
all of them. The wall-clock is the same here, because the cache is fast and the
78 paths are 20.7 MiB.

**The daemon is the only source that works.** `--extra-substituters local` and
`--extra-substituters 'local?root=/'` both fail with `path '...' is required,
but there is no substituter that can build it`. A client cannot read the
database of the local store; the daemon owns it.

**The substituter form prints one warning for each realisation.** The patched
Nix is 2.36-pre and the daemon of this machine is older, so each build prints
`warning: the daemon is missing the 'realisation-with-path-not-hash' protocol
feature, needed to support content-addressing derivations`. One build of
`pure-app` printed it 16 times, and a whole run of `run-uv.sh` printed it 145
times in 1088 lines.

**So `run-uv.sh` keeps `nix copy`.** That script is the demonstration, and its
output is what a reader reads. It also cannot become an offline script whatever
it does, because the graph downloads each artefact from PyPI. The one-command
form in `README.md` keeps the substituter, because a reader of that form wants
the environment and not the transcript.

## A package that compiles, and what its compiler costs

### A node of the graph holds no compiler, and one argument gives it one

`graph.nix` gives every node `coreutils`, `unzip`, `tar`, `gzip` and the
interpreter, and no more. A source distribution that holds C code therefore
fails inside its backend, where the interpreter looks for `cc`:

```
> demoback: cc -shared -fPIC -I/nix/store/…-python3-3.14.6/include/python3.14 …
> FileNotFoundError: [Errno 2] No such file or directory: 'cc'
```

Measured on 2026-08-12, with `pytest ddrn/examples/venv-graph/tests/test_sealed_graph.py`.
`tests/fixtures/demo-native` is the archive that produces it, and
`TestAProjectThatHoldsCCode` asserts the message.

**The list stays short because a compiler in it would reach every node.** A
wheel is already built, so the node that installs one needs no compiler, and
the index serves a wheel for almost every package. `buildTools` of
`default.nix` names one for the package that needs it:

```nix
buildTools = {
  psutil = [ pkgs.stdenv.cc ];
};
```

The planner attaches the list to the node that builds that package, and to no
other node. **The canonicalisation of the name happens in Python**, with
`packaging.utils.canonicalize_name`, so `graph.nix` looks nothing up and
matches no spelling.

### The compiler is a build input, and it reaches no environment

| store object | closure | paths |
| --- | ---: | --- |
| `python3` | 219013232 | 23 |
| `sealed-native-venv` | 219054112 | 25 |

The environment costs 40880 bytes and two paths over its interpreter, and
those two are the install of the package and the environment itself. The
compiled module is 15824 bytes of that.

By name, the closure of the environment holds `glibc`, `gcc-lib`, `libgcc` and
`xgcc-libgcc`, and the closure of `python3` holds the same four. **It holds no
`gcc-wrapper` and no `binutils`.** So the argument buys a build step and adds
nothing that a user of the environment carries.

Measured on 2026-08-12, with `nix path-info --closure-size` over the private
store of the test, and a comparison of the two path lists by name. The two
stores hold two revisions of nixpkgs, so a comparison by store path answers
nothing here and a comparison by name answers the question.

### The argument moved every planner, and no environment

`buildTools` changed `graph.nix` and the planner, so the `.drv` of every
planner moved. The environment did not:

```console
$ nix build ... --expr '(import <before>/uv.nix { ... project = <before>/projects/pure-app; }).env'
$ nix build ... --expr '(import <after>/uv.nix  { ... project = <after>/projects/pure-app; }).env'
/nix/store/2ppg8xs82055lxw8vg22a1zbbvjnmfjc-pure-app
/nix/store/2ppg8xs82055lxw8vg22a1zbbvjnmfjc-pure-app
```

Measured on 2026-08-12, over a copy of the example at the commit before the
argument. **A floating output takes its path from what the build wrote**, so
one path means one content, and this is the same proof that the change to the
menu used. Under input addressing the path would have moved with the `.drv` of
the planner, and a reader could not tell a rewritten plan from a rewritten
environment.

A caller that names no tool gets no `buildTools` key in the document of the
planner, and a package that names none gets no key on its node. The rule is
one shape for one meaning, and `projects/native-app` is the one project of the
eight that carries either.

### `NIX_CFLAGS_COMPILE` reaches nothing here, and `CPPFLAGS` does

The first version of `buildTools` set `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`,
which is what a `mkDerivation` sets. `lxml` then failed at the link step:

```
lxml> ld.bfd: cannot find -lz: No such file or directory
```

`zlib` was in the list, so the variable reached nothing. `add-flags.sh` of the
wrapper says why. It reads
`NIX_CFLAGS_COMPILE_x86_64_unknown_linux_gnu`, and `mangleVarList` copies the
plain name into that one for each role that `accumulateRoles` found:

```bash
accumulateRoles() {
    declare -ga role_suffixes=()
    if [ "${NIX_CC_WRAPPER_TARGET_HOST_x86_64_unknown_linux_gnu:-}" ]; then
        role_suffixes+=('')
    fi
    ...
}
```

**The setup hook of the wrapper exports that variable, and a node of this
graph runs no setup hook.** So `role_suffixes` is empty, nothing is copied, and
the salted variable stays empty.

`CPPFLAGS` and `LDFLAGS` are what `customize_compiler` of
`setuptools._distutils` reads, and it appends each one to the command that
`sysconfig` gives. It reads `CFLAGS` as well and **replaces** that value, so
`graph.nix` does not set `CFLAGS`.

Read the source, and do not infer the rule from the name of a variable. This
is the second time in this lab that a guess about a tool cost a build.

### `-g0` is 58% of the closure of a package that compiles

`projects` holds no package that compiles, so this measurement used a project
of its own: `psutil` and `lxml`, both under `tool.uv.no-binary-package`, with
`buildTools` naming `stdenv.cc` for the first and `stdenv.cc`, `libxml2`,
`libxslt`, `zlib` and `pkg-config` for the second. Both built, and both import.

| the environment | closure | paths |
| --- | ---: | ---: |
| as first built | 545822056 | 41 |
| with `-g0` | 228729808 | 28 |

Measured on 2026-08-12, with
`nix path-info --closure-size` over the private store of the test.

**A `.so` with debug information names every include directory of its build.**
`strings` over `_psutil_linux.abi3.so` of the first row finds these, and the
scan of the output makes each one a reference:

```
/nix/store/…-gcc-15.3.0/lib/gcc/x86_64-unknown-linux-gnu/15.3.0/include
/nix/store/…-glibc-2.42-67-dev/include
/nix/store/…-python3-3.14.6/include/python3.14
```

So the first row carried `gcc`, `glibc-dev`, `linux-headers`, the `dev` output
of each library, and the four libraries that gcc itself needs: `gmp`, `isl`,
`mpfr` and `libmpc`. `stdenv` of nixpkgs strips a binary and removes the same
strings, and a node of this graph runs no `stdenv`.

With `-g0`, the environment adds exactly five paths over its interpreter:

```
libxml2-2.15.3  libxslt-1.1.45  lxml-6.1.1  native-real  psutil-7.2.2
```

`libxml2` and `libxslt` are real: the `.so` of `lxml` links against both.
Nothing of the compiler is there.

### `cffi` asks `pkg-config` and does not need the answer

`projects/native-app` is the gate over two packages of PyPI, and
`check-native.nix` names `pkg-config` for `cffi` because `cffi` asks it first.
**The build succeeds with no `pkg-config` at all.** `_ask_pkg_config` of
`cffi/setup.py` fails, `cffi` falls back to a hand-written list of directories,
and the `-isystem` of `CPPFLAGS` answers before that list does:

```
'-g0', '-isystem', '/nix/store/…-gcc-wrapper-15.3.0/include',
'-DFFI_BUILDING=1', '-I/usr/include/ffi', '-I/usr/include/libffi',
'-I/nix/store/…-python3-3.14.6/include/python3.14'
```

The header is what the build needs. Without `libffi.dev` in the list the same
build stops:

```
cffi> src/c/_cffi_backend.c:15:10: fatal error: ffi.h: No such file or directory
```

Measured on 2026-08-12, by building `check-native.nix` three times: with the
argument as written, with `pkg-config` removed, and with the two outputs of
`libffi` removed.

**So no test of this repository covers `PKG_CONFIG_PATH`.** The variable is
there because a build that reads it is ordinary, and the two gates cover
`PATH`, `CPPFLAGS` and `LDFLAGS` alone.

### The wheel says which interpreter it is for

The fixture backend reads `sysconfig`, and it writes
`demo_native-1.0.0-cp314-cp314-linux_x86_64.whl` with `Root-Is-Purelib: false`
when the wheel holds a `.so`, and `py3-none-any` with `Root-Is-Purelib: true`
when it does not. The module arrives as
`_speedup.cpython-314-x86_64-linux-gnu.so`, which is `EXT_SUFFIX` of the
interpreter that built it.

**A wheel that carries a binary may not say `py3-none-any`.** That tag means
"any interpreter, any platform", and a compiled module runs on neither.

### The project declares its build tools, and the declaration is the argument

`[tool.uv] no-binary-package` is a setting that uv keeps in `pyproject.toml`
and writes into no lock file, and `uv.nix` reads it there. A build tool is the
same kind of setting, and PEP 518 gives `[tool.*]` to exactly this:

```toml
[tool.uv]
no-binary-package = ["cffi", "psutil"]

[tool.ddrn.build-tools]
psutil = ["stdenv.cc"]
cffi = ["stdenv.cc", "pkg-config", "libffi.dev", "libffi"]
```

**Each value names an attribute of nixpkgs, and a dot walks into it.** This is
a lookup and not a rule: `libffi.dev` names the `dev` output of one package,
and nothing globs, compares a version or evaluates a marker. So the boundary
that `ddrn/NIX-FACTS.md` records holds. `lib.hasAttrByPath` refuses a name that
nixpkgs does not answer, and the message names the package that asked:

```
error: uv.nix: [tool.ddrn.build-tools] of native-app gives 'psutil' the build
tool 'stdenv.cxx', and nixpkgs holds no attribute of that name.
```

**The two routes end at one derivation.** `projects/native-app` declares the
table, and the same two entries as a `buildTools` argument give the same
planner:

```console
$ nix eval --impure --raw --expr \
    '(import ./uv.nix { project = ./projects/native-app; nanopynixEnv = "…"; }).drvPath'
/nix/store/b4bin047ms8yybwrpglbcrhnq2p4s7bk-venv-planner.drv
$ nix eval --impure --raw --expr \
    'let pkgs = import <nixpkgs> {}; in (import ./uv.nix { inherit pkgs; … buildTools = { … }; }).drvPath'
/nix/store/b4bin047ms8yybwrpglbcrhnq2p4s7bk-venv-planner.drv
```

Measured on 2026-08-12. The argument replaces the entry of each package that
it names, and an empty list takes the tools of one package away.

**No check reads the key here.** A package name that this target does not
install is not a mistake: one lock file resolves to more than one environment,
so a table that covers a workspace names packages that a single member never
builds.

**What this buys is one file.** uv2nix asks for an overlay in the Nix language
for the same decision, so a user of it describes the project in two places and
in two languages. `nix build --file uv.nix env --arg project ./.` now needs no
second argument for a project that compiles.

### Three decisions belong to the project, and three belong to the build

`[tool.ddrn]` holds one key for each decision that `uv.lock` does not record
and that the project answers the same way every time:

| key | what it decides | how the argument overrides it |
| --- | --- | --- |
| `build-tools` | what one package needs to build | per package |
| `from-nixpkgs` | which package nixpkgs supplies rather than the index | per package |
| `from-sdist` | which member ships an archive rather than a redirect | the whole list |

**`from-sdist` takes a different shape because it is a list.** A table merges
per key, and a list cannot. So `fromSdist` of `uv.nix` defaults to `null`,
which reads the project, and a list replaces the table. `select`, `extras` and
`groups` already take that shape, for the same reason.

**Three others stay an argument, and the reason is that they change for each
build of one project.** `select`, `extras` and `groups` differ for each
environment of one lock file, and `projects/workspace` builds two environments
out of one file. `application` decides the shape of one output, and the same
project ships as a program in one place and as an environment in another.

Measured on 2026-08-12, over `projects/binary-app` and `projects/packaged-app`
with the table written into a copy of each. `tests/test_projects.py`, in
`TestTheProjectDeclaresTheOtherTwoDecisions`, holds the comparison: the table
and the argument give one `.drv`, and a different value gives another one.

## PEP 751, and what `packaging` already does with it

PEP 751 gives one lock file format that more than one tool writes. uv 0.12.1
exports it, and `packaging` 26.2 reads it. Measured on 2026-08-12.

```console
$ uv export --format pylock.toml --frozen --all-packages --all-groups \
    --python <interpreter> -o pylock.toml
```

The output name must be `pylock.toml`, or `pylock.<name>.toml` with a name that
holds no dot. uv refuses any other name. `--python` is necessary here, because
uv downloads an interpreter that NixOS cannot start.

Over `projects/workspace`, the export is 3274 bytes and `uv.lock` is 8874.

### `packaging.pylock` does the work that `_uvlock` does

`packaging/pylock.py` is 33890 bytes, and it holds `Pylock`, `Package`,
`PackageWheel`, `PackageSdist`, `PackageDirectory`, `PackageVcs` and
`PackageArchive`. `Pylock` holds `from_dict`, `validate`, `to_dict` and
`select`:

```python
Pylock.select(*, environment=None, tags=None, extras=None, dependency_groups=None)
    -> Iterator[tuple[Package, PackageVcs | PackageDirectory | PackageArchive | PackageWheel | PackageSdist]]
```

**That one method is the marker evaluation, the extras and the groups, and the
choice of a wheel by its PEP 425 tags.** It gives the chosen artefact beside
each package. Over the export above it gave 13 packages of 13: a wheel for each
package of an index, and a directory for each member of the workspace.

### Each awkward source kind survives, in a standard spelling

| the source | what `uv.lock` writes | what `pylock.toml` writes |
| --- | --- | --- |
| a member of the workspace | `source = { editable = "packages/ws-cli" }` | `directory = { path = "packages/ws-cli", editable = true }` |
| a tarball of the project | `source = { path = "vendor/archived-1.0.0.tar.gz" }` | `archive = { path = "...", hashes = { sha256 = "9207336..." } }` |
| a registry that is a directory | `source = { registry = "index" }`, and a row with a path | `index = "file:///.../index"`, and an `sdist` with a `file://` url |
| a git source | one url, as `git+https://...?subdirectory=x#<rev>` | `vcs = { type = "git", url = ..., commit-id = ..., subdirectory = ... }` |

**The git row is the one that would delete code.** `./uv-git.nix` parses that
url, and PEP 751 gives the same three parts as fields.

The local registry loses no hash. `uv.lock` writes `sdist = { path =
"crated-1.0.0.tar.gz" }` with no hash of its own, and the export writes
`hashes = {}`.

### `builtins.fromTOML` needs one more experimental feature, and nothing else

PEP 751 writes `upload-time` as a TOML datetime, and uv writes one for each
artefact. The default reader refuses it:

```console
$ nix eval --impure --expr 'builtins.fromTOML (builtins.readFile ./pylock.toml)'
error: while parsing TOML: Dates and times are not supported
```

**`parse-toml-timestamps` is the whole answer.**
`src/libexpr/primops/fromTOML.cc:131-149` handles `local_datetime`,
`offset_datetime`, `local_date` and `local_time`, and it raises that message
only when `Xp::ParseTomlTimestamps` is disabled. A timestamp arrives as an
attribute set:

```console
$ nix eval --impure --extra-experimental-features parse-toml-timestamps --expr '...'
{ name = "editables"; packages = 13; uploadTime = { _type = "timestamp"; value = "2026-04-14T10:39:03Z"; }; }
```

Measured on 2026-08-12, on both bands: Nix 2.34.8 of this machine and the
2.36.0pre-ddnix of the lab. The exports of `projects/vendored-app`,
`projects/local-index` and `projects/git-app` each read as well.

So the cost is one more entry beside the four features that this example needs
already. **No patch of Nix, and no derivation that rewrites the file.** A
derivation would be the worse answer twice over: it would rewrite a lock file
before a reader saw it, and reading its output during evaluation is an import
from derivation, which this example does not do today.

`upload-time` is the only datetime that PEP 751 defines. `packaging.pylock`
gives it to `PackageWheel`, `PackageSdist` and `PackageArchive`, and to no
other class.

### What the export does not carry

The export of `projects/workspace` holds no `extras` key, no
`dependency-groups` key, no `default-groups` key, and no `dependencies` on any
package. uv resolves the selection while it exports, and it writes the result.

**So one `pylock.toml` is one selection.** `uv.nix` takes `select`, `extras`
and `groups` as arguments, and `tests/test_projects.py` builds three
environments from the one `uv.lock` of `projects/workspace`. Each of those
would need an exported file of its own.

The format itself does not force this. `Pylock` holds `extras`,
`dependency_groups` and `default_groups`, and `select` takes the first two. The
exporter of uv is what flattens them.

**The export also carries no `[package.metadata]`.** `ddrn_venv._freshness`
compares that table with the `pyproject.toml` of each member, and it is how
this lab refuses a stale lock file. A `pylock.toml` records no declaration to
compare against, so a checked-in export goes stale with no reader to notice.

### A reader of the standard format is half the size, and it parses nothing

`examples/venv-graph/pylock.nix` is that reader, written as a spike.
`examples/venv-graph/tests/test_pylock.py` runs it over an export of each of the
seven projects, and 18 tests pass in 5.9 s. Measured on 2026-08-12, with
`pytest ddrn/examples/venv-graph/tests/test_pylock.py`.

| file | lines | what it reads |
| --- | ---: | --- |
| `uv.nix` | 846 | the whole of `uv.lock`, and the graph that it names |
| `uv.nix`, lines 422 to 719 | 298 | the source of each package, and its artefacts |
| `uv-git.nix` | 94 | one `git+https://...#<rev>` url |
| `pylock.nix` | 188 | the whole of `pylock.toml` |

So the comparable read is 392 lines against 188. **The 204 lines that go are
the ones that decide what a source is.** `uv.nix` reads `package.source` and
asks which of five keys it holds, then joins a registry against a path, then
splits a url into a repository, a subdirectory and a revision. PEP 751 gives
each shape a key of its own, so `pylock.nix` asks which key is present and
reads it.

`uv-git.nix` disappears whole: `vcs.url`, `vcs.commit-id` and
`vcs.subdirectory` are three fields, and no reader parses them apart.

One translation arrives in return. uv writes `sha256:<hex>` as a string, and
PEP 751 writes a table of one algorithm to one digest. That is `hashOf`, and it
is 12 lines.

### The menu is the same menu, and the wheel choice stays at build time

The export of each project offers the artefacts that its `uv.lock` offers, and
the two counts are equal for all seven. `test_the_menu_is_the_menu_that_uv_lock_gives`
asserts that.

| project | artefacts | packages | trees |
| --- | ---: | ---: | --- |
| pure-app | 10 | 5 | — |
| packaged-app | 24 | 13 | packaged-app |
| workspace | 22 | 13 | ws-cli, ws-core |
| binary-app | 100 | 4 | — |
| git-app | 2 | 2 | flit-core |
| vendored-app | 6 | 4 | — |
| local-index | 6 | 4 | — |

**This is the fact that matters for the target.** An export resolves which
packages the target installs, and it keeps every wheel of each one. So the
planner still chooses an artefact by the PEP 425 tags of the target, and the
machine that exported chose no wheel. `binary-app` keeps all 100 rows of
`numpy`, and its export also carries two per-package `marker =` lines.

The only name that the export drops is the virtual root of the project, which
installs nothing. `test_the_export_drops_the_virtual_root_and_nothing_else`
asserts the equality against the `virtual` sources of `uv.lock`.

### What the spike does not answer

The spike reads a lock file, and it builds nothing. `default.nix` takes the
menu that `pylock.nix` gives, so a planner over it is a small change and not a
proven one. Three things stay open, and each one is a fact of the section
above and not of the reader:

- One `pylock.toml` is one selection, so `select`, `extras` and `groups` need
  an exported file of each.
- The export carries no `[package.metadata]`, so `ddrn_venv._freshness` has
  nothing to compare.
- `packaging.pylock` reads the same file in the planner, and the spike calls
  it nowhere.

## The order that these facts imply

**Keep the two halves apart, the way pyproject.nix and uv2nix keep them
apart.** `graph.nix` is the base and reads no lock file. `plan.py` produces the
plan document. A lock reader is anything that fills `nodes`. Then a `uv.lock`
reader and a `poetry.lock` reader are two readers over one base, and the base
does not change when the second one arrives.

1. **Split `plan.py` into a base and a reader**, with the plan document as the
   interface it already is. Nothing else changes, and the shape is the one that
   the next four steps need.
2. **Write the `uv.lock` reader, and drop the hand written `lock.json`.** The
   lock already holds the dependency edges, so a closure walk replaces the flat
   `roots` list. `lock.json` of this lab holds `package`, `version`,
   `filename`, `kind`, `url`, `sha256` and `marker`, and **no edge at all**,
   which is why the walk has nothing to walk today.
3. **Then forked resolution**, which the same reader gets almost free, and
   which costs uv2nix most of `lock1.nix`.
4. **A planner node for each source distribution: done.**
   `ddrn/examples/venv-graph/scripts/plan-sdist.py` reads `pyproject.toml` out
   of the archive, resolves the backend against the same menu, and submits the
   graph that builds the distribution. `lock.json` no longer carries a
   `backend` or a `backend-requires` for `idna`, and the environment that
   comes out is the same store path as before,
   `rfy15b0q3y3nmrld2wbw3j9arvjrf4wl-demo-venv`.

   The run reads:

   ```text
   plan-sdist: idna-3.18.tar.gz builds through flit_core.buildapi
   plan-sdist:   backend flit_core-4.0.2-py3-none-any.whl for flit_core<5,>=3.11
   ```

   **The planner is instantiated outside the sandbox, and it has to be.** A
   planner cannot read a store path that the planner itself added, so
   `builtins.outputOf` on a derivation written inside fails.
   `ddrn/NIX-FACTS.md` gives the measurement. `default.nix` therefore
   instantiates one planner for each source distribution of the lock file, to
   a fixed depth, and hands each `.drv` in the way the menu hands in a fetch.

   This closes the gap that uv2nix documents as an override and that
   poetry2nix documents as burnout. It is the step that no other tool can
   take.
5. **Then the nixpkgs override, by tier 4.** A pre-processing step outside the
   sandbox instantiates the candidates and passes a menu of
   `{ name, version, drv, out }`. The planner names the chosen `.drv` in the
   node that it writes. This also needs the `write_bin` unwrapping plus an
   interpreter check, because a package that is in the lock takes its edges
   from the lock, and the nixpkgs package takes its edges from nixpkgs.
6. **Then the merge semantics of `make_venv.py`**: recursion into a directory
   for a namespace package, bytecode collision skipping, and the skip and
   ignore patterns.
7. **A `poetry.lock` reader is then a new reader over the same base**, and it
   is new capability rather than a port, because the pyproject.nix ecosystem
   has no such layer.

**Passing the builders of pyproject.nix into the planner is a separate
question, and tier 4 answers the part of it that matters.** Tier 1 works today
and buys the pure Nix libraries. Tier 2 needs `QueryPathInfo` in the allowlist,
and it would still hand the planner derivations whose sources the sandbox
cannot fetch, so this lab does not need it. Tier 3 is what the lab does now.
Tier 4 is the step 5 above, and it needs no patched Nix. None of the steps
waits on tier 2.
