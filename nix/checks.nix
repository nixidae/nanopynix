# The static gates, as derivations, so CI can fail on them.
#
# Commands that every contributor is asked to keep clean and that nothing
# enforced. `ruff-strict.toml` in particular is a large, considered
# configuration whose whole value is that it reports nothing, and until this
# existed the only thing keeping it at zero was habit.
#
# One derivation each rather than one that runs all of them, so a failing run
# names the gate that failed, and so the cheap ones still report when pyright
# is the slow one.
#
# `pytest-agent` is a gate here that runs tests rather than a static tool,
# because nothing else would run them. It carries its own pytest
# configuration, and the repository's own test runner (nix/tests.nix)
# collects what `testpaths` in the repository `pytest.ini` names, so a suite
# that nobody names runs nowhere. It is the plugin that every other suite
# here reports through, so a defect in it reaches every job.
{
  lib,
  runCommand,
  ruff,
  pyright,
  shellcheck,
  pythonSet,
  pynix,
  bashInteractive,
  fish,
  zsh,
  ncurses,
  nix,
  man,
  completionSpike,
}:
let
  # Only the trees the gates read. An allowlist, and not the shared denylist
  # of `nix/source.nix` that the test runner and the docs build take: a lint
  # gate must not report on a file that is gitignored and absent under a flake
  # evaluation, and naming each input keeps an unrelated edit from rebuilding
  # all four gates.
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      # `pyproject.toml` carries the pyright configuration, and `ruff.toml`
      # carries the default ruff one. Omitting `ruff.toml` does not fail the
      # build -- ruff silently falls back to its own defaults, which select
      # fewer rules and assume an older Python. Measured: the first build of
      # this file reported three `F821 Undefined name BaseExceptionGroup`,
      # because the fallback target is older than the `py313` that
      # `ruff.toml` sets.
      ../pyproject.toml
      ../ruff.toml
      ../ruff-strict.toml
      # The CLI layer, which issue #222 moved out of `../pynix`. Without it
      # `check-lint`, `check-format` and `check-types` read a smaller tree
      # than `ruff check .` in the dev shell reads.
      ../libpynix
      ../pynix
      # The language server, which issue #107 moved out of `../pynix`. Without
      # it `check-lint`, `check-format` and `check-types` read a smaller tree
      # than `ruff check .` in the dev shell reads.
      ../pynix-lsp
      ../completion-spike
      ../pytest-agent
      ../test-support
      ../nanopynix-testing
      ../tests
      ../tools
      ../docs
      ../ci
      # `scripts/` holds hand-written shell, which no gate read until
      # `check-shell` below. `writeShellApplication` shellchecks only the
      # scripts that it generates, and none of these is one of those.
      ../scripts
      # The renderer of the completion scripts. `nix/mk-app.nix` runs it inside
      # each application build, and `checks.completions` runs it as well: the
      # dev-shell branch of its `scripts` fixture renders the three scripts
      # rather than reading them out of a store path, and
      # `test_the_renderer_writes_where_it_is_told` states the arguments it
      # takes. Absent from this list the file is not in the source, and Python
      # answers a missing script with exit status 2 and no message at all.
      #
      # It also brings the file into `check-lint` and `check-format`.
      ../nix/render-completions.py
      # The renderer of the manual pages. `nix/mk-app.nix` runs it inside
      # each application build that asks for pages, with the same reason and
      # the same two gates as the entry above it.
      ../nix/render-manpages.py
      # Tracked, and therefore in scope, although it holds one module and no
      # project of its own. With it the gate reads 259 Python files, which is
      # what `ruff format --check .` reads in the dev shell. Without it the
      # gate reads fewer, and a hole in a gate is worse than no gate.
      ../tmp
    ];
  };

  # Not editable, and the same reasoning as the test runner: an editable
  # install bakes in an absolute path outside the store, which is exactly what
  # a sandbox does not have. pyright reads first-party code through the
  # `extraPaths` in pyproject.toml, so what this env is really for is the
  # third-party stubs.
  #
  # The spec matches the dev shell's, `docs` extra included, because
  # `extraPaths` lists `docs` and `docs/conf.py` imports sphinx.
  pythonEnv = pythonSet.mkVirtualEnv "nanopynix-check-env" {
    # pyright reads `libpynix/src` and `libpynix/tests`, and that suite
    # imports `argcomplete` and `pytest` directly.
    libpynix = [ "test" ];
    pynix = [
      "test"
      "docs"
    ];
    # The language server, which issue #107 moved out of `pynix`. pyright
    # reads `pynix-lsp/src` and `pynix-lsp/tests`, so the env has to carry
    # `pygls`, `lsprotocol`, `jsonschema` and `pytest-lsp`.
    pynix-lsp = [ "test" ];
    pytest-agent = [ ];
    # `huggorm_bindings-stubs`, the engine's PEP 561 stubs. They are
    # `partial`, so pyright reads `huggorm_bindings` beside them for what no
    # stub describes, such as the words of `words.py`.
    huggorm-generated = [ ];
    test-support = [ "test" ];
    nanopynix-testing = [ ];
    # `completion-spike` itself is not a package of this set -- it is a nixpkgs
    # `buildPythonApplication`. Its two dependencies are named directly, so
    # that pyright can read its source and its tests. Without them pyright
    # reported 56 errors on that tree, every one of them an unknown type
    # reached through `cyclopts` or `pexpect`.
    cyclopts = [ ];
    pexpect = [ ];
  };

  # A second venv, holding this one project. This one must also be an installed
  # `pytest-agent` rather than a `pythonpath` entry: the suite asks
  # `plugin_registered_via_entry_points()` to decide whether to pass `-p
  # pytest_agent.plugin` to each inner pytest, and that question only has the
  # right answer when the distribution metadata is really there.
  pytestAgentEnv = pythonSet.mkVirtualEnv "pytest-agent-test-env" {
    pytest-agent = [ "test" ];
  };

  # And a third. `test-support` holds the helpers that every suite here shares
  # and that name no Nix concept, so its own suite must not reach anything that
  # does: a venv with huggorm in it would let a helper grow a dependency on
  # the engine and nothing would report it. That is the rule this project
  # exists to keep, so the venv is what enforces it.
  testSupportEnv = pythonSet.mkVirtualEnv "test-support-test-env" {
    test-support = [ "test" ];
  };

  # And a fourth. `libpynix` is the command-line layer that issue #222 moved
  # out of `pynix`. A venv holding this project alone is what proves it stands
  # up without `pynix`, and without any part of Nix: the whole reason it is a
  # separate project is that a consumer should be able to take the parser
  # without the evaluator.
  libpynixEnv = pythonSet.mkVirtualEnv "libpynix-test-env" {
    libpynix = [ "test" ];
  };

  # And a fifth. `pynix` and nothing else, which is what the `pynix-isolated`
  # gate below asks a question of. The dev shell carries the language server
  # on purpose, so no environment that a person works in can answer it.
  pynixOnlyEnv = pythonSet.mkVirtualEnv "pynix-only-env" {
    pynix = [ ];
  };

  # And an eighth, for the completion suite of `pynix`. `test-support` carries
  # `shell_pty`, which drives a shell on a pty, and its `test` extra carries
  # pytest. **`pynix` is deliberately not in it**: that suite completes against
  # the *installed* application, which the gate names by its store path, and a
  # second `pynix` on the search path would make the answer ambiguous.
  #
  # `libpynix` is in it for `argcomplete` alone. `nix/render-completions.py`
  # imports `argcomplete.shell_integration`, and
  # `test_the_renderer_writes_where_it_is_told` runs that script with this
  # interpreter. `libpynix` names `argcomplete` and names nothing else, and it
  # is not a second `pynix`, so the ambiguity above stays avoided.
  completionsEnv = pythonSet.mkVirtualEnv "pynix-completions-test-env" {
    test-support = [ "test" ];
    libpynix = [ ];
  };

  mkCheck =
    name: nativeBuildInputs: command:
    runCommand "nanopynix-check-${name}" { inherit nativeBuildInputs; } ''
      # Both tools want somewhere to write a cache, and the source is a
      # read-only store path.
      export HOME="$TMPDIR"
      export RUFF_CACHE_DIR="$TMPDIR/ruff"
      cd ${source}
      ${command}
      touch "$out"
    '';
