"""Event-driven backtest / paper simulator.

The same loop runs a backtest and a paper session; only the data source and the
executor differ. Strategies are capital-partitioned so attribution is honest and
one bot's blast radius cannot reach another bot's book.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..config import Config
from ..execution import ExecutionModel
from ..models import (
    ClosedTrade,
    ExitReason,
    StrategyId,
    TokenSnapshot,
)
from ..portfolio import Portfolio, split_capital
from ..risk import RiskEngine
from ..strategies.base import Strategy, StrategyContext
from ..strategies.gambler import GamblerStrategy
from ..strategies.house import HouseStrategy
from ..strategies.spy import SpyStrategy
from .market import MarketData, SyntheticMarket
from .metrics import Metrics, compute_metrics


@dataclass
class StrategyResult:
    strategy: StrategyId
    starting_equity: float
    ending_equity: float
    equity_curve: list[tuple[float, float]]
    closed: list[ClosedTrade]
    metrics: Metrics
    rejected: dict[str, int] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    results: dict[StrategyId, StrategyResult]
    combined_curve: list[tuple[float, float]]
    combined_metrics: Metrics
    start_ts: float
    end_ts: float
    step_s: float
    config: Config
    market: MarketData | None = None


def _build_strategies(cfg: Config) -> dict[StrategyId, Strategy]:
    out: dict[StrategyId, Strategy] = {}
    if cfg.gambler.enabled:
        out[StrategyId.GAMBLER] = GamblerStrategy(cfg.gambler, cfg.risk)
    if cfg.house.enabled:
        out[StrategyId.HOUSE] = HouseStrategy(
            cfg.house, cfg.risk, seed=cfg.backtest.seed)
    if cfg.spy.enabled:
        out[StrategyId.SPY] = SpyStrategy(cfg.spy, cfg.risk)
    return out


class Simulator:
    def __init__(self, config: Config, market: MarketData | None = None) -> None:
        self.cfg = config
        self.market = market or SyntheticMarket(
            days=config.backtest.days,
            step_s=config.backtest.step_s,
            seed=config.backtest.seed,
            tokens_per_day=config.backtest.tokens_per_day,
        ).generate()

    def run(self) -> BacktestResult:
        cfg = self.cfg
        md = self.market
        step = cfg.backtest.step_s

        strategies = _build_strategies(cfg)
        self.strategies = strategies
        weights = {sid: {
            StrategyId.GAMBLER: cfg.gambler.capital_pct,
            StrategyId.HOUSE: cfg.house.capital_pct,
            StrategyId.SPY: cfg.spy.capital_pct,
        }[sid] for sid in strategies}
        capital = split_capital(cfg.backtest.start_equity_usd, weights)

        portfolios = {
            sid: Portfolio(sid, capital[sid], capital[sid])
            for sid in strategies
        }
        risk = {sid: RiskEngine.build(cfg.risk) for sid in strategies}
        exec_model = ExecutionModel(cfg.execution)
        spy = strategies.get(StrategyId.SPY)
        if isinstance(spy, SpyStrategy):
            spy.prime(md.warmup_actions)

        curves: dict[StrategyId, list[tuple[float, float]]] = {
            sid: [] for sid in strategies}
        rejected: dict[StrategyId, Counter] = {
            sid: Counter() for sid in strategies}
        last_prices: dict[str, float] = {}

        ts = md.start_ts
        prev_ts = md.start_ts
        while ts <= md.end_ts + 1e-9:
            snaps = md.snapshots_at(ts)
            prices = {m: s.price for m, s in snaps.items()}
            last_prices.update(prices)
            actions = md.actions_between(prev_ts, ts)

            for sid, strat in strategies.items():
                pf = portfolios[sid]
                # feed liquidity history for the breaker
                for mint in list(pf.positions):
                    if mint in snaps:
                        risk[sid].observe_liquidity(
                            mint, ts, snaps[mint].liquidity_usd)

                ctx = StrategyContext(
                    ts=ts, snapshots=snaps, portfolio=pf,
                    prices=last_prices, feed_age_s=0.0,
                    leader_actions=actions)

                if isinstance(strat, SpyStrategy):
                    for a in actions:
                        strat.on_leader_action(a)
                strat.on_tick(ctx)

                # --- exits first ---
                for order in strat.exits(ctx):
                    snap = snaps.get(order.mint)
                    if snap is None:
                        continue
                    fill = exec_model.execute(order, snap)
                    reason = ExitReason(order.meta.get("exit_reason", "manual"))
                    trade = pf.apply_sell(order.mint, fill, reason)
                    risk[sid].on_trade_closed(trade)
                    strat.on_trade_closed(trade)

                # --- entries through the gate stack ---
                for order in strat.entries(ctx):
                    snap = snaps.get(order.mint)
                    if snap is None:
                        continue
                    gate = risk[sid].pre_trade(
                        order, snap, pf, last_prices, ctx.feed_age_s)
                    if not gate:
                        for r in gate.reasons:
                            rejected[sid][r.split(":")[0].strip()] += 1
                        continue
                    fill = exec_model.execute(order, snap)
                    if fill.notional_usd + fill.fee_usd > pf.cash + 1e-9:
                        rejected[sid]["cash"] += 1
                        continue
                    stop = fill.price * (1.0 - cfg.risk.stop_loss_pct)
                    tp = fill.price * (1.0 + cfg.risk.take_profit_pct)
                    pf.apply_buy(
                        fill, stop, tp,
                        wallet=order.meta.get("wallet"),
                        leader_entry_price=order.meta.get("leader_entry_price"))
                    strat.on_fill(order, fill)

                eq = pf.mark(last_prices)
                curves[sid].append((ts, eq))

            # day boundary: reset the daily-loss reference
            if int(ts // 86_400) != int(prev_ts // 86_400):
                for pf in portfolios.values():
                    pf.reset_day(last_prices)
            prev_ts = ts
            ts += step

        results: dict[StrategyId, StrategyResult] = {}
        for sid, pf in portfolios.items():
            metrics = compute_metrics(
                curves[sid], pf.closed, step, pf.fees_paid,
                starting_equity=capital[sid])
            extra: dict = {}
            strat = strategies[sid]
            if isinstance(strat, HouseStrategy):
                extra = {
                    "launches": len(strat.launches),
                    "realized_fees_usd": round(strat.realized_fees_usd, 2),
                    "total_costs_usd": round(strat.total_costs_usd, 2),
                    "break_even_ratio": round(strat.break_even_ratio(), 3),
                    "seed_returned_usd": round(strat.seed_returned_usd, 2),
                    "net_pnl_usd": round(strat.net_pnl(), 2),
                    "holds_supply": strat.holds_supply(pf),
                }
            elif isinstance(strat, SpyStrategy):
                extra = {
                    "copies": strat.copies,
                    "skipped_chases": strat.skipped_chases,
                    "demoted_wallets": sorted(strat.demoted),
                }
            results[sid] = StrategyResult(
                strategy=sid,
                starting_equity=capital[sid],
                ending_equity=curves[sid][-1][1] if curves[sid] else capital[sid],
                equity_curve=curves[sid],
                closed=pf.closed,
                metrics=metrics,
                rejected=dict(rejected[sid]),
                extra=extra,
            )

        combined = self._combine(curves)
        combined_metrics = compute_metrics(
            combined,
            [t for pf in portfolios.values() for t in pf.closed],
            step,
            sum(pf.fees_paid for pf in portfolios.values()),
            starting_equity=cfg.backtest.start_equity_usd)
        return BacktestResult(
            results=results, combined_curve=combined,
            combined_metrics=combined_metrics,
            start_ts=md.start_ts, end_ts=md.end_ts, step_s=step,
            config=cfg, market=md)

    @staticmethod
    def _combine(curves: dict[StrategyId, list[tuple[float, float]]]
                 ) -> list[tuple[float, float]]:
        if not curves:
            return []
        n = min(len(c) for c in curves.values())
        out: list[tuple[float, float]] = []
        for i in range(n):
            ts = next(iter(curves.values()))[i][0]
            out.append((ts, sum(c[i][1] for c in curves.values())))
        return out
