# Test Plan — TrustPay

Testing has two parts:

1. **Automated tests** (108) — run on a local in-memory EVM and a Flask test client,
   plus a browser end-to-end test (`tests/e2e/run_e2e.py`, Ganache + Playwright).
2. **Manual tests on Sepolia** — run through the web app with MetaMask; take a
   screenshot of each for the report.

## 1. Automated tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

### Smart contract — `tests/test_contract.py` (56 tests)

| Group | What is verified |
|---|---|
| Creation | All fields stored; ETH held by contract; stats updated; `EscrowCreated` event; user index for buyer/seller/arbiter |
| Invalid input (13 cases) | zero amount, zero seller/arbiter, buyer = seller, buyer = arbiter, seller = arbiter, past deadline, deadline < 5 min, deadline > 365 days, review period < 5 min or > 30 days, empty title, title > 64 bytes, plain ETH transfer, unknown id |
| Happy paths | deliver → release; release without delivery mark; seller refund; buyer refund after deadline |
| Deadline rules | `claimRefund` blocked before deadline and after delivery |
| Disputes (4 combos) | buyer/seller raises × arbiter pays seller/refunds buyer; funds frozen during dispute; `DisputeResolved` event |
| Access control | every write function rejects every non-authorised role; only arbiter resolves; arbiter/outsider cannot dispute |
| State machine | no double payout; no double delivery; escrows isolated from each other |
| Security | **reentrancy attack** by a malicious seller contract is blocked; recipient that rejects ETH → whole tx reverts, funds stay locked, arbiter can refund |
| Liveness (v2) | delivery cannot be marked after the deadline; seller claims payment only after the review period and only if the buyer did not release/dispute; buyer can still release late; dispute expires only after the arbiter window, refunds the buyer, callable by buyer or seller only; arbiter can decide late until expiry, not after; buyer can dispute within the review period but not after it (seller still can; no limit before delivery) |
| Storage layout | fixed fields packed in 3 slots — title begins at slot 3 (checked with `eth_getStorageAt`) |
| Gas | gas used per action (`pytest -s -k gas`) |

### Backend — `tests/test_backend.py` (52 tests)

| Group | What is verified |
|---|---|
| Pages | all pages render with security headers; 404 page |
| API flow | record → confirm / fail; duplicate hash rejected (409); final status cannot change |
| Filters | by address (case-insensitive), escrow id, status |
| Validation (12 cases) | bad hash, bad action, bad address, negative/decimal/zero amount, negative/boolean escrow id, missing escrow id, non-Sepolia chain id |
| Robustness | non-JSON body, body > 16 KB (413), SQL-injection string rejected |
| On-chain verification (simulated Sepolia node) | confirmation uses receipt data and ignores what the client claims; escrow id read from calldata / `EscrowCreated` log; reverted tx → failed; unmined tx stays pending and is settled later by the history list; fake hash can never be confirmed; tx signed by another wallet → 422; receipt without the matching TrustPay event (other contract or other action) → 422; **delegated tx (MetaMask smart account, `to` = DelegationManager) with the TrustPay event → confirmed**; node down → stays pending, recovers later; old database migrated; amount taken from the transaction value, not the client; a hash the node never sees expires to failed after 30 min, while a known-but-slow transaction stays pending; **amount always taken from the event** (a client-supplied 999 ETH on a release is replaced, and dropped for actions without an amount); amounts read from every event type; **signer must fit the role**: the seller's `refundBuyer` cannot be logged as `claimRefund` and vice versa |

### Browser end-to-end — `tests/e2e/run_e2e.py`

Real Flask app + contract on a local Ganache chain (chain id 11155111), driven by
Playwright with a scripted wallet. Covers create → deliver → release, readable revert
errors, dispute → arbiter refund, seller cancel, the **live countdown** (*Claim payment*
unlocks without a page reload), the late-dispute block, review-period claim, arbiter
timeout, dashboards, History with every row verified on-chain, the wrong-network /
no-wallet banners and the mobile layout. See [tests/e2e/README.md](../tests/e2e/README.md).

## 2. Manual test cases on Sepolia

Use three MetaMask accounts: **B** (buyer), **S** (seller), **A** (arbiter).
Fill the *Result* column with Pass/Fail when you run them.

