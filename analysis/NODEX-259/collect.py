"""Capture public SQD GraphQL evidence with stable keyset pagination.

Run with Python 3.11 + requests. Existing pages are reused for this capture;
use a new output directory for a new snapshot. No signing or wallet access.
"""
import datetime as dt
import gzip
import json
from pathlib import Path
import time
import requests

OUT = Path(__file__).parent / 'raw'
URL = 'https://subsquid.squids.live/subsquid-network-mainnet/graphql'
OUT.mkdir(exist_ok=True)

def query(q):
    for attempt in range(5):
        try:
            r = requests.post(URL, json={'query': q}, timeout=60)
            r.raise_for_status()
            result = r.json()
            if 'errors' in result:
                raise ValueError(result['errors'])
            return result['data']
        except (requests.RequestException, ValueError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)

def save(path, data):
    with gzip.open(path, 'wt') as f:
        json.dump(data, f, separators=(',', ':'))

def read(path):
    with gzip.open(path, 'rt') as f:
        return json.load(f)

def main():
    meta_path = OUT / 'metadata.json'
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
    else:
        now = dt.datetime.now(dt.timezone.utc)
        meta = {'endpoint': URL, 'captured_at': now.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                'cutoff': (now - dt.timedelta(days=91)).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                'counts': query('{ workersConnection(orderBy:id_ASC){totalCount} }'),
                'indexer_commit': '302b8f19bbe7280dd0e3f60eb5242acbb19576bb',
                'dashboard_commit': '0ad8de31818a84bd343149317f27cbcc7d456b47'}
        meta_path.write_text(json.dumps(meta, indent=2))
    fields = 'id peerId ownerId name bond apr stakerApr totalDelegation capedDelegation claimableReward claimedReward totalDelegationRewards status createdAt online jailed uptime24Hours uptime90Days liveness dTenure trafficWeight'
    for entity, selection, size, extra in [
        ('workers', fields, 250, ''),
        ('commitments', 'id from to fromBlock toBlock recipients {workerId workerReward workerApr stakerReward stakerApr}', 25,
         ',to_gte:' + json.dumps(meta['cutoff']) + ',to_lte:' + json.dumps(meta['captured_at'])),
    ]:
        after = ''
        page = 0
        total = 0
        while True:
            path = OUT / f'{entity}-{page:04}.json.gz'
            if path.exists():
                rows = read(path)
            else:
                q = '{' + entity + '(limit:' + str(size) + ',orderBy:id_ASC,where:{id_gt:' + json.dumps(after) + extra + '}) {' + selection + '}}'
                rows = query(q)[entity]
                save(path, rows)
            if not rows:
                break
            assert all(r['id'] > after for r in rows)
            assert len({r['id'] for r in rows}) == len(rows)
            after = rows[-1]['id']
            total += len(rows)
            page += 1
            print(entity, page, total, flush=True)
        print('complete', entity, total, flush=True)
    meta['collection_finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))

if __name__ == '__main__':
    main()
