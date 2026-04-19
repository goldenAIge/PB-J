"""V2 CLOB connectivity test — read-only validation against clob-v2.polymarket.com."""

import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from py_clob_client_v2 import ClobClient, BalanceAllowanceParams, AssetType

V2_HOST = "https://clob-v2.polymarket.com"
CHAIN_ID = 137
# US/Iran nuclear deal test market token (from migration docs)
TEST_TOKEN_ID = "102936224134271070189104847090829839924697394514566827387181305960175107677216"

key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
if not key:
    print("FAIL: POLYGON_WALLET_PRIVATE_KEY not set in .env")
    sys.exit(1)


def run_test(name, fn):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        result = fn()
        print(f"PASS: {name}")
        return True
    except Exception as e:
        print(f"FAIL: {name}")
        traceback.print_exc()
        return False


# --- Test functions ---

def test_basic_connection():
    """Test a) — basic GET endpoint."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID)
    resp = client.get_ok()
    print(f"  get_ok() -> {resp}")
    version = client.get_version()
    print(f"  get_version() -> {version}")
    server_time = client.get_server_time()
    print(f"  get_server_time() -> {server_time}")
    return resp


def test_market_data():
    """Test b) — fetch orderbook for test market token."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID)
    book = client.get_order_book(TEST_TOKEN_ID)
    print(f"  Order book type: {type(book).__name__}")
    if hasattr(book, "bids"):
        print(f"  Bids: {len(book.bids)} levels")
    if hasattr(book, "asks"):
        print(f"  Asks: {len(book.asks)} levels")
    if hasattr(book, "market"):
        print(f"  Market: {book.market}")
    print(f"  Raw: {str(book)[:300]}")
    return book


def test_api_credentials():
    """Test c) — derive API key via L1 auth."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID, key=key)
    creds = client.create_or_derive_api_key()
    print(f"  API key derived: {creds.api_key[:16]}...")
    print(f"  Secret: {creds.api_secret[:8]}...")
    print(f"  Passphrase: {creds.api_passphrase[:8]}...")
    return creds


def test_authenticated_read():
    """Test d) — authed read using derived creds."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID, key=key)
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Balance/allowance: {bal}")
    return bal


def test_tick_size():
    """Test e1) — query tick size for test market token."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID)
    tick = client.get_tick_size(TEST_TOKEN_ID)
    print(f"  Tick size: {tick}")
    return tick


def test_fee_rate():
    """Test e2) — query fee rate for test market token."""
    client = ClobClient(V2_HOST, chain_id=CHAIN_ID, key=key)
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    fee = client.get_fee_rate_bps(TEST_TOKEN_ID)
    print(f"  Fee rate (bps): {fee}")
    return fee


# --- Run all tests ---

if __name__ == "__main__":
    print("V2 CLOB Connectivity Test")
    print(f"Host: {V2_HOST}")
    print(f"Chain: {CHAIN_ID}")
    print(f"Test token: {TEST_TOKEN_ID[:30]}...")

    tests = [
        ("a) Basic connection", test_basic_connection),
        ("b) Market data read", test_market_data),
        ("c) API credentials", test_api_credentials),
        ("d) Authenticated read", test_authenticated_read),
        ("e1) Tick size query", test_tick_size),
        ("e2) Fee rate query", test_fee_rate),
    ]

    results = []
    for name, fn in tests:
        results.append(run_test(name, fn))

    print(f"\n{'='*60}")
    print(f"RESULTS: {sum(results)}/{len(results)} passed")
    print(f"{'='*60}")
