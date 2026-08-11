# What a real `uv.lock` costs to evaluate

`./measure.sh` instantiates `../uv.nix` over `./uv.lock` and reports what that
costs. **It needs no network and it builds nothing.** Instantiation writes a
`.drv` for every artefact of the lock file and downloads none of them, which is
the property that the whole design rests on.

```console
$ ddrn/examples/venv-graph/bench/measure.sh          # warm, in the store of this machine
$ ddrn/examples/venv-graph/bench/measure.sh --cold   # into an empty store, which writes each .drv
```

`ddrn/PACKAGING-FACTS.md` holds the numbers that this script produced, and it
names this script beside each one.

## The project

`uv` wrote `uv.lock` from `pyproject.toml`, with the command that
`../projects/README.md` gives for every other project:

```console
$ nix run --file . pkgs.uv -- add --no-sync anthropic boto3 jupyterlab pandas
$ nix run --file . pkgs.uv -- add --no-sync --group ddrn installer "flit-core>=3.11,<4"
```

Four dependencies that a real user has: an API client, a cloud SDK, a notebook
server and a data frame library. The resolution is 111 packages and 715
artefacts, and 110 of those artefacts are source distributions.

**Nothing builds this project.** `../run-uv.sh` builds the seven projects of
`../projects`, and this one is not among them: the point here is the cost of
the menu, and a build of `pandas` measures a compiler.

## The method, and what each number means

**The planner environment is a constant, and not the subject.** `uv.nix` builds
the same menu whatever the planner is, so the script takes any store path and
takes `pkgs.hello` by default. A real nanopynix environment costs a 20 minute
build and changes no number below, except the closure size, which then also
holds that environment.

**A warm run and a cold run measure two different things**, and the script
separates them:

- *warm* runs against the store of this machine, where every `.drv` is already
  written. It measures the evaluation alone.
- *cold* copies the placeholder environment into an empty store and
  instantiates there. It measures the evaluation plus the write of every
  `.drv`.

A run that says "first" against a store that already holds the result measures
the warm case and reports it as the cold one. The script therefore makes an
empty store rather than trust the order of two runs.

## What the numbers said, and what changed because of them

The measurement found that 331 of the 1811 `.drv` files carry a copy of the
whole menu, and that those copies are 116 MB of the 119 MB of derivation text.
`ddrn/PACKAGING-FACTS.md` records the number and the change that followed.
