"""TrustPay - Escrow Payment DApp (Flask backend).

The smart contract on Sepolia is the source of truth for escrow state and
funds. This backend:
  * serves the web interface,
  * exposes the deployment configuration (contract address, chain id) to the
    frontend,
  * keeps an off-chain log of every transaction submitted through the UI
    (hash, action, status) in SQLite so users get a fast, searchable history,
  * VERIFIES every logged transaction against the Sepolia blockchain (see
    chain.py): a transaction is only marked "confirmed" after the server itself
    has fetched the receipt and checked it was a successful call to the
    TrustPay contract from the claimed wallet. The browser cannot fake it.
"""
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, render_template, request

import config
from chain import (NOT_FOUND, PENDING, RPCUnavailable, VerificationError, check_transaction, http_rpc,
                   verify_receipt)
from database import get_db, init_db

# ---- validation rules -----------------------------------------------------
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
UINT_RE = re.compile(r"^[0-9]{1,78}$")  # uint256 max has 78 digits
ACTIONS = {
    "createEscrow",
    "markDelivered",
    "releaseFunds",
    "refundBuyer",
    "claimRefund",
    "raiseDispute",
    "resolveDispute",
    "claimPayment",
    "expireDispute",
}
STATUSES = {"pending", "confirmed", "failed"}
MAX_ESCROW_ID = 2**63 - 1  # SQLite INTEGER limit


class ValidationError(Exception):
    pass


def _require_address(value, field):
    if not isinstance(value, str) or not ADDRESS_RE.match(value):
        raise ValidationError(f"'{field}' must be a 0x-prefixed 20-byte hex address")
    return value.lower()


