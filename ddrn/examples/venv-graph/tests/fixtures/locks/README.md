# Lock files, from uv2nix and from uv

Each `.lock` file here is a copy of `lib/fixtures/<name>/uv.lock` of
[uv2nix](https://github.com/pyproject-nix/uv2nix). `kitchen-sink.lock` is
`lib/fixtures/kitchen-sink/a/uv.lock`. `two-indexes.lock` comes from uv itself,
and the next section gives it.

**These are the files that uv itself wrote**, and a hand-written lock file is
not. A reader that a hand-written fixture accepts proves that the reader
agrees with the person who wrote the fixture, and nothing more.

uv2nix also holds the expected resolution of seven of them, in
`lib/expected/lock1.resolveDependencies.*.json`. `test_uvlock.py` carries those
seven answers, so this reader and uv2nix must agree.

uv2nix is MIT, Copyright (c) 2024 Adam "adisbladis" Hoese. The `LICENSE` file
of that repository holds the text.

## The two files that name a place

`git-subdirectory.lock` and `url-subdirectory.lock` are the two source kinds
that name a place on the network, and both of them name one project:
`hatchling`, which lives in `backend` of `pypa/hatch`. So the pair describes
one project through two doors, and `test_git_and_url_sources.py` reads the
reference out of each file rather than write the string again.

**No test of this directory reaches the network.** `test_uvlock.py` and
`test_git_and_url_sources.py` parse the files and nothing more. The sealed
test builds the same two shapes over fixtures of `../`, which are derivations
of this repository, and `ddrn/PACKAGING-FACTS.md` records what a git source
costs when a real evaluation reads one.

## The file that holds one package twice

`two-indexes.lock` is the output of `lock_multiple_sources_index_disjoint_markers`,
which is a test of uv (`crates/uv/tests/lock/lock.rs`). uv writes the whole lock
file into the source of that test, so the copy here is what uv produced.

It pins `jinja2` 3.1.3 to one index of PyTorch for Windows, and to another index
for every other system. **The two entries carry one name and one version, and
they differ in the source alone.** A reader that identifies a package by the
name and the version finds them equal, and it installs whichever one it met
first. `TestThePackageIdentity` of `test_uvlock.py` resolves the file for two
environments and asserts the index of each answer.

To take a new one from uv2nix, copy the file and give it the name of its
directory:

```console
$ cp ~/Code/uv2nix/lib/fixtures/<name>/uv.lock <name>.lock
```
