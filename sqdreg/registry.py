"""Wrapper around the SQD WorkerRegistration contract and its bond token."""

from dataclasses import dataclass

from sqdreg.networks import Network

# Gas for register() when estimation is unavailable. Measured with read-only
# eth_estimateGas against Arbitrum One (state overrides supplying the bond
# balance and allowance; nothing was sent):
#
#   metadata ""                            ->  358,661 gas
#   metadata {"name":"nodexeus-001"} (23B) ->  379,718 gas
#   metadata 251 bytes                     ->  560,203 gas
#
# That is roughly 915 gas per metadata byte on top of a ~358,661 floor, so the
# 256-byte cap in naming.MAX_METADATA_BYTES costs about
# 358,661 + 256 * 915 = 592,901. The constant must cover that on its own: the
# previous 350,000 was below even the empty-metadata cost and only survived
# because of the caller's 25% pad.
FALLBACK_REGISTER_GAS = 600_000
# Real mainnet transactions used 55,746-81,581 gas for deregister() and
# 118,104-132,037 for withdraw(). Rounded well clear of the observed maxima.
# Both are far cheaper than register(): neither moves a bond in, and neither
# writes metadata.
FALLBACK_DEREGISTER_GAS = 120_000
# claim() loops over every worker the wallet owns, so its gas scales with the
# fleet. Measured on mainnet: 82,011 gas for 0 workers and 1,619,348 for 201,
# i.e. about 7,650 per worker. The fallback covers a 1000-worker sweep.
FALLBACK_CLAIM_BASE_GAS = 100_000
CLAIM_GAS_PER_WORKER = 8_000
FALLBACK_WITHDRAW_GAS = 200_000

# A worker's position in its lifecycle, derived from on-chain state.
UNREGISTERED = "unregistered"
# Registered and live, so not a registration candidate.
REGISTERED = "registered"
# Registered, but register() sets registeredAt = nextEpoch(), so the worker is
# not live until that boundary arrives and cannot be deregistered before then.
REGISTERING = "registering"
ACTIVE = "active"
DEREGISTERING = "deregistering"
LOCKED = "locked"
WITHDRAWABLE = "withdrawable"
FOREIGN = "foreign"

# Worker struct field order: creator, peerId, bond, registeredAt,
# deregisteredAt, metadata.
_REGISTERED_AT_INDEX = 3

_WORKER_COMPONENTS = [
    {"name": "creator", "type": "address"},
    {"name": "peerId", "type": "bytes"},
    {"name": "bond", "type": "uint256"},
    {"name": "registeredAt", "type": "uint128"},
    {"name": "deregisteredAt", "type": "uint128"},
    {"name": "metadata", "type": "string"},
]

WORKER_REGISTRATION_ABI = [
    {
        "inputs": [
            {"name": "peerId", "type": "bytes"},
            {"name": "metadata", "type": "string"},
        ],
        "name": "register",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "peerId", "type": "bytes"}],
        "name": "workerIds",
        "outputs": [{"name": "id", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "workerId", "type": "uint256"}],
        "name": "getWorker",
        "outputs": [
            {"name": "", "type": "tuple", "components": _WORKER_COMPONENTS}
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "bondAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "SQD",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "peerId", "type": "bytes"}],
        "name": "deregister",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "peerId", "type": "bytes"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "workerId", "type": "uint256"}],
        "name": "isWorkerActive",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "lockPeriod",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "getOwnedWorkers",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class WorkerState:
    """Where one peer ID sits in the worker lifecycle, read from the chain."""

    peer_bytes: bytes
    worker_id: int
    creator: str
    bond: int
    registered_at: int
    deregistered_at: int
    is_active: bool
    state: str
    unlock_block: int | None = None
    # Already present in the getWorker() response, so keeping it costs no
    # extra call. deregister and withdraw use it to name the worker they are
    # about to act on, which is how an operator checks it is the right one.
    metadata: str = ""


