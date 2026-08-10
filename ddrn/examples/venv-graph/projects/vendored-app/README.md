# vendored-app

Two dependencies that no index serves. Each one is a file of `vendor/`, and
`uv.lock` names it by a path relative to this directory.

`vendored` is a wheel, and `archived` is a source distribution. The two are the
two shapes that a `path` source takes, and `uv.nix` reads both:

```toml
[[package]]
name = "archived"
source = { path = "vendor/archived-1.0.0.tar.gz" }
sdist = { hash = "sha256:9207336232d4501be209c97c00472b0e34f1ef720b231274743ca2b9d21a76b2" }

[[package]]
name = "vendored"
source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
wheels = [
    { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:7353…" },
]
```

**The location is on the source, and the row of the artefact carries no
address.** A package of an index gives a `url` on each row of `wheels` and on
`sdist`. Here the wheel row gives the file name and the hash, and the `sdist`
table gives the hash alone. `PACKAGING-FACTS.md` records the three shapes that
`WheelWireSource` of uv defines.

`archived` needs a build, so `flit-core` is in the `ddrn` dependency group. The
selection does not enable that group, so `flit_core` is not in the environment;
the graph installs it into the build of `archived` only.

The two files of `vendor/` are in this repository, because a lock file that
names a path needs that path to exist. Both are small, and
`ddrn/examples/venv-graph/scripts/check-vendored.py` reads what they hold.
