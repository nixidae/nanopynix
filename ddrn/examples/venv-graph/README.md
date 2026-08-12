# Build your own uv project as a graph of derivations

`./uv.nix` reads the `uv.lock` of a project and gives back one derivation. That
derivation is a **planner**: it runs in a sandbox, resolves the lock file with
`packaging`, and registers a graph of derivations that builds the environment.
`builtins.outputOf` names the environment that the graph produced.

**Read `ddrn/ARCHITECTURE.md` first if you want to know why.** This file says
how to use it.

## What it needs

This example does not run under the Nix of this repository's pin. It needs the
patched Nix that `nix/nix-master.nix` builds, and it needs the environment of
`nanopynix` built against that same Nix:

```console
$ repo=/path/to/npn-ddrn
$ nix build --impure --expr "(import $repo { }).nanopynixMaster.nix-cli" \
    --out-link ./nix-patched
$ nix build --impure --expr \
    "(import $repo { }).nanopynixMaster.pythonSet.mkVirtualEnv \"planner\" { nanopynix = [ ]; }" \
    --out-link ./planner-env
```

Every build below runs under `./nix-patched/bin/nix`, and it needs four
experimental features and two system features:

```console
$ features='nix-command flakes ca-derivations dynamic-derivations'
$ system='builder-rpc-v0 recursive-nix'
```

**The build goes into a private chroot store, and not into the store of the
machine.** The system daemon does not offer `builder-rpc-v0`, and a build that
the daemon runs never reaches the new code path. A chroot store makes the
client do the building itself. The store directory stays `/nix/store`, so
every dependency substitutes from the binary cache rather than building:

```console
$ store=$PWD/store
$ mkdir -p "$store"
$ ./nix-patched/bin/nix copy --to "$store" --no-check-sigs \
    "$(readlink -f ./nix-patched)" "$(readlink -f ./planner-env)" \
    --extra-experimental-features 'nix-command flakes'
```

`--no-check-sigs` is needed and is safe: both inputs were built on this
machine, so no cache signed them, and a chroot store refuses an unsigned path
by default.

`./run-uv.sh` does all of the above, and it then builds two projects of
`./projects`, one step at a time. Copy it rather than write these commands
again. `tests/test_projects.py` builds all seven, and it is the gate.

## The two steps

**The planner is one derivation, and the environment is the output of what it
registered.** So a build takes two steps, and the second one names the first:

```console
$ drv=$(./nix-patched/bin/nix build --store "$store" \
    --file "$repo/ddrn/examples/venv-graph/uv.nix" \
    --argstr nanopynixEnv "$(readlink -f ./planner-env)" \
    --arg project /path/to/my-project \
    --impure --no-link --print-out-paths --print-build-logs \
    --extra-experimental-features "$features" --system-features "$system")
$ ./nix-patched/bin/nix build --store "$store" "$drv^out" \
    --no-link --print-out-paths \
    --extra-experimental-features "$features" --system-features "$system"
```

The first command prints a `.drv` path, because a planner writes a derivation
and not a directory. The second builds the graph behind it.

**The `env` attribute holds the second step, so one command is enough.** See
"`env`, and one command instead of two" below. The two commands above stay the
way to read what each step produced.

**Give `project` an absolute path.** A relative path resolves against the
directory of `uv.nix`, and not against the shell.

**A store path of a chroot store is absolute, and the directory it names is
under the prefix.** So a program of the environment does not run from the
shell that built it: `bin/python` is a symbolic link to `/nix/store/...`,
which the host resolves to its own store. Run such a check as a derivation.
`./check-uv.nix` is the pattern, and every `check-*.nix` of this directory
uses it.

From a Nix expression, `builtins.outputOf` is the same step:

```nix
let
  planner = import ./uv.nix {
    inherit pkgs nanopynixEnv;
    project = ./my-project;
  };
in
builtins.outputOf planner.outPath "out"
```

`./check-uv.nix` is the smallest complete example of that expression.

## What your project needs in it

Two things, and `uv` writes both.

