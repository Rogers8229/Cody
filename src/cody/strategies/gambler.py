"""Bot one: the Gambler.

Scans fresh launches, applies contract gates, and trades direction. Edge is
being right about which coin moves. It dies on a streak of wrong reads sized up
by previous wins — so sizing here is a fixed fraction of the current book and
exits are mechanical.
"""
from __future__ import annotations

from ..config import GamblerConfig, RiskConfig
from ..models import ExitReason, Order, Side, StrategyId
from ..portfolio import fixed_fraction_size
from .base import Strategy, StrategyContext


class GamblerStrategy(Strategy):
    id = StrategyId.GAMBLER

    def __init__(self, cfg: GamblerConfig, risk: RiskConfig) -> None:
        self.cfg = cfg
        self.risk = risk

    def _score(self, snap) -> float:
        surge = min(snap.volume_surge / 3.0, 1.0)
        momentum = min(max(snap.change_5m, 0.0) / 0.15, 1.0)
        return (0.45 * snap.buy_pressure
                + 0.30 * surge
                + 0.25 * momentum)

    def entries(self, ctx: StrategyContext) -> list[Order]:
        orders: list[Order] = []
        eq = ctx.portfolio.equity(ctx.prices)
        for mint, snap in ctx.snapshots.items():
            if ctx.portfolio.has_position(mint):
                continue
            if not snap.age_ok(self.cfg.launch_window_s):
                continue
            if snap.buy_pressure < self.cfg.min_buy_pressure:
                continue
            if snap.volume_surge < self.cfg.min_volume_surge:
                continue
            if snap.change_5m < self.cfg.min_change_5m:
                continue
            score = self._score(snap)
            if score < self.cfg.min_score:
                continue
            notional = fixed_fraction_size(
                equity=eq,
                risk_per_trade_pct=self.risk.risk_per_trade_pct,
                max_position_pct=self.risk.max_position_pct,
                liquidity_usd=snap.liquidity_usd,
                max_liquidity_frac=self.risk.max_liquidity_frac,
            )
            if notional < 10.0:
                continue
            orders.append(Order(
                strategy=self.id, mint=mint, symbol=snap.symbol,
                side=Side.BUY, notional_usd=notional, ts=ctx.ts,
                ref_price=snap.price,
                reason=f"launch momentum score={score:.2f}",
                meta={"score": score, "buy_pressure": snap.buy_pressure,
                      "volume_surge": snap.volume_surge,
                      "change_5m": snap.change_5m},
            ))
        return orders

    def exits(self, ctx: StrategyContext) -> list[Order]:
        orders: list[Order] = []
        for mint, pos in list(ctx.portfolio.positions.items()):
            snap = ctx.snapshots.get(mint)
            if snap is None:
                continue
            price = snap.price
            pos.update_peak(price)
            reason = None
            if price <= pos.stop_price:
                reason = ExitReason.STOP
            elif price >= pos.take_profit_price:
                reason = ExitReason.TAKE_PROFIT
            elif (pos.peak_price > pos.avg_price
                  and price <= pos.peak_price * (1.0 - self.risk.trailing_stop_pct)):
                reason = ExitReason.TRAILING
            elif ctx.ts - pos.opened_ts >= self.risk.max_hold_s:
                reason = ExitReason.MAX_HOLD
            if reason is None:
                continue
            orders.append(Order(
                strategy=self.id, mint=mint, symbol=pos.symbol,
                side=Side.SELL,
                notional_usd=pos.market_value(price),
                ts=ctx.ts, ref_price=price,
                reason=f"exit {reason.value}",
                meta={"exit_reason": reason.value},
            ))
        return orders
