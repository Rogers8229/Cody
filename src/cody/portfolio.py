"""Portfolio book and position sizing.

The single most important invariant in this file: **size is a fixed fraction of
the current book, never a function of the last trade.** The Gambler failure mode
in the desk write-up is a martingale wearing a hoodie; the sizing function here
is the thing that makes that structurally impossible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    ClosedTrade,
    ExitReason,
    Fill,
    Position,
    Side,
    StrategyId,
)


def fixed_fraction_size(
    equity: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    liquidity_usd: float,
    max_liquidity_frac: float,
) -> float:
    """Return the USD notional for one new position.

    Three independent caps, all functions of *current* state only:

    1. risk budget: equity * risk_per_trade_pct
    2. concentration cap: equity * max_position_pct
    3. liquidity cap: pool_liquidity * max_liquidity_frac

    There is no argument for "last trade result" anywhere in the signature. That
    is intentional and is enforced by test_sizing.py.
    """
    if equity <= 0:
        return 0.0
    risk_budget = equity * risk_per_trade_pct
    concentration_cap = equity * max_position_pct
    liquidity_cap = max(0.0, liquidity_usd) * max_liquidity_frac
    return max(0.0, min(risk_budget, concentration_cap, liquidity_cap))


@dataclass
class Portfolio:
    """A single strategy's book. Strategies are capital-partitioned so that one
    bot's blast radius cannot reach another bot's capital."""

    strategy: StrategyId
    cash: float
    starting_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)
    fees_paid: float = 0.0
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    locked_capital: float = 0.0  # seed liquidity deployed, recoverable

    def __post_init__(self) -> None:
        if self.peak_equity <= 0:
            self.peak_equity = self.starting_equity
        if self.day_start_equity <= 0:
            self.day_start_equity = self.starting_equity

    # -- valuation ---------------------------------------------------------
    def equity(self, prices: dict[str, float]) -> float:
        mv = sum(p.market_value(prices.get(m, p.avg_price))
                 for m, p in self.positions.items())
        return self.cash + mv + self.locked_capital

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(m, p.avg_price))
                   for m, p in self.positions.items())

    def has_position(self, mint: str) -> bool:
        return mint in self.positions

    def wallet_exposure(self, wallet: str, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(m, p.avg_price))
                   for m, p in self.positions.items() if p.wallet == wallet)

    # -- lifecycle ---------------------------------------------------------
    def apply_buy(self, fill: Fill, stop_price: float, tp_price: float,
                  wallet: Optional[str] = None,
                  leader_entry_price: Optional[float] = None) -> Position:
        cost = fill.notional_usd + fill.fee_usd
        if cost > self.cash + 1e-9:
            raise ValueError("insufficient cash for fill")
        self.cash -= cost
        self.fees_paid += fill.fee_usd
        pos = Position(
            mint=fill.order.mint,
            symbol=fill.order.symbol,
            strategy=self.strategy,
            qty=fill.qty,
            avg_price=fill.price,
            opened_ts=fill.ts,
            entry_notional=fill.notional_usd,
            peak_price=fill.price,
            stop_price=stop_price,
            take_profit_price=tp_price,
            wallet=wallet,
            leader_entry_price=leader_entry_price,
        )
        self.positions[pos.mint] = pos
        return pos

    def apply_sell(self, mint: str, fill: Fill,
                   reason: ExitReason) -> ClosedTrade:
        pos = self.positions.pop(mint)
        proceeds = fill.notional_usd - fill.fee_usd
        self.cash += proceeds
        self.fees_paid += fill.fee_usd
        pnl = (fill.price - pos.avg_price) * pos.qty - fill.fee_usd
        trade = ClosedTrade(
            mint=pos.mint,
            symbol=pos.symbol,
            strategy=self.strategy,
            qty=pos.qty,
            entry_price=pos.avg_price,
            exit_price=fill.price,
            entry_ts=pos.opened_ts,
            exit_ts=fill.ts,
            pnl_usd=pnl,
            pnl_pct=(fill.price / pos.avg_price - 1.0) if pos.avg_price else 0.0,
            fees_usd=fill.fee_usd,
            exit_reason=reason,
            wallet=pos.wallet,
            meta=pos.meta,
        )
        self.closed.append(trade)
        return trade

    # -- accounting --------------------------------------------------------
    def mark(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        return eq

    def drawdown(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        if self.peak_equity <= 0:
            return 0.0
        return 1.0 - eq / self.peak_equity

    def daily_pnl_pct(self, prices: dict[str, float]) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.equity(prices) / self.day_start_equity - 1.0

    def realized_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.closed)

    def reset_day(self, prices: dict[str, float]) -> None:
        self.day_start_equity = self.equity(prices)


def split_capital(total: float, weights: dict[StrategyId, float]
                  ) -> dict[StrategyId, float]:
    """Partition starting capital across enabled strategies."""
    s = sum(weights.values())
    if s <= 0:
        return {}
    return {k: total * v / s for k, v in weights.items()}
