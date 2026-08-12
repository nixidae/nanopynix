# Facts about Nix, and where each one comes from

**Every entry names the file and the line that proves it.** A fact with no
citation does not belong here. A fact that a measurement proved says which
measurement.

Line numbers follow the Nix revision that `nix/nix-master.nix` pins, and the
workspace at `~/Code/ddnix`. A line number moves. The name of the function does
not, so each entry names the function too.

## Store paths

**A derivation lives at a text content-addressed path of its own contents.**
`infoForDerivation` (`src/libstore/derivations.cc:100`) hashes the unparsed
ATerm, builds `TextInfo{hash, references}`, and calls
`makeFixedOutputPathFromCA`. `Store::writeDerivation`
(`src/libstore/derivations.cc:117`) then ends with `assert(path2 == path)`.

Three consequences follow, and all three matter:

- A text-ingested store object named `X.drv`, holding the ATerm, with the
  inputs of the derivation as its references, lands **exactly** on the
  canonical derivation path. This is why the released `builder-rpc-v0` uses
  `outputHashMode = "text"`. It is the mechanism, and not a coincidence.
- The store path of a derivation is already a content hash of that derivation.
  A reference to a derivation needs no separate hash to be self-certifying.
- The references are part of the hash, so the same ATerm with a different
  reference set gives a different path.

**A fixed output has a store path that the name gives it.**
`DerivationOutput::CAFixed::path` (`src/libstore/derivation/output.cc:26`)
calls `makeFixedOutputPathFromCA(outputPathName(drvName, outputName), ...)`.
`CAFloating` returns `std::nullopt` from `path()`
(`src/libstore/derivation/output.cc:19`), because a floating output has no
path until a build gives it one.

## Names

**`Derivation::nameFromPath` requires the `.drv` extension.**
`src/libstore/derivations.cc:263` calls `drvPath.requireDerivation()`. So a
store object that Nix reads as a derivation must be named `*.drv`, whatever
else changes.

**`builtins.derivation` guards the `.drv` name.** `src/libexpr/primops.cc:1815`
refuses a name that ends in `.drv` unless the ingestion method is `Text` and
there is exactly one output, named `out`.

**`outputHashMode = "text"` needs `Xp::DynamicDerivations`, and no `.drv`
name.** `src/libexpr/primops.cc:1570`. So an ordinary name with text ingestion
is allowed, and only the reverse pairing is not.

## Why the `.drv` naming schema exists

**The question.** The released design makes a planner carry the name
`graph.drv`, and the root that it produces carry the name `graph`. Nothing
says why, and the rule looks arbitrary. It is not: it is the end of a chain,
and each link is enforced somewhere else.

**Link 1. The `.drv` suffix is a type tag that the store enforces.**
`LocalStore::registerValidPath` (`src/libstore/local-store.cc:757`) parses
**every** store object whose name ends in `.drv`, and then calls
`checkInvariants`. So "a store object named `*.drv` is a derivation" is an
invariant of the store, and not a convention. Measured: a text object named
`junk.drv` that holds the text "this is not a derivation" cannot enter the
store at all. `nix store add` fails with `error parsing derivation '...':
expected string 'D'` (`submit-drv-broken.sh`, attribute `notADerivation`).

**Link 2. An output takes the name of the derivation.** `outputPathName`
(`src/libstore/derivations.cc:150`) gives the output `out` the name of the
derivation, and gives each other output that name with `-<output>` after it.
So a derivation whose output is a derivation has to be named `*.drv` itself.

**Link 3. Evaluation refuses that name, unless the derivation makes a real
derivation.** `src/libexpr/primops.cc:1815` permits a name ending in `.drv`
only for `ingestionMethod == Text` with exactly one output, named `out`. This
turns the failure of link 1 into an error at evaluation, with a message that
names the cause, instead of a parse error at the end of a build.

**Link 4. Text ingestion is what makes the output a real derivation, and not a
copy of one.** A text-ingested object holding the ATerm, with the inputs of
the derivation as its references, lands on the canonical derivation path. See
"Store paths" above. So the permitted shape in link 3 is exactly the shape
that produces a genuine derivation where a derivation belongs.

**Link 5. The name rule then couples the two names.**
`checkOutputs` (`src/libstore/build/derivation-check.cc:109`) requires
`outputPathName(drv.name, outputName) == info.path.name()`. This is a general
rule for every output, and not a rule about dynamic derivations: for an
input-addressed output the path is computed with that name, so it holds
without a check, and for a content-addressed output the builder chooses the
path, so the check is the only thing that enforces it.

**The result of the chain is the coupling.** The planner must be named
`graph.drv` because the root it produces is named `graph`, and the planner
computes that root when it runs. So the planner declares a result of its own
execution, in advance.

**Upstream states the intent that links 1 to 4 protect.**
`doc/manual/source/store/resolution.md:209`: "an arbitrary store object can be
read back as a derivation (as will in fact be done in case for dynamic
derivations / nested output deriving paths)."

**The relaxation of this lab breaks link 5 only.** It keeps the invariant of
link 1 by *verifying* the submitted object rather than by *naming* it: the
object must parse as a derivation, and `computeStorePath` of that derivation
must give the path where it is.

### Where each limit came from

Nobody designed the coupling. It is the intersection of three rules that
arrived twenty years apart, and no commit ever states the coupling as a goal.
Found with `jj log -r 'diff_lines(substring:"...")'` over the whole history.

**Link 3 started as a flat ban, in January 2005.** Commit `6bb5efadeceb`, by
Eelco Dolstra: "Ensure that derivation names and sources don't end in `.drv'."
It added the ban for a derivation name and for a source path in the same
change, with the message `file names are not allowed to end in '.drv'`. The
reason is link 1: at that time the suffix was already the type tag, and the ban
kept a store object that is not a derivation from wearing it.

**Link 3 became the exception it is now in October 2020.** Commit
`a4e5de1b9d26`, by John Ericson: "Derivations can output 'text-hashed' data".
It changed the flat ban into `isDerivation(drvName) && ingestionMethod != Text`,
so a derivation that makes a real derivation may carry the name. The commit
message states the ramification and its limit:

> In particular, this means that derivations can output derivations. But that
> ramification isn't (yet!) useful as we would want, since there is no way to
> have a dependent derivation that is itself a dependent derivation.

**Link 2 and link 5 came from the content-addressed derivations work, in August
2020.** Commit `e913a2989fd7`, "Squashed get CA derivations building",
introduced `outputPathName` and the comparison in `checkOutputs` in one change.
The rule exists because a fixed content-addressed output computes its path from
that name, so the name and the path have to agree.

**So the limit predates dynamic derivations, and it protects a different
thing.** Link 3 protects the type tag, link 5 protects the path of a
content-addressed output, and neither one was written with a planner in mind.
The relaxation of this lab replaces link 5 for the one case that link 3 already
permits, and it protects the type tag directly instead of through a name.

## What the store checks on its own

**`LocalStore::registerValidPath` parses every `.drv` that it registers.**
`src/libstore/local-store.cc:757` calls `readInvalidDerivation` and then
`checkInvariants`. A store object named `*.drv` that does not parse cannot
enter the store at all.

**`checkInvariants` does not verify the store path.**
`src/libstore/derivations.cc:621` compares the name in the path with
`drv.name`, and it compares the output path of each input-addressed output.
It never recomputes the whole store path. A **floating** content-addressed
derivation has no output path to compare, so nothing catches one that sits at
a path its own contents do not give. Measured: `submit-drv-broken.sh`,
attribute `wrongPath`.

## builder-rpc-v0

**The feature requires a content-addressing derivation.**
`src/libstore/unix/build/derivation-builder.cc:481`:

```cpp
if (usingSubmitted && !type(drv).isCA())
    throw Error("The builder-rpc-v0 feature may only be used with content-addressing derivations");
```

The reason is the design, and not the protocol: the submitted output **is** the
derivation, and a derivation path is a content-addressed path. Remove that
identity and the requirement loses its reason.

**A builder-rpc-v0 build gets no `$out`.** The output arrives through
`SubmitOutput`, which is worker operation 1000.

**Paths that the builder adds are referenceable.**
`src/libstore/unix/build/derivation-builder.cc:1105` builds
`referenceablePaths` from the input paths, the scratch outputs, **and**
`state_.lock()->addedPaths`. `scanForReferences` runs over the output at line
1211. So an ordinary output that names a path the builder registered gets that
path recorded as a reference, with no declaration and no `--scan`.

**The allowlist of the restricted socket.** `src/libstore/daemon.cc`, in
`performOp` under `RecursiveFlag::RecursiveSubmitted`: `AddToStore`,
`AddMultipleToStore`, `AddToStoreNar`, `AddToStoreScanning`, `SubmitOutput`,
`AddTempRoot`, `IsValidPath`, and `EnsurePath` after the patch of this lab.
The refusal reads `Operation %d not allowed inside derivation`. `EnsurePath`
is operation 10.

**`builtins.storePath` and `builtins.appendContext` each make one daemon
call, and it is `ensurePath`.** `src/libexpr/primops.cc` and
`src/libexpr/primops/context.cc:274`. Every other store call of those two
primops is path arithmetic that `StoreDirConfig` answers with no daemon.

