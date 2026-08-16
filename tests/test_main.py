from unittest.mock import MagicMock, patch

import base58
import pytest

import bulk_register
from sqdreg.runlog import SUCCESS, Record, RunLog

BOND = 10**23


def peer_id_for(seed):
    raw = bytes([0x00, 36]) + bytes((seed + i) % 256 for i in range(36))
    return base58.b58encode(raw).decode()


def make_peer_file(tmp_path, count, names=False):
    lines = []
    for seed in range(count):
        peer_id = peer_id_for(seed)
        lines.append(f"{peer_id},named-{seed}" if names else peer_id)
    path = tmp_path / "peers.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def wired(monkeypatch):
    """Patch every boundary main() touches; yield the mocks."""
    account = MagicMock()
    account.address = "0x0000000000000000000000000000000000000001"
    w3 = MagicMock()
    # 1 ETH — comfortably covers gas for any work list these tests build.
    w3.eth.get_balance.return_value = 10**18
    registry = MagicMock()
    registry.bond_amount.return_value = BOND
    registry.sqd_balance.return_value = BOND * 1000
    registry.allowance.return_value = BOND * 1000
    registry.token_decimals.return_value = 18
    registry.registration_state.return_value = "unregistered"
    registry.owned_worker_ids.return_value = set()
    registry.estimate_register_gas.return_value = (300000, True)

    monkeypatch.setattr(bulk_register, "load_signer", lambda: account)
    monkeypatch.setattr(bulk_register, "connect", lambda network, rpc: w3)
    monkeypatch.setattr(bulk_register, "Registry", lambda *a, **k: registry)
    monkeypatch.setattr(
        bulk_register,
        "current_fees",
        lambda _w3: {"maxFeePerGas": 200, "maxPriorityFeePerGas": 10},
    )
    yield account, w3, registry


def test_dry_run_sends_nothing(wired, tmp_path, capsys):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 3)

    code = bulk_register.main(
        [str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")]
    )

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()
    assert "dry run" in capsys.readouterr().out.lower()


def test_dry_run_wins_over_yes(wired, tmp_path):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 3)

    bulk_register.main(
        [str(path), "--dry-run", "--yes", "--log", str(tmp_path / "l.jsonl")]
    )

    w3.eth.send_raw_transaction.assert_not_called()


def test_dry_run_shows_the_name_each_node_would_get(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 3)

    bulk_register.main(
        [
            str(path),
            "--dry-run",
            "--name-template",
            "sqd-{n:03d}",
            "--log",
            str(tmp_path / "l.jsonl"),
        ]
    )

    out = capsys.readouterr().out
    assert "sqd-001" in out
    assert "sqd-003" in out


def test_explicit_names_beat_the_template(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 2, names=True)

    bulk_register.main(
        [
            str(path),
            "--dry-run",
            "--name-template",
            "sqd-{n:03d}",
            "--log",
            str(tmp_path / "l.jsonl"),
        ]
    )

    out = capsys.readouterr().out
    assert "named-0" in out
    assert "sqd-001" not in out


def test_bad_template_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 2)

    with pytest.raises(SystemExit) as exc:
        bulk_register.main(
            [
                str(path),
                "--yes",
                "--name-template",
                "sqd-{bogus}",
                "--log",
                str(tmp_path / "l.jsonl"),
            ]
        )

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_oversized_name_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id_for(0)},{'x' * 300}\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_declining_the_prompt_sends_nothing(wired, tmp_path, monkeypatch):
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 2)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    code = bulk_register.main([str(path), "--log", str(tmp_path / "l.jsonl")])

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()


def test_confirmed_run_registers(wired, tmp_path):
    path = make_peer_file(tmp_path, 2)
    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=2)
        code = bulk_register.main(
            [str(path), "--yes", "--log", str(tmp_path / "l.jsonl")]
        )

    assert code == 0
    register_all.assert_called_once()


def test_approval_is_sent_when_the_allowance_is_short(wired, tmp_path):
    _, _, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)
    with patch.object(bulk_register, "send_and_wait") as send_and_wait, patch.object(
        bulk_register, "register_all"
    ) as register_all:
        send_and_wait.return_value = (
            "0xapproval",
            {"status": 1, "gasUsed": 1, "blockNumber": 1},
        )
        register_all.return_value = bulk_register.RunResult(registered=2)
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    registry.build_approve.assert_called_once()
    assert registry.build_approve.call_args.kwargs["amount"] == BOND * 2


