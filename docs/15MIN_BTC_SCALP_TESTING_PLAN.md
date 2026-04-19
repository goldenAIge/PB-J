# 15-Minute BTC Resolution Scalping — Testing Plan

**Goal:** Validate the resolution scalping strategy on 15-min BTC markets with **conservative risk** before considering any all-in sizing. Preserve capital while measuring edge.

---

## Why test first

- One loss with all-in = full wipeout. We need evidence the strategy has positive expectancy before risking 100% per trade.
- 15-min BTC markets are the same venue/timeframe as the trader who went $12→$100k; we test the *strategy*, not the *sizing*, first.

---

## Phases

### Phase 1: Restrict scalper to 15-min BTC + dry-run (no real money)

- **What:** Run the resolution scalper only on markets that:
  - Are BTC (or short-timeframe crypto) resolution markets.
  - Resolve in roughly 5–20 minutes (so we’re in the “near resolution” window).
- **How:** Use existing `resolution_scalper.py` with:
  - A **15-min BTC filter** (question/title + `endDate` window).
  - **Dry-run mode** and optional **simulated balance** (e.g. $100).
- **Success:** Bot finds 15-min BTC markets, evaluates them, and “trades” in dry-run. We confirm it doesn’t crash and that logic and logs make sense.

**Deliverable:** Scalper supports `--btc-15min-only` (or config) and runs in dry-run on those markets only.

---

### Phase 2: Paper / simulated run (log and measure)

- **What:** Run the scalper in dry-run for a fixed period (e.g. 24–48 hours or N scans) **only on 15-min BTC**.
- **How:** Log every opportunity found, every “trade” (simulated), and track:
  - Win rate (we only “win” when we would have held to resolution and outcome was correct; in dry-run we can assume we hold to resolution and use a simple heuristic or manual review).
  - For a **true** win rate we need either: (a) run live with tiny size and record outcomes, or (b) backtest on historical data (Phase 2b).
- **Success:** We see a steady stream of 15-min BTC opportunities and plausible entries; no unexpected errors.

**Note:** Full win-rate from dry-run alone is limited (we don’t get real resolutions in sim). So Phase 2 can be “does it run and find trades?” and Phase 2b or 3 gives us real win rate.

---

### Phase 3: Small live (real money, conservative size)

- **What:** Run **live** with small capital (e.g. $20–50) and **conservative position sizing** (e.g. 10–20% of balance per trade, not all-in).
- **How:** Same 15-min BTC filter; `--live` with `max_trade_percent=0.15` (or similar) and `max_trade_size` cap.
- **Success:** 15–30+ trades with:
  - Win rate **above ~85%** (for 0.90–0.96 style scalps, we need very high win rate to be profitable after fees).
  - Positive PnL and no catastrophic drawdown.

---

### Phase 4: Consider all-in (only if Phase 3 is successful)

- **What:** If Phase 3 shows sustained edge (e.g. 85%+ win rate, positive expectancy), **then** consider a separate mode or config that uses 100% of balance per trade (all-in), with the same entry rules and only on 15-min BTC.
- **How:** Optional `--all-in` or config flag; strict entry filters; user explicitly opts in.
- **Risk:** One loss = full wipeout. Only use a small “risk capital” allocation.

---

## Best approach summary

| Step | Action |
|------|--------|
| 1 | Add 15-min BTC market filter to resolution scalper + run dry-run. |
| 2 | Run dry-run for 24–48h (or many scans); confirm opportunities and stability. |
| 3 | Run live with small size (10–20% per trade); collect 15–30+ trades. |
| 4 | If win rate and PnL are strong, optionally add all-in mode with strict filters. |

**Principle:** Make money, but don’t lose what we have — test with no/small capital first, then scale sizing only after proof.

---

## Implementation checklist

- [x] Add 15-min BTC market filter (question + resolution window) to `resolution_scalper.py`.
- [x] Add CLI flag `--btc-15min-only` and optional `--min-minutes-to-resolution` / `--max-minutes-to-resolution`.
- [ ] Run dry-run with simulated balance and 15-min BTC only (Phase 1).
- [ ] (Optional) Extend `backtest.py` for resolution scalping on simulated 15-min markets to estimate win rate before live.
- [ ] Run Phase 2 (paper) and Phase 3 (small live) per above.

### Phase 1 – Run 15-min BTC dry-run

From repo root with venv activated:

```bash
# Dry-run, 15-min BTC only, simulated $100 balance (no real money)
python -m agents.application.resolution_scalper --dry-run --btc-15min-only --simulated-balance 100 --scan-interval 60
```

Optional: tighten the resolution window (default is 2–20 min):

```bash
python -m agents.application.resolution_scalper --dry-run --btc-15min-only --min-minutes-to-resolution 3 --max-minutes-to-resolution 15 --simulated-balance 100
```

Stop with Ctrl+C after you’ve seen a few scans (or let it run 24–48h for Phase 2).
