"""
Telegram Command Bot for Polymarket Trading

Responds to commands:
- /status - Show bot status and balance
- /positions - Show current positions
- /orders - Show open orders
- /pnl - Show profit/loss summary

Run this alongside your trading bots to enable Telegram commands.
"""

import os
import asyncio
import logging
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.error("python-telegram-bot not installed")

from py_clob_client.client import ClobClient


class TelegramCommandBot:
    """Telegram bot that responds to commands for checking positions/balance"""

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")

        # Initialize Polymarket client
        self.clob_client = None
        self._init_clob_client()

    def _init_clob_client(self):
        """Initialize the CLOB client"""
        try:
            self.clob_client = ClobClient(
                "https://clob.polymarket.com",
                key=self.private_key,
                chain_id=137
            )
            self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())
            logger.info("CLOB client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")

    def get_positions(self) -> list:
        """Get current positions from Polymarket"""
        try:
            # Get balances (positions)
            positions = self.clob_client.get_balance_allowance()
            return positions if positions else []
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_open_orders(self) -> list:
        """Get open orders from Polymarket"""
        try:
            orders = self.clob_client.get_orders()
            return orders if orders else []
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []

    def get_usdc_balance(self) -> float:
        """Get USDC.e balance"""
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")))
            account = w3.eth.account.from_key(self.private_key)
            wallet = account.address

            usdc_e = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
            erc20_abi = [{'constant': True, 'inputs': [{'name': 'account', 'type': 'address'}], 'name': 'balanceOf', 'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function'}]

            usdc = w3.eth.contract(address=usdc_e, abi=erc20_abi)
            balance = usdc.functions.balanceOf(wallet).call()
            return balance / 1e6
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if str(update.effective_chat.id) != self.chat_id:
            return  # Ignore messages from other chats

        try:
            balance = self.get_usdc_balance()
            orders = self.get_open_orders()

            message = f"""
📊 <b>BOT STATUS</b>

💰 <b>USDC.e Balance:</b> ${balance:.2f}
📋 <b>Open Orders:</b> {len(orders)}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            await update.message.reply_text(message, parse_mode='HTML')
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    def _load_strategy_map(self) -> dict:
        """Load trade logs and build a map of market titles to strategies"""
        strategy_map = {}  # title -> strategy (ARB or SCA)

        # Load arbitrage trades
        try:
            with open('trades.log', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('{'):
                        try:
                            trade = json.loads(line)
                            question = trade.get('question', '')
                            if question:
                                strategy_map[question.lower()[:40]] = 'ARB'
                        except:
                            pass
        except FileNotFoundError:
            pass

        # Load scalper trades
        try:
            with open('resolution_scalp_trades.log', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('{'):
                        try:
                            trade = json.loads(line)
                            question = trade.get('question', '')
                            if question:
                                strategy_map[question.lower()[:40]] = 'SCA'
                        except:
                            pass
        except FileNotFoundError:
            pass

        return strategy_map

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /positions command - shows current holdings"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        try:
            import httpx
            from web3 import Web3

            # Get wallet address
            w3 = Web3()
            account = w3.eth.account.from_key(self.private_key)
            wallet = account.address.lower()

            # Load strategy map from trade logs
            strategy_map = self._load_strategy_map()

            # Fetch current positions from data API
            response = httpx.get(
                f"https://data-api.polymarket.com/positions?user={wallet}",
                timeout=15
            )

            if response.status_code != 200:
                await update.message.reply_text("Error fetching positions.")
                return

            positions = response.json()

            if not positions:
                await update.message.reply_text("📭 No active positions.")
                return

            message = "📈 <b>CURRENT POSITIONS</b>\n\n"
            total_value = 0
            total_pnl = 0

            for i, pos in enumerate(positions[:10]):
                title = pos.get('title', 'Unknown')
                title_display = title[:35]
                outcome = pos.get('outcome', '?')
                size = float(pos.get('size', 0))
                cur_price = float(pos.get('curPrice', 0))
                pnl = float(pos.get('cashPnl', 0))
                end_date = pos.get('endDate', '')
                redeemable = pos.get('redeemable', False)

                current_value = size * cur_price
                total_value += current_value
                total_pnl += pnl

                # Look up strategy from trade logs
                strategy = strategy_map.get(title.lower()[:40], '?')
                strategy_tag = f"[{strategy}]" if strategy != '?' else ""

                # Format resolution date
                date_str = ""
                if end_date:
                    try:
                        from datetime import datetime as dt
                        end_dt = dt.fromisoformat(end_date.replace('Z', '+00:00'))
                        date_str = f" | 📅 {end_dt.strftime('%b %d')}"
                    except:
                        pass

                status = "✅ REDEEMABLE" if redeemable else ""
                pnl_emoji = "📈" if pnl >= 0 else "📉"

                message += f"<b>{i+1}. {strategy_tag} {title_display}...</b>\n"
                message += f"   {outcome} | {size:.2f} shares @ ${cur_price:.3f}{date_str}\n"
                message += f"   {pnl_emoji} P&L: ${pnl:+.2f} {status}\n\n"

            message += f"<b>Total Value:</b> ${total_value:.2f}\n"
            message += f"<b>Total P&L:</b> ${total_pnl:+.2f}\n"
            message += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await update.message.reply_text(message, parse_mode='HTML')
        except Exception as e:
            await update.message.reply_text(f"Error getting positions: {e}")

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history command - shows trade history"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        try:
            trades = self.clob_client.get_trades() if self.clob_client else []

            if not trades:
                await update.message.reply_text("📭 No trade history found.")
                return

            message = "📜 <b>TRADE HISTORY</b>\n\n"
            for i, trade in enumerate(trades[:10]):
                side = trade.get('side', 'N/A')
                price = float(trade.get('price', 0))
                size = float(trade.get('size', 0))

                side_emoji = "🟢" if side.upper() == "BUY" else "🔴"
                message += f"{i+1}. {side_emoji} {side} | {size:.2f} shares @ ${price:.4f}\n"

            message += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await update.message.reply_text(message, parse_mode='HTML')
        except Exception as e:
            await update.message.reply_text(f"Error getting history: {e}")

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /orders command"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        try:
            orders = self.get_open_orders()

            if not orders:
                await update.message.reply_text("📭 No open orders.")
                return

            message = "📋 <b>OPEN ORDERS</b>\n\n"
            for i, order in enumerate(orders[:10]):
                side = order.get('side', 'N/A')
                price = order.get('price', 'N/A')
                size = order.get('original_size', 'N/A')
                status = order.get('status', 'N/A')
                message += f"{i+1}. {side} | Size: {size} @ ${price} | {status}\n"

            message += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await update.message.reply_text(message, parse_mode='HTML')
        except Exception as e:
            await update.message.reply_text(f"Error getting orders: {e}")

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pnl command"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        try:
            usdc_balance = self.get_usdc_balance()
            initial = 298.0  # $98 initial + $200 added ~Feb 10
            from web3 import Web3
            wallet = Web3().eth.account.from_key(str(self.private_key)).address

            # Get positions from data API (accurate P&L)
            import httpx
            positions_value = 0.0
            total_pnl = 0.0
            position_count = 0
            try:
                response = httpx.get(
                    f"https://data-api.polymarket.com/positions?user={wallet}",
                    timeout=15
                )
                if response.status_code == 200:
                    positions = response.json()
                    for pos in positions:
                        size = float(pos.get('size', 0))
                        cur_price = float(pos.get('curPrice', 0))
                        pnl = float(pos.get('cashPnl', 0))
                        if size > 0:
                            positions_value += size * cur_price
                            total_pnl += pnl
                            position_count += 1
            except:
                pass

            total_portfolio = usdc_balance + positions_value
            pnl_from_initial = total_portfolio - initial
            pnl_pct = (pnl_from_initial / initial) * 100 if initial > 0 else 0

            emoji = "📈" if pnl_from_initial >= 0 else "📉"

            message = f"""
