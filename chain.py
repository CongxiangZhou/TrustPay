"""On-chain verification of TrustPay transactions (no third-party libraries).

The browser tells the backend "I sent transaction X". Instead of trusting that,
the backend asks a public Sepolia JSON-RPC node for the transaction and its
receipt and checks:

  1. the transaction was signed by the wallet that claims it (tx.from),
  2. it succeeded (receipt status 1) - or failed (status 0),
  3. the TrustPay contract itself emitted the event that the claimed action
     produces (e.g. FundsReleased for releaseFunds).

Check 3 deliberately looks at the contract's EVENTS rather than the top-level
`to` address: modern wallets (e.g. MetaMask smart accounts) may route a call
through a delegation/batching contract, so `tx.to` is not always TrustPay - but
an event can only be emitted by TrustPay's own code, which makes it the stronger
proof.

The escrow id is read from the event's first indexed topic and the amount from the
event data - neither is taken from the client. The address in the event's second topic
(buyer / seller / arbiter) must also fit the role that performs the claimed action.
"""
import json
import urllib.error
import urllib.request

# keccak256(signature) of every TrustPay event (v2 ABI)
EVENT_TOPICS = {
    "EscrowCreated": "0x1929c1524f26404f5a40813b9548017178632ae3025bde12fd9b509a8c4bcdb3",
    "DeliveryMarked": "0xab5777bc8b6396a32092aa0fe216d466bac291952cecd3a1343de17296a30f1c",
    "FundsReleased": "0x6e3c6096795c8298a218b2cfb8bde42726ff7c9a3d27b4d3ba41ab7f74feb5fb",
    "BuyerRefunded": "0xfe66ed18307ecad9b13ed2fa3b1b0abe9ca6244b20fc18d4bda1b30adccf7b7e",
    "DisputeRaised": "0x84a477df8a28a4276ca6dee4458a06c3015f30c477d9c949ede4e13ff8a552b4",
    "DisputeResolved": "0x53cddb6a5f925a3aa8339a277f6586fa68ecf2f6a1b1bcddc4d2c5f3eb7f3757",
    "PaymentClaimed": "0xdd74eb9cba06f514be98342b0ed0290146aee9feaf27e8a7ddcb151bb43dac86",
    "DisputeExpired": "0x08f688403835364b3322ffc96429f5dd41b01bf4d500dcc681e1ea7035a4e67d",
}
ESCROW_CREATED_TOPIC = EVENT_TOPICS["EscrowCreated"]

# The event each state-changing function emits
ACTION_EVENT = {
    "createEscrow": "EscrowCreated",
    "markDelivered": "DeliveryMarked",
    "releaseFunds": "FundsReleased",
    "refundBuyer": "BuyerRefunded",
    "claimRefund": "BuyerRefunded",
    "raiseDispute": "DisputeRaised",
    "resolveDispute": "DisputeResolved",
    "claimPayment": "PaymentClaimed",
    "expireDispute": "DisputeExpired",
}

# Who must have signed each action, checked against the address in the event's second
# topic (seller / buyer / raisedBy / arbiter). "same": the signer IS that address;
# "other": the signer must NOT be that address. This separates actions that share an
# event: claimRefund (signed by the buyer) and refundBuyer (signed by the seller) both
# emit BuyerRefunded(id, buyer, amount). expireDispute may be sent by buyer or seller.
SIGNER_RULE = {
    "markDelivered": "same",    # DeliveryMarked(id, seller)        - seller signs
    "releaseFunds": "other",    # FundsReleased(id, seller, amount) - buyer signs
    "claimPayment": "same",     # PaymentClaimed(id, seller, amount)
    "refundBuyer": "other",     # BuyerRefunded(id, buyer, amount)  - seller signs
    "claimRefund": "same",      # BuyerRefunded(id, buyer, amount)  - buyer signs
    "raiseDispute": "same",     # DisputeRaised(id, raisedBy)
    "resolveDispute": "same",   # DisputeResolved(id, arbiter, paidToSeller, amount)
}

# Position (32-byte word) of the escrow amount in each event's data, if it has one.
AMOUNT_WORD = {
    "EscrowCreated": 1,         # word 0 = arbiter
    "FundsReleased": 0,
    "BuyerRefunded": 0,
    "PaymentClaimed": 0,
    "DisputeExpired": 0,
    "DisputeResolved": 1,       # word 0 = paidToSeller
}

NOT_FOUND = "not_found"   # the node has never seen this hash
PENDING = "pending"       # known to the node but not mined yet


class RPCUnavailable(Exception):
    """The RPC node could not be reached or returned an error."""


class VerificationError(Exception):
    """The transaction exists but does not match what the client claimed."""


def http_rpc(url, timeout=6):
    """Return a function rpc(method, params) that calls a JSON-RPC endpoint."""

    def call(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                               "User-Agent": "TrustPay/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RPCUnavailable(str(exc)) from exc
        if "error" in data:
            raise RPCUnavailable(str(data["error"]))
        return data.get("result")

    return call


def _lower(x):
    return x.lower() if isinstance(x, str) else x


def _topic_address(topic):
    return "0x" + topic[-40:].lower()


def check_transaction(rpc, tx_hash, from_address):
    """Check a submitted (possibly still pending) transaction.

    Returns the transaction dict, or None if the node does not know it yet.
    Raises VerificationError if it was not signed by `from_address`.
    """
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    if tx is None:
        return None
    if _lower(tx.get("from")) != _lower(from_address):
        raise VerificationError("Transaction was not sent by this wallet")
    return tx


def verify_receipt(rpc, tx_hash, contract_address, from_address, action):
    """Return the verified final state of a transaction.

    Returns NOT_FOUND or PENDING (strings) if there is no final state yet, else
    {"status": "confirmed"|"failed", "block_number", "gas_used", "escrow_id",
     "amount_wei", "error_message"}.
    """
    tx = check_transaction(rpc, tx_hash, from_address)
    if tx is None:
        return NOT_FOUND
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return PENDING

    result = {
        "block_number": int(receipt["blockNumber"], 16),
        "gas_used": int(receipt["gasUsed"], 16),
        "escrow_id": None,
        "amount_wei": None,
    }
    if int(receipt.get("status", "0x0"), 16) != 1:
        return {**result, "status": "failed", "error_message": "Transaction reverted on-chain"}

    wanted = EVENT_TOPICS[ACTION_EVENT[action]]
    event = next((log for log in receipt.get("logs", [])
                  if _lower(log.get("address")) == _lower(contract_address)
                  and (log.get("topics") or [""])[0].lower() == wanted), None)
    if event is None:
        raise VerificationError(f"Transaction did not perform {action} on the TrustPay contract")

    topics = event["topics"]
    result["escrow_id"] = int(topics[1], 16)
    party = _topic_address(topics[2])
    if action == "createEscrow" and party != _lower(from_address):
        raise VerificationError("Escrow buyer does not match this wallet")
    rule = SIGNER_RULE.get(action)
    if (rule == "same" and party != _lower(from_address)) or (rule == "other" and party == _lower(from_address)):
        raise VerificationError(f"This wallet's role does not match {action}")

    # amount always comes from the event, never from the client (None if the event has none)
    word = AMOUNT_WORD.get(ACTION_EVENT[action])
    data = (event.get("data") or "0x")[2:]
    if word is not None and len(data) >= 64 * (word + 1):
        result["amount_wei"] = str(int(data[64 * word:64 * (word + 1)], 16))
    return {**result, "status": "confirmed", "error_message": None}