**The mount table does not save `builtins.storePath`.** That primop skips
`ensurePath` when `state.storeFS->getMount(...)` finds the path, and the
closure is bind-mounted in the sandbox, so the guard looked like it might fire
already. Measured with the allowlist entry removed and Nix rebuilt: both
primops fail with `Operation 10 not allowed inside derivation`. The mount
table fills lazily and holds no entry for a closure path that nothing touched.

**`RestrictedBuilder::ensurePath` substitutes nothing.**
`src/libstore/restricted-store.cc:275` asserts `goal.isAllowed(path)` and then
does nothing. `isAllowed` means the path is in `inputPaths`, or this builder
added it. So the operation reaches no further than `IsValidPath` does.

### The allowlist stops an import from derivation, and the setting does not

The root evaluation of `ddrn/examples/venv-graph/uv.nix` does no import from
derivation. Measured on 2026-08-12, with `--option
allow-import-from-derivation false`: every one of the seven projects gives its
`env.drvPath`, and so do `app`, `shell` and `lockCheck`. The default of
`nanopynixEnv`, which imports this whole repository, gives it as well, and a
whole build under the same option succeeds. `builtins.outputOf` is what makes
this work: `nix eval --raw ... app` prints the placeholder
`/0cnf5h68fly83gkk6rv5g269qddvls2pb6pqk4q15b7a3ncqmhfd`, and not a store path,
so nothing is built to name the application.

**The planner runs a second evaluator, and the option of the first one does not
reach it.** `ddrn_venv._plan.submit` builds `nix_expr.EvalState(store)` over
`graph.nix` inside the sandbox. Two things keep the two evaluators apart:

- `EvalState` holds `const EvalSettings & settings`
  (`src/libexpr/include/nix/expr/eval.hh:403`), and the check reads that
  reference (`src/libexpr/primops.cc:118`). The only global `evalSettings` is
  `extern EvalSettings evalSettings` of **libcmd**
  (`src/libcmd/include/nix/cmd/common-eval-args.hh:37`), which belongs to the
  command line program.
- `PyEvalState` owns `nix::EvalSettings evalSettings{_readOnlyMode}`
  (`nanopynix-bindings/src/py_eval.hh:34`), one for each evaluator, and
  `NixEvalSettings.allow_import_from_derivation` is a live field of that
  instance.

So an evaluator that forbids the import and an evaluator that permits it can
run in one process, and the planner is a separate process in any case.

**The allowlist is what stops the nested evaluator, and it stops it whatever
the setting says.** Measured with a temporary `builtins.readFile (derivation
{ ... })` in `graph.nix`, forced with `builtins.seq`, and the option left at
its default of `true`:

```
venv-planner>            37|   ifdProbe = builtins.readFile (derivation {
venv-planner>        error: Operation 9 not allowed inside derivation
```

Operation 9 is `BuildPaths`
(`src/libstore/include/nix/store/worker-protocol.hh:238`). The code path of the
import reads the setting first and calls `store->buildPaths` after
(`src/libexpr/primops.cc:118` and the lines below it), so the refusal comes
from the store and not from the evaluator.

**To permit an import inside a planner, add a build operation to
`validOperations`.** No setting does it. That is the guard against a build that
is not reproducible, which is the reason the allowlist exists, and
`RestrictedBuilder` already asserts closure membership and substitutes nothing.

## Submission, and what the daemon checks

**`SubmitOutput` accepts an `Opaque` deriving path only.**
`src/libstore/unix/build/derivation-builder-impl.hh:192`. A `Built` path gives
`Attempted to submit Built path '%s' for output '%s'.\n Only Opaque paths are
supported`, and the message links NixOS/nix#12727. A caller that holds a
derivation must therefore reduce it to the store path of that derivation.

**`checkSubmittedOutputs` is the whole of the submission check.**
`src/libstore/unix/build/derivation-builder.cc:1700`. It reads the path info of
each submitted path, runs `checkOutputs` with `OutputSource::Submitted`,
refuses a declared output that no submission covered, asserts that the output
is `CAFixed` or `CAFloating`, and registers one realisation for each output.
Nothing else runs. The output gets no signature, because only the realisation
matters.

**The ingestion method must agree, and this is why `outputHashMode = "text"`
stays necessary for a planner that submits a derivation.**
`src/libstore/build/derivation-check.cc:53`, in the `CAFloating` branch of
`checkCAOutput`:

```cpp
if (info.ca->method != dof.method)
    throw BuildError(... "was hashed with method '%s', expected '%s'" ...);
```

Every derivation ingests as `text`, because `writeDerivation` adds the ATerm
with text ingestion. So a planner that submits a derivation declares `text`,
and a planner that submits an ordinary directory declares `nar`.

**The `Deferred`, `Impure` and `InputAddressed` branches of `checkCAOutput` do
nothing.** Same file, line 74. The check is meaningful only for a
content-addressing output, which is the output kind that `builder-rpc-v0`
requires anyway.

## The evaluator inside a build

**`EvalState::coerceToSingleDerivedPathUnchecked` needs a string.**
`src/libexpr/eval.cc:2660` calls `forceString` and then requires exactly one
context entry. An attribute set that is a derivation fails with `expected a
string but found a set`, so a caller reduces the derivation through its
`outPath` attribute first. `state->s.outPath` is the symbol, and
`state->isDerivation(v)` is the test.

**A derivation stands for its output, so `--submit` takes the `drvPath` field
of the resulting `Built` path.** The build that runs now makes the derivation.
No build made the output of that derivation yet, so the output is not a store
object that anything can submit.

**The evaluator inside the sandbox gives the same store path as an evaluator
outside it.** `tests/functional/dyn-drv/eval-submit.sh` builds the same graph
through two routes: one planner evaluates it directly, and a second planner two
levels down evaluates it inside a second sandbox. The test asserts that the two
root derivation paths are equal, and they are.

**`builtins.toFile` records the store paths of the context of its argument as
references of the file that it makes.** So an expression that interpolates
another `toFile` path reaches the sandbox with the whole closure, as an input
of the derivation that reads it. This is how a nested planner gets its script.

**`builtins.storePath` is what gives a plain path a context inside the
sandbox.** A path that an expression holds as literal text is no input of the
derivation that the evaluator writes. `builtins.storePath` asks the daemon,
which is `EnsurePath`, and the restricted socket answers only because of the
allowlist entry of this lab. The test asserts the resulting `inputs.srcs`
entry.

**`builtins.outputOf` chains as deep as the graph goes.** The same test follows
three levels: a planner submits the derivation of a second planner, that
planner submits the root of a graph, and the root gives the file.

### How far the evaluator inside a build reaches into nixpkgs

Measured with a probe that a planner runs, over nixpkgs
`26.11pre1040357.e2587caef70c`. `ddrn/PACKAGING-FACTS.md` says why the question
matters.

**The whole nixpkgs tree fits in the sandbox.** One attribute,
`DDRN_NIXPKGS = "${pkgs.path}"`, makes 345 MiB and 53558 files an input source
of the planner, through the string context of that path. The probe finds the
directory, and it finds `lib/` under it.

**`import (nixpkgs + "/lib")` works, and it costs nothing.** 0.0 s, 86 MiB of
resident memory, no store operation at all, and it returns
`26.11pre1040357.e2587caef70c`. The `lib` of nixpkgs is pure Nix, and it never
speaks to a store.

**`import nixpkgs { system = ...; config = {}; overlays = []; }` works too.**
0.1 s, 127 MiB, no store operation, and it returns a set. Evaluation is lazy,
so the whole package set exists before any derivation of it does.

**Instantiating one derivation of that set does not work.**
`(import nixpkgs { ... }).hello.drvPath` fails with `Operation 26 not allowed
inside derivation`. Operation 26 is `QueryPathInfo`
(`src/libstore/include/nix/store/worker-protocol.hh:253`).

**`builtins.readFile` of a path in the store reaches that same operation, and
libexpr swallows the error.** `src/libexpr/primops.cc:2174` reads the file and
then asks `queryPathInfo` for the references of the store path, so that it can
re-scan them into the string context. The call sits in
`try { ... } catch (Error &) { }`. The probe read `/.version` of the nixpkgs
tree and got `26.11` back, with no context.

**A refused operation is not a soft failure. It kills the connection.** The
refusal throws in `daemon.cc` at protocol dispatch, before the arguments of the
operation come off the wire, so the protocol desynchronises. Measured: after
one swallowed `readFile` refusal, the next store call of the same evaluator
failed with `write of 80 bytes: Broken pipe`, and an earlier run of the probe
ended in

```text
terminating due to unexpected unrecoverable internal error: Assertion 'block'
failed in nix::WorkerProto::BasicClientConnection::processStderrReturn at
../worker-protocol-connection.cc:106
```

and then SIGSEGV. **A caller that catches the error still loses the socket.**
This belongs upstream on its own: an allowlist that desynchronises the protocol
gives a crash where it means to give a diagnostic.

**Every kind of child works under a submitted root.** The test covers a graph
of floating content-addressed derivations, a graph of input-addressed
derivations, and an input-addressed root over one floating child. The third
case makes the root deferred, so the root has no output path until the child is
built.

