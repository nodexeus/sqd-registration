import json

import pytest

from sqdreg.naming import (
    MAX_METADATA_BYTES,
    NamingError,
    encode_metadata,
    prepare,
    resolve_name,
    validate_template,
)
from sqdreg.peerids import PeerEntry


def entry(peer_id="peer-a", name=None, index=1):
    return PeerEntry(peer_id=peer_id, peer_bytes=peer_id.encode(), name=name, index=index)


def test_explicit_name_wins_over_template():
    assert resolve_name(entry(name="explicit"), "template-{n}") == "explicit"


def test_template_fills_in_when_no_explicit_name():
    assert resolve_name(entry(index=7), "sqd-{n}") == "sqd-7"


def test_no_name_and_no_template_yields_none():
    assert resolve_name(entry(), None) is None


def test_template_substitutes_peer_id():
    assert resolve_name(entry(peer_id="abc"), "w-{peer_id}") == "w-abc"


def test_template_supports_format_specs():
    assert resolve_name(entry(index=3), "nodexeus-{n:03d}") == "nodexeus-003"


def test_template_without_placeholders_is_allowed():
    assert resolve_name(entry(), "static") == "static"


def test_validate_template_rejects_unknown_placeholder():
    with pytest.raises(NamingError, match="unknown placeholder"):
        validate_template("sqd-{nope}")


def test_validate_template_rejects_positional_placeholder():
    with pytest.raises(NamingError, match="unknown placeholder"):
        validate_template("sqd-{0}")


def test_validate_template_rejects_malformed_braces():
    with pytest.raises(NamingError, match="invalid"):
        validate_template("sqd-{n")


def test_validate_template_accepts_valid_templates():
    validate_template("sqd-{n:04d}-{peer_id}")


def test_encode_metadata_is_compact_json():
    assert encode_metadata("worker-1") == '{"name":"worker-1"}'
    assert json.loads(encode_metadata("worker-1")) == {"name": "worker-1"}


def test_encode_metadata_is_empty_string_when_unnamed():
    assert encode_metadata(None) == ""
    assert encode_metadata("") == ""


def test_encode_metadata_escapes_json_correctly():
    assert json.loads(encode_metadata('quote " and \\ backslash')) == {
        "name": 'quote " and \\ backslash'
    }


def test_encode_metadata_rejects_oversized_names():
    with pytest.raises(NamingError, match=str(MAX_METADATA_BYTES)):
        encode_metadata("x" * MAX_METADATA_BYTES)


def test_encode_metadata_accepts_utf8_within_byte_cap():
    # JSON wrapper {"name":""} is 11 bytes.
    # 81 × "あ" at 3 bytes each = 243 bytes, total 254 bytes (under 256 cap).
    # This test discriminates ensure_ascii=False (254 bytes, passes) from ensure_ascii=True (would be 11 + 486 = 497 bytes, fails).
    name = "あ" * 81
    metadata = encode_metadata(name)
    assert len(metadata.encode()) == 254
    assert json.loads(metadata) == {"name": name}


def test_encode_metadata_rejects_utf8_over_byte_cap():
    # 82 × "あ" at 3 bytes each = 246 bytes, total 257 bytes (over 256 cap).
    name = "あ" * 82
    with pytest.raises(NamingError):
        encode_metadata(name)


def test_prepare_resolves_every_entry():
    entries = [entry(peer_id="a", index=1), entry(peer_id="b", name="named", index=2)]

    prepared = prepare(entries, "sqd-{n:02d}")

    assert [p.name for p in prepared] == ["sqd-01", "named"]
    assert [p.metadata for p in prepared] == ['{"name":"sqd-01"}', '{"name":"named"}']
    assert [p.entry for p in prepared] == entries


def test_prepare_validates_the_template_once_up_front():
    with pytest.raises(NamingError, match="unknown placeholder"):
        prepare([entry()], "sqd-{bad}")


def test_prepare_with_no_template_leaves_unnamed_entries_empty():
    prepared = prepare([entry(peer_id="a"), entry(peer_id="b", name="named")], None)

    assert [p.name for p in prepared] == [None, "named"]
    assert [p.metadata for p in prepared] == ["", '{"name":"named"}']


def test_numbering_starts_at_one_regardless_of_file_position():
    """`{n}` counts allocations, not line numbers.

    Callers pass only the entries they are about to register, so a group that
    happens to sit at lines 3-4 of the file still numbers from 1.
    """
    entries = [entry(peer_id="c", index=3), entry(peer_id="d", index=4)]

    prepared = prepare(entries, "sqd-{n}")

    assert [p.name for p in prepared] == ["sqd-1", "sqd-2"]


def test_numbering_skips_names_already_used():
    """Resuming an interrupted group continues instead of colliding from 1."""
    entries = [entry(peer_id="c"), entry(peer_id="d")]

    prepared = prepare(entries, "sqd-{n:03d}", used_names={"sqd-001", "sqd-002"})

    assert [p.name for p in prepared] == ["sqd-003", "sqd-004"]


def test_a_different_template_starts_its_own_sequence():
    """The whole point: a second group is not continued from the first."""
    entries = [entry(peer_id="c"), entry(peer_id="d")]
    used = {f"nodexeus-{i:03d}" for i in range(1, 101)}

    prepared = prepare(entries, "newname-{n:03d}", used_names=used)

    assert [p.name for p in prepared] == ["newname-001", "newname-002"]


def test_numbering_fills_a_gap_left_by_a_failed_registration():
    """A failed send never landed, so its number is genuinely free."""
    entries = [entry(peer_id="c"), entry(peer_id="d")]

    prepared = prepare(entries, "sqd-{n:03d}", used_names={"sqd-001", "sqd-003"})

    assert [p.name for p in prepared] == ["sqd-002", "sqd-004"]


def test_an_explicit_name_is_never_renumbered_and_reserves_itself():
    entries = [entry(peer_id="c", name="sqd-001"), entry(peer_id="d")]

    prepared = prepare(entries, "sqd-{n:03d}")

    assert [p.name for p in prepared] == ["sqd-001", "sqd-002"]


def test_a_generated_name_cannot_collide_with_a_reserved_explicit_name():
    """Explicit names further down the file are passed in as used."""
    entries = [entry(peer_id="c"), entry(peer_id="d")]

    prepared = prepare(entries, "sqd-{n:03d}", used_names={"sqd-002"})

    assert [p.name for p in prepared] == ["sqd-001", "sqd-003"]


def test_a_template_without_n_does_not_hang():
    """Searching for an unused value would never terminate; it must not try."""
    entries = [entry(peer_id="c"), entry(peer_id="d")]

    prepared = prepare(entries, "fixed-{peer_id}", used_names={"fixed-c"})

    assert [p.name for p in prepared] == ["fixed-c", "fixed-d"]