class Registry:
    """Reads and unsigned-transaction builders for WorkerRegistration.

    Transactions are returned unsigned; signing and sending belong to the
    caller so this class stays trivially mockable in tests.
    """

    def __init__(self, w3, network: Network, address: str):
        self.w3 = w3
        self.network = network
        self.address = address
        self.contract = w3.eth.contract(
            address=w3.to_checksum_address(network.worker_registration),
            abi=WORKER_REGISTRATION_ABI,
        )
        self._token = None

    # --- reads ---

    def is_registered(self, peer_bytes: bytes) -> bool:
        """Whether the registry holds a *live* registration for this peer ID.

        Two reads, not one: `withdraw()` deletes the worker but leaves
        `workerIds[peerId]` pointing at the vacated slot, and `register()`
        explicitly allows re-registering it. Trusting `workerIds` alone would
        permanently skip any peer ID that had been cycled out.
        """
        worker_id = self.contract.functions.workerIds(peer_bytes).call()
        if worker_id == 0:
            return False
        worker = self.contract.functions.getWorker(worker_id).call()
        return worker[_REGISTERED_AT_INDEX] != 0

    def lock_period(self) -> int:
        return self.contract.functions.lockPeriod().call()

    def owned_worker_ids(self) -> set[int]:
        """Worker IDs this account has ever created.

        `withdraw()` zeroes the worker but leaves the id in `ownedWorkers`, so
        this is the only way to tell a vacated slot we may re-register from one
        belonging to somebody else — `register()` requires membership here.
        """
        return set(self.contract.functions.getOwnedWorkers(self.address).call())

    def worker_state(
        self, peer_bytes: bytes, l1_block: int, lock_period: int, owned: set[int]
    ) -> WorkerState:
        """Classify one peer ID against the contract's own preconditions.

        `l1_block` must be the block number the *contract* sees. On Arbitrum
        that is the L1 block number, which is an order of magnitude smaller
        than the L2 number `eth_blockNumber` returns; comparing the L2 number
        against `deregisteredAt + lockPeriod` would mark every locked worker
        withdrawable and produce a run of "Worker is locked" reverts.
        """
        worker_id = self.contract.functions.workerIds(peer_bytes).call()
        if worker_id == 0:
            return WorkerState(
                peer_bytes=peer_bytes,
                worker_id=0,
                creator="",
                bond=0,
                registered_at=0,
                deregistered_at=0,
                is_active=False,
                state=UNREGISTERED,
            )

        creator, _, bond, registered_at, deregistered_at, metadata = (
            self.contract.functions.getWorker(worker_id).call()
        )

        if registered_at == 0:
            # Vacated by withdraw(). Re-registerable only by the original
            # creator, whose address `delete` has already zeroed — so ownership
            # has to come from ownedWorkers.
            state = UNREGISTERED if worker_id in owned else FOREIGN
            return WorkerState(
                peer_bytes=peer_bytes,
                worker_id=worker_id,
                creator=creator,
                bond=bond,
                registered_at=0,
                deregistered_at=deregistered_at,
                is_active=False,
                state=state,
            )

        if creator.lower() != self.address.lower():
            return WorkerState(
                peer_bytes=peer_bytes,
                worker_id=worker_id,
                creator=creator,
                bond=bond,
                registered_at=registered_at,
                deregistered_at=deregistered_at,
                is_active=False,
                state=FOREIGN,
            )

        is_active = self.contract.functions.isWorkerActive(worker_id).call()
        if deregistered_at == 0:
            # Not yet live: deregister() requires isWorkerActive and would
            # revert, so this is a distinct state, not an active worker.
            state, unlock = (ACTIVE if is_active else REGISTERING), None
        else:
            unlock = deregistered_at + lock_period
            if is_active:
                # deregister() takes effect at the next epoch boundary,
                # which is workerEpochLength away -- 100 blocks, about 20
                # minutes on mainnet. Not to be confused with lockPeriod,
                # which is ~13.9 days and governs the bond release.
                state = DEREGISTERING
            elif l1_block < unlock:
                state = LOCKED
            else:
                state = WITHDRAWABLE

        return WorkerState(
            peer_bytes=peer_bytes,
            worker_id=worker_id,
            creator=creator,
            bond=bond,
            registered_at=registered_at,
            deregistered_at=deregistered_at,
            is_active=is_active,
            state=state,
            unlock_block=unlock,
            metadata=metadata,
        )

    def registration_state(self, peer_bytes: bytes, owned: set[int]) -> str:
        """Whether this account could register this peer ID right now.

        Returns UNREGISTERED (it could), REGISTERED (a live worker holds it), or
        FOREIGN (the slot exists but belongs to another account).

        The FOREIGN case is why `is_registered` is not enough on its own.
        withdraw() leaves `workerIds[peerId]` pointing at a vacated slot, so a
        peer ID somebody else registered and withdrew looks free — but
        register() requires `ownedWorkers[msg.sender]` to contain that worker,
        and reverts with "Worker already registered by different account". Left
        unchecked that costs a reverted transaction, and inflates the bond the
        funds check asks for.

        Same two reads as `is_registered`; `owned` is fetched once per run.
        """
        worker_id = self.contract.functions.workerIds(peer_bytes).call()
        if worker_id == 0:
            return UNREGISTERED
        worker = self.contract.functions.getWorker(worker_id).call()
        if worker[_REGISTERED_AT_INDEX] != 0:
            return REGISTERED
        return UNREGISTERED if worker_id in owned else FOREIGN

    def bond_amount(self) -> int:
        return self.contract.functions.bondAmount().call()

    def token(self):
        """The bond token, read from the registry rather than hardcoded."""
        if self._token is None:
            address = self.contract.functions.SQD().call()
            self._token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(address), abi=ERC20_ABI
            )
        return self._token

    def sqd_balance(self) -> int:
        return self.token().functions.balanceOf(self.address).call()

    def allowance(self) -> int:
        return (
            self.token().functions.allowance(self.address, self.contract.address).call()
        )

    def token_decimals(self) -> int:
        return self.token().functions.decimals().call()

    # --- writes ---

    def _base_tx(self, nonce: int | None, fees: dict) -> dict:
        """Common transaction fields.

        `nonce=None` omits it, for signers that assign their own — Fireblocks
        maintains its own nonce sequence per vault account, and supplying one
        would fight it.
        """
        tx = {
            "from": self.address,
            "chainId": self.network.chain_id,
            **fees,
        }
        if nonce is not None:
            tx["nonce"] = nonce
        return tx

    def build_approve(self, amount: int, nonce: int, fees: dict) -> dict:
        return self.token().functions.approve(
            self.contract.address, amount
        ).build_transaction(self._base_tx(nonce, fees))

    def build_register(
        self, peer_bytes: bytes, metadata: str, nonce: int, fees: dict, gas: int
    ) -> dict:
        """Build a register() transaction with gas supplied explicitly.

        Gas is never auto-estimated here: estimation reverts while the bond
        allowance is missing, which would abort an otherwise valid run.
        """
        return self.contract.functions.register(
            peer_bytes, metadata
        ).build_transaction({**self._base_tx(nonce, fees), "gas": gas})

    def build_deregister(
        self, peer_bytes: bytes, nonce: int, fees: dict, gas: int
    ) -> dict:
        return self.contract.functions.deregister(peer_bytes).build_transaction(
            {**self._base_tx(nonce, fees), "gas": gas}
        )

    def build_withdraw(
        self, peer_bytes: bytes, nonce: int, fees: dict, gas: int
    ) -> dict:
        return self.contract.functions.withdraw(peer_bytes).build_transaction(
            {**self._base_tx(nonce, fees), "gas": gas}
        )

    def estimate_deregister_gas(self, peer_bytes: bytes) -> tuple[int, bool]:
        return self._estimate(
            self.contract.functions.deregister(peer_bytes), FALLBACK_DEREGISTER_GAS
        )

    def estimate_withdraw_gas(self, peer_bytes: bytes) -> tuple[int, bool]:
        return self._estimate(
            self.contract.functions.withdraw(peer_bytes), FALLBACK_WITHDRAW_GAS
        )

    def _estimate(self, fn, fallback: int) -> tuple[int, bool]:
        """Estimate one call's gas, falling back when the node reverts it."""
        try:
            return fn.estimate_gas({"from": self.address}), True
        except Exception:
            return fallback, False

    def estimate_register_gas(
        self, peer_bytes: bytes, metadata: str
    ) -> tuple[int, bool]:
        """Estimate register() gas, falling back when estimation reverts.

        Returns (gas, exact). Estimation fails whenever the allowance is not
        yet in place — the normal case for a dry run on a fresh wallet — so
        any failure yields the documented fallback instead of an error.
        """
        try:
            gas = self.contract.functions.register(peer_bytes, metadata).estimate_gas(
                {"from": self.address}
            )
        except Exception:
            return FALLBACK_REGISTER_GAS, False
        return gas, True


