/* Escrow detail: on-chain details, role-aware actions, progress tracker and activity timeline. */
(function () {
  "use strict";
  const { $, esc, shortAddr, formatEth, formatDate, formatDuration, timeLeft, statusBadge, parseEscrow, roleOf, addrLink, txLink } = TP;

  const root = () => $("#escrow-root");
  const escrowId = () => Number(root().dataset.escrowId);

  // ------------------------------------------------------------------ actions per role/state
  function actionsFor(e, role) {
    const nowSec = Date.now() / 1000;
    const deadlinePassed = nowSec > e.deadline;
    const windowOver = nowSec > e.windowEndsAt;   // review (Delivered) or arbiter (Disputed) window
    const A = [];
    if (role === "buyer") {
      if (e.status === "Funded" && deadlinePassed) {
        A.push({ fn: "claimRefund", label: "Claim refund", kind: "primary",
          help: "The deadline passed without delivery — take your ETH back." });
      }
      if (e.status === "Funded" || e.status === "Delivered") {
        A.push({ fn: "releaseFunds", label: "Release payment to seller", kind: e.status === "Delivered" ? "primary" : "secondary",
          help: e.status === "Delivered"
            ? `Only do this once you have received what you paid for. Your review period ${windowOver ? "has ended — the seller can now claim the payment" : `ends ${formatDate(e.windowEndsAt)} (${timeLeft(e.windowEndsAt)})`}.`
            : "Only do this once you have received what you paid for. This cannot be undone." });
        if (e.status === "Funded" || !windowOver) {
          A.push({ fn: "raiseDispute", label: "Raise dispute", kind: "danger",
            help: e.status === "Delivered"
              ? `Freeze the funds and let the arbiter decide. Only possible until ${formatDate(e.windowEndsAt)}.`
              : "Freeze the funds and let the arbiter decide." });
        } else {
          A.push({ disabled: true, label: "Raise dispute", kind: "danger",
            help: "Your review period has ended, so you can no longer dispute — the seller is now entitled to claim the payment." });
        }
      }
      if (e.status === "Funded" && !deadlinePassed) {
        A.push({ disabled: true, label: "Claim refund", kind: "secondary",
          help: `Available after the deadline (${timeLeft(e.deadline)}) if the seller has not marked delivery.` });
      }
      if (e.status === "Disputed") A.push(expireAction(e, windowOver));
    } else if (role === "seller") {
      if (e.status === "Funded" && !deadlinePassed) {
        A.push({ fn: "markDelivered", label: "Mark as delivered", kind: "primary",
          help: `Confirm you have shipped the item / delivered the work. This starts the buyer's ${formatDuration(e.reviewPeriod)} review period.` });
      }
      if (e.status === "Funded" && deadlinePassed) {
        A.push({ disabled: true, label: "Mark as delivered", kind: "secondary",
          help: "The delivery deadline has passed, so delivery can no longer be marked. Refund the buyer or wait for them to claim a refund." });
      }
      if (e.status === "Delivered") {
        A.push(windowOver
          ? { fn: "claimPayment", label: "Claim payment", kind: "primary",
              help: "The buyer's review period ended without a release or dispute — you can now collect the payment." }
          : { disabled: true, label: "Claim payment", kind: "secondary",
              help: `If the buyer stays silent, you can claim the payment after ${formatDate(e.windowEndsAt)} (${timeLeft(e.windowEndsAt)}).` });
      }
      if (e.status === "Funded" || e.status === "Delivered") {
        A.push({ fn: "refundBuyer", label: "Cancel & refund buyer", kind: "secondary",
          help: "Return the full amount to the buyer. This cannot be undone." });
        A.push({ fn: "raiseDispute", label: "Raise dispute", kind: "danger",
          help: "Freeze the funds and let the arbiter decide." });
      }
      if (e.status === "Disputed") A.push(expireAction(e, windowOver));
    } else if (role === "arbiter" && e.status === "Disputed") {
      A.push({ fn: "resolveDispute", args: [true], label: "Pay the seller", kind: "primary",
        help: `Send ${formatEth(e.amount)} ETH to the seller.` });
      A.push({ fn: "resolveDispute", args: [false], label: "Refund the buyer", kind: "secondary",
        help: `Return ${formatEth(e.amount)} ETH to the buyer.` });
    }
    return A;
  }

  function expireAction(e, windowOver) {
    return windowOver
      ? { fn: "expireDispute", label: "Close dispute & refund buyer", kind: "secondary",
          help: "The arbiter did not decide in time. Closing the dispute returns the funds to the buyer." }
      : { disabled: true, label: "Close dispute & refund buyer", kind: "secondary",
          help: `Available if the arbiter has not decided by ${formatDate(e.windowEndsAt)} (${timeLeft(e.windowEndsAt)}).` };
  }

  function roleMessage(e, role) {
    const final = e.status === "Released" || e.status === "Refunded";
    if (final) {
      const who = e.status === "Released" ? "the seller" : "the buyer";
      return `This escrow is closed — ${formatEth(e.amount)} ETH was paid to ${who}${e.resolvedByArbiter ? " by the arbiter's decision" : ""}.`;
    }
    if (role === "viewer") return "Your connected wallet is not a party to this escrow, so you can only view it.";
    if (role === "arbiter") return e.status === "Disputed"
      ? `The parties are in dispute. Review the evidence they share with you, then decide who receives the funds — before ${formatDate(e.windowEndsAt)}, or the buyer can close the dispute and be refunded.`
      : "You are the arbiter. You only need to act if the buyer or seller raises a dispute.";
    if (e.status === "Disputed") return `This escrow is frozen. Waiting for the arbiter's decision (time limit ${formatDate(e.windowEndsAt)}).`;
    if (role === "buyer" && e.status === "Funded") return "Your ETH is locked. Waiting for the seller to deliver.";
    if (role === "buyer" && e.status === "Delivered") return Date.now() / 1000 > e.windowEndsAt
      ? "The seller marked this as delivered and your review period has ended. You can still release the payment; otherwise the seller can claim it."
      : `The seller marked this as delivered. Check what you received, then release the payment or raise a dispute before ${formatDate(e.windowEndsAt)}.`;
    if (role === "seller" && e.status === "Funded") return "The buyer's ETH is locked in the contract. Deliver, then mark it as delivered.";
    if (role === "seller" && e.status === "Delivered") return "Waiting for the buyer to release the payment. If they stay silent for the whole review period, you can claim it.";
    return "";
  }

  // ------------------------------------------------------------------ progress tracker
  // Steps are built from what actually happened. Before the on-chain events have loaded
  // we only know the current status; once they load (loadTimeline) the tracker is redrawn
  // from the real history, e.g. Funded -> Refunded for a refund without delivery.
  function progress(e, eventNames) {
    const final = e.status === "Released" || e.status === "Refunded";
    const steps = ["Funded"];
    if (eventNames) {
      if (eventNames.has("DeliveryMarked")) steps.push("Delivered");
      if (eventNames.has("DisputeRaised")) steps.push("Disputed");
    } else {
      if (e.status === "Delivered") steps.push("Delivered");
      if (e.status === "Disputed" || e.resolvedByArbiter) steps.push("Disputed");
    }
    if (final) steps.push(e.status);
    else if (e.status === "Funded") steps.push("Delivered", "Settled");
    else steps.push("Settled");
    const cur = final ? steps.length : steps.lastIndexOf(e.status);
    return `<ol class="progress">${steps.map((s, i) =>
      `<li class="${i < cur ? "done" : i === cur ? "current" : ""} ${s === "Disputed" ? "is-dispute" : ""}">${s}</li>`
    ).join("")}</ol>`;
  }

  // ------------------------------------------------------------------ render
  function party(label, addr, isYou) {
    return `<div class="party">
      <span class="party__label">${label}${isYou ? ' <span class="you">you</span>' : ""}</span>
      <a class="mono" href="${addrLink(addr)}" target="_blank" rel="noopener" title="${esc(addr)}">${shortAddr(addr)}</a>
      <button class="copy" type="button" data-copy="${esc(addr)}" aria-label="Copy address">⧉</button>
    </div>`;
  }

  // Time-dependent parts (countdowns and which buttons are enabled) are re-evaluated by a
  // ticker, so e.g. "Claim payment" unlocks by itself when the review period ends.
  const live = { e: null, role: null, busy: false, timer: null, lastPoll: 0 };
  const left = (ts) => `<span data-left="${ts}">${timeLeft(ts)}</span>`;

  function render(e, role) {
    root().innerHTML = `
      <div class="detail-head">
        <div>
          <span class="mono muted">Escrow #${e.id}</span>
          <h1>${esc(e.title)}</h1>
          <p class="muted">${esc(e.statusHint)}</p>
        </div>
        <div class="detail-head__right">
          <div class="big-amount">${formatEth(e.amount)} <small>ETH</small></div>
          ${statusBadge(e.status)}
        </div>
      </div>

      <div id="progress-wrap">${progress(e)}</div>

      <div class="detail-grid">
        <section class="panel">
          <h2>Details</h2>
          <div class="parties">
            ${party("Buyer", e.buyer, role === "buyer")}
            ${party("Seller", e.seller, role === "seller")}
            ${party("Arbiter", e.arbiter, role === "arbiter")}
          </div>
          <dl class="kv">
            <dt>Created</dt><dd>${formatDate(e.createdAt)}</dd>
            <dt>Deadline</dt><dd>${formatDate(e.deadline)}${["Funded", "Delivered"].includes(e.status) ? ` <span class="muted">(${left(e.deadline)})</span>` : ""}</dd>
            <dt>Review period</dt><dd>${formatDuration(e.reviewPeriod)}</dd>
            ${e.status === "Delivered" ? `<dt>Buyer review ends</dt><dd>${formatDate(e.windowEndsAt)} <span class="muted">(${left(e.windowEndsAt)})</span></dd>` : ""}
            ${e.status === "Disputed" ? `<dt>Arbiter must decide by</dt><dd>${formatDate(e.windowEndsAt)} <span class="muted">(${left(e.windowEndsAt)})</span></dd>` : ""}
            <dt>Resolved by arbiter</dt><dd>${e.resolvedByArbiter ? "Yes" : "No"}</dd>
          </dl>
        </section>

        <section class="panel actions" id="actions-panel"></section>
      </div>

      <section class="panel">
        <div class="panel__head"><h2>On-chain activity</h2><span class="muted small">Read directly from contract events on Sepolia</span></div>
        <ol class="timeline" id="timeline"><li class="muted"><span class="spinner"></span> Loading events…</li></ol>
      </section>
    `;

    root().querySelectorAll("[data-copy]").forEach((b) =>
      b.addEventListener("click", () => {
        navigator.clipboard?.writeText(b.dataset.copy);
        b.textContent = "✓";
        setTimeout(() => (b.textContent = "⧉"), 1200);
      }));

    live.e = e; live.role = role; live.busy = false;
    renderActions(e, role);
  }

  function renderActions(e, role) {
    const panel = document.getElementById("actions-panel");
    if (!panel) return;
    const actions = actionsFor(e, role);
    panel.innerHTML = `
          <h2>Your actions <span class="role role--${role}">${role}</span></h2>
          <p class="actions__msg">${esc(roleMessage(e, role))}</p>
          ${actions.map((a, i) => `
            <div class="action">
              <button class="btn btn--${a.kind} btn--block" type="button" data-action="${i}" ${a.disabled ? "disabled" : ""}>${esc(a.label)}</button>
              <p class="action__help">${esc(a.help)}</p>
            </div>`).join("")}`;

    panel.querySelectorAll("[data-action]").forEach((btn) => {
      const a = actions[Number(btn.dataset.action)];
      if (a.disabled) return;
      let armed = false;
      btn.addEventListener("click", async () => {
        // two-step confirmation for every state-changing action
        if (!armed) {
          armed = true; live.busy = true;
          btn.dataset.label = btn.textContent;
          btn.textContent = "Click again to confirm";
          btn.classList.add("is-armed");
          setTimeout(() => {
            if (armed) { armed = false; live.busy = false; btn.textContent = btn.dataset.label; btn.classList.remove("is-armed"); }
          }, 4000);
          return;
        }
        armed = false; live.busy = true;
        panel.querySelectorAll("[data-action]").forEach((b) => (b.disabled = true));
        btn.textContent = "Waiting for MetaMask…";
        try {
          await TP.sendTx({
            action: a.fn,
            title: `${a.label} · escrow #${e.id}`,
            escrowId: e.id,
            send: (c) => c[a.fn](e.id, ...(a.args || [])),
          });
        } catch (_) { /* panel already shows the error */ }
        load(TP.state);
      });
    });
  }

  // Every 10 s (while no action is in progress): refresh the countdown texts and redraw the
  // action buttons, so an action unlocks as soon as its deadline or window passes. Every 30 s: re-read the escrow so a change made by the other
  // party (e.g. the seller marking delivery) appears without a manual reload.
  async function tick() {
    const { e, role } = live;
    if (!e || live.busy || document.hidden) return;
    root().querySelectorAll("[data-left]").forEach((s) => (s.textContent = timeLeft(Number(s.dataset.left))));
    renderActions(e, role);   // re-evaluates which actions are open and their countdowns
    if (Date.now() - live.lastPoll > 30000) {
      live.lastPoll = Date.now();
      try {
        const fresh = parseEscrow(e.id, await TP.readContract().getEscrow(e.id));
        if (!live.busy && fresh.status !== e.status) load(TP.state);
      } catch (_) { /* RPC hiccup: try again next time */ }
    }
  }

  // ------------------------------------------------------------------ timeline from events
  const EVENT_TEXT = {
    EscrowCreated: (a) => `Escrow created and <strong>${formatEth(a.amount)} ETH</strong> locked by the buyer`,
    DeliveryMarked: () => "Seller marked the order as <strong>delivered</strong>",
    FundsReleased: (a) => `<strong>${formatEth(a.amount)} ETH released</strong> to the seller`,
    BuyerRefunded: (a) => `<strong>${formatEth(a.amount)} ETH refunded</strong> to the buyer`,
    DisputeRaised: (a) => `Dispute raised by <span class="mono">${shortAddr(a.raisedBy)}</span>`,
    DisputeResolved: (a) => `Arbiter resolved the dispute in favour of the <strong>${a.paidToSeller ? "seller" : "buyer"}</strong>`,
    PaymentClaimed: (a) => `Review period ended — seller claimed <strong>${formatEth(a.amount)} ETH</strong>`,
    DisputeExpired: (a) => `Arbiter did not decide in time — <strong>${formatEth(a.amount)} ETH refunded</strong> to the buyer`,
  };

  async function loadTimeline(contract, id, escrow) {
    const el = $("#timeline");
    try {
      const from = TP.CONFIG.deployBlock || 0;
      const names = Object.keys(EVENT_TEXT);
      const results = await Promise.all(names.map((n) => contract.queryFilter(contract.filters[n](id), from)));
      const events = results.flat().sort((x, y) => x.blockNumber - y.blockNumber || x.index - y.index);
      const wrap = document.getElementById("progress-wrap");
      if (wrap && escrow) wrap.innerHTML = progress(escrow, new Set(events.map((ev) => ev.fragment.name)));
      if (!events.length) {
        el.innerHTML = `<li class="muted">No events found.</li>`;
        return;
      }
      const blocks = {};
      await Promise.all([...new Set(events.map((ev) => ev.blockNumber))].map(async (n) => {
        blocks[n] = await contract.runner.provider.getBlock(n);
      }));
      el.innerHTML = events.map((ev) => `
        <li>
          <span class="timeline__dot timeline__dot--${ev.fragment.name}"></span>
          <div>
            <div>${EVENT_TEXT[ev.fragment.name](ev.args)}</div>
            <div class="muted small">${blocks[ev.blockNumber] ? formatDate(blocks[ev.blockNumber].timestamp) : ""} · block ${ev.blockNumber} ·
              <a class="mono" href="${txLink(ev.transactionHash)}" target="_blank" rel="noopener">${shortAddr(ev.transactionHash)} ↗</a></div>
          </div>
        </li>`).join("");
    } catch (err) {
      console.warn(err);
      el.innerHTML = `<li class="muted">Could not load events from the RPC (${esc(TP.friendlyError(err))}). See the History page for the transaction log.</li>`;
    }
  }

  // ------------------------------------------------------------------ load
  async function load(state) {
    const id = escrowId();
    live.e = null;
    if (!TP.contractConfigured()) {
      root().innerHTML = `<div class="empty">The contract address is not configured yet.</div>`;
      return;
    }
    if (!state.account || !state.chainOk) {
      root().innerHTML = `<div class="empty">${!state.account ? "Connect MetaMask" : "Switch MetaMask to Sepolia"} to view escrow #${id}.
        ${!state.account ? `<button class="btn btn--primary btn--sm" type="button" id="empty-connect">Connect MetaMask</button>` : ""}</div>`;
      const b = $("#empty-connect");
      if (b) b.onclick = () => TP.connect().catch(TP.toastError);
      return;
    }
    try {
      const contract = TP.readContract();
      const e = parseEscrow(id, await contract.getEscrow(id));
      render(e, roleOf(e, state.account));
      live.lastPoll = Date.now();
      if (!live.timer) live.timer = setInterval(tick, 10000);
      loadTimeline(contract, id, e);
    } catch (err) {
      root().innerHTML = `<div class="empty empty--error">${esc(TP.friendlyError(err))}</div>`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => TP.onReady(load));
})();
