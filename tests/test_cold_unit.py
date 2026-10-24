"""Cold signer behaviour that needs no chain."""

import pytest

from zecpy import ColdSigner, WatchBundle

HEIGHTS = {"overwinter": 1, "sapling": 1, "blossom": 1, "heartwood": 1, "canopy": 1,
           "nu5": 1, "nu6": 1, "nu6_1": 1, "nu6_2": 1, "nu6_3": 1}


def test_generate_is_offline_and_round_trips():
    signer, mnemonic = ColdSigner.generate(network="regtest", regtest_heights=HEIGHTS)
    assert len(mnemonic.reveal().split()) == 24
    bundle = signer.watch_bundle()
    assert bundle.ufvk.startswith("uviewregtest") and len(bundle.seed_fingerprint) == 64
    again = ColdSigner(mnemonic, network="regtest", regtest_heights=HEIGHTS).watch_bundle()
    assert again == bundle, "the same mnemonic must give the same watch bundle"
    assert WatchBundle.from_json(bundle.to_json()) == bundle


def test_two_seeds_differ():
    a, _ = ColdSigner.generate(network="test")
    b, _ = ColdSigner.generate(network="test")
    assert a.watch_bundle().seed_fingerprint != b.watch_bundle().seed_fingerprint


def test_bad_inputs_are_rejected():
    with pytest.raises(ValueError, match="mnemonic"):
        ColdSigner("not a real mnemonic", network="test")
    signer, _ = ColdSigner.generate(network="test")
    with pytest.raises(ValueError, match="PCZT"):
        signer.inspect(b"definitely not a pczt")
    assert "abandon" not in repr(signer) and "ColdSigner(" in repr(signer)