REWARD_TREASURY_ABI = [
    {
        "inputs": [
            {"name": "rewardDistribution", "type": "address"},
            {"name": "sender", "type": "address"},
        ],
        "name": "claimable",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "rewardDistribution", "type": "address"}],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class Treasury:
    """Reward claiming, which is per wallet rather than per peer ID.

    `claim()` takes no peer ID: the distributor loops over
    `getOwnedWorkers(msg.sender)`, zeroes each worker's balance and adds
    staking rewards, so one transaction sweeps the whole fleet.
    """

    def __init__(self, w3, network: Network, address: str):
        self.w3 = w3
        self.network = network
        self.address = address
        self.distribution = w3.to_checksum_address(network.rewards_distribution)
        self.contract = w3.eth.contract(
            address=w3.to_checksum_address(network.reward_treasury),
            abi=REWARD_TREASURY_ABI,
        )

    def claimable(self) -> int:
        return self.contract.functions.claimable(
            self.distribution, self.address
        ).call()

    def build_claim(self, nonce: int | None, fees: dict, gas: int) -> dict:
        tx = {
            "from": self.address,
            "chainId": self.network.chain_id,
            "gas": gas,
            **fees,
        }
        if nonce is not None:
            tx["nonce"] = nonce
        return self.contract.functions.claim(self.distribution).build_transaction(tx)

    def estimate_claim_gas(self, worker_count: int) -> tuple[int, bool]:
        """Estimate the sweep, falling back to a per-worker projection.

        Estimation reverts when there is nothing to claim, which is exactly
        when a dry run is most likely, so the fallback scales with the fleet
        rather than being a single constant.
        """
        try:
            return (
                self.contract.functions.claim(self.distribution).estimate_gas(
                    {"from": self.address}
                ),
                True,
            )
        except Exception:
            return (
                FALLBACK_CLAIM_BASE_GAS + CLAIM_GAS_PER_WORKER * worker_count,
                False,
            )


