"""Answer a Tab with the attributes of the file or flake the caller named.

**The baseline is Nix, and it is a real program that can be asked.** ``nix``
answers its own completion through ``NIX_GET_COMPLETIONS=<n>``: it prints the
kind of completion on the first line and then one candidate for each line
after it. So an equivalence between this and that is measurable, and
``pynix/completions/tests/test_nix_equivalence.py`` measures it::

    $ NIX_GET_COMPLETIONS=4 nix build --file ./tmp/autocomplete.nix nixos.
    attrs
    nixos._type
    nixos.class
    nixos.config

Three things in that answer are decisions this module copies.

**A candidate is the whole path, and not the last component.** Nix answers
``nixos.config`` and not ``config``. A shell replaces the word under the
cursor, and the word is the whole dotted path, so a last component alone would
delete what the caller already typed.

**No value is forced.** ``nixos.config.system`` comes back although
``config.system.built.toplevel`` is ``builtins.derivation {}``, which throws
when it is forced. Listing the names of an attribute set forces the set and
not what is in it, and this module asks for nothing else.

**The two spellings agree.** ``--file F --attr a.b`` and ``--file F#a.b`` are
the same selection -- :meth:`pynix.target.EvaluationTarget.selected_attr`
joins the fragment and the option -- so the completion of each has to be the
same set. That is why `complete_file` below completes the part after a ``#``
itself rather than leaving it to the shell.

**The walk is in `huggorm.attrcompletion`, and this module is the
program around it.** That library holds the two rules Nix applies -- one for
``--file`` and one for the fragment of a flake -- because they need an
evaluator and nothing else, and a second Nix CLI that wants them already
depends on it. What is here is what belongs to a program: the store, the
session, the budget, the ``#`` spelling that is ours, and the argcomplete
glue.

**A completion runs while a person holds a key down, so it gives up.** Issue
#223 is the question this answers, and a budget is the answer. Evaluating a
Nix file has no upper bound: it can fetch a flake input, or walk nixpkgs. When
the budget runs out this returns nothing at all, which is what the shell shows
when no program answers. A wrong answer is worse than no answer here, because
the shell writes a candidate onto the command line.
"""

from __future__ import annotations

import contextlib
import functools
import io
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from typing import Any, Literal

from huggorm import AsyncEvalState, AsyncSession, AsyncStore
from huggorm.attrpath import AttrPathSearch

from libpynix.command import Completer

#: Which attribute-path search a command applies to the fragment of ``--flake``.
#:
#: **A name, and not the search itself.** ``pynix.target`` builds each search,
#: and importing it costs 101 ms that a command which only lists an option must
#: not pay. So a command module names the one it wants and
#: :func:`_flake_search` resolves the name inside the completion.
type FlakeSearch = Literal["base", "dev-shell", "repl", "app", "exact"]

#: The scheme that an indirect flake reference carries, and that a caller omits.
_FLAKE_SCHEME = "flake:"

#: The setting whose empty value means "no global registry layer" to Nix.
_NO_GLOBAL_REGISTRY = "flake-registry"

#: What a completion tells `git` about a transfer that goes silent.
#:
#: **`git` reads none of Nix's settings, because it is a separate process.**
#: `_completion_settings` bounds what curl fetches, and a `git+https:` flake
#: input is fetched by `git`, which Nix runs as a child. These two variables
#: are git's own, and Nix passes its environment to that child.
#:
#: Measured against a socket that accepts and never writes, completing a flake
#: whose one input names it: at git's defaults the completion outlasted 120 s
#: and was killed, and with these it answered nothing after 4.619 s and left
#: no `git` process behind. Two seconds and not three, because Nix retries the
#: fetch once, so the completion pays the figure twice and the budget is 5 s.
_GIT_STALL_VARIABLES = {
    "GIT_HTTP_LOW_SPEED_LIMIT": "1",
    "GIT_HTTP_LOW_SPEED_TIME": "2",
}


