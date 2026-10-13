# TrustPay — Escrow Payment DApp

**SC6113 Individual Assignment · Financial Decentralized Application**
Author: ZHOU Congxiang · Network: **Ethereum Sepolia testnet** (chain id `11155111`)

TrustPay lets two strangers trade safely online. The buyer locks ETH in a smart
contract instead of paying the seller directly. The seller is paid only when the
buyer confirms the deal; the buyer can reclaim the money if the seller never
delivers; and if they disagree, a neutral arbiter they both chose decides.
Built-in timeouts guarantee the money can never be stuck: if the buyer goes silent
after delivery the seller can claim payment, and if the arbiter never decides the
buyer is refunded.

> ⚠️ Sepolia test ETH only. This project is for education and has not been audited.

---

## Live deployment (Sepolia)

| Item | Value |
|---|---|
| Contract (v2, final) | [`0x12864358FA87f5a1Eff6E9ac467645Bf1BcE8067`](https://sepolia.etherscan.io/address/0x12864358FA87f5a1Eff6E9ac467645Bf1BcE8067) |
| Deployment tx | [`0x3dfee67c…ea9ec1`](https://sepolia.etherscan.io/tx/0x3dfee67cbb4c11f54fcf10b24e22e9d6ffe2b0a41c69bf9778db4df467ea9ec1) — block 11,906,851 · 12,278,279 gas |
| Compiler | solc 0.8.31, optimizer enabled (200 runs) |
| Source verification | Verified on [Blockscout](https://eth-sepolia.blockscout.com/address/0x12864358FA87f5a1Eff6E9ac467645Bf1BcE8067?tab=contract) — metadata hash matches `contracts/TrustPay.sol` |

Earlier deployments (superseded, kept for the gas comparison below):
v2 before the late-dispute rule [`0xA96ABDa0…3Bb0`](https://sepolia.etherscan.io/address/0xA96ABDa0e0d75fAc52EED971BfAE1cB0EcDc3Bb0) ·
v1 optimised [`0xB556De78…C909`](https://sepolia.etherscan.io/address/0xB556De786904487557c3a16ccfa7e4fE024DC909) ·
v1 unoptimised [`0x253b894A…25dd`](https://sepolia.etherscan.io/address/0x253b894A75e39dC6f61b22767abE91d20C1C25dd).

---

## 1. Problem

Peer-to-peer deals (second-hand marketplaces, freelance work, ticket resale) suffer
from a trust gap: whoever pays or delivers first carries all the risk, and scams
where a "seller" takes a deposit and disappears are common. Centralised escrow
services fix this but charge fees, hold custody of the money and can freeze accounts.

**TrustPay** replaces the intermediary with a transparent smart contract whose rules
both parties can read and nobody — including the developer — can override.

## 2. Features

| Area | Feature |
|---|---|
| Wallet | Connect MetaMask, show address & balance, detect wrong network and switch to Sepolia in one click |
| Escrow | Create escrow (seller, arbiter, delivery deadline, review period, title) and lock ETH in one transaction |
| Escrow | Seller marks delivered (before the deadline) · buyer releases payment · seller cancels & refunds |
| Escrow | Buyer reclaims funds after the deadline if not delivered |
| Liveness | **Review-period auto-release**: if the buyer stays silent after delivery, the seller claims the payment |
| Disputes | Buyer or seller raises a dispute (funds frozen) · arbiter pays seller or refunds buyer |
| Liveness | **Arbiter timeout**: if the arbiter never decides, either party closes the dispute and the buyer is refunded |
| Backend | **On-chain verification**: the server confirms every transaction from its Sepolia receipt — the browser cannot fake a status |
| UI | Dashboard with platform stats and your escrows by role (buying / selling / arbitrating), "action needed" flags |
| UI | Escrow page with progress tracker, role-aware actions only, two-click confirmation, on-chain event timeline |
| Status | Live transaction panel: *confirm in MetaMask → pending → confirmed / failed* with Etherscan link |
| History | Transaction history page (Flask + SQLite) with status filter, block, gas used, "verified on-chain" badge and Etherscan links |
| Validation | Three layers: browser form validation, Flask API validation, smart-contract checks |
| Errors | Contract custom errors & wallet errors translated into plain-English messages |

## 3. Architecture

```
┌────────────────────────────┐        JSON API         ┌───────────────────────────┐
│  Browser (HTML/CSS/JS)     │ ──────────────────────▶ │  Flask backend (app.py)   │
│  ethers.js v6              │  /api/transactions      │  • serves pages           │
│  • dashboard / create /    │ ◀────────────────────── │  • validates input        │
│    escrow / history        │                         │  • tx log in SQLite       │
└─────────────┬──────────────┘                         │  • verifies receipts      │
              │ EIP-1193 (window.ethereum)             │    (chain.py)             │
              ▼                                        └──────┬──────────┬─────────┘
┌────────────────────────────┐                                │          │ JSON-RPC
│  MetaMask wallet           │                                ▼          │ (public node)
│  • holds the private key   │                ┌───────────────────────┐  │
│  • signs every transaction │                │ instance/trustpay.db  │  │
└─────────────┬──────────────┘                │ (public data only)    │  │
              │ signed transactions           └───────────────────────┘  │
              ▼                                                          ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  TrustPay.sol on Ethereum Sepolia                                            │
│  • holds the escrowed ETH  • enforces roles, states, deadlines, timeouts     │
│  • emits events = permanent on-chain history                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

* **The contract is the source of truth** for funds and escrow state. The frontend
  reads it directly through MetaMask's provider.
* **The backend never touches private keys or funds.** It keeps an off-chain log of
  transactions for a fast, filterable history, and **verifies each one on-chain**:
  a transaction is only marked *confirmed* after the server has fetched its receipt
  from a Sepolia node and checked that it was signed by the claimed wallet, succeeded,
  and made **the TrustPay contract emit the event of the claimed action** (e.g.
  `FundsReleased`). Checking the contract's own event — rather than the transaction's
  top-level `to` address — also works for smart-account wallets: MetaMask may route a
  call through its `DelegationManager`, so `to` is not TrustPay, but only TrustPay can
  emit TrustPay's events. The escrow id and amount are read from the event (a
  client-supplied amount never survives verification), the event's buyer / seller /
  arbiter address must fit the role that performs the claimed action (so the seller's
  `refundBuyer` cannot be logged as the buyer's `claimRefund`, which emits the same
  event), and a hash the node has never seen is marked failed after 30 minutes.

## 4. Technologies

| Layer | Technology |
|---|---|
| Smart contract | Solidity `^0.8.24`, compiled & deployed with solc 0.8.31 (optimizer 200 runs) |
| Blockchain | Ethereum Sepolia testnet |
| Wallet | MetaMask (EIP-1193) |
| Frontend | HTML, CSS, vanilla JavaScript, ethers.js 6.13.4 (bundled locally) |
| Backend | Python 3, Flask 3, SQLite |
| Testing | pytest, web3.py, eth-tester (py-evm in-memory EVM) |
| Tools | Remix IDE (deployment), VS Code, GitHub, Render (hosting) |

## 5. Smart contract design (`contracts/TrustPay.sol`)

### State machine

```
createEscrow ─▶ Funded ──markDelivered (before deadline)──▶ Delivered ──claimPayment──┐
                 │   │                                        │   │   (review expired) │
   claimRefund ◀─┘   └──────────── raiseDispute ──────────────┘   │ releaseFunds       │
 (after deadline)                       ▼                         │ refundBuyer        │
                                     Disputed ──resolveDispute──▶ Released / Refunded ◀┘
                                        └──expireDispute (arbiter silent)──▶ Refunded
```

Every non-final state has a way out that does not depend on the other party:
*Funded* → buyer refund after the deadline; *Delivered* → seller claim after the
review period; *Disputed* → buyer refund after the arbiter window. **No escrow can be
stuck forever.**

### Functions

| Function | Who | Allowed when | Effect |
|---|---|---|---|
| `createEscrow(seller, arbiter, deadline, reviewPeriod, title)` *payable* | anyone (becomes buyer) | — | Locks `msg.value`, status → Funded |
| `markDelivered(id)` | seller | Funded, **before deadline** | → Delivered, starts review period |
| `releaseFunds(id)` | buyer | Funded, Delivered | Pays seller → Released |
| `refundBuyer(id)` | seller | Funded, Delivered | Refunds buyer → Refunded |
| `claimRefund(id)` | buyer | Funded **and** deadline passed | Refunds buyer → Refunded |
| `claimPayment(id)` | seller | Delivered **and** review period over | Pays seller → Released |
| `raiseDispute(id)` | buyer or seller | Funded, Delivered (buyer: only **within the review period** after delivery) | → Disputed (frozen), starts arbiter window |
| `resolveDispute(id, payToSeller)` | arbiter | Disputed | Pays winner → Released / Refunded |
| `expireDispute(id)` | buyer or seller | Disputed **and** arbiter window over | Refunds buyer → Refunded |
| `getEscrow(id)`, `getUserEscrows(addr)`, `getStats()` | anyone (view) | — | Read data |

Events: `EscrowCreated`, `DeliveryMarked`, `FundsReleased`, `BuyerRefunded`, `DisputeRaised`, `DisputeResolved`, `PaymentClaimed`, `DisputeExpired`.

`reviewPeriod` (5 minutes – 30 days, chosen per escrow) is both the buyer's review
window after delivery and the arbiter's decision window after a dispute. The arbiter
may still decide after the window, until someone calls `expireDispute`.

### Security

* **Access control** – every action checks the caller's role for *that* escrow.
* **State machine** – each action checks the current status, so funds can only
  move once (no double release / double refund).
* **Reentrancy** – Checks-Effects-Interactions ordering *plus* a `nonReentrant`
  guard on every function that sends ETH (proven by an attacker-contract test).
* **Input validation** – non-zero amount, non-zero and distinct buyer/seller/arbiter,
  deadline between 5 minutes and 365 days, review period 5 minutes – 30 days, title 1–64 bytes.
* **Liveness** – timeouts mean a silent buyer or arbiter can never lock the funds;
  a seller cannot mark delivery after the deadline to block the buyer's refund, and a
  buyer cannot let the review period lapse and then stall the seller with a late dispute.
* **Overflow** – Solidity ≥0.8 checked arithmetic; amount bounded to `uint96`.
* **No stray ETH** – no `receive`/`fallback`, so plain transfers to the contract revert.
* **Failed payouts revert atomically** – if the recipient rejects ETH the whole
  transaction reverts and the funds stay safely locked (arbiter can then refund).

### Gas optimisation

* Custom errors instead of revert strings (smaller bytecode, cheaper reverts).
* Struct packing: all fixed-size fields fit in 3 storage slots using `uint40`
  timestamps (valid until year 36812):
  `buyer+amount(uint96)` · `seller+deadline+status+resolvedByArbiter+actionAt` ·
  `arbiter+createdAt+reviewPeriod(uint32)`. A status change and its timestamp are
  written in **one** `SSTORE` — adding the review clock to `markDelivered` cost only
  ~210 extra gas.
* `calldata` strings, `external` functions, `constant`s, `unchecked` id counter.
* Full history kept in **events** (cheap) rather than storage arrays.

Deployment on Sepolia — effect of the compiler optimizer (same source, solc 0.8.31):

| Build | Runtime size | Deployment gas | Fee @ 1.5 gwei |
|---|---|---|---|
| Optimizer **off** (v1, first deployment) | ~11.7 KB | 18,360,285 | 0.0275 ETH |
| Optimizer **on**, 200 runs (v1) | ~6.0 KB | 9,537,975 | 0.0143 ETH |
| **Saving** | **−49 %** | **−48 %** | **−48 %** |
| v2 (timeouts added), optimizer on | ~7.6 KB | 12,037,052 | 0.0181 ETH |
| v2 final (+ late-dispute rule), optimizer on — live contract | ~7.7 KB | 12,278,279 | 0.0184 ETH |

Gas per action (local EVM, `pytest -s tests/test_contract.py -k gas`):

| Action | Gas |
|---|---|
| createEscrow (first) | ~235,100 |
| createEscrow (repeat) | ~209,300 |
| markDelivered | ~28,900 |
| releaseFunds | ~70,700 |
| raiseDispute | ~30,800 |
| resolveDispute | ~68,300 |
| claimPayment | ~48,500 |
| raiseDispute (with review-period check) | ~31,000 |
| expireDispute | ~48,500 |

## 6. Project structure

```
TrustPay/
├── contracts/TrustPay.sol          # the smart contract
├── app.py                          # Flask app: pages + REST API + validation
├── chain.py                        # on-chain receipt verification (JSON-RPC, no extra deps)
├── config.py                       # Sepolia config, CONTRACT_ADDRESS, DEPLOY_BLOCK, RPC URL
├── database.py                     # SQLite schema & connection
├── templates/                      # base, index, dashboard, create, escrow, history, error
├── static/
│   ├── css/style.css
│   ├── js/app.js                   # wallet, network, tx status, error messages
│   ├── js/{dashboard,create,escrow,history}.js
│   ├── abi/TrustPay.abi.json       # contract ABI used by the frontend
│   └── vendor/ethers-6.13.4.umd.min.js
├── scripts/compile.py              # compiles the contract with solc 0.8.31
├── tests/
│   ├── test_contract.py            # 56 smart-contract tests
│   ├── test_backend.py             # 52 backend/API/verification tests
│   ├── e2e/run_e2e.py              # browser end-to-end test (Ganache + Playwright)
│   └── contracts/TestHelpers.sol   # attacker contracts used by tests only
├── docs/DEPLOYMENT.md              # Remix → Sepolia → config → Render
├── docs/TEST_PLAN.md               # test cases + screenshot checklist
├── requirements.txt / requirements-dev.txt / Procfile
├── start_server.bat                # Windows one-click: venv + install + run
└── instance/trustpay.db            # created on first run
```

## 7. Run locally

Requirements: Python 3.10+, Chrome/Edge/Firefox with MetaMask, some Sepolia ETH.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

1. Deploy the contract to Sepolia — see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.
2. Put the deployed address in `config.py` (`CONTRACT_ADDRESS`) — or set the
   environment variable `CONTRACT_ADDRESS` (and optionally `DEPLOY_BLOCK`).
3. Start the app:

```bash
python app.py
```

Open <http://127.0.0.1:5000>, click **Connect MetaMask**, and make sure MetaMask is on **Sepolia**.

To try every role you need three MetaMask accounts (buyer, seller, arbiter), each with
a little Sepolia ETH for gas.

## 8. Tests

```bash
pip install -r requirements-dev.txt
pytest -v                      # 108 tests: contract + backend
pytest -s tests/test_contract.py -k gas   # print the gas report
```

A browser end-to-end test (real Flask app + contract on a local Ganache chain with
Sepolia's chain id, driven by Playwright) is in [tests/e2e](tests/e2e/README.md).

The contract tests run on an in-memory EVM, so they need no testnet ETH. The first
run downloads solc 0.8.31 (the deployment compiler) automatically. Manual test cases and the screenshot
checklist are in [docs/TEST_PLAN.md](docs/TEST_PLAN.md).

## 9. Backend API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/config` | Contract address, chain id, explorer URL |
| POST | `/api/transactions` | Record a submitted tx `{tx_hash, action, from_address, escrow_id?, amount_wei?, chain_id}`. If the node already knows the hash, it must be signed by `from_address`, else **422** |
| PATCH | `/api/transactions/<tx_hash>` | Ask the server to verify the tx on-chain. **200** = confirmed/failed from the receipt, **202** = not mined yet / node unreachable, **422** = TrustPay did not emit the event of the claimed action |
| GET | `/api/transactions?address=&escrow_id=&status=&limit=` | List transactions (pending ones are re-verified automatically) |

Verification uses the public node in `SEPOLIA_RPC_URL` (default
`https://ethereum-sepolia-rpc.publicnode.com`). Setting `VERIFY_ONCHAIN=0` falls back
to trusting the browser, for offline demos only.

All inputs are validated (hex formats, allowed actions, integer ranges, Sepolia
chain id, 16 KB body limit); SQL uses parameterised queries; responses carry
security headers (CSP, `X-Frame-Options`, `nosniff`).

## 10. Privacy & ethics

* Only public blockchain data is stored off-chain (addresses, tx hashes, amounts).
  No names, emails or keys.
* Everything on-chain — including escrow titles — is **public forever**; users
  should not put personal information in titles.
* Private keys stay in MetaMask; the app never sees or stores them.
* The arbiter is trusted by both parties; choosing a fair arbiter is a social, not
  technical, guarantee.

## 11. Limitations & future work

* Arbiter is a single trusted address and is unpaid → multi-sig / DAO voting, and an
  optional arbiter fee to give them an incentive to act.
* A seller can falsely mark an order delivered; the buyer's remedy is a dispute (by
  design — the contract cannot see the physical world). An oracle or courier
  integration could provide proof of delivery.
* The default outcome of an expired dispute is a refund to the buyer — fair to the
  payer, but a dishonest buyer could dispute (before delivery or within the review
  period) and hope the arbiter stays silent. Choosing an active arbiter matters.
* Anyone can POST a transaction hash to the log API. Fake entries can never be marked
  confirmed and expire after 30 minutes, but proper protection would require signing
  API requests with the wallet (Sign-In with Ethereum).
* Only native ETH → add ERC-20 stablecoins (e.g. USDC) to avoid price volatility.
* Push payments → a pull-withdrawal pattern would tolerate recipients that reject ETH.
  Today, if a recipient contract rejects ETH the payout reverts and the funds stay
  locked until another exit applies (e.g. the arbiter refunds the buyer).
* Timeouts never fire by themselves: the party who benefits must send the
  `claimRefund` / `claimPayment` / `expireDispute` transaction.
* Verification relies on one public RPC node → use several providers or a dedicated node.
* No partial payments or milestones → split one escrow into milestone releases.

## License

MIT
