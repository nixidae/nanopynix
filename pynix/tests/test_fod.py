from __future__ import annotations

import uuid
from collections.abc import Iterator

import huggorm
import pytest
from huggorm.fod import FodHashMismatch
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, with_nixpkgs

from pynix import _util
from pynix._build_log import BuildLog
from pynix.fod import FodBuildError, build_fixed_outputs, mismatches_in

FAKE = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEEP_GOING = {"keep-going": "true"}


def _fod(name: str, payload: str, extra: str = "") -> str:
    return f"""runCommand "{name}" {{
    {extra}
    outputHash = lib.fakeHash;
    outputHashAlgo = "sha256";
    outputHashMode = "flat";
  }} "printf '%s\\n' {payload} > $out\""""


async def _drv_paths(environment: NixTestEnvironment, nixpkgs_path: str, attrs: str) -> dict[str, str]:
    """The `.drv` of each attribute of `with import <nixpkgs> {}; { attrs }`."""
    async with _util.huggorm_eval_session(environment.store_uri) as (_session, store, state):
        value = await state.eval_expr(with_nixpkgs(f"with import <nixpkgs> {{}}; {{ {attrs} }}", nixpkgs_path))
        await state.force(value)
        drvs: dict[str, str] = {}
        for name in await value.names():
            attr = await value.get(name)
            await state.force(attr)
            drvs[name] = await store.print_store_path(await attr.drv_path())
        return drvs


@pytest.fixture
def ca_derivations() -> Iterator[None]:
    before = huggorm.get_setting("experimental-features") or ""
    huggorm.set_setting("experimental-features", f"{before} ca-derivations")
    yield
    huggorm.set_setting("experimental-features", before)


@LINUX_CHROOT_BUILD
async def test_every_requested_mismatch_comes_back_from_one_build(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str
) -> None:
    nonce = uuid.uuid4().hex
    drvs = await _drv_paths(
        shared_nix_environment,
        nixpkgs_path,
        f"""a = {_fod("a", f"a-{nonce}")};
            b = {_fod("b", f"b-{nonce}")};
            ok = runCommand "ok" {{ }} "echo {nonce} > $out";""",
    )
    async with _util.huggorm_build_session(shared_nix_environment.store_uri, settings=KEEP_GOING) as opened:
        session, build_store, _eval_store, _state = opened
        result = await build_fixed_outputs(session, build_store, list(drvs.values()))

    assert set(result.mismatches) == {drvs["a"], drvs["b"]}
    assert {m.specified for m in result.mismatches.values()} == {FAKE}
    assert result.mismatches[drvs["a"]].got != result.mismatches[drvs["b"]].got
    assert set(result.failed) == {drvs["a"], drvs["b"]}
    assert set(result.built) == {drvs["ok"]}


@LINUX_CHROOT_BUILD
async def test_a_dependency_mismatch_is_named_by_the_dependency(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str
) -> None:
    nonce = uuid.uuid4().hex
    drvs = await _drv_paths(
        shared_nix_environment,
        nixpkgs_path,
        f"""dep = {_fod("dep", f"dep-{nonce}")};
            consumer = runCommand "consumer" {{ }} "cat ${{{_fod("dep", f"dep-{nonce}")}}} > $out";""",
    )
    async with _util.huggorm_build_session(shared_nix_environment.store_uri, settings=KEEP_GOING) as opened:
        session, build_store, _eval_store, _state = opened
        result = await build_fixed_outputs(session, build_store, [drvs["consumer"]])

    assert list(result.mismatches) == [drvs["dep"]]
    assert list(result.failed) == [drvs["consumer"]]


@LINUX_CHROOT_BUILD
@pytest.mark.usefixtures("ca_derivations")
async def test_a_mismatch_built_resolved_is_named_by_the_evaluated_derivation(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str
) -> None:
    """A floating content-addressed input makes Nix build a resolved copy, and
    name the copy in the mismatch. The caller holds the evaluated path."""
    nonce = uuid.uuid4().hex
    drvs = await _drv_paths(
        shared_nix_environment,
        nixpkgs_path,
        f"""fod = let input = runCommand "floating" {{ __contentAddressed = true; }} "echo {nonce} > $out"; in
              {_fod("fod", f"fod-{nonce}", "inherit input;")};""",
    )
    async with _util.huggorm_build_session(shared_nix_environment.store_uri, settings=KEEP_GOING) as opened:
        session, build_store, _eval_store, _state = opened
        result = await build_fixed_outputs(session, build_store, [drvs["fod"]])

    assert list(result.mismatches) == [drvs["fod"]]
    assert "resolved" in result.failed[drvs["fod"]]


async def test_it_refuses_to_build_without_keep_going(shared_nix_environment: NixTestEnvironment) -> None:
    async with _util.huggorm_build_session(
        shared_nix_environment.store_uri, settings={"keep-going": "false"}
    ) as opened:
        session, build_store, _eval_store, _state = opened
        with pytest.raises(FodBuildError, match="keep-going is off"):
            await build_fixed_outputs(session, build_store, [])


def _message(drv: str, got: str) -> str:
    return f"error: hash mismatch in fixed-output derivation '{drv}':\n  specified: {FAKE}\n     got:    {got}"


def test_a_resolved_copy_is_renamed_to_its_original() -> None:
    log = BuildLog(originals={"/nix/store/r-x.drv": "/nix/store/o-x.drv"})
    got = "sha256-" + "B" * 43 + "="
    assert mismatches_in([_message("/nix/store/r-x.drv", got)], log) == {
        "/nix/store/o-x.drv": FodHashMismatch(specified=FAKE, got=got, drv_path="/nix/store/o-x.drv")
    }


def test_the_same_mismatch_said_twice_is_one() -> None:
    got = "sha256-" + "B" * 43 + "="
    assert len(mismatches_in([_message("/nix/store/a-x.drv", got)] * 2, BuildLog())) == 1


def test_two_different_mismatches_for_one_derivation_raise() -> None:
    first, second = "sha256-" + "B" * 43 + "=", "sha256-" + "C" * 43 + "="
    with pytest.raises(FodBuildError, match="two different mismatches"):
        mismatches_in([_message("/nix/store/a-x.drv", first), _message("/nix/store/a-x.drv", second)], BuildLog())
