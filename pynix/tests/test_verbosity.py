from __future__ import annotations

import pytest
from huggorm import Verbosity

from pynix._verbosity import verbosity_of


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Verbosity.CHATTY, Verbosity.CHATTY),
        (0, Verbosity.ERROR),
        ("0", Verbosity.ERROR),
        ("ERROR", Verbosity.ERROR),
        ("err", Verbosity.ERROR),
        ("warn", Verbosity.WARN),
        ("warning", Verbosity.WARN),
        ("notice", Verbosity.NOTICE),
        ("INFO", Verbosity.INFO),
        ("talkative", Verbosity.TALKATIVE),
        ("chatty", Verbosity.CHATTY),
        ("debug", Verbosity.DEBUG),
        ("lvlVomit", Verbosity.VOMIT),
        ("lvl_debug", Verbosity.DEBUG),
        ("log-level-info", Verbosity.INFO),
        (" 7 ", Verbosity.VOMIT),
    ],
)
def test_verbosity_of_accepts_nix_names_and_numbers(raw: Verbosity | int | str, expected: Verbosity) -> None:
    assert verbosity_of(raw) is expected


@pytest.mark.parametrize("raw", [-1, 8, "loud", "", "8", True])
def test_verbosity_of_rejects_what_names_no_level(raw: int | str) -> None:
    with pytest.raises((TypeError, ValueError)):
        verbosity_of(raw)