**A `uv.lock` of version 1.** `uv.nix` refuses any other version, and it names
the file when it does.

**A wheel installer in a dependency group.** Every node of the graph installs
its wheel with `pypa/installer`, and that program has to come from somewhere.
It comes from your own lock file, so one file pins the whole build:

```console
$ uv add --no-sync --group ddrn installer
```

The selection does not enable that group, so `installer` builds the environment
and does not enter it.

**A backend for each source distribution that the build reaches.** `uv.lock`
records no build system, for your project or for any archive it names, so each
backend goes in the same group:

```console
$ uv add --no-sync --group ddrn hatchling editables
```

`projects/README.md` gives the whole table of which project needs which
backend, and `ddrn/PACKAGING-FACTS.md` gives the reason that no lock file holds
this.

## The arguments you are most likely to want

| argument | what it decides |
| --- | --- |
| `project` | the directory that holds `pyproject.toml` and `uv.lock` |
| `select` | which members, extras and groups to install, spelled out. `null` reads the workspace and builds the list, the way `uv sync --all-packages` does |
| `extras` | which extras each member installs. `null` is no extra, `"all"` is `uv sync --all-extras`, and a list names them |
| `groups` | which dependency groups each member installs. `null` reads `tool.uv.default-groups`, `"all"` is `uv sync --all-groups`, and a list names them |
| `venvName` | the name of the environment. The default is the name of the project |
| `python` | the interpreter that the environment runs. See below |
| `fromNixpkgs` | `{ pyzmq = pkgs.python3Packages.pyzmq; }` takes a package from nixpkgs instead of the index, and the graph builds no wheel for that name |
| `fromSdist` | each member to install from its own source distribution, rather than as a PEP 660 editable |
| `editableRoot` | what a PEP 660 editable redirects to. The default is the variable `$DDRN_EDITABLE_ROOT`, so one environment serves every tree |
| `markers` | `platform_release` and `platform_version`, which no build can know |
| `uvCheck` | `true` runs `uv lock --check` over the project before the planner runs. See below |
| `application` | which members to render as programs rather than an environment. One name, or a list. See below |
| `mainProgram` | the program that `meta.mainProgram` of the `app` attribute names. The default is the sole name of `application`. See below |

## Extras and groups, the way `uv sync` takes them

`uv.lock` records which extra and which group each package declares, and it
records no decision about them. `uv sync` takes that decision on its command
line, and `uv.nix` takes it as `extras` and `groups`.

```nix
# every optional dependency of every member, and the test group of each one
import ./uv.nix {
  project = ./.;
  extras = "all";
  groups = [ "test" ];
}
```

Two defaults differ, and uv is the reason for both. `extras = null` installs no
extra, because uv keeps no default for an extra in `pyproject.toml`.
`groups = null` reads `tool.uv.default-groups` of your project, and it installs
the group `dev` when that table says nothing. A group that
`[dependency-groups]` does not declare stops the build with the message that uv
gives for the same file.

`select` overrides both. It names each member and the list of extras and groups
of that member, so a member that needs a different set than the others reaches
it through `select` and not through these two.

## An environment, or one program

The default output is a virtual environment, and a virtual environment holds
every program of every member:

```
bin/activate  bin/markdown-it  bin/pygmentize  bin/python  bin/python3  bin/ws-cli
```

That is what you want in a development shell. It is not what you want in
`environment.systemPackages` of a NixOS configuration, because `bin/python` of
the environment then shadows the interpreter of the system, and `bin/ws-cli`
arrives with five programs that your project does not ship.

`application` names one member, and the output becomes the tree of that member
over the same environment:

```nix
import ./uv.nix {
  project = ./.;
  application = "ws-cli";
}
```

```
bin/ws-cli -> /nix/store/...-workspace-app-env/bin/ws-cli
```

`pyproject.nix` calls this `util.mkApplication`, and it works the same way: the
`$out` of the one package is the template of the shape, and each path of that
template becomes a link into the environment. `nix-support` and
`site-packages` are the two things that the template does not offer.

