"""Static network parameters for SQD worker registration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    """One deployment of the SQD network."""

    name: str
    chain_id: int
    rpc_url: str
    worker_registration: str
    reward_treasury: str
    rewards_distribution: str


NETWORKS: dict[str, Network] = {
    "mainnet": Network(
        name="mainnet",
        chain_id=42161,
        rpc_url="https://arb1.arbitrum.io/rpc",
        worker_registration="0x36e2b147db67e76ab67a4d07c293670ebefcae4e",
        reward_treasury="0x237abf43bc51fd5c50d0d598a1a4c26e56a8a2a0",
        rewards_distribution="0x4de282bD18aE4987B3070F4D5eF8c80756362AEa",
    ),
    "tethys": Network(
        name="tethys",
        chain_id=421614,
        rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
        worker_registration="0xCD8e983F8c4202B0085825Cf21833927D1e2b6Dc",
        reward_treasury="0x785136e611E15D532C36502AaBdfE8E35008c7ca",
        rewards_distribution="0x68f9fE3504652360afF430dF198E1Cb7B2dCfD57",
    ),
}