def _optional_uint(value, field, maximum=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not UINT_RE.match(str(value)):
        raise ValidationError(f"'{field}' must be a non-negative integer")
    if maximum is not None and int(value) > maximum:
        raise ValidationError(f"'{field}' is too large")
    return int(value) if maximum is not None else str(value)


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


def _row_to_dict(row):
    d = dict(row)
    d["verified"] = bool(d.get("verified"))
    d["explorer_url"] = f"{config.EXPLORER_URL}/tx/{d['tx_hash']}"
    return d


def _get_row(tx_hash):
    return get_db().execute("SELECT * FROM transactions WHERE tx_hash = ?", (tx_hash.lower(),)).fetchone()


def _apply_result(tx_hash, result, verified):
    db = get_db()
    # A verified confirmation replaces the amount with the on-chain value (or NULL for
    # actions whose event carries no amount), so a client-supplied amount never survives.
    amount_sql = "amount_wei = ?" if verified and result["status"] == "confirmed" else "amount_wei = COALESCE(?, amount_wei)"
    db.execute(
        "UPDATE transactions SET status = ?, block_number = COALESCE(?, block_number), "
        "gas_used = COALESCE(?, gas_used), escrow_id = COALESCE(?, escrow_id), "
        f"{amount_sql}, error_message = ?, "
        "verified = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE tx_hash = ?",
        (result["status"], result.get("block_number"), result.get("gas_used"), result.get("escrow_id"),
         result.get("amount_wei"), result.get("error_message"), 1 if verified else 0, tx_hash.lower()),
    )
    db.commit()


# ---- app factory ------------------------------------------------------------

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=config.DATABASE,
        MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH,
        CONTRACT_ADDRESS=config.CONTRACT_ADDRESS,
        DEPLOY_BLOCK=config.DEPLOY_BLOCK,
        VERIFY_ONCHAIN=config.VERIFY_ONCHAIN,
        PENDING_TIMEOUT_MINUTES=config.PENDING_TIMEOUT_MINUTES,
        RPC=http_rpc(config.SEPOLIA_RPC_URL),
    )
    if test_config:
        app.config.update(test_config)
    init_db(app)

    def verify_row(row):
        """Re-check a pending row on-chain. Returns 'updated', 'pending', 'unavailable' or 'rejected'."""
        rpc = app.config["RPC"]
        try:
            result = verify_receipt(rpc, row["tx_hash"], app.config["CONTRACT_ADDRESS"],
                                    row["from_address"], row["action"])
        except RPCUnavailable:
            return "unavailable"
        except VerificationError as exc:
            _apply_result(row["tx_hash"], {"status": "failed", "error_message": f"Rejected: {exc}"}, True)
            return "rejected"
        if result == NOT_FOUND:
            # A real transaction reaches the node within seconds. If the node still has
            # never seen this hash after the timeout, it was never broadcast (or is fake).
            created = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            limit = timedelta(minutes=app.config["PENDING_TIMEOUT_MINUTES"])
            if datetime.now(timezone.utc) - created > limit:
                _apply_result(row["tx_hash"], {
                    "status": "failed",
                    "error_message": f"Not found on Sepolia after {app.config['PENDING_TIMEOUT_MINUTES']} minutes",
                }, False)
                return "expired"
            return "pending"
        if result == PENDING:
            return "pending"
        _apply_result(row["tx_hash"], result, True)
        return "updated"

    def frontend_config():
        return {
            "contractAddress": app.config["CONTRACT_ADDRESS"],
            "deployBlock": app.config["DEPLOY_BLOCK"],
            "chainId": config.CHAIN_ID,
            "chainName": config.CHAIN_NAME,
            "explorerUrl": config.EXPLORER_URL,
        }

    @app.context_processor
    def inject_config():
        return {"frontend_config": frontend_config()}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return resp

    # ---- pages ----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template("index.html", page="home")

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html", page="dashboard")

    @app.get("/create")
    def create():
        return render_template("create.html", page="create")

    @app.get("/escrow/<int:escrow_id>")
    def escrow_detail(escrow_id):
        if escrow_id > MAX_ESCROW_ID:
            abort(404)
        return render_template("escrow.html", page="escrow", escrow_id=escrow_id)

    @app.get("/history")
    def history():
        return render_template("history.html", page="history")

    # ---- API --------------------------------------------------------------
    @app.get("/api/config")
    def api_config():
        return jsonify(frontend_config())

    @app.post("/api/transactions")
    def api_record_transaction():
        """Record a transaction right after MetaMask returns its hash."""
        data = _json_body()
        tx_hash = data.get("tx_hash")
        if not isinstance(tx_hash, str) or not TX_HASH_RE.match(tx_hash):
            raise ValidationError("'tx_hash' must be a 0x-prefixed 32-byte hex hash")
        action = data.get("action")
        if action not in ACTIONS:
            raise ValidationError(f"'action' must be one of {sorted(ACTIONS)}")
        from_address = _require_address(data.get("from_address"), "from_address")
        escrow_id = _optional_uint(data.get("escrow_id"), "escrow_id", MAX_ESCROW_ID)
        amount_wei = _optional_uint(data.get("amount_wei"), "amount_wei")
        if action == "createEscrow" and (amount_wei is None or int(amount_wei) == 0):
            raise ValidationError("'amount_wei' must be greater than 0 for createEscrow")
        if action != "createEscrow" and escrow_id is None:
            raise ValidationError("'escrow_id' is required for this action")
        chain_id = data.get("chain_id", config.CHAIN_ID)
        if chain_id != config.CHAIN_ID:
            raise ValidationError(f"Only Sepolia (chain id {config.CHAIN_ID}) transactions are accepted")

        if app.config["VERIFY_ONCHAIN"]:
            # Reject hashes that exist on-chain but were signed by a different wallet.
            # A brand-new transaction may not have reached the node yet - that is allowed
            # and will be verified when its receipt is checked.
            try:
                check_transaction(app.config["RPC"], tx_hash, from_address)
            except VerificationError as exc:
                return jsonify(error=f"Transaction rejected: {exc}"), 422
            except RPCUnavailable:
                pass

        db = get_db()
        try:
            db.execute(
                "INSERT INTO transactions (tx_hash, action, escrow_id, from_address, amount_wei, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (tx_hash.lower(), action, escrow_id, from_address, amount_wei),
            )
            db.commit()
        except Exception as exc:  # sqlite3.IntegrityError - duplicate hash
            if "UNIQUE" in str(exc):
                return jsonify(error="Transaction already recorded"), 409
            raise
        row = db.execute("SELECT * FROM transactions WHERE tx_hash = ?", (tx_hash.lower(),)).fetchone()
        return jsonify(_row_to_dict(row)), 201

    @app.patch("/api/transactions/<tx_hash>")
    def api_update_transaction(tx_hash):
        """Ask the server to (re)check a transaction's final status.

        With on-chain verification enabled (default) the server ignores any
        status sent by the client and reads the receipt from Sepolia itself.
        Returns 200 with the verified record, or 202 if it is not mined yet /
        the RPC node is unreachable (status stays "pending").
        """
        if not TX_HASH_RE.match(tx_hash):
            raise ValidationError("Invalid transaction hash")
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object")
        row = _get_row(tx_hash)
        if row is None:
            return jsonify(error="Transaction not found"), 404

        if app.config["VERIFY_ONCHAIN"]:
            if row["status"] != "pending":
                return jsonify(_row_to_dict(row))           # already final
            outcome = verify_row(row)
            row = _get_row(tx_hash)
            body = _row_to_dict(row)
            if outcome == "rejected":
                return jsonify(error=row["error_message"], **body), 422
            if outcome == "expired":
                return jsonify(body)
            if outcome in ("pending", "unavailable"):
                body["verification"] = outcome
                return jsonify(body), 202
            return jsonify(body)

        # ---- fallback when verification is switched off (offline demo): trust the client
        status = data.get("status")
        if status not in STATUSES - {"pending"}:
            raise ValidationError("'status' must be 'confirmed' or 'failed'")
        block_number = _optional_uint(data.get("block_number"), "block_number", MAX_ESCROW_ID)
        gas_used = _optional_uint(data.get("gas_used"), "gas_used", MAX_ESCROW_ID)
        escrow_id = _optional_uint(data.get("escrow_id"), "escrow_id", MAX_ESCROW_ID)
        error_message = data.get("error_message")
        if error_message is not None:
            if not isinstance(error_message, str):
                raise ValidationError("'error_message' must be a string")
            error_message = error_message[:300]
        if row["status"] != "pending" and row["status"] != status:
            return jsonify(error=f"Transaction is already {row['status']}"), 409
        _apply_result(tx_hash, {"status": status, "block_number": block_number, "gas_used": gas_used,
                                "escrow_id": escrow_id, "error_message": error_message}, False)
        return jsonify(_row_to_dict(_get_row(tx_hash)))

    @app.get("/api/transactions")
    def api_list_transactions():
        """List recorded transactions, filtered by wallet address and/or escrow id."""
        clauses, params = [], []
        address = request.args.get("address")
        if address:
            clauses.append("from_address = ?")
            params.append(_require_address(address, "address"))
        escrow_id = request.args.get("escrow_id")
        if escrow_id:
            clauses.append("escrow_id = ?")
            params.append(_optional_uint(escrow_id, "escrow_id", MAX_ESCROW_ID))
        status = request.args.get("status")
        if status:
            if status not in STATUSES:
                raise ValidationError(f"'status' must be one of {sorted(STATUSES)}")
            clauses.append("status = ?")
            params.append(status)
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        except ValueError:
            raise ValidationError("'limit' must be an integer")

        sql = "SELECT * FROM transactions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = get_db().execute(sql, (*params, limit)).fetchall()
        if app.config["VERIFY_ONCHAIN"]:
            # Lazily settle pending transactions (e.g. the user closed the tab before it was mined).
            checked = 0
            for r in rows:
                if r["status"] == "pending" and checked < 5:
                    checked += 1
                    if verify_row(r) == "unavailable":
                        break  # node down - don't slow the page further
            if checked:
                rows = get_db().execute(sql, (*params, limit)).fetchall()
        return jsonify([_row_to_dict(r) for r in rows])

    # ---- errors -----------------------------------------------------------
    @app.errorhandler(ValidationError)
    def handle_validation(err):
        return jsonify(error=str(err)), 400

    @app.errorhandler(404)
    def handle_404(_err):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(413)
    def handle_413(_err):
        return jsonify(error="Request body too large"), 413

    @app.errorhandler(405)
    def handle_405(_err):
        return jsonify(error="Method not allowed"), 405

    @app.errorhandler(500)
    def handle_500(_err):
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error"), 500
        return render_template("error.html", code=500, message="Something went wrong"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
