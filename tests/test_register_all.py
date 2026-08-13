from unittest.mock import MagicMock

from web3.exceptions import TimeExhausted

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry
from sqdreg.runlog import FAILED, PENDING, SUCCESS, RunLog

FEES = {"maxFeePerGas": 200, "maxPriorityFeePerGas": 10}


def work(*names):
    items = []
    for index, name in enumerate(names, start=1):
        entry = PeerEntry(
            peer_id=name, peer_bytes=name.encode(), name=None, index=index
        )
        items.append(
            NamedPeer(entry=entry, name=f"worker-{name}", metadata=f'{{"name":"{name}"}}')
        )
    return items


def make_env(receipts, start_nonce=5):
    """Build (w3, account, registry) whose receipts follow `receipts`.

    Each entry is either a status int (1 success, 0 revert) or an exception
    instance to raise from wait_for_transaction_receipt.
    """
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = start_nonce
    w3.eth.send_raw_transaction.side_effect = [
        MagicMock(hex=lambda i=i: f"0x{i:02x}") for i in range(len(receipts))
    ]

    def receipt(_tx_hash, timeout=None):
        entry = receipts.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return {"status": entry, "gasUsed": 100, "blockNumber": 1000}

    w3.eth.wait_for_transaction_receipt.side_effect = receipt

    account = MagicMock()
    account.address = "0x0000000000000000000000000000000000000001"
    registry = MagicMock()
    registry.build_register.side_effect = lambda **kwargs: {"nonce": kwargs["nonce"]}
    return w3, account, registry


def test_all_successes_are_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed, result.pending) == (2, 0, 0)
    assert result.gas_used == 200
    assert result.aborted is None
    assert [r.status for r in log.records()] == [SUCCESS, SUCCESS]


def test_the_resolved_name_is_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])

    bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert log.records()[0].name == "worker-a"


def test_metadata_is_passed_to_the_builder(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])

    bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert registry.build_register.call_args.kwargs["metadata"] == '{"name":"a"}'


def test_nonces_increment_by_one_per_transaction(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1, 1, 1], start_nonce=7)

    bulk_register.register_all(
        w3, account, registry, work("a", "b", "c"), log, FEES, gas=300000
    )

    nonces = [c.kwargs["nonce"] for c in registry.build_register.call_args_list]
    assert nonces == [7, 8, 9]


def test_a_revert_is_logged_and_the_run_continues(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (1, 1)
    assert [r.status for r in log.records()] == [FAILED, SUCCESS]
    assert result.aborted is None
    # A revert still costs gas, and gas_used accumulates before the
    # success/revert branch runs, so a revert's gasUsed must be counted too.
    assert result.gas_used == 200


def test_a_send_failure_mid_run_frees_the_nonce_for_the_next_attempt(tmp_path):
    """A send failure never reaches the mempool, so its nonce must be reused.

    Regression test: `test_send_failure_is_logged_as_failed` only has one work
    item, so there is no second `build_register` call whose nonce could be
    checked. Without this test, moving `nonce += 1` into the send-failure
    except branch would silently skip a nonce and strand every subsequent
    transaction in the run, and the rest of the suite would still pass.
    """
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1, 1], start_nonce=5)
    w3.eth.send_raw_transaction.side_effect = [
        ValueError("boom"),
        MagicMock(hex=lambda: "0x01"),
        MagicMock(hex=lambda: "0x02"),
    ]

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b", "c"), log, FEES, gas=300000
    )

    nonces = [c.kwargs["nonce"] for c in registry.build_register.call_args_list]
    # a's send fails at nonce 5, so it stays free; b reuses nonce 5 and
    # succeeds; only then does c move on to nonce 6.
    assert nonces == [5, 5, 6]
    assert (result.registered, result.failed) == (2, 1)


def test_a_non_timeout_receipt_error_records_pending_and_aborts(tmp_path):
    """Any receipt-lookup failure, not just a timeout, must not lose the hash.

    The transaction was broadcast, so its outcome is unknown but its nonce is
    consumed either way. Only `wait_for`'s real hash can ever resolve it, so a
    connection error or RPC 502 must be handled exactly like a timeout: log
    `pending` with that hash and stop the run.
    """
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([ConnectionError("connection reset"), 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.pending, result.registered) == (1, 0)
    assert result.aborted is not None
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    assert records[0].tx_hash == "0x00"


def test_three_consecutive_failures_abort(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 0, 0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b", "c", "d"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (0, 3)
    assert "consecutive" in result.aborted
    assert len(log.records()) == 3


def test_a_success_resets_the_failure_counter(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([0, 0, 1, 0, 0, 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b", "c", "d", "e", "f"), log, FEES, gas=300000
    )

    assert (result.registered, result.failed) == (2, 4)
    assert result.aborted is None


def test_receipt_timeout_records_pending_and_aborts(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([TimeExhausted("too slow"), 1])

    result = bulk_register.register_all(
        w3, account, registry, work("a", "b"), log, FEES, gas=300000
    )

    assert (result.pending, result.registered) == (1, 0)
    assert "timed out" in result.aborted
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    # The hash must survive the timeout — it is the only way to resolve the
    # transaction later.
    assert records[0].tx_hash == "0x00"


def test_send_failure_is_logged_as_failed(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, account, registry = make_env([1])
    w3.eth.send_raw_transaction.side_effect = ValueError("nonce too low")

    result = bulk_register.register_all(
        w3, account, registry, work("a"), log, FEES, gas=300000
    )

    assert result.failed == 1
    assert log.records()[0].status == FAILED
    assert "nonce too low" in log.records()[0].error
