"""Local versus remote signing.

Fireblocks holds the key across MPC shares and cannot export it, so those
transactions go out unsigned over eth_sendTransaction for the endpoint to sign.
"""

from unittest.mock import MagicMock

import pytest
from web3.exceptions import TimeExhausted

import bulk_register
from sqdreg.runlog import PENDING, SUCCESS, RunLog


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(bulk_register.time, "sleep", lambda _s: None)


def local_signer():
    account = MagicMock()
    account.address = "0x000000000000000000000000000000000000dEaD"
    account.sign_transaction.return_value = MagicMock(
        raw_transaction=b"raw", hash=MagicMock(hex=lambda: "0xlocal")
    )
    return bulk_register.LocalSigner(account)


# --- the two signers differ in exactly two ways ----------------------------


def test_a_local_signer_owns_the_nonce_and_a_remote_one_does_not():
    assert bulk_register.LocalSigner(MagicMock()).manages_nonce is False
    assert bulk_register.RemoteSigner("0xabc").manages_nonce is True


def test_a_local_signer_knows_the_hash_before_broadcasting():
    signer = local_signer()

    _payload, tx_hash = signer.prepare({"nonce": 1})

    assert tx_hash.hex() == "0xlocal"


def test_a_remote_signer_has_no_hash_until_it_has_signed():
    """The hash is a function of the signed payload, which only it can produce."""
    signer = bulk_register.RemoteSigner("0xabc")

    payload, tx_hash = signer.prepare({"nonce": 1})

    assert tx_hash is None
    assert payload == {"nonce": 1}


def test_a_remote_signer_sends_the_transaction_unsigned():
    w3 = MagicMock()
    w3.eth.send_transaction.return_value = MagicMock(hex=lambda: "0xremote")
    signer = bulk_register.RemoteSigner("0xabc")

    sent = signer.dispatch(w3, {"to": "0x1"})

    assert sent.hex() == "0xremote"
    w3.eth.send_raw_transaction.assert_not_called()


def test_a_remote_signer_allows_a_longer_receipt_wait():
    """Fireblocks queues for policy evaluation and MPC signing before it even
    broadcasts, so the round trip is slower than a local signature."""
    assert (
        bulk_register.RemoteSigner("0xabc").receipt_timeout
        > bulk_register.LocalSigner(MagicMock()).receipt_timeout
    )


# --- the loop under a remote signer ----------------------------------------


def remote_env(hashes, statuses):
    w3 = MagicMock()
    w3.eth.send_transaction.side_effect = [
        MagicMock(hex=lambda h=h: h) for h in hashes
    ]
    w3.eth.wait_for_transaction_receipt.side_effect = [
        {"status": s, "gasUsed": 10, "blockNumber": 1} for s in statuses
    ]
    registry = MagicMock()
    registry.build_register.side_effect = lambda **kw: dict(kw)
    return w3, bulk_register.RemoteSigner("0xabc"), registry


def work(*ids):
    from sqdreg.naming import NamedPeer
    from sqdreg.peerids import PeerEntry

    return [
        NamedPeer(
            entry=PeerEntry(peer_id=i, peer_bytes=i.encode(), name=None, index=1),
            name=None,
            metadata="",
        )
        for i in ids
    ]


def test_no_nonce_is_supplied_to_a_remote_signer(tmp_path):
    """Fireblocks keeps its own sequence per vault account; ours would fight it."""
    w3, signer, registry = remote_env(["0xa", "0xb"], [1, 1])

    bulk_register.register_all(
        w3, signer, registry, work("a", "b"), RunLog(tmp_path / "l.jsonl"),
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet",
    )

    nonces = [c.kwargs["nonce"] for c in registry.build_register.call_args_list]
    assert nonces == [None, None]
    w3.eth.get_transaction_count.assert_not_called()


def test_the_hash_from_the_remote_signer_is_logged(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    w3, signer, registry = remote_env(["0xfeed"], [1])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet",
    )

    record = log.records()[0]
    assert record.status == SUCCESS
    assert record.tx_hash == "0xfeed"


