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


# --- Layer 2 write tests ---

live_order_id = None  # set by test_place_order, used by 7/8/9


def test_place_order():
    """Test 6: place unfillable limit buy ($0.01 bid on a $0.99 ask market)."""
    global live_order_id
    resp = poly.execute_limit_buy(TEST_TOKEN_ID, price=0.01, size=5)
    print(f"  Response: {resp}")
    assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
    order_id = resp.get("orderID") or resp.get("id")
    assert order_id, f"No orderID in response: {resp}"
    live_order_id = order_id
    print(f"  Order ID: {live_order_id}")
    print(f"\n  {'='*55}")
    print(f"  ORDER PLACED: {live_order_id}")
    print(f"  You can manually cancel at any time via:")
    print(f'    cd ~/Documents/PB\\&J && PYTHONPATH="." venv/bin/python3 -c \\')
    print(f"      \"from agents.polymarket.polymarket_v2 import Polymarket; \\")
    print(f"       Polymarket().cancel_order('{live_order_id}')\"")
    print(f"  {'='*55}")
    return resp


def test_verify_live():
    """Test 7: verify order is live on the book."""
    import time
    # V2 CLOB has eventual consistency — GET endpoint needs ~1s to index a newly placed order
    time.sleep(1)
    assert live_order_id, "No order ID from test 6"
    resp = poly.get_order(live_order_id)
    print(f"  Response: {resp}")
    assert resp is not None, "get_order returned None"
    return resp


def test_cancel():
    """Test 8: cancel the order."""
    assert live_order_id, "No order ID from test 6"
    resp = poly.cancel_order(live_order_id)
    print(f"  Response: {resp}")
    assert resp is not None, "cancel_order returned None"
    return resp


def test_verify_cancelled():
    """Test 9: verify order is no longer live."""
    assert live_order_id, "No order ID from test 6"
    resp = poly.get_order(live_order_id)
    print(f"  Response: {resp}")
    # Cancelled orders may return None, empty, or a dict with cancelled status
    if resp is None:
        print("  Order gone (None) — cancel confirmed")
        return True
    if isinstance(resp, dict):
        status = str(resp.get("status", resp.get("order_status", ""))).lower()
        print(f"  Status: {status}")
        assert "live" not in status, f"Order still LIVE after cancel: {resp}"
    return resp


# --- Approval verification tests ---


def test_ensure_sell_approval():
    """Test 10: ensure_sell_approval returns True (approvals set Apr 21)."""
    result = poly.ensure_sell_approval()
    print(f"  Result: {result}")
    assert result is True, f"Expected True, got {result}"
    return result


def test_approval_caching():
    """Test 11: second call uses cache (instant, no on-chain read)."""
    import time
    assert poly._sell_approval_verified, "Cache flag not set after test 10"
    start = time.perf_counter()
    result = poly.ensure_sell_approval()
    elapsed = time.perf_counter() - start
    print(f"  Result: {result}")
    print(f"  Elapsed: {elapsed*1000:.1f}ms")
    print(f"  Cache flag: {poly._sell_approval_verified}")
    assert result is True, f"Expected True, got {result}"
    assert elapsed < 0.1, f"Cached call took {elapsed:.3f}s — cache not working"
    return result


# --- Run all ---

if __name__ == "__main__":
    read_tests = [
        ("1) Init + API keys", test_init),
        ("2) get_best_ask", test_best_ask),
        ("3) get_orderbook", test_orderbook),
        ("4) get_usdc_balance (pUSD)", test_balance),
        ("5) get_order (nonexistent)", test_nonexistent_order),
    ]

    write_tests = [
        ("6) Place unfillable limit buy", test_place_order),
        ("7) Verify order is live", test_verify_live),
        ("8) Cancel order", test_cancel),
        ("9) Verify cancellation", test_verify_cancelled),
    ]

    results = []
    for name, fn in read_tests:
        results.append((name, run_test(name, fn)))

    # Write tests: 7/8/9 depend on 6 succeeding
    t6_result = run_test(*write_tests[0])
    results.append((write_tests[0][0], t6_result))

    if t6_result:
        for name, fn in write_tests[1:]:
            results.append((name, run_test(name, fn)))
        # Safety check: if cancel failed (test 8), warn loudly
        cancel_ok = results[-2][1]  # test 8 result
        if not cancel_ok and live_order_id:
            print(f"\n{'!'*60}")
            print(f"  WARNING: ORDER {live_order_id} MAY STILL BE LIVE")
            print(f"  Cancel it manually at polymarket.com or re-run cancel_order()")
            print(f"{'!'*60}")
    else:
        for name, _ in write_tests[1:]:
            print(f"\n{'='*60}")
            print(f"TEST: {name}")
            print(f"{'='*60}")
            print(f"SKIPPED: {name} (depends on test 6)")
            results.append((name, None))

    # Approval verification tests (independent of write tests)
    approval_tests = [
        ("10) ensure_sell_approval", test_ensure_sell_approval),
        ("11) Approval caching", test_approval_caching),
    ]
    for name, fn in approval_tests:
        ok = run_test(name, fn)
        results.append((name, ok))
        if name.startswith("10") and not ok:
            print(f"\n{'!'*60}")
            print(f"  WARNING: V2 APPROVALS MAY NEED RE-SETTING")
            print(f"  Run: PYTHONPATH='.' python3 scripts/python/set_v2_approvals.py --execute")
            print(f"{'!'*60}")

    print(f"\n{'='*60}")
    passed = sum(1 for _, ok in results if ok is True)
    skipped = sum(1 for _, ok in results if ok is None)
    failed = sum(1 for _, ok in results if ok is False)
    total = len(results)
    print(f"RESULTS: {passed}/{total} passed", end="")
    if skipped:
        print(f", {skipped} skipped", end="")
    if failed:
        print(f", {failed} failed", end="")
        for name, ok in results:
            if ok is False:
                print(f"\n  FAILED: {name}", end="")
    print(f"\n{'='*60}")
