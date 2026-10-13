# Deployment Guide — TrustPay on Sepolia

Everything runs on the **Ethereum Sepolia testnet** (chain id `11155111`). You never
need real ETH, and your private key never leaves MetaMask.

## 1. Prepare MetaMask

1. Install MetaMask and create (or use) a wallet.
2. Turn on test networks: MetaMask → network selector → *Show test networks* → select **Sepolia**.
3. Create **three accounts** (MetaMask → *Add account*): `Buyer`, `Seller`, `Arbiter`.
   Account 1 can also be the deployer.
4. Get Sepolia test ETH from a faucet, for example:
   * Google Cloud Web3 faucet — <https://cloud.google.com/application/web3/faucet/ethereum/sepolia>
   * Alchemy Sepolia faucet — <https://www.alchemy.com/faucets/ethereum-sepolia>
5. Send a small amount (e.g. 0.02 ETH) from the Buyer account to the Seller and Arbiter
   accounts so they can pay gas.

## 2. Deploy the contract with Remix

1. Open <https://remix.ethereum.org>.
2. In *File explorer*, create `TrustPay.sol` and paste the contents of `contracts/TrustPay.sol`.
3. **Solidity compiler** tab:
   * Compiler: `0.8.31+commit.fd3a2265`
   * *Advanced configurations* → tick **Enable optimization**, runs = `200`
   * EVM version: `default`
   * Click **Compile TrustPay.sol** — there should be no errors or warnings.
   * ⚠️ Make sure *Enable optimization* is ticked **before** deploying. Without it the
     deployment costs ~2× more gas (measured: 18.36M vs 9.54M gas).
4. **Deploy & run transactions** tab:
   * Environment: **Injected Provider – MetaMask** (MetaMask must be on Sepolia; Remix shows `Custom (11155111) network`).
   * Account: your deployer account.
   * Contract: `TrustPay - TrustPay.sol`
   * Click **Deploy** and confirm in MetaMask.
5. When the transaction confirms, copy the contract address from *Deployed Contracts*.
6. Open the deployment transaction on <https://sepolia.etherscan.io> and note the **block number**.
7. 📸 Screenshot: Remix compile success, deploy transaction confirmed, and the contract page on Etherscan.

### ABI

The ABI the frontend uses is already in `static/abi/TrustPay.abi.json` (generated from the
same source with solc 0.8.31). Remix's ABI (Compiler tab → *ABI* button) will be identical.
If you ever change the contract, run `python scripts/compile.py` to regenerate it.

## 3. (Optional, recommended) Verify the source on Etherscan

Makes the contract readable on Etherscan — good evidence for the report.

1. On the contract's Etherscan page → *Contract* → **Verify and Publish**.
2. Compiler type: *Solidity (Single file)*, compiler `v0.8.31+commit.fd3a2265`, license *MIT*.
3. Paste `TrustPay.sol`, set *Optimization: Yes*, runs `200`, EVM version `default`.
4. Submit. The *Contract* tab then shows a green tick and *Read/Write Contract* tabs.

## 4. Configure the app

Edit `config.py` (or set the environment variables of the same names):

```python
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "0xYourDeployedAddress")
DEPLOY_BLOCK = int(os.environ.get("DEPLOY_BLOCK", "1234567") or 0)
```

`DEPLOY_BLOCK` makes the escrow activity timeline load faster (events are searched from
that block instead of from genesis).

The backend verifies every transaction on-chain through `SEPOLIA_RPC_URL`
(default `https://ethereum-sepolia-rpc.publicnode.com`, no API key needed). If that node
is down, transactions simply stay *pending* until it is reachable again.

Run locally:

```bash
pip install -r requirements.txt
python app.py      # http://127.0.0.1:5000
```

## 5. Deploy the web app to Render

1. Push the repository to GitHub.
2. Render → **New → Web Service** → connect the repo.
3. Settings:
   * Runtime: Python 3
   * Build command: `pip install -r requirements.txt`
   * Start command: `gunicorn app:app`
4. *Environment* → add `CONTRACT_ADDRESS` and `DEPLOY_BLOCK` (and optionally `SEPOLIA_RPC_URL`).
5. Deploy and open the `.onrender.com` URL.

> Render's free tier has an ephemeral disk: the SQLite history resets when the service
> restarts. On-chain data (escrows and events) is never lost.

## 6. Security checklist before submitting

- [ ] No private key, seed phrase or `.env` file in the repository.
- [ ] `CONTRACT_ADDRESS` points to the Sepolia deployment.
- [ ] Contract verified on Etherscan (optional).
- [ ] README updated with the contract address and Etherscan link.
