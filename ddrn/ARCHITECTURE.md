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

`plan.py` runs there, and it takes four steps:

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
| which fork, which extras, which groups | `_uvlock.py` | the rules are uv's, and uv's source states them |
| which artefact fits this target | `_resolve.py` | `packaging` is the reference implementation of PEP 425, 440 and 508 |
| which backend builds an archive | `_sdist.py`, in the sandbox | the answer is inside the archive, and evaluation cannot read it |
| how a node builds | `graph.nix` | a build step is a derivation, and only Nix writes one |
| where an artefact comes from | `uv.nix` | a fetch is a fixed-output derivation, and only Nix writes one |

## The tests

| suite | what it proves | command |
| --- | --- | --- |
| the decisions | each packaging rule, with no Nix | `pytest ddrn/examples/venv-graph/tests -m "not nix"` |
| the graph | the same code through a sealed build | `pytest ddrn/examples/venv-graph/tests` |
| the ATerm writer | it agrees with Nix itself | `pytest ddrn/tests` |
| seven real projects | end to end, from lock files that uv wrote | `ddrn/examples/venv-graph/run-uv.sh` |

The sealed suite reaches no index: every artefact is a derivation of this
repository. `run-uv.sh` needs a network, because its artefacts come from PyPI.

## Keeping this document current

`ddrn/architecture.dot` is the source of the drawing. After an edit of it, run:

```console
$ scripts/render-architecture.sh --write
```

`ddrn/tests/test_architecture.py` fails when the document holds a stale
drawing, and it fails when a node names a file that no longer exists.
