"""Wrap USDC.e → pUSD via Polymarket CollateralOnramp for V2 CLOB migration.

Usage:
    # Dry run (no transactions)
    PYTHONPATH="." python3 scripts/python/wrap_usdc_to_pusd.py --amount 10.0

    # Execute for real
    PYTHONPATH="." python3 scripts/python/wrap_usdc_to_pusd.py --amount 10.0 --execute
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

try:
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as ExtraDataToPOAMiddleware

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# --- Constants ---

CHAIN_ID = 137
DECIMALS = 6  # Both USDC.e and pUSD use 6 decimals

USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PUSD_ADDRESS = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
ONRAMP_ADDRESS = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")

# Minimal ERC-20 ABI: balanceOf, approve, allowance
ERC20_ABI = [
    {"inputs": [{"internalType": "address", "name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "spender", "type": "address"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "owner", "type": "address"},
                {"internalType": "address", "name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

# Minimal CollateralOnramp ABI: wrap(address _asset, address _to, uint256 _amount)
ONRAMP_ABI = [
    {"inputs": [{"internalType": "address", "name": "_asset", "type": "address"},
                {"internalType": "address", "name": "_to", "type": "address"},
                {"internalType": "uint256", "name": "_amount", "type": "uint256"}],
     "name": "wrap", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]

MATIC_USD = 0.50  # Rough MATIC/USD for gas estimation


def to_raw(amount: float) -> int:
    return int(amount * 10**DECIMALS)


def from_raw(raw: int) -> float:
    return raw / 10**DECIMALS


def send_tx(w3, txn, private_key, label: str) -> dict:
    """Sign, send, and wait for a transaction receipt."""
    signed = w3.eth.account.sign_transaction(txn, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {label} tx sent: {tx_hash.hex()}")
    print(f"  Waiting for confirmation (60s timeout)...")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    except Exception:
        print(f"  TIMEOUT — check manually: https://polygonscan.com/tx/{tx_hash.hex()}")
        raise
    if receipt.status != 1:
        print(f"  REVERTED — tx: {tx_hash.hex()}")
        raise RuntimeError(f"{label} transaction reverted (status=0)")
    gas_used = receipt.gasUsed
    gas_price = txn.get("gasPrice", w3.eth.gas_price)
    gas_cost_matic = gas_used * gas_price / 10**18
    print(f"  Confirmed in block {receipt.blockNumber} | gas: {gas_used} | cost: {gas_cost_matic:.6f} MATIC")
    return receipt


def main():
    parser = argparse.ArgumentParser(description="Wrap USDC.e → pUSD via CollateralOnramp")
    parser.add_argument("--amount", type=float, required=True, help="USDC.e amount to wrap (e.g., 10.5)")
    parser.add_argument("--execute", action="store_true", help="Actually submit transactions (default: dry run)")
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
    amount_raw = to_raw(args.amount)

    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
    onramp = w3.eth.contract(address=ONRAMP_ADDRESS, abi=ONRAMP_ABI)

    # --- Pre-checks ---
    usdc_bal = usdc.functions.balanceOf(wallet).call()
    pusd_bal = pusd.functions.balanceOf(wallet).call()
    current_allowance = usdc.functions.allowance(wallet, ONRAMP_ADDRESS).call()
    matic_bal = w3.eth.get_balance(wallet)
    gas_price = w3.eth.gas_price

    mode = "EXECUTE" if args.execute else "DRY RUN"
    banner = f"{'='*60}\n  {mode} — WRAP {args.amount:.2f} USDC.e → pUSD\n{'='*60}"
    print(f"\n{banner}\n")
    print(f"Wallet:           {wallet}")
    print(f"RPC:              {rpc_url}")
    print(f"USDC.e balance:   {from_raw(usdc_bal):.6f}")
    print(f"pUSD balance:     {from_raw(pusd_bal):.6f}")
    print(f"MATIC balance:    {matic_bal / 10**18:.6f}")
    print(f"Current allowance (USDC.e → Onramp): {from_raw(current_allowance):.6f}")
    print(f"Wrap amount:      {args.amount:.6f} ({amount_raw} raw)")
    print()

    if usdc_bal < amount_raw:
        print(f"ERROR: Insufficient USDC.e balance ({from_raw(usdc_bal):.2f} < {args.amount:.2f})")
        sys.exit(1)

    # Estimate gas via estimateGas() — aborts early if tx would revert
    needs_approve = current_allowance < amount_raw
    est_gas_approve = 0
    est_gas_wrap = 0

    print("Planned transactions:")
    if needs_approve:
        print(f"  1. APPROVE: USDC.e.approve(Onramp={ONRAMP_ADDRESS}, amount={amount_raw})")
        try:
            est_gas_approve = usdc.functions.approve(ONRAMP_ADDRESS, amount_raw).estimate_gas({"from": wallet})
            print(f"     Est gas: {est_gas_approve:,}")
        except Exception as e:
            print(f"  ERROR: approve estimateGas() failed — transaction would revert:\n    {e}")
            sys.exit(1)
    else:
        print(f"  1. APPROVE: SKIP (allowance {from_raw(current_allowance):.2f} >= {args.amount:.2f})")

    print(f"  2. WRAP:    Onramp.wrap(USDC.e, {wallet}, {amount_raw})")
    if args.execute:
        # In execute mode, wrap estimateGas runs AFTER approve is confirmed (see below)
        est_gas_wrap = 150_000  # placeholder for pre-execute summary
        print(f"     Est gas: ~{est_gas_wrap:,} (will verify after approve)")
    else:
        # Dry-run: can't simulate wrap without on-chain approval, use conservative estimate
        est_gas_wrap = 150_000
        print(f"     Est gas: ~{est_gas_wrap:,} (estimated pre-approve — actual checked at execute time)")

    est_total_gas = est_gas_approve + est_gas_wrap
    est_cost_matic = est_total_gas * gas_price / 10**18
    est_cost_usd = est_cost_matic * MATIC_USD
    print(f"\nEstimated total gas: ~{est_total_gas:,} ({est_cost_matic:.6f} MATIC ≈ ${est_cost_usd:.4f})")

    if not args.execute:
        print(f"\n{banner}\n")
        return

    # --- Confirmation prompt ---
    print("\n⚠️  This will send real transactions on Polygon mainnet.")
    confirm = input("Type WRAP to proceed: ")
    if confirm.strip() != "WRAP":
        print("Aborted.")
        sys.exit(0)

    print()
    nonce = w3.eth.get_transaction_count(wallet)

    # --- Step 1: Approve ---
    if needs_approve:
        print("Step 1: Approving USDC.e spend by Onramp...")
        approve_txn = usdc.functions.approve(ONRAMP_ADDRESS, amount_raw).build_transaction({
            "chainId": CHAIN_ID,
            "from": wallet,
            "nonce": nonce,
            "gasPrice": gas_price,
        })
        receipt = send_tx(w3, approve_txn, private_key, "Approve")
        nonce += 1
    else:
        print("Step 1: Approve SKIP (sufficient allowance)")

    # --- Step 2: Wrap ---
    print("\nStep 2: Verifying wrap transaction...")
    try:
        verified_gas = onramp.functions.wrap(USDC_ADDRESS, wallet, amount_raw).estimate_gas({"from": wallet})
        print(f"  wrap estimateGas() OK: {verified_gas:,} gas")
    except Exception as e:
        print(f"  ERROR: wrap estimateGas() failed AFTER approve confirmed:\n    {e}")
        print(f"  Aborting — not sending a doomed wrap transaction.")
        print(f"  Note: approve tx already confirmed. Allowance is set on-chain.")
        sys.exit(1)

    print("  Submitting wrap transaction...")
    wrap_txn = onramp.functions.wrap(USDC_ADDRESS, wallet, amount_raw).build_transaction({
        "chainId": CHAIN_ID,
        "from": wallet,
        "nonce": nonce,
        "gasPrice": gas_price,
    })
    receipt = send_tx(w3, wrap_txn, private_key, "Wrap")

    # --- Final balances ---
    print("\n--- Final balances ---")
    new_usdc = usdc.functions.balanceOf(wallet).call()
    new_pusd = pusd.functions.balanceOf(wallet).call()
    print(f"USDC.e: {from_raw(usdc_bal):.6f} → {from_raw(new_usdc):.6f} (Δ {from_raw(new_usdc - usdc_bal):+.6f})")
    print(f"pUSD:   {from_raw(pusd_bal):.6f} → {from_raw(new_pusd):.6f} (Δ {from_raw(new_pusd - pusd_bal):+.6f})")
    print(f"\n{'='*60}\n  WRAP COMPLETE\n{'='*60}\n")


if __name__ == "__main__":
    main()