**nanopynix reaches the same result, and `ddrn/examples/evaluated-graph` is the
measurement.** A derivation named `planner` submitted
`/nix/store/ydwl54aq…-graph.drv`, and the graph realised to a directory holding
`alpha` and `beta`. The route is `EvalState.eval_string`, then
`Value.derived_path`, then `Store.submit_output`. No ATerm, and no `nix`
binary.

**The two kinds of child look different in the ATerm, and the difference is
visible in that run.** `nix derivation show` of the submitted root gives:

```json
"args": ["-c", "mkdir -p \"$out\"\ncp /1jgic4bscb63… \"$out/a\"\ncp /nix/store/5q9bz587…-leaf-b \"$out/b\"\n"]
```

`leaf-a` floats, so the evaluator wrote a downstream placeholder, which the
build rewrites. `leaf-b` is input-addressed, so the evaluator wrote the real
output path. One `EvalState` computed that path with no read of a `.drv` back
out of the store, which is the read that the allowlist refuses.

**`inputs.srcs` of that root holds bash and coreutils.** Each one reached the
derivation through `builtins.storePath`, and so through `EnsurePath`. Without
the allowlist entry of this lab, the expression stops at that primop.

**The realised root sits at a different path from the submitted root.** The run
submitted `ydwl54aq…-graph.drv` and built `cz50wrj9…-graph.drv`. The second is
the *resolved* derivation: a floating child has a real output path once it is
built, and resolution rewrites the placeholder to it. This is ordinary for a
content-addressed graph, and it is not a symptom.

## A pre-instantiated menu, and how a planner uses one

**`builtins.appendContext` turns a bare output path back into a dependency.**
`src/libexpr/primops/context.cc:304`, the `outputs` branch, makes a
`NixStringContextElem::Built` for each named output of the key:

```cpp
context.emplace(NixStringContextElem::Built{
    .drvPath = makeConstantStorePathRef(namePath),
    .output = std::string{outputName},
});
```

So `builtins.appendContext out { ${drv} = { outputs = [ "out" ]; }; }` gives a
string that *is* the output path and that *depends on* the derivation. A
derivation that interpolates it records `drv` as an input.

**That primop calls `ensurePath` on the key** (same file, line 275), so it
reaches the allowlist entry of this lab. A planner cannot use a
pre-instantiated menu without it.

**The two `unsafeDiscard*` builtins are what keep the menu lazy, and they are
the inverse of the line above.** `unsafeDiscardOutputDependency` leaves the
`.drv` as an input source while the output stays unbuilt.
`unsafeDiscardStringContext` gives the output path with no dependency at all.

**`builtins.toJSON` keeps the string context of what it serialises.** So a
menu of store paths in one environment variable makes every path in it an
input source of the derivation. `ddrn/examples/venv-graph/default.nix` passes
21 artefacts, 6 tools and 3 scripts that way, in `DDRN_INPUTS`.

### What a `.drv`-only menu costs, measured

A probe in the scratchpad put `unsafeDiscardOutputDependency pkg.drvPath` in
the environment of a plain `derivation`, and counted `/nix/store` from inside
the sandbox. **This is unpatched Nix. The mechanism needs no allowlist entry
and no experimental feature.**

The `.drv` file arrives, its whole `.drv` closure arrives with it, and the
output stays absent:

```
probe: DRVREF present   /nix/store/0nnrl…-hello-2.12.3.drv
probe: OUTREF ABSENT    /nix/store/c2h2f…-hello-2.12.3
probe: STDENV present   /nix/store/yd8x4…-stdenv-linux.drv
probe: SRCDRV present   /nix/store/z85z5…-hello-2.12.3.tar.gz.drv
```

`stdenv-linux.drv` and the `fetchurl` derivation of the source are both there,
so the closure is the transitive one and not the single file.

The count of `.drv` files in the sandbox, against the menu that the derivation
declares:

| menu | store entries | of which `.drv` |
| --- | --- | --- |
| nothing | 29 | 0 |
| `hello` | 792 | 605 |
| `python3` | 927 | 699 |
| `python3Packages.numpy` | 1362 | 1057 |
| `numpy`, `pandas`, `requests` | 1390 | 1085 |
| all of `python3Packages` | 37669 | 35994 |

**The closures overlap almost completely.** Two more packages after `numpy`
cost 28 more `.drv`, because `stdenv` and the interpreter are already there.
The first package of a menu is the expensive one, and each package after it is
nearly free.

**A menu of more than a few thousand entries does not fit in an environment
variable.** All of `python3Packages` is 10558 packages that evaluate, and 2.2
MB of JSON. The build then fails before the builder starts:

```
error: executing '…/bin/bash': Argument list too long
```

**`pkgs.writeText` carries a menu that `builtins.toFile` cannot.** `toFile`
refuses a string that holds a store path. `writeText` is a derivation, so the
reference scan of its output records every `.drv` path in the text, and the
closure of that one file carries the whole menu into the sandbox. The full
`python3Packages` menu measures 154.4 MiB over 37640 paths, of which 35994 are
`.drv`, and **no output of any of the 10558 packages is built**.

Evaluating that whole set costs 17.7 s with a cold evaluation cache.

### The return path, proved

A second probe closed the loop. A `builder-rpc-v0` planner took `hello` as a
`.drv`-only dependency, wrote one node whose `input_drvs` names that `.drv`,
and submitted the node. The planner reported the state it saw:

```
==> menu .drv present in the sandbox: True
==> menu output already built:        False
==> wrote /nix/store/3lw0vkz9nixhpwm6v1dgp3g4fnsvnf2x-graph.drv
```

`builtins.outputOf` then realised that submitted `.drv` in the outer store:

```
copying path '/nix/store/c2h2f4cw9p8i8zcfy52fd1dd6g0yhnki-hello-2.12.3' from 'https://cache.nixos.org'...
building '/nix/store/3lw0vkz9nixhpwm6v1dgp3g4fnsvnf2x-graph.drv'...
```

The node then printed `Hello, world!`. **So a planner may name a
pre-instantiated `.drv` in a node that it writes, and the outer store resolves
it.**

Two details of that log matter:

- **The outer store substituted `hello` and did not build it.** A menu entry
  keeps its identity, so the binary cache serves it. A nixpkgs package that a
  planner selects costs a download and not a compile.
- **`fill_in_output_paths` succeeded over an input `.drv` that the planner did
  not write.** The output path of the node depends on the hash modulo of every
  input derivation, and the planner obtained that for `hello.drv` without any
  refused operation.

Nix warns once during the realisation, and the warning is harmless here:

```
warning: Ignoring dynamic derivation …-graph.drv.drv^out while querying missing paths; not yet implemented
```

## Python packaging, measured

**A graph whose nodes the planner chose, and whose shape it decided, builds and
runs.** `ddrn/examples/venv-graph` resolved 21 artefacts down to 4 nodes, 3 of
them members of the environment:

```text
plan: certifi: certifi-2024.8.30-py3-none-any.whl (wheel)
plan: charset-normalizer: charset_normalizer-3.4.4-cp314-cp314-manylinux2014_x86_64…whl (wheel)
plan: colorama-0.4.6-py2.py3-none-any.whl: marker excludes this target
plan:   backend flit_core-4.0.2-py3-none-any.whl
plan: idna: idna-3.18.tar.gz (sdist)
```

**One generated derivation feeds another, and the second one could not exist
before the plan ran.** `nix derivation show` of the `idna` node:

```text
node: idna-3.18
  inputDrvs:
    fr6sp6rp…-idna-3.18.tar.gz.drv
    hlr53jdq…-flit-core-4.0.2.drv
  backendPath: /1pf0x0b8…/lib/python3.14/site-packages
  backend: flit_core.buildapi
```

`backendPath` is a downstream placeholder, because the backend node floats.
The planner read `flit_core` from the `pyproject.toml` requirement recorded in
the lock file, resolved that name against the same lock file, and made the
wheel of it a node.

**A PEP 517 backend builds offline with nothing but itself on `PYTHONPATH`.**
Measured outside Nix first, and then inside the sandbox:
`flit_core.buildapi.build_wheel` over the `idna` sdist gives
`idna-3.18-py3-none-any.whl`, with no network and no index.

**The environment runs.** `idna.encode("ドメイン.テスト")` gives
`xn--eckwd4c7c.xn--zckzah`, from a package that the graph built from source.

**A change to the planner rebuilds the planner and nothing else.** A later edit
to `plan.py` changed the store path of the planner, so the planner ran again.
It submitted the same root, `x6bjdmvp…-demo-venv.drv`, and the environment
stayed at `9jsa78yq…-demo-venv`. The graph is a function of the decisions, and
not of the source that made them, so content addressing stops the rebuild at
the planner. This is the property that the single-derivation form cannot have:
there, one install step holds every wheel, and any change rebuilds all of
them.

### Bytecode, and why a node was not reproducible

**A floating output is a fixed point only when the build is reproducible, and
this graph was not.** The environment moved from `rwl64yg3…-demo-venv` to
`xvzhwvp…-demo-venv` across two runs that changed no build step. Four of the
five members took a new path, and each of the four is installed by a node that
runs `installer`.

The difference inside a member was the bytecode alone. The header of one
`.pyc` gives the reason:

```text
pc4d3y9y…-certifi: flags=0 (timestamp-based) field1=1786350115 field2=4426
naf1lw9a…-certifi: flags=0 (timestamp-based) field1=1786355678 field2=4426
```

