"""
Backtesting Module for Arbitrage Strategy

Simulates the arbitrage strategy on historical or simulated market data
to estimate win rate and profitability.

Usage:
    python -m agents.application.backtest [--num-simulations 1000]
"""

import random
import json
import logging
import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
from statistics import mean, stdev

from agents.polymarket.gamma import GammaMarketClient
from agents.application.risk_manager import RiskConfig

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class SimulatedMarket:
    """A simulated market for backtesting"""
    market_id: int
    yes_price: float
    no_price: float
    combined_price: float
    volatility: float
    resolution: Optional[str] = None  # "YES" or "NO"


@dataclass
class BacktestTrade:
    """Record of a backtested trade"""
    market_id: int
    entry_yes_price: float
    entry_no_price: float
    entry_combined: float
    trade_size: float
    resolution: str
    payout: float
    profit: float
    profit_percent: float
    fees_paid: float


@dataclass
class BacktestResult:
    """Results from a backtest run"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_profit: float
    total_fees: float
    avg_profit_per_trade: float
    max_profit: float
    max_loss: float
    profit_std_dev: float
    sharpe_ratio: float
    starting_balance: float
    ending_balance: float
    return_percent: float


class ArbitrageBacktester:
    """
    Backtesting engine for the arbitrage strategy.

    Can use:
    1. Historical market data from Polymarket (when available)
    2. Simulated market data based on realistic price distributions
    """

    def __init__(self, risk_config: Optional[RiskConfig] = None, starting_balance: float = 1000.0):
        self.risk_config = risk_config or RiskConfig()
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.trades: list[BacktestTrade] = []
        self.gamma = GammaMarketClient()

    def generate_simulated_markets(self, num_markets: int = 1000) -> list[SimulatedMarket]:
        """
        Generate simulated markets with realistic price distributions.

        In real markets, arbitrage opportunities are rare. We simulate a mix:
        - 95% of markets: YES + NO = ~1.00 (no arb opportunity)
        - 4% of markets: YES + NO = 0.98-1.00 (marginal/no opportunity)
        - 1% of markets: YES + NO < 0.98 (arbitrage opportunity)
        """
        markets = []

        for i in range(num_markets):
            rand = random.random()

            if rand < 0.01:  # 1% - Clear arbitrage opportunity
                combined = random.uniform(0.90, 0.97)
            elif rand < 0.05:  # 4% - Marginal opportunity
                combined = random.uniform(0.97, 0.995)
            else:  # 95% - No opportunity
                combined = random.uniform(0.995, 1.02)

            # Generate YES price (typically between 0.10 and 0.90)
            yes_price = random.uniform(0.10, 0.90)
            no_price = combined - yes_price

            # Ensure no_price is valid
            if no_price <= 0 or no_price >= 1:
                no_price = 1.0 - yes_price
                combined = yes_price + no_price

            # Determine resolution (weighted by prices - higher price more likely to win)
            resolution = "YES" if random.random() < yes_price else "NO"

            # Add some volatility factor (how much price might change)
            volatility = random.uniform(0.01, 0.10)

            markets.append(SimulatedMarket(
                market_id=i,
                yes_price=round(yes_price, 4),
                no_price=round(no_price, 4),
                combined_price=round(combined, 4),
                volatility=volatility,
                resolution=resolution
            ))

        return markets

    def fetch_historical_markets(self, limit: int = 100) -> list[dict]:
        """Fetch current markets from Polymarket for analysis"""
        try:
            markets = self.gamma.get_all_current_markets(limit=limit)
            logger.info(f"Fetched {len(markets)} markets from Polymarket")
            return markets
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    def is_arbitrage_opportunity(self, market: SimulatedMarket) -> bool:
        """Check if market presents an arbitrage opportunity"""
        gross_profit = 1.0 - market.combined_price
        fees = market.combined_price * self.risk_config.fee_rate
        net_profit_percent = (gross_profit - fees) / market.combined_price

        return net_profit_percent >= self.risk_config.min_profit_threshold

    def calculate_trade_size(self) -> float:
        """Calculate trade size based on current balance and risk config"""
        max_by_percent = self.current_balance * self.risk_config.max_trade_percent
        trade_size = min(max_by_percent, self.risk_config.max_trade_size)
        trade_size = max(trade_size, self.risk_config.min_trade_size)

        if trade_size > self.current_balance:
            trade_size = self.current_balance

        return trade_size

    def simulate_trade(self, market: SimulatedMarket) -> Optional[BacktestTrade]:
        """Simulate executing an arbitrage trade on a market"""
        if not self.is_arbitrage_opportunity(market):
            return None

        trade_size = self.calculate_trade_size()
        if trade_size < self.risk_config.min_trade_size:
            return None

        # Calculate fees
        fees = trade_size * self.risk_config.fee_rate

        # In arbitrage, we buy equal amounts of YES and NO
        # At resolution, one side pays out $1 per share
        # Our total cost is: trade_size + fees
        # Our payout is: trade_size / combined_price (shares) * $1

        shares_bought = trade_size / market.combined_price
        payout = shares_bought  # Each share resolves to $1

        total_cost = trade_size + fees
        profit = payout - total_cost
        profit_percent = profit / total_cost if total_cost > 0 else 0

        # Update balance
        self.current_balance += profit

        trade = BacktestTrade(
            market_id=market.market_id,
            entry_yes_price=market.yes_price,
            entry_no_price=market.no_price,
            entry_combined=market.combined_price,
            trade_size=trade_size,
            resolution=market.resolution,
            payout=payout,
            profit=profit,
            profit_percent=profit_percent,
            fees_paid=fees
        )

        self.trades.append(trade)
        return trade

    def run_backtest(self, markets: list[SimulatedMarket]) -> BacktestResult:
        """Run backtest on a list of markets"""
        logger.info(f"Running backtest on {len(markets)} markets...")
        logger.info(f"Starting balance: ${self.starting_balance:.2f}")

        self.current_balance = self.starting_balance
        self.trades = []

        for market in markets:
            self.simulate_trade(market)

        # Calculate results
        if not self.trades:
            logger.warning("No trades executed during backtest")
            return BacktestResult(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0,
                total_profit=0,
                total_fees=0,
                avg_profit_per_trade=0,
                max_profit=0,
                max_loss=0,
                profit_std_dev=0,
                sharpe_ratio=0,
                starting_balance=self.starting_balance,
                ending_balance=self.current_balance,
                return_percent=0
            )

        profits = [t.profit for t in self.trades]
        winning = [t for t in self.trades if t.profit > 0]
        losing = [t for t in self.trades if t.profit <= 0]

        total_profit = sum(profits)
        total_fees = sum(t.fees_paid for t in self.trades)
        avg_profit = mean(profits) if profits else 0
        profit_std = stdev(profits) if len(profits) > 1 else 0

        # Sharpe ratio (simplified - assuming risk-free rate of 0)
        sharpe = avg_profit / profit_std if profit_std > 0 else 0

        result = BacktestResult(
            total_trades=len(self.trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=len(winning) / len(self.trades) if self.trades else 0,
            total_profit=total_profit,
            total_fees=total_fees,
            avg_profit_per_trade=avg_profit,
            max_profit=max(profits) if profits else 0,
            max_loss=min(profits) if profits else 0,
            profit_std_dev=profit_std,
            sharpe_ratio=sharpe,
            starting_balance=self.starting_balance,
            ending_balance=self.current_balance,
            return_percent=(self.current_balance - self.starting_balance) / self.starting_balance
        )

        return result

    def print_results(self, result: BacktestResult):
        """Print formatted backtest results"""
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"\nTrade Statistics:")
        print(f"  Total Trades:     {result.total_trades}")
        print(f"  Winning Trades:   {result.winning_trades}")
        print(f"  Losing Trades:    {result.losing_trades}")
        print(f"  Win Rate:         {result.win_rate:.2%}")

        print(f"\nProfit/Loss:")
        print(f"  Starting Balance: ${result.starting_balance:.2f}")
        print(f"  Ending Balance:   ${result.ending_balance:.2f}")
        print(f"  Total Profit:     ${result.total_profit:.2f}")
        print(f"  Total Fees:       ${result.total_fees:.2f}")
        print(f"  Return:           {result.return_percent:.2%}")

        print(f"\nPer-Trade Statistics:")
        print(f"  Avg Profit:       ${result.avg_profit_per_trade:.4f}")
        print(f"  Max Profit:       ${result.max_profit:.4f}")
        print(f"  Max Loss:         ${result.max_loss:.4f}")
        print(f"  Std Deviation:    ${result.profit_std_dev:.4f}")
        print(f"  Sharpe Ratio:     {result.sharpe_ratio:.4f}")

        print("\n" + "=" * 60)


def run_monte_carlo(num_simulations: int = 100, markets_per_sim: int = 1000,
                    starting_balance: float = 1000.0) -> dict:
    """
    Run Monte Carlo simulation to estimate strategy performance.

    Returns statistics across multiple simulation runs.
    """
    logger.info(f"Running Monte Carlo simulation: {num_simulations} runs, {markets_per_sim} markets each")

    results = []

    for i in range(num_simulations):
        backtester = ArbitrageBacktester(starting_balance=starting_balance)
        markets = backtester.generate_simulated_markets(markets_per_sim)
        result = backtester.run_backtest(markets)
        results.append(result)

        if (i + 1) % 10 == 0:
            logger.info(f"Completed {i + 1}/{num_simulations} simulations")

    # Aggregate statistics
    win_rates = [r.win_rate for r in results]
    returns = [r.return_percent for r in results]
    total_profits = [r.total_profit for r in results]
    trade_counts = [r.total_trades for r in results]

    summary = {
        "num_simulations": num_simulations,
        "markets_per_simulation": markets_per_sim,
        "starting_balance": starting_balance,
        "avg_win_rate": mean(win_rates),
        "win_rate_std": stdev(win_rates) if len(win_rates) > 1 else 0,
        "avg_return": mean(returns),
        "return_std": stdev(returns) if len(returns) > 1 else 0,
        "avg_profit": mean(total_profits),
        "profit_std": stdev(total_profits) if len(total_profits) > 1 else 0,
        "avg_trades": mean(trade_counts),
        "min_return": min(returns),
        "max_return": max(returns),
    }

    print("\n" + "=" * 60)
    print("MONTE CARLO SIMULATION RESULTS")
    print("=" * 60)
    print(f"\nSimulation Parameters:")
    print(f"  Simulations:      {num_simulations}")
    print(f"  Markets/Sim:      {markets_per_sim}")
    print(f"  Starting Balance: ${starting_balance:.2f}")

    print(f"\nWin Rate:")
    print(f"  Average:          {summary['avg_win_rate']:.2%}")
    print(f"  Std Deviation:    {summary['win_rate_std']:.2%}")

    print(f"\nReturns:")
    print(f"  Average Return:   {summary['avg_return']:.2%}")
    print(f"  Std Deviation:    {summary['return_std']:.2%}")
    print(f"  Min Return:       {summary['min_return']:.2%}")
    print(f"  Max Return:       {summary['max_return']:.2%}")

    print(f"\nProfit:")
    print(f"  Average Profit:   ${summary['avg_profit']:.2f}")
    print(f"  Avg Trades/Sim:   {summary['avg_trades']:.1f}")

    print("\n" + "=" * 60)

    return summary


def main():
    parser = argparse.ArgumentParser(description='Backtest Arbitrage Strategy')
    parser.add_argument('--num-simulations', type=int, default=100,
                        help='Number of Monte Carlo simulations')
    parser.add_argument('--markets-per-sim', type=int, default=1000,
                        help='Markets per simulation')
    parser.add_argument('--starting-balance', type=float, default=1000.0,
                        help='Starting balance for simulation')
    parser.add_argument('--single-run', action='store_true',
                        help='Run single backtest instead of Monte Carlo')

    args = parser.parse_args()

    if args.single_run:
        backtester = ArbitrageBacktester(starting_balance=args.starting_balance)
        markets = backtester.generate_simulated_markets(args.markets_per_sim)
        result = backtester.run_backtest(markets)
        backtester.print_results(result)
    else:
        run_monte_carlo(
            num_simulations=args.num_simulations,
            markets_per_sim=args.markets_per_sim,
            starting_balance=args.starting_balance
        )


if __name__ == "__main__":
    main()
