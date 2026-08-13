from unittest.mock import MagicMock

from sqdreg.networks import NETWORKS
from sqdreg.registry import FALLBACK_REGISTER_GAS, Registry

ADDRESS = "0x0000000000000000000000000000000000000001"
TOKEN = "0x0000000000000000000000000000000000000002"
PEER = b"\x00$peer"
METADATA = '{"name":"worker-1"}'


def make_registry(network_name="mainnet"):
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda value: value
    registration = MagicMock()
    token = MagicMock()
    w3.eth.contract.side_effect = [registration, token]
    return Registry(w3, NETWORKS[network_name], ADDRESS), registration, token


def worker_tuple(registered_at):
    """Worker struct: creator, peerId, bond, registeredAt, deregisteredAt, metadata."""
    return ["0x0", PEER, 100, registered_at, 0, ""]


def test_unseen_peer_id_is_not_registered_and_needs_only_one_read():
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 0

    assert registry.is_registered(PEER) is False
    registration.functions.getWorker.assert_not_called()


def test_live_worker_is_registered():
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 5
    registration.functions.getWorker.return_value.call.return_value = worker_tuple(900)

    assert registry.is_registered(PEER) is True
    registration.functions.getWorker.assert_called_once_with(5)


def test_withdrawn_worker_is_not_registered():
    """withdraw() vacates the slot but leaves workerIds populated."""
    registry, registration, _ = make_registry()
    registration.functions.workerIds.return_value.call.return_value = 5
    registration.functions.getWorker.return_value.call.return_value = worker_tuple(0)

    assert registry.is_registered(PEER) is False


def test_bond_amount_reads_the_contract():
    registry, registration, _ = make_registry()
    registration.functions.bondAmount.return_value.call.return_value = 10**23

    assert registry.bond_amount() == 10**23


def test_token_address_comes_from_the_registry_and_is_cached():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN

    assert registry.token() is token
    assert registry.token() is token
    registration.functions.SQD.return_value.call.assert_called_once()


def test_balance_and_allowance_use_the_signing_address():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN
    token.functions.balanceOf.return_value.call.return_value = 500
    token.functions.allowance.return_value.call.return_value = 100

    assert registry.sqd_balance() == 500
    assert registry.allowance() == 100
    token.functions.balanceOf.assert_called_once_with(ADDRESS)
    token.functions.allowance.assert_called_once_with(ADDRESS, registry.contract.address)


def test_build_register_passes_metadata_nonce_chain_id_and_gas():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    tx = registry.build_register(
        peer_bytes=PEER,
        metadata=METADATA,
        nonce=5,
        fees={"maxFeePerGas": 200, "maxPriorityFeePerGas": 10},
        gas=123456,
    )

    assert tx["nonce"] == 5
    assert tx["gas"] == 123456
    assert tx["chainId"] == 42161
    assert tx["from"] == ADDRESS
    assert tx["maxFeePerGas"] == 200
    registration.functions.register.assert_called_once_with(PEER, METADATA)


def test_build_register_uses_empty_metadata_for_unnamed_workers():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    registry.build_register(
        peer_bytes=PEER, metadata="", nonce=1, fees={}, gas=1
    )

    registration.functions.register.assert_called_once_with(PEER, "")


def test_build_approve_targets_the_registry_as_spender():
    registry, registration, token = make_registry()
    registration.functions.SQD.return_value.call.return_value = TOKEN
    token.functions.approve.return_value.build_transaction.side_effect = (
        lambda params: dict(params)
    )

    tx = registry.build_approve(
        999, nonce=3, fees={"maxFeePerGas": 200, "maxPriorityFeePerGas": 10}
    )

    assert tx["nonce"] == 3
    token.functions.approve.assert_called_once_with(registry.contract.address, 999)


def test_estimate_register_gas_returns_exact_estimate():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.estimate_gas.return_value = 210000

    assert registry.estimate_register_gas(PEER, METADATA) == (210000, True)


def test_estimate_register_gas_falls_back_when_estimation_reverts():
    registry, registration, _ = make_registry()
    registration.functions.register.return_value.estimate_gas.side_effect = Exception(
        "execution reverted: ERC20: insufficient allowance"
    )

    assert registry.estimate_register_gas(PEER, METADATA) == (
        FALLBACK_REGISTER_GAS,
        False,
    )
