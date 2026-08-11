# The architecture of `ddrn/examples/venv-graph`

**A Python environment, where Python makes every packaging decision and Nix
builds every step.** `uv2nix` and `pyproject.nix` do the same work in the Nix
language, and they carry a copy of PEP 508, PEP 440 and PEP 425 to do it. This
lab calls `packaging`, which is the reference implementation of all three, and
it calls it inside a build sandbox.

This document gives the shape and the order. `README.md` gives the argument,
`PACKAGING-FACTS.md` gives each measured fact, and `UPSTREAM.md` gives the
three changes to Nix that the lab needs.

## The flow

<!-- architecture:begin -->
```text
                   +---------------+
                   |    uv.lock    |
                   +---------------+
                     |
                     |
                     v
                   +---------------+
                   | uv.nix: menu  |
                   +---------------+
                     |
                     |
                     v
                   +---------------+
                   |  planner drv  |
                   +---------------+
                     |
                     |
                     v
                   +---------------+
                   |    plan.py    |
                   +---------------+
                     |
                     |
                     v
                   +---------------+
                   |   graph.nix   | <+
                   +---------------+  '
                     |                '
                     |                '
                     v                '
+------------+     +---------------+  '
| local node | <-- | SubmitOutput  | -+----+
+------------+     +---------------+  '    |
  |                  |                '    |
  |                  |                '    |
  v                  v                '    |
+------------+     +---------------+  '    |
| venv node  | <-- | sdist planner |  +    |
+------------+     +---------------+       |
  ^                                        |
  |                                        |
  |                                        |
  |                +---------------+       |
  +--------------- |  wheel node   | <-----+
                   +---------------+
```
<!-- architecture:end -->

`ddrn/architecture.dot` is the source of that drawing, and each node of it
names the file that does the work. Run `scripts/render-architecture.sh --term`
to draw the same graph in colour.

## The three phases

| phase | where it runs | what it produces |
| --- | --- | --- |
| evaluate | the host, in the evaluator of the user | one derivation for each candidate, and the planner |
| plan | inside the sandbox of the planner | one derivation for each node of the graph |
| build | the host, over the submitted graph | the virtual environment |

### Phase 1: evaluate, and build nothing

`uv.nix` reads `uv.lock` and makes one `fetchurl` for each artefact of each
package. An artefact that this machine already holds has no address, so
`builtins.path` copies that file into the store instead. **It makes no
packaging decision.** It
does not read a wheel tag, it does not evaluate a marker, and it does not
choose between a wheel and a source distribution.

`default.nix` hands the planner a **menu**: for each candidate, the store path
of the `.drv` file and the store path of its output, with the dependency
between the two discarded by `unsafeDiscardOutputDependency` and
`unsafeDiscardStringContext`.

**That discard is what keeps the plan lazy.** Nix writes every `.drv` at
instantiation, so a candidate exists in the store before anything builds it. A
candidate that the planner rejects is never fetched. `ddrn/examples/venv`
measures the effect: 3 of 20 wheels downloaded.

One input breaks the rule. A `git` source records the commit and no hash of the
tree, so there is no fixed-output derivation to instantiate and
`builtins.fetchGit` runs during the evaluation. `PACKAGING-FACTS.md` gives the
one line of `uv.lock` that would remove that cost.

### Phase 2: plan, inside the sandbox

The planner is a derivation with `outputHashMode = "text"`, which tells Nix to
read its output back as a derivation. `ddrn/nix/planner.nix` wraps that.
`builder-rpc-v0` gives its builder a restricted daemon socket at `NIX_REMOTE`,
and one new operation, `SubmitOutput`.

`plan.py` runs there, and it takes five steps:

0. **Refuse a stale lock file.** `ddrn_venv._freshness` compares the
   `[package.metadata]` of each member with the `pyproject.toml` that the
   workspace holds now, and `ddrn_venv._workspace` reads the member globs over
   the directories that `workspace.nix` listed. A project that grew a
   dependency, or a workspace that grew a member, would otherwise get the old
   environment and no message. uv makes both comparisons, in
   `Lock::satisfies`.
1. **Read the lock file.** `ddrn_venv._uvlock` resolves `uv.lock` for the
   target: which fork, which extras, which groups. It follows the same three
   steps that `Installable::to_resolution` of uv takes, and
   `PACKAGING-FACTS.md` records each rule with the function of uv that holds
   it.
2. **Choose an artefact for each package.** `ddrn_venv._resolve` asks
   `packaging` for the PEP 425 tags of the target and for the PEP 508 markers,
   and it takes the best wheel, or the source distribution when no wheel fits.
