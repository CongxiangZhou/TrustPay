"""SQLite persistence for the off-chain transaction log.

Only public data is stored: transaction hashes, wallet addresses, escrow ids,
amounts and status. No names, e-mails or private keys.
"""
import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash       TEXT    NOT NULL UNIQUE,
    action        TEXT    NOT NULL,
    escrow_id     INTEGER,
    from_address  TEXT    NOT NULL,
    amount_wei    TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending',
    block_number  INTEGER,
    gas_used      INTEGER,
    error_message TEXT,
    verified      INTEGER NOT NULL DEFAULT 0,   -- 1 = status read from the Sepolia receipt by the server
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_from   ON transactions(from_address);
CREATE INDEX IF NOT EXISTS idx_tx_escrow ON transactions(escrow_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app.config["DATABASE"])
    try:
        with conn:   # commits the schema/migration; closing is done below
            conn.executescript(SCHEMA)
            # migrate databases created before on-chain verification existed
            cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
            if "verified" not in cols:
                conn.execute("ALTER TABLE transactions ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    finally:
        conn.close()
    app.teardown_appcontext(close_db)