def test_fees_are_recomputed_after_the_confirmation(wired, tmp_path, monkeypatch):
    """The prompt can sit for minutes; the plan's fee cap goes stale."""
    fees = MagicMock(return_value={"maxFeePerGas": 200, "maxPriorityFeePerGas": 10})
    monkeypatch.setattr(bulk_register, "current_fees", fees)
    path = make_peer_file(tmp_path, 2)

    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=2)
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    # Once for the plan display, once after the prompt for the real sends.
    assert fees.call_count == 2


def test_gas_is_measured_once_and_only_after_the_approval(wired, tmp_path):
    """The only useful measurement is the one taken after the allowance exists.

    Before it, register() reverts because transferFrom does, so estimating is a
    call with a known answer — and a signing provider logs that revert as an
    error, which is alarming to show an operator mid-run. So the plan projects,
    and the run measures once the approval has landed.
    """
    _, _, registry = wired
    registry.allowance.return_value = 0  # forces an approval
    registry.estimate_register_gas.return_value = (480000, True)
    path = make_peer_file(tmp_path, 2)

    with patch.object(bulk_register, "send_and_wait") as send_and_wait, patch.object(
        bulk_register, "register_all"
    ) as register_all:
        send_and_wait.return_value = (
            "0xapproval",
            {"status": 1, "gasUsed": 1, "blockNumber": 1},
        )
        register_all.return_value = bulk_register.RunResult(registered=2)
        bulk_register.main(
            [str(path), "--yes", "--log", str(tmp_path / "l.jsonl")]
        )

    # Once, not twice: the doomed pre-approval estimate is never attempted.
    assert registry.estimate_register_gas.call_count == 1
    # And the registrations use that measurement, padded.
    assert register_all.call_args.kwargs["gas"] == 480000 + 480000 * 25 // 100


def test_the_plan_says_the_gas_figure_is_projected(wired, tmp_path, capsys):
    _, _, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 1)

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    out = capsys.readouterr().out
    assert "measured once the approval lands" in out
    registry.estimate_register_gas.assert_not_called()

def test_an_approval_send_failure_exits_cleanly_with_the_hash(wired, tmp_path, capsys):
    """A broadcast-but-unresolved approval must not be a raw traceback.

    It consumed a nonce that a resume would reuse, so the operator has to be
    told the hash and told to wait for it to settle.
    """
    _, _, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)

    with patch.object(bulk_register, "send_and_wait") as send_and_wait, patch.object(
        bulk_register, "register_all"
    ) as register_all:
        send_and_wait.side_effect = bulk_register.SendFailed(
            ConnectionError("connection reset"), "0xapproval"
        )
        with pytest.raises(SystemExit) as exc:
            bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    register_all.assert_not_called()
    err = capsys.readouterr().err
    assert "0xapproval" in err
    assert "connection reset" in err


def test_an_approval_failure_without_a_hash_still_exits_cleanly(wired, tmp_path, capsys):
    _, _, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)

    with patch.object(bulk_register, "send_and_wait") as send_and_wait:
        send_and_wait.side_effect = bulk_register.SendFailed(
            ValueError("cannot sign"), None
        )
        with pytest.raises(SystemExit) as exc:
            bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    assert "approval failed" in capsys.readouterr().err


def test_an_interrupted_run_exits_130_with_a_resume_hint(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 3)

    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(
            registered=1, pending=1, interrupted=True, aborted="interrupted"
        )
        code = bulk_register.main(
            [str(path), "--yes", "--log", str(tmp_path / "l.jsonl")]
        )

    assert code == 130
    assert "resume with" in capsys.readouterr().out


def test_dry_run_never_sends_the_approval(wired, tmp_path):
    _, w3, registry = wired
    registry.allowance.return_value = 0
    path = make_peer_file(tmp_path, 2)

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    w3.eth.send_raw_transaction.assert_not_called()


def test_malformed_input_exits_before_any_transaction(wired, tmp_path):
    _, w3, _ = wired
    path = tmp_path / "peers.txt"
    path.write_text("garbage-0OIl\n")

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_nothing_to_do_exits_cleanly(wired, tmp_path, capsys):
    _, w3, registry = wired
    registry.registration_state.return_value = "registered"
    path = make_peer_file(tmp_path, 2)

    code = bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()
    assert "nothing to register" in capsys.readouterr().out.lower()


