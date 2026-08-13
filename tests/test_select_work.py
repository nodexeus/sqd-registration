from unittest.mock import MagicMock

import bulk_register
from sqdreg.naming import NamedPeer
from sqdreg.peerids import PeerEntry
from sqdreg.runlog import FAILED, PENDING, SUCCESS, Record, RunLog


def prepared(*names):
    items = []
    for index, name in enumerate(names, start=1):
        entry = PeerEntry(
            peer_id=name, peer_bytes=name.encode(), name=None, index=index
        )
        items.append(NamedPeer(entry=entry, name=None, metadata=""))
    return items


def registry_with(registered=()):
    registry = MagicMock()
    registered = set(registered)
    registry.is_registered.side_effect = lambda raw: raw.decode() in registered
    return registry


def test_returns_everything_when_nothing_is_registered(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert logged == []
    assert onchain == []


def test_skips_peers_already_registered_onchain(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, logged, onchain = bulk_register.select_work(
        prepared("a", "b", "c"), log, registry_with(registered=["b"]), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "c"]
    assert onchain == ["b"]
    assert logged == []


def test_skips_peers_logged_as_successful_without_an_onchain_read(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS))
    registry = registry_with()

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry, None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["b"]
    assert logged == ["a"]
    registry.is_registered.assert_called_once_with(b"b")


def test_failed_and_pending_log_entries_do_not_skip(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=FAILED, error="reverted"))
    log.append(Record(peer_id="b", status=PENDING, tx_hash="0xabc"))

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert logged == []


def test_limit_applies_after_the_skip_filters(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, onchain = bulk_register.select_work(
        prepared("a", "b", "c", "d"), log, registry_with(registered=["a", "b"]), 2, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["c", "d"]
    assert onchain == ["a", "b"]


def test_limit_stops_the_onchain_scan_early(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    registry = registry_with()

    work, _, _ = bulk_register.select_work(
        prepared("a", "b", "c", "d", "e"), log, registry, 2, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert registry.is_registered.call_count == 2


def test_limit_larger_than_the_actionable_set_clamps(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")

    work, _, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), 99, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]


def test_limit_preserves_file_order_across_sequential_runs(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    everything = prepared("a", "b", "c", "d")

    first, _, _ = bulk_register.select_work(
        everything, log, registry_with(), 2, "mainnet"
    )
    for item in first:
        log.append(Record(peer_id=item.entry.peer_id, status=SUCCESS))
    second, _, _ = bulk_register.select_work(
        everything, log, registry_with(), 2, "mainnet"
    )

    assert [w.entry.peer_id for w in first] == ["a", "b"]
    assert [w.entry.peer_id for w in second] == ["c", "d"]


def test_a_success_logged_for_another_network_is_not_skipped(tmp_path):
    """A tethys rehearsal must not remove work from a mainnet run.

    The log lives next to the input file and the same file is rehearsed on
    tethys, so an unscoped skip filter would report "nothing to register" on
    mainnet with nothing actually registered.
    """
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS, network="tethys"))

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["a", "b"]
    assert logged == []


def test_a_success_logged_for_this_network_is_skipped(tmp_path):
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS, network="mainnet"))

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["b"]
    assert logged == ["a"]


def test_a_legacy_success_without_a_network_is_still_skipped(tmp_path):
    """Back-compat: a log written before the network field keeps resuming."""
    log = RunLog(tmp_path / "run.jsonl")
    log.append(Record(peer_id="a", status=SUCCESS))

    work, logged, _ = bulk_register.select_work(
        prepared("a", "b"), log, registry_with(), None, "mainnet"
    )

    assert [w.entry.peer_id for w in work] == ["b"]
    assert logged == ["a"]
