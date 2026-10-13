"""TrustPay configuration.

All values can be overridden with environment variables (e.g. on Render).
After deploying TrustPay.sol on Sepolia, set CONTRACT_ADDRESS (and optionally
DEPLOY_BLOCK) here or as environment variables.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---- Blockchain (Ethereum Sepolia testnet) -------------------------------
CHAIN_ID = 11155111
CHAIN_NAME = "Sepolia"
EXPLORER_URL = "https://sepolia.etherscan.io"

# Address of the deployed TrustPay contract (v2) on Sepolia.
# Deployed 2026-10-13 (solc 0.8.31, optimizer 200 runs), verified on Blockscout.
# Deployment tx 0x3dfee67cbb4c11f54fcf10b24e22e9d6ffe2b0a41c69bf9778db4df467ea9ec1
# (Older deployments 0xA96ABDa0e0d75fAc52EED971BfAE1cB0EcDc3Bb0 [v2 without the late-dispute rule],
#  0xB556De786904487557c3a16ccfa7e4fE024DC909 / 0x253b894A75e39dC6f61b22767abE91d20C1C25dd [v1, old ABI]
#  must NOT be used with this frontend.)
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "0x12864358FA87f5a1Eff6E9ac467645Bf1BcE8067")

# Block number the contract was deployed in (speeds up event queries; 0 = from genesis).
DEPLOY_BLOCK = int(os.environ.get("DEPLOY_BLOCK", "11906851") or 0)

# Public Sepolia JSON-RPC endpoint the BACKEND uses to verify transaction receipts.
SEPOLIA_RPC_URL = os.environ.get("SEPOLIA_RPC_URL", "https://ethereum-sepolia-rpc.publicnode.com")
# Set VERIFY_ONCHAIN=0 to fall back to trusting the browser (offline demos only).
VERIFY_ONCHAIN = os.environ.get("VERIFY_ONCHAIN", "1") != "0"
# A logged transaction the node has still never seen after this long is marked failed.
PENDING_TIMEOUT_MINUTES = int(os.environ.get("PENDING_TIMEOUT_MINUTES", "30"))

# ---- Backend -------------------------------------------------------------
DATABASE = os.environ.get("DATABASE_PATH", str(BASE_DIR / "instance" / "trustpay.db"))
MAX_CONTENT_LENGTH = 16 * 1024  # reject request bodies larger than 16 KB
