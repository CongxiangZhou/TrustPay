/* TrustPay - shared frontend logic
 * - MetaMask connection + Sepolia network enforcement
 * - Contract access (ethers v6)
 * - Transaction lifecycle UI: awaiting signature -> pending -> confirmed / failed,
 *   mirrored to the Flask backend (/api/transactions)
 * - Human-readable error messages for wallet and contract errors
 */
(function () {
  "use strict";

  const CONFIG = JSON.parse(document.getElementById("tp-config").textContent);
  const SEPOLIA_HEX = "0x" + CONFIG.chainId.toString(16); // 0xaa36a7

  const STATUS = ["None", "Funded", "Delivered", "Disputed", "Released", "Refunded"];
  const STATUS_HINT = {
    Funded: "ETH locked · waiting for delivery",
    Delivered: "Seller marked delivered · buyer reviewing",
    Disputed: "Frozen · waiting for arbiter",
    Released: "Paid to seller",
    Refunded: "Returned to buyer",
  };

  const state = {
    abi: null,
    provider: null,   // ethers.BrowserProvider
    account: null,    // checksummed address or null
    chainOk: false,
    ready: false,
  };
  const readyCallbacks = [];

  // ------------------------------------------------------------------ utils
  const $ = (sel, root = document) => root.querySelector(sel);

  function esc(value) {
    return String(value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function shortAddr(addr) {
    return addr ? addr.slice(0, 6) + "…" + addr.slice(-4) : "";
  }

  function formatEth(wei, digits = 6) {
    const s = ethers.formatEther(wei);
    const [i, d = ""] = s.split(".");
    const trimmed = d.slice(0, digits).replace(/0+$/, "");
    return trimmed ? `${i}.${trimmed}` : i;
  }

  function formatDate(unixSeconds) {
    return new Date(Number(unixSeconds) * 1000).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function formatDuration(seconds) {
    seconds = Number(seconds);
    if (seconds % 86400 === 0) return `${seconds / 86400} day${seconds === 86400 ? "" : "s"}`;
    if (seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? "" : "s"}`;
    return `${Math.round(seconds / 60)} min`;
  }

  function timeLeft(unixSeconds) {
    const diff = Number(unixSeconds) * 1000 - Date.now();
    if (diff <= 0) return "passed";
    const m = Math.floor(diff / 60000);
    if (m < 60) return `${m} min left`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h} h left`;
    return `${Math.floor(h / 24)} days left`;
  }

  function txLink(hash) {
    return `${CONFIG.explorerUrl}/tx/${hash}`;
  }

  function addrLink(addr) {
    return `${CONFIG.explorerUrl}/address/${addr}`;
  }

  function statusBadge(statusName) {
    return `<span class="badge badge--${statusName.toLowerCase()}">${esc(statusName)}</span>`;
  }

  function sameAddr(a, b) {
    return !!a && !!b && a.toLowerCase() === b.toLowerCase();
  }

  function parseEscrow(id, r) {
    const status = STATUS[Number(r.status)];
    return {
      id: Number(id),
      buyer: r.buyer,
      amount: r.amount,
      seller: r.seller,
      deadline: Number(r.deadline),
      actionAt: Number(r.actionAt),
      reviewPeriod: Number(r.reviewPeriod),
      // end of the buyer review (Delivered) or arbiter window (Disputed)
      windowEndsAt: Number(r.actionAt) + Number(r.reviewPeriod),
      status,
      statusHint: STATUS_HINT[status] || "",
      resolvedByArbiter: r.resolvedByArbiter,
      arbiter: r.arbiter,
      createdAt: Number(r.createdAt),
      title: r.title,
    };
  }

  function roleOf(escrow, account) {
    if (sameAddr(escrow.buyer, account)) return "buyer";
    if (sameAddr(escrow.seller, account)) return "seller";
    if (sameAddr(escrow.arbiter, account)) return "arbiter";
    return "viewer";
  }

  // ------------------------------------------------------------------ banners
  function showBanner(id, kind, html) {
    let el = document.getElementById("banner-" + id);
    if (!el) {
      el = document.createElement("div");
      el.id = "banner-" + id;
      $("#banners").appendChild(el);
    }
    el.className = `banner banner--${kind}`;
    el.innerHTML = `<div class="banner__inner">${html}</div>`;
    return el;
  }

  function hideBanner(id) {
    const el = document.getElementById("banner-" + id);
    if (el) el.remove();
  }

  // ------------------------------------------------------------------ errors
  const CONTRACT_ERRORS = {
    ZeroAmount: () => "The amount must be greater than 0.",
    AmountTooLarge: () => "The amount is too large.",
    InvalidAddress: () => "Seller and arbiter must be valid, non-zero addresses.",
    InvalidParties: () => "Buyer, seller and arbiter must be three different wallets.",
    InvalidDeadline: () => "The deadline must be between 5 minutes and 365 days from now.",
    InvalidReviewPeriod: () => "The review period must be between 5 minutes and 30 days.",
    DeadlinePassed: (args) => `The delivery deadline (${formatDate(args[0])}) has passed — delivery can no longer be marked.`,
    WindowStillOpen: (args) => `Not yet — the waiting period ends ${formatDate(args[0])}.`,
    ReviewPeriodOver: (args) => `The review period ended ${formatDate(args[0])} — the buyer can no longer dispute.`,
    InvalidTitle: () => "The title must be between 1 and 64 characters.",
    EscrowNotFound: () => "This escrow does not exist.",
    NotAuthorized: () => "Your wallet's role does not allow this action on this escrow.",
    InvalidStatus: (args) => `This action is not allowed while the escrow is "${STATUS[Number(args[0])]}".`,
    DeadlineNotReached: (args) => `The deadline has not passed yet (${formatDate(args[0])}).`,
    TransferFailed: () => "The recipient could not receive ETH, so the transaction was cancelled.",
    ReentrantCall: () => "Re-entrant call blocked.",
  };

  function friendlyError(err) {
    if (!err) return "Unknown error";
    const code = err.code || err.info?.error?.code;
    if (code === "ACTION_REJECTED" || code === 4001) return "You rejected the request in MetaMask.";
    if (code === "INSUFFICIENT_FUNDS") return "Not enough Sepolia ETH for the amount plus gas. Get test ETH from a Sepolia faucet.";
    if (code === -32002) return "MetaMask already has a pending request — open MetaMask to continue.";
    if (code === "NETWORK_ERROR") return "The network changed. Please switch MetaMask to Sepolia and retry.";

    let revert = err.revert;
    if (!revert && err.data && state.abi) {
      try { revert = new ethers.Interface(state.abi).parseError(err.data); } catch (_) { /* ignore */ }
    }
    if (revert && CONTRACT_ERRORS[revert.name]) return CONTRACT_ERRORS[revert.name](revert.args || []);
    if (err instanceof TPError) return err.message;
    return err.shortMessage || err.reason || err.message || String(err);
  }

  class TPError extends Error {}

  // ------------------------------------------------------------------ wallet
  function hasWallet() {
    return typeof window.ethereum !== "undefined";
  }

  // "Disconnect" is remembered per browser so the app stays logged out even if
  // the wallet does not support revoking permissions.
  const DISCONNECT_KEY = "trustpay-disconnected";
  function isDisconnected() {
    try { return localStorage.getItem(DISCONNECT_KEY) === "1"; } catch (_) { return false; }
  }
  function setDisconnected(flag) {
    try {
      if (flag) localStorage.setItem(DISCONNECT_KEY, "1");
      else localStorage.removeItem(DISCONNECT_KEY);
    } catch (_) { /* storage unavailable - ignore */ }
  }

  async function refreshWalletState() {
    if (!hasWallet()) return;
    const chainId = await window.ethereum.request({ method: "eth_chainId" });
    state.chainOk = chainId.toLowerCase() === SEPOLIA_HEX;
    const accounts = isDisconnected() ? [] : await window.ethereum.request({ method: "eth_accounts" });
    state.account = accounts.length ? ethers.getAddress(accounts[0]) : null;
    state.provider = new ethers.BrowserProvider(window.ethereum, "any");
    renderWallet();
  }

  async function renderWallet() {
    const btn = $("#connect-btn");
    const chip = $("#account-wrap");
    const net = $("#net-chip");

    if (!hasWallet()) {
      btn.textContent = "Install MetaMask";
      btn.onclick = () => window.open("https://metamask.io/download/", "_blank", "noopener");
      showBanner("wallet", "warn",
        `<strong>MetaMask not detected.</strong> Install the MetaMask browser extension to use TrustPay. ` +
        `<a href="https://metamask.io/download/" target="_blank" rel="noopener">Get MetaMask →</a>`);
      return;
    }

    if (!state.account) {
      btn.hidden = false;
      chip.hidden = true;
      net.hidden = true;
      btn.textContent = "Connect MetaMask";
      btn.onclick = () => connect().catch((e) => toastError(e));
      hideBanner("network");
      return;
    }

    btn.hidden = true;
    chip.hidden = false;
    $("#account-address").textContent = shortAddr(state.account);
    $("#account-chip").title = state.account;
    $("#account-full").textContent = state.account;

    net.hidden = false;
    if (state.chainOk) {
      net.textContent = CONFIG.chainName;
      net.className = "net-chip net-chip--ok";
      hideBanner("network");
      try {
        const bal = await state.provider.getBalance(state.account);
        $("#account-balance").textContent = formatEth(bal, 4) + " ETH";
      } catch (_) { /* ignore */ }
    } else {
      net.textContent = "Wrong network";
      net.className = "net-chip net-chip--bad";
      $("#account-balance").textContent = "";
      const b = showBanner("network", "error",
        `<strong>Wrong network.</strong> TrustPay runs on the Ethereum Sepolia testnet. ` +
        `<button class="btn btn--sm btn--light" type="button" id="switch-net-btn">Switch to Sepolia</button>`);
      $("#switch-net-btn", b).onclick = () => switchToSepolia().catch((e) => toastError(e));
    }
  }

  async function connect() {
    if (!hasWallet()) throw new TPError("MetaMask is not installed.");
    setDisconnected(false);
    await window.ethereum.request({ method: "eth_requestAccounts" });
    await refreshWalletState();
    if (!state.chainOk) await switchToSepolia();
    fireReady();
  }

  /** Opens MetaMask's account picker so the user can connect / choose another account. */
  async function switchAccount() {
    if (!hasWallet()) throw new TPError("MetaMask is not installed.");
    await window.ethereum.request({ method: "wallet_requestPermissions", params: [{ eth_accounts: {} }] });
    setDisconnected(false);
    window.location.reload();
  }

  /** Log out: revoke the site's MetaMask permission (if supported) and forget the account. */
  async function disconnect() {
    setDisconnected(true);
    try {
      await window.ethereum?.request({ method: "wallet_revokePermissions", params: [{ eth_accounts: {} }] });
    } catch (_) { /* older wallets: the local flag above is enough */ }
    window.location.reload();
  }

  function setupAccountMenu() {
    const chip = $("#account-chip");
    const menu = $("#account-menu");
    if (!chip || !menu) return;
    const open = (yes) => {
      menu.hidden = !yes;
      chip.setAttribute("aria-expanded", yes ? "true" : "false");
    };
    chip.addEventListener("click", (e) => { e.stopPropagation(); open(menu.hidden); });
    document.addEventListener("click", (e) => { if (!menu.contains(e.target)) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
    $("#menu-copy").addEventListener("click", () => {
      navigator.clipboard?.writeText(state.account || "");
      $("#menu-copy").textContent = "Copied ✓";
      setTimeout(() => { $("#menu-copy").textContent = "Copy address"; }, 1200);
    });
    $("#menu-switch").addEventListener("click", () => { open(false); switchAccount().catch(toastError); });
    $("#menu-disconnect").addEventListener("click", () => { open(false); disconnect(); });
  }

  async function switchToSepolia() {
    try {
      await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: SEPOLIA_HEX }] });
    } catch (err) {
      if (err.code === 4902) {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [{
            chainId: SEPOLIA_HEX,
            chainName: "Sepolia",
            nativeCurrency: { name: "Sepolia Ether", symbol: "ETH", decimals: 18 },
            rpcUrls: ["https://ethereum-sepolia-rpc.publicnode.com", "https://sepolia.drpc.org"],
            blockExplorerUrls: [CONFIG.explorerUrl],
          }],
        });
      } else {
        throw err;
      }
    }
  }

  // ------------------------------------------------------------------ contract
  function contractConfigured() {
    return /^0x[0-9a-fA-F]{40}$/.test(CONFIG.contractAddress || "");
  }

  function readContract() {
    if (!contractConfigured()) throw new TPError("The contract address is not configured yet.");
    if (!state.chainOk) throw new TPError("Switch MetaMask to Sepolia to load on-chain data.");
    return new ethers.Contract(CONFIG.contractAddress, state.abi, state.provider);
  }

  async function writeContract() {
    if (!hasWallet()) throw new TPError("MetaMask is not installed.");
    if (!state.account) {
      await connect();
    }
    if (!state.chainOk) {
      await switchToSepolia();
      throw new TPError("Switched to Sepolia — please submit again.");
    }
    if (!contractConfigured()) throw new TPError("The contract address is not configured yet.");
    const signer = await state.provider.getSigner();
    return new ethers.Contract(CONFIG.contractAddress, state.abi, signer);
  }

  // ------------------------------------------------------------------ backend API
  async function api(method, url, body) {
    const resp = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new TPError(data.error || `Request failed (${resp.status})`);
    return data;
  }

  const backend = {
    record: (tx) => api("POST", "/api/transactions", tx),
    update: (hash, patch) => api("PATCH", `/api/transactions/${hash}`, patch),
    list: (params) => api("GET", "/api/transactions?" + new URLSearchParams(params).toString()),
  };

  // ------------------------------------------------------------------ tx panel
  const STEP_LABEL = {
    signing: "Confirm in MetaMask",
    pending: "Pending on Sepolia",
    confirmed: "Confirmed",
    failed: "Failed",
  };

  function renderPanel({ step, title, message, hash }) {
    const panel = $("#txpanel");
    panel.hidden = false;
    panel.className = `txpanel txpanel--${step}`;
    const steps = ["signing", "pending", "confirmed"];
    const idx = step === "failed" ? -1 : steps.indexOf(step);
    panel.innerHTML = `
      <div class="txpanel__head">
        <span class="txpanel__title">${esc(title)}</span>
        <button class="txpanel__close" type="button" aria-label="Close">×</button>
      </div>
      <ol class="txsteps">
        ${steps.map((s, i) => `<li class="${i < idx || step === "confirmed" ? "done" : i === idx ? "current" : ""}">${STEP_LABEL[s]}</li>`).join("")}
      </ol>
      ${step === "failed" ? `<p class="txpanel__error">${esc(message)}</p>` : message ? `<p class="txpanel__msg">${esc(message)}</p>` : ""}
      ${hash ? `<a class="txpanel__link mono" href="${txLink(hash)}" target="_blank" rel="noopener">${shortAddr(hash)} · view on Etherscan ↗</a>` : ""}
    `;
    $(".txpanel__close", panel).onclick = () => { panel.hidden = true; };
    if (step === "confirmed") setTimeout(() => { if (panel.className.includes("confirmed")) panel.hidden = true; }, 12000);
  }

  function toastError(err) {
    renderPanel({ step: "failed", title: "Something went wrong", message: friendlyError(err) });
  }

  /**
   * Send a contract transaction with full status tracking.
   * @param {object} opts
   *   action     - contract function name (logged in backend)
   *   title      - text shown in the status panel
   *   escrowId   - escrow id (null for createEscrow)
   *   amountWei  - bigint amount (createEscrow) for the log
   *   send       - async (contract) => TransactionResponse
   * @returns receipt (or throws)
   */
  async function sendTx({ action, title, escrowId = null, amountWei = null, send }) {
    let hash = null;
    try {
      renderPanel({ step: "signing", title, message: "Review and confirm the transaction in MetaMask." });
      const contract = await writeContract();
      const tx = await send(contract);
      hash = tx.hash;

      renderPanel({ step: "pending", title, message: "Waiting for the transaction to be mined (usually ~12–30 s).", hash });
      backend.record({
        tx_hash: hash,
        action,
        escrow_id: escrowId,
        from_address: state.account,
        amount_wei: amountWei != null ? amountWei.toString() : null,
        chain_id: CONFIG.chainId,
      }).catch((e) => console.warn("[TrustPay] backend record failed:", e));

      const receipt = await tx.wait();
      let newEscrowId = escrowId;
      if (action === "createEscrow") {
        for (const log of receipt.logs) {
          try {
            const parsed = contract.interface.parseLog(log);
            if (parsed && parsed.name === "EscrowCreated") newEscrowId = Number(parsed.args.id);
          } catch (_) { /* not our event */ }
        }
      }
      await backend.update(hash, {
        status: "confirmed",
        block_number: receipt.blockNumber,
        gas_used: receipt.gasUsed.toString(),
        escrow_id: newEscrowId,
      }).catch((e) => console.warn("[TrustPay] backend update failed:", e));

      renderPanel({
        step: "confirmed", title,
        message: `Included in block ${receipt.blockNumber} · gas used ${receipt.gasUsed.toLocaleString()}`,
        hash,
      });
      refreshWalletState();
      return { receipt, escrowId: newEscrowId };
    } catch (err) {
      const message = friendlyError(err);
      renderPanel({ step: "failed", title, message, hash });
      if (hash) {
        backend.update(hash, { status: "failed", error_message: message }).catch(() => {});
      }
      throw err;
    }
  }

  // ------------------------------------------------------------------ init
  function onReady(cb) {
    readyCallbacks.push(cb);
    if (state.ready) cb(state);
  }

  function fireReady() {
    state.ready = true;
    readyCallbacks.forEach((cb) => {
      try { cb(state); } catch (e) { console.error(e); }
    });
  }

  async function init() {
    setupAccountMenu();
    if (!contractConfigured()) {
      showBanner("contract", "info",
        "<strong>Contract not configured.</strong> Deploy <code>contracts/TrustPay.sol</code> to Sepolia and set " +
        "<code>CONTRACT_ADDRESS</code> in <code>config.py</code> (see README).");
    }
    try {
      const res = await fetch("/static/abi/TrustPay.abi.json");
      state.abi = await res.json();
    } catch (e) {
      showBanner("abi", "error", "Could not load the contract ABI.");
    }
    if (hasWallet()) {
      window.ethereum.on?.("accountsChanged", () => window.location.reload());
      window.ethereum.on?.("chainChanged", () => window.location.reload());
      try { await refreshWalletState(); } catch (e) { console.warn(e); }
    } else {
      renderWallet();
    }
    fireReady();
  }

  window.TP = {
    CONFIG, STATUS, state,
    $, esc, shortAddr, formatEth, formatDate, formatDuration, timeLeft, txLink, addrLink, statusBadge, sameAddr,
    parseEscrow, roleOf, friendlyError, TPError,
    hasWallet, connect, switchAccount, disconnect, switchToSepolia, contractConfigured, readContract, sendTx, backend,
    onReady, showBanner, hideBanner, toastError,
  };

  document.addEventListener("DOMContentLoaded", init);
})();