**A list names several members, and it is still one output.** A project that
ships three programs ships them together, and three applications would each
hold a link into the same environment:

```nix
application = [ "ws-cli" "ws-web" ];
```

**The closure does not shrink, and that is not what this is for.** Measured
over `projects/workspace`: the application is 238229120 bytes over 31 store
paths, and its environment is 238228600 bytes over 30. The application adds
520 bytes and one path, which is itself. Every link names the environment, so
the interpreter stays. What changes is what reaches the PATH of a user.

An application is made of members of the environment, so the selection has to
install each one. A name that no member answers stops the build, and the
message names every such name and the members that the environment does hold.
A member that installs no program outside `site-packages` stops it as well: a
library adds nothing to an application, and an output that built and held
nothing is the worse failure.

### The environment of the same arguments

The result carries an `environment` attribute, which is this same project with
no application, and an `app` attribute, which is the application as a
derivation. So one set of arguments gives every shape:

```nix
let target = import ./uv.nix { project = ./.; application = "ws-cli"; };
in {
  packages.default = target.app;              # bin/ws-cli, and `nix run` finds it
  devShells.default = target.environment;     # bin/python, for a shell
}
```

**It is the same store path that the application links into.** The environment
of an application takes the `-env` suffix, and the attribute takes the same
suffix, so a user who installs both gets one environment and not two copies of
it. It costs a second planner build, because the two planners differ in one
variable, and no second environment: every node they submit is
content-addressing and identical.

### `env`, and one command instead of two

"The two steps" above gives the two commands that a build takes: one for the
planner, and one for the graph that the planner registered. `env` holds the
second step, so one command is enough:

```console
$ nix build --file ./uv.nix env --arg project /path/to/project
$ ./result/bin/python -c 'import rich'
```

It is the same wrapper that `app` is, and `application.nix` gives the reason a
wrapper is necessary: `builtins.outputOf` gives a string, and `nix build` takes
a derivation. The whole content is one symbolic link, so the closure grows by
this path alone.

`env` is the environment whether the call names an application or not. So `app`
and `env` of one call are the two halves of one project, and both reach the one
environment that the section above describes.

### `app`, and why a wrapper is necessary

`builtins.outputOf` gives a string, and a string carries no `meta`. So `nix
run` over the application itself fails with `attribute 'type' does not exist`,
and no hand-written attribute set repairs it: `nix run` reads `outPath` during
evaluation, and a dynamic output has no path until the graph runs.

`app` is a derivation whose whole content is one symbolic link to the
application. `nix run` realises it and reads the result after, so `nix run`
works. It costs 176 bytes and one store path, measured over
`projects/workspace`. `ddrn/NIX-FACTS.md` gives every measurement.

```console
$ nix build --file ./uv.nix app --arg project /path/to/workspace \
    --arg application '["ws-cli"]'
$ ./result/bin/ws-cli
```

`meta.mainProgram` takes the sole name of `application`, which is right
whenever the console script of a member carries the name of that member. **The
build verifies the name rather than trust it.** A member whose entry point is
called something else fails, and the message lists what the application holds:

```
the application holds no program called 'not-a-program'.
It holds:
ws-cli
Name one of those with the 'mainProgram' argument.
```

`application.nix` holds this derivation, and `uv.nix` is one caller of it.
`tests/sealed.nix` is the other, which is how the file is covered with no
network.

`lib.getExe` gives the deferred placeholder rather than a path, because it
interpolates `outPath` during evaluation. That placeholder is correct inside
another derivation, and a shell cannot use it.

## A stale `uv.lock` stops the build

`uv.nix` puts the `pyproject.toml` of each member into the store, and the
planner compares each one with the declaration that `uv.lock` recorded:

```
ddrn_venv._uvlock.LockError: uv.lock is stale: 'my-project' declares different
dependencies than the lock file resolved. pyproject.toml adds
['typing-extensions']. Run `uv lock` and build again.
```

