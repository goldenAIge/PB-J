"""Set on-chain approvals for Polymarket CLOB V2 exchange contracts.

V2 requires pUSD (ERC-20) approve + CTF (ERC-1155) setApprovalForAll for:
  - exchange_v2
  - neg_risk_exchange_v2
  - neg_risk_adapter (CTF may already be set from V1)

Usage:
    # Dry run — check current approval state
    PYTHONPATH="." python3 scripts/python/set_v2_approvals.py

    # Execute — set all missing approvals on-chain
    PYTHONPATH="." python3 scripts/python/set_v2_approvals.py --execute
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.constants import MAX_INT

try:
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as ExtraDataToPOAMiddleware

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# --- Contract addresses (Polygon mainnet, chain_id 137) ---

CHAIN_ID = 137

PUSD_ADDRESS = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

EXCHANGE_V2 = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EXCHANGE_V2 = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

MAX_APPROVAL = int(MAX_INT, 0)  # 2**256 - 1

# Minimal ERC-20 ABI: approve, allowance
ERC20_ABI = [
    {"inputs": [{"internalType": "address", "name": "spender", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "owner", "type": "address"},
                {"internalType": "address", "name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# Minimal ERC-1155 ABI: setApprovalForAll, isApprovedForAll
ERC1155_ABI = [
    {"inputs": [{"internalType": "address", "name": "operator", "type": "address"},
                {"internalType": "bool", "name": "approved", "type": "bool"}],
     "name": "setApprovalForAll", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"},
                {"internalType": "address", "name": "operator", "type": "address"}],
     "name": "isApprovedForAll", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
]

# Labels for readable output
SPENDER_LABELS = {
    EXCHANGE_V2: "exchange_v2",
    NEG_RISK_EXCHANGE_V2: "neg_risk_exchange_v2",
    NEG_RISK_ADAPTER: "neg_risk_adapter",
}

MATIC_USD = 0.50


def send_tx(w3, txn, private_key, label: str) -> dict:
    """Sign, send, and wait for a transaction receipt."""
    signed = w3.eth.account.sign_transaction(txn, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"    tx sent: {tx_hash.hex()}")
    print(f"    Waiting for confirmation (60s timeout)...")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    except Exception:
        print(f"    TIMEOUT — check manually: https://polygonscan.com/tx/{tx_hash.hex()}")
        raise
    if receipt.status != 1:
        print(f"    REVERTED — tx: {tx_hash.hex()}")
        raise RuntimeError(f"{label} transaction reverted (status=0)")
    gas_cost = receipt.gasUsed * txn.get("gasPrice", w3.eth.gas_price) / 10**18
    print(f"    Confirmed block {receipt.blockNumber} | gas: {receipt.gasUsed} | cost: {gas_cost:.6f} MATIC")
    return receipt


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Set V2 CLOB on-chain approvals")
    parser.add_argument("--execute", action="store_true", help="Submit transactions (default: dry run)")
    args = parser.parse_args()

    private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
    if not private_key:
        print("ERROR: POLYGON_WALLET_PRIVATE_KEY not set in .env")
        sys.exit(1)

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to RPC {rpc_url}")
        sys.exit(1)

    wallet = w3.eth.account.from_key(private_key).address
    pusd = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
    gas_price = w3.eth.gas_price

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n{'='*60}")
    print(f"  {mode} — V2 CLOB APPROVALS")
    print(f"{'='*60}\n")
    print(f"Wallet: {wallet}")
    print(f"RPC:    {rpc_url}\n")

    # --- Pre-check: query all 6 approval states ---

    spenders = [EXCHANGE_V2, NEG_RISK_EXCHANGE_V2, NEG_RISK_ADAPTER]
    approval_checks = []  # list of (label, type, is_set, build_tx_fn)

    print("Current approval state:")
    print(f"  {'Contract':<25} {'Type':<12} {'Status':<10}")
    print(f"  {'-'*25} {'-'*12} {'-'*10}")

    for spender in spenders:
        label = SPENDER_LABELS[spender]

        # pUSD ERC-20 allowance
        allowance = pusd.functions.allowance(wallet, spender).call()
        pusd_ok = allowance > 0
        status = f"OK ({allowance})" if pusd_ok else "NOT SET"
        print(f"  {label:<25} {'pUSD approve':<12} {status}")
        if not pusd_ok:
            approval_checks.append((
                f"pUSD.approve({label})",
                "ERC-20",
                lambda s=spender: pusd.functions.approve(s, MAX_APPROVAL),
            ))

        # CTF ERC-1155 isApprovedForAll
        ctf_ok = ctf.functions.isApprovedForAll(wallet, spender).call()
        status = "OK" if ctf_ok else "NOT SET"
        print(f"  {label:<25} {'CTF approve':<12} {status}")
        if not ctf_ok:
            approval_checks.append((
                f"CTF.setApprovalForAll({label})",
                "ERC-1155",
                lambda s=spender: ctf.functions.setApprovalForAll(s, True),
            ))

    print()

    if not approval_checks:
        print("All approvals already set. Nothing to do.")
        return

    # --- Build transaction plan with gas estimates ---

    print(f"Transactions needed: {len(approval_checks)}\n")
    planned_txs = []
    total_est_gas = 0

    for i, (desc, token_type, build_fn) in enumerate(approval_checks, 1):
        print(f"  {i}. {desc}")
        try:
            est_gas = build_fn().estimate_gas({"from": wallet})
            print(f"     Est gas: {est_gas:,}")
        except Exception as e:
            print(f"     ERROR: estimateGas() failed — transaction would revert:\n       {e}")
            sys.exit(1)
        total_est_gas += est_gas
        planned_txs.append((desc, build_fn, est_gas))

    est_cost_matic = total_est_gas * gas_price / 10**18
    est_cost_usd = est_cost_matic * MATIC_USD
    print(f"\nTotal estimated gas: ~{total_est_gas:,} ({est_cost_matic:.6f} MATIC ≈ ${est_cost_usd:.4f})")

    if not args.execute:
        print(f"\n{'='*60}")
        print(f"  DRY RUN — NO TRANSACTIONS SENT")
        print(f"{'='*60}\n")
        return

    # --- Confirmation ---

    print(f"\n⚠️  This will send {len(planned_txs)} transaction(s) on Polygon mainnet.")
    confirm = input("Type APPROVE to proceed: ")
    if confirm.strip() != "APPROVE":
        print("Aborted.")
        sys.exit(0)

    # --- Execute transactions sequentially ---

    print()
    nonce = w3.eth.get_transaction_count(wallet)
    total_gas_used = 0

    for i, (desc, build_fn, est_gas) in enumerate(planned_txs, 1):
        print(f"  [{i}/{len(planned_txs)}] {desc}")
        txn = build_fn().build_transaction({
            "chainId": CHAIN_ID,
            "from": wallet,
            "nonce": nonce,
            "gasPrice": gas_price,
        })
        receipt = send_tx(w3, txn, private_key, desc)
        total_gas_used += receipt.gasUsed
        nonce += 1
        print()

    total_cost = total_gas_used * gas_price / 10**18
    print(f"Total gas used: {total_gas_used:,} ({total_cost:.6f} MATIC ≈ ${total_cost * MATIC_USD:.4f})")

    # --- Post-check: re-query all 6 approvals ---

    print(f"\n--- Final approval state ---")
    print(f"  {'Contract':<25} {'Type':<12} {'Status':<10}")
    print(f"  {'-'*25} {'-'*12} {'-'*10}")

    all_ok = True
    for spender in spenders:
        label = SPENDER_LABELS[spender]

        allowance = pusd.functions.allowance(wallet, spender).call()
        pusd_ok = allowance > 0
        print(f"  {label:<25} {'pUSD approve':<12} {'OK' if pusd_ok else 'FAILED'}")
        if not pusd_ok:
            all_ok = False

        ctf_ok = ctf.functions.isApprovedForAll(wallet, spender).call()
        print(f"  {label:<25} {'CTF approve':<12} {'OK' if ctf_ok else 'FAILED'}")
        if not ctf_ok:
            all_ok = False

    if all_ok:
        print(f"\n{'='*60}")
        print(f"  ALL V2 APPROVALS SET SUCCESSFULLY")
        print(f"{'='*60}\n")
    else:
        print(f"\n⚠️  Some approvals failed — check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