The two builds are 5563 seconds apart, and the recorded field is exactly that
far apart. **The field is a timestamp, and it is the time of the build.**

`installer` compiles bytecode. `destinations.py:261` imports `compileall`, and
line 267 calls `compileall.compile_file` for each level of
`bytecode_optimization_levels`. That call takes the default invalidation mode
of `py_compile`, and `_get_default_invalidation_mode`
(`py_compile.py:72` of CPython 3.14) reads one variable:

The fence says `text`, because the block is CPython as CPython writes it. A
`python` fence makes `ruff format` restyle the quote, and a restyled quote is
no longer evidence.

```text
def _get_default_invalidation_mode():
    if os.environ.get('SOURCE_DATE_EPOCH'):
        return PycInvalidationMode.CHECKED_HASH
    else:
        return PycInvalidationMode.TIMESTAMP
```

A node of this graph is a plain `derivation` and runs no `stdenv`, so nothing
set that variable. `graph.nix` now sets `SOURCE_DATE_EPOCH = "315532800"` in
`mk`, which is the value that `stdenv` of nixpkgs uses, and the mode becomes
`CHECKED_HASH`, which records a hash of the source instead of its mtime.

**Proved by a second store.** A build from scratch in a store that this run
created, with nothing shared but the binary cache, gives
`rfy15b0q3y3nmrld2wbw3j9arvjrf4wl-demo-venv`, which is what the first store
gives.

The trap generalises: **a graph of floating outputs turns one irreproducible
node into a rebuild of everything above it**, and the symptom looks like a
change that the author made. This one was mistaken for a refactor at first.

### Installing a wheel, and what `unzip` does not do

**`unzip` is not an install.** The difference is visible from inside the
environment: `importlib.metadata.distributions()` finds a package only when an
installer wrote its `.dist-info`, with `RECORD` and `METADATA`. A console script
exists only when an installer read `entry_points.txt` and wrote it.

**This lab installs with `pypa/installer`, which is the cross path of
`pyproject.nix` and not its default path.** The default hook of
`pyproject.nix` runs `uv pip install`
(`build/hooks/pyproject-install-hook.sh`), and `pyprojectPypaInstallHook`
replaces it only when the build is cross. `ddrn/PACKAGING-FACTS.md` gives the
two comments that say so. This lab takes the cross path on purpose:
`installer` is pure Python and is one wheel of the same lock file, so it
becomes a node of the graph, and `uv` is a compiled program that a node would
have to take from outside the plan.

**The installer is a node of the graph, and it bootstraps by unpacking.**
Nothing can install the installer, so the planner gives that one artefact a
node that unzips it. Every other node then installs with it. This is the
bootstrap that `pip` performs, expressed as one node.

**A store path is not a wheel file name.** `installer` reads the name of the
file to learn which `.dist-info` the wheel must carry, and a store path puts a
hash in front of that name:

```text
installer.sources._WheelFileBadDistInfo: Wheel .dist-info directory doesn't
match wheel filename (filename='/nix/store/m5r5inpx…-certifi-2024.8.30-py3-none-any.whl',
dist_info='certifi-2024.8.30.dist-info')
```

So the wheel goes under its own name first. nixpkgs and `pyproject.nix` install
from a `dist` directory for the same reason.

**A console script needs its shebang rewritten.** The shebang that `installer`
writes names the interpreter that ran the install, which is the one in the
store, and that interpreter does not see the environment. `pyproject.nix` calls
the fix `write_bin`. Match the shebang on the *name* of the interpreter, and
not on its whole path: `installer` takes the shebang from `sys.executable`, and
CPython reports that after it resolves the symlink, so an install run through
`python3` gives a shebang that names `python3.14`.

**`venv.EnvBuilder` gives a real environment.** The result has `pyvenv.cfg`,
`bin/python` and `bin/activate`, and `sys.prefix` is the environment, so an
import needs no `PYTHONPATH`. Measured from inside:

```text
interpreter  /nix/store/w9pzpvlp…-demo-venv/bin/python
prefix       /nix/store/w9pzpvlp…-demo-venv
entry points ['idna=idna.cli:main']
```

**Bytecode makes an installed package self-referential.** `installer` compiles
`.pyc` by default, and a `.pyc` embeds the absolute path of its source, which
is inside the output. Measured on `certifi`: 6 files hold the hash of their own
store path, and `path-info` records the path in its own `references`. A
floating content-addressed output takes this in its stride, because Nix
computes the hash modulo the self-reference.

**Unresolved, and it belongs to the harness and not to the graph.**
`nix copy --from <chroot store>` of such a path into the daemon of this
machine, which runs Nix 2.34, fails:

```text
error: ca hash mismatch importing path '/nix/store/2h0bgg8v…-certifi-2024.8.30';
         specified: sha256:1bq7qm90rqvml05gzii2ic6w9kyq8knjmnmasd188v1iwasl68yz
         got:       sha256:0dnahkz7iaic6q5bpr7dixwsj76yr83g056d8hjcjphydjv3c4hz
```

The path has a self-reference, so the two sides have to agree on the hash
modulo that reference. **This is not root-caused.** The graph itself is not
affected: `ddrn/examples/venv-graph/check.nix` runs the environment inside the
store that holds it, and it passes.

**A check of a store output runs as a derivation.** Every symlink that
`make-venv.py` writes is absolute, as every store path is, and a chroot store
puts the store under a prefix. Those symlinks resolve from inside a build of
that store, and not from a shell outside it. `check.nix` interpolates
`builtins.outputOf planner.outPath "out"` into an ordinary derivation, which is
also the consumer side of the whole feature.

### A binary wheel, and the loader that it names

**A manylinux wheel names a loader that this system does not have.** Measured
on `ninja-1.13.0-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`,
which ships an ELF executable at `ninja-1.13.0.data/scripts/ninja`:

```text
interpreter: /lib64/ld-linux-x86-64.so.2
needed:      libstdc++.so.6 libm.so.6 libgcc_s.so.1 libpthread.so.0 libc.so.6
rpath:       ''
```

Running that file gives `Could not start dynamically linked executable`, and
neither `libstdc++.so.6` nor `libgcc_s.so.1` is on the default search path of
this system.

**`auto-patchelf` of nixpkgs corrects both, and a graph node calls it
directly.** `pyproject.nix` puts `autoPatchelfHook` in `nativeBuildInputs`
(`build/hacks/default.nix`), but a node of this graph is a plain `derivation`
and runs no setup hook. `pkgs.auto-patchelf` is the program that the hook
calls. It needs `patchelf` on the path, and it needs `NIX_BINTOOLS` to name a
bintools wrapper: it reads the loader from `nix-support/dynamic-linker` and the
libc from `nix-support/orig-libc`. The same file, after the node installed it:

```text
interpreter: /nix/store/l8si8gnv…-glibc-2.42-67/lib/ld-linux-x86-64.so.2
rpath:       /nix/store/0iv8glcs…-gcc-15.3.0-lib/lib:/nix/store/kw26hfd5…-gcc-15.3.0-libgcc/lib
```

**A shared library of a wheel often needs no patch, and an executable always
does.** The two extension modules of `charset_normalizer` need `libc.so.6` and
`libpthread.so.0` and nothing else, and the interpreter that loads them has
already loaded both, so they work with no RPATH at all. The failure appears
when the wheel carries an executable, or a library that needs more than libc.

**The tag of a wheel says which node needs this.** A wheel tagged `any` carries
no compiled code, so the planner marks only the other ones. Measured: the node
of `certifi` references itself and nothing else, and the node of `ninja`
references `gcc-15.3.0-lib`, `gcc-15.3.0-libgcc` and `glibc`. Every one of
those three is already in the closure of `python3`, so the environment grew by
0.4 MiB, which is the size of `ninja`.

### An editable install, and how it survives the store

**A PEP 660 wheel carries a path, and not code.** `flit_core` writes one file,
`myapp.pth`, whose whole content is the directory that the backend ran in.
Under Nix that directory is a copy of the project inside the store, which is
read only. So the install has to be redirected after it is made.

**`os.path.expandvars` is what makes the redirection outlive the build.**
`pyproject.nix` wraps every replaced path in it (`build/hooks/editable_hook`,
`patch_editable.py`), and the reason is a store path: a literal root is part of
what the node builds, so a different root gives a different environment. A
reference to a variable is the same bytes for every tree. Measured, from the
node output:

```text
import sys; import os.path; sys.path.append(os.path.expandvars('$DDRN_EDITABLE_ROOT/src'))
```

`ddrn/examples/venv-graph/run.sh` builds two check derivations that name two
different trees, and both read `rwl64yg3…-demo-venv`. One environment, two
sources, no rebuild.

**A `.pth` line that is a bare path cannot expand a variable.** `site` runs a
line that starts with `import`, and appends any other line to `sys.path`
verbatim. So the rewrite has to turn the path into code, which is what
`patch_pth` of `pyproject.nix` does and what this lab copies.

**A backend that writes an import finder needs more than a text rewrite.**
setuptools puts the build path inside a string literal of a generated `.py`
file. `pyproject.nix` rewrites that literal as a concrete syntax tree, with
`libcst`, so that it changes the literal and nothing else. This lab uses
`flit_core`, which writes the `.pth` form only, and raises rather than pretend
to handle the other form.

