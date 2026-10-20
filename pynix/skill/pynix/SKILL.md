---
name: pynix
description: Run Nix through the pynix CLI instead of the nix CLI -- eval, build, develop, print-dev-env, path-info, log, why-depends, copy, store, search, flake, config, registry. Load before any Nix command; fall back to nix only for what pynix lacks.
---

# pynix

`pynix` is nanopynix's Nix CLI: libnix in-process, results as JSON on stdout.
Use it in place of `nix` for every command it has. Agent use is the dogfood:
a gap or a bug found here is a pynix issue, not a reason to drop it quietly.

## Mapping

| nix | pynix |
|---|---|
| `nix eval --file . a.b` | `pynix eval --file . --attr a.b` or `--file '.#a.b'` |
| `nix eval --expr E` | `pynix eval --expr E`, or E on stdin (below) |
| `nix build --file . a` | `pynix build --file . --attr a` |
| `nix build .#a` | `pynix build --flake .#a` |
| `nix develop --file . a -c CMD` | `pynix develop --file . --attr a -- CMD` |
| `nix print-dev-env` | `pynix print-dev-env [--json]` |
| `nix path-info P` | `pynix path-info P` (one path) |
| `nix log P`, `nix why-depends A B [--precise]` | same verb |
| `nix copy --to S P` | `pynix copy --to S P`, or `--file`/`--flake` instead of P |
| `nix store gc/ls/cat/diff-closures/verify/...` | `pynix store <same>` |
| `nix derivation show --file . a` | `pynix derivation show --file . --attr a` (a target, not a `.drv` path) |
| `nix flake show/metadata`, `nix config show`, `nix registry` | same verbs |
| `nix search`, option search | `pynix search --file '<nixpkgs>' [--options/--packages] QUERY [--json-output]`; `--file`/`--flake` required |

Differences that bite:

- No positional attribute: `pynix build --file . foo` is a usage error. Use
  `--attr foo` or `--file '.#foo'`.
- `--file` takes a path, `<name>` lookup path, URL or flake ref, each with an
  optional `#attr`. A bare word (`nixpkgs`) tries `NIX_PATH` first, then the
  registry; write local paths with `./`.
- `build` writes no `result` link. Its stdout is
  `{"outputs": {"out": "/nix/store/..."}, ...}`: take the path with `jq -r .outputs.out`.
- `eval` always prints JSON. For `--raw`, pipe through `jq -r`.
- `build --dry-run` belongs to `--update-fod`; alone it builds nothing and prints nothing.
- Store selection is `--store URI`, plus `--eval-store` on build and develop.

## Output

stdout holds only the result. stderr holds what `nix` would print: warnings,
errors, `building '...'...`, and build logs with `--print-build-logs`. A build
that changes nothing prints nothing there. Do not discard stderr; it is where
the error is. `--verbosity talkative` adds pynix's own progress events.

A stderr line that is noise, not news, is a pynix bug: file it.

## Nix programs on stdin

`pynix eval` with no `--expr` reads the expression from stdin, so a heredoc
carries a Nix program:

```sh
pynix eval <<'EOF'
let pkgs = import <nixpkgs> { }; in pkgs.hello.version
EOF
```

Other commands cannot read stdin yet: `--file -` is refused and `--file
/dev/stdin` fails (#862). For `build` and the rest, write the expression to a
file in the scratchpad and pass `--file`.

## pynix-only

- `build --nom`: nix-output-monitor style progress on stderr.
- `build --update-fod [--dry-run]`: rewrites stale fixed-output hash literals.
- `build --namespaced [--overlay-dir D] [--copy-back] [--sandbox-path P]`: a
  build in a private user namespace over an overlay of the host store. The host
  store is unchanged unless `--copy-back`. Linux only.
- `build --copy URI`: copy the output closure to another store after the build.
- `search` with no query and a terminal opens a TUI; pass a query or `--no-tui`.

## Fall back to nix

Use `nix` only for what pynix lacks, and say so in the reply:

- `nix run`, `nix shell` (#113)
- `--arg`, `--argstr` (#524)
- `nix fmt`, `nix edit` (#118)
- any other subcommand missing from `pynix --help`

Before falling back for a gap not listed here, or after a pynix failure that
nix does not share, search `gh issue list -R nixidae/nixidae --search "pynix ..."`.
If no issue covers it, file one in nixidae/nixidae with label
`source: nanopynix`: the command, the nix command that works, the stderr log.

## Configuration

Defaults come from, first wins: the flag, `PYNIX_<OPTION>` /
`PYNIX_NIX_<SETTING>`, `$XDG_CONFIG_HOME/pynix/config.toml` (`[defaults]`
for pynix options, `[nix]` for Nix settings), the built-in default.
