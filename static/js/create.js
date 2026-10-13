/* Create escrow: client-side validation, live summary, createEscrow transaction. */
(function () {
  "use strict";
  const { $, shortAddr, formatEth, formatDate, sameAddr } = TP;

  const MIN_MINUTES = 6;            // contract minimum is 5 min; +1 min buffer for mining time
  const MAX_DAYS = 365;
  const AMOUNT_RE = /^(?:\d+)(?:\.\d{1,18})?$/;
  const ZERO = "0x0000000000000000000000000000000000000000";
  let balanceWei = null;

  const f = {};

  function toLocalInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function setError(name, msg) {
    const el = document.querySelector(`.field__error[data-for="${name}"]`);
    el.textContent = msg || "";
    f[name].classList.toggle("is-invalid", !!msg);
    f[name].setAttribute("aria-invalid", msg ? "true" : "false");
  }

  // Each validator returns an error message or "".
  const validators = {
    title(v) {
      const bytes = new TextEncoder().encode(v.trim()).length;
      if (!v.trim()) return "Please describe what this payment is for.";
      if (bytes > 64) return "Too long — maximum 64 characters.";
      return "";
    },
    seller(v) {
      v = v.trim();
      if (!v) return "Seller address is required.";
      if (!ethers.isAddress(v)) return "Not a valid Ethereum address (0x + 40 hex characters).";
      if (sameAddr(v, ZERO)) return "The zero address cannot be a seller.";
      if (sameAddr(v, TP.state.account)) return "You cannot be your own seller.";
      return "";
    },
    arbiter(v) {
      v = v.trim();
      if (!v) return "Arbiter address is required.";
      if (!ethers.isAddress(v)) return "Not a valid Ethereum address (0x + 40 hex characters).";
      if (sameAddr(v, ZERO)) return "The zero address cannot be an arbiter.";
      if (sameAddr(v, TP.state.account)) return "The arbiter must be someone other than you.";
      if (sameAddr(v, f.seller.value.trim())) return "The arbiter must be different from the seller.";
      return "";
    },
    amount(v) {
      v = v.trim();
      if (!v) return "Enter an amount.";
      if (!AMOUNT_RE.test(v)) return "Use a positive number with at most 18 decimals.";
      const wei = ethers.parseEther(v);
      if (wei === 0n) return "Amount must be greater than 0.";
      if (balanceWei !== null && wei >= balanceWei) return `Not enough ETH — your balance is ${formatEth(balanceWei, 4)} ETH (gas is extra).`;
      return "";
    },
    review(v) {
      const n = Number(v);
      if (!Number.isInteger(n) || n < 300 || n > 30 * 86400) return "Choose a review period between 5 minutes and 30 days.";
      return "";
    },
    deadline(v) {
      if (!v) return "Pick a deadline.";
      const t = new Date(v).getTime();
      if (Number.isNaN(t)) return "Invalid date.";
      if (t < Date.now() + MIN_MINUTES * 60000) return `Deadline must be at least ${MIN_MINUTES} minutes from now.`;
      if (t > Date.now() + MAX_DAYS * 86400000) return `Deadline must be within ${MAX_DAYS} days.`;
      return "";
    },
  };

  function validate(name) {
    const msg = validators[name](f[name].value);
    setError(name, msg);
    return !msg;
  }

  function updateSummary() {
    const t = f.title.value.trim();
    $("#title-count").textContent = `${new TextEncoder().encode(t).length} / 64`;
    $("#sum-title").textContent = t || "—";
    $("#sum-seller").textContent = ethers.isAddress(f.seller.value.trim()) ? shortAddr(f.seller.value.trim()) : "—";
    $("#sum-arbiter").textContent = ethers.isAddress(f.arbiter.value.trim()) ? shortAddr(f.arbiter.value.trim()) : "—";
    $("#sum-deadline").textContent = f.deadline.value ? formatDate(new Date(f.deadline.value).getTime() / 1000) : "—";
    $("#sum-review").textContent = TP.formatDuration(f.review.value);
    const a = f.amount.value.trim();
    $("#sum-amount").textContent = AMOUNT_RE.test(a) ? a : "0";
  }

  async function onWallet(state) {
    $("#sum-buyer").textContent = state.account ? shortAddr(state.account) : "Connect wallet";
    balanceWei = null;
    $("#balance-hint").textContent = "";
    if (state.account && state.chainOk) {
      try {
        balanceWei = await state.provider.getBalance(state.account);
        $("#balance-hint").textContent = `Balance: ${formatEth(balanceWei, 4)} ETH`;
      } catch (_) { /* ignore */ }
    }
  }

  async function submit(ev) {
    ev.preventDefault();
    if (!TP.state.account) {
      try { await TP.connect(); } catch (e) { TP.toastError(e); return; }
    }
    const ok = ["title", "seller", "arbiter", "amount", "deadline", "review"].map(validate).every(Boolean);
    if (!ok) {
      const first = document.querySelector(".is-invalid");
      if (first) first.focus();
      return;
    }

    const title = f.title.value.trim();
    const seller = ethers.getAddress(f.seller.value.trim().toLowerCase());
    const arbiter = ethers.getAddress(f.arbiter.value.trim().toLowerCase());
    const value = ethers.parseEther(f.amount.value.trim());
    const deadline = BigInt(Math.floor(new Date(f.deadline.value).getTime() / 1000));
    const reviewPeriod = Number(f.review.value);

    const btn = $("#submit-btn");
    btn.disabled = true;
    btn.textContent = "Waiting for confirmation…";
    try {
      const { escrowId } = await TP.sendTx({
        action: "createEscrow",
        title: `Create escrow · ${formatEth(value)} ETH`,
        amountWei: value,
        send: (c) => c.createEscrow(seller, arbiter, deadline, reviewPeriod, title, { value }),
      });
      btn.textContent = "Escrow created — opening…";
      setTimeout(() => { window.location.href = `/escrow/${escrowId}`; }, 1200);
    } catch (_) {
      btn.disabled = false;
      btn.textContent = "Lock ETH in escrow";
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const name of ["title", "seller", "arbiter", "amount", "deadline", "review"]) {
      f[name] = document.getElementById(name);
      f[name].addEventListener(name === "review" ? "change" : "input", () => {
        updateSummary();
        if (f[name].classList.contains("is-invalid")) validate(name);
      });
      f[name].addEventListener("blur", () => { if (f[name].value) validate(name); });
    }
    const now = new Date();
    f.deadline.min = toLocalInput(new Date(now.getTime() + MIN_MINUTES * 60000));
    f.deadline.max = toLocalInput(new Date(now.getTime() + MAX_DAYS * 86400000));
    f.deadline.value = toLocalInput(new Date(now.getTime() + 7 * 86400000));

    document.querySelectorAll(".quick button").forEach((b) => {
      b.addEventListener("click", () => {
        f.deadline.value = toLocalInput(new Date(Date.now() + Number(b.dataset.add) * 60000 + 60000));
        validate("deadline");
        updateSummary();
      });
    });

    $("#create-form").addEventListener("submit", submit);
    updateSummary();
    TP.onReady(onWallet);
  });
})();
