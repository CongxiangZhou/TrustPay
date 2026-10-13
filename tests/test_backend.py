"""Tests for the Flask backend: pages, transaction API and server-side validation.

Run:  pytest -v tests/test_backend.py
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app

ADDR = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
HASH = "0x" + "12" * 32
HASH2 = "0x" + "34" * 32


@pytest.fixture
def client(tmp_path):
    """Client with on-chain verification OFF - tests the API/validation layer on its own."""
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"),
                      "CONTRACT_ADDRESS": "0x" + "ee" * 20, "VERIFY_ONCHAIN": False})
    return app.test_client()


def record(client, **overrides):
    body = {"tx_hash": HASH, "action": "createEscrow", "from_address": ADDR, "amount_wei": "1000000000000000"}
    body.update(overrides)
    return client.post("/api/transactions", json=body)


# ---------------------------------------------------------------- pages

@pytest.mark.parametrize("path", ["/", "/dashboard", "/create", "/escrow/0", "/history"])
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert b"TrustPay" in resp.data
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_unknown_page_404(client):
    assert client.get("/nope").status_code == 404
    assert client.get("/escrow/abc").status_code == 404


def test_config_endpoint(client):
    data = client.get("/api/config").get_json()
    assert data["chainId"] == 11155111
    assert data["contractAddress"] == "0x" + "ee" * 20


# ---------------------------------------------------------------- record + update flow

def test_record_then_confirm(client):
    resp = record(client)
    assert resp.status_code == 201
    tx = resp.get_json()
    assert tx["status"] == "pending" and tx["from_address"] == ADDR
    assert tx["explorer_url"].endswith(HASH)

    resp = client.patch(f"/api/transactions/{HASH}",
                        json={"status": "confirmed", "block_number": 123, "gas_used": 208772, "escrow_id": 7})
    assert resp.status_code == 200
    tx = resp.get_json()
    assert (tx["status"], tx["block_number"], tx["gas_used"], tx["escrow_id"]) == ("confirmed", 123, 208772, 7)


def test_record_failed_with_message(client):
    record(client)
    resp = client.patch(f"/api/transactions/{HASH}", json={"status": "failed", "error_message": "reverted"})
    assert resp.get_json()["status"] == "failed"


def test_duplicate_hash_rejected(client):
    record(client)
    assert record(client).status_code == 409


def test_final_status_cannot_change(client):
    record(client)
    client.patch(f"/api/transactions/{HASH}", json={"status": "confirmed"})
    assert client.patch(f"/api/transactions/{HASH}", json={"status": "failed"}).status_code == 409


def test_update_unknown_tx(client):
    assert client.patch(f"/api/transactions/{HASH2}", json={"status": "confirmed"}).status_code == 404


def test_list_filters(client):
    record(client)
    record(client, tx_hash=HASH2, action="releaseFunds", escrow_id=3, from_address=OTHER, amount_wei=None)
    assert len(client.get("/api/transactions").get_json()) == 2
    mine = client.get(f"/api/transactions?address={ADDR}").get_json()
    assert [t["tx_hash"] for t in mine] == [HASH]
    by_escrow = client.get("/api/transactions?escrow_id=3").get_json()
    assert [t["action"] for t in by_escrow] == ["releaseFunds"]
    # address matching is case-insensitive
    assert len(client.get(f"/api/transactions?address={ADDR.upper().replace('0X', '0x')}").get_json()) == 1


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"tx_hash": "0x123"}, "tx_hash"),
        ({"tx_hash": "nothex" * 11}, "tx_hash"),
        ({"action": "stealFunds"}, "action"),
        ({"from_address": "0x123"}, "from_address"),
        ({"from_address": "0x" + "zz" * 20}, "from_address"),
        ({"amount_wei": "-5"}, "amount_wei"),
        ({"amount_wei": "1.5"}, "amount_wei"),
        ({"amount_wei": "0"}, "greater than 0"),
        ({"escrow_id": -1}, "escrow_id"),
        ({"escrow_id": True}, "escrow_id"),
        ({"action": "releaseFunds", "escrow_id": None}, "required"),
        ({"chain_id": 1}, "Sepolia"),
    ],
)
def test_record_validation(client, overrides, fragment):
    resp = record(client, **overrides)
    assert resp.status_code == 400
    assert fragment in resp.get_json()["error"]


def test_non_json_body_rejected(client):
    resp = client.post("/api/transactions", data="hello", content_type="text/plain")
    assert resp.status_code == 400


def test_body_too_large(client):
    resp = client.post("/api/transactions", data="x" * 20000, content_type="application/json")
    assert resp.status_code == 413


def test_update_validation(client):
    record(client)
    assert client.patch(f"/api/transactions/{HASH}", json={"status": "pending"}).status_code == 400
    assert client.patch(f"/api/transactions/{HASH}", json={"status": "confirmed", "gas_used": "abc"}).status_code == 400
    assert client.patch("/api/transactions/0xbad", json={"status": "confirmed"}).status_code == 400


def test_list_validation(client):
    assert client.get("/api/transactions?address=bob").status_code == 400
    assert client.get("/api/transactions?status=lost").status_code == 400
    assert client.get("/api/transactions?limit=ten").status_code == 400


def test_sql_injection_is_harmless(client):
    record(client)
    resp = client.get("/api/transactions?address=0x' OR '1'='1")
    assert resp.status_code == 400
    assert len(client.get("/api/transactions").get_json()) == 1


# ---------------------------------------------------------------- on-chain verification (simulated Sepolia node)

from chain import ACTION_EVENT, EVENT_TOPICS, RPCUnavailable  # noqa: E402

CONTRACT = "0x" + "ee" * 20
DELEGATION_MANAGER = "0x" + "db" * 20
COUNTERPARTY = "0x" + "77" * 20


def _word(n):
    return format(n, "064x")


class FakeChain:
    """Minimal stand-in for a Sepolia JSON-RPC node."""

    def __init__(self):
        self.txs, self.receipts, self.down = {}, {}, False

    def add(self, tx_hash, action="createEscrow", frm=ADDR, to=CONTRACT, escrow_arg=3,
            mined=True, ok=True, created_id=5, emitter=CONTRACT, event_action=None, value=10**15,
            party=None, amount=10**16):
        """`to` is the top-level recipient (may be a wallet's delegation contract);
        `emitter` is the contract that emits the event (TrustPay unless faked);
        `party` is the address in the event's second topic (by default the signer, or the
        counterparty for actions signed by the other side: releaseFunds, refundBuyer);
        `amount` is the escrow amount carried in the event data."""
        self.txs[tx_hash] = {"hash": tx_hash, "from": frm, "to": to, "input": "0xdeadbeef",
                             "value": hex(value if action == "createEscrow" else 0)}
        if mined:
            logs = []
            ev = ACTION_EVENT[event_action or action]
            if ok and ev:
                escrow_id = created_id if (event_action or action) == "createEscrow" else escrow_arg
                kind = event_action or action
                if party is None:
                    party = COUNTERPARTY if kind in ("releaseFunds", "refundBuyer") else frm
                topics = [EVENT_TOPICS[ev], "0x" + _word(escrow_id), "0x" + "00" * 12 + party[2:]]
                data = {"EscrowCreated": _word(0xabc) + _word(value),
                        "DisputeResolved": _word(1) + _word(amount),
                        "FundsReleased": _word(amount), "BuyerRefunded": _word(amount),
                        "PaymentClaimed": _word(amount), "DisputeExpired": _word(amount)}.get(ev, "")
                data = "0x" + data
                logs = [{"address": emitter, "topics": topics, "data": data}]
            self.receipts[tx_hash] = {"status": "0x1" if ok else "0x0", "blockNumber": hex(9_000_000),
                                      "gasUsed": hex(209_282), "logs": logs}

    def __call__(self, method, params):
        if self.down:
            raise RPCUnavailable("connection refused")
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        raise AssertionError(method)


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def vclient(tmp_path, chain):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "v.db"), "CONTRACT_ADDRESS": CONTRACT,
                      "VERIFY_ONCHAIN": True, "RPC": chain})
    return app.test_client()


def test_verified_confirmation_uses_chain_data(vclient, chain):
    chain.add(HASH, created_id=5)
    assert record(vclient).status_code == 201
    # client lies about everything - server must ignore it
    resp = vclient.patch(f"/api/transactions/{HASH}", json={"status": "failed", "block_number": 1, "escrow_id": 99})
    tx = resp.get_json()
    assert resp.status_code == 200
    assert (tx["status"], tx["verified"], tx["block_number"], tx["gas_used"], tx["escrow_id"]) == \
        ("confirmed", True, 9_000_000, 209_282, 5)


def test_escrow_id_taken_from_calldata(vclient, chain):
    chain.add(HASH, action="releaseFunds", escrow_arg=7)
    record(vclient, action="releaseFunds", escrow_id=1, amount_wei=None)
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["escrow_id"] == 7                 # chain value overrides what the client sent
    assert tx["status"] == "confirmed" and tx["verified"] is True


def test_reverted_transaction_marked_failed(vclient, chain):
    chain.add(HASH, ok=False)
    record(vclient)
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["status"] == "failed" and tx["verified"] is True
    assert "reverted" in tx["error_message"]


def test_not_yet_mined_stays_pending(vclient, chain):
    chain.add(HASH, mined=False)
    record(vclient)
    resp = vclient.patch(f"/api/transactions/{HASH}", json={"status": "confirmed"})
    assert resp.status_code == 202
    assert resp.get_json()["status"] == "pending" and resp.get_json()["verified"] is False
    # mined later -> the history list settles it automatically
    chain.add(HASH)
    rows = vclient.get(f"/api/transactions?address={ADDR}").get_json()
    assert rows[0]["status"] == "confirmed" and rows[0]["verified"] is True


def test_unknown_hash_cannot_be_confirmed(vclient, chain):
    # fake hash that does not exist on-chain: accepted as pending, but never confirmed
    assert record(vclient).status_code == 201
    resp = vclient.patch(f"/api/transactions/{HASH}", json={"status": "confirmed"})
    assert resp.status_code == 202 and resp.get_json()["status"] == "pending"


def test_wrong_signer_rejected_on_record(vclient, chain):
    chain.add(HASH, frm=OTHER)
    resp = record(vclient)
    assert resp.status_code == 422
    assert "not sent by this wallet" in resp.get_json()["error"]
    assert vclient.get("/api/transactions").get_json() == []


def test_delegated_transaction_is_accepted(vclient, chain):
    """MetaMask smart accounts route calls through a DelegationManager: tx.to is not
    TrustPay, but TrustPay still emits the event - that must count as verified."""
    chain.add(HASH, action="releaseFunds", to=DELEGATION_MANAGER, escrow_arg=4)
    assert record(vclient, action="releaseFunds", escrow_id=4, amount_wei=None).status_code == 201
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert (tx["status"], tx["verified"], tx["escrow_id"]) == ("confirmed", True, 4)


@pytest.mark.parametrize("kwargs, fragment", [
    ({"emitter": "0x" + "11" * 20}, "did not perform createEscrow"),          # event from another contract
    ({"event_action": "releaseFunds"}, "did not perform createEscrow"),       # different TrustPay action
])
def test_transaction_without_matching_event_rejected(vclient, chain, kwargs, fragment):
    chain.add(HASH, **kwargs)
    assert record(vclient).status_code == 201
    resp = vclient.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 422
    assert fragment in resp.get_json()["error"]
    assert resp.get_json()["status"] == "failed"


def test_mismatch_discovered_later_is_rejected(vclient, chain):
    record(vclient)                      # node did not know the tx yet -> accepted as pending
    chain.add(HASH, frm=OTHER)           # it turns out to be someone else's transaction
    resp = vclient.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 422
    assert resp.get_json()["status"] == "failed"


def test_rpc_down_keeps_pending(vclient, chain):
    chain.add(HASH)
    chain.down = True
    assert record(vclient).status_code == 201       # recording still works offline
    resp = vclient.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 202 and resp.get_json()["verification"] == "unavailable"
    chain.down = False
    assert vclient.patch(f"/api/transactions/{HASH}").get_json()["status"] == "confirmed"


def test_old_database_is_migrated(tmp_path, chain):
    import sqlite3
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, tx_hash TEXT UNIQUE NOT NULL, "
                     "action TEXT NOT NULL, escrow_id INTEGER, from_address TEXT NOT NULL, amount_wei TEXT, "
                     "status TEXT NOT NULL DEFAULT 'pending', block_number INTEGER, gas_used INTEGER, "
                     "error_message TEXT, created_at TEXT, updated_at TEXT)")
    create_app({"TESTING": True, "DATABASE": str(db), "RPC": chain})
    with closing(sqlite3.connect(db)) as conn, conn:
        assert "verified" in {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}


def test_amount_is_taken_from_chain(vclient, chain):
    chain.add(HASH)                                   # on-chain value = 0.001 ETH (10**15 wei)
    record(vclient, amount_wei="999000000000000000000")  # client claims 999 ETH
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["verified"] is True
    assert tx["amount_wei"] == str(10**15)


def test_client_amount_never_survives_for_other_actions(vclient, chain):
    # release: the event carries the real amount (0.01 ETH); the client claims 999 ETH
    chain.add(HASH, action="releaseFunds", amount=10**16)
    record(vclient, action="releaseFunds", escrow_id=3, amount_wei="999000000000000000000")
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["verified"] is True and tx["amount_wei"] == str(10**16)
    # markDelivered: the event has no amount, so a client-supplied amount is dropped
    chain.add(HASH2, action="markDelivered")
    record(vclient, tx_hash=HASH2, action="markDelivered", escrow_id=3, amount_wei="999000000000000000000")
    tx = vclient.patch(f"/api/transactions/{HASH2}").get_json()
    assert tx["verified"] is True and tx["amount_wei"] is None


@pytest.mark.parametrize("action, amount_word", [("claimPayment", 0), ("expireDispute", 0), ("resolveDispute", 1)])
def test_amount_read_from_each_event(vclient, chain, action, amount_word):
    chain.add(HASH, action=action, amount=7 * 10**15)
    record(vclient, action=action, escrow_id=3, amount_wei=None)
    tx = vclient.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["verified"] is True and tx["amount_wei"] == str(7 * 10**15)


def test_refund_labels_cannot_be_swapped(vclient, chain):
    # the seller's refundBuyer emits BuyerRefunded(id, buyer=COUNTERPARTY); logging it as the
    # buyer's claimRefund must be rejected, and the correct label accepted
    chain.add(HASH, action="refundBuyer", party=COUNTERPARTY)
    record(vclient, action="claimRefund", escrow_id=3, amount_wei=None)
    resp = vclient.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 422 and "role does not match claimRefund" in resp.get_json()["error"]

    chain.add(HASH2, action="refundBuyer", party=COUNTERPARTY)
    record(vclient, tx_hash=HASH2, action="refundBuyer", escrow_id=3, amount_wei=None)
    assert vclient.patch(f"/api/transactions/{HASH2}").get_json()["verified"] is True


def test_buyer_refund_cannot_be_logged_as_seller_refund(vclient, chain):
    # the buyer's own claimRefund (event buyer == signer) labelled refundBuyer -> rejected
    chain.add(HASH, action="claimRefund")
    record(vclient, action="refundBuyer", escrow_id=3, amount_wei=None)
    resp = vclient.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 422 and "role does not match refundBuyer" in resp.get_json()["error"]


def _age_row(db_path, minutes):
    import sqlite3
    with closing(sqlite3.connect(db_path)) as conn, conn:
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE transactions SET created_at = ?", (old,))


def test_unknown_pending_expires_after_timeout(tmp_path, chain):
    db = tmp_path / "e.db"
    app = create_app({"TESTING": True, "DATABASE": str(db), "CONTRACT_ADDRESS": CONTRACT, "RPC": chain})
    c = app.test_client()
    record(c)                                          # hash the node has never seen
    assert c.patch(f"/api/transactions/{HASH}").status_code == 202   # still young -> pending
    _age_row(db, 31)
    tx = c.patch(f"/api/transactions/{HASH}").get_json()
    assert tx["status"] == "failed" and tx["verified"] is False
    assert "Not found on Sepolia" in tx["error_message"]


def test_known_but_unmined_tx_does_not_expire(tmp_path, chain):
    db = tmp_path / "k.db"
    app = create_app({"TESTING": True, "DATABASE": str(db), "CONTRACT_ADDRESS": CONTRACT, "RPC": chain})
    c = app.test_client()
    chain.add(HASH, mined=False)                       # in the mempool, just slow
    record(c)
    _age_row(db, 120)
    resp = c.patch(f"/api/transactions/{HASH}")
    assert resp.status_code == 202 and resp.get_json()["status"] == "pending"


def test_history_list_expires_junk(tmp_path, chain):
    db = tmp_path / "j.db"
    app = create_app({"TESTING": True, "DATABASE": str(db), "CONTRACT_ADDRESS": CONTRACT, "RPC": chain})
    c = app.test_client()
    record(c)
    _age_row(db, 45)
    rows = c.get(f"/api/transactions?address={ADDR}").get_json()
    assert rows[0]["status"] == "failed"
