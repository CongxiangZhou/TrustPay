/* History: the connected wallet's transaction log from the Flask/SQLite backend. */
(function () {
  "use strict";
  const { $, esc, shortAddr, formatEth } = TP;

  const ACTION_LABEL = {
    createEscrow: "Create escrow",
    markDelivered: "Mark delivered",
    releaseFunds: "Release funds",
    refundBuyer: "Refund buyer",
    claimRefund: "Claim refund",
    raiseDispute: "Raise dispute",
    resolveDispute: "Resolve dispute",
    claimPayment: "Claim payment (review expired)",
    expireDispute: "Close expired dispute",
  };

  async function load(state) {
    const body = $("#history-body");
    if (!state.account) {
      body.innerHTML = `<tr><td colspan="8" class="empty">Connect MetaMask to see your transactions.
        <button class="btn btn--primary btn--sm" type="button" id="empty-connect">Connect MetaMask</button></td></tr>`;
      const b = $("#empty-connect");
      if (b) b.onclick = () => TP.connect().catch(TP.toastError);
      return;
    }
    body.innerHTML = `<tr><td colspan="8" class="empty"><span class="spinner"></span> Loading…</td></tr>`;
    try {
      const params = { address: state.account };
      const status = $("#filter-status").value;
      if (status) params.status = status;
      const rows = await TP.backend.list(params);
      if (!rows.length) {
        body.innerHTML = `<tr><td colspan="8" class="empty">No transactions yet. <a href="/create">Create an escrow →</a></td></tr>`;
        return;
      }
      body.innerHTML = rows.map((t) => `
        <tr>
          <td class="nowrap">${esc(new Date(t.created_at).toLocaleString())}</td>
          <td>${esc(ACTION_LABEL[t.action] || t.action)}</td>
          <td>${t.escrow_id !== null ? `<a href="/escrow/${t.escrow_id}">#${t.escrow_id}</a>` : "—"}</td>
          <td class="num">${t.amount_wei ? formatEth(BigInt(t.amount_wei)) + " ETH" : "—"}</td>
          <td><span class="badge badge--tx-${esc(t.status)}">${esc(t.status)}</span>
              ${t.verified ? `<div class="verified" title="Status read from the Sepolia receipt by the TrustPay server">✓ verified on-chain</div>` : t.status === "pending" ? `<div class="small muted">awaiting on-chain check</div>` : ""}
              ${t.error_message ? `<div class="small muted">${esc(t.error_message)}</div>` : ""}</td>
          <td class="num">${t.block_number ?? "—"}</td>
          <td class="num">${t.gas_used ? Number(t.gas_used).toLocaleString() : "—"}</td>
          <td><a class="mono" href="${esc(t.explorer_url)}" target="_blank" rel="noopener">${shortAddr(t.tx_hash)} ↗</a></td>
        </tr>`).join("");
    } catch (err) {
      body.innerHTML = `<tr><td colspan="8" class="empty empty--error">${esc(TP.friendlyError(err))}</td></tr>`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("#filter-status").addEventListener("change", () => load(TP.state));
    $("#refresh-btn").addEventListener("click", () => load(TP.state));
    TP.onReady(load);
  });
})();
