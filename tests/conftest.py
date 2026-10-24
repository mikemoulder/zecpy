"""Fixtures for tests that run against the local regtest chain (devnet/).

Devnet tests are skipped when the chain is not running. Start it with
    devnet/up.sh "$(cat ~/.zecpy-dev/wallets/miner.addr)"
"""

from __future__ import annotations

import pytest

from tools.devnet import DEV, LWD, REPO, Devnet, activation_heights, rpc

__all__ = ["DEV", "LWD", "REPO", "Devnet"]


@pytest.fixture(scope="session")
def devnet() -> Devnet:
    try:
        rpc("getblockcount")
    except OSError:
        pytest.skip("devnet is not running (devnet/up.sh)")
    return Devnet(server=LWD, heights=activation_heights())