def test_the_printed_resume_hint_carries_the_bounding_flags(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 5)
    log = tmp_path / "l.jsonl"
    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=1, failed=1)
        bulk_register.main(
            [
                str(path),
                "--yes",
                "--limit",
                "2",
                "--name-template",
                "sqd-{n:03d}",
                "--log",
                str(log),
            ]
        )

    out = capsys.readouterr().out
    assert "--limit 2" in out
    assert "--name-template 'sqd-{n:03d}'" in out
    assert f"--log {log}" in out
    assert "--yes" not in out


def test_a_truncated_scan_reports_an_upper_bound_not_a_figure(
    wired, tmp_path, capsys
):
    """Under --limit the peers past the cap were never examined.

    Measured before the fix: a 256-entry file whose first 50 were unregistered,
    run with --limit 10, claimed '246 peer ID(s) still unregistered' when 40
    were left.
    """
    path = make_peer_file(tmp_path, 20)

    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=2)
        bulk_register.main(
            [str(path), "--yes", "--limit", "2", "--log", str(tmp_path / "l.jsonl")]
        )

    out = capsys.readouterr().out
    assert "up to 18 peer ID(s) may still be unregistered" in out
    assert "18 peer ID(s) still unregistered" not in out


def test_a_complete_scan_reports_an_exact_figure(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 5)

    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=2, failed=1)
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert "3 peer ID(s) still unregistered" in capsys.readouterr().out


def test_a_corrupt_run_log_exits_cleanly_without_sending(wired, tmp_path, capsys):
    """A truncated log is the resume path; a traceback there invites deletion."""
    _, w3, _ = wired
    path = make_peer_file(tmp_path, 2)
    log = tmp_path / "l.jsonl"
    log.write_text('{"peer_id": "a", "status": "success"}\n{"peer_id": "b", "sta')

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(log)])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()
    assert "line 2" in capsys.readouterr().err


def test_a_logged_tethys_success_does_not_cancel_a_mainnet_run(
    wired, tmp_path, capsys
):
    """The whole point of scoping the log: a rehearsal cannot fake completion.

    Registering the file on tethys and then running it on mainnet used to print
    "nothing to register" and exit 0 with zero mainnet nodes registered.
    """
    path = make_peer_file(tmp_path, 2)
    log = tmp_path / "shared.jsonl"
    for seed in range(2):
        RunLog(log).append(
            Record(peer_id=peer_id_for(seed), status=SUCCESS, network="tethys")
        )

    code = bulk_register.main([str(path), "--dry-run", "--log", str(log)])

    assert code == 0
    assert "to register: 2" in capsys.readouterr().out


def test_the_network_reaches_the_registration_loop(wired, tmp_path):
    path = make_peer_file(tmp_path, 1)
    with patch.object(bulk_register, "register_all") as register_all:
        register_all.return_value = bulk_register.RunResult(registered=1)
        bulk_register.main(
            [
                str(path),
                "--yes",
                "--network",
                "tethys",
                "--log",
                str(tmp_path / "l.jsonl"),
            ]
        )

    assert register_all.call_args.kwargs["network"] == "tethys"


def test_the_default_log_path_includes_the_network(wired, tmp_path, capsys):
    path = make_peer_file(tmp_path, 1)

    bulk_register.main([str(path), "--dry-run", "--network", "tethys"])

    assert f"{path}.tethys.run.jsonl" in capsys.readouterr().out


def test_duplicate_warnings_are_printed(wired, tmp_path, capsys):
    peer_id = peer_id_for(0)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id}\n{peer_id}\n")

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    assert "duplicate" in capsys.readouterr().err.lower()


def test_insufficient_eth_aborts_before_any_transaction(wired, tmp_path, capsys):
    _, w3, _ = wired
    w3.eth.get_balance.return_value = 1  # 1 wei
    path = make_peer_file(tmp_path, 3)

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()
    err = capsys.readouterr()
    assert "insufficient ETH" in err.err
    # The plan is printed first, so the shortfall is read in context.
    assert "bond total:" in err.out
    assert "ETH balance:" in err.out


def test_a_dry_run_also_reports_insufficient_eth(wired, tmp_path):
    """Catching it in a dry run is the point of a pre-check."""
    _, w3, _ = wired
    w3.eth.get_balance.return_value = 1
    path = make_peer_file(tmp_path, 3)

    with pytest.raises(SystemExit) as exc:
        bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    assert exc.value.code == 2
    w3.eth.send_raw_transaction.assert_not_called()


