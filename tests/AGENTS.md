# Where a test goes

This file maps the directories under `tests/`. Read it before you add a test
module, and choose the directory by what the test *looks at*, not by which
package the change touched.

The repository root `AGENTS.md` holds the rules that apply to all Python in
this repository, and `pytest-agent/SKILL.md` holds the procedure to run pytest
here and to read the results. This file only says where a file belongs.

## The map

`tests/` holds what belongs to the repository and to no single project.
Everything else moved out in issue #130, so that a Nix invocation reads one
project and not the whole checkout.

| directory | holds | scope of one test |
|---|---|---|
| `tests/meta/` | self-checks: the repository examined as text and structure | the repository |
| `tests/gates/` | the static gates of CI, run as tools | one gate |
| `tests/scripts/` | the shell of `scripts/`, run against a real repository | one script |
| `tests/support/` | scanners for the self-checks. **No tests.** | — |

Each project carries its own suite, its own `pytest.ini` and therefore its own
rootdir:

| directory | holds | scope of one test |
|---|---|---|
| `pynix/tests/` | the CLI | one command |
| `pynix/tests/support/` | the store scenario and the shared helpers. **No tests.** | — |
| `pynix-lsp/tests/` | the language server | one editor request |
| `pynix/completions/tests/` | the shell completions of the installed `pynix` | one line a user can type |
| `libpynix/tests/` | the command-line layer | one parser behaviour |
| `test-support/tests/` | the tests of the generic helpers | one helper behaviour |

**`pynix/completions/tests/` is the one suite that the repository run does not
reach, and that is deliberate.** It is not in `testpaths`, and it carries its
own `pytest.ini`. Every test there spawns fish, bash and zsh on a pty and
completes against an *installed* `pynix` -- a store path with `share/` in it.
No job of the CI matrix has a shell to drive or an application to complete, so
a run there would report the sandbox and not the code. `checks.completions`
runs it, and the dev shell carries the three shells so a developer can run it
too:

    pytest pynix/completions/tests

`test_support.shell_pty` drives the shell. `completion-spike` drives the same
three, so the driver is in the project that names no Nix concept.

The shared test layer:

| directory | holds |
|---|---|
| `test-support/src/test_support/` | helpers that name no Nix concept |
| `nanopynix-testing/src/nanopynix_testing/` | the beartype hook that every suite loads first |
| `nanopynix-testing/.../_subprocess_startup/` | `sitecustomize.py` for a spawned interpreter |

The Nix fixtures are huggorm's: `huggorm.testing.environment` and
`huggorm.testing.settings`.

**Every plugin is registered in a `pytest.ini` with `-p`, and never with
`pytest_plugins` in a conftest.** `pytest_plugins` is legal only in a
top-level conftest, so the same file is legal in a run of its own project and
illegal in a run from the repository root. Each `pytest.ini` records this, and
so does the repository's own.

**A helper leaves `tests/support/` when a second project needs it.**
`tests/support/` imports as `tests.support.<name>`, which only the repository
rootdir resolves. What stays there is what only this repository's own suite
reads: the scanners of the meta tests.

**A suite that loses the beartype hook still passes every test, so the loss is
silent.** `pynix/tests/test_beartype_instrumentation.py` notices it. The hook
sets `DEFERIMPORT=eager`, so every name an annotation uses is real.

## The rule for `tests/scripts/`

**It runs the script, which is why it is not in `tests/meta/`.** `scripts/`
holds hand-written shell that belongs to the repository and to no project, so
its tests belong beside the other tests in that position rather than inside a
project's suite.

`check-shell` reads every file there, and `shellcheck` is a gate and not a
test: it says the shell is well formed, and nothing about what the script
decides. `scripts/last-green.sh` declines to move a branch backwards, and no
gate can see that.

A test here needs `git`, a temporary directory and nothing else. No Nix, no
store, no network. Build the remote as a bare repository under `tmp_path` and
push to it, so the test exercises the same commands the workflow runs.

## The rule for a test of the harness itself

**A harness test asserts a thing every other test depends on, and that no
other test would notice was broken.**

