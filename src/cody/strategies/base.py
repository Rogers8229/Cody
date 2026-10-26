"""Strategy interface shared by Gambler, House, and Spy."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ClosedTrade, Order, StrategyId, TokenSnapshot
from ..portfolio import Portfolio
from ..wallets import LeaderAction


@dataclass
class StrategyContext:
    ts: float
    snapshots: dict[str, TokenSnapshot]
    portfolio: Portfolio
    prices: dict[str, float]
    feed_age_s: float
    leader_actions: list[LeaderAction] = field(default_factory=list)


class Strategy:
    id: StrategyId

    def entries(self, ctx: StrategyContext) -> list[Order]:
        return []

    def exits(self, ctx: StrategyContext) -> list[Order]:
        return []

    def on_tick(self, ctx: StrategyContext) -> None:
        """Optional hook for strategies with their own ledger (House)."""

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass

    def on_fill(self, order: Order, fill) -> None:
        pass
