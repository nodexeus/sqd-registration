#!/usr/bin/env python3
"""Delete bv nodes whose SQD mainnet worker has been deregistered.

    .venv/bin/python tools/prune_deregistered.py                  # dry run
    .venv/bin/python tools/prune_deregistered.py --apply          # delete
    .venv/bin/python tools/prune_deregistered.py --hosts 'sqd2*'  # subset

Walks every host `tsh ls` knows about, finds the SQD mainnet worker nodes that
bv runs there, asks each for its peer ID, and looks that peer ID up in the SQD
network indexer. A node whose worker is DEREGISTERED is only burning a server:
it can never earn again under that peer ID.

Nothing is deleted without --apply. Even then a node is deleted only when the
indexer knows the peer ID and every record it holds for it is DEREGISTERED. A
peer ID can be registered again after a deregistration, which leaves an old
DEREGISTERED record beside a live one, so "any record says DEREGISTERED" would
delete a working node. An empty answer, an error, or anything unexpected
leaves the node alone: deleting is the one step here that cannot be undone.
"""

import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Run from anywhere: this lives one level below the package it imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqdreg.peerids import PeerIdError, decode_peer_id

INDEXER = "https://subsquid.squids.live/subsquid-network-mainnet/graphql"
IMAGE_PREFIX = "sqd/sqd-worker-mainnet/"
DEREGISTERED = "DEREGISTERED"
SSH_TIMEOUT = 120
QUERY_CHUNK = 100

# bv prints a table even when asked for JSON, so rows are found by their
# leading node UUID rather than by position under the header.
ROW = re.compile(r"^\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s")
# Names are interpolated into a remote shell; bv's own are word-dash-word.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class Node:
    host: str
    name: str
    image: str
    peer_id: str | None = None
    statuses: tuple[str, ...] = ()
    problem: str | None = None


def tsh_hosts(pattern):
    """Every hostname Teleport lists, optionally narrowed by a glob."""
    out = subprocess.run(
        ["tsh", "ls", "--format=json"], capture_output=True, text=True,
        check=True,
    ).stdout
    hosts = sorted(n["spec"]["hostname"] for n in json.loads(out))
    return [h for h in hosts if fnmatch.fnmatch(h, pattern)]


