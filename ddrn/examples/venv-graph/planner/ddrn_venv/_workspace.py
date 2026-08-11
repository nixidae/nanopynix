"""Which packages a uv workspace holds, read from the directories it holds.

**`[tool.uv.workspace] members` holds a glob, and the lock file holds names.**
uv expands the glob against the file system, and it writes the names it found
into `[manifest] members`. `Lock::satisfies` compares the two sets, and it
calls the difference `SatisfiesResult::MismatchedMembers`. A member directory
that the lock file never saw reaches no other comparison, because every other
comparison starts from the lock file.

`workspace.nix` gives this module the listing: the `pyproject.toml` of every
directory under the workspace, keyed by the path of that directory. **That file
walks and decides nothing.** Every rule below is here, and `fnmatch` of the
standard library matches each glob component.

## The rules, and where uv states them

`collect_members_only` of `crates/uv-workspace/src/workspace.rs` builds the
member list, and these five rules come from it:

1. The root is a member when it declares `[project]`. uv adds it before it
   expands one glob, and it asks for no `tool.uv.managed` there. A root with no
   `[project]` is the virtual root of `Workspace::is_non_project`.
2. A directory that an `exclude` glob names is no member.
   `WorkspaceExclusions::new` builds that list.
3. A directory that says `tool.uv.managed = false` is no member.
4. A directory that holds no `pyproject.toml` is no member. uv reaches further
   here: it skips a hidden directory and a directory of ignored files only,
   and it raises for any other. Such a directory is not in the listing at all,
   so this module skips every one of them. That under-reports a workspace that
   uv itself refuses to lock, and it raises no false alarm.
5. A directory is read once, however many globs name it.

`LockTarget::members` of `crates/uv/src/commands/project/lock_target.rs` gives
the sixth rule, and it is not in that function: **uv writes an empty
`[manifest] members` for a workspace of one member that is a project.** Every
single project therefore has an empty list. `ddrn_venv._freshness` applies that
rule, because it belongs to the comparison and not to the walk.

## What the globs mean

The `glob` crate of Rust expands each pattern, and `Pattern::new`
(`glob-0.3.4/src/lib.rs`) gives the token set: a literal character, `?`, `*`,
`**`, `[abc]` and `[!abc]`. **There is no alternation**, so `{a,b}` names a
directory of that literal name.

`**` matches a whole path component or uv refuses the pattern, with
`recursive wildcards must form a single path component`. It matches zero or
more directory levels, it matches directories only, and it matches the base
directory as well when it is the last component of the pattern.

**`packages/**` is a pattern that uv refuses for most trees**, and this reader
accepts more than uv does there. `**` names every directory at every depth, so
the `src` of a member becomes a member with no `pyproject.toml`, and uv then
raises `Workspace member ... is missing a pyproject.toml (matches:
packages/**)`. This module skips such a directory instead. That is a lock file
that uv cannot write at all, so a build of it fails for its own reason, and
this reader raises no false alarm.

`fnmatch` of the standard library reads one component the same way, and this
module gives `**` the meaning above. Two edges differ, and each one is small:
an unterminated `[` is an error to the `glob` crate and a literal to `fnmatch`,
and `[^a]` is a literal `^` to the crate and a negation to some readers.

## What this module refuses to answer

**A false alarm stops a build that is correct, which is worse than the gap that
this module closes.** So `members` gives `None` rather than guess:

- a pattern that holds `**` inside a component, which uv refuses;
- a `pyproject.toml` that TOML cannot read, or that declares no `[project]`,
  which uv raises `MissingProject` for.

Two boundaries stay, and neither is a false alarm. `workspace.nix` does not
descend into `.git`, `node_modules` and the other names that it lists, and it
follows no symbolic link. uv reaches both, so a member behind one is not
found here.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ddrn_venv._uvlock import LockError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: The key that `workspace.nix` gives the root of the tree.
ROOT = ""

#: The name of the group that `tool.uv.dev-dependencies` fills, and the one
#: that uv defaults to. `DEV_DEPENDENCIES` of
#: `crates/uv-normalize/src/group_name.rs` gives the same value.
DEV = "dev"

#: The literal that `tool.uv.default-groups` takes in place of a list, which
#: `DefaultGroups::All` holds. `uv sync --all-extras` and `--all-groups` mean
#: the same for the two arguments of `./uv.nix`.
ALL = "all"

#: What a caller asks for: nothing, every one of them, or a list of names.
type Choice = str | Sequence[str] | None


class Members(NamedTuple):
    """What the file system says a workspace holds."""

    #: The name of each member, as `pyproject.toml` spells it.
    names: tuple[str, ...]
    #: The name of the root project, or `None` for a virtual root.
    root: str | None
    #: The `pyproject.toml` of each member, keyed by that same name. `select`
    #: reads the extras and the groups of a member out of it.
    documents: dict[str, dict[str, Any]]


def split(pattern: str) -> list[str]:
    """One glob, as its path components.

    uv calls `normalize_path` on each pattern first, which drops a leading
    `./`. An empty component comes from a `//` or from a trailing separator.
    """
    return [component for component in pattern.split("/") if component not in {"", "."}]


def matches(pattern: Sequence[str], components: Sequence[str]) -> bool:
    """Whether one directory answers one glob, component by component."""
    if not pattern:
        return not components
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # **`**` matches zero or more components, and the empty match is what
        # makes `packages/**` name `packages` itself.** The `glob` crate takes
        # the empty branch first, in `Pattern::matches_from`, and it returns
        # the directory itself when the pattern ends in the recursive token.
        return any(matches(rest, components[index:]) for index in range(len(components) + 1))
    if not components:
        return False
    return fnmatch.fnmatchcase(components[0], head) and matches(rest, components[1:])


def readable(pattern: Sequence[str]) -> bool:
    """Whether uv reads this pattern at all.

    `**` forms a whole component or the `glob` crate refuses the pattern, so
    `a/**b`, `a/bc**` and `a/*****` are each an error and not a glob.
    """
    return all("**" not in component or component == "**" for component in pattern)


def _select(patterns: Iterable[str], directories: Iterable[str]) -> set[str]:
    parsed = [split(pattern) for pattern in patterns]
    return {directory for directory in directories for pattern in parsed if matches(pattern, split(directory))}


def members(listing: Mapping[str, str]) -> Members | None:
    """The members of the workspace that `listing` describes, or `None`.

    `listing` maps the path of a directory, relative to the workspace root, to
    the `pyproject.toml` that the directory holds. `ROOT` is the key of the
    root itself. `None` says that a rule of uv reaches further than this
    reader, and the caller then makes no comparison.
    """
    documents: dict[str, dict[str, Any]] = {}
    for relative, path in listing.items():
        try:
            documents[relative] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return None

    root_document = documents.get(ROOT)
    if root_document is None:
        return None

    table = root_document.get("tool", {}).get("uv", {}).get("workspace")
    member_globs: Sequence[str] = [] if table is None else table.get("members", [])
    exclude_globs: Sequence[str] = [] if table is None else table.get("exclude", [])
    if not all(readable(split(glob)) for glob in [*member_globs, *exclude_globs]):
        return None

    # A directory of the listing is a candidate when a member glob names it and
    # no exclude glob does. `ROOT` is never a candidate: uv adds the root by
    # its own rule, and no glob of a workspace names its own root.
    directories = [relative for relative in documents if relative != ROOT]
    candidates = _select(member_globs, directories) - _select(exclude_globs, directories)

    names: list[str] = []
    held: dict[str, dict[str, Any]] = {}
    for relative in sorted(candidates):
        document = documents[relative]
        if document.get("tool", {}).get("uv", {}).get("managed", True) is False:
            continue
        project = document.get("project")
        if project is None:
            return None
        names.append(project["name"])
        held[project["name"]] = document

    root_project = root_document.get("project")
    root_name = None if root_project is None else root_project["name"]
    if root_name is not None:
        names.append(root_name)
        held[root_name] = root_document

    return Members(names=tuple(dict.fromkeys(names)), root=root_name, documents=held)


def extras_of(document: Mapping[str, Any], want: Choice) -> list[str]:
    """Which extras of one member to enable.

    **uv keeps no default for an extra in `pyproject.toml`.** `sync.rs` reads
    `DefaultExtras::default()` and leaves the project unread, with the binding
    named `_project` to say so, and that default is the empty list. So `None`
    here is `uv sync` with no flag.

    `ALL` is `uv sync --all-extras`, and the extras of a member are the keys of
    `[project.optional-dependencies]`. A list names them, and it names the same
    ones for each member, the way `uv sync --extra` does.
    """
    if want is None:
        return []
    optional: Mapping[str, Any] = document.get("project", {}).get("optional-dependencies", {})
    if want == ALL:
        return sorted(optional)
    return [str(name) for name in want]


def groups_of(document: Mapping[str, Any], want: Choice) -> list[str]:
    """Which dependency groups of one member to enable.

    **uv does keep a default for a group, and `tool.uv.default-groups` is it.**
    `default_dependency_groups` of `crates/uv/src/commands/project/mod.rs`
    reads that table, and it gives `["dev"]` when the table says nothing.
    `DEV_DEPENDENCIES` of `crates/uv-normalize/src/group_name.rs` is that name.
    So `None` here is `uv sync` with no flag, and it is not "no group at all".

    The same function validates an explicit list: a name that
    `[dependency-groups]` does not declare gives `MissingDefaultGroup`. It does
    not validate the `["dev"]` that it falls back to, and neither does this.

    `tool.uv.default-groups` also takes the literal `"all"`, which
    `DefaultGroups::All` holds. `ALL` here is that value, and it is
    `uv sync --all-groups` as well.
    """
    declared: Mapping[str, Any] = document.get("dependency-groups", {})
    legacy: Sequence[str] = document.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    names: set[str] = {str(name) for name in declared}
    if legacy:
        names.add(DEV)
    every = sorted(names)

    if want is None:
        default = document.get("tool", {}).get("uv", {}).get("default-groups")
        if default is None:
            return [DEV]
        if default == ALL:
            return every
        chosen: Sequence[str] = default
        missing = [name for name in chosen if name not in declared]
        if missing:
            raise LockError(
                f"pyproject.toml gives tool.uv.default-groups = {list(default)}, "
                f"and [dependency-groups] declares no {missing}. uv refuses the same file."
            )
        return [str(name) for name in chosen]
    if want == ALL:
        return every
    return [str(name) for name in want]


def selection(found: Members, extras: Choice, groups: Choice) -> dict[str, list[str]]:
    """What each member installs, as `resolve_lock` takes it.

    **`uv.lock` records none of this.** `uv sync` takes the decision on its
    command line, and `uv.nix` takes it as two arguments. One list carries both
    kinds, because `Package.edges` reads an extra out of
    `optional-dependencies` and a group out of `dev-dependencies`, and a name
    that is neither contributes no edge.
    """
    return {
        name: sorted({*extras_of(found.documents[name], extras), *groups_of(found.documents[name], groups)})
        for name in found.names
    }
