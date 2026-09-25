"""Cross-check a day of actual Distributed events against GraphQL commitments."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web3 import Web3
from sqdreg.networks import NETWORKS
from tools.health import DISTRIBUTED, DISTRIBUTED_ABI, LOG_CHUNK, L2_BLOCKS_PER_DAY
from collect import OUT, save

n = NETWORKS['mainnet']
w3 = Web3(Web3.HTTPProvider(n.rpc_url, request_kwargs={'timeout': 30}))
assert w3.eth.chain_id == n.chain_id
end = w3.eth.block_number
start = end - L2_BLOCKS_PER_DAY
c = w3.eth.contract(address=w3.to_checksum_address(n.rewards_distribution), abi=DISTRIBUTED_ABI)
topic = Web3.to_hex(w3.keccak(text=DISTRIBUTED))
rows = []
for first in range(start, end + 1, LOG_CHUNK):
    logs = w3.eth.get_logs({'address': c.address, 'topics': [topic], 'fromBlock': first, 'toBlock': min(first + LOG_CHUNK - 1, end)})
    for log in logs:
        a = c.events.Distributed().process_log(log)['args']
        rows.append({'blockNumber': log['blockNumber'], 'tx': log['transactionHash'].hex(), 'logIndex': log['logIndex'],
                     'fromBlock': a['fromBlock'], 'toBlock': a['toBlock'],
                     'recipients': [{'workerId': str(i), 'workerReward': str(w), 'stakerReward': str(s)} for i,w,s in zip(a['recipients'], a['workerRewards'], a['stakerRewards'])]})
    print(first, len(rows), flush=True)
save(OUT / 'chain.json.gz', {'startBlock': start, 'endBlock': end, 'endTimestamp': w3.eth.get_block(end)['timestamp'], 'events': rows})
print('saved', len(rows), 'events', flush=True)
