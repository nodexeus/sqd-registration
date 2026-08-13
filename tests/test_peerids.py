from pathlib import Path

import base58
import pytest

from sqdreg.peerids import PeerIdError, decode_peer_id, parse_file


def make_peer_id(code: int = 0x00, digest_len: int = 36, seed: int = 0):
    """Build a structurally valid peer ID and its raw bytes."""
    digest = bytes((seed + i) % 256 for i in range(digest_len))
    raw = bytes([code, digest_len]) + digest
    return base58.b58encode(raw).decode(), raw


def test_decodes_identity_multihash():
    peer_id, raw = make_peer_id(code=0x00, digest_len=36)
    assert decode_peer_id(peer_id) == raw


def test_decodes_sha256_multihash():
    peer_id, raw = make_peer_id(code=0x12, digest_len=32)
    assert decode_peer_id(peer_id) == raw


def test_rejects_non_base58():
    with pytest.raises(PeerIdError, match="not valid base58"):
        decode_peer_id("not-a-peer-id-0OIl")


def test_rejects_too_short():
    raw = bytes([0x00, 4]) + b"abcd"
    with pytest.raises(PeerIdError, match="expected 32-64"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_over_64_bytes():
    raw = bytes([0x00, 70]) + bytes(70)
    with pytest.raises(PeerIdError, match="expected 32-64"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_unknown_multihash_code():
    raw = bytes([0x99, 36]) + bytes(36)
    with pytest.raises(PeerIdError, match="unsupported multihash code 0x99"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_rejects_declared_length_mismatch():
    raw = bytes([0x00, 40]) + bytes(36)
    with pytest.raises(PeerIdError, match="declares digest length 40"):
        decode_peer_id(base58.b58encode(raw).decode())


def test_parse_file_reads_entries_in_order(tmp_path):
    first, first_raw = make_peer_id(seed=1)
    second, second_raw = make_peer_id(seed=2)
    path = tmp_path / "peers.txt"
    path.write_text(f"{first}\n{second}\n")

    entries, duplicates = parse_file(path)

    assert [e.peer_id for e in entries] == [first, second]
    assert [e.peer_bytes for e in entries] == [first_raw, second_raw]
    assert [e.name for e in entries] == [None, None]
    assert [e.index for e in entries] == [1, 2]
    assert duplicates == []


def test_parse_file_reads_the_optional_name_column(tmp_path):
    peer_id, _ = make_peer_id(seed=3)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},prod-worker-01\n")

    entries, _ = parse_file(path)

    assert entries[0].name == "prod-worker-01"


def test_parse_file_strips_whitespace_around_both_fields(tmp_path):
    peer_id, _ = make_peer_id(seed=4)
    path = tmp_path / "peers.txt"
    path.write_text(f"  {peer_id} ,  spaced name  \n")

    entries, _ = parse_file(path)

    assert entries[0].peer_id == peer_id
    assert entries[0].name == "spaced name"


def test_parse_file_splits_on_the_first_comma_only(tmp_path):
    peer_id, _ = make_peer_id(seed=5)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},name,with,commas\n")

    entries, _ = parse_file(path)

    assert entries[0].name == "name,with,commas"


def test_parse_file_rejects_a_bare_trailing_comma(tmp_path):
    peer_id, _ = make_peer_id(seed=6)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},\n")

    with pytest.raises(PeerIdError, match="line 1"):
        parse_file(path)


def test_parse_file_ignores_blanks_and_comments(tmp_path):
    peer_id, raw = make_peer_id(seed=7)
    path = tmp_path / "peers.txt"
    path.write_text(f"# a comment\n\n   \n{peer_id}\n   # indented comment\n")

    entries, duplicates = parse_file(path)

    assert [e.peer_id for e in entries] == [peer_id]
    assert duplicates == []


def test_parse_file_collapses_duplicates_and_warns(tmp_path):
    peer_id, _ = make_peer_id(seed=8)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id},first-name\n{peer_id},second-name\n")

    entries, duplicates = parse_file(path)

    assert len(entries) == 1
    assert entries[0].name == "first-name"
    assert len(duplicates) == 1
    assert "line 2" in duplicates[0]
    assert peer_id in duplicates[0]


def test_index_is_assigned_after_duplicates_collapse(tmp_path):
    first, _ = make_peer_id(seed=9)
    second, _ = make_peer_id(seed=10)
    path = tmp_path / "peers.txt"
    path.write_text(f"{first}\n{first}\n{second}\n")

    entries, _ = parse_file(path)

    assert [(e.peer_id, e.index) for e in entries] == [(first, 1), (second, 2)]


def test_parse_file_reports_line_number_of_bad_entry(tmp_path):
    good, _ = make_peer_id(seed=11)
    path = tmp_path / "peers.txt"
    path.write_text(f"{good}\ngarbage-0OIl\n")

    with pytest.raises(PeerIdError, match="line 2"):
        parse_file(path)


def test_entry_is_immutable(tmp_path):
    import dataclasses

    peer_id, _ = make_peer_id(seed=12)
    path = tmp_path / "peers.txt"
    path.write_text(f"{peer_id}\n")

    entries, _ = parse_file(path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entries[0].name = "nope"


EXAMPLE_FILE = Path(__file__).resolve().parents[1] / "peer_ids.txt.example"


def test_the_shipped_example_file_registers_nothing_as_is():
    """The example must not be runnable: a valid peer ID would bond 100,000 SQD."""
    entries, duplicates = parse_file(EXAMPLE_FILE)

    assert entries == []
    assert duplicates == []


def test_the_shipped_example_ids_are_valid_once_uncommented(tmp_path):
    """Placeholders must be real peer IDs, not text that fails as 'not base58'."""
    lines = [
        line.lstrip("#")
        for line in EXAMPLE_FILE.read_text().splitlines()
        if line.startswith("#12D3KooW")
    ]
    assert len(lines) == 3
    path = tmp_path / "peers.txt"
    path.write_text("\n".join(lines) + "\n")

    entries, _ = parse_file(path)

    assert [entry.name for entry in entries] == [
        "prod-worker-01",
        "prod-worker-02",
        None,
    ]