| ID | Scenario | Steps | Expected result | Result |
|---|---|---|---|---|
| T01 | Contract deployment | Deploy `TrustPay.sol` via Remix + MetaMask on Sepolia | Tx confirmed; contract visible on Etherscan | |
| T02 | Wallet connection | Open app → *Connect MetaMask* | Address, balance and green "Sepolia" chip shown | |
| T03 | Wrong network | Switch MetaMask to Ethereum Mainnet / another network | Red "Wrong network" banner + *Switch to Sepolia* button works | |
| T04 | No wallet | Open app in a browser without MetaMask | "MetaMask not detected" banner | |
| T05 | Form validation | On *New escrow*: empty title, own address as seller, `0x123` as arbiter, amount 0 | Inline error under each field; no MetaMask popup | |
| T06 | Create escrow | B creates escrow (S, A, 0.01 ETH, +1 day) | Status panel: confirm → pending → confirmed; redirected to escrow page; status **Funded** | |
| T07 | Reject in MetaMask | Start any action, click *Reject* in MetaMask | Panel shows "You rejected the request in MetaMask." | |
| T08 | Mark delivered | S opens escrow → *Mark as delivered* | Status **Delivered**; timeline shows event | |
| T09 | Release payment | B → *Release payment to seller* | Status **Released**; S's balance increases; Etherscan shows internal transfer | |
| T10 | Double release blocked | (Covered by contract tests) or try to act on a closed escrow | Actions hidden; contract would revert with `InvalidStatus` | |
| T11 | Seller refund | New escrow → S → *Cancel & refund buyer* | Status **Refunded**; B gets ETH back | |
| T12 | Refund after deadline | New escrow with **+10 min** deadline → wait → B *Claim refund* | Button disabled before deadline; after deadline status **Refunded** | |
| T13 | Dispute | New escrow → B or S *Raise dispute* | Status **Disputed**; B/S see no release/refund buttons | |
| T14 | Arbiter decision | A opens disputed escrow → *Pay the seller* or *Refund the buyer* | Status Released/Refunded; "Resolved by arbiter: Yes" | |
| T15 | Unauthorised view | Open an escrow with an account that is not B/S/A | "You can only view it" message; no action buttons | |
| T16 | Dashboard | Open dashboard as B and as S | Stats + escrows listed by role; "Action needed" flags | |
| T17 | History | Open *History* | All your transactions with status, block, gas and Etherscan links; filter works | |
| T18 | Insufficient funds | Create escrow with more ETH than the balance | Inline "Not enough ETH" error | |
| T19 | Review-period auto-release | New escrow with **Review period: 10 minutes** → S marks delivered → B does nothing → after 10 min S clicks *Claim payment* | Button disabled with countdown before; after the period status **Released**, timeline "seller claimed" | |
| T20 | Arbiter timeout | New escrow (10-minute review period) → B raises dispute → A does nothing → after 10 min B clicks *Close dispute & refund buyer* | Status **Refunded**, timeline "Arbiter did not decide in time"; A can no longer resolve | |
| T21 | Late delivery blocked | Escrow with a **+10 min** deadline → wait until it passes → S opens it | *Mark as delivered* disabled with explanation; B can *Claim refund* | |
| T23 | Late dispute blocked | Escrow with 10-minute review period → S marks delivered → wait 10 min → B opens it | *Raise dispute* disabled with explanation; B can still release; S can *Claim payment* | |
| T22 | Server-side verification | Open *History* after any transactions | Each confirmed row shows **✓ verified on-chain**; block and gas match Etherscan | |

## 3. Screenshot checklist for the report

| # | Screenshot | Proves (guide section) |
|---|---|---|
| 1 | Remix: successful compile (green tick) | Smart contract deployment |
| 2 | Remix + MetaMask: deploy confirmation, then Etherscan contract page | Smart contract deployment |
| 3 | App header after connecting (address, balance, Sepolia chip) | Wallet connection |
| 4 | Wrong-network banner | Wallet connection / error handling |
| 5 | Create form with validation errors | Invalid input handling |
| 6 | MetaMask confirmation popup for `createEscrow` showing the ETH amount | Successful transaction |
| 7 | Status panel *Pending* and *Confirmed* with Etherscan link | Transaction status |
| 8 | Escrow page as seller (Funded) and after *Mark as delivered* | DApp functionality |
| 9 | Escrow page after release — progress tracker complete + on-chain activity timeline | Recorded on blockchain |
| 10 | Etherscan transaction page for the release (internal ETH transfer to seller) | Recorded on blockchain |
| 11 | Disputed escrow + arbiter view + resolved result | DApp functionality |
| 12 | MetaMask *Reject* → friendly error message | Error handling |
| 13 | Claim-refund before deadline (disabled) and after deadline (Refunded) | Validation via smart contract |
| 14 | Dashboard (buyer and seller views) | Frontend UI |
| 15 | History page | Transaction history |
| 16 | Terminal output of `pytest -v` (108 passed) and the gas report | Testing & evaluation |
| 17 | Mobile view of any page (browser dev tools, ~390 px) | Usability |
| 18 | *Claim payment* locked (countdown) and then claimed (T19) | Innovation — liveness |
| 19 | Expired dispute refunded to buyer (T20) | Innovation — liveness |
| 20 | History rows with **✓ verified on-chain** (T22) | Backend integration / authentication |