def ssh(login, host, command):
    """Run `command` on `host`; returns (exit code, combined output)."""
    try:
        proc = subprocess.run(
            ["tsh", "ssh", f"{login}@{host}", command],
            capture_output=True, text=True, timeout=SSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {SSH_TIMEOUT}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def parse_node_list(text):
    """[(name, image)] for every row of a `bv node list` table."""
    rows = []
    for line in text.splitlines():
        if ROW.match(line):
            fields = line.split()
            rows.append((fields[1], fields[2]))
    return rows


def parse_addresses(text):
    """{name: address} from the `name<TAB>address` lines we print remotely."""
    out = {}
    for line in text.splitlines():
        name, sep, address = line.partition("\t")
        if sep:
            out[name.strip()] = address.strip()
    return out


def discover(login, host):
    """The SQD mainnet worker nodes on one host, with their peer IDs."""
    code, text = ssh(login, host, "bv node list")
    if code != 0:
        return [], f"bv node list failed ({code}): {text.splitlines()[-1] if text else ''}"

    nodes = []
    for name, image in parse_node_list(text):
        if not image.startswith(IMAGE_PREFIX):
            continue
        node = Node(host, name, image)
        if not SAFE_NAME.match(name):
            node.problem = "unexpected characters in node name"
        nodes.append(node)

    wanted = [n.name for n in nodes if not n.problem]
    if not wanted:
        return nodes, None

    script = "; ".join(
        f"printf '%s\\t%s\\n' {shlex.quote(n)} "
        f"\"$(bv node run address {shlex.quote(n)} 2>&1 | tail -n1)\""
        for n in wanted
    )
    code, text = ssh(login, host, script)
    addresses = parse_addresses(text) if code == 0 else {}
    for node in nodes:
        if node.problem:
            continue
        address = addresses.get(node.name)
        if address is None:
            node.problem = f"no address returned ({code})"
            continue
        try:
            decode_peer_id(address)
        except PeerIdError:
            node.problem = f"address is not a peer ID: {address[:60]!r}"
            continue
        node.peer_id = address
    return nodes, None


def fetch_statuses(peer_ids, post=None):
    """{peer_id: (status, ...)} for every peer ID the indexer knows."""
    post = post or _post
    found = {}
    for i in range(0, len(peer_ids), QUERY_CHUNK):
        chunk = peer_ids[i:i + QUERY_CHUNK]
        body = post({
            "query": "query($ids: [String!]) {"
                     " workers(where: {peerId_in: $ids}) { peerId status } }",
            "variables": {"ids": chunk},
        })
        if body.get("errors"):
            raise RuntimeError(f"indexer error: {body['errors']}")
        for worker in body["data"]["workers"]:
            found.setdefault(worker["peerId"], []).append(worker["status"])
    return {k: tuple(v) for k, v in found.items()}


def _post(payload):
    request = urllib.request.Request(
        INDEXER, data=json.dumps(payload).encode(),
        # The indexer's edge rejects urllib's default User-Agent with a 403.
        headers={"content-type": "application/json",
                 "user-agent": "sqd-registration/prune-deregistered"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def should_delete(statuses):
    """Only a peer ID the indexer knows, and knows only as DEREGISTERED."""
    return bool(statuses) and all(s == DEREGISTERED for s in statuses)


def action_for(node):
    if node.problem:
        return "skip"
    if not node.statuses:
        return "skip"
    return "delete" if should_delete(node.statuses) else "keep"


def delete(login, node):
    code, text = ssh(login, node.host, f"bv n delete {shlex.quote(node.name)} -y")
    return code == 0, text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete; without it this is a dry run")
    parser.add_argument("--hosts", default="*",
                        help="glob over tsh hostnames (default: all)")
    parser.add_argument("--login", default="root", help="ssh login (default root)")
    parser.add_argument("--parallel", type=int, default=8,
                        help="hosts to query at once (default 8)")
    args = parser.parse_args(argv)

    hosts = tsh_hosts(args.hosts)
    print(f"hosts: {len(hosts)}    mode: {'APPLY' if args.apply else 'dry run'}",
          flush=True)

    nodes, host_errors = [], {}
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for host, (found, error) in zip(
            hosts, pool.map(lambda h: discover(args.login, h), hosts)
        ):
            nodes += found
            if error:
                host_errors[host] = error

    peer_ids = sorted({n.peer_id for n in nodes if n.peer_id})
    try:
        statuses = fetch_statuses(peer_ids)
    except Exception as exc:  # no answer means no deletes, not a guess
        sys.exit(f"error: could not query the indexer: {exc}")
    for node in nodes:
        if node.peer_id:
            node.statuses = statuses.get(node.peer_id, ())
            if not node.statuses:
                node.problem = "peer ID not in the indexer"

    print(f"\n{'host':<24} {'node':<30} {'peer ID':<54} {'status':<24} action")
    for node in sorted(nodes, key=lambda n: (action_for(n) != "delete", n.host, n.name)):
        status = ",".join(node.statuses) or (node.problem or "")
        print(f"{node.host:<24} {node.name:<30} {node.peer_id or '-':<54} "
              f"{status:<24} {action_for(node)}")

    doomed = [n for n in nodes if action_for(n) == "delete"]
    failed = []
    if args.apply:
        print()
        for node in doomed:
            ok, text = delete(args.login, node)
            print(f"  {'deleted' if ok else 'FAILED '} {node.host} {node.name}"
                  + ("" if ok else f": {text.splitlines()[-1] if text else ''}"),
                  flush=True)
            if not ok:
                failed.append(node)

    counts = {a: sum(1 for n in nodes if action_for(n) == a)
              for a in ("delete", "keep", "skip")}
    print(f"\nSQD mainnet nodes: {len(nodes)}    "
          f"deregistered: {counts['delete']}    active/other: {counts['keep']}    "
          f"skipped: {counts['skip']}")
    if host_errors:
        print(f"hosts without a usable bv ({len(host_errors)}):")
        for host, error in sorted(host_errors.items()):
            print(f"  {host}: {error}")
    if doomed and not args.apply:
        print(f"\ndry run: rerun with --apply to delete {len(doomed)} node(s)")
    if failed:
        sys.exit(f"{len(failed)} delete(s) failed")


if __name__ == "__main__":
    main()
