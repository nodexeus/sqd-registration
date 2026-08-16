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


def test_default_log_path_derives_from_input_and_network():
    assert (
        bulk_register.default_log_path("peers.txt", "mainnet")
        == "peers.txt.mainnet.run.jsonl"
    )


def test_default_log_paths_differ_between_networks():
    """A tethys rehearsal must not write into the mainnet run's log."""
    assert bulk_register.default_log_path(
        "peers.txt", "tethys"
    ) != bulk_register.default_log_path("peers.txt", "mainnet")


def test_resume_command_echoes_every_supplied_flag():
    """Dropping a flag from the hint changes what a resume spends.

    Without --limit the resume plans the whole file instead of the operator's
    cap; without --name-template the rest of the file registers unnamed;
    without --log the resume reads a different log and re-registers.
    """
    args = bulk_register.parse_args(
        [
            "peers.txt",
            "--network",
            "tethys",
            "--limit",
            "50",
            "--name-template",
            "nodexeus-{n:03d}",
            "--log",
            "custom.jsonl",
            "--rpc-url",
            "http://localhost:8545",
        ]
    )

    command = bulk_register.resume_command(args)

    assert command.startswith("bulk_register.py peers.txt")
    assert "--network tethys" in command
    assert "--limit 50" in command
    assert "--name-template 'nodexeus-{n:03d}'" in command
    assert "--log custom.jsonl" in command
    assert "--rpc-url http://localhost:8545" in command


def test_resume_command_omits_yes_so_a_resume_reprompts():
    args = bulk_register.parse_args(["peers.txt", "--yes"])

    assert "--yes" not in bulk_register.resume_command(args)


def test_resume_command_omits_flags_that_were_not_given():
    command = bulk_register.resume_command(bulk_register.parse_args(["peers.txt"]))

    assert command == "bulk_register.py peers.txt --network mainnet"


def test_resume_command_quotes_a_path_with_spaces():
    args = bulk_register.parse_args(["my peers.txt"])

    assert "'my peers.txt'" in bulk_register.resume_command(args)


def test_resume_command_names_this_script_not_the_caller(monkeypatch):
    """A programmatic main() must not print the driving script's path."""
    monkeypatch.setattr(bulk_register.sys, "argv", ["/some/other/driver.py"])

    command = bulk_register.resume_command(bulk_register.parse_args(["peers.txt"]))

    assert command.startswith("bulk_register.py ")


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
        assert bulk_register.connect(NETWORKS["mainnet"], "http://rpc.test") is w3


def test_connect_exits_on_chain_id_mismatch(capsys):
    w3 = MagicMock()
    w3.eth.chain_id = 421614
    with patch.object(bulk_register, "Web3", return_value=w3):
        with pytest.raises(SystemExit) as exc:
            bulk_register.connect(NETWORKS["mainnet"], "http://rpc.test")
    assert exc.value.code == 2
    assert "42161" in capsys.readouterr().err


def test_connect_exits_on_chain_id_mismatch_reverse(capsys):
    w3 = MagicMock()
    w3.eth.chain_id = 42161
    with patch.object(bulk_register, "Web3", return_value=w3):
        with pytest.raises(SystemExit) as exc:
            bulk_register.connect(NETWORKS["tethys"], "http://rpc.test")
    assert exc.value.code == 2
    assert "421614" in capsys.readouterr().err


def test_load_signer_exits_on_malformed_private_key(monkeypatch, capsys):
    malformed_key = "0xNOTAVALIDHEX"
    monkeypatch.setenv("PRIVATE_KEY", malformed_key)
    monkeypatch.delenv("MNEMONIC", raising=False)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()
    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert malformed_key not in stderr


def test_load_signer_exits_on_malformed_mnemonic(monkeypatch, capsys):
    malformed_phrase = "banana elephant zebra gamma delta omega"
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.setenv("MNEMONIC", malformed_phrase)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()
    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    # Verify no words from the malformed phrase appear in stderr
    for word in malformed_phrase.split():
        assert word not in stderr


# --- interactive credential prompt -----------------------------------------


@pytest.fixture
def no_env_credentials(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("MNEMONIC", raising=False)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)


def tty(monkeypatch, interactive=True):
    monkeypatch.setattr(bulk_register.sys.stdin, "isatty", lambda: interactive)


def test_a_missing_credential_prompts_when_interactive(
    no_env_credentials, monkeypatch
):
    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", lambda _prompt: KEY)

    assert bulk_register.load_signer().address.startswith("0x")


def test_the_prompt_accepts_a_mnemonic_too(no_env_credentials, monkeypatch):
    """Detected by whitespace, so a pasted phrase is not read as a bad key."""
    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", lambda _prompt: TEST_MNEMONIC)

    # The standard test phrase derives this well-known first account.
    assert (
        bulk_register.load_signer().address
        == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    )


def test_the_prompt_is_hidden(no_env_credentials, monkeypatch):
    """getpass, never input() — the key must not reach the terminal or history."""
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("used input()"))
    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", lambda _prompt: KEY)

    bulk_register.load_signer()


def test_no_prompt_when_not_a_tty(no_env_credentials, monkeypatch, capsys):
    """Under cron or nohup a prompt would hang forever; fail fast instead."""
    tty(monkeypatch, interactive=False)
    monkeypatch.setattr(
        bulk_register, "getpass", lambda _prompt: pytest.fail("prompted")
    )

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()

    assert exc.value.code == 2
    assert "not a terminal" in capsys.readouterr().err


def test_an_empty_prompt_response_exits(no_env_credentials, monkeypatch):
    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", lambda _prompt: "   ")

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()

    assert exc.value.code == 2


def test_an_interrupted_prompt_exits_cleanly(no_env_credentials, monkeypatch):
    def interrupted(_prompt):
        raise EOFError

    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", interrupted)

    with pytest.raises(SystemExit) as exc:
        bulk_register.load_signer()

    assert exc.value.code == 2


def test_a_malformed_prompt_response_does_not_echo_the_secret(
    no_env_credentials, monkeypatch, capsys
):
    tty(monkeypatch)
    monkeypatch.setattr(bulk_register, "getpass", lambda _prompt: "zebra-canary-xyz")

    with pytest.raises(SystemExit):
        bulk_register.load_signer()

    err = capsys.readouterr().err
    assert "zebra" not in err and "canary" not in err


def test_env_credentials_still_win_over_prompting(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", KEY)
    monkeypatch.setattr(bulk_register, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        bulk_register, "getpass", lambda _prompt: pytest.fail("prompted")
    )

    assert bulk_register.load_signer().address.startswith("0x")