📊 <b>PORTFOLIO SUMMARY</b>

💵 Initial Investment: ${initial:.2f}

<b>Current Holdings:</b>
💰 USDC.e (cash): ${usdc_balance:.2f}
📈 Positions ({position_count}): ${positions_value:.2f}
📊 <b>Total Portfolio:</b> ${total_portfolio:.2f}

<b>Performance:</b>
{emoji} P&L vs Initial: ${pnl_from_initial:+.2f} ({pnl_pct:+.1f}%)
📉 Positions P&L: ${total_pnl:+.2f}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            await update.message.reply_text(message, parse_mode='HTML')
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        if str(update.effective_chat.id) != self.chat_id:
            return

        message = """
🤖 <b>POLYMARKET BOT COMMANDS</b>

/status - Show balance and bot status
/positions - Show current holdings with resolution dates
/orders - Show open orders
/history - Show trade history
/pnl - Show profit/loss summary
/help - Show this message
"""
        await update.message.reply_text(message, parse_mode='HTML')

    def run(self):
        """Start the Telegram bot"""
        if not TELEGRAM_AVAILABLE:
            logger.error("python-telegram-bot not available")
            return

        if not self.bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not set")
            return

        print("Starting Telegram command bot...")
        print("Commands: /status, /positions, /orders, /pnl, /help")

        app = Application.builder().token(self.bot_token).build()

        # Add command handlers
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("start", self.cmd_help))

        # Start polling
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    """Run the Telegram command bot"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    bot = TelegramCommandBot()
    bot.run()


if __name__ == "__main__":
    main()
