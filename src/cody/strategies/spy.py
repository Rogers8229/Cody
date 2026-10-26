"""Bot three: the Spy.

Tracks profitable wallets, ranks them, mirrors their trades. Edge is borrowing
someone else's proven edge. It dies on latency: you copy the right wallet at the
wrong price.

Hard rules:
* max chase — skip any copy more than 15% above the leader's entry
* mirror exits, not only entries
* cap allocation per wallet
* demote a wallet after three losing copies in a row
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import RiskConfig, SpyConfig
from ..models import ExitReason, Order, Side, StrategyId
from ..portfolio import fixed_fraction_size
from ..wallets import LeaderAction, WalletTracker
from .base import Strategy, StrategyContext


@dataclass
class _PendingCopy:
    action: LeaderAction
    due_ts: float


@dataclass
class _PendingExit:
    wallet: str
    mint: str
    due_ts: float


class SpyStrategy(Strategy):
    id = StrategyId.SPY

    def __init__(self, cfg: SpyConfig, risk: RiskConfig) -> None:
        self.cfg = cfg
        self.risk = risk
        self.tracker = WalletTracker()
        self.pending_entries: list[_PendingCopy] = []
        self.pending_exits: list[_PendingExit] = []
        self.loss_streak: dict[str, int] = {}
        self.demoted: set[str] = set()
        self.skipped_chases = 0
        self.copies = 0
        self._seen: set[tuple[str, str, str]] = set()

    # -- ingestion ---------------------------------------------------------
    def prime(self, history: list[LeaderAction]) -> None:
        """Warm up wallet stats from historical actions without copying."""
        for a in history:
            self.tracker.ingest(a)

    def on_leader_action(self, action: LeaderAction) -> None:
        key = (action.wallet, action.mint, action.action)
        if key in self._seen:
            return
        self._seen.add(key)
        self.tracker.ingest(action)
        if action.action == "enter":
            self.pending_entries.append(
                _PendingCopy(action, action.ts + self.cfg.copy_latency_s))
        else:
            self.pending_exits.append(
                _PendingExit(action.wallet, action.mint,
                             action.ts + self.cfg.copy_latency_s))

    # -- helpers -----------------------------------------------------------
    def _wallet_allowed(self, wallet: str) -> bool:
        if wallet in self.demoted:
            return False
        stat = self.tracker.stats().get(wallet)
        if stat is None:
            return False
        return stat.score() >= self.cfg.min_wallet_score

    def _wallet_room(self, wallet: str, ctx: StrategyContext) -> float:
        eq = ctx.portfolio.equity(ctx.prices)
        cap = eq * self.risk.max_allocation_per_wallet_pct
        return max(0.0, cap - ctx.portfolio.wallet_exposure(wallet, ctx.prices))

    # -- orders ------------------------------------------------------------
    def entries(self, ctx: StrategyContext) -> list[Order]:
        orders: list[Order] = []
        remaining: list[_PendingCopy] = []
        eq = ctx.portfolio.equity(ctx.prices)
        for p in self.pending_entries:
            if p.due_ts > ctx.ts:
                remaining.append(p)
                continue
            a = p.action
            snap = ctx.snapshots.get(a.mint)
            if snap is None or ctx.portfolio.has_position(a.mint):
                continue
            if not self._wallet_allowed(a.wallet):
                continue
            chase = snap.price / a.price - 1.0 if a.price > 0 else 1.0
            if chase > self.risk.max_chase_pct:
                self.skipped_chases += 1
                continue
            room = self._wallet_room(a.wallet, ctx)
            notional = fixed_fraction_size(
                equity=eq,
                risk_per_trade_pct=self.risk.risk_per_trade_pct,
                max_position_pct=self.risk.max_position_pct,
                liquidity_usd=snap.liquidity_usd,
                max_liquidity_frac=self.risk.max_liquidity_frac,
            )
            notional = min(notional, room)
            if notional < 10.0:
                continue
            self.copies += 1
            orders.append(Order(
                strategy=self.id, mint=a.mint, symbol=a.symbol,
                side=Side.BUY, notional_usd=notional, ts=ctx.ts,
                ref_price=snap.price,
                reason=f"copy {a.label} chase={chase:.1%}",
                meta={"leader_entry_price": a.price, "wallet": a.wallet,
                      "leader_ts": a.ts, "chase": chase},
            ))
        self.pending_entries = remaining
        return orders

    def exits(self, ctx: StrategyContext) -> list[Order]:
        orders: list[Order] = []
        # mirror exits first
        exit_due = [e for e in self.pending_exits if e.due_ts <= ctx.ts]
        self.pending_exits = [e for e in self.pending_exits if e.due_ts > ctx.ts]
        for e in exit_due:
            pos = ctx.portfolio.positions.get(e.mint)
            if pos is None or pos.wallet != e.wallet:
                continue
            snap = ctx.snapshots.get(e.mint)
            price = snap.price if snap else pos.avg_price
            orders.append(Order(
                strategy=self.id, mint=e.mint, symbol=pos.symbol,
                side=Side.SELL, notional_usd=pos.market_value(price),
                ts=ctx.ts, ref_price=price, reason="mirror leader exit",
                meta={"exit_reason": ExitReason.MIRROR.value},
            ))
        exiting = {o.mint for o in orders}

        # protective backstops so a missed exit signal cannot run forever
        for mint, pos in list(ctx.portfolio.positions.items()):
            if mint in exiting:
                continue
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
                side=Side.SELL, notional_usd=pos.market_value(price),
                ts=ctx.ts, ref_price=price, reason=f"exit {reason.value}",
                meta={"exit_reason": reason.value},
            ))
        return orders

    # -- feedback ----------------------------------------------------------
    def on_trade_closed(self, trade) -> None:
        wallet = trade.wallet
        if not wallet:
            return
        if trade.pnl_usd < 0:
            self.loss_streak[wallet] = self.loss_streak.get(wallet, 0) + 1
            if self.loss_streak[wallet] >= self.risk.wallet_demote_after_losses:
                self.demoted.add(wallet)
        else:
            self.loss_streak[wallet] = 0
