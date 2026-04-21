"""Test the polymarket_v2.py Layer 1 wrapper against the live V2 CLOB.

Exercises the same methods the crypto latency bot uses, in the order
a real bot session would call them.

Usage:
    PYTHONPATH="." python3 scripts/python/v2_wrapper_test.py
"""

import traceback

# US/Iran nuclear deal test market (same as connectivity test)
TEST_TOKEN_ID = "102936224134271070189104847090829839924697394514566827387181305960175107677216"


def run_test(name, fn):
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        result = fn()
        print(f"PASS: {name}")
        return True
    except Exception:
        print(f"FAIL: {name}")
        traceback.print_exc()
        return False


# --- Tests ---

poly = None  # set in test_init


def test_init():
    """Test 1: instantiation and API key derivation."""
    global poly
    from agents.polymarket.polymarket_v2 import Polymarket
    poly = Polymarket()
    print(f"  clob_url: {poly.clob_url}")
    print(f"  chain_id: {poly.chain_id}")
    print(f"  wallet:   {poly.get_address_for_private_key()}")
    print(f"  client:   {type(poly.client).__name__}")
    assert poly.client is not None, "ClobClient is None"
    assert poly.client.creds is not None, "API creds not set"
    return poly


def test_best_ask():
    """Test 2: get_best_ask returns a float between 0 and 1."""
    ask = poly.get_best_ask(TEST_TOKEN_ID)
    print(f"  Best ask: {ask}")
    assert ask is not None, "get_best_ask returned None"
    assert isinstance(ask, float), f"Expected float, got {type(ask)}"
    assert 0 < ask <= 1.0, f"Ask {ask} outside valid range (0, 1]"
    return ask


def test_orderbook():
    """Test 3: get_orderbook returns bids and asks."""
    book = poly.get_orderbook(TEST_TOKEN_ID)
    print(f"  Type: {type(book).__name__}")

    bids = book.get("bids", []) if isinstance(book, dict) else getattr(book, "bids", [])
    asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
    print(f"  Bids: {len(bids)} levels")
    print(f"  Asks: {len(asks)} levels")

    print("  Top 3 bids:")
    for b in bids[:3]:
        price = b["price"] if isinstance(b, dict) else b.price
        size = b["size"] if isinstance(b, dict) else b.size
        print(f"    ${price} x {size}")

    print("  Top 3 asks:")
    for a in asks[:3]:
        price = a["price"] if isinstance(a, dict) else a.price
        size = a["size"] if isinstance(a, dict) else a.size
        print(f"    ${price} x {size}")

    assert len(bids) > 0 or len(asks) > 0, "Order book is completely empty"
    return book


def test_balance():
    """Test 4: get_usdc_balance returns pUSD balance (~10.0 expected)."""
    bal = poly.get_usdc_balance()
    print(f"  pUSD balance: {bal:.6f}")
    assert isinstance(bal, float), f"Expected float, got {type(bal)}"
    assert bal >= 0, f"Negative balance: {bal}"
    if abs(bal - 10.0) > 1.0:
        print(f"  WARNING: expected ~10.0 pUSD (wrapped earlier), got {bal:.6f}")
    return bal


def test_nonexistent_order():
    """Test 5: get_order with fake ID — should handle gracefully."""
    fake_id = "nonexistent_order_id_12345"
    try:
        result = poly.get_order(fake_id)
        print(f"  Response: {result}")
        print(f"  (Returned without error — empty or error response is fine)")
        return result
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "not found" in err_str.lower() or "400" in err_str:
            print(f"  Expected error for fake order: {err_str[:120]}")
            return True
        raise


# --- Run all ---

if __name__ == "__main__":
    tests = [
        ("1) Init + API keys", test_init),
        ("2) get_best_ask", test_best_ask),
        ("3) get_orderbook", test_orderbook),
        ("4) get_usdc_balance (pUSD)", test_balance),
        ("5) get_order (nonexistent)", test_nonexistent_order),
    ]

    results = []
    for name, fn in tests:
        results.append(run_test(name, fn))

    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"RESULTS: {passed}/{len(results)} passed")
    if passed < len(results):
        for (name, _), ok in zip(tests, results):
            if not ok:
                print(f"  FAILED: {name}")
    print(f"{'='*60}")