**The editable node holds no source.** Measured, the whole output:

```text
bin/myapp
lib/python3.14/site-packages/myapp.pth
lib/python3.14/site-packages/myapp-0.1.0.dist-info/{METADATA,RECORD,WHEEL,entry_points.txt}
```

**A local project declares its backend the way a source distribution does, and
the planner resolves both against the same lock file.** The planner reads
`build-system.requires` from the `pyproject.toml` of the project, parses each
entry as a PEP 508 requirement, normalises the name by PEP 503, and matches the
version by PEP 440. Measured: `myapp` asks for `flit_core >=3.11,<5`, the lock
file calls that package `flit-core`, and the node that the `idna` source
distribution already needed is the node that the editable gets.

## Deriving paths

**A `SingleDerivedPath` names a derivation only when it is the `drvPath` field
of another deriving path.** Every other position resolves an output path,
which may be any store object.

**These call sites resolve a `drvPath` and then read the result as a
derivation.** Measured by conversion, and by the two failures that the missed
ones produced:

- `src/libstore/misc.cc`, both `resolveDerivedPath` overloads
- `src/libstore/build/derivation-trampoline-goal.cc`, lines 83 and 114
- `src/libstore/legacy-ssh-store.cc:359`, `src/libstore/remote-store.cc:688`,
  `src/libstore/restricted-store.cc:345`
- **`src/nix/build.cc:38` and `:57`**, the `toJSON` fallback, which then calls
  `queryPartialDerivationOutputMap` on the result
- **`src/libcmd/installables.cc:725`**, `toDerivations`
- **`src/nix/log.cc:40`**, and **`src/libexpr/primops/context.cc:201`**
  (`builtins.getContext`)

`src/libstore/derivations.cc:400` is **not** one of them. `tryResolve` resolves
an output path there, and the `drvPath` inside that call is already a separate
resolution.

**An indirection that only the goal follows breaks the last four.** Measured on
the `.drvref` branch, against a live store:

```
nix build --dry-run --json  → error: error parsing derivation
                                '/nix/store/…-graph.drvref': store path … is
                                not a valid derivation path
nix derivation show         → {"derivations":{},"version":4}
```

## Resolution, and when a path is known

**A static output path needs no build, and gets no validity check.**
`Store::queryStaticPartialDerivationOutput` (`src/libstore/store-api.cc:458`)
reads the derivation and returns `outputsAndOptPaths(drv)`. An
**input-addressed** output therefore resolves to its path before anything
builds it, and nothing tells the caller that the path holds no store object
yet.

**A floating output resolves to nothing until a realisation exists.**
`deepQueryPartialDerivationOutputImpl` (`src/libstore/outputs-query.cc`)
returns the static result at once when there is one, and only a floating
output falls through to `queryRealisation`. Before the build there is no
realisation, so the result is `std::nullopt`, and `resolveDerivedPath` then
throws `MissingRealisation`.

**The trampoline tolerates exactly one exception.**
`src/libstore/build/derivation-trampoline-goal.cc:83`:

```cpp
try { drvPath = resolveDerivedPath(worker.store, *drvReq); }
catch (MissingRealisation &) { return std::nullopt; }
```

`std::nullopt` means "go and obtain the derivation first", which is the branch
that builds the planner. Any other exception escapes and fails the goal.

**Consequence for a `.drvref` follow inside `resolveDerivedPath`.** The follow
reads a store object, so it must not raise a plain error when that object does
not exist yet:

- With a **content-addressed** planner the follow is never reached too early.
  `MissingRealisation` fires first, the planner builds, and the second
  resolution follows a reference that exists.
- With an **input-addressed** planner the path resolves statically, so the
  follow is the first thing that touches the store, and a plain `InvalidPath`
  escapes the trampoline before it can build the planner. The follow has to
  signal "not yet" the way a missing realisation does.

## Substitution

**A substitution realises every reference first.**
`PathSubstitutionGoal` (`src/libstore/build/substitution-goal.cc:129`) makes a
substitution goal for each reference before it fetches the path, and it then
checks that each reference is valid (line 184). So a `.drvref` store object
that records the derivation as a reference brings that derivation with it
whenever it is substituted. The closure invariant does this, and no new code
is needed.

**`queryMissing` skips a dynamic derivation.** `Store::queryMissing`
(`src/libstore/misc.cc:197`) handles a `DerivedPath::Built` only when its
`drvPath` is `Opaque`, and otherwise warns "Ignoring dynamic derivation %s
while querying missing paths; not yet implemented". So `nix build --dry-run`
cannot say what a dynamic derivation will build. This is upstream behaviour
today, and it affects every design in this lab equally.

## Traps

**`nix derivation show` wraps its result.** The shape is
`{"derivations": {"<base name>": {...}}, "version": 4}`. A `jq` filter of
`.[].inputs.drvs` finds nothing; the filter is `.derivations[].inputs.drvs`.

**`inputs.drvs` is keyed by the base name of the store path**, and not by the
whole path. Schema `derivation-v4.yaml`.

**A store path sorts by its hash.** `sort` over a list of store paths gives an
order that has nothing to do with the names. Strip the hash first, or assert
without an order. This cost two test failures in one session.

**`nix develop --command` does not define the stdenv phase functions.** It
exports `$stdenv` and `$mesonFlags`, so `configurePhase` is not a command.
Drive meson and ninja directly. Entry costs about 12 seconds, for the flake
and for `--file shell.nix` alike, so batch the work into one invocation.

**`meson test` rebuilds before it runs.** A command of the form
`ninja -C build && meson test -C build` therefore prints `ninja: no work to
do` from the second build, after the first one did the work. Do not read that
line as proof that a source edit went uncompiled. Compare the mtime of the
object file with the mtime of the source.

## Purity, and which half of a planner can have it

**`builtins.storePath` is refused in pure evaluation mode.** Measured, against
an ordinary Nix:

```text
$ nix eval --expr 'builtins.storePath "/nix/store/di26b1kk…-coreutils-9.11"'
error: 'builtins.storePath' is not allowed in pure evaluation mode
```

`builtins.appendContext` is in the same class, and a planner needs both. So
the two halves of this example do not have the same purity, and they cannot:

- **The outer entrypoint can be pure.** Nothing about instantiating the
  planner needs an impure primop, once the tools arrive as derivations rather
  than as path strings. `default.nix` of this example takes `nanopynixEnv` as
  a string today, and `builtins.storePath` on that string is the one thing
  that forces `--impure`.
- **The evaluator inside the sandbox cannot be pure.** A planner receives
  store paths as strings, and `storePath` and `appendContext` are the primops
  that turn a string back into a dependency. There is no pure spelling of
  that.

**The classification is too coarse for a builder, and this is an argument for
upstream.** `storePath` is impure because it can name any path of the host
store. Inside a `builder-rpc-v0` sandbox it cannot: `RestrictedBuilder::
ensurePath` asserts that the path is in the input closure of this build. So
the primop is bounded there by construction, and the reason for the pure-mode
refusal does not apply.

**`builtins.fetchGit` with a commit stays pure, and it still fetches during
the evaluation.** The two properties are separate, and `uv.nix` meets both:
a commit pins the content, so pure evaluation mode accepts the call, and the
call still reaches the network before the evaluation can go on. That is the
opposite of `pkgs.fetchurl`, which is a fixed-output derivation: instantiating
one costs nothing and downloads nothing.

The difference is what the caller knows. `fetchurl` gets a hash of the output,
so the evaluator writes a derivation and the build downloads it later.
`fetchGit` gets a commit, which is not a hash of a NAR, so the evaluator
cannot write that derivation and fetches instead. `ddrn/PACKAGING-FACTS.md`
records what that costs a lock file that holds a git source.

## A planner cannot read what a planner wrote

**A store path that the builder adds is not visible in that builder's own
sandbox.** The write succeeds, the submit succeeds, and the file is absent
from the filesystem that the builder sees. A probe measured it directly:

```text
==> wrote /nix/store/3lw0vkz9nixhpwm6v1dgp3g4fnsvnf2x-graph.drv
==> the .drv it wrote is visible from inside: False
==> and NOT readable: [Errno 2] No such file or directory: '/nix/store/3lw0vkz9…-graph.drv'
==> submitted /nix/store/3lw0vkz9nixhpwm6v1dgp3g4fnsvnf2x-graph.drv
```

**This is why `builtins.outputOf` fails on a derivation that the planner
itself wrote.** `prim_outputOf` (`primops.cc:2527`) calls
`coerceToSingleDerivedPath`, which renders the derived path, and rendering a
`Built` whose `drvPath` is `Opaque` calls `store->readDerivation` to learn
whether the output path is statically known. That read opens the file:

```text
… while calling the 'outputOf' builtin
error: opening file "/nix/store/x226q2i9…-idna-3.18.drv.drv": No such file or directory
```

**The remedy is the one that the menu already uses.** Instantiate the nested
planner *outside*, pass its `.drv` in with
`builtins.unsafeDiscardOutputDependency`, and the file is an input source of
the outer planner and therefore present. `builtins.appendContext` then puts
the build dependency back, and `outputOf` reads a file that is there.
`ddrn/examples/venv-graph/default.nix` does this, and `graph.nix` says why at
the node of kind `sdist`.

