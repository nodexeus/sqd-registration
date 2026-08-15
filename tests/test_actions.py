"""Tests for --action deregister / withdraw / status."""

from unittest.mock import MagicMock

import pytest

import bulk_register
from sqdreg.peerids import PeerEntry
from sqdreg.registry import (
    ACTIVE,
    FOREIGN,
    LOCKED,
    UNREGISTERED,
    WITHDRAWABLE,
    WorkerState,
)
from sqdreg.runlog import SUCCESS, Record, RunLog


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(bulk_register.time, "sleep", lambda _s: None)


def entry(peer_id):
    return PeerEntry(
        peer_id=peer_id, peer_bytes=peer_id.encode(), name=None, index=1
    )


def state(peer_id, kind, bond=10**23, unlock=None, worker_id=7):
    return WorkerState(
        peer_bytes=peer_id.encode(),
        worker_id=0 if kind == UNREGISTERED else worker_id,
        creator="0xcreator",
        bond=bond,
        registered_at=0 if kind == UNREGISTERED else 100,
        deregistered_at=200 if kind in (LOCKED, WITHDRAWABLE) else 0,
        is_active=kind == ACTIVE,
        state=kind,
        unlock_block=unlock,
    )


# --- selection --------------------------------------------------------------


def test_deregister_selects_only_active_workers(tmp_path):
    entries = [entry("a"), entry("b"), entry("c")]
    states = [state("a", ACTIVE), state("b", WITHDRAWABLE), state("c", FOREIGN)]

    work, logged, not_ready = bulk_register.select_by_state(
        entries, states, RunLog(tmp_path / "l.jsonl"), None, "mainnet", "deregister"
    )

    assert [w.peer_id for w in work] == ["a"]
    assert [s.state for _e, s in not_ready] == [WITHDRAWABLE, FOREIGN]
    assert logged == []


def test_withdraw_selects_only_withdrawable_workers(tmp_path):
    entries = [entry("a"), entry("b"), entry("c")]
    states = [
        state("a", ACTIVE),
        state("b", WITHDRAWABLE),
        state("c", LOCKED, unlock=999),
    ]

    work, _logged, _nr = bulk_register.select_by_state(
        entries, states, RunLog(tmp_path / "l.jsonl"), None, "mainnet", "withdraw"
    )

    assert [w.peer_id for w in work] == ["b"]


def test_a_locked_worker_is_never_selected_for_withdraw(tmp_path):
    """The precondition the L1/L2 block-number confusion would have broken."""
    entries = [entry("a")]
    states = [state("a", LOCKED, unlock=10**9)]

    work, _logged, not_ready = bulk_register.select_by_state(
        entries, states, RunLog(tmp_path / "l.jsonl"), None, "mainnet", "withdraw"
    )

    assert work == []
    assert not_ready[0][1].state == LOCKED


def test_a_prior_success_for_the_same_action_is_skipped(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    log.append(
        Record(peer_id="a", status=SUCCESS, network="mainnet", action="deregister")
    )
    entries = [entry("a"), entry("b")]
    states = [state("a", ACTIVE), state("b", ACTIVE)]

    work, logged, _nr = bulk_register.select_by_state(
        entries, states, log, None, "mainnet", "deregister"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert logged == ["a"]


def test_a_register_success_does_not_block_a_deregister(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    log.append(
        Record(peer_id="a", status=SUCCESS, network="mainnet", action="register")
    )

    work, logged, _nr = bulk_register.select_by_state(
        [entry("a")], [state("a", ACTIVE)], log, None, "mainnet", "deregister"
    )

    assert [w.peer_id for w in work] == ["a"]
    assert logged == []


def test_limit_caps_the_actionable_set(tmp_path):
    entries = [entry(c) for c in "abcd"]
    states = [state(c, ACTIVE) for c in "abcd"]

    work, _l, _nr = bulk_register.select_by_state(
        entries, states, RunLog(tmp_path / "l.jsonl"), 2, "mainnet", "deregister"
    )

    assert [w.peer_id for w in work] == ["a", "b"]


# --- the L1 block number ----------------------------------------------------


def test_l1_block_number_prefers_the_l1_field():
    """Solidity's block.number on Arbitrum is L1, not the L2 eth_blockNumber."""
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"number": 494_000_000, "l1BlockNumber": 25_700_000}
    w3.eth.block_number = 494_000_000

    assert bulk_register.l1_block_number(w3) == 25_700_000


def test_l1_block_number_accepts_a_hex_string():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"l1BlockNumber": "0x1890314"}

    assert bulk_register.l1_block_number(w3) == 0x1890314


def test_l1_block_number_falls_back_on_a_non_arbitrum_chain():
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"number": 123}
    w3.eth.block_number = 123

    assert bulk_register.l1_block_number(w3) == 123


# --- transaction building ---------------------------------------------------


