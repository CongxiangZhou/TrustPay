import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_build():
    """Load compiled artifacts; compile with py-solc-x if they are missing."""
    tp = ROOT / "build" / "TrustPay.json"
    helpers = ROOT / "build" / "TestHelpers.json"
    if not (tp.exists() and helpers.exists()):
        from scripts.compile import compile_all

        compile_all()
    return json.loads(tp.read_text()), json.loads(helpers.read_text())


@pytest.fixture(scope="session")
def artifacts():
    return _load_build()


@pytest.fixture
def w3():
    from web3 import Web3, EthereumTesterProvider

    return Web3(EthereumTesterProvider())


@pytest.fixture
def accounts(w3):
    acc = w3.eth.accounts
    # buyer, seller, arbiter, outsider
    return {"buyer": acc[0], "seller": acc[1], "arbiter": acc[2], "outsider": acc[3]}


@pytest.fixture
def trustpay(w3, artifacts, accounts):
    tp_art, _ = artifacts
    Contract = w3.eth.contract(abi=tp_art["abi"], bytecode=tp_art["bytecode"])
    tx = Contract.constructor().transact({"from": accounts["outsider"]})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    return w3.eth.contract(address=receipt.contractAddress, abi=tp_art["abi"])
