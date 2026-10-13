"""Unit tests for TrustPay.sol running on a local in-memory EVM (eth-tester / py-evm).

Run:  pytest -v tests/test_contract.py
Add -s to also print the gas report used in the project report.
"""
import ast

import pytest
from eth_tester.exceptions import TransactionFailed
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError

FUNDED, DELIVERED, DISPUTED, RELEASED, REFUNDED = 1, 2, 3, 4, 5
ONE_ETH = Web3.to_wei(1, "ether")
DAY = 24 * 60 * 60
REVIEW = 3 * DAY  # default review / arbiter window used in tests


# ---------------------------------------------------------------- helpers

def selector(signature):
    return "0x" + Web3.keccak(text=signature)[:4].hex().removeprefix("0x")


def revert_data(exc):
    """Extract the raw revert data (hex string) from a web3 / eth-tester exception."""
    data = getattr(exc, "data", None)
    if isinstance(data, str):
        return data
    msg = str(exc)
    if "execution reverted: b" in msg:  # eth-tester: "execution reverted: b'\x0b...'"
        raw = ast.literal_eval(msg.split("execution reverted: ", 1)[1])
        return "0x" + raw.hex()
    return msg


def expect_revert(signature, fn, *args, **tx):
    """Assert that calling fn(*args).transact(tx) reverts with the given custom error."""
    with pytest.raises((ContractCustomError, ContractLogicError, TransactionFailed)) as exc:
        fn(*args).transact(tx)
    data = revert_data(exc.value)
    assert data.startswith(selector(signature)), f"expected {signature}, got {data}"


def now(w3):
    return w3.eth.get_block("latest")["timestamp"]


