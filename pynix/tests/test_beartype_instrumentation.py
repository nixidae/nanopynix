"""The suite runs under beartype's import hook, with deferimport off.

A suite that loses the hook still reports every test as passed, so these tests
are the only place that notices. ``nanopynix_testing.beartype_hook`` gives the
mechanism.
"""

from __future__ import annotations

import sys

import deferimport
import pytest
from beartype.roar import BeartypeCallHintParamViolation

from pynix._option_paths import split_path


def test_the_import_hook_is_installed() -> None:
    assert "beartype.claw" in sys.modules


def test_a_wrong_argument_raises() -> None:
    with pytest.raises(BeartypeCallHintParamViolation):
        split_path(42)  # type: ignore[arg-type]  # the wrong type is the point of the test


def test_imports_are_eager_under_beartype() -> None:
    assert deferimport.installed() is None
