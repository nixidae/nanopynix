"""Keep `ddrn/ARCHITECTURE.md` in step with `ddrn/architecture.dot`.

**A document that describes code goes stale, and a machine can see when.**
Two things rot here, and each one has a check:

1. The document holds a copy of the drawing. `scripts/render-architecture.sh
   --check` renders the dot file again and compares.
2. Each node of the dot file names the file that does its work. A rename
   leaves the name behind, and nothing else says so.

`ddrn/tests/AGENTS.md` of the repository gives the rule that a convention a
machine can check belongs in a test, and not in a paragraph.

Run it with ``direnv exec . pytest ddrn/tests``.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[2]
DOT = ROOT / "ddrn/architecture.dot"
DOC = ROOT / "ddrn/ARCHITECTURE.md"
RENDER = ROOT / "scripts/render-architecture.sh"

#: One `file = "..."` attribute of a node of the dot file.
FILE_ATTRIBUTE = re.compile(r'\bfile\s*=\s*"([^"]+)"')

#: One `phase = "..."` attribute of a node of the dot file.
PHASE_ATTRIBUTE = re.compile(r'\bphase\s*=\s*"([^"]+)"')

#: One node declaration, which is a name and then a bracket of attributes.
NODE = re.compile(r"^\s{2}(\w+)\s+\[(.*)\];$", re.MULTILINE)

#: The three words that start a statement of defaults in the dot language.
#: Such a statement looks like a node and declares none.
KEYWORDS = frozenset({"node", "edge", "graph"})


def nodes() -> list[tuple[str, str]]:
    """Each node of the dot file, as its name and its attribute text."""
    return [(name, attributes) for name, attributes in NODE.findall(DOT.read_text()) if name not in KEYWORDS]


#: Read once, because `parametrize` needs the names at collection.
NODES = nodes()


def test_the_dot_file_declares_nodes() -> None:
    # A guard on the two patterns above. A change to the layout of the dot
    # file that these stop matching would make every test below vacuous.
    assert len(NODES) >= 8


@pytest.mark.parametrize(("name", "attributes"), NODES, ids=[name for name, _ in NODES])
def test_each_node_names_a_file_that_exists(name: str, attributes: str) -> None:
    found = FILE_ATTRIBUTE.search(attributes)
    assert found, f"the node '{name}' names no file"
    path = ROOT / found.group(1)
    assert path.exists(), f"the node '{name}' names '{found.group(1)}', and that path is gone"


def test_each_node_says_which_phase_it_runs_in() -> None:
    # The document gives one section for each phase, so a node of a phase that
    # the document does not hold is a node with no explanation.
    found = {name: PHASE_ATTRIBUTE.search(attributes) for name, attributes in nodes()}
    missing = sorted(name for name, phase in found.items() if phase is None)
    assert not missing, f"these nodes name no phase: {missing}"
    named = {phase.group(1) for phase in found.values() if phase is not None}
    assert named == {"evaluate", "plan", "build"}


async def test_the_document_holds_the_current_drawing() -> None:
    # **This is the check that keeps the drawing honest.** The script renders
    # the dot file again and compares it with what the document holds, so an
    # edit of the graph that nobody drew again fails here.
    result = await anyio.run_process([str(RENDER), "--check"], check=False)
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