def test_a_remote_send_failure_is_pending_without_a_hash(tmp_path):
    """No hash can exist yet, so the log says so rather than inventing one.
    Recovery is the Fireblocks console, which records every signing request.
    """
    log = RunLog(tmp_path / "l.jsonl")
    w3, signer, registry = remote_env([], [])
    w3.eth.send_transaction.side_effect = ConnectionError("reset")

    result = bulk_register.register_all(
        w3, signer, registry, work("a", "b"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet",
    )

    assert result.pending == 1
    assert result.aborted
    record = log.records()[0]
    assert record.status == PENDING
    assert record.tx_hash is None


def test_a_remote_receipt_timeout_still_keeps_its_hash(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    w3, signer, registry = remote_env(["0xslow"], [])
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted("slow")

    bulk_register.register_all(
        w3, signer, registry, work("a"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet",
    )

    assert log.records()[0].tx_hash == "0xslow"


# --- choosing the signer ---------------------------------------------------


def args_for(**kw):
    base = dict(signer="fireblocks", address=None, rpc_url="http://localhost:8545")
    base.update(kw)
    return MagicMock(**base)


def test_the_address_comes_from_the_endpoint():
    w3 = MagicMock()
    w3.eth.accounts = ["0x000000000000000000000000000000000000dEaD"]
    w3.to_checksum_address.side_effect = lambda v: v

    signer = bulk_register.build_signer(args_for(), w3)

    assert signer.address == "0x000000000000000000000000000000000000dEaD"
    assert signer.manages_nonce is True


def test_address_selects_among_several_vault_accounts():
    w3 = MagicMock()
    w3.eth.accounts = ["0xaaa", "0xBBB"]
    w3.to_checksum_address.side_effect = lambda v: v

    signer = bulk_register.build_signer(args_for(address="0xbbb"), w3)

    assert signer.address == "0xbbb"


def test_an_address_the_signer_does_not_offer_is_rejected(capsys):
    w3 = MagicMock()
    w3.eth.accounts = ["0xaaa"]

    with pytest.raises(SystemExit) as exc:
        bulk_register.build_signer(args_for(address="0xzzz"), w3)

    assert exc.value.code == 2
    assert "not offered by the signer" in capsys.readouterr().err


def test_no_accounts_is_a_clear_error(capsys):
    w3 = MagicMock()
    w3.eth.accounts = []

    with pytest.raises(SystemExit) as exc:
        bulk_register.build_signer(args_for(), w3)

    assert exc.value.code == 2
    assert "cannot sign" in capsys.readouterr().err


def test_an_unreachable_endpoint_names_the_proxy(capsys):
    w3 = MagicMock()
    type(w3.eth).accounts = property(
        lambda _self: (_ for _ in ()).throw(ConnectionError("refused"))
    )

    with pytest.raises(SystemExit) as exc:
        bulk_register.build_signer(args_for(), w3)

    assert exc.value.code == 2
    assert "fireblocks-json-rpc" in capsys.readouterr().err


def test_local_signing_never_asks_the_endpoint_for_accounts(monkeypatch):
    account = MagicMock()
    account.address = "0xlocal"
    monkeypatch.setattr(bulk_register, "load_signer", lambda: account)
    w3 = MagicMock()

    signer = bulk_register.build_signer(args_for(signer="local"), w3)

    assert isinstance(signer, bulk_register.LocalSigner)
    assert signer.manages_nonce is False


# --- endpoint resolution ----------------------------------------------------


def test_an_ipc_path_gets_an_ipc_provider():
    """The proxy listens on a unix socket unless started with --http."""
    from web3 import Web3

    provider = bulk_register.provider_for("/Users/x/.fireblocks/json-rpc.ipc")

    assert isinstance(provider, Web3.IPCProvider)


def test_a_url_gets_an_http_provider():
    from web3 import Web3

    assert isinstance(
        bulk_register.provider_for("http://127.0.0.1:8545"), Web3.HTTPProvider
    )


def test_the_proxy_address_is_picked_up_from_the_environment(monkeypatch):
    """The proxy exports it into the child, so the child needs no plumbing."""
    from sqdreg.networks import NETWORKS

    monkeypatch.setenv("FIREBLOCKS_JSON_RPC_ADDRESS", "/tmp/fb.ipc")
    args = MagicMock(rpc_url=None, signer="fireblocks")

    assert (
        bulk_register.resolve_rpc_url(args, NETWORKS["tethys"]) == "/tmp/fb.ipc"
    )


def test_an_explicit_rpc_url_wins(monkeypatch):
    from sqdreg.networks import NETWORKS

    monkeypatch.setenv("FIREBLOCKS_JSON_RPC_ADDRESS", "/tmp/fb.ipc")
    args = MagicMock(rpc_url="http://explicit", signer="fireblocks")

    assert (
        bulk_register.resolve_rpc_url(args, NETWORKS["tethys"]) == "http://explicit"
    )


def test_fireblocks_without_an_endpoint_explains_the_wrapper(monkeypatch, capsys):
    from sqdreg.networks import NETWORKS

    monkeypatch.delenv("FIREBLOCKS_JSON_RPC_ADDRESS", raising=False)
    args = MagicMock(rpc_url=None, signer="fireblocks")

    with pytest.raises(SystemExit) as exc:
        bulk_register.resolve_rpc_url(args, NETWORKS["tethys"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "fireblocks-json-rpc --chainId 421614" in err


def test_local_signing_still_defaults_to_the_network_rpc(monkeypatch):
    from sqdreg.networks import NETWORKS

    monkeypatch.setenv("FIREBLOCKS_JSON_RPC_ADDRESS", "/tmp/fb.ipc")
    args = MagicMock(rpc_url=None, signer="local")

    assert (
        bulk_register.resolve_rpc_url(args, NETWORKS["tethys"])
        == NETWORKS["tethys"].rpc_url
    )


# --- transaction hashes -----------------------------------------------------


def test_a_hash_gets_a_0x_prefix():
    """hexbytes 1.x drops it, leaving hashes unpasteable into an explorer."""
    from hexbytes import HexBytes

    assert bulk_register.tx_hash_hex(HexBytes("0xabcd")).startswith("0x")
    assert bulk_register.tx_hash_hex("abcd") == "0xabcd"


def test_an_already_prefixed_hash_is_not_doubled():
    assert bulk_register.tx_hash_hex("0xabcd") == "0xabcd"


def test_the_logged_hash_is_prefixed(tmp_path):
    log = RunLog(tmp_path / "l.jsonl")
    w3, signer, registry = remote_env(["abcd"], [1])

    bulk_register.register_all(
        w3, signer, registry, work("a"), log,
        fees={"maxFeePerGas": 1, "maxPriorityFeePerGas": 0},
        gas=100, network="mainnet",
    )

    assert log.records()[0].tx_hash == "0xabcd"


def test_a_workspace_error_does_not_blame_the_plumbing(capsys):
    """The proxy answering with an error is a workspace problem, not a missing
    proxy. Repeating install instructions there sends the operator the wrong
    way — which it did, twice, before this was fixed."""
    from web3.exceptions import Web3RPCError

    w3 = MagicMock()
    type(w3.eth).accounts = property(
        lambda _self: (_ for _ in ()).throw(
            Web3RPCError(
                "Failed to populate accounts: No ETH-AETH asset wallet found "
                "for vault account with id 1"
            )
        )
    )

    with pytest.raises(SystemExit) as exc:
        bulk_register.build_signer(args_for(), w3)

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "workspace question" in err
    assert "FIREBLOCKS_VAULT_ACCOUNT_IDS" in err
    assert "--network" in err          # the actual cause here
    assert "npm install" not in err    # the proxy is clearly running