def _completion_settings() -> dict[str, str]:
    """Nix's own retry settings, cut down to what a keypress can afford.

    **One attempt, and not five.** `download-attempts` defaults to 5, and Nix
    backs off between them. Measured with no network at all: the flake
    registry raised after 4.646 s at the default and after 0.002 s at one
    attempt. The first figure is over the budget, so the completion was
    cancelled where it should have fallen back to the local registry layers --
    which is exactly what a build sandbox reported, three times, as an empty
    answer with no cause.

    **Three seconds to connect, and not fifteen.** One attempt against a host
    that refuses still costs `connect-timeout`, and the default outlasts the
    whole budget. A person holding a key down is not waiting on a slow mirror.

    **Three seconds of silence, and not three hundred.** A host that *accepts*
    the connection and then sends nothing is not covered by the two above:
    the connect succeeded, and there is one attempt in flight. Measured
    against a socket that accepts and never writes: at the default
    `stalled-download-timeout` of 300 s the call outlasted 25 s and was killed,
    and at 3 s it raised in 3.004 s. Issue #231 opened on that shape.

    **It bounds what curl fetches, and not what `git` fetches.** A
    `git+https:` input runs `git` as a separate process, which reads none of
    these settings. Issue #231 keeps that half, and `GIT_HTTP_LOW_SPEED_TIME`
    is the thing to measure for it.

    All three are Nix's own settings, and all three are named here explicitly.
    A `nix.conf` does reach this program since issue #234, and an explicit
    value beats the file, so these three stand however patient the file is. A
    caller who wants the patient values for a real command still gets them,
    because a real command does not come through here.

    **What the file now does reach is every other fetch setting**, which is
    the point of #234 rather than a side effect of it: `flake-registry` is how
    a person turns the downloading layer off, and a value in their `nix.conf`
    used to do nothing here.
    """
    return {
        "download-attempts": "1",
        "connect-timeout": "3",
        "stalled-download-timeout": "3",
    }


#: Seconds a completion may take before it answers with nothing.
#:
#: **Long enough for a local file, short enough to feel like a keypress.**
#: A small `default.nix` answers in well under a second, and a whole command
#: costs 0.639 s measured. nixpkgs does not, and a caller who asks for it gets
#: nothing rather than a wedged terminal.
#:
#: **5.0 s, and it was 2.0 s.** The old figure was under the cost of the case
#: it existed for: issue #231 measured a flake whose input never answers, and
#: the call returned at 4.086 s. A budget that a real case overruns is a
#: budget that reports nothing about the case, so it moved above it.
#:
#: :data:`BUDGET_VARIABLE` overrides it. Read once, from the environment, and
#: not through the settings model of `pynix`: this is one number that every
#: keypress pays for, and a settings tree would cost the start it saves.
BUDGET_SECONDS = float(os.environ.get("PYNIX_COMPLETION_BUDGET", "5.0"))

#: The variable that overrides the budget, named here so a test can set it.
BUDGET_VARIABLE = "PYNIX_COMPLETION_BUDGET"

#: Set this to a file name to make a failed completion write its traceback there.
#:
#: **A completion that answers nothing looks the same whatever went wrong**, and
#: that is deliberate: a shell shows no candidates, and the caller keeps typing.
#: It also means a defect here is invisible. Measured: the gate ran this suite
#: with no `pynix` on its search path, every row answered an empty set, and the
#: failure read as a completer offering nothing rather than one that never ran.
#: So there is a way to look, and it writes to a file rather than to stderr,
#: because stderr during a completion lands in the command line.
DEBUG_VARIABLE = "PYNIX_COMPLETION_DEBUG"

#: `nix::lvlError`, the quietest level Nix has.
_NIX_LVL_ERROR = 0


@contextlib.contextmanager
def _impatient_nix() -> Generator[None]:
    """Give Nix the timeouts of :func:`_completion_settings` and no voice, and give both back afterwards.

    **Errors only, because no one forwards the logs.** A message that no
    subscriber takes goes to fd 2, which is the command line during a
    completion. Measured: a file in a dirty git tree printed "Git tree ... is
    dirty" there at the default level.

    The settings and the level are global to the process, and `pynix/tests/`
    completes in-process, so the restore keeps them from reaching every later
    test.
    """
    # Imported here for the reason `_names` gives.
    import huggorm  # noqa: PLC0415 -- see `_names`

    wanted = _completion_settings()
    previous = {name: huggorm.get_setting(name) for name in wanted}
    previous_verbosity = huggorm.default_verbosity()
    for name, value in wanted.items():
        huggorm.set_setting(name, value)
    huggorm.set_default_verbosity(_NIX_LVL_ERROR)
    try:
        yield
    finally:
        huggorm.set_default_verbosity(previous_verbosity)
        for name, old in previous.items():
            if old is not None:
                huggorm.set_setting(name, old)


