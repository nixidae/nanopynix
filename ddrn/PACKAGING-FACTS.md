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
| `virtual` | 5 | not covered; a member with edges and no install |
| `url` | 1 | not covered; a direct URL |
| `git` | 1 | not covered; needs a fetcher in the menu |
| `directory` | 1 | not covered; a local path that is not editable |

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
4. **Then a planner node for each source distribution**, which closes the gap
   that uv2nix documents as an override and that poetry2nix documents as
   burnout. This is the step that no other tool can take.
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
