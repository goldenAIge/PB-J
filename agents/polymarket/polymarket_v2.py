"""Polymarket CLOB V2 API wrapper.

Side-by-side with polymarket.py (V1). Each bot migrates independently by
importing from this module instead of polymarket.py. After all bots are
migrated and V1 is deprecated, polymarket.py can be removed.

V2 differences from V1:
  - Host: clob-v2.polymarket.com (pre-cutover), clob.polymarket.com (post Apr 28)
  - Collateral: pUSD (0xC011...2DFB) instead of USDC.e (0x2791...4174)
  - get_balance_allowance() requires explicit AssetType
  - cancel_order() takes OrderPayload(orderID=...) instead of bare string
  - get_order_book() returns a dict, not an OrderBookSummary object
  - Side enum: Side.BUY / Side.SELL (IntEnum) instead of BUY/SELL string constants

Layer 1: Read operations + setup (no order placement yet).
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3

try:
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as ExtraDataToPOAMiddleware

from py_clob_client_v2 import (
    ClobClient,
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
)

load_dotenv()

logger = logging.getLogger(__name__)

# --- Constants ---

V2_HOST = "https://clob-v2.polymarket.com"
CHAIN_ID = 137

# pUSD is the V2 collateral token (replaces USDC.e from V1)
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Minimal ERC-20 ABI for balance queries
_ERC20_BALANCE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class Polymarket:
    """Polymarket CLOB V2 client wrapper.

    Provides the same method signatures as polymarket.py (V1) so bots can
    swap imports without changing their calling code.
    """

    def __init__(
        self,
        private_key: str = None,
        api_key: str = None,
        api_secret: str = None,
        api_passphrase: str = None,
    ):
        self.chain_id = CHAIN_ID
        self.private_key = private_key or os.getenv("POLYGON_WALLET_PRIVATE_KEY")

        # V2 CLOB client
        self.clob_url = V2_HOST

        creds = None
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

        self.client = ClobClient(
            self.clob_url,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds,
        )

        # Derive API credentials if not provided
        if self.private_key:
            self._init_api_keys(creds)

        # Web3 for on-chain balance reads
        self.polygon_rpc = os.getenv(
            "POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"
        )
        self.web3 = Web3(Web3.HTTPProvider(self.polygon_rpc))
        self.web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        # pUSD contract for balance queries
        self.pusd = self.web3.eth.contract(
            address=Web3.to_checksum_address(PUSD_ADDRESS),
            abi=_ERC20_BALANCE_ABI,
        )

    def _init_api_keys(self, existing_creds: ApiCreds = None) -> None:
        """Derive or reuse API credentials for L2 auth."""
        if existing_creds:
            self.client.set_api_creds(existing_creds)
            return
        creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds)

    # --- Read operations ---

    def get_address_for_private_key(self) -> str:
        """Return the wallet address derived from the private key."""
        return self.web3.eth.account.from_key(str(self.private_key)).address

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch the full order book for a token.

        Note: V2 returns a plain dict with 'bids' and 'asks' lists,
        not an OrderBookSummary object like V1.
        """
        return self.client.get_order_book(token_id)

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get the best (lowest) ask price from the CLOB order book.

        Returns None if the book has no asks or the request fails.
        """
        try:
            book = self.client.get_order_book(token_id)
            asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
            if asks:
                return min(float(a["price"] if isinstance(a, dict) else a.price) for a in asks)
        except Exception:
            logger.debug(f"Failed to fetch best ask for {token_id[:20]}...")
        return None

    def get_order(self, order_id: str) -> dict:
        """Get order status by order ID. Requires L2 auth."""
        return self.client.get_order(order_id)

    def get_usdc_balance(self) -> float:
        """Get the pUSD balance for this wallet.

        SEMANTIC CHANGE from V1: V1 returned USDC.e balance. V2 returns pUSD
        balance because pUSD is the collateral token for V2 CLOB. Method name
        kept as get_usdc_balance() for caller compatibility.
        """
        # TODO post-April-28: Rename to get_pusd_balance() and update all callers.
        # Keeping V1 name for caller compatibility during migration.
        wallet = Web3.to_checksum_address(self.get_address_for_private_key())
        balance_raw = self.pusd.functions.balanceOf(wallet).call()
        return float(balance_raw / 1e6)  # 6 decimals

    def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> dict:
        """Query V2 CLOB balance/allowance state.

        V2 requires explicit asset_type (COLLATERAL or CONDITIONAL).
        """
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL,
        )
        return self.client.get_balance_allowance(params)
