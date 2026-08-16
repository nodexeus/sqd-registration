import json
from itertools import count
from unittest.mock import MagicMock

import pytest
from web3.exceptions import ProviderConnectionError, TimeExhausted

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
    """Build (w3, signer, registry) whose receipts follow `receipts`.

    Each entry is either a status int (1 success, 0 revert) or an exception
    instance to raise from wait_for_transaction_receipt (BaseException, so a
    KeyboardInterrupt can be simulated too).

    Transaction hashes come from the signer, because the code derives them from
    the signed payload rather than from the send's return value: 0x00 for the
    first transaction signed, 0x01 for the second, and so on.
    """
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = start_nonce

    def receipt(_tx_hash, timeout=None):
        entry = receipts.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return {"status": entry, "gasUsed": 100, "blockNumber": 1000}

    w3.eth.wait_for_transaction_receipt.side_effect = receipt

    account = MagicMock()
    account.address = "0x0000000000000000000000000000000000000001"
    signed = count()
    account.sign_transaction.side_effect = lambda _tx: MagicMock(
        raw_transaction=b"raw",
        hash=MagicMock(hex=lambda i=next(signed): f"0x{i:02x}"),
    )

    registry = MagicMock()
    registry.build_register.side_effect = lambda **kwargs: {"nonce": kwargs["nonce"]}
    # The real LocalSigner, so these tests cover the actual signing path rather
    # than a stand-in for it.
    return w3, bulk_register.LocalSigner(account), registry


def test_all_successes_are_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1, 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.registered, result.failed, result.pending) == (2, 0, 0)
    assert result.gas_used == 200
    assert result.aborted is None
    assert [r.status for r in log.records()] == [SUCCESS, SUCCESS]


