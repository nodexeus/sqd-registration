from unittest.mock import MagicMock

import pytest

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry

BOND = 10**23  # 100,000 SQD at 18 decimals


def registry_with(bond=BOND, balance=0, allowance=0):
    registry = MagicMock()
    registry.bond_amount.return_value = bond
    registry.sqd_balance.return_value = balance
    registry.allowance.return_value = allowance
    registry.token_decimals.return_value = 18
    return registry


def item(peer_id, metadata):
    entry = PeerEntry(
        peer_id=peer_id, peer_bytes=peer_id.encode(), name=None, index=1
    )
    return NamedPeer(entry=entry, name=None, metadata=metadata)


def test_sufficient_balance_and_allowance_needs_no_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 5, allowance=BOND * 5), count=3
    )

    assert check.required == BOND * 3
    assert check.needs_approval is False


def test_short_allowance_flags_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 5, allowance=BOND), count=3
    )

    assert check.needs_approval is True
    assert check.required == BOND * 3


def test_exact_allowance_needs_no_approval():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 3, allowance=BOND * 3), count=3
    )

    assert check.needs_approval is False


def test_short_balance_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        bulk_register.check_funds(
            registry_with(balance=BOND, allowance=BOND * 9), count=3
        )

    assert exc.value.code == 2
    assert "insufficient SQD" in capsys.readouterr().err


def test_required_uses_the_limited_count_not_the_file_count():
    check = bulk_register.check_funds(
        registry_with(balance=BOND * 500, allowance=0), count=10
    )

    assert check.required == BOND * 10


def test_current_fees_doubles_base_fee_and_adds_priority():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.max_priority_fee = 10

    assert bulk_register.current_fees(w3) == {
        "maxFeePerGas": 210,
        "maxPriorityFeePerGas": 10,
    }


def test_current_fees_tolerates_a_chain_without_base_fee():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {}
    w3.eth.max_priority_fee = 10

    assert bulk_register.current_fees(w3)["maxFeePerGas"] == 10


def test_gas_limit_estimates_against_the_longest_metadata():
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (200000, True)
    work = [
        item("a", '{"name":"short"}'),
        item("b", '{"name":"a-much-longer-worker-name"}'),
        item("c", ""),
    ]

    gas, exact = bulk_register.gas_limit_for(registry, work)

    assert exact is True
    assert gas == 250000  # 200000 + 25%
    registry.estimate_register_gas.assert_called_once_with(
        b"b", '{"name":"a-much-longer-worker-name"}'
    )


def test_gas_limit_reports_an_inexact_estimate():
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (400000, False)

    gas, exact = bulk_register.gas_limit_for(registry, [item("a", "")])

    assert exact is False
    assert gas == 500000


def test_format_units_renders_whole_and_fractional_amounts():
    assert bulk_register.format_units(10**18, 18) == "1"
    assert bulk_register.format_units(BOND, 18) == "100000"
    assert bulk_register.format_units(15 * 10**17, 18) == "1.5"


def test_confirm_returns_true_immediately_when_assume_yes():
    assert bulk_register.confirm("go?", assume_yes=True) is True


@pytest.mark.parametrize(
    "reply,expected",
    [("y", True), ("Y", True), ("yes", True), ("n", False), ("", False)],
)
def test_confirm_reads_stdin(monkeypatch, reply, expected):
    monkeypatch.setattr("builtins.input", lambda _: reply)
    assert bulk_register.confirm("go?", assume_yes=False) is expected
