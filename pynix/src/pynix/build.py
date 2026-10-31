from __future__ import annotations

from pathlib import Path
from typing import override

from libpynix import opt
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import (
    ConfiguredCommand,
    eval_store_option,
    option,
    print_build_logs_option,
    store_option,
    verbosity_option,
)


class Build(ConfiguredCommand):
    """Build a Nix derivation value"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to build with.")

    eval_store: str | None = eval_store_option()

    # None, not a literal list: an absent flag leaves the value to
    # `PYNIX_NIX_*`, then to `[nix]`, then to `nix.conf`. A parser default
    # would rank above the environment, and `PYNIX_NIX_SUBSTITUTERS` would
    # never take effect.
    substituters: str | None = opt(None, help="Space-separated substituter URLs.")

    trusted_public_keys: str | None = opt(None, help="Space-separated substituter public keys.")

    # None, not a literal level: Nix's own compiled-in default (info) stands
    # when no verbosity is named, as it does for `nix build`.
    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    nom: bool = opt(
        False,
        help=(
            "Show the progress as nix-output-monitor does: the dependency graph of the builds and "
            "downloads, and a row of totals, at the bottom of stderr."
        ),
    )

    update_fod: bool = opt(False, help="Update plain fixed-output hash literals after a hash mismatch.")

    dry_run: bool = opt(False, help="Show --update-fod changes without writing or rebuilding.")

    namespaced: bool = opt(
        False,
        help=(
            "Build in a private user namespace, against an overlay store whose lower layer is the host "
            "store. Nothing is copied in, the host store does not change, and this process owns the "
            "sandbox settings that the daemon otherwise controls. Linux only."
        ),
    )

    overlay_dir: Path | None = opt(
        None,
        help=(
            "Keep the overlay's upper layer here, instead of in a temporary directory that is deleted "
            "on exit. Reuse the same directory to keep what earlier --namespaced builds produced. "
            "Implies --namespaced."
        ),
    )

    linux_builder: bool = opt(
        False,
        help=(
            "Build in a Linux VM that this command starts on Apple's Virtualization.framework, and stops "
            "when it exits. The VM builds aarch64-linux, and x86_64-linux through Rosetta. Its store "
            "overlays the host store, so nothing is copied in. macOS only, and the host /nix must be "
            "case-sensitive."
        ),
    )

    linux_builder_guest: str | None = option(
        "The manifest of the --linux-builder guest: the store path that "
        "`nix build --file nanopynix internal.linuxBuilder.manifest` prints."
    )

    copy_back: bool = opt(
        True,
        # `argparse.BooleanOptionalAction` writes `--copy-back` and
        # `--no-copy-back` from this one declaration.
        negatable=True,
        help=(
            "Copy the outputs of a --namespaced or --linux-builder build into the host store when the "
            "build succeeds. Without it the outputs are gone when the worker or the VM exits."
        ),
    )

    copy: str | None = opt(
        None,
        help=(
            "Store URI to copy the closure of the outputs into when the build succeeds. --store and the "
            "configured builders decide where the build runs, and this option decides where the result ends up."
        ),
    )

    # No default is written: `libpynix` gives a repeated option a new empty
    # list for each command, because one shared literal would keep whatever a
    # previous parse appended to it.
    sandbox_path: list[str] = opt(
        help=(
            "Extra path to mount into the build sandbox, as /inside=/outside or /path. Repeatable. "
            "Requires --namespaced, because the daemon does not let a client change its sandbox."
        ),
    )

    @override
    async def run(self) -> None:
        await _impl.build.run_build(self)