A workspace that grew a member gets the other message. `uv.nix` lists the
directories of the workspace during the evaluation, because a directory that
`uv.lock` never named is in no store path, and the planner reads
`[tool.uv.workspace] members` over that listing:

```
ddrn_venv._uvlock.LockError: uv.lock is stale: it resolved a different set of
workspace members. the workspace adds ['ws-extra']. Run `uv lock` and build
again.
```

Run `uv lock`, and build again. `ddrn/PACKAGING-FACTS.md` gives what each
comparison covers, the one edit that passes it, and what each glob of
`[tool.uv.workspace] members` means.

### Ask uv itself, with `uvCheck = true`

Those two comparisons are the ones that a build reaches, and `Lock::satisfies`
of uv holds 21. A marker on one dependency passes both, because uv simplifies a
marker against `requires-python` and a reader that compared the text would call
an ordinary lock file stale.

`uvCheck = true` runs `uv lock --check` over your project, in a derivation that
the planner waits for:

```nix
import ./uv.nix {
  project = ./.;
  uvCheck = true;
}
```

`--check` is the mode that asserts `uv.lock` would not change after a
resolution, and it exits with an error when it would. That exit code is the
verdict of the derivation. The other three modes of `uv lock` each exit 0 over
a stale project, and plain `uv lock` would rewrite the copy of the lock file in
the sandbox and hide the difference. When the check refuses, it asks uv once
more with `--frozen`, which resolves nothing and names a structural cause such
as `Missing workspace member ws-extra`.

The check costs 27 ms and reaches no network, and it changes no output: the
same project builds the same environment with the argument on and off. It costs
one thing, which is why it is off by default: **the whole project tree enters
the store**, so every edit of every file makes a new check. Without the
argument, this file reads `pyproject.toml` and `uv.lock` and nothing else.

`nix build --file ./uv.nix lockCheck --arg project ./.` runs the check alone,
and builds no graph.

## A smaller environment, for one deployment only

**The interpreter is the environment.** A two-package environment is 238220632
bytes, and `python3` is 141459368 of that. The `python` argument takes a
smaller one:

```nix
python = pkgs.python3.override {
  stripConfig = true;
  stripIdlelib = true;
  stripTests = true;
  stripTkinter = true;
};
```

That drops the whole closure to 159686920 bytes, which is 33%, and every
optional module still imports. It costs a build of CPython, because no binary
cache holds a derivation that nixpkgs never made.

**Use it for a single-purpose container image, and for nothing else.** A store
that holds `pkgs.python3` already shares 22 of the 23 paths of the override, so
the override adds a second interpreter rather than replacing the first:

| a target that already holds `pkgs.python3` | it adds | new paths |
| --- | --- | --- |
| the default, `pkgs.python3` | 19207400 | 5 |
| the stripped override | 82133056 | 6 |

**Do not reach for `pkgs.python3Minimal`.** It is smaller again, and it drops
`zlib`, so it cannot open a wheel at all. `ddrn/ARCHITECTURE.md` gives the
message that `zipfile` raises, and the whole table.

## The tests

```console
$ pytest ddrn/examples/venv-graph/tests -m "not nix"    # the decisions, one second
$ shell="nix develop --impure --file . nanopynixMaster.shell --command"
$ $shell pytest ddrn/examples/venv-graph/tests -m "not network"   # and the sealed graph
$ $shell pytest ddrn/examples/venv-graph/tests          # and the seven real projects
$ ddrn/examples/venv-graph/run-uv.sh                    # two of them, step by step
$ ddrn/examples/venv-graph/bench/measure.sh --cold      # what an evaluation costs
```

**Every test that builds runs through nanopynix, and none runs the `nix`
command.** One RPC session evaluates each expression, builds each derivation
and reads each result. That session has to link the patched Nix, and
`nanopynixMaster.shell` is the development shell that does: the worker is the
build of the client, so the two cannot differ. A plain `pytest` skips each
such test, and the message gives the command above.

These tests are not part of the suite of the repository. `tests/pytest.ini`
gives that directory its own rootdir, and says why.