**The recorded defect below is one instance of the same rule.**
`writeDerivation` not priming the hash memo matters only because the fallback
is a read, and a planner cannot read.

### A list is a memo, and a function is not

**Nix evaluates each element of a list at most once, and it evaluates the body
of a function again at every call.** So a value that a recursion needs at each
level belongs in a list, and not in a recursive function.

`default.nix` instantiates one planner for each source distribution, and each
of those planners carries the inputs of the level below it. A recursive
function therefore builds the whole level `n` times, once for each planner of
the level above, which costs O(n ^ depth). `builtins.genList` gives the same
values as a list of thunks, and each level is then built once, which costs
O(n * depth).

The measurement is the evaluation of `uv.nix` over `projects/pure-app`, which
holds 5 source distributions, at each value of `sdistPlannerDepth`:

| depth | a recursive function | a `genList` of levels |
| --- | --- | --- |
| 3 | 1.08 s | 1.00 s |
| 5 | 2.83 s | 0.98 s |
| 8 | not measured | 1.24 s |
| 12 | not measured | 1.86 s |

**The change altered no derivation.** The planner is
`0mhmq8vd54jvwvmxzszjjy29nrflmjzh-venv-planner.drv` before it and after it.

The lock files that `ddrn/PACKAGING-FACTS.md` measured hold up to 98 source
distributions, and 98 ^ 3 is 941192. The recursive form did not reach such a
lock file, and this is the change that lets a later project try.

### The bound of the levels, reached and passed

**A depth that nothing reaches is a depth that nothing tests.** The example
built one level for a long time: `demo-tool` is a source distribution, and its
backend is a wheel. So `sdistPlannerDepth = 3` allowed two levels that no run
had ever used, and the `throw` past the bound had never run at all.

`tests/fixtures/default.nix` now holds a chain of four source distributions.
Each link declares the next one as its build backend, and the wheel `link-d`
ends the chain, so the number of planners is the number of links above it. The
two roots of `tests/sealed.nix` differ in nothing but the name:

| root | planners | result |
| --- | --- | --- |
| `link-a` | 3 | the environment builds |
| `link-0` | 4 | `graph.nix` throws |

The failure arrives inside the **third** planner, which is the build of
`link-b`, when that planner evaluates `graph.nix` and asks for a fourth:

```text
link-b> error: link_c-1.0.0.tar.gz needs a planner, and the menu has none.
link-b>        A chain of source distributions ran past `sdistPlannerDepth` of default.nix.
error: Cannot build '/nix/store/…-link-b-1.0.0.drv.drv'. Reason: builder failed with exit code 1.
```

**The outer planner builds either way.** It registers the graph and submits
it, and nothing in that step reads past the first level. So the bound is a
property of the realisation and not of the plan, and a test that builds only
the planner would see nothing.

### Who needs `builder-rpc-v0`, and when

**The feature gates a build, and not a realisation.** To realise a derived
path is to make its outputs valid in the store, and Nix has two routes to
that: substitute, or build. `requiredSystemFeatures` sits on the second route
only.

`DerivationBuildingGoal::tryToBuild` compares the required features with the
features of the store (`derivation-building-goal.cc:373`):

```cpp
auto required = drvOptions.getRequiredSystemFeatures(*drv);
auto & available = worker.store.config.systemFeatures.get();
if (std::ranges::any_of(required, [&](const std::string & f) { return !available.count(f); }))
    wrongStore.missingFeatures = WrongLocalStore::Pair<StringSet>{required, available};
```

The coroutine before it in the same file is `gaveUpOnSubstitution`. So Nix
reaches that comparison only after substitution failed.

**Therefore a consumer that substitutes needs no system feature.** A cache
that holds the realisation of the planner and the output of each node serves
the environment by download, and `systemFeatures` is never read. This is the
ordinary property of Nix: the machine that uses a package does not need what
built it.

**What the consumer does need is the `dynamic-derivations` experimental
feature, at evaluation time.** `builtins.outputOf` is what names the
environment, so no cache removes that requirement. It is a language feature
and not a system feature, and the two are separate things.

`ddrn/examples/venv-graph/run.sh` passes `--system-features` because that
script builds everything itself: it makes a private store that no cache has
ever served, so the local machine is the builder of the planner.

**One report is incomplete for a dynamic derivation, and this is by design.**
`queryMissing` walks the derived path graph before anything runs, and it warns
`Ignoring dynamic derivation %s while querying missing paths; not yet
implemented` (`misc.cc:201`). It cannot do better: the inner derivation is
what the planner produces, so nothing knows the graph until the planner runs.
The warning is about the accounting, and not about the realisation.

### Why the sealed test is a pytest run, and not a Nix check

`ddrn/examples/venv-graph/tests` reaches no index, so it looks like a
candidate for `nix/checks.nix`. It is not one, and three properties of a build
sandbox say why:

1. **A check is a derivation, so the graph would run inside a build.** The
   outer build then needs the `recursive-nix` system feature, and the daemon
   of the machine has to grant it. A CI runner grants what its `nix.conf`
   names, and no expression of this repository can change that.
2. **A build substitutes nothing.** Every path that the sealed graph needs —
   `bash`, `coreutils`, `python3`, the patched Nix and the planner environment
   — would have to be an input of the outer derivation, and the inner store
   would have to be seeded from those inputs.
3. **The inner build needs `builder-rpc-v0` as well.** A planner is a build,
   and the section above gives the comparison that gates it. So the outer
   sandbox has to permit a nested sandbox that permits a second feature.

The pytest run makes each of those a property of the machine that runs it,
where it belongs. `ddrn/examples/venv-graph/tests/pytest.ini` keeps the run
separate, and the `testpaths` of the repository does not name that directory.

### An overlay store works, and the host store cannot be its lower layer

`nanopynix.namespace.OverlayNamespace` mounts an OverlayFS at `/nix/store`
inside the worker, with the host store as the lower layer. The worker then
builds against every path the host already holds and copies nothing. This host
supports it: `probe_namespace_support` reports `supported=True`, and a session
over such a store builds an ordinary derivation and substitutes another one:

```
lower: daemon  uri: local-overlay://
trivial: [('built', True)]
hello:   [('substituted', True)]
```

**The graph of this example does not build there, and the reason is the Nix of
the machine.** Every node of it is a floating content-addressed derivation, and
a build trace of such a node has to reach a store. The host runs Nix 2.34, and
the two ways to name it as the lower layer each fail:

| lower store | what happens |
| --- | --- |
| `daemon` | `the daemon is missing the 'realisation-with-path-not-hash' protocol feature, needed to support content-addressing derivations` |
| `local?read-only=true` | `no such table: BuildTraceV3 (in '/nix/var/nix/db/db.sqlite')`, which the patch below removes |
| `local` | untried: it would write to the store database of the machine |

Both were measured on 2026-08-12, over `check-uv.nix` of `projects/pure-app`.
The planner itself builds in the overlay. The failure arrives when the graph
that the planner submitted registers its first output.

**The second row is one defect, and the message says where.**
`src/libstore/ca-specific-schema.sql` creates `BuildTraceV3`, and
`src/libstore/local-store.cc:345` applies it under
`if (!config->readOnly && upgradeDBSchema(...))`. So a read-only store never
gets the table. `local-store.cc:378` then prepares four statements that name
that table, under `Xp::CaDerivations` alone. SQLite refuses to prepare a
statement over a table that is absent, so the store cannot open at all.

**The message is about preparation, and not about a write.** `creating
statement` is the text of `SQLiteStmt::create`, which the constructor of
`LocalStore` calls. So `/nix/var/nix/db/db.sqlite` is the database of the store
that failed to open, which is the lower store. An earlier version of this file
read that name as the target of a write, and that reading was wrong:
`LocalOverlayStore::registerDrvOutputUnchecked`
(`src/libstore/local-overlay-store.cc:82`) writes through `LocalStore::` on
itself, which is the state directory of the overlay.

### The patch that opens such a store

`~/Code/ddnix`, change `mktswroz`, `fix(store): open a read-only store whose
database has no build trace table`:

- `LocalStore::State` holds `hasBuildTrace`. The constructor asks
  `sqlite_master` whether `BuildTraceV3` exists, and prepares the four
  statements only when the answer is yes.
- `queryRealisationCore_` answers `std::nullopt` when the flag is false,
  because a store with no table holds no build trace.
- `registerDrvOutputUnchecked` and `deleteBuildTraces` raise an error that
  names the store and says what to do.

`tests/functional/read-only-build-trace.sh` proves it. The test creates a store
with no `ca-derivations`, opens that store with `local?read-only=true` and
`ca-derivations`, and reads a path. **The test was run against the code before
the patch, and it failed with the same message as the probe:**
`no such table: BuildTraceV3`. With the patch it passes. `meson test` of the
`main`, `ca` and `dyn-drv` suites gives 167 Ok and 0 Fail with the patch, and
the three unit suites give 3 Ok and 0 Fail.

### The patch opens the store, and this host still cannot be a lower layer

The patch removes the refusal, and it does not make the store of this machine a
lower layer. Measured on 2026-08-12, with the patched Nix, over `check-uv.nix`
of `projects/pure-app`:

