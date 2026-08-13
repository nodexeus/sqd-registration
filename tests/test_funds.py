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


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retry backoff must not slow the suite down."""
    monkeypatch.setattr(bulk_register.time, "sleep", lambda _seconds: None)


def test_read_rpc_retries_a_transient_failure(capsys):
    call = MagicMock(side_effect=[ConnectionError("429 too many requests"), 7])

    assert bulk_register.read_rpc(call, what="probe") == 7
    assert call.call_count == 2
    assert "retrying" in capsys.readouterr().err


def test_read_rpc_fails_cleanly_once_retries_are_exhausted(capsys):
    call = MagicMock(side_effect=ConnectionError("502 bad gateway"))

    with pytest.raises(SystemExit) as exc:
        bulk_register.read_rpc(call, what="probe")

    assert exc.value.code == 2
    assert call.call_count == bulk_register.RPC_ATTEMPTS
    assert "probe failed after" in capsys.readouterr().err


def test_read_rpc_passes_arguments_through():
    call = MagicMock(return_value=True)

    bulk_register.read_rpc(call, b"peer", what="probe")

    call.assert_called_once_with(b"peer")


def test_a_transient_read_failure_does_not_abort_the_funds_check():
    registry = registry_with(balance=BOND * 5, allowance=BOND * 5)
    registry.bond_amount.side_effect = [ConnectionError("429"), BOND]

    assert bulk_register.check_funds(registry, count=2).required == BOND * 2


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


FLOOR = bulk_register.MIN_PRIORITY_FEE_WEI


def test_current_fees_doubles_base_fee_and_adds_priority():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.max_priority_fee = FLOOR * 3

    assert bulk_register.current_fees(w3) == {
        "maxFeePerGas": 200 + FLOOR * 3,
        "maxPriorityFeePerGas": FLOOR * 3,
    }


def test_current_fees_tolerates_a_chain_without_base_fee():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {}
    w3.eth.max_priority_fee = FLOOR * 3

    assert bulk_register.current_fees(w3)["maxFeePerGas"] == FLOOR * 3


def test_current_fees_floors_a_zero_priority_fee():
    """Arbitrum suggests 0, which would leave the transaction with no tip."""
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"baseFeePerGas": 100}
    w3.eth.max_priority_fee = 0

    fees = bulk_register.current_fees(w3)

    assert fees["maxPriorityFeePerGas"] == FLOOR
    assert fees["maxFeePerGas"] == 200 + FLOOR


def test_refreshed_fees_keeps_the_previous_cap_when_the_read_fails(capsys):
    w3 = MagicMock()
    w3.eth.get_block.side_effect = ConnectionError("rpc down")
    previous = {"maxFeePerGas": 7, "maxPriorityFeePerGas": 1}

    assert bulk_register.refreshed_fees(w3, previous) == previous
    assert "could not refresh" in capsys.readouterr().err


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


def test_gas_limit_prefers_longest_metadata_by_byte_length_not_codepoints():
    """Verify gas_limit_for uses UTF-8 byte length, not Unicode codepoint count.

    When comparing metadata lengths:
    - Item A: "a" * 25 = 25 ASCII characters = 25 UTF-8 bytes
    - Item B: "你" * 10 = 10 CJK characters = 30 UTF-8 bytes (3 bytes each)

    By codepoints: A (25) > B (10) — current buggy code picks A
    By bytes: B (30) > A (25) — correct code picks B

    The test verifies the function calls estimate_register_gas with B's data.
    """
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (200000, True)
    work = [
        item("a", "a" * 25),  # 25 codepoints, 25 bytes
        item("b", "你" * 10),  # 10 codepoints, 30 bytes
        item("c", ""),
    ]

    gas, exact = bulk_register.gas_limit_for(registry, work)

    # Should estimate against item "b" (30 bytes is longest), not "a" (25 codepoints is longest)
    registry.estimate_register_gas.assert_called_once_with(
        b"b", "你" * 10
    )
    assert gas == 250000


def test_gas_limit_fails_on_empty_work_list(capsys):
    with pytest.raises(SystemExit) as exc:
        bulk_register.gas_limit_for(MagicMock(), [])

    assert exc.value.code == 2
    assert "no peers" in capsys.readouterr().err


def test_gas_limit_truncates_buffer_calculation():
    """Verify buffer padding uses integer division and doesn't overflow.

    Estimate: 333333, Buffer: 25%
    Calculation: 333333 + (333333 * 25 // 100) = 333333 + 83333 = 416666
    """
    registry = MagicMock()
    registry.estimate_register_gas.return_value = (333333, True)

    gas, exact = bulk_register.gas_limit_for(registry, [item("a", "")])

    assert gas == 416666


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
