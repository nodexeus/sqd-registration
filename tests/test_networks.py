import dataclasses

import pytest
from eth_utils import to_checksum_address

from sqdreg.networks import NETWORKS


def test_both_networks_are_defined():
    assert set(NETWORKS) == {"mainnet", "tethys"}


def test_mainnet_parameters():
    mainnet = NETWORKS["mainnet"]
    assert mainnet.name == "mainnet"
    assert mainnet.chain_id == 42161
    assert mainnet.rpc_url == "https://arb1.arbitrum.io/rpc"
    assert to_checksum_address(mainnet.worker_registration) == to_checksum_address(
        "0x36e2b147db67e76ab67a4d07c293670ebefcae4e"
    )


def test_tethys_parameters():
    tethys = NETWORKS["tethys"]
    assert tethys.name == "tethys"
    assert tethys.chain_id == 421614
    assert tethys.rpc_url == "https://sepolia-rollup.arbitrum.io/rpc"
    assert to_checksum_address(tethys.worker_registration) == to_checksum_address(
        "0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc"
    )


def test_key_matches_name_field():
    for key, network in NETWORKS.items():
        assert key == network.name


def test_network_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        NETWORKS["mainnet"].chain_id = 1