| lower store | what happens now |
| --- | --- |
| `daemon` | the store opens and the graph fails. `capture_logs` gives the reason: `the daemon is missing the 'realisation-with-path-not-hash' protocol feature`. The `InvalidPathError` that the caller sees names `pcre2-10.47`, and that path is valid on the host. **Read the log rather than the exception.** |
| `local?read-only=true` | the store opens, and the view of it is stale |

**A read-only store gives a stale view of a live store, and Nix documents
this.** `read-only = true` opens SQLite with the `immutable` parameter
(`local-store.cc:560`), and an immutable open ignores the write-ahead log. The
warning on the `read-only` setting
(`src/libstore/include/nix/store/local-store.hh:119`) says: "Do not use this
unless the filesystem is read-only", and "can cause incorrect query results".

The measurement, with a `db.sqlite-wal` of 154170432 bytes beside a `db.sqlite`
of 111005696 bytes:

```
uri: local-overlay://
old path (before the last checkpoint): True
new path (after it): False
new path exists on disk: True
```

The "new path" is a `ddrn-planner-env` that the daemon registered minutes
before, and the last checkpoint of the host database was 28 minutes before
that. A copy of that closure into the upper layer fails as well, with
`fchmodat2 "/nix/store/...-nix-util-2.36.0pre-ddnix+2": Operation not
permitted`: the copy tries to add a path that the lower layer already holds as
bytes, and OverlayFS cannot copy up a directory that root owns.

**So the lower store has to be `daemon`, and that needs a newer Nix on the
machine.** `OverlayNamespace` mounts `lowerdir=/nix/store`
(`nanopynix/namespace.py:339`), so the bytes always come from the store of the
machine and the `lower_store` URI only supplies the metadata of those bytes. A
separate store of the lab cannot take that place.

### Three tests of this repository failed against Nix master, and did so before

`pytest tests` inside `nanopynixMaster.shell` gave 2594 passed and 3 failed.
It gives 2587 passed, 0 failed and 6 skipped now, and the default band gives
2583 passed, 0 failed and 10 skipped. These were the three:

| test | what differs |
| --- | --- |
| `test_settings_model_matches_registered_nix_settings` | Nix registers `build-hook-kill-timeout`, and the settings model does not hold it |
| `test_optional_settings_models_match_their_surfaces` | the same setting |
| `test_flake_metadata_matches_nix[local]` | `fingerprint`: pynix computes `b7a528f2…` and `nix flake metadata` gives `d4266f14…` |

**Each one was proved pre-existing, and not proved by argument.** The build of
`nanopynixMaster.nanopynix-bindings` was repeated from the commit below the
patch of `~/Code/ddnix`, which gives
`/nix/store/5a1j3c06jshc16rzcks5dril4b6nf98n-python3.14-nanopynix-bindings`
rather than `qb5sbdwqiy3dxhav6lc9n1sm6fmpc76w`. The same three tests fail there.

`jj file annotate` gives the origin of the first two: an upstream author added
`build-hook-kill-timeout` to `worker-settings.hh` on 2026-07-28, and the pin of
this repository carries that commit. The default band runs Nix 2.34, which has
no such setting, so `pytest tests` passes there. The settings model takes the
field with `since(NIX_2_36)`, and `field_is_supported` then keeps it out of the
comparison on every older band.

#### The oracle of a comparison has to be the Nix that nanopynix links

The third one is not a defect of pynix. `nix/shell.nix` put no `nix` in the
shell, so `PATH` gave the Nix of the machine whichever band the shell belonged
to. In the shell of the master band, before the repair:

```
command -v nix         ->  /run/current-system/sw/bin/nix
nix --version          ->  nix (Nix) 2.34.8
running_nix_version()  ->  2.36.0pre-ddnix+2
```

The flake fingerprint is the first algorithm where those two disagree, and the
string that Nix hashes says exactly how. The string was rebuilt in Python over
a flake with no inputs, and each half of it identified:

| what hashes to it | the string |
| --- | --- |
| pynix, on master | `git:<rev>;;<lock JSON>;hasRevCount;lastModified=<n>` |
| `nix flake metadata`, 2.34 | `<rev>;;<lock JSON>;revCount=1;lastModified=<n>` |

So master adds two things that 2.34 has not: the name of the scheme in front
of the fingerprint of the input (`Input::getFingerprint`,
`src/libfetchers/fetchers.cc:130`), and the `;hasRevCount` branch for a lazy
`revCount` (`src/libflake/flake.cc:995`). A git fetch always writes a lazy
`revCount` (`src/libfetchers/git.cc:923`), and `maybeGetIntAttr` resolves a
copy rather than write the value back, so no call order makes it concrete.
Every other key of the two objects is equal, including `locks`, `revision`,
`revCount` and `lastModified`.

**The repair is a shell that carries the `nix` of its own band.**
`nix/shell.nix` takes `nix-cli`, which each Nix-version scope defines beside
the library, so the command and the library come from one source:

```
command -v nix         ->  /nix/store/8kzpn67…-nix-2.36.0pre-ddnix+2/bin/nix
nix --version          ->  nix (Nix) 2.36.0pre-ddnix+2
running_nix_version()  ->  2.36.0pre-ddnix+2
```

The two oracle tests then run on both bands, and `test_flake_metadata_matches_nix`
passes on master: pynix and `nix flake metadata` give one fingerprint.
`pytest tests` gives 2590 passed, 0 failed and 3 skipped on the master band,
and 2583 passed, 0 failed and 10 skipped on the default band.

**The daemon of the machine is still the daemon of the machine.** A command in
that shell which speaks to `/nix/var/nix/daemon-socket` is a newer client than
that daemon. Give a store of your own for work that the protocol of the machine
cannot carry.

`tests/support/nix_oracle.py` holds the rule that made the difference visible,
and it stays: a test asks for the oracle through `require_nix_oracle`, which
skips when the command reports another version and names both versions.

### A nested deriving path names the output of a submitted derivation

`builtins.outputOf p "out"` gives a string that holds a placeholder, so
nothing can evaluate `.drvPath` of it. The deriving path says the same thing,
and Nix reads it:

| what a caller writes | what it names |
| --- | --- |
| `<planner>.drv^out` | the derivation that the planner submitted |
| `<planner>.drv^out^out` | the `out` output of **that** derivation |
| `<planner>.drv^*^*` | refused: `does not have an output named '*'` |

Measured on 2026-08-12, over `tests/sealed.nix`. `build_paths_with_results`
takes the two-level form and reports the failure of the graph behind it. The
`*` form works at the first level only, because `*` asks the derivation for its
outputs and the second level has no derivation to ask until the first one
builds.

`ddrn/examples/venv-graph/tests/toolchain.py` reads the second form in
`build_graph_failing`, which is the only place that realises a graph without a
derivation of its own around it.

### A build that a planner submitted has no log to ask for

`Store.get_build_log(path)` needs a path. A derivation that a planner wrote has
no path that a caller knows, so the log of it cannot be asked for by name.

**The log events of the session carry it instead.** `Session.capture_logs()`
records each event, and `message_without_ansi` is the text. That is what
`--print-build-logs` gave the command line.

Nix trims the log that it puts in a failure message, and `log-lines` decides
how much. The default is 25. Measured over the chain of four planners in
`tests/sealed.nix`: at 25 lines the message stops before the `throw` that names
the cause, and at 1000 lines the whole log arrives.

### A dynamic output carries no `meta`

`builtins.outputOf` gives a string, and a string has no attributes. So every
reader of `meta` refuses the output of a planner. Measured on 2026-08-11,
against the application of `projects/workspace`:

| what a consumer writes | what Nix says |
| --- | --- |
| `nix run --expr 'builtins.outputOf p "out"'` | `error: attribute 'type' does not exist` |
| `nix run --expr '{ type = "app"; program = ...; }'` | `error: attribute '' should have type 'derivation'` |
| `nix run --expr '{ type = "derivation"; outPath = ...; drvPath = p; }'` | `error: path "/0a988..." is not in the Nix store` |
| `nix build --expr 'builtins.outputOf p "out"'` | the environment, as a store path |

The third row is the interesting one. That attribute set gets past
`meta.mainProgram`, and it then fails on the path: `nix run` reads `outPath`
during evaluation, and `builtins.outputOf` gives the deferred placeholder
`/0a988pgzhkniq4ghpd379bqdqdb05hlshkip1bh9xk6h0ap3y5a3` until the graph runs.
No hand-written attribute set can answer this, because no evaluation knows the
path.

**A wrapper derivation is the shape that works.** Its input is the application,
so its own output is deferred as well, and `nix run` still works: `nix run`
realises the installable first and reads the result after. The whole content of
the wrapper is one symbolic link.

```console
$ nix build --expr '(import ./uv.nix { ... application = ["ws-cli"]; }).app'
/nix/store/67b8z8nw8j1jmpm34zk8hh2g64a1b789-ws-cli
$ nix store ls --long /nix/store/67b8...-ws-cli
lrwxrwxrwx 0  -> /nix/store/nm6v51ajg9rcbl8p6k8xjpwx4b3zb515-workspace-app
```

The application closure is 238229120 bytes, and the wrapper closure is
238229296 bytes. So the wrapper costs 176 bytes and one path.

