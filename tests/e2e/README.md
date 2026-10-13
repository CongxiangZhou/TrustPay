# End-to-end browser test

`run_e2e.py` drives the real web app in Chromium (Playwright) against the real
contract deployed on a local Ganache chain that uses Sepolia's chain id. A scripted
`window.ethereum` stands in for MetaMask, and the pages' clock is moved together with
chain time so deadlines and review periods can be tested without waiting.

It checks: wallet connection, form validation, create → deliver → release, a readable
error for a contract revert, dispute → arbiter refunds buyer, seller cancels and refunds,
the **live countdown** (the *Claim payment* button unlocks without reloading the page),
the late-dispute block, the review-period claim, the arbiter timeout, dashboards, the
History page with every transaction verified on-chain by the backend, the wrong-network
and no-wallet banners, and the mobile layout.

```bash
# terminal 1 - local chain with Sepolia's chain id (Ganache 7 supports up to Shanghai)
npx ganache@7.9.2 --chain.chainId 11155111 --chain.hardfork shanghai --wallet.deterministic --port 8545

# terminal 2 - from the project root
pip install -r requirements-dev.txt playwright
playwright install chromium
python tests/e2e/run_e2e.py
```

The script compiles `contracts/TrustPay.sol` with solc 0.8.31 for the Shanghai EVM,
deploys it, starts Flask on port 5055 with a temporary database, runs the flows and
saves screenshots to `tests/e2e/shots/`. It prints `E2E PASSED` and exits with code 0
on success.