def test_the_eth_budget_covers_the_approval_when_one_is_needed(wired, tmp_path, capsys):
    _, w3, registry = wired
    registry.allowance.return_value = 0  # forces an approval
    path = make_peer_file(tmp_path, 2)

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    # 2 registrations + 1 approval must be budgeted, not just the 2.
    gas = int(300000 * 1.25)
    fees = {"maxFeePerGas": 200}
    without_approval = gas * 2 * fees["maxFeePerGas"]
    assert "ETH balance:" in capsys.readouterr().out
    check = bulk_register.check_eth(
        w3, "0xabc", gas=gas, fees=fees, count=2, needs_approval=True
    )
    assert check.required > without_approval


def test_each_template_starts_its_own_sequence_across_runs(wired, tmp_path):
    """Two groups from one unnamed peer ID list, each numbered from 001.

    The operator hands over a plain list of peer IDs and chooses the naming at
    run time: 5 as nodexeus-001..005, then 5 as newname-001..005. Under the old
    file-position rule the second group would have come out newname-006..010.
    """
    _, _, registry = wired
    path = make_peer_file(tmp_path, 10)
    log = tmp_path / "l.jsonl"

    def run(template):
        captured = {}
        with patch.object(bulk_register, "register_all") as register_all:
            def record(w3, account, reg, work, runlog, fees, gas, network, **_kw):
                captured["names"] = [w.name for w in work]
                for w in work:
                    runlog.append(
                        Record(
                            peer_id=w.entry.peer_id,
                            status=SUCCESS,
                            name=w.name,
                            network=network,
                        )
                    )
                return bulk_register.RunResult(registered=len(work))

            register_all.side_effect = record
            bulk_register.main(
                [str(path), "--yes", "--limit", "5",
                 "--name-template", template, "--log", str(log)]
            )
        return captured["names"]

    assert run("nodexeus-{n:03d}") == [f"nodexeus-{i:03d}" for i in range(1, 6)]
    assert run("newname-{n:03d}") == [f"newname-{i:03d}" for i in range(1, 6)]


def test_an_interrupted_group_resumes_its_numbering(wired, tmp_path):
    """The property the old file-position rule existed to protect."""
    path = make_peer_file(tmp_path, 10)
    log = RunLog(tmp_path / "l.jsonl")
    for seed in range(3):
        log.append(
            Record(
                peer_id=peer_id_for(seed),
                status=SUCCESS,
                name=f"sqd-{seed + 1:03d}",
                network="mainnet",
            )
        )

    captured = {}
    with patch.object(bulk_register, "register_all") as register_all:

        def record(w3, account, reg, work, runlog, fees, gas, network, **_kw):
            captured["names"] = [w.name for w in work]
            return bulk_register.RunResult(registered=len(work))

        register_all.side_effect = record
        bulk_register.main(
            [str(path), "--yes", "--limit", "3",
             "--name-template", "sqd-{n:03d}", "--log", str(tmp_path / "l.jsonl")]
        )

    assert captured["names"] == ["sqd-004", "sqd-005", "sqd-006"]


def test_a_real_run_writes_the_registered_csv(wired, tmp_path):
    path = make_peer_file(tmp_path, 2)
    with patch.object(bulk_register, "register_all") as register_all:
        def record(w3, account, reg, work, runlog, fees, gas, network, **_kw):
            for w in work:
                runlog.append(
                    Record(
                        peer_id=w.entry.peer_id,
                        status=SUCCESS,
                        name=w.name,
                        tx_hash="0xabc",
                        block=1,
                        network=network,
                    )
                )
            return bulk_register.RunResult(registered=len(work))

        register_all.side_effect = record
        bulk_register.main(
            [str(path), "--yes", "--name-template", "sqd-{n:03d}",
             "--log", str(tmp_path / "l.jsonl")]
        )

    csv_path = tmp_path / "peers.txt.mainnet.registered.csv"
    lines = csv_path.read_text().splitlines()
    assert lines[0] == "peer_id,name,tx_hash,block,registered_at"
    assert len(lines) == 3
    assert "sqd-001" in lines[1] and "sqd-002" in lines[2]


def test_a_dry_run_writes_no_csv(wired, tmp_path):
    """A CSV asserts these nodes are registered; a dry run registers nothing."""
    path = make_peer_file(tmp_path, 2)

    bulk_register.main([str(path), "--dry-run", "--log", str(tmp_path / "l.jsonl")])

    assert not (tmp_path / "peers.txt.mainnet.registered.csv").exists()
