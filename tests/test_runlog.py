import json

from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog, utc_now


def test_append_then_read_round_trips(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    record = Record(
        peer_id="peer-a", status=SUCCESS, name="worker-1", tx_hash="0xabc", block=42
    )

    log.append(record)

    assert log.records() == [record]


def test_records_is_empty_when_file_absent(tmp_path):
    assert RunLog(tmp_path / "missing.jsonl").records() == []
    assert RunLog(tmp_path / "missing.jsonl").succeeded_peer_ids("mainnet") == set()


def test_append_preserves_existing_records(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="peer-a", status=SUCCESS))
    log.append(Record(peer_id="peer-b", status=FAILED, error="reverted"))

    assert [r.peer_id for r in log.records()] == ["peer-a", "peer-b"]


def test_succeeded_excludes_failed_and_pending(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="ok", status=SUCCESS, network="mainnet"))
    log.append(Record(peer_id="bad", status=FAILED, error="reverted", network="mainnet"))
    log.append(Record(peer_id="slow", status=PENDING, tx_hash="0xdef", network="mainnet"))

    assert log.succeeded_peer_ids("mainnet") == {"ok"}


def test_a_success_on_another_network_does_not_count(tmp_path):
    """A tethys rehearsal must never make a mainnet run think it is done."""
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="rehearsed", status=SUCCESS, network="tethys"))

    assert log.succeeded_peer_ids("mainnet") == set()
    assert log.succeeded_peer_ids("tethys") == {"rehearsed"}


def test_a_record_without_a_network_still_counts(tmp_path):
    """Back-compat: logs written before the network field keep resuming."""
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"peer_id": "legacy", "status": SUCCESS}) + "\n")

    assert RunLog(path).succeeded_peer_ids("mainnet") == {"legacy"}
    assert RunLog(path).succeeded_peer_ids("tethys") == {"legacy"}


def test_the_network_is_persisted(tmp_path):
    path = tmp_path / "run.jsonl"
    RunLog(path).append(Record(peer_id="peer-a", status=SUCCESS, network="mainnet"))

    assert json.loads(path.read_text().splitlines()[0])["network"] == "mainnet"


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"peer_id": "ok", "status": SUCCESS}) + "\n\n")

    assert [r.peer_id for r in RunLog(path).records()] == ["ok"]


def test_the_name_is_persisted(tmp_path):
    path = tmp_path / "run.jsonl"
    log = RunLog(path)
    log.append(Record(peer_id="peer-a", status=SUCCESS, name="worker-1"))

    assert json.loads(path.read_text().splitlines()[0])["name"] == "worker-1"


def test_utc_now_is_iso_with_timezone():
    stamp = utc_now()
    assert "T" in stamp
    assert stamp.endswith("+00:00")
