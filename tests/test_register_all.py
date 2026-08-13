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
