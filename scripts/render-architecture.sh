#!/usr/bin/env bash
# Draw `ddrn/architecture.dot`, and keep `ddrn/ARCHITECTURE.md` in step with it.
#
# **The dot file is the source, and the document holds a copy of the drawing.**
# A copy rots, so `ddrn/tests/test_architecture.py` runs `--check` and fails
# when the two disagree. Run `--write` after each edit of the dot file.
#
#   scripts/render-architecture.sh --show    # print the drawing
#   scripts/render-architecture.sh --write   # put it in the document
#   scripts/render-architecture.sh --check   # fail when the document is stale
#   scripts/render-architecture.sh --term    # draw it in colour, in a terminal
#
# `graph-easy` writes the ASCII art, and `dot` reads the same file to check
# that it parses and to draw it in a terminal. Both are in the dev shell.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
dot_file="$root/ddrn/architecture.dot"
doc_file="$root/ddrn/ARCHITECTURE.md"
begin='<!-- architecture:begin -->'
end='<!-- architecture:end -->'

# `graph-easy` reads no `phase` attribute and no `file` attribute, and it warns
# once for each node that carries one. Those attributes are for
# `test_architecture.py`, so the warnings are expected and go nowhere.
render() {
  dot -Tcanon "$dot_file" >/dev/null
  graph-easy --input "$dot_file" --as=ascii 2>/dev/null
}

# The document holds the drawing between two markers, in a fenced block. This
# writes everything before the first marker, then the drawing, then everything
# after the second one.
compose() {
  awk -v begin="$begin" -v end="$end" -v art="$1" '
    $0 == begin { print; print "```text"; printf "%s", art; print "```"; skip = 1; next }
    $0 == end   { print; skip = 0; next }
    !skip       { print }
  ' "$doc_file"
}

mode=${1:---show}
case "$mode" in
  --show)
    render
    ;;
  --term)
    dot -Tvt "$dot_file"
    ;;
  --write)
    art=$(render)
    compose "$art"$'\n' >"$doc_file.new"
    mv "$doc_file.new" "$doc_file"
    echo "wrote the drawing into $doc_file" >&2
    ;;
  --check)
    art=$(render)
    if diff -u "$doc_file" <(compose "$art"$'\n'); then
      echo "$doc_file holds the current drawing" >&2
    else
      echo "$doc_file is stale. Run scripts/render-architecture.sh --write" >&2
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 [--show|--term|--write|--check]" >&2
    exit 2
    ;;
esac
