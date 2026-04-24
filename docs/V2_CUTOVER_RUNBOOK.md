# V2 CLOB Cutover Runbook

## Preamble

**Purpose:** Step-by-step guide to execute the Polymarket CLOB V2 migration on April 28, 2026 at ~11:00 UTC.

**Expected total time:** 20-30 minutes

**Scope:**
- Crypto latency bot — migrate to V2
- Wallet stalker — migrate to V2
- Directional scanner — **NOT restarted** (pending bug investigation: DeepSeek V4 correctness issue)

**Prerequisites completed:**
- V2 SDK installed (`py-clob-client-v2==1.0.0`)
- V2 wrapper built and tested (`agents/polymarket/polymarket_v2.py`, 11/11 tests passing)
- pUSD wrapped (~247.63 pUSD available)
- V2 on-chain approvals set (pUSD + CTF for all three exchange contracts)
- All three bots validated against V2 in dry-run mode
- All bots stopped as of April 24

---

## 1. Pre-Cutover Checklist (run ~24hr before)

```bash
# Verify branch is clean and pushed
cd ~/Documents/PB\&J
git branch --show-current          # should be: v2-migration
git status --short                 # should show only runtime state files (predictions.json, etc.)
git log --oneline origin/v2-migration..v2-migration  # should be empty (in sync)

# Verify pUSD balance
PYTHONPATH="." venv/bin/python3 -c "
from agents.polymarket.polymarket_v2 import Polymarket
p = Polymarket()
print(f'pUSD: {p.get_usdc_balance():.2f}')
"
# Expected: ~247.63

# Verify MATIC balance (need gas for trades)
PYTHONPATH="." venv/bin/python3 -c "
from web3 import Web3; import os; from dotenv import load_dotenv; load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.getenv('POLYGON_RPC_URL', 'https://polygon-bor-rpc.publicnode.com')))
wallet = w3.eth.account.from_key(os.getenv('POLYGON_WALLET_PRIVATE_KEY')).address
print(f'MATIC: {w3.eth.get_balance(wallet) / 1e18:.2f}')
"
# Expected: ~51 MATIC (sufficient for hundreds of transactions)

# Verify no bot processes running
ps aux | grep -E "cli.py run-crypto|wallet_monitor|directional" | grep -v grep
# Expected: empty output

# Verify source files still on V1 imports
grep "from agents.polymarket.polymarket import Polymarket" \
  agents/application/crypto_latency_bot.py \
  agents/application/wallet_monitor.py \
  agents/application/claude_forecaster.py
# Expected: all three files show V1 import

# Verify main branch is at expected state
git fetch origin
git log --oneline main -1
# Expected: the last commit on main before v2-migration work started
# If main has unexpected commits, investigate before proceeding

# Check Polymarket official channels
# - https://status.polymarket.com
# - https://x.com/PolymarketDevs
# - Discord: #developers channel
```

---

## 2. Cutover Sequence (execute at ~11:00 UTC April 28)

### Step 1: Verify Polymarket V2 is live

```bash
# Check if the V2 CLOB is responding
curl -s https://clob.polymarket.com/ok
# Expected: "OK" (V2 is now the production endpoint)

# Check version
curl -s https://clob.polymarket.com/version
# Expected: 2 (confirms V2, not V1)
```

If either check fails or returns V1 responses, **STOP. V2 cutover has not happened yet.** Wait and re-check.

### Step 2: Update .env with V2 production host

Edit `~/Documents/PB&J/.env` and add or update:

```
CLOB_HOST=https://clob.polymarket.com
```

This tells `polymarket_v2.py` to use the production V2 endpoint instead of the pre-cutover test URL.

### Step 3: Update source imports

Open each file in your editor and change this one line:

```
FROM: from agents.polymarket.polymarket import Polymarket
TO:   from agents.polymarket.polymarket_v2 import Polymarket
```

Files to edit:
- `agents/application/crypto_latency_bot.py` (around line 40)
- `agents/application/wallet_monitor.py` (around line 32)

**DO NOT edit `agents/application/claude_forecaster.py` — scanner stays offline.**

```bash
# Verify the changes
grep 'from agents.polymarket.polymarket_v2 import Polymarket' \
  agents/application/crypto_latency_bot.py \
  agents/application/wallet_monitor.py
# Expected: both files show polymarket_v2

# Commit
git add agents/application/crypto_latency_bot.py agents/application/wallet_monitor.py
git commit -m "Cutover: switch crypto_latency and wallet_monitor to V2 wrapper"
```

### Step 4: Merge v2-migration to main

```bash
git checkout main
git merge --ff-only v2-migration
# If this fails (can't fast-forward), STOP and investigate.
# There should be no commits on main that aren't in v2-migration.
git push origin main
```

### Step 5: Quick V2 sanity check (30 seconds)

```bash
PYTHONPATH="." venv/bin/python3 scripts/python/v2_wrapper_test.py
```

**Expected:** 11/11 passing, now against the production V2 URL.

**If any test fails: STOP. See Rollback Plan below.** Do not start any bots.

### Step 6: Start crypto latency bot

