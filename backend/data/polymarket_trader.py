"""
Polymarket live-trading client.

Wraps py-clob-client so the rest of the bot can call a uniform
`place_order(...)` regardless of whether we are in sim or live mode.

Live mode kicks in only when:
  * settings.SIMULATION_MODE is False, AND
  * all four creds (api_key/secret/passphrase + private_key) are set, AND
  * py-clob-client is importable and the client can authenticate.

If any of those is missing the helpers return None and the caller should
fall back to its existing simulation path.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from backend.config import settings

logger = logging.getLogger("trading_bot")


@dataclass
class LiveOrderResult:
    """Outcome of placing a live order."""
    order_id: str
    success: bool
    filled_size: float       # number of shares actually filled
    filled_notional: float   # USDC actually spent
    avg_price: float         # average fill price (0–1)
    status: str              # "matched" | "partial" | "rejected" | "error"
    error: Optional[str] = None
    raw: Optional[dict] = None


def live_trading_enabled() -> bool:
    """True only if every cred is present AND sim mode is off."""
    if settings.SIMULATION_MODE:
        return False
    creds = (
        settings.POLYMARKET_API_KEY,
        settings.POLYMARKET_API_SECRET,
        settings.POLYMARKET_API_PASSPHRASE,
        settings.POLYMARKET_PRIVATE_KEY,
    )
    return all(c for c in creds)


class PolymarketTrader:
    """
    Singleton-ish wrapper around py-clob-client.

    Instantiated lazily on first use. Holds the client across calls so we
    don't reauth on every order. Thread-safe init.
    """

    _instance: Optional["PolymarketTrader"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # Lazy import — keeps sim mode working without the SDK installed.
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        host = settings.POLYMARKET_HOST
        chain_id = settings.POLYMARKET_CHAIN_ID
        creds = ApiCreds(
            api_key=settings.POLYMARKET_API_KEY or "",
            api_secret=settings.POLYMARKET_API_SECRET or "",
            api_passphrase=settings.POLYMARKET_API_PASSPHRASE or "",
        )

        # signature_type=1 → EOA (externally owned account / direct wallet).
        # If using Polymarket's "Magic" smart wallet, this would be 2 with a
        # funder address. We default to EOA + optional funder override.
        client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=settings.POLYMARKET_PRIVATE_KEY or "",
            signature_type=2 if settings.POLYMARKET_FUNDER_ADDRESS else 1,
            funder=settings.POLYMARKET_FUNDER_ADDRESS or None,
        )
        client.set_api_creds(creds)
        self._client = client

        # Cached USDC balance (rate-limit RPC).
        self._balance_cache: Tuple[float, float] = (0.0, 0.0)  # (timestamp, balance_usd)
        self._balance_ttl = 15.0

    @classmethod
    def get(cls) -> Optional["PolymarketTrader"]:
        """Return the singleton, building it on first call. None if creds missing."""
        if not live_trading_enabled():
            return None
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is not None:
                return cls._instance
            try:
                cls._instance = cls()
                logger.info("Polymarket live trading client initialised.")
            except ImportError as e:
                logger.error(f"py-clob-client not installed; cannot enable live mode: {e}")
                return None
            except Exception as e:
                logger.error(f"Failed to initialise Polymarket client: {e}")
                return None
        return cls._instance

    # ----- balance -----

    def get_usdc_balance(self) -> float:
        """USDC balance in the connected wallet. Cached for 15s."""
        now = time.time()
        cached_ts, cached_val = self._balance_cache
        if now - cached_ts < self._balance_ttl:
            return cached_val
        try:
            # py-clob-client returns balance in USDC base units (6 decimals).
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self._client.get_balance_allowance(params)
            raw_balance = float(resp.get("balance", 0)) / 1_000_000.0
            self._balance_cache = (now, raw_balance)
            return raw_balance
        except Exception as e:
            logger.warning(f"Failed to fetch USDC balance: {e}")
            return cached_val  # return stale value rather than 0 to avoid sizing crashes

    # ----- orders -----

    def place_order(
        self,
        *,
        token_id: str,
        side: str,        # "BUY" or "SELL"
        price: float,     # 0..1 limit price
        size_usd: float,  # dollar amount to spend (we convert to shares internally)
    ) -> LiveOrderResult:
        """
        Place a FAK (fill-and-kill) limit order. The unfilled remainder is
        cancelled immediately, so we never end up with a resting order.

        Returns LiveOrderResult — caller persists order_id and reconciles.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        # Enforce live caps independent of the per-strategy cap
        size_usd = min(size_usd, settings.LIVE_TRADE_MAX_USD)

        if price <= 0 or price >= 1:
            return LiveOrderResult(
                order_id="", success=False, filled_size=0.0, filled_notional=0.0,
                avg_price=0.0, status="rejected", error=f"invalid price {price}",
            )
        if size_usd <= 0:
            return LiveOrderResult(
                order_id="", success=False, filled_size=0.0, filled_notional=0.0,
                avg_price=0.0, status="rejected", error="size_usd must be > 0",
            )

        # Polymarket orders are sized in shares (the YES/NO outcome token).
        # 1 share pays $1 on resolution if correct.
        shares = round(size_usd / price, 2)  # CLOB requires 2-decimal precision

        try:
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY if side.upper() == "BUY" else SELL,
            )
            signed = self._client.create_order(args)
            # OrderType.FAK = fill-and-kill (partial fills accepted, rest cancelled).
            resp = self._client.post_order(signed, OrderType.FAK)
        except Exception as e:
            logger.error(f"Polymarket order placement failed: {e}")
            return LiveOrderResult(
                order_id="", success=False, filled_size=0.0, filled_notional=0.0,
                avg_price=0.0, status="error", error=str(e),
            )

        # Response shape (varies; defensive parse):
        # { success: true, errorMsg: "", orderID: "0x...",
        #   transactionsHashes: [...], status: "matched"|"live"|"unmatched" }
        order_id = str(resp.get("orderID") or resp.get("order_id") or "")
        status = str(resp.get("status") or ("matched" if resp.get("success") else "rejected"))

        # Try to read realised fill if reported (Polymarket returns size_matched in some responses).
        filled_shares = float(resp.get("size_matched", 0) or 0)
        if filled_shares == 0 and status in ("matched",):
            filled_shares = shares
        filled_notional = filled_shares * price
        avg_price = price  # FAK at our limit → effective price is at or better

        return LiveOrderResult(
            order_id=order_id,
            success=bool(resp.get("success", status in ("matched", "live"))),
            filled_size=filled_shares,
            filled_notional=filled_notional,
            avg_price=avg_price,
            status=status,
            error=resp.get("errorMsg") or None,
            raw=resp,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Best-effort cancel. FAK orders never rest, so this is mainly for safety."""
        try:
            resp = self._client.cancel(order_id=order_id)
            return bool(resp.get("canceled") or resp.get("success"))
        except Exception as e:
            logger.warning(f"Cancel failed for {order_id}: {e}")
            return False