3. **Write the plan document.** `ddrn_venv._plan` builds one node for each
   install step. The document is the interface: a reader of a different lock
   format is a new producer of the same document, and `graph.nix` does not
   change when one arrives.
4. **Evaluate `graph.nix` over that document, and submit the root.** The
   evaluator runs in the sandbox, through `nanopynix`. It writes each
   derivation through the restricted socket, and `store.submit_output` names
   the root as the output of this build.

**`graph.nix` reads no lock file.** It says how a node of each kind builds, and
`plan.py` says which nodes exist. Neither half repeats the other.
`builtins.appendContext` in `graph.nix` puts back the dependency that the menu
discarded, for the candidates that the planner chose.

### Phase 3: build the graph

Each node is a floating content-addressed derivation, so a node that reads
another node interpolates a placeholder and no half does hash arithmetic.
`graph.nix` holds nine kinds; these four carry the work:

| node | what it does |
| --- | --- |
| wheel | installs one wheel with `pypa/installer` |
| sdist planner | plans the build of one source distribution |
| local | runs one PEP 517 hook over a tree of the user |
| venv | composes the members into the environment |

**The `sdist planner` node is a planner again, and that is the recursion in the
drawing.** A source distribution declares its build system in its own archive,
so no menu of the host can hold that answer: the archive has to be read before
the answer exists. The node reads it, resolves the backend against the same
lock file, and submits a graph whose root is the built distribution.

`sdistPlannerDepth` in `default.nix` bounds that recursion. The list of levels
is a memo, so the cost is the depth times the number of packages, and not the
number of packages raised to the depth.

## What is decided where, and why

| decision | where | why not the other half |
| --- | --- | --- |
| whether the lock file is current | `_freshness.py`, in the sandbox | the comparison needs the lock file and the declaration together, and only the planner reads the lock file |
| whether uv agrees that it is current | `uv lock --check`, in a derivation of its own | `Lock::satisfies` holds 21 rules, and the reader implements the two that a build reaches. The `uvCheck` argument gives the rest, and it costs the whole project tree as an input |
| which directories exist | `workspace.nix`, at evaluation | a member that the lock file never named is in no store path, so only the evaluator finds it. **That file lists, and decides nothing** |
| which directories are members | `_workspace.py`, in the sandbox | the rule is a glob of uv, and `fnmatch` reads a glob |
| which extras and groups a member installs | `_workspace.py`, in the sandbox | the default is `tool.uv.default-groups` of each `pyproject.toml`, and the store path of that file arrives beside the lock file |
| which fork, which extras, which groups | `_uvlock.py` | the rules are uv's, and uv's source states them |
| which artefact fits this target | `_resolve.py` | `packaging` is the reference implementation of PEP 425, 440 and 508 |
| which backend builds an archive | `_sdist.py`, in the sandbox | the answer is inside the archive, and evaluation cannot read it |
| how a node builds | `graph.nix` | a build step is a derivation, and only Nix writes one |
| where an artefact comes from | `uv.nix` | a fetch is a fixed-output derivation, and only Nix writes one |
| which shape the root takes | `Plan.root`, in the sandbox | the environment and the application are two nodes of one graph, and only the planner knows which node installed the member |

## Two shapes of a result, and which one to install

The root of the graph is a node of kind `venv` or a node of kind
`application`, and the `application` argument of `uv.nix` decides.

**A virtual environment holds every program of every member.** It carries
`bin/python`, `bin/activate` and `pyvenv.cfg`, because `make-venv.py` writes a
real one with `venv.EnvBuilder`. That is the shape for a development shell.

**An application holds the programs of the members that the caller names.**
`make-application.py` walks the store path of the node that installed each
named member, and it links each path of those trees out of the environment. A
list of members is one output, and not one output for each name. `nix-support` and `site-packages` are the two
things it skips, and `pyproject.nix` skips the same two in
`build/util/mk-application.py`. The output of `projects/workspace` with
`application = "ws-cli"` holds one entry:

```
bin/ws-cli -> /nix/store/...-workspace-app-env/bin/ws-cli
```

That shape is the one for `environment.systemPackages` of a NixOS
configuration. An environment there would put `bin/python` of the environment
on the PATH of every user, in front of the interpreter of the system.

**The closure does not shrink, and that is not the point.** The application is
238229120 bytes over 31 store paths, and its environment is 238228600 bytes
over 30. Every link names the environment, so the interpreter stays. What
changes is the PATH surface.

The result carries an `environment` attribute, which is the same project with
no application, so one set of arguments gives both shapes. **The two name one
store path**: the environment of an application takes the `-env` suffix, and
the attribute takes the same suffix, so a user who installs both gets one
environment. It costs a second planner build and no second environment, which
content addressing is the reason for.

