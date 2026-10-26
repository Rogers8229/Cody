"""Wallet tracking for the Spy strategy.

The Spy does not guess. It ranks wallets by a transparent score, then mirrors
entries and exits — with a hard chase limit and per-wallet allocation cap so it
cannot become exit liquidity for the wallet it is copying.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import WalletStat


@dataclass
class LeaderAction:
    wallet: str
    label: str
    mint: str
    symbol: str
    action: str  # "enter" | "exit"
    ts: float
    price: float
    entry_price: float
    size_usd: float
    outcome_pct: float = 0.0


@dataclass
class _WalletAcc:
    address: str
    label: str
    trades: int = 0
    wins: int = 0
    pnl_sum: float = 0.0
    hold_sum: float = 0.0
    peak_equity: float = 1.0
    equity: float = 1.0
    max_dd: float = 0.0
    last_active_ts: float = 0.0

    def to_stat(self) -> WalletStat:
        return WalletStat(
            address=self.address,
            label=self.label,
            trades=self.trades,
            win_rate=self.wins / self.trades if self.trades else 0.0,
            pnl_pct=self.pnl_sum,
            avg_hold_s=self.hold_sum / self.trades if self.trades else 0.0,
            max_drawdown_pct=self.max_dd,
            last_active_ts=self.last_active_ts,
        )


@dataclass
class WalletTracker:
    """Accumulates closed leader round-trips into rankable wallet stats."""

    accumulators: dict[str, _WalletAcc] = field(default_factory=dict)
    open_entries: dict[tuple[str, str], LeaderAction] = field(default_factory=dict)

    def ingest(self, action: LeaderAction) -> None:
        acc = self.accumulators.setdefault(
            action.wallet, _WalletAcc(action.wallet, action.label))
        acc.last_active_ts = action.ts
        key = (action.wallet, action.mint)
        if action.action == "enter":
            self.open_entries[key] = action
            return
        entry = self.open_entries.pop(key, None)
        if entry is None:
            return
        acc.trades += 1
        if action.outcome_pct > 0:
            acc.wins += 1
        acc.pnl_sum += action.outcome_pct
        acc.hold_sum += max(0.0, action.ts - entry.ts)
        acc.equity *= (1.0 + action.outcome_pct)
        acc.peak_equity = max(acc.peak_equity, acc.equity)
        if acc.peak_equity > 0:
            acc.max_dd = max(acc.max_dd, 1.0 - acc.equity / acc.peak_equity)

    def stats(self) -> dict[str, WalletStat]:
        return {a: acc.to_stat() for a, acc in self.accumulators.items()}

    def ranked(self, top_n: int, min_score: float) -> list[WalletStat]:
        ranked = sorted(self.stats().values(), key=lambda s: s.score(), reverse=True)
        return [s for s in ranked if s.score() >= min_score][:top_n]
