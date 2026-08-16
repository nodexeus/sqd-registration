"""Classifying who registered a worker.

The answer decides whether deregister() and withdraw() can be sent directly or
must be wrapped in a vesting contract's execute(), so a wrong classification
sends an operator down the wrong path entirely.
"""

from unittest.mock import MagicMock

from tools.owners import classify_owner

VESTING = "0xB35728D533Ea887862b9Ed00cfe2B7F3D36A4e71"
BENEFICIARY = "0xA205c6e35e0814B0A602b016B539E819807f27F3"


def w3_with(code: bytes, responses: dict):
    """A chain where the address has `code` and answers `responses` by name."""
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    w3.eth.get_code.return_value = code

    contract = MagicMock()

    def fn(name):
        holder = MagicMock()
        if name in responses:
            holder.return_value.call.return_value = responses[name]
        else:
            holder.return_value.call.side_effect = Exception("no such function")
        return holder

    contract.functions.owner = fn("owner")
    contract.functions.beneficiary = fn("beneficiary")
    contract.functions.expectedTotalAmount = fn("expectedTotalAmount")
    contract.functions.depositedIntoProtocol = fn("depositedIntoProtocol")
    w3.eth.contract.return_value = contract
    return w3


def test_an_address_with_no_code_is_a_wallet():
    kind, controller = classify_owner(w3_with(b"", {}), VESTING)

    assert kind == "eoa"
    assert controller == VESTING


def test_a_vesting_contract_is_identified_by_its_own_fields():
    """Probed by call, not bytecode hash, so a redeploy still matches."""
    w3 = w3_with(
        b"\x60" * 100,
        {"owner": BENEFICIARY, "expectedTotalAmount": 19 * 10**24},
    )

    kind, controller = classify_owner(w3, VESTING)

    assert kind == "vesting"
    assert controller == BENEFICIARY


def test_beneficiary_is_used_when_owner_is_absent():
    w3 = w3_with(
        b"\x60" * 100,
        {"beneficiary": BENEFICIARY, "depositedIntoProtocol": 0},
    )

    kind, controller = classify_owner(w3, VESTING)

    assert kind == "vesting"
    assert controller == BENEFICIARY


def test_another_kind_of_contract_is_not_called_vesting():
    """A Safe holding workers needs its own path, so guessing 'vesting' would
    send the operator down the wrong one."""
    w3 = w3_with(b"\x60" * 100, {"owner": BENEFICIARY})

    kind, controller = classify_owner(w3, VESTING)

    assert kind == "contract"
    assert controller == BENEFICIARY


def test_a_zero_creator_is_a_vacated_slot():
    """withdraw() deletes the worker, zeroing its creator."""
    kind, controller = classify_owner(MagicMock(), "0x" + "0" * 40)

    assert kind == "vacated"
    assert controller is None
