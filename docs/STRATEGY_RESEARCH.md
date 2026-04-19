# Strategy Research & Testing

**Use this doc to anchor strategy-focused chats.** Keep this file open or @-mention it so the AI stays in project scope and focused on researching and testing strategies for the PB&J trade bot.

## Purpose

- Research and compare strategies (e.g. arbitrage, resolution scalping, sentiment edges).
- Design and run backtests; interpret results.
- Propose and test changes in this repo (no separate project).
- Prefer paper/sim and small stakes; avoid advising large live risk unless you ask.

## Project context

- Codebase: PB&J (Polymarket trading bot).
- Key modules: `agents/application/` (arbitrage_trader, backtest, resolution_scalper, risk_manager), `agents/polymarket/`, CLI under `scripts/python/`.
- Roadmap and phases: see `nextsteps.md`.

When you’re in “strategy research mode,” keep this file or the relevant strategy modules in context so the Cursor rule for strategy research applies.
