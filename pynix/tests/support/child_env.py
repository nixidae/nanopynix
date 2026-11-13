"""The environment of a child ``pynix`` or ``nix`` that a test starts.

The tests in this process read no configuration of the host
(``_host_nix_conf_stays_out``) and run with ``BASE_SETTINGS`` and
``EXPERIMENTAL_FEATURES``. A child gets the same settings. With the host's
``nix.conf`` instead, ``use-cgroups = true`` there fails every build of a user
against a local store: the user cannot create a cgroup in a root-owned session
scope (nixidae/nixidae#1037).
"""

from __future__ import annotations

import os
from pathlib import Path

from huggorm.testing.environment import EXPERIMENTAL_FEATURES
from huggorm.testing.settings import BASE_SETTINGS

#: The settings of the tests in this process, as ``NIX_CONFIG`` gives them to a child.
TEST_NIX_CONFIG = "".join(
    [
        *(f"{name} = {value}\n" for name, value in BASE_SETTINGS.items()),
        f"experimental-features = {' '.join(EXPERIMENTAL_FEATURES)}\n",
    ]
)


def child_environment(directory: Path) -> dict[str, str]:
    """``os.environ`` with no configuration of the host, the user or pynix.

    *directory* holds no configuration file: each variable names a path in it
    that does not exist.
    """
    absent = directory / "absent"
    return {
        **os.environ,
        "NIX_CONF_DIR": str(absent),
        "NIX_USER_CONF_FILES": str(absent / "nix.conf"),
        "PYNIX_CONFIG": str(absent / "pynix.toml"),
        "NIX_CONFIG": TEST_NIX_CONFIG,
    }