def test_the_resolved_name_is_logged(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert log.records()[0].name == "worker-a"


def test_metadata_is_passed_to_the_builder(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert registry.build_register.call_args.kwargs["metadata"] == '{"name":"a"}'


def test_nonces_increment_by_one_per_transaction(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1, 1, 1], start_nonce=7)

    bulk_register.register_all(
        w3, signer, registry, work("a", "b", "c"), log, FEES, gas=300000, network="mainnet"
    )

    nonces = [c.kwargs["nonce"] for c in registry.build_register.call_args_list]
    assert nonces == [7, 8, 9]


def test_a_revert_is_logged_and_the_run_continues(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([0, 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
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
    w3, signer, registry = make_env([1, 1], start_nonce=5)
    w3.eth.send_raw_transaction.side_effect = [
        ValueError("boom"),
        MagicMock(hex=lambda: "0x01"),
        MagicMock(hex=lambda: "0x02"),
    ]

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b", "c"), log, FEES, gas=300000, network="mainnet"
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
    w3, signer, registry = make_env([ConnectionError("connection reset"), 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.pending, result.registered) == (1, 0)
    assert result.aborted is not None
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    assert records[0].tx_hash == "0x00"


def test_three_consecutive_failures_abort(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([0, 0, 0, 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b", "c", "d"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.registered, result.failed) == (0, 3)
    assert "consecutive" in result.aborted
    assert len(log.records()) == 3


def test_a_success_resets_the_failure_counter(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([0, 0, 1, 0, 0, 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b", "c", "d", "e", "f"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.registered, result.failed) == (2, 4)
    assert result.aborted is None


def test_receipt_timeout_records_pending_and_aborts(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([TimeExhausted("too slow"), 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.pending, result.registered) == (1, 0)
    assert "timed out" in result.aborted
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    # The hash must survive the timeout — it is the only way to resolve the
    # transaction later.
    assert records[0].tx_hash == "0x00"


@pytest.mark.parametrize(
    "exc,unknown",
    [
        (ConnectionError("reset"), True),
        (TimeoutError("read timeout"), True),
        (OSError("broken pipe"), True),
        (ProviderConnectionError("no route"), True),
        (ValueError("execution reverted"), False),
        # A JSONDecodeError is a ValueError, so it reads like a rejection — but
        # an undecodable reply says nothing about whether the node accepted the
        # transaction.
        (json.JSONDecodeError("Expecting value", "<html>502</html>", 0), True),
    ],
)
def test_transport_errors_are_the_ones_with_an_unknown_outcome(exc, unknown):
    assert bulk_register.is_transport_error(exc) is unknown


def test_an_html_error_body_behind_http_200_is_treated_as_unknown():
    """Proves the real web3 decode path, not a synthetic exception.

    web3 calls raise_for_status() before decoding, so a non-200 never reaches
    the decoder. A proxy that returns HTTP 200 with an HTML error body does,
    and misclassifying that as a rejection would log FAILED for a transaction
    the node may have accepted.
    """
    from web3._utils.encoding import FriendlyJsonSerde

    with pytest.raises(json.JSONDecodeError) as raised:
        FriendlyJsonSerde().json_decode("<html>502 Bad Gateway</html>")

    assert isinstance(raised.value, ValueError)  # why it was misread before
    assert bulk_register.is_transport_error(raised.value) is True


def test_an_undecodable_reply_records_pending_with_the_hash_and_aborts(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1, 1])
    w3.eth.send_raw_transaction.side_effect = json.JSONDecodeError(
        "Expecting value", "<html>502</html>", 0
    )

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.pending, result.registered, result.failed) == (1, 0, 0)
    assert result.aborted
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    # The hash is the only route to resolving it, so it must be recorded.
    assert records[0].tx_hash


def test_send_and_wait_reports_the_hash_when_the_send_fails():
    w3, signer, _ = make_env([])
    w3.eth.send_raw_transaction.side_effect = ConnectionError("reset")

    with pytest.raises(bulk_register.SendFailed) as exc:
        bulk_register.send_and_wait(w3, signer, {"nonce": 1}, label="approval")

    assert exc.value.tx_hash == "0x00"
    assert "reset" in str(exc.value)


def test_send_and_wait_reports_the_hash_when_the_receipt_fails(capsys):
    w3, signer, _ = make_env([TimeExhausted("too slow")])

    with pytest.raises(bulk_register.SendFailed) as exc:
        bulk_register.send_and_wait(w3, signer, {"nonce": 1}, label="approval")

    assert exc.value.tx_hash == "0x00"
    # The hash is printed as soon as it is broadcast, so even a Ctrl-C during
    # the wait leaves the operator holding it.
    assert "0x00" in capsys.readouterr().out


def test_a_rejected_send_keeps_the_hash_but_stays_failed(tmp_path):
    """A JSON-RPC rejection is a real failure, but must still be traceable."""
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1])
    w3.eth.send_raw_transaction.side_effect = ValueError("nonce too low")

    result = bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert result.failed == 1
    assert log.records()[0].status == FAILED
    assert log.records()[0].tx_hash == "0x00"


def test_a_signing_failure_is_failed_with_no_hash(tmp_path):
    """Signing never reaches the node, so the nonce stays free and it is failed."""
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1, 1])
    signer.account.sign_transaction.side_effect = ValueError("bad transaction fields")

    result = bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.failed, result.pending) == (1, 0)
    assert log.records()[0].status == FAILED
    assert log.records()[0].tx_hash is None
    w3.eth.send_raw_transaction.assert_not_called()


def test_a_transport_level_send_failure_is_pending_not_failed(tmp_path):
    """A dropped connection does not prove the transaction was refused.

    The node may have accepted the raw transaction and failed only when
    replying, so the nonce may be consumed and the peer may in fact be
    registered. Claiming `failed` would make the log actively wrong about a real
    registration, which is the one thing the log exists to prevent.
    """
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1, 1])
    w3.eth.send_raw_transaction.side_effect = ConnectionError("connection reset")

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert (result.pending, result.failed, result.registered) == (1, 0, 0)
    assert result.aborted is not None
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    assert records[0].tx_hash == "0x00"
    assert "connection reset" in records[0].error


def test_a_pending_record_persists_the_reason(tmp_path):
    """`pending` is the hardest state to diagnose, so it must carry the why."""
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([TimeExhausted("too slow")])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert "too slow" in log.records()[0].error


def test_a_non_timeout_pending_record_persists_the_reason(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([ConnectionError("rpc 502")])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert "rpc 502" in log.records()[0].error


def test_ctrl_c_during_the_receipt_wait_keeps_the_hash(tmp_path):
    """Ctrl-C must not lose the hash of a broadcast, unresolved transaction."""
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([KeyboardInterrupt(), 1])

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log, FEES, gas=300000, network="mainnet"
    )

    assert result.interrupted is True
    assert result.pending == 1
    records = log.records()
    assert len(records) == 1
    assert records[0].status == PENDING
    assert records[0].tx_hash == "0x00"
    assert "interrupted" in records[0].error


def test_ctrl_c_during_the_send_keeps_the_hash(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1])
    w3.eth.send_raw_transaction.side_effect = KeyboardInterrupt()

    result = bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert result.interrupted is True
    assert log.records()[0].status == PENDING
    assert log.records()[0].tx_hash == "0x00"


def test_fees_are_refreshed_during_a_long_run(tmp_path, monkeypatch):
    """One fee read cannot cover a 15-30 minute run.

    maxFeePerGas is only 2x the base fee at the moment it was read, so a
    sustained rise past the cap would leave every later transaction unmined
    until its receipt wait timed out.
    """
    interval = bulk_register.FEE_REFRESH_INTERVAL
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1] * (interval + 1))
    refreshed = {"maxFeePerGas": 999, "maxPriorityFeePerGas": 99}
    monkeypatch.setattr(bulk_register, "current_fees", lambda _w3: refreshed)

    bulk_register.register_all(
        w3,
        signer,
        registry,
        work(*[f"p{i}" for i in range(interval + 1)]),
        log,
        FEES,
        gas=300000,
        network="mainnet",
    )

    used = [c.kwargs["fees"] for c in registry.build_register.call_args_list]
    assert used[0] == FEES
    assert used[interval - 1] == FEES
    assert used[interval] == refreshed


def test_send_failure_is_logged_as_failed(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    w3, signer, registry = make_env([1])
    w3.eth.send_raw_transaction.side_effect = ValueError("nonce too low")

    result = bulk_register.register_all(
        w3, signer, registry, work("a"), log, FEES, gas=300000, network="mainnet"
    )

    assert result.failed == 1
    assert log.records()[0].status == FAILED
    assert "nonce too low" in log.records()[0].error