VESTING_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "data", "type": "bytes"},
            {"name": "requiredApprove", "type": "uint256"},
        ],
        "name": "execute",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class DirectCalls:
    """Transactions sent straight from the signing account."""

    address = None

    def __init__(self, signer_address: str):
        self.signer_address = signer_address

    def wrap(self, tx: dict, required_approve: int = 0) -> dict:
        return tx


class VestingCalls:
    """Transactions routed through a holding contract's execute().

    A worker registered by a vesting contract has that contract as its
    `creator`, and register/deregister/withdraw all check
    `creator == msg.sender`. The beneficiary therefore cannot call them
    directly: it drives them through `execute(to, data, requiredApprove)`,
    which the contract restricts to its own `owner()`.

    `requiredApprove` also removes the separate approval step for
    registration: the contract approves exactly that amount immediately before
    forwarding the call, so no allowance is left standing afterwards.
    """

    def __init__(self, w3, network: Network, address: str, signer_address: str):
        self.w3 = w3
        self.network = network
        self.address = w3.to_checksum_address(address)
        self.signer_address = signer_address
        self.contract = w3.eth.contract(address=self.address, abi=VESTING_ABI)

    def controller(self) -> str:
        """The only account allowed to call execute()."""
        return self.contract.functions.owner().call()

    def wrap(self, tx: dict, required_approve: int = 0) -> dict:
        """Re-target an inner transaction through execute().

        The inner transaction is built normally and then unpacked: only its
        destination and calldata matter, since the outer call carries the
        sender, gas and fees.
        """
        outer = {
            "from": self.signer_address,
            "chainId": self.network.chain_id,
            "gas": tx["gas"],
        }
        for field in ("nonce", "maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"):
            if field in tx:
                outer[field] = tx[field]
        return self.contract.functions.execute(
            self.w3.to_checksum_address(tx["to"]),
            bytes.fromhex(tx["data"][2:] if tx["data"].startswith("0x") else tx["data"]),
            required_approve,
        ).build_transaction(outer)
