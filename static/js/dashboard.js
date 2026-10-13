/* Dashboard: platform stats + the connected wallet's escrows, filterable by role. */
(function () {
  "use strict";
  const { $, esc, shortAddr, formatEth, formatDate, timeLeft, statusBadge, parseEscrow, roleOf } = TP;

  let escrows = [];
  let activeRole = "all";

  async function load(state) {
    const list = $("#escrow-list");
    if (!TP.contractConfigured()) {
      list.innerHTML = `<div class="empty">The contract address is not configured yet.</div>`;
      return;
    }
    if (!state.account) {
      list.innerHTML = `<div class="empty">Connect MetaMask to see your escrows.
        <button class="btn btn--primary btn--sm" type="button" id="empty-connect">Connect MetaMask</button></div>`;
      const b = $("#empty-connect");
      if (b) b.onclick = () => TP.connect().catch(TP.toastError);
      return;
    }
    if (!state.chainOk) {
      list.innerHTML = `<div class="empty">Switch MetaMask to the Sepolia network to load your escrows.</div>`;
      return;
    }

    list.innerHTML = `<div class="empty"><span class="spinner"></span> Loading from Sepolia…</div>`;
    try {
      const contract = TP.readContract();
      const [stats, ids] = await Promise.all([contract.getStats(), contract.getUserEscrows(state.account)]);
      $("#stat-count").textContent = stats.count.toString();
      $("#stat-locked").textContent = formatEth(stats.valueLocked, 4);
      $("#stat-released").textContent = formatEth(stats.released, 4);
      $("#stat-refunded").textContent = formatEth(stats.refunded, 4);

      // An id appears more than once only if the same wallet holds two roles, which the contract forbids,
      // but de-duplicate defensively.
      const uniqueIds = [...new Set(ids.map((x) => Number(x)))].sort((a, b) => b - a);
      const raw = await Promise.all(uniqueIds.map((id) => contract.getEscrow(id)));
      escrows = raw.map((r, i) => {
        const e = parseEscrow(uniqueIds[i], r);
        e.role = roleOf(e, state.account);
        return e;
      });
      render();
    } catch (err) {
      list.innerHTML = `<div class="empty empty--error">${esc(TP.friendlyError(err))}</div>`;
    }
  }

  function render() {
    for (const role of ["all", "buyer", "seller", "arbiter"]) {
      $("#count-" + role).textContent = role === "all" ? escrows.length : escrows.filter((e) => e.role === role).length;
    }
    const shown = activeRole === "all" ? escrows : escrows.filter((e) => e.role === activeRole);
    const list = $("#escrow-list");
    if (!shown.length) {
      list.innerHTML = `<div class="empty">No escrows here yet. <a href="/create">Create your first escrow →</a></div>`;
      return;
    }
    list.innerHTML = shown.map((e) => {
      const counterpart = e.role === "buyer" ? ["Seller", e.seller] : e.role === "seller" ? ["Buyer", e.buyer] : ["Buyer", e.buyer];
      const nowSec = Date.now() / 1000;
      const windowOver = nowSec > e.windowEndsAt;
      const needsYou =
        (e.role === "seller" && e.status === "Funded" && nowSec <= e.deadline) ||
        (e.role === "seller" && e.status === "Delivered" && windowOver) ||
        (e.role === "buyer" && e.status === "Delivered" && !windowOver) ||
        (e.role === "buyer" && e.status === "Funded" && nowSec > e.deadline) ||
        (e.role !== "arbiter" && e.status === "Disputed" && windowOver) ||
        (e.role === "arbiter" && e.status === "Disputed");
      return `
        <a class="escrow-row" href="/escrow/${e.id}">
          <div class="escrow-row__main">
            <span class="escrow-row__id mono">#${e.id}</span>
            <span class="escrow-row__title">${esc(e.title)}</span>
            ${needsYou ? `<span class="pill pill--action">Action needed</span>` : ""}
          </div>
          <div class="escrow-row__meta">
            <span class="role role--${e.role}">${e.role}</span>
            <span class="muted">${counterpart[0]} <span class="mono">${shortAddr(counterpart[1])}</span></span>
            <span class="muted">Deadline ${formatDate(e.deadline)}${["Funded", "Delivered"].includes(e.status) ? ` · ${timeLeft(e.deadline)}` : ""}</span>
          </div>
          <div class="escrow-row__right">
            <span class="escrow-row__amount">${formatEth(e.amount)} <small>ETH</small></span>
            ${statusBadge(e.status)}
          </div>
        </a>`;
    }).join("");
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#role-tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll("#role-tabs .tab").forEach((t) => t.classList.remove("is-active"));
        tab.classList.add("is-active");
        activeRole = tab.dataset.role;
        render();
      });
    });
    $("#refresh-btn").addEventListener("click", () => load(TP.state));
    TP.onReady(load);
  });
})();