```bash
# Foreground test first (30 iterations, ~30 seconds)
PYTHONPATH="." venv/bin/python3 scripts/python/cli.py run-crypto-latency \
  --dry-run --assets btc,eth,sol --windows 5,15 \
  --min-move 0.4 --max-entry 0.80 --max-iterations 30

# Watch for:
# - "CryptoLatencyBot initialized" (V2 wrapper loaded)
# - Binance WebSocket connected for all 3 assets
# - Polymarket book WebSocket connected
# - Balance shows pUSD amount (~247)
# - Clean termination after 30 iterations
# - Zero errors

# NOTE: The foreground test above uses --dry-run. The production start
# command below uses --no-dry-run. Switching to production mode is what
# activates real trading.

# If clean, start in production mode (backgrounded):
cd ~/Documents/PB\&J
./scripts/bash/start_bot.sh

# Verify running:
ps aux | grep "cli.py run-crypto" | grep -v grep
tail -20 crypto_latency_live.log
```

### Step 7: Start wallet stalker

```bash
# Foreground test first (5 iterations, ~2.5 minutes)
PYTHONPATH="." venv/bin/python3 -m agents.application.wallet_monitor \
  --dry-run --max-iterations 5

# Watch for:
# - V2 wrapper initialized
# - ensure_sell_approval() passes (approvals verified)
# - Both wallets seeded (scottilicious + winner877)
# - Clean polling, no errors
# - Tracked positions loaded from wallet_monitor_positions.json

# If clean, start in production mode (backgrounded):
cd ~/Documents/PB\&J
PYTHONPATH="." nohup venv/bin/python3 -m agents.application.wallet_monitor \
  --live --max-copy-size 15 >> wallet_monitor_trades.log 2>&1 &

# Verify running:
ps aux | grep wallet_monitor | grep -v grep
tail -10 wallet_monitor_trades.log
```

---

## 3. Post-Cutover Monitoring (first 2 hours)

```bash
# Watch both logs in parallel (two terminal tabs)
tail -f crypto_latency_live.log
tail -f wallet_monitor_trades.log
```

### Monitoring Checklist

```
☐ 5 min post-start:  Both bots still running (ps aux check)
☐ 15 min post-start: No error stack traces in either log file
☐ 30 min post-start: Telegram startup alerts received for both bots
☐ 1 hour post-start: Crypto latency bot has scanned at least 100 times
                      (check log for iteration count or status prints)
☐ 1 hour post-start: Wallet stalker has polled whale activity at least 60 times
                      (check log for [Iter N] lines)
☐ First trade:       tx hash captured, Polygonscan verified, Telegram alert received
☐ 2 hours post-start: No unexpected crashes or restarts
```

### What to watch for in logs
- Any `PolyApiException` errors (V2 API rejections)
- Any `web3` errors (on-chain interaction failures)
- `ensure_sell_approval()` should only fire once per session
- Balance reads should show pUSD (not USDC.e)

---

## 4. Rollback Plan

### Scenario A: V2 bot crashes on startup

1. Stop the failing bot
2. Revert the import change in the affected file
3. **V1 CLOB is retired — V1 bots CANNOT run.** Do not attempt.
4. Debug the V2 wrapper in place. Common issues:
   - Missing pUSD balance → run `wrap_usdc_to_pusd.py`
   - Missing approvals → run `set_v2_approvals.py`
   - API change → inspect error, check V2 SDK release notes

### Scenario B: V2 wrapper test fails at Step 5

1. **STOP entire cutover.** Do not start any bots.
2. Investigate the specific failing test
3. Check if Polymarket made last-minute V2 API changes
4. Keep bots stopped until the issue is resolved and tests pass 11/11

### Scenario C: First live trade fails

1. Stop the bot immediately
2. Capture the full error output and order response
3. Check the tx hash on Polygonscan (if a transaction was submitted)
4. Common failure modes:
   - `InsufficientAllowance` → re-run `set_v2_approvals.py --execute`
   - `InsufficientBalance` → check pUSD balance, wrap more if needed
   - `order_version_mismatch` → V2 SDK should auto-retry; if persistent, check SDK version
5. Do not restart until diagnosed

---

## 5. Post-Migration Cleanup (within 48 hours)

```bash
# If not already done in Step 4:
git checkout main
git merge v2-migration
git push origin main

# Delete the migration branch
git branch -d v2-migration
git push origin --delete v2-migration

# Add CLAUDE.md entry documenting successful cutover
# (or document any issues encountered)

# Set up directional scanner cron (ONLY after scanner bug is investigated)
# crontab -e
# 0 6 * * * cd /Users/pablo/Documents/PB\&J && ...
```

---

## 6. Known Items NOT Addressed on Cutover Day

| Item | Status | When |
|------|--------|------|
| Directional scanner bug investigation | Blocked | Post-cutover task |
| Layers 3 & 4 of polymarket_v2.py (Gamma API, redemption) | Not needed | Only if bots require these methods |
| Rename `get_usdc_balance()` → `get_pusd_balance()` | Cosmetic | Post-cutover cleanup |
| V1 winning positions resolving as USDC.e | One-off wraps | As positions resolve |
| Remove V1 `polymarket.py` | After all bots migrated | Post-scanner-fix |
| Update `cli.py` line 4 top-level Polymarket import | After scanner migrated | Affects redeem/check-balance commands |
