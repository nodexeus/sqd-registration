"""Static network parameters for SQD worker registration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    """One deployment of the SQD network."""

    name: str
    chain_id: int
    rpc_url: str
    worker_registration: str


NETWORKS: dict[str, Network] = {
    "mainnet": Network(
        name="mainnet",
        chain_id=42161,
        rpc_url="https://arb1.arbitrum.io/rpc",
        worker_registration="0x36e2b147db67e76ab67a4d07c293670ebefcae4e",
    ),
    "tethys": Network(
        name="tethys",
        chain_id=421614,
        rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
        worker_registration="0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc",
    ),
}
