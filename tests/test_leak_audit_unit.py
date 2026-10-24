"""The leak-audit classifier, without a chain."""

import pytest

from tools.leak_audit.classify import Label, classify
from tools.leak_audit.schema import METHODS


def test_every_lightwalletd_method_has_an_explicit_rule():
    """A new RPC in a future proto must be classified on purpose, not by the fallback."""
    for name in METHODS:
        _, reason = classify(name, {"height": 9_999})
        assert not reason.startswith("unknown method"), name


@pytest.mark.parametrize(
    ("method", "request_", "label"),
    [
        ("GetBlockRange", {"start": {"height": 1}, "end": {"height": 9}}, Label.PUBLIC),
        ("GetSubtreeRoots", {"shieldedProtocol": 1}, Label.PUBLIC),
        ("GetTreeState", {"height": 2_999_999}, Label.COARSE),  # birthday 3,000,000
        ("GetTreeState", {"height": 2_911_999}, Label.COARSE),  # sync batch boundary
        ("GetTreeState", {"height": 334}, Label.IDENTIFYING),  # where a previous sync stopped
        ("GetTreeState", {"height": 1}, Label.COARSE),  # regtest floor
        ("GetTreeState", {"height": 419_200}, Label.COARSE),  # Mainnet Sapling activation
        ("GetTreeState", {"height": 2_912_345}, Label.IDENTIFYING),  # an exact birthday
        ("GetTreeState", {"hash": "ab"}, Label.IDENTIFYING),
        ("GetTransaction", {"hash": "ab"}, Label.IDENTIFYING),
        ("GetAddressUtxos", {"addresses": ["t1..."]}, Label.IDENTIFYING),
        ("GetTaddressTxids", {"address": "t1..."}, Label.IDENTIFYING),
        ("SendTransaction", {"data_bytes": 9000}, Label.BROADCAST),
        ("SomeFutureMethod", {}, Label.IDENTIFYING),  # fails closed
    ],
)
def test_classification(method, request_, label):
    assert classify(method, request_)[0] is label
