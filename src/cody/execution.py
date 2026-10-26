"""Execution: latency, slippage, fees, and the live-adapter boundary.

Backtest and paper share this model so that a fill is computed the same way in
both. Live adapters implement the same `Executor` protocol; the only difference
is where the price comes from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import ExecutionConfig
from .models import Fill, Order, Side, TokenSnapshot


def compute_slippage_bps(notional_usd: float, liquidity_usd: float,
                         config: ExecutionConfig) -> float:
    """Base spread plus a linear price-impact term.

    Taking 1% of a pool costs `impact_coeff` extra bps; taking 10% costs 10x
    that. This is the term that makes a "profitable" strategy that trades size
    into thin liquidity visibly unprofitable.
    """
    if liquidity_usd <= 0:
        return 10_000.0  # untradeable
    frac = notional_usd / liquidity_usd
    return config.base_slippage_bps + config.impact_coeff * frac * 10_000.0


@dataclass
class ExecutionModel:
    config: ExecutionConfig

    def execute(self, order: Order, fill_snap: TokenSnapshot,
                latency_s: float | None = None) -> Fill:
        lat = self.config.latency_s if latency_s is None else latency_s
        slip_bps = compute_slippage_bps(
            order.notional_usd, fill_snap.liquidity_usd, self.config)
        slip = slip_bps / 10_000.0
        if order.side is Side.BUY:
            price = fill_snap.price * (1.0 + slip)
        else:
            price = max(0.0, fill_snap.price * (1.0 - slip))
        qty = order.notional_usd / price if price > 0 else 0.0
        fee = order.notional_usd * self.config.taker_fee_bps / 10_000.0
        return Fill(
            order=order,
            ts=order.ts + lat,
            price=price,
            qty=qty,
            notional_usd=order.notional_usd,
            fee_usd=fee,
            slippage_bps=slip_bps,
            latency_s=lat,
        )


class Executor(Protocol):
    """Anything that can turn an Order into a Fill.

    Live adapters (Jupiter, Raydium, a CEX, ...) implement this. The engine does
    not care which one it is holding.
    """

    def execute(self, order: Order, fill_snap: TokenSnapshot,
                latency_s: float | None = None) -> Fill: ...