**`lib.getExe` still gives a placeholder**, because it interpolates `outPath`
during evaluation:

```console
$ nix eval --raw --expr 'lib.getExe (import ./uv.nix { ... }).app'
/0g0zqixw2g9wxh1mi0ik0n83nzb4sx5vz0j41i5774c6k6m9a012/bin/ws-cli
```

That placeholder is correct inside another derivation, which is where
`lib.getExe` usually goes. It is not a path that a shell can use.

`ddrn/examples/venv-graph/uv.nix` builds the wrapper, and its `mainProgram`
argument names the program. The build verifies that the application holds that
program, so a member whose console script carries another name fails with a
message that lists what the application does hold.

**The same wrapper removes the two-step build of an environment.** The result
of `uv.nix` is the planner, so a build of that result gives a `.drv` and a
second build gives the graph. The `env` attribute is that wrapper over the
environment, so one build reaches the environment. Measured on 2026-08-12, over
the application of `projects/workspace`:

| the store path | the closure |
| --- | --- |
| `fnw2a8v...-workspace-app-env`, the environment | 238228600 bytes |
| `jqygw70...-workspace-app-env`, the `env` wrapper | 238228776 bytes |

So `env` costs the same 176 bytes and the same one path that `app` costs, and
the two names agree. The whole content of the wrapper is one symbolic link:

```console
$ nix --store "$store" store ls --long /nix/store/jqygw70...-workspace-app-env
lrwxrwxrwx 0  -> /nix/store/fnw2a8vvml2y7fcx2cnzzn2hs94lyjnk-workspace-app-env
```

**A development shell takes that wrapper, and the symbolic link is no
obstacle.** stdenv asks `-d "$pkg/bin"`, which follows a link, so `bin` of the
environment reaches the PATH. `nix print-dev-env` is the way to read the answer
without a terminal, and it also builds the graph. Measured on 2026-08-12, over
`projects/packaged-app` in the private store of the tests:

```console
$ nix print-dev-env --store "$store" --file ./uv.nix shell \
    --arg project "$PWD/projects/packaged-app" --impure ...
DDRN_EDITABLE_ROOT='/home/.../ddrn/examples/venv-graph/projects/packaged-app'
export DDRN_EDITABLE_ROOT
... PATH=/nix/store/gyrrfcs...-packaged-app/bin:...
$ nix --store "$store" store ls --long /nix/store/gyrrfcs...-packaged-app
lrwxrwxrwx 0  -> /nix/store/a851fa0dqrjd1bshd2x6v02ck3mmr1n1-packaged-app
```

`DDRN_EDITABLE_ROOT` names the tree of the machine, so an editable member of
the environment reads what the user edits.

**Two planners reach one environment, and the second one costs a planner.**
`app` and `env` of one call are two planners: the second is the same call with
`application = null` and the `-env` suffix. Every node that the two submit is
content-addressing and identical, so the environment is one store path.
Measured on 2026-08-12, over `projects/workspace` with a `venvName` that no
store had held:

| the command | wall clock |
| --- | --- |
| `nix build ... app` | 7.221 s |
| `nix build ... env`, after that | 4.234 s |
| `nix build ... env`, again | 0.611 s |

The 4.234 s is the second planner and the trampoline, and the environment is
not part of it. `bin/ws-cli` of the application points at
`/nix/store/3cghs9ya4kdwzl05yz8rb39zsrwxawg9-speed-app-env`, and the `env`
wrapper of the same call points at that same path.

### nanopynix builds the whole graph, with no `nix` command

The tests drive the example through the patched `nix` command. That command is
not necessary. A nanopynix session that the patched Nix links builds the same
graph in process, and it builds the dynamic derivation as well.

The measurement, on 2026-08-11:

```python
async with Session(
    store_uri="/tmp/ddrn-venv-graph-uv/store",
    experimental_features=["nix-command", "flakes", "ca-derivations", "dynamic-derivations"],
    settings=NixSettings(system_features=["builder-rpc-v0", "recursive-nix"]),
) as session:
    async with session.store() as store:
        async with session.eval(store, eval_settings=NixEvalSettings(pure_eval=False)) as ev:
            root = await ev.string(EXPR)  # `import ./check-workspace.nix { ... }`
            drv = await root.attr("drvPath").as_string()
            await store.build_paths_with_results([f"{drv}^out"])
```

The first run reported `status='built'`, and the second run reported
`status='resolves-to-already-valid'`. So the first run ran the planner, the
planner submitted the graph over the restricted socket, and the trampoline
realised the root. Each of the three is code of `libnixstore`, and no part of
it belongs to the command line program.

Four facts make the session equal to the command line:

| what the command gives | what the session gives |
| --- | --- |
| `--store /path` | `Session(store_uri="/path")`, which reaches `openStore` |
| `--extra-experimental-features` | `Session(experimental_features=[...])` |
| `--system-features` | `NixSettings(system_features=[...])` |
| `--impure` | `NixEvalSettings(pure_eval=False)` |

**The client has to be the patched build, because the worker is the client.**
`nanopynix/src/nanopynix/rpc/_worker_argv.py:70` builds the argument vector as
`[sys.executable, "-m", WORKER_MODULE]`, and the docstring gives the reason:
the two ends cannot be different builds, which is also why `InitRequest`
carries no version field. `Session` offers no interpreter argument, so a client
of the development shell cannot drive a patched worker. The process that
evaluates has to run the interpreter of `nanopynixMaster`.

**The development shell of that scope is that interpreter, and it is
editable.** `default.nix:502` defines `shell` inside the scope, so
`nanopynixMaster.shell` is the same development shell built against the patched
Nix. It carries pytest, ruff and pyright, and its nanopynix is an editable
install of this checkout:

```console
$ nix develop --impure --file . nanopynixMaster.shell \
    --command pytest ddrn/examples/venv-graph/tests
335 passed
```

**`nix develop` sets `TMPDIR` to a fresh directory for each invocation.**
Measured: the harness read `TMPDIR` to place its work directory, so every run
under `nix develop` took an empty store, copied the environment again, and
built every graph from nothing. It cost 1.6 GB of `/tmp` for each run, and the
run took 221 s rather than 60 s. `work_directory` reads `DDRN_WORK` and a
stable default now, and no `TMPDIR`.

**One session is 2.4 times faster than the command line.** Measured over the
same 335 tests, with a warm store:

| harness | time |
| --- | --- |
| `nix build` for each step, one process for each | 60.1 s |
| one RPC session, and no `nix` command | 25.3 s, 25.7 s |

The saving is the process for each build, and the `nix store cat` for each
report: a chroot store is a plain directory tree, so a test opens the file.

### A chroot store takes the daemon as a substituter, so no `nix copy` seeds it

`run-uv.sh` builds the planner environment against the store of the machine and
then copies it into the private chroot store, because that store starts empty
and nothing has ever served the environment. `--extra-substituters daemon`
removes the copy: the chroot store asks the daemon of the machine for every
path, and the daemon holds the environment because this machine built it.

`--option require-sigs false` goes with it. No cache signed a path that this
machine built, and a chroot store refuses an unsigned path by default. It is
the same reason that the copy passes `--no-check-sigs`.

Measured on 2026-08-12, over `projects/pure-app`, from an empty directory:

```console
$ nix build --store "$PWD/store" --file ./uv.nix env \
    --arg project .../projects/pure-app \
    --impure --no-link --print-out-paths \
    --extra-substituters daemon --option require-sigs false \
    --extra-experimental-features 'nix-command flakes ca-derivations dynamic-derivations' \
    --system-features 'builder-rpc-v0 recursive-nix'
warning: the daemon is missing the 'realisation-with-path-not-hash' protocol feature, needed to support content-addressing derivations
/nix/store/90mx650ayjnhh0ivf92kmjgpbymskkzm-pure-app
```

The store went from empty to 1.3 GB, and the result is the `env` wrapper: one
symbolic link to `/nix/store/2ppg8xs82055lxw8vg22a1zbbvjnmfjc-pure-app`, whose
`bin` holds `python`, `markdown-it` and `pygmentize`.

**The warning is about the substituter, and not about the build.** Nix 2.34 on
this machine cannot serve a realisation of a content-addressing derivation, so
the daemon serves the ordinary paths and the client builds every node of the
graph itself. That is what the private store is for.

## Upstream defects that this lab found

**`SubmitStore::require` has no definition.** An undefined symbol.

**`Store::writeDerivation` does not prime `nix::derivation::hashes`.** The memo
that `pathInputModulo` reads (`src/libstore/derivation/aterm.cc:745`) is
process-global, and a miss calls `store.readInvalidDerivation`, which the
restricted allowlist refuses. A planner avoids the memo by giving the graph
floating content-addressed outputs, so that no output path comes from a hash
modulo the inputs.

**A read-only local store skips the content-addressing migration, and then
cannot open.** `src/libstore/local-store.cc:345` applies the schema under
`if (!config->readOnly && ...)`, and `local-store.cc:378` prepares four
statements that name `BuildTraceV3` under `Xp::CaDerivations` alone. SQLite
refuses to prepare a statement over a table that is absent, so such a store
fails at the constructor, with a message about a table rather than about the
mode. The section "An overlay store works" holds the patch that this lab wrote,
and the functional test that proves it.

None of these is reported upstream.
