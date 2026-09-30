"""Deleting nodes whose worker is deregistered.

A delete cannot be undone, so the failures that matter are the ones that
delete a node still earning: a re-registered peer ID whose old DEREGISTERED
record sits beside a live one, an indexer that simply does not know the peer
ID, or a dry run that deletes anyway.
"""

from tools import prune_deregistered as prune
from tools.prune_deregistered import Node

LIVE = "12D3KooWSDUxgrkSSEG6gaMstBK3joQeWUiZ43H6AeEXQ5zELJc8"
DEAD = "12D3KooWLWc2QdbiRbfwHZpTN1UHdSE7a1vjqhs5gQP9TQB51yA4"

BV_LIST = """\
------------------------------------------------------------------------
 ID                                    Name                   Image                               State    IP Address     Uptime  [h:m:s]
 84711723-60a0-4839-b6dd-49d88cc3fbe0  honestly-notable-fly   sqd/sqd-worker-mainnet/2.16.0-nr.3  Running  92.205.34.126  2d 12:38:13
 0bb2f359-fdab-4187-9873-afc0f51124ce  allegedly-teaching     ethereum/reth-mainnet-full/2.6.0    Running  38.102.126.69  2d 15:27:53
 c507e6d0-1782-4e98-8f4e-5ccfa5935a1a  immensely-curious-pig  sqd/sqd-worker-mainnet/2.16.0-nr.3  Stopped  92.205.34.120  0d 00:00:00
------------------------------------------------------------------------
"""


def test_table_rows_are_found_and_header_and_rules_ignored():
    assert prune.parse_node_list(BV_LIST) == [
        ("honestly-notable-fly", "sqd/sqd-worker-mainnet/2.16.0-nr.3"),
        ("allegedly-teaching", "ethereum/reth-mainnet-full/2.6.0"),
        ("immensely-curious-pig", "sqd/sqd-worker-mainnet/2.16.0-nr.3"),
    ]


def test_only_deregistered_everywhere_is_deleted():
    assert prune.should_delete(("DEREGISTERED",))
    assert prune.should_delete(("DEREGISTERED", "DEREGISTERED"))


def test_a_reregistered_peer_id_is_kept():
    """An old DEREGISTERED record beside a live one is a working node."""
    assert not prune.should_delete(("DEREGISTERED", "ACTIVE"))


def test_unknown_or_in_flight_statuses_are_kept():
    assert not prune.should_delete(())
    for status in ("ACTIVE", "REGISTERING", "DEREGISTERING", "WITHDRAWN"):
        assert not prune.should_delete((status,))


def test_a_node_with_a_problem_is_never_deleted():
    node = Node("h", "n", "img", peer_id=DEAD, statuses=("DEREGISTERED",),
                problem="address is not a peer ID")
    assert prune.action_for(node) == "skip"


def test_statuses_group_every_record_under_its_peer_id():
    def post(payload):
        assert set(payload["variables"]["ids"]) == {LIVE, DEAD}
        return {"data": {"workers": [
            {"peerId": DEAD, "status": "DEREGISTERED"},
            {"peerId": LIVE, "status": "DEREGISTERED"},
            {"peerId": LIVE, "status": "ACTIVE"},
        ]}}

    assert prune.fetch_statuses([LIVE, DEAD], post) == {
        DEAD: ("DEREGISTERED",),
        LIVE: ("DEREGISTERED", "ACTIVE"),
    }


def test_statuses_cover_every_chunk(monkeypatch):
    monkeypatch.setattr(prune, "QUERY_CHUNK", 1)
    seen = []

    def post(payload):
        seen.append(payload["variables"]["ids"])
        return {"data": {"workers": [
            {"peerId": p, "status": "ACTIVE"} for p in payload["variables"]["ids"]
        ]}}

    assert set(prune.fetch_statuses([LIVE, DEAD], post)) == {LIVE, DEAD}
    assert seen == [[LIVE], [DEAD]]


def fake_fleet(monkeypatch, statuses):
    """One host with a live and a deregistered SQD node; records every ssh."""
    calls = []

    def ssh(login, host, command):
        calls.append(command)
        if command == "bv node list":
            return 0, BV_LIST
        if "bv node run address" in command:
            return 0, (f"honestly-notable-fly\t{LIVE}\n"
                       f"immensely-curious-pig\t{DEAD}\n")
        return 0, "deleted"

    monkeypatch.setattr(prune, "tsh_hosts", lambda pattern: ["sqd208"])
    monkeypatch.setattr(prune, "ssh", ssh)
    monkeypatch.setattr(prune, "fetch_statuses", lambda ids: statuses)
    return calls


def deletes(calls):
    return [c for c in calls if "delete" in c]


def test_dry_run_deletes_nothing(monkeypatch, capsys):
    calls = fake_fleet(monkeypatch, {LIVE: ("ACTIVE",), DEAD: ("DEREGISTERED",)})
    prune.main([])
    assert deletes(calls) == []
    assert "rerun with --apply to delete 1 node(s)" in capsys.readouterr().out


def test_apply_deletes_only_the_deregistered_node(monkeypatch):
    calls = fake_fleet(monkeypatch, {LIVE: ("ACTIVE",), DEAD: ("DEREGISTERED",)})
    prune.main(["--apply"])
    assert deletes(calls) == ["bv n delete immensely-curious-pig -y"]


def test_apply_skips_a_peer_id_the_indexer_does_not_know(monkeypatch):
    calls = fake_fleet(monkeypatch, {LIVE: ("ACTIVE",)})
    prune.main(["--apply"])
    assert deletes(calls) == []


def test_non_sqd_images_are_never_looked_up(monkeypatch):
    calls = fake_fleet(monkeypatch, {})
    prune.main([])
    address_call = next(c for c in calls if "bv node run address" in c)
    assert "allegedly-teaching" not in address_call