Most helpers need no test of their own: the test that uses a helper fails when
the helper breaks. A few do not have that property. The first one was
`hang_report.py`, which runs only when a test has already hit its deadline. If
it returned an empty string, every run would stay green and the report would
be silently useless at the one moment it matters.

Ask whether a bug in the helper makes some other test fail. When the answer is
yes, write no test. When the answer is "no, it just stops helping", the helper
needs its own test, beside the helper: `hang_report.py` and
`subprocess_output.py` are tested from `test-support/tests/`.

## The rule for `tests/gates/`

**A gate test runs a tool that CI already runs, and it never fails the run.**

A meta test reads the repository and finishes in milliseconds, and `ruff` and
`zuban` are subprocesses that take seconds, so they cannot go in
`tests/meta/` without taking that property away from every test in it.

Three things hold for each test here:

- **The gate is a copy, and not the authority.** `nix/checks.nix` builds each
  gate as a derivation, and the `static-checks` job of CI refuses the merge.
  Run the same command, so that a finding here is the finding there.
- **`xfail(strict=False)`, always.** A failing tool must not stop a developer
  from running the suite, and CI already refuses. A clean tool reports `xpass`
  and a failing one reports `xfail`, and the run stays green either way.
- **Skip when the tool is absent.** The packaged runner carries no dev shell
  tool, so `shutil.which` returns `None` there and the test skips. Do not add
  the tool to `nix/tests.nix` to make it run: that job builds the derivation
  instead, and running it twice only costs time.

The order comes from `test_support.plugin`, which puts these items after the
forked tests and before everything else.

## The rule for `tests/meta/`

**A meta test reads the repository. It does not run it.**

A meta test opens source files, parses them, walks module objects or compares
a checked-in generated file against what the generator produces now. It needs
no Nix, no store, no subprocess and no network, and it finishes in
milliseconds.

What lives there now:

| module | asserts |
|---|---|
| `test_suppression_grammar.py` | every lint or type suppression says why it exists |
| `test_agent_note_imports.py` | no test module imports `pytest_agent` directly |
| `test_docs_reference.py` | the checked-in CLI reference matches the live command tree |
| `test_impl_package.py` | `pynix._impl` imports every implementation module there is |
| `test_ansi_filtering.py` | no module writes its own regular expression for an ANSI escape sequence |
| `test_ci_step_policy.py` | a workflow step is one line, and no expression reaches a shell body |
| `test_licensing.py` | every project states Apache-2.0 |
| `test_suite_roster.py` | the roster of suites matches the repository |
| `test_suite_module_names.py` | no two test modules of the repository run share a basename |

## The shape of a meta test

Copy `test_suppression_grammar.py`. Three parts, and the first two are not
decoration:

1. **A "can see it" guard.** Assert that the scanner found source to scan. The
   packaged CI runner `cd`s into a store copy of the repository rather than
   running from the checkout (`nix/tests.nix`), so a scanner pointed at a path
   that copy does not carry would return nothing and the conformance test
   below would pass by checking nothing. That silent no-op is the failure mode
   this whole directory exists to prevent.

2. **Scanner unit tests, in both directions.** Pin that a known-bad shape is
   caught *and* that the shapes the repository legitimately uses are not.

3. **The conformance test.** The assertion itself, with a failure message that
   names the offending file and line and says what to do about it.

Put the scanner in `tests/support/` and the test in `tests/meta/`. Reuse
`iter_python_files` from `suppressions.py` rather than writing another tree
walk. Read the structure, not the text: walk the AST, because a regular
expression cannot tell an import from the same words in a docstring.

## Derived against declared

A test that compares a set derived from the tree against a literal restated in
the test file makes a new member fail the suite until a person decides whether
it belongs. Write the reason in the literal, next to the entry. Use a literal
only when the answer is a judgement, and key it on the unit that the judgement
covers.

A ledger must also be able to get shorter. Assert both directions, so an entry
whose subject is gone fails and gets deleted, rather than sitting there as a
rubber stamp.

## What a meta test must not claim to check

Say so in the module docstring when a nearby rule is *not* machine-checkable,
so that nobody later reads the file as a wider guarantee than it gives.
Examples that stay unchecked on purpose: whether prose follows ASD-STE100, and
whether a recorded reason is true. A test can prove that a reason exists. It
cannot prove the reason is honest.
