import json
import random

import pytest

from sqdreg.naming import (
    DEFAULT_BATCH_SIZE,
    base_words,
    peer_suffix,
    pick_batch_words,
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


def test_an_entry_with_no_name_and_no_template_gets_a_generated_one():
    """Nameless registration is unmanageable at 1000 nodes, so never happens."""
    prepared = prepare([entry(peer_id="a"), entry(peer_id="b", name="named")], None)

    assert prepared[1].name == "named"
    generated = prepared[0].name
    assert generated and generated != "named"
    assert "-" in generated  # a friendly two-word slug
    assert prepared[0].metadata == '{"name":"%s"}' % generated


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


# --- batch-generated names -------------------------------------------------


def test_a_batch_shares_one_word_and_the_next_batch_gets_another():
    entries = [entry(peer_id=f"peer-{i:02d}") for i in range(5)]

    prepared = prepare(entries, None, batch_size=2)

    words = [p.name.split("-", 1)[0] for p in prepared]
    assert words[0] == words[1] != words[2] == words[3] != words[4]


def test_the_suffix_is_the_tail_of_the_peer_id():
    prepared = prepare([entry(peer_id="12D3KooWabcXYZ123456")], None)

    assert prepared[0].name.endswith("-123456")
    assert peer_suffix("12D3KooWabcXYZ123456") == "123456"


def test_names_are_unique_across_a_realistic_batch():
    """Uniqueness comes from the peer ID tail, not from collision retries."""
    entries = [entry(peer_id=f"12D3KooW{i:08d}") for i in range(1000)]

    prepared = prepare(entries, None, batch_size=DEFAULT_BATCH_SIZE)

    names = [p.name for p in prepared]
    assert len(set(names)) == 1000


def test_a_thousand_nodes_makes_twenty_groups_by_default():
    entries = [entry(peer_id=f"12D3KooW{i:08d}") for i in range(1000)]

    prepared = prepare(entries, None)

    assert len({p.name.split("-", 1)[0] for p in prepared}) == 20


def test_batch_size_controls_the_number_of_groups():
    entries = [entry(peer_id=f"12D3KooW{i:08d}") for i in range(100)]

    prepared = prepare(entries, None, batch_size=10)

    assert len({p.name.split("-", 1)[0] for p in prepared}) == 10


def test_a_word_already_used_is_not_drawn_again():
    """Redrawing a word would make two separate batches look like one."""
    pool_word = pick_batch_words(1, rng=random.Random(1))[0]

    chosen = pick_batch_words(
        5, exclude={pool_word}, rng=random.Random(1)
    )

    assert pool_word not in chosen


def test_words_already_in_the_log_are_excluded():
    entries = [entry(peer_id="12D3KooWaaaaaa")]
    first = prepare(entries, None)[0].name.split("-", 1)[0]

    again = prepare(entries, None, used_names={f"{first}-zzzzzz"})[0]

    assert again.name.split("-", 1)[0] != first


def test_base_words_reads_the_word_off_existing_names():
    assert base_words({"otter-E2uQHC", "kestrel-8CbBZ5", "nameless"}) == {
        "otter",
        "kestrel",
    }


def test_running_out_of_words_is_a_clear_error():
    with pytest.raises(NamingError, match="raise --batch"):
        pick_batch_words(10_000)


def test_a_batch_size_below_one_is_rejected():
    with pytest.raises(NamingError, match="at least 1"):
        prepare([entry(peer_id="a")], None, batch_size=0)


def test_an_explicit_name_still_wins():
    prepared = prepare([entry(peer_id="a", name="mine")], None)

    assert prepared[0].name == "mine"


def test_a_template_still_wins_and_suppresses_batching():
    prepared = prepare([entry(peer_id="a")], "sqd-{n:03d}")

    assert prepared[0].name == "sqd-001"


def test_explicit_names_do_not_consume_batch_slots():
    """Only entries that need a generated name count toward a batch."""
    entries = [
        entry(peer_id="p1"),
        entry(peer_id="p2", name="explicit"),
        entry(peer_id="p3"),
    ]

    prepared = prepare(entries, None, batch_size=2)

    generated = [p.name.split("-", 1)[0] for p in prepared if p.name != "explicit"]
    assert generated[0] == generated[1]  # both in the first batch of two


def test_the_word_pool_is_not_one_category():
    """Animals only would make every batch word an animal."""
    from sqdreg.naming import _word_pool

    pool = set(_word_pool())
    assert len(pool) > 1000
    # A sampling of coolname categories that are not animals.
    assert {"lemon", "quartz", "furious"} & pool
    assert not any("-" in w or " " in w for w in pool)


def test_batch_words_are_drawn_across_categories():
    words = set(pick_batch_words(60, rng=random.Random(7)))
    from sqdreg.naming import _word_pool

    animals = set()
    from coolname.data import config

    for key in ("animal", "animal_breed", "animal_legendary"):
        animals |= set(config[key]["words"])
    # Some animals are fine; all of them would mean a single-category pool.
    assert not words <= animals


# --- website and description -----------------------------------------------


def test_website_and_description_are_encoded_alongside_the_name():
    metadata = encode_metadata("w1", "https://example.com/", "Hosted by Example")

    assert json.loads(metadata) == {
        "name": "w1",
        "website": "https://example.com/",
        "description": "Hosted by Example",
    }


def test_absent_fields_are_omitted_not_blank():
    """A blank website is worse than none: the indexer would show "" not null."""
    assert json.loads(encode_metadata("w1")) == {"name": "w1"}
    assert json.loads(encode_metadata("w1", website="https://x")) == {
        "name": "w1",
        "website": "https://x",
    }


def test_metadata_key_order_is_stable():
    """Byte length feeds the gas estimate, so the encoding must be predictable."""
    first = encode_metadata("w1", "https://x", "d")
    assert first == encode_metadata("w1", "https://x", "d")
    assert first.index('"name"') < first.index('"website"') < first.index('"description"')


def test_a_worker_with_only_a_website_still_encodes():
    assert json.loads(encode_metadata(None, "https://x")) == {"website": "https://x"}


def test_nothing_at_all_encodes_empty():
    assert encode_metadata(None, None, None) == ""


def test_the_cap_error_points_at_the_run_wide_fields():
    with pytest.raises(NamingError, match="apply to every node"):
        encode_metadata("w1", "https://x", "d" * 300)


def test_prepare_applies_the_fields_to_every_entry():
    entries = [entry(peer_id="a"), entry(peer_id="b", name="explicit")]

    prepared = prepare(
        entries, None, website="https://nodexeus.com/", description="Hosted"
    )

    for item in prepared:
        parsed = json.loads(item.metadata)
        assert parsed["website"] == "https://nodexeus.com/"
        assert parsed["description"] == "Hosted"
    # and the names are still per-entry
    assert json.loads(prepared[1].metadata)["name"] == "explicit"


def test_the_branding_defaults_are_applied_without_any_flag():
    import bulk_register
    from sqdreg.naming import DEFAULT_DESCRIPTION, DEFAULT_WEBSITE

    args = bulk_register.parse_args(["peers.txt"])

    assert args.website == DEFAULT_WEBSITE
    assert args.description == DEFAULT_DESCRIPTION


def test_an_empty_string_suppresses_a_default_field():
    import bulk_register

    args = bulk_register.parse_args(
        ["peers.txt", "--website", "", "--description", ""]
    )
    prepared = prepare(
        [entry(peer_id="a")], None,
        website=args.website, description=args.description,
    )

    assert json.loads(prepared[0].metadata).keys() == {"name"}


def test_the_defaults_leave_room_under_the_byte_cap():
    """Guards against a future default that cannot fit alongside a long name."""
    from sqdreg.naming import DEFAULT_DESCRIPTION, DEFAULT_WEBSITE

    longest_plausible_name = "x" * 40
    size = len(
        encode_metadata(
            longest_plausible_name, DEFAULT_WEBSITE, DEFAULT_DESCRIPTION
        ).encode()
    )
    assert size < MAX_METADATA_BYTES
