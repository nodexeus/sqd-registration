## Information
1. For any of the old nodes, if their wallets are not in Fireblocks, add `--signer local` to the command.
e.g. `./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action claim` -> `./sqd batches/peers-03-eoa-cd65B8Be-20.txt --action claim --signer local`

## Setup
1. `cp fireblocks.env.example fireblocks.env`
2. Populate: *FIREBLOCKS_API_KEY=* and *FIREBLOCKS_API_PRIVATE_KEY_PATH=*
3. Install pre-requisites:
  ```
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  npm install @fireblocks/fireblocks-json-rpc
  ```
4. Verify setup:
  ```
  ./sqd check.txt --action status
  ```

## Deregister
### Test
` ./sqd batches/peers-01-eoa-cd65B8Be-20.txt --action deregister --dry-run`

**Verify no errors**
*Run for real* (11 commands)
```
./sqd batches/peers-01-eoa-cd65B8Be-20.txt --action deregister
./sqd batches/peers-02-eoa-dADB8013-1.txt --action deregister
./sqd batches/peers-03-eoa-5CF5A099-275.txt --action deregister
./sqd batches/peers-04-eoa-43d6A791-65.txt --action deregister
./sqd batches/peers-05-vesting-5CF5A099-10.txt --action deregister
./sqd batches/peers-06-vesting-80c88d21-130.txt --action deregister
./sqd batches/peers-07-vesting-678d14c7-100.txt --action deregister
./sqd batches/peers-08-vesting-2aA9ADb8-100.txt --action deregister
./sqd batches/peers-09-vesting-51FF3579-61.txt --action deregister
./sqd batches/peers-10-vesting-dADB8013-49.txt --action deregister
./sqd batches/peers-11-vesting-A205c6e3-188.txt --action deregister
```


## Claim Outstanding Rewards
*Adding `--dry-run` for any of these will show pending available SQD*

```
./sqd batches/peers-01-eoa-cd65B8Be-20.txt --action claim --dry-run
registered by: 0xcd65B8BeeE22C8D1De6653A47e928d387e037aD3
(dry run: no credential needed)
would be signed by: 0xcd65B8BeeE22C8D1De6653A47e928d387e037aD3
network:     mainnet (chain 42161)
wallet:      0xcd65B8BeeE22C8D1De6653A47e928d387e037aD3
workers:     20
claimable:   2495.423041214096680381 SQD
gas:         ~0.0000188315415 ETH max
ETH balance: 0.019915841567404 ETH (0xcd65B8BeeE22C8D1De6653A47e928d387e037aD3)

-- dry run, nothing sent --
  would claim 2495.423041214096680381 SQD to 0xcd65B8BeeE22C8D1De6653A47e928d387e037aD3
```

```
./sqd batches/peers-01-eoa-cd65B8Be-20.txt --action claim
./sqd batches/peers-02-eoa-dADB8013-1.txt --action claim
./sqd batches/peers-03-eoa-5CF5A099-275.txt --action claim
./sqd batches/peers-04-eoa-43d6A791-65.txt --action claim
./sqd batches/peers-05-vesting-5CF5A099-10.txt --action claim
./sqd batches/peers-06-vesting-80c88d21-130.txt --action claim
./sqd batches/peers-07-vesting-678d14c7-100.txt --action claim
./sqd batches/peers-08-vesting-2aA9ADb8-100.txt --action claim
./sqd batches/peers-09-vesting-51FF3579-61.txt --action claim
./sqd batches/peers-10-vesting-dADB8013-49.txt --action claim
./sqd batches/peers-11-vesting-A205c6e3-188.txt --action claim
```


## Register new nodes
### Options
**Naming:** To follow similar patterns you've used you can use the `--name-template` option

**Batches:** You can run in a defined batch size using `--limit 100` option to limit to 100 (replace 100 with any number)

**Examples:**

*Create nodes named `Valoria-1` through `Valoria-100`*

`./sqd new_peer_ids.txt --limit 100 --name-template Valoria-{n:1d}` 

If you run that exact same command again, it would then create `Valoria-101` through `Valoria-200`

If you run that command again but change `Valoria-{n:1d}` to `alpha-{n:1d}` the next batch would be named `alpha-1` through `alpha-100`.


*Test one node*

1. `./sqd new_peer_ids.txt --name-template nodename-{n:1d} --limit 1`


*If no errors, test a batch of 10*

2. `./sqd new_peer_ids.txt --name-template nodename-{n:1d} --limit 10`


*Still no errors, run remaining* (Group in to desired batch size with `--limit`)

3. `./sqd new_peer_ids.txt --name-template nodename-{n:1d}`
