"""Invoice logic that needs no chain: the event state machine and the payment URI."""

import pytest

from zecpy.invoices import Invoice, _next_state


@pytest.mark.parametrize(
    ("previous", "depth", "expected"),
    [
        (None, 0, None),  # never mined: nothing to report
        (None, 1, "detected"),
        (None, 3, "confirmed"),  # first seen already deep enough
        ("detected", 2, None),  # still short of 3: no duplicate event
        ("detected", 3, "confirmed"),
        ("confirmed", 9, None),  # deeper: no duplicate event
        ("confirmed", 0, "reverted"),  # reorg removed its block
        ("detected", 0, "reverted"),
        ("reverted", 0, None),
        ("reverted", 1, "detected"),  # mined again on the new chain
        ("reverted", 3, "confirmed"),
    ],
)
def test_state_transitions(previous, depth, expected):
    assert _next_state(previous, depth, confirmations=3) == expected


def test_uri_follows_zip321():
    invoice = Invoice(1, "order #77 & co", 150_000, "u1abc", 0)
    assert invoice.uri == "zcash:u1abc?amount=0.0015&message=order%20%2377%20%26%20co"


def test_uri_whole_and_tiny_amounts():
    assert Invoice(1, "a", 100_000_000, "u1x", 0).uri.startswith("zcash:u1x?amount=1&")
    assert Invoice(1, "a", 1, "u1x", 0).uri.startswith("zcash:u1x?amount=0.00000001&")