def make_env(receipts, action):
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 5
    w3.eth.send_raw_transaction.side_effect = [
        MagicMock(hex=lambda i=i: f"0x{i:02x}") for i in range(len(receipts))
    ]
    w3.eth.wait_for_transaction_receipt.side_effect = [
        {"status": s, "gasUsed": 10, "blockNumber": 1} for s in receipts
    ]
    account = MagicMock()
    account.address = "0x1"
    account.sign_transaction.return_value = MagicMock(
        raw_transaction=b"raw", hash=MagicMock(hex=lambda: "0xhash")
    )
    registry = MagicMock()
    for name in ("build_register", "build_deregister", "build_withdraw"):
        getattr(registry, name).side_effect = lambda **kw: {"nonce": kw["nonce"]}
    return w3, account, registry


def work_items(*ids):
    from sqdreg.naming import NamedPeer

    return [NamedPeer(entry=entry(i), name=None, metadata="") for i in ids]


@pytest.mark.parametrize(
    "action,builder",
    [
        ("deregister", "build_deregister"),
        ("withdraw", "build_withdraw"),
        ("register", "build_register"),
    ],
)
def test_each_action_builds_its_own_transaction(tmp_path, action, builder):
    log = RunLog(tmp_path / "l.jsonl")
    w3, account, registry = make_env([1], action)

    bulk_register.register_all(
        w3, account, registry, work_items("a"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet", action=action,
    )

    assert getattr(registry, builder).call_count == 1
    for other in {"build_register", "build_deregister", "build_withdraw"} - {builder}:
        assert getattr(registry, other).call_count == 0


def test_the_action_is_recorded_in_the_log(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    w3, account, registry = make_env([1], "withdraw")

    bulk_register.register_all(
        w3, account, registry, work_items("a"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet", action="withdraw",
    )

    assert log.records()[0].action == "withdraw"


# --- status mode ------------------------------------------------------------


def status_env(monkeypatch, states, tmp_path):
    registry = MagicMock()
    registry.lock_period.return_value = 100
    registry.owned_worker_ids.return_value = set()
    # Keyed by call order, not peer_bytes: the real call receives the decoded
    # multihash, while these fixtures carry placeholder bytes.
    registry.worker_state.side_effect = list(states)
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"l1BlockNumber": 1000}
    monkeypatch.setattr(bulk_register, "connect", lambda n, r: w3)
    monkeypatch.setattr(bulk_register, "Registry", lambda *a, **k: registry)
    monkeypatch.setattr(
        bulk_register, "load_signer", lambda: pytest.fail("asked for a credential")
    )
    return w3, registry


def peer_file(tmp_path, ids):
    path = tmp_path / "peers.txt"
    path.write_text("\n".join(ids) + "\n")
    return path


def test_status_needs_no_credential_when_given_an_address(
    monkeypatch, tmp_path, capsys
):
    """A read-only report should not require a key at all."""
    import base58

    ids = [
        base58.b58encode(bytes([0x00, 36]) + bytes((s + i) % 256 for i in range(36))).decode()
        for s in range(2)
    ]
    states = [state(i, WITHDRAWABLE, unlock=500) for i in ids]
    status_env(monkeypatch, states, tmp_path)
    path = peer_file(tmp_path, ids)

    code = bulk_register.main(
        [str(path), "--action", "status", "--address", "0x000000000000000000000000000000000000dEaD"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "withdrawable" in out
    assert "dEaD" in out


def test_status_writes_a_csv_and_totals_the_withdrawable_bond(
    monkeypatch, tmp_path, capsys
):
    import base58

    ids = [
        base58.b58encode(bytes([0x00, 36]) + bytes((s + i) % 256 for i in range(36))).decode()
        for s in range(2)
    ]
    states = [
        state(ids[0], WITHDRAWABLE, unlock=500),
        state(ids[1], ACTIVE),
    ]
    status_env(monkeypatch, states, tmp_path)
    path = peer_file(tmp_path, ids)

    bulk_register.main([str(path), "--action", "status", "--address", "0x000000000000000000000000000000000000dEaD"])

    csv_path = tmp_path / "peers.txt.mainnet.status.csv"
    lines = csv_path.read_text().splitlines()
    assert lines[0] == "peer_id,state,worker_id,bond,unlock_block,detail"
    assert len(lines) == 3
    # one worker's bond, not both
    assert "100000 SQD is withdrawable now" in capsys.readouterr().out


def test_status_sends_nothing(monkeypatch, tmp_path):
    import base58

    ids = [base58.b58encode(bytes([0x00, 36]) + bytes(range(36))).decode()]
    w3, _registry = status_env(monkeypatch, [state(ids[0], ACTIVE)], tmp_path)
    path = peer_file(tmp_path, ids)

    bulk_register.main([str(path), "--action", "status", "--address", "0x000000000000000000000000000000000000dEaD"])

    w3.eth.send_raw_transaction.assert_not_called()


# --- targeting specific peer IDs -------------------------------------------


def test_peer_id_narrows_the_file():
    entries = [entry("a"), entry("b"), entry("c")]

    chosen = bulk_register.restrict_to(entries, ["b"])

    assert [e.peer_id for e in chosen] == ["b"]


def test_peer_id_can_be_repeated_and_keeps_file_order():
    entries = [entry("a"), entry("b"), entry("c")]

    chosen = bulk_register.restrict_to(entries, ["c", "a"])

    assert [e.peer_id for e in chosen] == ["a", "c"]


def test_an_unknown_peer_id_is_an_error_not_an_empty_selection(capsys):
    """Selecting nothing reads exactly like 'already done' — too dangerous."""
    with pytest.raises(SystemExit) as exc:
        bulk_register.restrict_to([entry("a")], ["typo"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "typo" in err and "not found" in err


def test_address_is_rejected_for_a_write_action(tmp_path, capsys):
    """The reported bug: a peer ID passed to --address was silently ignored,
    so the run selected every actionable node instead of the one intended."""
    path = tmp_path / "peers.txt"
    path.write_text("whatever\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(
            [str(path), "--action", "deregister", "--address", "12D3KooWabc"]
        )

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--peer-id" in err
    assert "only applies to --action status" in err


def test_a_non_address_is_rejected_even_for_status(tmp_path, capsys):
    path = tmp_path / "peers.txt"
    path.write_text("whatever\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(
            [str(path), "--action", "status", "--address", "12D3KooWabc"]
        )

    assert exc.value.code == 2
    assert "not an Ethereum address" in capsys.readouterr().err


def test_the_resume_hint_carries_peer_id_and_action():
    args = bulk_register.parse_args(
        ["peers.txt", "--action", "withdraw", "--peer-id", "a", "--peer-id", "b"]
    )

    hint = bulk_register.resume_command(args)

    assert "--action withdraw" in hint
    assert hint.count("--peer-id") == 2


# --- acting without an input file -------------------------------------------


def test_peer_id_alone_is_enough(tmp_path, monkeypatch, capsys):
    """No file needed to act on IDs you have already named."""
    import base58

    pid = base58.b58encode(bytes([0x00, 36]) + bytes(range(36))).decode()
    status_env(monkeypatch, [state(pid, ACTIVE)], tmp_path)
    monkeypatch.chdir(tmp_path)

    code = bulk_register.main(
        [
            "--action", "status",
            "--peer-id", pid,
            "--address", "0x000000000000000000000000000000000000dEaD",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "peer IDs:    1" in out  # not "in file"
    assert (tmp_path / "adhoc.mainnet.status.csv").exists()


def test_neither_a_file_nor_a_peer_id_is_an_error():
    with pytest.raises(SystemExit) as exc:
        bulk_register.parse_args(["--action", "status"])

    assert exc.value.code == 2


def test_a_malformed_peer_id_flag_is_rejected_like_a_bad_file_line(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(["--action", "status", "--peer-id", "garbage-0OIl",
                            "--address", "0x000000000000000000000000000000000000dEaD"])

    assert exc.value.code == 2


def test_adhoc_artifacts_do_not_collide_with_a_file_driven_run():
    """An ad-hoc action must not append to the log a bulk run depends on."""
    assert bulk_register.artifact_base(None) == "adhoc"
    assert bulk_register.artifact_base("peers.txt") == "peers.txt"
    assert bulk_register.default_log_path(
        bulk_register.artifact_base(None), "mainnet"
    ) == "adhoc.mainnet.run.jsonl"


# --- the epoch gap after registering ---------------------------------------


def test_a_worker_awaiting_its_epoch_is_not_reported_active():
    """register() sets registeredAt = nextEpoch(), so a worker is not live
    immediately. Calling it active would let a deregister run select it, and
    the contract requires isWorkerActive — it would revert 'Worker not active'.
    """
    from sqdreg.registry import REGISTERING, Registry

    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda v: v
    contract, token = MagicMock(), MagicMock()
    w3.eth.contract.side_effect = [contract, token]
    from sqdreg.networks import NETWORKS

    registry = Registry(w3, NETWORKS["tethys"], "0xowner")
    contract.functions.workerIds.return_value.call.return_value = 250
    contract.functions.getWorker.return_value.call.return_value = [
        "0xowner", b"p", 10**23, 11_495_400, 0, ""
    ]
    contract.functions.isWorkerActive.return_value.call.return_value = False

    st = registry.worker_state(b"p", l1_block=11_495_381, lock_period=99_999, owned=set())

    assert st.state == REGISTERING


def test_a_registering_worker_is_not_selected_for_deregister(tmp_path):
    from sqdreg.registry import REGISTERING

    entries = [entry("a"), entry("b")]
    states = [state("a", REGISTERING), state("b", ACTIVE)]

    work, _logged, not_ready = bulk_register.select_by_state(
        entries, states, RunLog(tmp_path / "l.jsonl"), None, "tethys", "deregister"
    )

    assert [w.peer_id for w in work] == ["b"]
    assert not_ready[0][1].state == REGISTERING