in
{
  lint = mkCheck "lint" [ ruff ] "ruff check --no-cache .";

  # The configuration that AGENTS.md says to keep at zero findings. This is
  # the sentence that makes that true.
  lint-strict = mkCheck "lint-strict" [ ruff ] "ruff check --no-cache --config ruff-strict.toml .";

  # `ruff format --check`, never `treefmt`. treefmt writes. Its exclusion of
  # the LSP fixture tree also belongs to the nix formatter alone, so this
  # command covers the same files as treefmt's python formatter, and no more.
  format = mkCheck "format" [ ruff ] "ruff format --no-cache --check .";

  types = mkCheck "types" [
    pyright
    pythonEnv
  ] "pyright --pythonpath ${pythonEnv}/bin/python";

  # `scripts/` was covered by nothing. `writeShellApplication` runs shellcheck
  # over the script it builds, which covers the test runner of
  # nix/tests.nix and no hand-written file, so these three grew without a
  # gate. `-x` follows a `source`, and the scripts are the only shell here.
  shell = mkCheck "shell" [
    shellcheck
  ] "shellcheck -x scripts/*.sh";

  # **`pynix` alone, and the language server must not arrive with it.** Issue
  # #107 split `pynix-lsp` out to take `pygls`, `lsprotocol` and `jsonschema`
  # off the start-up of `pynix build`: 349 of the 966 modules that `import
  # pynix` loaded came from those three, and the split removed 62 of the 966.
  #
  # A test in either suite cannot state this. The dev shell installs both
  # projects, because a developer here runs the server as well as the CLI. So
  # the question "is the server absent" only has an answer inside a venv built
  # for the question, which is what `pynixOnlyEnv` is.
  #
  # **The gate asks two questions, and `jsonschema` is why there are two.**
  # `pygls`, `lsprotocol` and `pynix_lsp` must not be installed at all: no
  # other project here needs them. `jsonschema` may arrive as a dependency of
  # another package, so it can be installed -- what must not happen is
  # `import pynix` loading it. The second
  # question is therefore about `sys.modules` after the import, which is also
  # the cost that issue #107 measured. Issue #123 tracks that cost.
  #
  # There is no third question about a subcommand any more. `pynix` mounted
  # `Lsp` through an optional import until issue #123, so this gate also had to
  # state that the mount did not happen here. `pynix-lsp` is the program an
  # editor calls, and it sits beside `pynix` on the PATH of the dev shell, so
  # the alias is gone and the two questions above are the whole gate.
  #
  # `runCommand` and not `mkCheck`: this gate reads no file of the source
  # tree, and `cd`-ing into one would put a `pynix/` directory on `sys.path`
  # as a namespace portion for no reason.
  pynix-isolated =
    runCommand "nanopynix-check-pynix-isolated" { nativeBuildInputs = [ pynixOnlyEnv ]; }
      ''
        python - <<'EOF'
        import importlib.util
        import sys

        NOT_INSTALLED = ("pygls", "lsprotocol", "pynix_lsp")
        NOT_IMPORTED = (*NOT_INSTALLED, "jsonschema")

        present = [n for n in NOT_INSTALLED if importlib.util.find_spec(n)]
        if present:
            raise SystemExit(f"the venv of pynix must not hold the language server, but it holds: {present}")

        import pynix

        loaded = sorted({m.split(".")[0] for m in sys.modules} & set(NOT_IMPORTED))
        if loaded:
            raise SystemExit(f"`import pynix` must not load these, and it loaded: {loaded}")

        names = {sub.cli_name or sub.__name__ for sub in pynix.Pynix.subcommands}
        print(f"pynix alone: {len(sys.modules)} modules, {len(names)} subcommands, no language server")
        EOF
        touch "$out"
      '';

  # The plugin every other suite in this repository reports through, gated for
  # the first time. See this file's header.
  #
  # `tests`, spelled out, for the same reason as the gate above it.
  #
  # `-p no:cacheprovider` for the read-only store path, and
  # `PYTEST_AGENT_NO_AUTODETECT=1` because the plugin activates itself when it
  # finds an agent-harness variable in the environment. Without it, this suite
  # would run in agent mode when a developer builds the gate from a Claude
  # Code session and in plain mode in CI -- two different runs from one
  # command. The inner sessions that the tests start are unaffected: they get
  # a cleaned environment from the suite's own autouse fixture.
  pytest-agent = mkCheck "pytest-agent" [ pytestAgentEnv ] ''
    export PYTEST_AGENT_NO_AUTODETECT=1
    python -m pytest -p no:cacheprovider pytest-agent/tests
  '';

  # The CLI layer of issue #222. The directory and not `libpynix/tests`: this
  # project carries its own `pytest.ini`, and pointing pytest at the project
  # makes `testpaths` apply.
  libpynix = mkCheck "libpynix" [
    libpynixEnv
  ] "python -m pytest -p no:cacheprovider libpynix";

  # The directory, and not `test-support/tests`. This project carries its own
  # `pytest.ini`, so pointing pytest at the project makes `testpaths` and
  # `anyio_mode` apply. Read that file: without `anyio_mode`, every async test
  # here fails rather than skips.
  #
  # `-p no:cacheprovider` for the read-only store path, as above.
  test-support = mkCheck "test-support" [
    testSupportEnv
  ] "python -m pytest -p no:cacheprovider test-support";

  # The completion spike, whose suite drives fish, bash and zsh on a pty. Named
  # here so that the `static-checks` job builds it: that job takes its list
  # from this attribute set (ci/workflows/lib.nix), and a package that no job
  # names is a suite that never runs.
  #
  # An alias, and not a `mkCheck`. The suite is the check phase of the package
  # itself, so building the package *is* running the gate. See
  # nix/completion-spike.nix for why that shape was chosen.
  # **The shell completions `pynix` installs, driven in the three shells that
  # load them.**
  #
  # Two questions, and `pynix/completions/tests/` holds both. The first is what
  # each installed file *is*: a program asked for a completion script the click
  # way prints its help screen and exits 0, and a file holding an ANSI-coloured
  # help screen sits at a path the shell loads and reports nothing. Issue #105
  # measured exactly that on a sibling program.
  #
  # The second is what each shell then *offers*, for every line a user can
  # type. This gate asked one line, `pynix bu`, and answered it in a shell
  # probe written in this file. Issue #213 measured the other eight: four are
  # wrong, and one of the four puts a command the user did not ask for on the
  # command line. A case table replaces the probe, and each broken row is
  # `xfail(strict=True)` against #105, so the fix turns the row green and the
  # run red until someone removes the mark.
  #
  # `PYNIX_INSTALLED_PREFIX` names the built application, so the suite reads
  # `share/` out of the thing a user installs. Without it the suite renders the
  # same three scripts itself, which is what a run in the dev shell does.
  #
  # `bashInteractive` and not `bash`: the driver spawns an interactive shell
  # and sends `bind`, which the non-interactive build has no readline for.
  # `ncurses` carries the terminfo database, and the driver asks for an `xterm`
  # terminal, because fish draws no candidate list at all on a terminal it
  # believes cannot address the cursor.
  #
  # `SHELL` is deliberately absent from this sandbox. clypi resolved a
  # completion through the user's login shell and raised when it did not know
  # it, so each generated script had to name its own shell; argcomplete asks
  # the shell that is running the script and reads no such variable. Issue
  # #214. Keeping the variable unset here is what would catch a return to
  # anything that needs it.
  completions = mkCheck "completions" [
    completionsEnv
    bashInteractive
    fish
    zsh
    ncurses
    nix
  ] ''
    unset SHELL
    export PYNIX_INSTALLED_PREFIX="${pynix}"
    export TERMINFO_DIRS="${ncurses}/share/terminfo"
    # **`nix` is here as a baseline, and not as a tool.**
    # `tests/test_nix_equivalence.py` asks `nix` what it would offer, through
    # `NIX_GET_COMPLETIONS`, and asserts that `pynix` offers the same. So the
    # expectation is a running program rather than a table this repository
    # wrote, and it moves when Nix moves.
    #
    # Both programs need a store before they can evaluate, and the sandbox has
    # no daemon to talk to. `NIX_REMOTE` points each of them at a local store
    # under the build directory, which is writable and empty.
    export NIX_REMOTE="$TMPDIR/completion-store"
    # `-p no:cacheprovider`, because the source is a read-only store path.
    cd pynix/completions
    pytest . -p no:cacheprovider
  '';

  # **The manual pages `pynix` installs, rendered and read back.**
  #
  # Four questions. First, every command the help lists owns a page: the
  # names come out of `pynix --help`, so a new subcommand without a page
  # fails here rather than shipping silently. Second, every page renders
  # without a warning. Third, no page carries an escape sequence: Python
  # 3.14 themes argparse usage when its color detection passes, and troff
  # cannot use the escapes. Fourth, the root page carries the hand-written
  # text: the NAME line and one EXAMPLES entry, matched literally.
  #
  # The last question this gate answers is the one issue #233 asked. fish
  # describes a command name through `whatis`, so the gate indexes the pages
  # with `mandb` and asks `whatis` for the name. A page whose NAME section
  # mandb cannot parse fails that lookup the way the report did.
  #
  # `installManPage` takes pages uncompressed, and the assembled application
  # keeps them uncompressed. man, mandb and whatis read them as is.
  manpages = mkCheck "manpages" [ man ] ''
    set -eu -o pipefail
    # A pager, a terminal and colors all shape what `man` and `pynix --help`
    # print. The sandbox has no TTY today, so both default sanely today. The
    # pins keep that true when the sandbox changes.
    export PAGER=cat MANPAGER=cat TERM=dumb NO_COLOR=1
    pages="${pynix}/share/man/man1"
    test -f "$pages/pynix.1"
    # Capture first, parse second: an empty file says the program did not
    # start, and empty names say the help layout moved. Both fail loudly.
    "${pynix}/bin/pynix" --help >"$TMPDIR/help.txt"
    test -s "$TMPDIR/help.txt"
    names=$(awk '/^    [a-z0-9-]+  / {print $1}' "$TMPDIR/help.txt")
    test -n "$names"
    for name in $names; do
      test -f "$pages/pynix-$name.1"
    done
    for page in "$pages"/pynix*.1; do
      if grep -q $'\x1b' "$page"; then
        echo "escape sequence in $page"
        exit 1
      fi
      man --local-file --warnings -l "$page" >"$TMPDIR/rendered.txt" 2>"$TMPDIR/rendered.err"
      test ! -s "$TMPDIR/rendered.err"
      test -s "$TMPDIR/rendered.txt"
    done
    man --local-file --warnings -l "$pages/pynix.1" >"$TMPDIR/root.txt" 2>"$TMPDIR/root.err"
    test ! -s "$TMPDIR/root.err"
    grep -q -F "pynix - command-line tool for Nix" "$TMPDIR/root.txt"
    grep -q -F "pynix eval --expr '1 + 1'" "$TMPDIR/root.txt"
    mkdir -p "$TMPDIR/manroot/man1"
    cp "$pages"/pynix*.1 "$TMPDIR/manroot/man1/"
    mandb -q -c "$TMPDIR/manroot" 2>"$TMPDIR/mandb.err"
    test ! -s "$TMPDIR/mandb.err"
    MANPATH="$TMPDIR/manroot" whatis pynix >"$TMPDIR/whatis.txt"
    grep -q -F "command-line tool for Nix" "$TMPDIR/whatis.txt"
  '';

  completion-spike = completionSpike;

  # **No gate for the pynix suite, and that is not an oversight.** Issue #130
  # moved it to `pynix/tests/`, and the two gates above exist because a moved
  # suite would otherwise run nowhere. That suite is different: it drives a
  # real store, a real evaluator and `nix-build`, so it runs in the CI matrix
  # against every supported Nix version. The repository `pytest.ini` names it
  # in `testpaths`, which is what the packaged runner reads. A derivation here
  # would check one version inside a sandbox with no network, so it would be
  # strictly weaker than what already runs.
}
