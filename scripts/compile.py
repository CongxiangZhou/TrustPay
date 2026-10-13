"""Compile TrustPay.sol (and the test helper contracts) into build/*.json.

Usage:
    python scripts/compile.py

Downloads solc 0.8.31 the first time (via py-solc-x). The output ABI is the same
one Remix produces, and is also copied to static/abi/TrustPay.abi.json for the
frontend.
"""
import json
from pathlib import Path

import solcx

SOLC_VERSION = "0.8.31"  # same compiler used for the Sepolia deployment
ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "TrustPay.sol": ROOT / "contracts" / "TrustPay.sol",
    "TestHelpers.sol": ROOT / "tests" / "contracts" / "TestHelpers.sol",
}


def compile_all():
    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        print(f"Installing solc {SOLC_VERSION} ...")
        solcx.install_solc(SOLC_VERSION)

    standard_input = {
        "language": "Solidity",
        "sources": {name: {"content": path.read_text(encoding="utf-8")} for name, path in SOURCES.items()},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    out = solcx.compile_standard(standard_input, solc_version=SOLC_VERSION)

    build = {}
    for contracts in out["contracts"].values():
        for name, c in contracts.items():
            build[name] = {"abi": c["abi"], "bytecode": "0x" + c["evm"]["bytecode"]["object"]}

    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    (build_dir / "TrustPay.json").write_text(json.dumps(build["TrustPay"], indent=2))
    (build_dir / "TestHelpers.json").write_text(
        json.dumps({k: build[k] for k in ("ReentrantSeller", "RejectingReceiver")}, indent=2)
    )
    abi_path = ROOT / "static" / "abi" / "TrustPay.abi.json"
    abi_path.parent.mkdir(parents=True, exist_ok=True)
    abi_path.write_text(json.dumps(build["TrustPay"]["abi"], indent=2))
    print("Compiled TrustPay ->", build_dir)


if __name__ == "__main__":
    compile_all()
