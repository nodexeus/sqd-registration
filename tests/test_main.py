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
    registry = MagicMock()
    registry.bond_amount.return_value = BOND
    registry.sqd_balance.return_value = BOND * 1000
    registry.allowance.return_value = BOND * 1000
    registry.token_decimals.return_value = 18
    registry.is_registered.return_value = False
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
    registry.is_registered.return_value = True
    path = make_peer_file(tmp_path, 2)

    code = bulk_register.main([str(path), "--yes", "--log", str(tmp_path / "l.jsonl")])

    assert code == 0
    w3.eth.send_raw_transaction.assert_not_called()
    assert "nothing to register" in capsys.readouterr().out.lower()


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

    assert "tethys" in register_all.call_args.args


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
