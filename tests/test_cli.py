from unittest.mock import MagicMock, patch

import pytest

import bulk_register
from sqdreg.networks import NETWORKS

KEY = "0x" + "11" * 32
# The standard public Hardhat/Foundry test phrase. Holds nothing.
TEST_MNEMONIC = "test " * 11 + "junk"


def test_defaults():
    args = bulk_register.parse_args(["peers.txt"])
    assert args.peer_id_file == "peers.txt"
    assert args.network == "mainnet"
    assert args.limit is None
    assert args.name_template is None
    assert args.dry_run is False
    assert args.yes is False
    assert args.rpc_url is None


def test_all_flags_parse():
    args = bulk_register.parse_args(
        [
            "peers.txt",
            "--network",
            "tethys",
            "--limit",
            "10",
            "--name-template",
            "sqd-{n:03d}",
            "--dry-run",
            "--yes",
            "--rpc-url",
            "http://localhost:8545",
            "--log",
            "custom.jsonl",
        ]
    )
    assert args.network == "tethys"
    assert args.limit == 10
    assert args.name_template == "sqd-{n:03d}"
    assert args.dry_run is True
    assert args.yes is True
    assert args.rpc_url == "http://localhost:8545"
    assert args.log == "custom.jsonl"


def test_short_limit_flag():
    assert bulk_register.parse_args(["peers.txt", "-n", "5"]).limit == 5


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_limit_is_rejected(value):
    with pytest.raises(SystemExit) as exc:
        bulk_register.parse_args(["peers.txt", "--limit", value])
    assert exc.value.code == 2


def test_unknown_network_is_rejected():
    with pytest.raises(SystemExit):
        bulk_register.parse_args(["peers.txt", "--network", "nope"])


def test_default_log_path_derives_from_input():
    assert bulk_register.default_log_path("peers.txt") == "peers.txt.run.jsonl"


def test_load_signer_prefers_private_key(monkeypatch, capsys):
    monkeypatch.setenv("PRIVATE_KEY", KEY)
    monkeypatch.setenv("MNEMONIC", TEST_MNEMONIC)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    account = bulk_register.load_signer()

    assert account.address.startswith("0x")
    assert "both PRIVATE_KEY and MNEMONIC" in capsys.readouterr().err


def test_load_signer_accepts_mnemonic(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.setenv("MNEMONIC", TEST_MNEMONIC)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    assert bulk_register.load_signer().address.startswith("0x")


def test_load_signer_exits_when_no_credentials(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("MNEMONIC", raising=False)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()
    assert exc.value.code == 2


def test_connect_accepts_matching_chain_id():
    w3 = MagicMock()
    w3.eth.chain_id = 42161
    with patch.object(bulk_register, "Web3", return_value=w3):
        assert bulk_register.connect(NETWORKS["mainnet"], None) is w3


def test_connect_exits_on_chain_id_mismatch(capsys):
    w3 = MagicMock()
    w3.eth.chain_id = 421614
    with patch.object(bulk_register, "Web3", return_value=w3):
        with pytest.raises(SystemExit) as exc:
            bulk_register.connect(NETWORKS["mainnet"], None)
    assert exc.value.code == 2
    assert "42161" in capsys.readouterr().err
