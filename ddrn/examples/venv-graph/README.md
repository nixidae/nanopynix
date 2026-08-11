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

`./run-uv.sh` does all of the above, and it builds the seven projects of
`./projects`. Copy it rather than write these commands again.

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

**Give `project` an absolute path.** A relative path resolves against the
directory of `uv.nix`, and not against the shell.

**A store path of a chroot store is absolute, and the directory it names is
under the prefix.** So a program of the environment does not run from the
shell that built it: `bin/python` is a symbolic link to `/nix/store/...`,
which the host resolves to its own store. Run such a check as a derivation.
`./check-uv.nix` is the pattern, and every check of `run-uv.sh` uses it.

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
$ pytest ddrn/examples/venv-graph/tests -m "not nix"   # the decisions, one second
$ pytest ddrn/examples/venv-graph/tests                # and the sealed graph
$ ddrn/examples/venv-graph/run-uv.sh                   # seven real projects
$ ddrn/examples/venv-graph/bench/measure.sh --cold     # what an evaluation costs
```

These tests are not part of the suite of the repository. `tests/pytest.ini`
gives that directory its own rootdir, and says why.
