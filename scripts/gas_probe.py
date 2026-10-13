"""Gas probe: how much does writing ONE new storage slot cost?

Explains why `createEscrow` uses ~861k gas on Sepolia but ~209k on the local test EVM.

Method: estimate the gas of three tiny contract-creation transactions that differ only in
how many new storage slots their init code writes. No compiler is involved - the probes
are raw EVM bytecode:

    0 slots  0x600160005050          PUSH1 1  PUSH1 0  POP  POP      (no store)
    1 slot   0x600160005500          PUSH1 1  PUSH1 0  SSTORE  STOP  (store 1 -> slot 0)
    2 slots  0x6001600055600160015500  ... SSTORE to slot 0, then to slot 1, STOP

The 0-slot and 1-slot probes have the same length (6 bytes), so their difference is the
cost of one new storage slot; the 2-slot probe checks that the cost is linear.

Usage (from the project root):
    python scripts/gas_probe.py                 # Sepolia, via SEPOLIA_RPC_URL from config.py
    python scripts/gas_probe.py --rpc <url>     # any JSON-RPC endpoint
    python scripts/gas_probe.py --local         # local in-memory EVM (eth-tester / py-evm),
                                                #   the same EVM the contract tests use
Results are printed and written to docs/gas_probe_results.json (one entry per network).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBES = {
    "0_slots": "0x600160005050",
    "1_slot": "0x600160005500",
    "2_slots": "0x6001600055600160015500",
}
# any address works for an estimate (no value is sent); a fixed one keeps runs comparable
FROM = "0x0000000000000000000000000000000000000001"
RESULTS = ROOT / "docs" / "gas_probe_results.json"


def probe_rpc(url):
    from chain import http_rpc
    rpc = http_rpc(url, timeout=20)
    chain_id = int(rpc("eth_chainId", []), 16)
    block = rpc("eth_blockNumber", [])                       # pin every estimate to one block
    est = {k: int(rpc("eth_estimateGas", [{"from": FROM, "data": v}, block]), 16) for k, v in PROBES.items()}
    return {"network": f"chain {chain_id}", "endpoint": url, "block": int(block, 16), "from": FROM, "estimates": est}


def probe_local():
    """Local EVM: send the probes as real transactions and read gasUsed from the receipts
    (exact), and also report eth_estimateGas for comparison."""
    from web3 import EthereumTesterProvider, Web3
    w3 = Web3(EthereumTesterProvider())
    acct = w3.eth.accounts[0]
    used = {}
    for k, v in PROBES.items():
        tx = w3.eth.send_transaction({"from": acct, "data": v, "gas": 1_000_000})
        used[k] = w3.eth.wait_for_transaction_receipt(tx).gasUsed
    est = {k: w3.eth.estimate_gas({"from": acct, "data": v}) for k, v in PROBES.items()}
    return {"network": "local eth-tester / py-evm (the EVM used by tests/test_contract.py)",
            "endpoint": "in-memory", "block": w3.eth.block_number, "from": acct,
            "gas_used": used, "estimates": est}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rpc", help="JSON-RPC URL (default: SEPOLIA_RPC_URL from config.py)")
    ap.add_argument("--local", action="store_true", help="probe the local in-memory EVM instead")
    args = ap.parse_args()

    if args.local:
        result = probe_local()
    else:
        import config
        result = probe_rpc(args.rpc or config.SEPOLIA_RPC_URL)
    e = result.get("gas_used") or result["estimates"]      # exact receipts when available
    result["per_new_slot"] = e["1_slot"] - e["0_slots"]
    result["second_slot"] = e["2_slots"] - e["1_slot"]
    result["measured_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result["probes"] = PROBES

    print(json.dumps(result, indent=2))
    data = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    data[result["network"]] = result
    RESULTS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"saved to {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
