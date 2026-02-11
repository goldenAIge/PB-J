"""
Telegram Alerts Module for Polymarket Trading Bot

Sends real-time notifications for:
- Trade executions
- Arbitrage opportunities found
- Sentiment signals
- Hourly PNL updates
- Error alerts

Setup:
1. Create a bot with @BotFather on Telegram
2. Get your bot token
3. Get your chat ID (send /start to @userinfobot)
4. Add to .env:
   TELEGRAM_BOT_TOKEN="your_bot_token"
   TELEGRAM_CHAT_ID="your_chat_id"

Usage:
    from agents.connectors.telegram_alerts import TelegramAlerter

    alerter = TelegramAlerter()
    await alerter.send_trade_alert(trade_data)
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Try to import telegram
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed - Telegram alerts disabled")


@dataclass
class TradeAlert:
    """Trade alert data"""
    action: str  # "BUY", "SELL", "ARBITRAGE"
    market: str
    side: str  # "YES", "NO"
    amount: float
    price: float
    status: str  # "EXECUTED", "PENDING", "FAILED", "DRY_RUN"
    profit_expected: Optional[float] = None
    sentiment: Optional[str] = None
    tx_hash: Optional[str] = None


@dataclass
class PNLUpdate:
    """PNL update data"""
    initial_balance: float
    current_balance: float
    total_trades: int
    winning_trades: int
    total_profit: float
    return_percent: float
    period: str  # "hourly", "daily", "weekly"


class TelegramAlerter:
    """
    Telegram alerting system for trading bot.

    Sends formatted messages to a Telegram chat for monitoring.
    """

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot = self._init_bot()
        self.enabled = self.bot is not None and self.chat_id is not None

        if self.enabled:
            logger.info("Telegram alerter initialized")
        else:
            logger.warning("Telegram alerter disabled - missing credentials")

    def _init_bot(self) -> Optional['Bot']:
        """Initialize Telegram bot"""
        if not TELEGRAM_AVAILABLE:
            return None

        if not self.bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set")
            return None

        try:
            bot = Bot(token=self.bot_token)
            return bot
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
            return None

    async def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram"""
        if not self.enabled:
            logger.debug(f"Telegram disabled, would send: {message[:100]}...")
            return False

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=parse_mode
            )
            return True
        except TelegramError as e:
            logger.error(f"Telegram send error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return False

    def send_message_sync(self, message: str, parse_mode: str = "HTML") -> bool:
        """Synchronous wrapper for send_message"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create a new task if loop is already running
                asyncio.create_task(self.send_message(message, parse_mode))
                return True
            else:
                return loop.run_until_complete(self.send_message(message, parse_mode))
        except RuntimeError:
            # No event loop, create one
            return asyncio.run(self.send_message(message, parse_mode))

    async def send_trade_alert(self, trade: TradeAlert) -> bool:
        """Send a trade execution alert"""
        emoji = self._get_status_emoji(trade.status)
        action_emoji = "🟢" if trade.action == "BUY" else "🔴"

        message = f"""
{emoji} <b>TRADE {trade.status}</b>

{action_emoji} <b>{trade.action} {trade.side}</b>
📊 Market: {trade.market[:50]}...
💰 Amount: ${trade.amount:.2f}
📈 Price: ${trade.price:.4f}
"""

        if trade.profit_expected:
            message += f"🎯 Expected Profit: ${trade.profit_expected:.2f}\n"

        if trade.sentiment:
            message += f"📡 Sentiment: {trade.sentiment}\n"

        if trade.tx_hash and trade.tx_hash != "pending_implementation":
            message += f"🔗 TX: <code>{trade.tx_hash[:20]}...</code>\n"

        message += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        return await self.send_message(message)

    async def send_opportunity_alert(self, opportunity_type: str, market: str,
                                      details: dict) -> bool:
        """Send an opportunity found alert"""
        if opportunity_type == "arbitrage":
            emoji = "⚡"
            message = f"""
{emoji} <b>ARBITRAGE OPPORTUNITY</b>

📊 {market[:50]}...
💵 YES: ${details.get('yes_price', 0):.4f}
💵 NO: ${details.get('no_price', 0):.4f}
📊 Combined: ${details.get('combined', 0):.4f}
💰 Potential Profit: {details.get('profit_percent', 0):.2%}
"""
        elif opportunity_type == "sentiment":
            emoji = "📡"
            signal = details.get('signal', 'unknown')
            signal_emoji = "🟢" if 'bullish' in signal.lower() else "🔴" if 'bearish' in signal.lower() else "⚪"

            message = f"""
{emoji} <b>SENTIMENT OPPORTUNITY</b>

📊 {market[:50]}...
{signal_emoji} Signal: {signal.upper()}
📈 Mismatch: {details.get('mismatch', 0):.1f}%
🎯 Recommended: {details.get('side', 'N/A')}
📊 Sources: {details.get('sources', 'N/A')}
"""
        else:
            message = f"🔔 <b>OPPORTUNITY: {opportunity_type}</b>\n{market}"

        message += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        return await self.send_message(message)

    async def send_pnl_update(self, pnl: PNLUpdate) -> bool:
        """Send periodic PNL update"""
        profit_emoji = "📈" if pnl.total_profit >= 0 else "📉"
        return_emoji = "🟢" if pnl.return_percent >= 0 else "🔴"

        win_rate = pnl.winning_trades / pnl.total_trades * 100 if pnl.total_trades > 0 else 0

        message = f"""
📊 <b>{pnl.period.upper()} PNL UPDATE</b>

💰 Balance: ${pnl.current_balance:.2f}
{profit_emoji} P&L: ${pnl.total_profit:+.2f}
{return_emoji} Return: {pnl.return_percent:+.2%}

📈 Trades: {pnl.total_trades}
✅ Win Rate: {win_rate:.1f}%

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        return await self.send_message(message)

    async def send_error_alert(self, error_type: str, message: str,
                                details: str = None) -> bool:
        """Send error/warning alert"""
        alert_message = f"""
🚨 <b>ERROR: {error_type}</b>

{message}
"""
        if details:
            alert_message += f"\n<code>{details[:200]}</code>"

        alert_message += f"\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        return await self.send_message(alert_message)

    async def send_startup_message(self, mode: str, config: dict) -> bool:
        """Send bot startup notification"""
        message = f"""
🤖 <b>TRADING BOT STARTED</b>

📍 Mode: {mode.upper()}
💰 Balance: ${config.get('balance', 0):.2f}
⚙️ Max Trade: {config.get('max_trade_percent', 0):.0%}
🛡️ Max Drawdown: {config.get('max_drawdown', 0):.0%}
⏱️ Scan Interval: {config.get('scan_interval', 0)}s

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        return await self.send_message(message)

    async def send_shutdown_message(self, reason: str, stats: dict) -> bool:
        """Send bot shutdown notification"""
        message = f"""
🛑 <b>TRADING BOT STOPPED</b>

📍 Reason: {reason}
📈 Total Trades: {stats.get('total_trades', 0)}
💰 Final Balance: ${stats.get('balance', 0):.2f}
📊 Total P&L: ${stats.get('total_pnl', 0):+.2f}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        return await self.send_message(message)

    def _get_status_emoji(self, status: str) -> str:
        """Get emoji for trade status"""
        emojis = {
            "EXECUTED": "✅",
            "PENDING": "⏳",
            "FAILED": "❌",
            "DRY_RUN": "🧪",
        }
        return emojis.get(status, "📝")


class AlertManager:
    """
    Manages alerts across the trading system.

    Provides synchronous methods for easy integration.
    """

    def __init__(self):
        self.telegram = TelegramAlerter()
        self._loop = None

    def _get_loop(self):
        """Get or create event loop"""
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def _run_async(self, coro):
        """Run async coroutine"""
        loop = self._get_loop()
        if loop.is_running():
            # Schedule for later execution
            asyncio.ensure_future(coro)
            return True
        return loop.run_until_complete(coro)

    def trade_executed(self, action: str, market: str, side: str, amount: float,
                       price: float, status: str, **kwargs) -> bool:
        """Send trade execution alert"""
        trade = TradeAlert(
            action=action,
            market=market,
            side=side,
            amount=amount,
            price=price,
            status=status,
            profit_expected=kwargs.get('profit_expected'),
            sentiment=kwargs.get('sentiment'),
            tx_hash=kwargs.get('tx_hash')
        )
        return self._run_async(self.telegram.send_trade_alert(trade))

    def opportunity_found(self, opp_type: str, market: str, **details) -> bool:
        """Send opportunity alert"""
        return self._run_async(
            self.telegram.send_opportunity_alert(opp_type, market, details)
        )

    def pnl_update(self, initial: float, current: float, trades: int,
                   winning: int, profit: float, period: str = "hourly") -> bool:
        """Send PNL update"""
        return_pct = (current - initial) / initial if initial > 0 else 0
        pnl = PNLUpdate(
            initial_balance=initial,
            current_balance=current,
            total_trades=trades,
            winning_trades=winning,
            total_profit=profit,
            return_percent=return_pct,
            period=period
        )
        return self._run_async(self.telegram.send_pnl_update(pnl))

    def error(self, error_type: str, message: str, details: str = None) -> bool:
        """Send error alert"""
        return self._run_async(
            self.telegram.send_error_alert(error_type, message, details)
        )

    def startup(self, mode: str, balance: float, config: dict) -> bool:
        """Send startup message"""
        config['balance'] = balance
        return self._run_async(self.telegram.send_startup_message(mode, config))

    def shutdown(self, reason: str, total_trades: int, balance: float, pnl: float) -> bool:
        """Send shutdown message"""
        stats = {
            'total_trades': total_trades,
            'balance': balance,
            'total_pnl': pnl
        }
        return self._run_async(self.telegram.send_shutdown_message(reason, stats))


# Global alert manager instance
alerts = AlertManager()


def test_alerts():
    """Test Telegram alerts"""
    import asyncio

    async def run_tests():
        alerter = TelegramAlerter()

        if not alerter.enabled:
            print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            print("\nTo set up Telegram alerts:")
            print("1. Message @BotFather on Telegram")
            print("2. Send /newbot and follow instructions")
            print("3. Copy the bot token")
            print("4. Message @userinfobot to get your chat ID")
            print("5. Add to .env:")
            print('   TELEGRAM_BOT_TOKEN="your_token"')
            print('   TELEGRAM_CHAT_ID="your_chat_id"')
            return

        print("Testing Telegram alerts...")

        # Test startup
        await alerter.send_startup_message("DRY_RUN", {
            'balance': 100.0,
            'max_trade_percent': 0.01,
            'max_drawdown': 0.05,
            'scan_interval': 30
        })

        # Test trade alert
        await alerter.send_trade_alert(TradeAlert(
            action="BUY",
            market="Will BTC reach $100k by end of 2025?",
            side="YES",
            amount=10.0,
            price=0.45,
            status="DRY_RUN",
            profit_expected=0.50,
            sentiment="bullish"
        ))

        # Test PNL update
        await alerter.send_pnl_update(PNLUpdate(
            initial_balance=100.0,
            current_balance=105.0,
            total_trades=5,
            winning_trades=4,
            total_profit=5.0,
            return_percent=0.05,
            period="hourly"
        ))

        print("Test alerts sent! Check your Telegram.")

    asyncio.run(run_tests())


if __name__ == "__main__":
    test_alerts()
