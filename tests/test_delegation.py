"""Reading delegated stake per worker.

The number this reports is what a migration forfeits, so an operator quotes it
to a customer. Two ways it could mislead: a chunk boundary silently dropping
workers from the total, or a Multicall3-less chain returning nothing at all
rather than falling back.
"""

from unittest.mock import MagicMock

from tools import delegation

STAKING = "0xB31a0D39D2C69Ed4B28d96E12cbf52C5f9Ac9a51"
ROUTER = "0x67F56D27dab93eEb07f6372274aCa277F49dA941"


def w3_with_multicall(results_by_chunk, code=b"\x60\x80\x60\x40"):
    """A chain whose Multicall3 returns each prepared chunk in turn."""
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    w3.eth.get_code.return_value = code

    multicall = MagicMock()
    calls = iter(results_by_chunk)
    multicall.functions.aggregate3.return_value.call.side_effect = lambda: [
        (True, v.to_bytes(32, "big")) for v in next(calls)
    ]
    w3.eth.contract.return_value = multicall
    return w3


def contract_stub():
    contract = MagicMock()
    contract.encode_abi.side_effect = lambda fn, args: b"data"
    return contract


def test_every_worker_is_counted_across_chunk_boundaries(monkeypatch):
    """A chunk boundary is where a total quietly loses workers."""
    monkeypatch.setattr(delegation, "CHUNK", 2)
    w3 = w3_with_multicall([[10, 20], [30, 40], [50]])

    out = delegation.batched_call(
        w3, STAKING, contract_stub(), "delegated", [1, 2, 3, 4, 5], "read"
    )

    assert out == [10, 20, 30, 40, 50]
    assert sum(out) == 150


def test_results_stay_aligned_with_their_inputs(monkeypatch):
    """Order matters: the caller zips these back onto worker ids."""
    monkeypatch.setattr(delegation, "CHUNK", 2)
    w3 = w3_with_multicall([[7, 8], [9]])

    assert delegation.batched_call(
        w3, STAKING, contract_stub(), "delegated", [101, 102, 103], "read"
    ) == [7, 8, 9]


def test_a_chain_without_multicall_falls_back_to_single_calls():
    """Slow beats reporting zero delegation on a chain that has some."""
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    w3.eth.get_code.return_value = b""  # no Multicall3 deployed

    contract = MagicMock()
    contract.functions.delegated.return_value.call.side_effect = [11, 22, 33]

    assert delegation.batched_call(
        w3, STAKING, contract, "delegated", [1, 2, 3], "read"
    ) == [11, 22, 33]


def test_a_reverted_call_stops_rather_than_counting_as_zero(monkeypatch):
    """Zero is a legitimate delegation, so a failure must not look like one."""
    monkeypatch.setattr(delegation, "CHUNK", 8)
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    w3.eth.get_code.return_value = b"\x60\x80\x60\x40"
    multicall = MagicMock()
    multicall.functions.aggregate3.return_value.call.return_value = [
        (True, (5).to_bytes(32, "big")),
        (False, b""),
    ]
    w3.eth.contract.return_value = multicall

    try:
        delegation.batched_call(
            w3, STAKING, contract_stub(), "delegated", [1, 2], "read"
        )
    except SystemExit as exc:
        assert "reverted" in str(exc)
    else:
        raise AssertionError("a reverted call should stop the run")


def test_staking_is_discovered_through_the_router():
    """Asking the chain keeps this working across networks without another
    hardcoded address to maintain."""
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v

    registration, router = MagicMock(), MagicMock()
    registration.functions.router.return_value.call.return_value = ROUTER
    router.functions.staking.return_value.call.return_value = STAKING
    w3.eth.contract.side_effect = [registration, router]

    network = MagicMock()
    network.worker_registration = "0x36e2b147db67e76ab67a4d07c293670ebefcae4e"

    assert delegation.staking_address(w3, network) == STAKING
