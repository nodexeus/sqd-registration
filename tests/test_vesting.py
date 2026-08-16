"""Routing calls through a holding contract's execute().

A worker registered by a vesting contract has that contract as its `creator`,
and register/deregister/withdraw all check `creator == msg.sender`. The
beneficiary cannot call them directly.
"""

from unittest.mock import MagicMock

import pytest

import bulk_register
from sqdreg.networks import NETWORKS
from sqdreg.registry import DirectCalls, VestingCalls

VESTING = "0xB35728D533Ea887862b9Ed00cfe2B7F3D36A4e71"
BENEFICIARY = "0xA205c6e35e0814B0A602b016B539E819807f27F3"
REGISTRY = "0x36e2b147db67e76ab67a4d07c293670ebefcae4e"


def vesting_calls():
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    contract = MagicMock()
    contract.functions.execute.return_value.build_transaction.side_effect = (
        lambda params: {**params, "to": VESTING, "data": "0xexecute"}
    )
    w3.eth.contract.return_value = contract
    calls = VestingCalls(w3, NETWORKS["mainnet"], VESTING, BENEFICIARY)
    return calls, contract


def inner_tx(**over):
    tx = {
        "to": REGISTRY,
        "data": "0xdeadbeef",
        "from": VESTING,
        "gas": 300_000,
        "nonce": 7,
        "maxFeePerGas": 200,
        "maxPriorityFeePerGas": 10,
        "chainId": 42161,
    }
    tx.update(over)
    return tx


def test_direct_calls_pass_the_transaction_through_untouched():
    tx = inner_tx()

    assert DirectCalls(BENEFICIARY).wrap(tx) == tx


def test_the_inner_call_becomes_execute_arguments():
    calls, contract = vesting_calls()

    calls.wrap(inner_tx(), required_approve=0)

    to, data, approve = contract.functions.execute.call_args.args
    assert to == REGISTRY          # the inner destination
    assert data == bytes.fromhex("deadbeef")   # the inner calldata
    assert approve == 0


def test_the_outer_transaction_is_sent_by_the_beneficiary_to_the_contract():
    """The signer is the beneficiary; the destination is the holding contract."""
    calls, _ = vesting_calls()

    outer = calls.wrap(inner_tx())

    assert outer["from"] == BENEFICIARY
    assert outer["to"] == VESTING


def test_the_nonce_gas_and_fees_carry_over():
    calls, _ = vesting_calls()

    outer = calls.wrap(inner_tx())

    assert outer["nonce"] == 7
    assert outer["gas"] == 300_000
    assert outer["maxFeePerGas"] == 200


def test_a_remote_signer_leaves_the_nonce_off():
    """Fireblocks assigns its own, so the inner transaction carries none."""
    calls, _ = vesting_calls()

    outer = calls.wrap({k: v for k, v in inner_tx().items() if k != "nonce"})

    assert "nonce" not in outer


def test_registration_approves_its_bond_inside_execute():
    """No separate approval, and no allowance left standing afterwards."""
    calls, contract = vesting_calls()
    bond = 100_000 * 10**18

    calls.wrap(inner_tx(), required_approve=bond)

    assert contract.functions.execute.call_args.args[2] == bond


def test_the_other_actions_approve_nothing():
    calls, contract = vesting_calls()

    calls.wrap(inner_tx(), required_approve=0)

    assert contract.functions.execute.call_args.args[2] == 0


# --- who may drive it -------------------------------------------------------


def test_a_signer_that_is_not_the_owner_is_rejected(monkeypatch, tmp_path, capsys):
    """execute() is restricted to owner(); failing early beats reverting."""
    account = MagicMock()
    account.address = "0x000000000000000000000000000000000000dEaD"
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    contract = MagicMock()
    contract.functions.owner.return_value.call.return_value = BENEFICIARY
    w3.eth.contract.return_value = contract

    monkeypatch.setattr(bulk_register, "load_signer", lambda: account)
    monkeypatch.setattr(bulk_register, "connect", lambda n, r: w3)
    monkeypatch.setattr(bulk_register.time, "sleep", lambda _s: None)
    path = tmp_path / "peers.txt"
    path.write_text("")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(
            [str(path), "--action", "status", "--via-vesting", VESTING]
        )

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "only accepts execute() from" in err
    assert BENEFICIARY in err


def test_a_malformed_vesting_address_is_rejected(tmp_path, capsys):
    path = tmp_path / "peers.txt"
    path.write_text("")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--via-vesting", "not-an-address"])

    assert exc.value.code == 2
    assert "not an address" in capsys.readouterr().err