def create(w3, tp, acc, value=ONE_ETH, deadline_offset=7 * DAY, title="Used MacBook Pro", review=REVIEW):
    tx = tp.functions.createEscrow(acc["seller"], acc["arbiter"], now(w3) + deadline_offset, review, title).transact(
        {"from": acc["buyer"], "value": value}
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    return tp.functions.escrowCount().call() - 1, receipt


def status(tp, id_):
    return tp.functions.getEscrow(id_).call()[4]


def balance(w3, addr):
    return w3.eth.get_balance(addr)


# ---------------------------------------------------------------- creation

def test_create_escrow_stores_all_fields(w3, trustpay, accounts):
    id_, receipt = create(w3, trustpay, accounts)
    e = trustpay.functions.getEscrow(id_).call()
    buyer, amount, seller, deadline, _status, by_arbiter, action_at, arbiter, created_at, review, title = e
    assert (buyer, seller, arbiter) == (accounts["buyer"], accounts["seller"], accounts["arbiter"])
    assert amount == ONE_ETH
    assert _status == FUNDED and by_arbiter is False
    assert deadline > created_at
    assert review == REVIEW and action_at == 0
    assert title == "Used MacBook Pro"
    assert w3.eth.get_balance(trustpay.address) == ONE_ETH
    assert trustpay.functions.getStats().call() == [1, ONE_ETH, 0, 0]

    logs = trustpay.events.EscrowCreated().process_receipt(receipt)
    assert logs[0]["args"]["id"] == id_ and logs[0]["args"]["amount"] == ONE_ETH


def test_user_escrow_index_covers_all_roles(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    for role in ("buyer", "seller", "arbiter"):
        assert trustpay.functions.getUserEscrows(accounts[role]).call() == [id_]
    assert trustpay.functions.getUserEscrows(accounts["outsider"]).call() == []


@pytest.mark.parametrize(
    "case, error",
    [
        ("zero_value", "ZeroAmount()"),
        ("zero_seller", "InvalidAddress()"),
        ("zero_arbiter", "InvalidAddress()"),
        ("self_seller", "InvalidParties()"),
        ("self_arbiter", "InvalidParties()"),
        ("seller_is_arbiter", "InvalidParties()"),
        ("deadline_past", "InvalidDeadline()"),
        ("deadline_too_soon", "InvalidDeadline()"),
        ("deadline_too_far", "InvalidDeadline()"),
        ("review_too_short", "InvalidReviewPeriod()"),
        ("review_too_long", "InvalidReviewPeriod()"),
        ("empty_title", "InvalidTitle()"),
        ("long_title", "InvalidTitle()"),
    ],
)
def test_create_escrow_rejects_invalid_input(w3, trustpay, accounts, case, error):
    a = accounts
    zero = "0x" + "0" * 40
    t = now(w3)
    args = dict(seller=a["seller"], arbiter=a["arbiter"], deadline=t + DAY, review=REVIEW, title="Deal", value=ONE_ETH)
    overrides = {
        "zero_value": dict(value=0),
        "zero_seller": dict(seller=zero),
        "zero_arbiter": dict(arbiter=zero),
        "self_seller": dict(seller=a["buyer"]),
        "self_arbiter": dict(arbiter=a["buyer"]),
        "seller_is_arbiter": dict(arbiter=a["seller"]),
        "deadline_past": dict(deadline=t - 1),
        "deadline_too_soon": dict(deadline=t + 60),
        "deadline_too_far": dict(deadline=t + 400 * DAY),
        "review_too_short": dict(review=60),
        "review_too_long": dict(review=31 * DAY),
        "empty_title": dict(title=""),
        "long_title": dict(title="x" * 65),
    }
    args.update(overrides[case])
    expect_revert(
        error,
        trustpay.functions.createEscrow,
        args["seller"], args["arbiter"], args["deadline"], args["review"], args["title"],
        **{"from": a["buyer"], "value": args["value"]},
    )
    assert trustpay.functions.escrowCount().call() == 0


def test_direct_eth_transfer_is_rejected(w3, trustpay, accounts):
    with pytest.raises(Exception):
        w3.eth.send_transaction({"from": accounts["buyer"], "to": trustpay.address, "value": ONE_ETH})


def test_unknown_escrow_reverts(w3, trustpay, accounts):
    expect_revert("EscrowNotFound(uint256)", trustpay.functions.markDelivered, 42, **{"from": accounts["seller"]})


# ---------------------------------------------------------------- happy paths

def test_full_flow_deliver_then_release(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    seller_before = balance(w3, accounts["seller"])

    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == DELIVERED

    trustpay.functions.releaseFunds(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == RELEASED
    assert balance(w3, accounts["seller"]) - seller_before > ONE_ETH * 99 // 100  # minus seller's gas for markDelivered
    assert w3.eth.get_balance(trustpay.address) == 0
    assert trustpay.functions.getStats().call() == [1, 0, ONE_ETH, 0]


def test_buyer_can_release_before_delivery_mark(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    seller_before = balance(w3, accounts["seller"])
    trustpay.functions.releaseFunds(id_).transact({"from": accounts["buyer"]})
    assert balance(w3, accounts["seller"]) - seller_before == ONE_ETH


def test_seller_refunds_buyer(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    buyer_before = balance(w3, accounts["buyer"])
    trustpay.functions.refundBuyer(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == REFUNDED
    assert balance(w3, accounts["buyer"]) - buyer_before == ONE_ETH
    assert trustpay.functions.getStats().call() == [1, 0, 0, ONE_ETH]


def test_seller_refunds_buyer_after_delivery(w3, trustpay, accounts):
    # Delivered -> refundBuyer: the seller can still cancel after marking delivery
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == DELIVERED
    buyer_before = balance(w3, accounts["buyer"])
    trustpay.functions.refundBuyer(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == REFUNDED
    assert balance(w3, accounts["buyer"]) - buyer_before == ONE_ETH
    assert w3.eth.get_balance(trustpay.address) == 0
    expect_revert("InvalidStatus(uint8)", trustpay.functions.claimPayment, id_, **{"from": accounts["seller"]})


def test_buyer_claims_refund_after_deadline(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts, deadline_offset=DAY)
    expect_revert("DeadlineNotReached(uint64)", trustpay.functions.claimRefund, id_, **{"from": accounts["buyer"]})

    w3.provider.ethereum_tester.time_travel(now(w3) + DAY + 10)
    trustpay.functions.claimRefund(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == REFUNDED


def test_no_deadline_refund_after_delivery(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts, deadline_offset=DAY)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    w3.provider.ethereum_tester.time_travel(now(w3) + 2 * DAY)
    expect_revert("InvalidStatus(uint8)", trustpay.functions.claimRefund, id_, **{"from": accounts["buyer"]})


# ---------------------------------------------------------------- disputes

@pytest.mark.parametrize("raiser", ["buyer", "seller"])
@pytest.mark.parametrize("pay_to_seller", [True, False])
def test_dispute_resolution(w3, trustpay, accounts, raiser, pay_to_seller):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts[raiser]})
    assert status(trustpay, id_) == DISPUTED

    # frozen: neither party can move the funds now
    expect_revert("InvalidStatus(uint8)", trustpay.functions.releaseFunds, id_, **{"from": accounts["buyer"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.refundBuyer, id_, **{"from": accounts["seller"]})

    winner = accounts["seller"] if pay_to_seller else accounts["buyer"]
    before = balance(w3, winner)
    receipt = w3.eth.wait_for_transaction_receipt(
        trustpay.functions.resolveDispute(id_, pay_to_seller).transact({"from": accounts["arbiter"]})
    )
    assert balance(w3, winner) - before == ONE_ETH
    e = trustpay.functions.getEscrow(id_).call()
    assert e[4] == (RELEASED if pay_to_seller else REFUNDED) and e[5] is True
    ev = trustpay.events.DisputeResolved().process_receipt(receipt)[0]["args"]
    assert ev["paidToSeller"] == pay_to_seller


def test_only_arbiter_resolves(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    for who in ("buyer", "seller", "outsider"):
        expect_revert("NotAuthorized()", trustpay.functions.resolveDispute, id_, True, **{"from": accounts[who]})


def test_cannot_resolve_without_dispute(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    expect_revert("InvalidStatus(uint8)", trustpay.functions.resolveDispute, id_, True, **{"from": accounts["arbiter"]})


# ---------------------------------------------------------------- access control & state machine

@pytest.mark.parametrize(
    "fn, allowed",
    [
        ("markDelivered", "seller"),
        ("releaseFunds", "buyer"),
        ("refundBuyer", "seller"),
        ("claimRefund", "buyer"),
    ],
)
def test_role_checks(w3, trustpay, accounts, fn, allowed):
    id_, _ = create(w3, trustpay, accounts)
    for who in ("buyer", "seller", "arbiter", "outsider"):
        if who != allowed:
            expect_revert("NotAuthorized()", getattr(trustpay.functions, fn), id_, **{"from": accounts[who]})


def test_outsider_and_arbiter_cannot_raise_dispute(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    for who in ("arbiter", "outsider"):
        expect_revert("NotAuthorized()", trustpay.functions.raiseDispute, id_, **{"from": accounts[who]})


def test_no_double_payout(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.releaseFunds(id_).transact({"from": accounts["buyer"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.releaseFunds, id_, **{"from": accounts["buyer"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.refundBuyer, id_, **{"from": accounts["seller"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.raiseDispute, id_, **{"from": accounts["buyer"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.markDelivered, id_, **{"from": accounts["seller"]})


def test_cannot_mark_delivered_twice(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    expect_revert("InvalidStatus(uint8)", trustpay.functions.markDelivered, id_, **{"from": accounts["seller"]})


def test_escrows_are_isolated(w3, trustpay, accounts):
    a, _ = create(w3, trustpay, accounts, value=ONE_ETH)
    b, _ = create(w3, trustpay, accounts, value=2 * ONE_ETH)
    trustpay.functions.releaseFunds(a).transact({"from": accounts["buyer"]})
    assert status(trustpay, b) == FUNDED
    assert w3.eth.get_balance(trustpay.address) == 2 * ONE_ETH
    assert trustpay.functions.totalValueLocked().call() == 2 * ONE_ETH


# ---------------------------------------------------------------- security

def _deploy_helper(w3, artifacts, name, *args, sender):
    art = artifacts[1][name]
    C = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
    rcpt = w3.eth.wait_for_transaction_receipt(C.constructor(*args).transact({"from": sender}))
    return w3.eth.contract(address=rcpt.contractAddress, abi=art["abi"])


def test_reentrancy_attack_is_blocked(w3, trustpay, accounts, artifacts):
    attacker = _deploy_helper(w3, artifacts, "ReentrantSeller", trustpay.address, sender=accounts["outsider"])
    tx = trustpay.functions.createEscrow(
        attacker.address, accounts["arbiter"], now(w3) + DAY, REVIEW, "Attack test"
    ).transact({"from": accounts["buyer"], "value": ONE_ETH})
    w3.eth.wait_for_transaction_receipt(tx)
    attacker.functions.setTarget(0).transact({"from": accounts["outsider"]})

    trustpay.functions.releaseFunds(0).transact({"from": accounts["buyer"]})

    assert attacker.functions.receivedCount().call() == 1
    assert w3.eth.get_balance(attacker.address) == ONE_ETH          # paid exactly once
    assert w3.eth.get_balance(trustpay.address) == 0
    err = attacker.functions.lastError().call()
    assert "0x" + err.hex().removeprefix("0x")[:8] == selector("ReentrantCall()")
    assert status(trustpay, 0) == RELEASED


def test_failed_transfer_reverts_whole_tx(w3, trustpay, accounts, artifacts):
    rejecter = _deploy_helper(w3, artifacts, "RejectingReceiver", sender=accounts["outsider"])
    tx = trustpay.functions.createEscrow(
        rejecter.address, accounts["arbiter"], now(w3) + DAY, REVIEW, "Reject test"
    ).transact({"from": accounts["buyer"], "value": ONE_ETH})
    w3.eth.wait_for_transaction_receipt(tx)

    expect_revert("TransferFailed()", trustpay.functions.releaseFunds, 0, **{"from": accounts["buyer"]})
    # state rolled back - funds still safely locked, buyer can still go to the arbiter
    assert status(trustpay, 0) == FUNDED
    assert w3.eth.get_balance(trustpay.address) == ONE_ETH
    trustpay.functions.raiseDispute(0).transact({"from": accounts["buyer"]})
    trustpay.functions.resolveDispute(0, False).transact({"from": accounts["arbiter"]})
    assert status(trustpay, 0) == REFUNDED


# ---------------------------------------------------------------- gas report

def test_gas_report(w3, trustpay, accounts):
    """Prints gas used per action (use `pytest -s`) - numbers go in the report."""
    def gas(tx):
        return w3.eth.wait_for_transaction_receipt(tx)["gasUsed"]

    t = now(w3) + 7 * DAY
    f = trustpay.functions
    a = accounts
    report = {
        "createEscrow (first)": gas(f.createEscrow(a["seller"], a["arbiter"], t, REVIEW, "Used MacBook Pro").transact({"from": a["buyer"], "value": ONE_ETH})),
        "createEscrow (repeat)": gas(f.createEscrow(a["seller"], a["arbiter"], t, REVIEW, "Used MacBook Pro").transact({"from": a["buyer"], "value": ONE_ETH})),
        "markDelivered": gas(f.markDelivered(0).transact({"from": a["seller"]})),
        "releaseFunds": gas(f.releaseFunds(0).transact({"from": a["buyer"]})),
        "raiseDispute": gas(f.raiseDispute(1).transact({"from": a["buyer"]})),
        "resolveDispute": gas(f.resolveDispute(1, False).transact({"from": a["arbiter"]})),
    }
    f.createEscrow(a["seller"], a["arbiter"], t, REVIEW, "Timeout demo").transact({"from": a["buyer"], "value": ONE_ETH})
    f.markDelivered(2).transact({"from": a["seller"]})
    w3.provider.ethereum_tester.time_travel(now(w3) + REVIEW + 10)
    report["claimPayment"] = gas(f.claimPayment(2).transact({"from": a["seller"]}))
    f.createEscrow(a["seller"], a["arbiter"], now(w3) + 7 * DAY, REVIEW, "Timeout demo").transact({"from": a["buyer"], "value": ONE_ETH})
    f.raiseDispute(3).transact({"from": a["seller"]})
    w3.provider.ethereum_tester.time_travel(now(w3) + REVIEW + 10)
    report["expireDispute"] = gas(f.expireDispute(3).transact({"from": a["buyer"]}))
    print("\n\nGas report (TrustPay)")
    for k, v in report.items():
        print(f"  {k:<24}{v:>10,}")
        assert v < 300_000


# ---------------------------------------------------------------- v2: timeouts (liveness)

def travel(w3, seconds):
    w3.provider.ethereum_tester.time_travel(now(w3) + seconds)


def test_cannot_mark_delivered_after_deadline(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts, deadline_offset=DAY)
    travel(w3, DAY + 10)
    expect_revert("DeadlinePassed(uint64)", trustpay.functions.markDelivered, id_, **{"from": accounts["seller"]})
    trustpay.functions.claimRefund(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == REFUNDED


def test_mark_delivered_starts_review_clock(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    receipt = w3.eth.wait_for_transaction_receipt(
        trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]}))
    e = trustpay.functions.getEscrow(id_).call()
    assert e[6] == w3.eth.get_block(receipt.blockNumber)["timestamp"]


def test_seller_claims_payment_after_silent_buyer(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    expect_revert("WindowStillOpen(uint64)", trustpay.functions.claimPayment, id_, **{"from": accounts["seller"]})

    travel(w3, REVIEW + 10)
    seller_before = balance(w3, accounts["seller"])
    receipt = w3.eth.wait_for_transaction_receipt(
        trustpay.functions.claimPayment(id_).transact({"from": accounts["seller"]}))
    assert status(trustpay, id_) == RELEASED
    gas_cost = receipt["gasUsed"] * receipt["effectiveGasPrice"]
    assert balance(w3, accounts["seller"]) - seller_before + gas_cost == ONE_ETH
    ev = trustpay.events.PaymentClaimed().process_receipt(receipt)[0]["args"]
    assert ev["amount"] == ONE_ETH
    assert trustpay.functions.getStats().call() == [1, 0, ONE_ETH, 0]


def test_only_seller_claims_payment(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    travel(w3, REVIEW + 10)
    for who in ("buyer", "arbiter", "outsider"):
        expect_revert("NotAuthorized()", trustpay.functions.claimPayment, id_, **{"from": accounts[who]})


def test_claim_payment_needs_delivery(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    travel(w3, REVIEW + 10)
    expect_revert("InvalidStatus(uint8)", trustpay.functions.claimPayment, id_, **{"from": accounts["seller"]})


def test_buyer_dispute_during_review_blocks_claim(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    travel(w3, REVIEW + 10)
    expect_revert("InvalidStatus(uint8)", trustpay.functions.claimPayment, id_, **{"from": accounts["seller"]})


def test_buyer_can_still_release_after_review_window(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    travel(w3, REVIEW + 10)
    trustpay.functions.releaseFunds(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == RELEASED


@pytest.mark.parametrize("caller", ["buyer", "seller"])
def test_dispute_expires_and_refunds_buyer(w3, trustpay, accounts, caller):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["seller"]})
    expect_revert("WindowStillOpen(uint64)", trustpay.functions.expireDispute, id_, **{"from": accounts[caller]})

    travel(w3, REVIEW + 10)
    buyer_before = balance(w3, accounts["buyer"])
    receipt = w3.eth.wait_for_transaction_receipt(
        trustpay.functions.expireDispute(id_).transact({"from": accounts[caller]}))
    assert status(trustpay, id_) == REFUNDED
    gas_cost = receipt["gasUsed"] * receipt["effectiveGasPrice"] if caller == "buyer" else 0
    assert balance(w3, accounts["buyer"]) - buyer_before + gas_cost == ONE_ETH
    assert trustpay.events.DisputeExpired().process_receipt(receipt)[0]["args"]["amount"] == ONE_ETH
    assert trustpay.functions.getEscrow(id_).call()[5] is False   # not resolved by arbiter
    # arbiter can no longer decide
    expect_revert("InvalidStatus(uint8)", trustpay.functions.resolveDispute, id_, True, **{"from": accounts["arbiter"]})


def test_only_parties_can_expire_dispute(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    travel(w3, REVIEW + 10)
    for who in ("arbiter", "outsider"):
        expect_revert("NotAuthorized()", trustpay.functions.expireDispute, id_, **{"from": accounts[who]})


def test_expire_requires_dispute(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    travel(w3, REVIEW + 10)
    expect_revert("InvalidStatus(uint8)", trustpay.functions.expireDispute, id_, **{"from": accounts["buyer"]})


def test_late_arbiter_decision_allowed_until_expired(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    travel(w3, REVIEW + 10)
    trustpay.functions.resolveDispute(id_, True).transact({"from": accounts["arbiter"]})
    assert status(trustpay, id_) == RELEASED
    expect_revert("InvalidStatus(uint8)", trustpay.functions.expireDispute, id_, **{"from": accounts["buyer"]})


def test_struct_fits_three_slots(w3, trustpay, accounts):
    """Gas design check: fixed-size fields are packed into slots 0-2; title starts at slot 3."""
    id_, _ = create(w3, trustpay, accounts, title="abc")
    # mapping _escrows is storage slot 4 -> base = keccak(id . 4)
    base = int.from_bytes(Web3.solidity_keccak(["uint256", "uint256"], [id_, 4]), "big")
    title_slot = w3.eth.get_storage_at(trustpay.address, base + 3)
    # short strings (<32 bytes) store data + length*2 in the same slot
    assert title_slot[:3] == b"abc" and title_slot[-1] == 6


def test_buyer_cannot_dispute_after_review_period(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    travel(w3, REVIEW + 10)
    expect_revert("ReviewPeriodOver(uint64)", trustpay.functions.raiseDispute, id_, **{"from": accounts["buyer"]})
    # the seller's claim is therefore safe
    trustpay.functions.claimPayment(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == RELEASED


def test_buyer_can_dispute_within_review_period(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    travel(w3, REVIEW - 60)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == DISPUTED


def test_seller_can_still_dispute_after_review_period(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts)
    trustpay.functions.markDelivered(id_).transact({"from": accounts["seller"]})
    travel(w3, REVIEW + 10)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["seller"]})
    assert status(trustpay, id_) == DISPUTED


def test_buyer_dispute_before_delivery_has_no_time_limit(w3, trustpay, accounts):
    id_, _ = create(w3, trustpay, accounts, deadline_offset=10 * DAY)
    travel(w3, 9 * DAY)
    trustpay.functions.raiseDispute(id_).transact({"from": accounts["buyer"]})
    assert status(trustpay, id_) == DISPUTED
