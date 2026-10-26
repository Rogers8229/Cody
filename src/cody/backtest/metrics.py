"""Performance metrics. Pure Python, no numpy required."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from ..models import ClosedTrade


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def max_drawdown(curve: Iterable[float]) -> float:
    peak = -float("inf")
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1.0 - v / peak)
    return mdd


def sharpe(returns: list[float], periods_per_year: float) -> float:
    sd = _std(returns)
    if sd == 0:
        return 0.0
    return _mean(returns) / sd * math.sqrt(periods_per_year)


def sortino(returns: list[float], periods_per_year: float) -> float:
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dd = math.sqrt(sum(r * r for r in downside) / len(downside))
    if dd == 0:
        return 0.0
    return _mean(returns) / dd * math.sqrt(periods_per_year)


@dataclass
class Metrics:
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    win_rate: float
    profit_factor: float
    expectancy_usd: float
    avg_win_usd: float
    avg_loss_usd: float
    trades: int
    fees_usd: float
    final_equity: float
    starting_equity: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total_return": round(self.total_return, 6),
            "cagr": round(self.cagr, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "calmar": round(self.calmar, 4),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy_usd": round(self.expectancy_usd, 2),
            "avg_win_usd": round(self.avg_win_usd, 2),
            "avg_loss_usd": round(self.avg_loss_usd, 2),
            "trades": self.trades,
            "fees_usd": round(self.fees_usd, 2),
            "final_equity": round(self.final_equity, 2),
            "starting_equity": round(self.starting_equity, 2),
        }


def compute_metrics(curve: list[tuple[float, float]],
                    trades: list[ClosedTrade],
                    step_s: float,
                    fees_usd: float = 0.0,
                    starting_equity: float | None = None) -> Metrics:
    equities = [e for _, e in curve]
    start = starting_equity if starting_equity is not None else (
        equities[0] if equities else 0.0)
    end = equities[-1] if equities else 0.0
    total_return = end / start - 1.0 if start > 0 else 0.0

    # Sharpe/Sortino on daily equity, not on 60-second steps (which inflates
    # the annualisation factor by ~sqrt(525600)).
    daily: dict[int, float] = {}
    for ts, eq in curve:
        daily[int(ts // 86_400)] = eq
    keys = sorted(daily)
    daily_returns = [daily[keys[i]] / daily[keys[i - 1]] - 1.0
                     for i in range(1, len(keys)) if daily[keys[i - 1]] > 0]
    periods_per_year = 365.0

    mdd = max_drawdown(equities)
    sh = sharpe(daily_returns, periods_per_year)
    so = sortino(daily_returns, periods_per_year)
    duration_years = (len(equities) * step_s) / (365.0 * 86_400.0)
    cagr = (end / start) ** (1.0 / duration_years) - 1.0 \
        if start > 0 and duration_years > 0 and end > 0 else 0.0
    calmar = cagr / mdd if mdd > 0 else 0.0

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    return Metrics(
        total_return=total_return, cagr=cagr, max_drawdown=mdd,
        sharpe=sh, sortino=so, calmar=calmar,
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=profit_factor,
        expectancy_usd=_mean([t.pnl_usd for t in trades]),
        avg_win_usd=_mean([t.pnl_usd for t in wins]),
        avg_loss_usd=_mean([t.pnl_usd for t in losses]),
        trades=len(trades), fees_usd=fees_usd,
        final_equity=end, starting_equity=start,
    )
