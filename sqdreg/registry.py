"""Wrapper around the SQD WorkerRegistration contract and its bond token."""

from sqdreg.networks import Network

FALLBACK_REGISTER_GAS = 350_000

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

    def _base_tx(self, nonce: int, fees: dict) -> dict:
        return {
            "from": self.address,
            "nonce": nonce,
            "chainId": self.network.chain_id,
            **fees,
        }

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
