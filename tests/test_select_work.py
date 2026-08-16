from unittest.mock import MagicMock

import pytest

import bulk_register
from sqdreg.peerids import PeerEntry
from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog


def prepared(*names):
    """Input entries. select_work filters entries; naming happens after."""
    return [
        PeerEntry(peer_id=name, peer_bytes=name.encode(), name=None, index=index)
        for index, name in enumerate(names, start=1)
    ]


def registry_with(registered=(), foreign=()):
    """A registry where `registered` are live and `foreign` belong elsewhere."""
    from sqdreg.registry import FOREIGN, REGISTERED, UNREGISTERED

    registry = MagicMock()
    live, theirs = set(registered), set(foreign)
    registry.owned_worker_ids.return_value = set()

    def state(raw, _owned):
        name = raw.decode()
        if name in live:
            return REGISTERED
        if name in theirs:
            return FOREIGN
        return UNREGISTERED

    registry.registration_state.side_effect = state
    registry.is_registered.side_effect = lambda raw: raw.decode() in live
    return registry


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(bulk_register.time, "sleep", lambda _seconds: None)


def test_a_transient_lookup_failure_is_retried(tmp_path, capsys):
    """600 reads against a public endpoint will hit a 429 sooner or later."""
    log = RunLog(tmp_path / "run.jsonl")
    registry = MagicMock()
    registry.registration_state.side_effect = [
        ConnectionError("429"), "unregistered", "unregistered"
    ]

    work, _, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry, None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]
    assert "retrying" in capsys.readouterr().err


def test_a_persistent_lookup_failure_exits_two(tmp_path, capsys):
    log = RunLog(tmp_path / "run.jsonl")
    registry = MagicMock()
    registry.registration_state.side_effect = ConnectionError("502 bad gateway")

    with pytest.raises(SystemExit) as exc:
        bulk_register.select_work(prepared("a"), log, registry, None, "mainnet")

    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_returns_everything_when_nothing_is_registered(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]
    assert logged == []
    assert onchain == []


def test_skips_peers_already_registered_onchain(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain, _foreign = bulk_register.select_work(
        prepared("a", "b", "c"), log, registry_with(registered=["b"]), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "c"]
    assert onchain == ["b"]
    assert logged == []


def test_skips_peers_logged_as_successful_without_an_onchain_read(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS))
    registry = registry_with()

    work, logged, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry, None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert logged == ["a"]
    registry.registration_state.assert_called_once_with(b"b", set())


def test_failed_and_pending_log_entries_do_not_skip(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=FAILED, error="reverted"))
    log.append(Record(peer_id="b", status=PENDING, tx_hash="0xabc"))

    work, logged, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]
    assert logged == []


def test_limit_applies_after_the_skip_filters(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, onchain, _foreign = bulk_register.select_work(
        prepared("a", "b", "c", "d"), log, registry_with(registered=["a", "b"]), 2, "mainnet"
    )

    assert [w.peer_id for w in work] == ["c", "d"]
    assert onchain == ["a", "b"]


def test_limit_stops_the_onchain_scan_early(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    registry = registry_with()

    work, _, _, _foreign = bulk_register.select_work(
        prepared("a", "b", "c", "d", "e"), log, registry, 2, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]
    assert registry.registration_state.call_count == 2


def test_limit_larger_than_the_actionable_set_clamps(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), 99, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]


def test_limit_preserves_file_order_across_sequential_runs(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    everything = prepared("a", "b", "c", "d")

    first, _, _, _foreign = bulk_register.select_work(
        everything, log, registry_with(), 2, "mainnet"
    )
    for item in first:
        log.append(Record(peer_id=item.peer_id, status=SUCCESS))
    second, _, _, _foreign = bulk_register.select_work(
        everything, log, registry_with(), 2, "mainnet"
    )

    assert [w.peer_id for w in first] == ["a", "b"]
    assert [w.peer_id for w in second] == ["c", "d"]


def test_a_success_logged_for_another_network_is_not_skipped(tmp_path):
    """A tethys rehearsal must not remove work from a mainnet run.

    The log lives next to the input file and the same file is rehearsed on
    tethys, so an unscoped skip filter would report "nothing to register" on
    mainnet with nothing actually registered.
    """
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS, network="tethys"))

    work, logged, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a", "b"]
    assert logged == []


def test_a_success_logged_for_this_network_is_skipped(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS, network="mainnet"))

    work, logged, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert logged == ["a"]


def test_a_legacy_success_without_a_network_is_still_skipped(tmp_path):
    """Back-compat: a log written before the network field keeps resuming."""
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS))

    work, logged, _, _foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert logged == ["a"]


def test_a_slot_vacated_by_another_account_is_not_offered(tmp_path):
    """withdraw() leaves workerIds populated, so such a peer ID looks free.

    register() requires ownedWorkers[msg.sender] to contain that worker and
    reverts otherwise, so offering it would spend gas on a certain revert and
    inflate the bond the funds check demands.
    """
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain, foreign = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(foreign=["a"]), None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert foreign == ["a"]
    assert onchain == [] and logged == []


def test_a_slot_this_account_vacated_is_still_offered(tmp_path):
    """The case the two-read check exists for: our own withdrawn peer ID."""
    from sqdreg.registry import UNREGISTERED

    log = RunLog(tmp_path / "run.jsonl")
    registry = MagicMock()
    registry.owned_worker_ids.return_value = {7}
    registry.registration_state.side_effect = lambda _raw, _owned: UNREGISTERED

    work, _l, onchain, foreign = bulk_register.select_work(
        prepared("a"), log, registry, None, "mainnet"
    )

    assert [w.peer_id for w in work] == ["a"]
    assert onchain == [] and foreign == []


def test_owned_workers_is_read_once_not_per_peer(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    registry = registry_with()

    bulk_register.select_work(
        prepared(*[str(i) for i in range(20)]), log, registry, None, "mainnet"
    )

    registry.owned_worker_ids.assert_called_once()
