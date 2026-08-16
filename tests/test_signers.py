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
