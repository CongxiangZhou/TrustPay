"""End-to-end browser test for TrustPay.

Runs the real Flask app and the real contract on a local Ganache chain that uses
Sepolia's chain id, and drives the UI with Playwright. A scripted `window.ethereum`
stands in for MetaMask (it signs with Ganache's unlocked accounts), and `Date.now()`
in the pages is shifted together with chain time so deadlines can be tested.

Prerequisites (see tests/e2e/README.md):
    npx ganache@7.9.2 --chain.chainId 11155111 --chain.hardfork shanghai \
        --wallet.deterministic --port 8545
    pip install -r requirements-dev.txt playwright && playwright install chromium

Run from the project root:
    python tests/e2e/run_e2e.py              # compiles the contract with solc 0.8.31
    python tests/e2e/run_e2e.py ARTIFACT     # or use a prebuilt {"abi","bytecode"} JSON
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
PORT = 5055
BASE = f"http://127.0.0.1:{PORT}"
RPC = os.environ.get("E2E_RPC", "http://127.0.0.1:8545")
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)
ACC = ["0x90F8bf6A479f320ead074411a4B0e7944Ea8c9C1", "0xFFcf8FDEE72ac11b5c542428B35EEF5769C409f0",
       "0x22d491Bde2303f2f43325b2108D26f1eAbA1e32b", "0xE11BA2b4D45Eaed5996Cd0823791E0C93114882d"]
BUYER, SELLER, ARBITER, OUTSIDER = ACC


def compiled_contract():
    """Ganache 7 supports up to the Shanghai hardfork, so compile for that EVM version."""
    if len(sys.argv) > 1:
        return json.loads(Path(sys.argv[1]).read_text())
    import solcx
    solcx.install_solc("0.8.31")
    out = solcx.compile_standard({
        "language": "Solidity",
        "sources": {"TrustPay.sol": {"content": (ROOT / "contracts" / "TrustPay.sol").read_text()}},
        "settings": {"evmVersion": "shanghai", "optimizer": {"enabled": True, "runs": 200},
                     "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}},
    }, solc_version="0.8.31")
    c = out["contracts"]["TrustPay.sol"]["TrustPay"]
    return {"abi": c["abi"], "bytecode": "0x" + c["evm"]["bytecode"]["object"]}


def deploy():
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 11155111, "start Ganache with --chain.chainId 11155111"
    art = compiled_contract()
    tx = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]).constructor().transact({"from": w3.eth.accounts[9]})
    r = w3.eth.wait_for_transaction_receipt(tx)
    return r.contractAddress, r.blockNumber


def start_server(address, block):
    try:
        urllib.request.urlopen(BASE + "/api/config", timeout=1)
        raise SystemExit(f"Port {PORT} is already in use - stop the other server first")
    except OSError:
        pass
    db = Path(tempfile.mkdtemp()) / "e2e.db"
    env = {**os.environ, "CONTRACT_ADDRESS": address, "DEPLOY_BLOCK": str(block),
           "SEPOLIA_RPC_URL": RPC, "VERIFY_ONCHAIN": "1", "DATABASE_PATH": str(db)}
    proc = subprocess.Popen([sys.executable, "-m", "flask", "--app", "app", "run", "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1)
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("Flask server did not start")


CONTRACT_ADDRESS, DEPLOY_BLOCK = deploy()
SERVER = start_server(CONTRACT_ADDRESS, DEPLOY_BLOCK)
import atexit
atexit.register(SERVER.terminate)   # stop Flask even if a check fails
print("contract", CONTRACT_ADDRESS, "block", DEPLOY_BLOCK)

MOCK = """
(() => {
  const ACCOUNT = "%s";
  const AUTO = %s;
  let id = 0;
  const _now = Date.now.bind(Date);
  Date.now = () => _now() + (parseInt(localStorage.getItem("timeShift") || "%d", 10));
  async function rpc(method, params) {
    const r = await fetch("/__rpc", {method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({jsonrpc: "2.0", id: ++id, method, params: params || []})});
    const j = await r.json();
    if (j.error) {
      const e = new Error(j.error.message); e.code = j.error.code;
      const d = j.error.data; e.data = (d && typeof d === "object") ? (d.result || d.data) : d;
      throw e;
    }
    return j.result;
  }
  const connected = () => AUTO || localStorage.getItem("mock-connected") === "1";
  window.ethereum = {
    isMetaMask: true,
    request: async ({method, params}) => {
      if (method === "eth_requestAccounts") { localStorage.setItem("mock-connected", "1"); return [ACCOUNT]; }
      if (method === "eth_accounts") return connected() ? [ACCOUNT] : [];
      if (method === "wallet_switchEthereumChain") return null;
      if (method === "eth_sendTransaction") params[0].from = ACCOUNT;
      return rpc(method, params);
    },
    on() {}, removeListener() {},
  };
})();
"""

errors = []


def forward(route):
    req = urllib.request.Request(RPC, data=route.request.post_data.encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        route.fulfill(status=200, content_type="application/json", body=resp.read())


def new_page(browser, account, auto=True, width=1280):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.add_init_script(MOCK % (account, "true" if auto else "false", SHIFT["s"] * 1000))
    page = ctx.new_page()
    page.route("**/__rpc", forward)
    page.on("console", lambda m: errors.append(f"{account[:6]} console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"{account[:6]} pageerror: {e}"))
    page.on("response", lambda r: errors.append(f"{account[:6]} HTTP {r.status} {r.request.method} {r.url}")
            if r.status >= 400 else None)
    return page


def confirm_click(page, label):
    btn = page.get_by_role("button", name=label)
    btn.click()
    page.get_by_role("button", name="Click again to confirm").click()
    expect(page.locator(".txpanel--confirmed")).to_be_visible(timeout=20000)


def chain_rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    with urllib.request.urlopen(urllib.request.Request(RPC, data=body, headers={"content-type": "application/json"})) as r:
        return json.loads(r.read())["result"]


# Ganache's clock may already run ahead of real time (e.g. a chain reused from an earlier
# run that called evm_increaseTime), so start the pages' clock at the chain's time.
_chain_now = int(chain_rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
SHIFT = {"s": max(0, _chain_now - int(time.time()))}


def travel(seconds, pages):
    """Advance chain time and shift Date.now() in every open page by the same amount."""
    chain_rpc("evm_increaseTime", [seconds])
    chain_rpc("evm_mine", [])
    SHIFT["s"] += seconds
    for pg in pages:
        pg.evaluate(f"localStorage.setItem('timeShift', '{SHIFT['s'] * 1000}')")


def step(msg):
    print("•", msg, flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---------------- landing + connect
    buyer = new_page(browser, BUYER, auto=False)
    buyer.goto(BASE + "/")
    expect(buyer.locator("#connect-btn")).to_have_text("Connect MetaMask")
    buyer.screenshot(path=OUT / "01_landing.png", full_page=True)
    buyer.locator("#connect-btn").click()
    expect(buyer.locator("#account-chip")).to_be_visible()
    expect(buyer.locator("#net-chip")).to_have_text("Sepolia")
    step("landing renders, wallet connects, Sepolia detected")

    # ---------------- create: validation errors
    buyer.goto(BASE + "/create")
    buyer.fill("#title", "")
    buyer.fill("#seller", BUYER)
    buyer.fill("#arbiter", "0x123")
    buyer.fill("#amount", "0")
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    expect(buyer.locator('[data-for="title"]')).to_contain_text("describe")
    expect(buyer.locator('[data-for="seller"]')).to_contain_text("own seller")
    expect(buyer.locator('[data-for="arbiter"]')).to_contain_text("valid Ethereum address")
    expect(buyer.locator('[data-for="amount"]')).to_contain_text("greater than 0")
    buyer.screenshot(path=OUT / "02_create_validation.png", full_page=True)
    step("client-side validation shows errors")

    # ---------------- create: valid
    buyer.fill("#title", "Used MacBook Pro 14 (2021)")
    buyer.fill("#seller", SELLER)
    buyer.fill("#arbiter", ARBITER)
    buyer.fill("#amount", "0.5")
    buyer.click('button[data-add="10080"]')
    buyer.screenshot(path=OUT / "03_create_filled.png", full_page=True)
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    expect(buyer.locator(".txpanel--confirmed")).to_be_visible(timeout=20000)
    buyer.screenshot(path=OUT / "04_create_confirmed.png")
    buyer.wait_for_url(BASE + "/escrow/0", timeout=10000)
    expect(buyer.locator(".badge--funded").first).to_be_visible(timeout=10000)
    expect(buyer.locator("#timeline")).to_contain_text("Escrow created", timeout=10000)
    buyer.screenshot(path=OUT / "05_escrow_funded_buyer.png", full_page=True)
    step("escrow #0 created, detail page shows Funded + on-chain event")

    # ---------------- seller marks delivered
    seller = new_page(browser, SELLER)
    seller.goto(BASE + "/escrow/0")
    expect(seller.get_by_role("button", name="Mark as delivered")).to_be_visible(timeout=10000)
    seller.screenshot(path=OUT / "06_escrow_seller_view.png", full_page=True)
    confirm_click(seller, "Mark as delivered")
    expect(seller.locator(".badge--delivered").first).to_be_visible(timeout=10000)
    step("seller marked delivered")

    # outsider sees view-only
    outsider = new_page(browser, OUTSIDER)
    outsider.goto(BASE + "/escrow/0")
    expect(outsider.locator(".actions__msg")).to_contain_text("not a party", timeout=10000)
    assert outsider.locator("[data-action]").count() == 0
    step("outsider gets view-only page")

    # ---------------- buyer releases
    buyer.goto(BASE + "/escrow/0")
    expect(buyer.get_by_role("button", name="Release payment to seller")).to_be_visible(timeout=10000)
    confirm_click(buyer, "Release payment to seller")
    expect(buyer.locator(".badge--released").first).to_be_visible(timeout=10000)
    expect(buyer.locator("#timeline")).to_contain_text("released", timeout=10000)
    buyer.screenshot(path=OUT / "07_escrow_released.png", full_page=True)
    step("buyer released funds -> Released")

    # ---------------- contract revert -> friendly message (bypass UI: release again)
    buyer.evaluate("""() => TP.sendTx({action: 'releaseFunds', title: 'Release again', escrowId: 0,
                       send: (c) => c.releaseFunds(0)}).catch(() => {})""")
    expect(buyer.locator(".txpanel--failed")).to_be_visible(timeout=10000)
    msg = buyer.locator(".txpanel__error").inner_text()
    print("   revert message:", msg)
    assert "Released" in msg, msg
    buyer.screenshot(path=OUT / "08_revert_friendly_error.png")
    step("double release blocked by contract with readable error")

    # ---------------- dispute flow: escrow #1
    buyer.goto(BASE + "/create")
    buyer.fill("#title", "Logo design – freelance")
    buyer.fill("#seller", SELLER)
    buyer.fill("#arbiter", ARBITER)
    buyer.fill("#amount", "0.2")
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    buyer.wait_for_url(BASE + "/escrow/1", timeout=30000)

    buyer.goto(BASE + "/escrow/1")
    confirm_click(buyer, "Raise dispute")
    expect(buyer.locator(".badge--disputed").first).to_be_visible(timeout=10000)
    buyer.screenshot(path=OUT / "09_escrow_disputed.png", full_page=True)

    arbiter = new_page(browser, ARBITER)
    arbiter.goto(BASE + "/escrow/1")
    expect(arbiter.get_by_role("button", name="Refund the buyer")).to_be_visible(timeout=10000)
    arbiter.screenshot(path=OUT / "10_arbiter_view.png", full_page=True)
    confirm_click(arbiter, "Refund the buyer")
    expect(arbiter.locator(".badge--refunded").first).to_be_visible(timeout=10000)
    expect(arbiter.locator("#timeline")).to_contain_text("in favour of the buyer", timeout=10000)
    arbiter.screenshot(path=OUT / "11_dispute_resolved.png", full_page=True)
    step("dispute raised by buyer, arbiter refunded buyer")

    # ---------------- escrow #2 left funded, for dashboard variety
    buyer.goto(BASE + "/create")
    buyer.fill("#title", "Concert tickets x2")
    buyer.fill("#seller", SELLER)
    buyer.fill("#arbiter", ARBITER)
    buyer.fill("#amount", "0.08")
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    buyer.wait_for_url(BASE + "/escrow/2", timeout=30000)
    seller.goto(BASE + "/escrow/2")
    confirm_click(seller, "Cancel & refund buyer")
    expect(seller.locator(".badge--refunded").first).to_be_visible(timeout=10000)
    expect(seller.locator("#timeline")).to_contain_text("refunded", timeout=10000)
    step("seller cancelled escrow #2 -> buyer refunded")

    # ---------------- v2: review-period auto release (escrow #3)
    buyer.goto(BASE + "/create")
    buyer.fill("#title", "Freelance website")
    buyer.fill("#seller", SELLER)
    buyer.fill("#arbiter", ARBITER)
    buyer.fill("#amount", "0.3")
    buyer.select_option("#review", "600")
    expect(buyer.locator("#sum-review")).to_have_text("10 min")
    buyer.screenshot(path=OUT / "17_create_review_period.png", full_page=True)
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    buyer.wait_for_url(BASE + "/escrow/3", timeout=30000)
    seller.goto(BASE + "/escrow/3")
    confirm_click(seller, "Mark as delivered")
    seller.goto(BASE + "/escrow/3")
    expect(seller.get_by_role("button", name="Claim payment")).to_be_disabled(timeout=10000)
    expect(seller.locator(".kv")).to_contain_text("Buyer review ends")
    seller.screenshot(path=OUT / "18_claim_payment_locked.png", full_page=True)
    travel(700, [buyer, seller, arbiter])
    # no reload: the page's ticker must unlock "Claim payment" by itself (live countdown)
    expect(seller.get_by_role("button", name="Claim payment")).to_be_enabled(timeout=15000)
    step("live countdown: Claim payment unlocked without reloading the page")
    buyer.goto(BASE + "/escrow/3")
    expect(buyer.get_by_role("button", name="Raise dispute")).to_be_disabled(timeout=10000)
    expect(buyer.get_by_role("button", name="Release payment to seller")).to_be_enabled()
    # contract also blocks it (bypassing the UI)
    buyer.evaluate("""() => TP.sendTx({action: 'raiseDispute', title: 'Late dispute', escrowId: 3,
                       send: (c) => c.raiseDispute(3)}).catch(() => {})""")
    expect(buyer.locator(".txpanel--failed")).to_be_visible(timeout=10000)
    late_msg = buyer.locator(".txpanel__error").inner_text()
    print("   late dispute message:", late_msg)
    assert "review period ended" in late_msg, late_msg
    seller.goto(BASE + "/escrow/3")
    expect(seller.get_by_role("button", name="Claim payment")).to_be_enabled(timeout=10000)
    confirm_click(seller, "Claim payment")
    expect(seller.locator(".badge--released").first).to_be_visible(timeout=10000)
    expect(seller.locator("#timeline")).to_contain_text("seller claimed", timeout=10000)
    seller.screenshot(path=OUT / "19_payment_claimed.png", full_page=True)
    step("v2: buyer silent -> seller claimed payment after review period")

    # ---------------- v2: arbiter timeout (escrow #4)
    buyer.goto(BASE + "/create")
    buyer.fill("#title", "Camera lens")
    buyer.fill("#seller", SELLER)
    buyer.fill("#arbiter", ARBITER)
    buyer.fill("#amount", "0.15")
    buyer.select_option("#review", "600")
    buyer.get_by_role("button", name="Lock ETH in escrow").click()
    buyer.wait_for_url(BASE + "/escrow/4", timeout=30000)
    buyer.goto(BASE + "/escrow/4")
    confirm_click(buyer, "Raise dispute")
    buyer.goto(BASE + "/escrow/4")
    expect(buyer.get_by_role("button", name="Close dispute & refund buyer")).to_be_disabled(timeout=10000)
    arbiter.goto(BASE + "/escrow/4")
    expect(arbiter.locator(".actions__msg")).to_contain_text("before", timeout=10000)
    travel(700, [buyer, seller, arbiter])
    buyer.goto(BASE + "/escrow/4")
    confirm_click(buyer, "Close dispute & refund buyer")
    expect(buyer.locator(".badge--refunded").first).to_be_visible(timeout=10000)
    expect(buyer.locator("#timeline")).to_contain_text("did not decide in time", timeout=10000)
    buyer.screenshot(path=OUT / "20_dispute_expired.png", full_page=True)
    step("v2: arbiter silent -> buyer closed dispute and was refunded")

    # ---------------- dashboard + history
    buyer.goto(BASE + "/dashboard")
    expect(buyer.locator(".escrow-row")).to_have_count(5, timeout=10000)
    expect(buyer.locator("#stat-count")).to_have_text("5")
    buyer.screenshot(path=OUT / "12_dashboard_buyer.png", full_page=True)
    seller.goto(BASE + "/dashboard")
    expect(seller.locator(".escrow-row")).to_have_count(5, timeout=10000)
    seller.click('.tab[data-role="seller"]')
    seller.screenshot(path=OUT / "13_dashboard_seller.png", full_page=True)
    step("dashboards list escrows by role with stats")

    buyer.goto(BASE + "/history")
    expect(buyer.locator("#history-body tr")).to_have_count(9, timeout=10000)
    expect(buyer.locator(".badge--tx-confirmed")).to_have_count(9, timeout=15000)
    buyer.reload()
    expect(buyer.locator(".verified")).to_have_count(9, timeout=15000)
    expect(buyer.locator(".badge--tx-failed")).to_have_count(0)  # rejected pre-flight -> never broadcast
    buyer.screenshot(path=OUT / "14_history.png", full_page=True)
    step("history: 9 txs, all confirmed AND verified on-chain by the backend")

    # ---------------- wrong network banner + mobile layout
    wrong = browser.new_context(viewport={"width": 1280, "height": 800})
    wrong.add_init_script("""window.ethereum = {request: async ({method}) =>
        method === 'eth_chainId' ? '0x1' : method === 'eth_accounts' ? ['%s'] : null, on(){}};""" % BUYER)
    wp = wrong.new_page()
    wp.goto(BASE + "/dashboard")
    expect(wp.locator("#banner-network")).to_contain_text("Wrong network")
    wp.screenshot(path=OUT / "15_wrong_network.png")
    nowallet = browser.new_context().new_page()
    nowallet.goto(BASE + "/dashboard")
    expect(nowallet.locator("#banner-wallet")).to_contain_text("MetaMask not detected")
    step("wrong-network and no-wallet banners")

    mobile = new_page(browser, BUYER, width=390)
    mobile.goto(BASE + "/escrow/0")
    expect(mobile.locator(".badge--released").first).to_be_visible(timeout=10000)
    mobile.screenshot(path=OUT / "16_mobile_escrow.png", full_page=True)
    assert mobile.evaluate("document.documentElement.scrollWidth") <= 390, "horizontal scroll on mobile"
    step("mobile layout has no horizontal scroll")

    browser.close()

SERVER.terminate()
relevant = [e for e in errors if "favicon" not in e]
print("\nconsole/page errors:", len(relevant))
for e in relevant:
    print("  ", e)
print("\nE2E PASSED" if not relevant else "\nE2E finished with console errors")
sys.exit(1 if relevant else 0)