@contextlib.asynccontextmanager
async def _completion_store() -> AsyncGenerator[tuple[AsyncSession, AsyncStore]]:
    """A store for one completion, in this process, with no log forwarding.

    `_guarded` sends both output streams to a buffer that nothing reads.
    Issue #226 measured why a completion opens nothing out of process: a
    worker process cost 0.834 s of a 1.536 s answer, against 0.007 s here.
    Whichever store a process opens first pays about one second, so
    `DEFAULT_STORE` stays: a completion answers what the command would answer.
    """
    # Imported here for the reason `_names` gives.
    import huggorm  # noqa: PLC0415 -- see `_names`

    from pynix._impl.settings import DEFAULT_STORE  # noqa: PLC0415 -- see `_names`
    from pynix._util import init_nix  # noqa: PLC0415 -- see `_names`

    init_nix()
    with _impatient_nix():
        async with huggorm.AsyncSession(DEFAULT_STORE) as session:
            yield session, session.store()


@contextlib.asynccontextmanager
async def _completion_state() -> AsyncGenerator[AsyncEvalState]:
    """An evaluator for one completion, on :func:`_completion_store`."""
    async with _completion_store() as (session, store):
        yield session.eval(store)


async def _names(source: str, attr_prefix: str) -> list[str]:
    """The attribute paths of the file *source* that *attr_prefix* could mean."""
    # Imported here and not at the top of the module. Issue #123 measured what
    # `pynix.target` costs a start that evaluates nothing -- 101 ms, because it
    # pulls structlog and the exception tree of nanopynix. A parser that only
    # lists an option must not pay that, and a completion is the one caller
    # that needs it.
    from huggorm.attrcompletion import complete_file_attr_path  # noqa: PLC0415 -- see above

    from pynix._evaluate import base_attr_search, evaluate_target  # noqa: PLC0415 -- see above
    from pynix.target import EvaluationTarget  # noqa: PLC0415 -- see above

    target = EvaluationTarget(file=source, attr=None, flake=None)
    async with _completion_state() as state:
        root = await evaluate_target(target, state, auto_call_file=True, attr_search=base_attr_search())
        return await complete_file_attr_path(state, root, attr_prefix)


