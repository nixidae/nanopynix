# local-index

A registry that is a directory of this project. `index/` holds one wheel and
one source distribution, and `uv.lock` resolves both out of it.

**The location has three parts, and the row of the artefact holds only the
last one:**

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

So the file of `crated` is `index/crated-1.0.0.tar.gz`, and a reader that
joined the row to the project would name `crated-1.0.0.tar.gz`, which does not
exist. uv joins the root of the workspace, the path of the registry and the
path of the row, in `to_registry_wheel` and in `to_source_dist`.
`ddrn/PACKAGING-FACTS.md` names the two lines.

**Neither row carries a hash.** A flat registry of a directory serves the file
itself, so uv records no digest for it. That costs nothing here, because
`uv.nix` copies the file with `builtins.path` and never fetches it.

`projects/vendored-app` is the other kind of file that this machine holds.
There the source *is* the file, and the row carries the name and the hash. The
two are the two shapes of `WheelWireSource` that name no address.

`crated` needs a build, so `flit-core` is in the `ddrn` dependency group. The
selection does not enable that group, so `flit_core` is not in the
environment; the graph installs it into the build of `crated` only.
`ddrn/examples/venv-graph/scripts/check-index.py` reads what the environment
holds.