## A smaller environment, and why it is usually larger

**The interpreter is the environment.** The environment of
`projects/pure-app` holds `rich` and two more distributions, and it is
238220632 bytes over 28 store paths. `python3` is 141459368 of those bytes, and
one file inside it, `libpython3.14.a`, is 66397194. No file of the environment
names that archive: it is the static library that builds an extension module.

**A total closure is the wrong number for a machine that runs Nix.** The store
is shared, and `pkgs.python3` is already in it: a NixOS system holds it, and so
does any other package of nixpkgs that needs an interpreter. What the
environment costs such a machine is what it adds, and that is a different
measurement:

```console
$ nix path-info -r --json <the environment>   # and subtract the closure of pkgs.python3
```

| a target that already holds `pkgs.python3` | it adds | new paths |
| --- | --- | --- |
| the environment, with `pkgs.python3` | 19207400 | 5 |
| the environment, with the stripped override | 82133056 | 6 |

**So a stripped interpreter makes the environment larger, and not smaller.**
The two interpreters share 22 of their 23 paths, and only the interpreter
itself differs: 141459368 bytes against 62925592. An override is a new store
path, so the machine holds both. The 62925592 bytes are added, and the
141459368 stay, because everything else on that machine still names them.

`default.nix` and `uv.nix` take a `python` argument anyway, and its default is
`pkgs.python3`:

```console
$ nix build --file ddrn/examples/venv-graph/uv.nix \
    --arg project ./ddrn/examples/venv-graph/projects/pure-app \
    --arg python '(import <nixpkgs> {}).python3.override {
      stripConfig = true; stripIdlelib = true; stripTests = true; stripTkinter = true;
    }'
```

| the whole closure, an empty target | bytes | the price |
| --- | --- | --- |
| `pkgs.python3`, the default | 238220632 | none |
| the stripped override | 159686920 | one CPython build, 357 s here |

**Those numbers apply to an empty target, and to nothing else.** A container
image that holds this closure and no more, or a `nix copy` to a machine that
runs nothing else, pays the whole 238220632 bytes, and the override takes 33%
off. Every other deployment shares the interpreter, and the override then costs
62925592 bytes and a build of CPython. Measure the target before you reach for
it.

The override does keep every optional dependency: `zlib`, `ssl`, `ctypes`,
`sqlite3`, `readline`, `curses`, `bz2`, `lzma` and `decimal` all import from
the environment that it builds. Each package that `fromNixpkgs` supplies needs
a build against the same interpreter as well.

**`pkgs.python3Minimal` is smaller still, and this lab cannot use it.** Its
closure is 77948792 bytes over 8 paths, which is 64% below `python3`, and
`cpython/default.nix` gives the reason for both halves of that saving:
`withMinimalDeps = true` turns off the optional dependencies, and
`stripConfig`, `stripIdlelib`, `stripTests` and `stripTkinter` each default to
that same value. **`zlib` goes with them, so the interpreter cannot open a
wheel.** Every wheel is a zip with deflate members, and `zipfile` raises:

```
RuntimeError: Compression requires the (missing) zlib module
```

`ssl` and `ctypes` are absent as well, so a project that speaks HTTPS or loads
a shared library cannot run there either. `ddrn/PACKAGING-FACTS.md` holds the
whole table of what each interpreter keeps, and the command that measured it.

## The tests

| suite | what it proves | command |
| --- | --- | --- |
| the decisions | each packaging rule, with no Nix | `pytest ddrn/examples/venv-graph/tests -m "not nix"` |
| the graph | the same code through a sealed build | `pytest ddrn/examples/venv-graph/tests -m "not network"` |
| the ATerm writer | it agrees with Nix itself | `pytest ddrn/tests` |
| seven real projects | end to end, from lock files that uv wrote | `pytest ddrn/examples/venv-graph/tests -m network` |

**A test that builds needs the development shell of the patched Nix**, because
it drives Nix through nanopynix and a worker is the build of its client:

```console
$ nix develop --impure --file . nanopynixMaster.shell \
    --command pytest ddrn/examples/venv-graph/tests
```


The sealed suite reaches no index: every artefact is a derivation of this
repository. The `network` mark names the tests whose artefacts come from PyPI,
so a failure of one of those may be an outage of an index.

`ddrn/examples/venv-graph/run-uv.sh` builds two of the seven, one step at a
time. It is the demonstration, and the pytest run above is the gate.

## Keeping this document current

`ddrn/architecture.dot` is the source of the drawing. After an edit of it, run:

```console
$ scripts/render-architecture.sh --write
```

`ddrn/tests/test_architecture.py` fails when the document holds a stale
drawing, and it fails when a node names a file that no longer exists.