@contextlib.contextmanager
def _impatient_git() -> Generator[None]:
    """Give `git` the timeouts of a keypress, and give them back afterwards.

    **A caller who set one keeps it.** These are ordinary git variables, and a
    person who has chosen their own figure means it. Only a name that is unset
    gets one here.

    The restore matters because this module is called in a process, not only
    in a fresh one: `pynix/tests/` completes in-process, and a test that left
    these behind would set the timeouts of every later test.
    """
    previous = {name: os.environ.get(name) for name in _GIT_STALL_VARIABLES}
    for name, value in _GIT_STALL_VARIABLES.items():
        os.environ.setdefault(name, value)
    try:
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _guarded(work: Callable[[], Awaitable[list[str]]]) -> list[str]:
    """Run *work* under the budget, and answer nothing when it fails.

    **Every failure is the same answer, and that is on purpose.** A file that
    does not parse, an attribute path that leads nowhere, a value that is not
    an attribute set, and a budget that ran out are all "no candidates" to a
    shell. A traceback here would land in the middle of a command line.
    """
    # Imported here for the reason `_names` gives.
    import anyio  # noqa: PLC0415 -- see `_names`

    async def bounded() -> list[str]:
        with anyio.fail_after(BUDGET_SECONDS):
            return await work()

    # **stderr goes nowhere while this runs.** The evaluator logs through
    # structlog, and a log line drawn into a command line is worse than a
    # missing candidate. The shell reads the answer off file descriptor 8, so
    # nothing here needs the two standard streams.
    try:
        with (
            _impatient_git(),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return anyio.run(bounded)
    except Exception:
        # Every failure is one answer here; the docstring above says why. A
        # cancellation is not caught, so Ctrl-C still ends the process.
        _record_the_failure()
        return []


def _answer(source: str, attr_prefix: str) -> list[str]:
    """The attribute paths of *source* under *attr_prefix*, or nothing.

    *source* is expanded here, and only here. A shell expands a ``~`` before it
    runs a command, so ``--file ~/x.nix`` reaches a real run as an absolute
    path and Nix never sees the tilde. A completion runs before that expansion,
    on the word as the caller typed it, so the evaluator would be given a path
    that no directory holds. Issue #279.
    """
    return _guarded(functools.partial(_names, os.path.expanduser(source), attr_prefix))  # noqa: PTH111 -- see `_paths`


def _record_the_failure() -> None:
    """Write the current traceback to the file :data:`DEBUG_VARIABLE` names.

    Does nothing when the variable is unset, which is every ordinary run.
    A failure to write is itself ignored: this exists to explain a completion
    that answered nothing, and it must never become the reason one did.
    """
    destination = os.environ.get(DEBUG_VARIABLE)
    if not destination:
        return
    # Imported here so an ordinary completion never loads it.
    import traceback  # noqa: PLC0415 -- see above

    try:
        with open(destination, "a", encoding="utf-8") as record:  # noqa: PTH123 -- a plain file, and `anyio.Path` is async
            record.write(traceback.format_exc())
    except OSError:
        return


def complete_attr(*, prefix: str, parsed_args: Any = None, **_: Any) -> Sequence[str]:
    """Candidates for ``--attr``, read out of the ``--file`` beside it.

    argcomplete passes the namespace it has parsed so far, so the file is
    already there when the caller types ``--file F --attr <TAB>``. The other
    order, ``--attr <TAB> --file F``, answers nothing: the file is not on the
    line yet, and there is nothing to evaluate.
    """
    source = getattr(parsed_args, "file", None)
    if not source:
        return []
    # A `#` in `--file` carries the first components of the same path, which is
    # what `EvaluationTarget.selected_attr` joins at run time.
    path, _, fragment = source.partition("#")
    joined = f"{fragment}.{prefix}" if fragment else prefix
    candidates = _answer(path, joined)
    if not fragment:
        return candidates
    cut = len(fragment) + 1
    return [candidate[cut:] for candidate in candidates]


def complete_file(*, prefix: str, **_: Any) -> Sequence[str]:
    """Candidates for ``--file``: a path before the ``#``, an attribute after it.

    Nix gives this option ``Args::completePath``, which offers a file and a
    directory alike, and :func:`_paths` is that function. Issue #279 is why it
    is here: this answered nothing before the ``#`` and left the file names to
    the shell, and a shell that offers no fall-back then offered nothing at
    all. ``complete --command pynix -f`` is the line argcomplete writes for
    fish, and ``-f`` is what turns the fall-back off.
    """
    path, separator, fragment = prefix.partition("#")
    if not separator:
        return _paths(prefix)
    # `-` is standard input, and a keypress must never wait on it.
    if path == "-":
        return []
    return [f"{path}#{candidate}" for candidate in _answer(path, fragment)]


def _flake_search(name: FlakeSearch) -> AttrPathSearch:
    """The :class:`~huggorm.attrpath.AttrPathSearch` that *name* stands for.

    ``"exact"`` is the empty search, which reads the fragment as one path and
    applies no prefix. ``pynix search`` passes no search to
    :func:`~pynix._evaluate.evaluate_target`, and that is what no search means.
    """
    # Imported here for the reason `_names` gives.
    from huggorm.attrpath import AttrPathSearch  # noqa: PLC0415 -- see `_names`

    from pynix._evaluate import (  # noqa: PLC0415 -- see `_names`
        app_attr_search,
        base_attr_search,
        dev_shell_attr_search,
        repl_attr_search,
    )

    if name == "base":
        return base_attr_search()
    if name == "dev-shell":
        return dev_shell_attr_search()
    if name == "repl":
        return repl_attr_search()
    if name == "app":
        return app_attr_search()
    return AttrPathSearch()


async def _flake_names(reference: str, fragment: str, name: FlakeSearch) -> list[str]:
    """The fragments of the flake *reference* that *fragment* could mean."""
    # Imported here for the reason `_names` gives.
    import huggorm  # noqa: PLC0415 -- see `_names`
    from huggorm.attrcompletion import complete_flake_fragment  # noqa: PLC0415 -- see `_names`
    from huggorm.attrpath import flake_outputs  # noqa: PLC0415 -- see `_names`

    search = _flake_search(name)
    async with _completion_state() as state:
        # **A completion never writes a lock file, and `nix` does.** Measured:
        # a flake with one unlocked input and no `flake.lock`, completed with
        # `NIX_GET_COMPLETIONS=2 nix build <flake>#top`, came back with its
        # candidates and a `flake.lock` beside its `flake.nix`.
        # `completeFlakeRefWithFragment` (`libcmd/installables.cc`) passes the
        # flags of the command it is completing straight to `lockFlake`, and
        # `writeLockFile` defaults to true.
        #
        # A Tab is not a command, and a keypress that changes the tree of the
        # caller is a surprise that no candidate is worth. This is a place
        # where `nix` is wrong and this program does not conform. Issue #231.
        locked = await state.lock_flake(huggorm.parse_flake_ref(reference), write_lock_file=False)
        try:
            outputs = await flake_outputs(state, await state.call_flake(locked))
        finally:
            await locked.aclose()
        return await complete_flake_fragment(state, outputs, search, fragment)


def _paths(prefix: str, *, only_dirs: bool = False) -> list[str]:
    """The paths that *prefix* could name, as ``Args::_completePath`` finds them.

    That function (``libutil/args.cc``) globs ``expandTilde(prefix) + "*"`` and
    keeps every match, or keeps what ``stat`` calls a directory when
    ``onlyDirs`` is set. This is the same two steps, and *only_dirs* is that
    flag. ``completePath`` is the completer Nix gives ``--file``, and
    ``completeDir`` is one of the three sources of the one it gives a flake
    reference.

    **A path is a candidate of this program and not of the shell.** Before this
    function ``--flake`` answered nothing before the ``#``, and the shell fell
    back to file names on its own. A completer that answers cannot fall back,
    so the half that the shell used to supply has to come from here.

    **A tilde comes back as a tilde, and in `nix` it does not.** Nix offers the
    expanded path, and its own bash completion puts that straight into
    ``COMPREPLY``, which bash does not filter. argcomplete does filter: it
    keeps a candidate only when ``candidate.startswith(prefix)``, so
    ``~/Code/nanop`` and ``/home/me/Code/nanopynix`` cost the caller every
    candidate. The head goes back on for that reason. The shell expands it
    again, so the two spellings name one path.

    ``os.path.expanduser`` also reads ``~user`` where ``expandTilde`` has a
    ``TODO`` and reads only ``~``. That is a candidate more and never one
    fewer, so it stands.
    """
    # Imported here for the reason `_names` gives. `glob` is cheap, and the
    # rule is one rule.
    import glob  # noqa: PLC0415 -- see `_names`

    expanded = os.path.expanduser(prefix)  # noqa: PTH111 -- the tilde rule of `_completePath`, and no I/O
    matches = glob.glob(expanded + "*")  # noqa: PTH207 -- `glob.glob` is what Nix calls, and `anyio.Path` is async
    if only_dirs:
        matches = [candidate for candidate in matches if os.path.isdir(candidate)]  # noqa: PTH112 -- see above
    return _retilde(prefix, matches)


def _retilde(prefix: str, candidates: list[str]) -> list[str]:
    """*candidates*, spelled with the ``~`` head that *prefix* carries.

    A no-op for a prefix that has no tilde, and for a ``~user`` that names
    nobody: ``expanduser`` answers such a head unchanged, so the replacement
    replaces the head with itself.
    """
    if not prefix.startswith("~"):
        return candidates
    head = prefix.partition("/")[0]
    home = os.path.expanduser(head)  # noqa: PTH111 -- see `_paths`
    return [head + candidate[len(home) :] if candidate.startswith(home) else candidate for candidate in candidates]


async def _registry_references(prefix: str) -> list[str]:
    """The registry entries that *prefix* could name, as ``completeFlakeRef`` finds them.

    That function (``libcmd/installables.cc``) walks every layer that
    ``fetchers::getRegistries`` returns and offers each ``from`` that starts
    with the prefix. A caller who has not typed ``flake:`` gets the entry
    without it, which is how ``nixp<TAB>`` reaches ``flake:nixpkgs``.

    **This uses the ``flake-registry`` setting as it stands, so a cold cache
    downloads.** `nix` does the same on the same keypress, and dropping the
    global layer would answer fewer candidates than `nix` on every machine
    that does not pin `nixpkgs` in a system registry.

    Four cases, measured on this machine for the prefix ``nixp``:

    ==================================  ========  =============================
    case                                elapsed   answer
    ==================================  ========  =============================
    cache warm                          0.54 s    7 candidates
    no network, cache within its TTL    0.54 s    7 candidates
    no network, TTL expired             4.10 s    7, read back from the cache
    a host that accepts and never       ~7 s      the directories alone
    answers
    ==================================  ========  =============================

    The last row is the budget doing its work, plus the two seconds the
    executor waits for the interrupt. `nix` in the same case takes 14.9 s and
    answers nothing at all, so this is the one place where following `nix`
    exactly would be worse than the budget.

    **One unreachable layer must not take the others down, and in `nix` it
    does.** `getRegistries` builds all four layers before it returns any of
    them, so an exception from the global layer discards the flag, user and
    system layers as well. Measured: on this machine, which pins `nixpkgs` in
    its system registry, `nix build nixp<TAB>` with an unreachable
    `flake-registry` offers nothing at all. A build sandbox reproduces it
    without any setting, because it has no network.

    Losing a local file because a remote one is unreachable is wrong, so this
    does not conform. The second call names an empty `flake-registry`, which
    is Nix's own value for "no global layer", and Nix answers from the layers
    that did work. It costs nothing: a function-local static whose initialiser
    threw is not initialised, so the retry runs it again -- measured at
    0.002 s, against 0.002 s for the failure that precedes it.

    **`nix` returns at once when the flakes feature is off, and this does
    not.** This module reads the registry whatever the feature says.
    """
    # Imported here for the reason `_names` gives.
    from huggorm import aio  # noqa: PLC0415 -- see `_names`

    async with _completion_store() as (_session, store):
        try:
            entries = await aio.registry_entries(store)
        except Exception:
            # Broad, because Nix reports every layer's failure the same way and
            # the second call is harmless whatever the first one hit: a store
            # that cannot answer at all fails again, and `_guarded` turns that
            # into the same empty list.
            entries = await aio.registry_entries(store, {_NO_GLOBAL_REGISTRY: ""})

    candidates: list[str] = []
    for entry in entries:
        source = entry.source()
        if not prefix.startswith(_FLAKE_SCHEME) and source.startswith(_FLAKE_SCHEME):
            without_scheme = source[len(_FLAKE_SCHEME) :]
            if without_scheme.startswith(prefix):
                candidates.append(without_scheme)
        elif source.startswith(prefix):
            candidates.append(source)
    return candidates


def _reference_candidates(prefix: str) -> list[str]:
    """Candidates for a flake reference, which is the part before the ``#``.

    The three sources are the three that ``completeFlakeRef`` reads, in the
    order that function reads them: the bare ``.`` for an empty prefix, the
    directories, and the registry. Nix collects them into a ``std::set``, so
    the answer is sorted and holds no duplicate.

    **The registry runs under the budget and the directories do not.** A glob
    of one directory cannot hang, and a registry that is slow or unreachable
    must not take the file names down with it.
    """
    candidates = set(_paths(prefix, only_dirs=True))
    if not prefix:
        candidates.add(".")
    candidates.update(_guarded(functools.partial(_registry_references, prefix)))
    return sorted(candidates)


def complete_flake(prefix: str, name: FlakeSearch) -> list[str]:
    """Candidates for ``--flake``: a reference before the ``#``, a fragment after it."""
    reference, separator, fragment = prefix.partition("#")
    if not separator:
        return _reference_candidates(prefix)
    return [
        f"{reference}#{candidate}" for candidate in _guarded(functools.partial(_flake_names, reference, fragment, name))
    ]


def flake_completer(name: FlakeSearch) -> Completer:
    """A completer for ``--flake`` that applies the search *name* names.

    **Each command searches differently, so each command gets its own.**
    ``nix develop F#<TAB>`` offers the names under ``devShells.<system>`` and
    ``nix build F#<TAB>`` does not, because the two commands override
    `getDefaultFlakeAttrPathPrefixes` differently. Seven commands of this
    program declare the option, and a single completer carrying the base pair
    would be wrong for four of them: both forms of ``develop``, ``repl``, and
    ``search``, which applies no search at all.
    """

    def complete(*, prefix: str, **_: Any) -> Sequence[str]:
        return complete_flake(prefix, name)

    return complete
