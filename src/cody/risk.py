"""Risk engine: gates, circuit breakers, and the failure-mode registry.

Two distinct mechanisms, deliberately not conflated:

* **Gates** reject an individual order. A gate is a boolean in the order path,
  not a score that lowers position size. (Contract checks are gates.)
* **Circuit breakers** halt *new entries* for a desk after a portfolio-level
  condition trips, then cool down. Each breaker ships with a test that fails the
  build if it ever stops firing.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .config import RiskConfig
from .models import (
    ClosedTrade,
    FailureMode,
    GateResult,
    Order,
    StrategyId,
    TokenSnapshot,
)
from .portfolio import Portfolio


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
class Gate:
    name: str = "gate"

    def check(self, order: Order, snap: TokenSnapshot, portfolio: Portfolio,
              prices: dict[str, float], feed_age_s: float,
              config: RiskConfig) -> GateResult:  # pragma: no cover - interface
        raise NotImplementedError


class ContractGate(Gate):
    """The contract checks from the write-up. These reject the order."""

    name = "contract"

    def check(self, order, snap, portfolio, prices, feed_age_s, config):
        reasons: list[str] = []
        if config.require_mint_authority_revoked and not snap.mint_authority_revoked:
            reasons.append("mint authority not revoked")
        if config.require_lp_locked and not snap.lp_locked:
            reasons.append("LP not locked/burned")
        if config.reject_honeypot and snap.honeypot:
            reasons.append("honeypot detected")
        if snap.top10_holder_pct > config.max_top10_holder_pct:
            reasons.append(
                f"top-10 holders {snap.top10_holder_pct:.0%} > "
                f"{config.max_top10_holder_pct:.0%}")
        if snap.liquidity_usd < config.min_liquidity_usd:
            reasons.append(
                f"liquidity ${snap.liquidity_usd:,.0f} < "
                f"${config.min_liquidity_usd:,.0f}")
        if not snap.age_ok(config.max_token_age_s):
            reasons.append(f"token age {snap.age_s:.0f}s outside window")
        if reasons:
            return GateResult.reject(*reasons)
        return GateResult.ok()


class ChaseGate(Gate):
    """Spy only: never pay more than max_chase_pct above the leader's entry."""

    name = "chase"

    def check(self, order, snap, portfolio, prices, feed_age_s, config):
        leader_entry = order.meta.get("leader_entry_price")
        if leader_entry is None or leader_entry <= 0:
            return GateResult.ok()
        chase = snap.price / leader_entry - 1.0
        if chase > config.max_chase_pct:
            return GateResult.reject(
                f"chase {chase:.1%} > cap {config.max_chase_pct:.1%} "
                f"(leader {leader_entry:.6f}, now {snap.price:.6f})")
        return GateResult.ok()


class FeedLagGate(Gate):
    name = "feed_lag"

    def check(self, order, snap, portfolio, prices, feed_age_s, config):
        if feed_age_s > config.max_feed_lag_s:
            return GateResult.reject(
                f"feed lag {feed_age_s:.1f}s > {config.max_feed_lag_s:.1f}s")
        return GateResult.ok()


class ExposureGate(Gate):
    name = "exposure"

    def check(self, order, snap, portfolio, prices, feed_age_s, config):
        eq = portfolio.equity(prices)
        if eq <= 0:
            return GateResult.reject("book equity <= 0")
        gross = portfolio.gross_exposure(prices) + order.notional_usd
        if gross / eq > config.max_gross_exposure_pct:
            return GateResult.reject(
                f"gross exposure {gross / eq:.0%} > "
                f"{config.max_gross_exposure_pct:.0%}")
        if portfolio.has_position(order.mint):
            return GateResult.reject("already holding this mint")
        return GateResult.ok()


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------
@dataclass
class BreakerStatus:
    name: str
    triggered: bool
    reason: str = ""
    until_ts: float = 0.0


class CircuitBreaker:
    name = "breaker"
    test = "test_breaker"

    def evaluate(self, ts: float, portfolio: Portfolio,
                 prices: dict[str, float],
                 feed_age_s: float) -> BreakerStatus:  # pragma: no cover
        raise NotImplementedError

    def record_trade(self, trade: ClosedTrade) -> None:
        pass


class ConsecutiveLossBreaker(CircuitBreaker):
    name = "consecutive_loss"
    test = "test_consecutive_loss_breaker_fires"

    def __init__(self, limit: int, cooldown_s: float) -> None:
        self.limit = limit
        self.cooldown_s = cooldown_s
        self.streak = 0
        self.halt_until = 0.0

    def record_trade(self, trade: ClosedTrade) -> None:
        if trade.pnl_usd < 0:
            self.streak += 1
            if self.streak >= self.limit:
                self.halt_until = trade.exit_ts + self.cooldown_s
        else:
            self.streak = 0

    def evaluate(self, ts, portfolio, prices, feed_age_s):
        if self.streak >= self.limit:
            if ts < self.halt_until:
                return BreakerStatus(
                    self.name, True,
                    f"{self.streak} consecutive losses (limit {self.limit})",
                    self.halt_until)
            # cooldown elapsed: allow one fresh probe rather than deadlock
            self.streak = 0
            self.halt_until = 0.0
        return BreakerStatus(self.name, False)


class DrawdownBreaker(CircuitBreaker):
    name = "drawdown"
    test = "test_drawdown_breaker_fires"

    def __init__(self, max_dd_pct: float, cooldown_s: float = 3600.0) -> None:
        self.max_dd_pct = max_dd_pct
        self.cooldown_s = cooldown_s
        self.halt_until = 0.0

    def evaluate(self, ts, portfolio, prices, feed_age_s):
        dd = portfolio.drawdown(prices)
        if dd >= self.max_dd_pct and ts >= self.halt_until:
            self.halt_until = ts + self.cooldown_s
        if ts < self.halt_until:
            return BreakerStatus(
                self.name, True,
                f"drawdown {dd:.1%} >= {self.max_dd_pct:.1%}", self.halt_until)
        return BreakerStatus(self.name, False)


class DailyLossBreaker(CircuitBreaker):
    name = "daily_loss"
    test = "test_daily_loss_breaker_fires"

    def __init__(self, max_daily_loss_pct: float) -> None:
        self.max_daily_loss_pct = max_daily_loss_pct
        self.halt_until = 0.0

    def evaluate(self, ts, portfolio, prices, feed_age_s):
        pnl = portfolio.daily_pnl_pct(prices)
        if pnl <= -self.max_daily_loss_pct and ts >= self.halt_until:
            self.halt_until = (ts // 86400 + 1) * 86400
        if ts < self.halt_until:
            return BreakerStatus(
                self.name, True,
                f"daily PnL {pnl:.1%} <= -{self.max_daily_loss_pct:.1%}",
                self.halt_until)
        return BreakerStatus(self.name, False)


class FeedLagBreaker(CircuitBreaker):
    name = "feed_lag"
    test = "test_feed_lag_breaker_fires"

    def __init__(self, max_lag_s: float) -> None:
        self.max_lag_s = max_lag_s

    def evaluate(self, ts, portfolio, prices, feed_age_s):
        if feed_age_s > self.max_lag_s:
            return BreakerStatus(
                self.name, True,
                f"feed stale {feed_age_s:.1f}s > {self.max_lag_s:.1f}s", ts)
        return BreakerStatus(self.name, False)


class LiquidityDropBreaker(CircuitBreaker):
    """Halts the desk when a token it holds loses liquidity fast.

    A memecoin whose pool can vanish in one block is not a price risk, it is a
    solvency risk. This breaker watches the pool, not the candle.
    """

    name = "liquidity_drop"
    test = "test_liquidity_drop_breaker_fires"

    def __init__(self, drop_pct: float, window_s: float) -> None:
        self.drop_pct = drop_pct
        self.window_s = window_s
        self.history: dict[str, deque[tuple[float, float]]] = {}

    def observe(self, mint: str, ts: float, liquidity_usd: float) -> None:
        q = self.history.setdefault(mint, deque())
        q.append((ts, liquidity_usd))
        while q and ts - q[0][0] > self.window_s:
            q.popleft()

    def evaluate(self, ts, portfolio, prices, feed_age_s):
        for mint, q in self.history.items():
            if not q or mint not in portfolio.positions:
                continue
            peak = max(v for _, v in q)
            latest = q[-1][1]
            if peak > 0 and latest / peak - 1.0 <= -self.drop_pct:
                return BreakerStatus(
                    self.name, True,
                    f"{mint} liquidity {latest / peak - 1.0:.0%} within "
                    f"{self.window_s:.0f}s", ts)
        return BreakerStatus(self.name, False)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass
class RiskEngine:
    config: RiskConfig
    gates: list[Gate] = field(default_factory=list)
    breakers: list[CircuitBreaker] = field(default_factory=list)

    @classmethod
    def build(cls, config: RiskConfig) -> "RiskEngine":
        return cls(
            config=config,
            gates=[ContractGate(), ChaseGate(), FeedLagGate(), ExposureGate()],
            breakers=[
                ConsecutiveLossBreaker(
                    config.consecutive_loss_limit,
                    config.consecutive_loss_cooldown_s),
                DrawdownBreaker(config.drawdown_halt_pct),
                DailyLossBreaker(config.daily_loss_halt_pct),
                FeedLagBreaker(config.max_feed_lag_s),
                LiquidityDropBreaker(
                    config.liquidity_drop_pct, config.liquidity_drop_window_s),
            ],
        )

    def observe_liquidity(self, mint: str, ts: float, liquidity_usd: float) -> None:
        for b in self.breakers:
            if isinstance(b, LiquidityDropBreaker):
                b.observe(mint, ts, liquidity_usd)

    def pre_trade(self, order: Order, snap: TokenSnapshot, portfolio: Portfolio,
                  prices: dict[str, float], feed_age_s: float) -> GateResult:
        for b in self.breakers:
            status = b.evaluate(order.ts, portfolio, prices, feed_age_s)
            if status.triggered:
                return GateResult.reject(f"breaker[{status.name}]: {status.reason}")
        reasons: list[str] = []
        for g in self.gates:
            res = g.check(order, snap, portfolio, prices, feed_age_s, self.config)
            if not res:
                reasons.extend(f"{g.name}: {r}" for r in res.reasons)
        return GateResult(not reasons, reasons)

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        for b in self.breakers:
            b.record_trade(trade)

    def breaker_report(self, ts: float, portfolio: Portfolio,
                       prices: dict[str, float], feed_age_s: float
                       ) -> list[BreakerStatus]:
        return [b.evaluate(ts, portfolio, prices, feed_age_s)
                for b in self.breakers]


# ---------------------------------------------------------------------------
# Failure-mode registry ("one sheet per bot")
# ---------------------------------------------------------------------------
FMEA_SHEET: list[FailureMode] = [
    # --- Gambler ---
    FailureMode(
        StrategyId.GAMBLER, "Martingale sizing after wins",
        severity=9, likelihood=7, detectability=8,
        mitigation="Position size is a fixed fraction of the current book; "
                   "no code path reads the last trade result.",
        test="test_sizing_is_fixed_fraction_never_martingale"),
    FailureMode(
        StrategyId.GAMBLER, "Rug / honeypot / unlocked LP",
        severity=10, likelihood=6, detectability=3,
        mitigation="Contract checks are gates that reject the order, not "
                   "scores that lower it.",
        test="test_contract_gate_rejects_bad_token"),
    FailureMode(
        StrategyId.GAMBLER, "Stale feed trades on old price",
        severity=8, likelihood=6, detectability=7,
        mitigation="Feed-lag breaker stops new entries when data is older "
                   "than N seconds.",
        test="test_feed_lag_breaker_fires"),
    FailureMode(
        StrategyId.GAMBLER, "Concentration in one mint",
        severity=8, likelihood=5, detectability=4,
        mitigation="Per-position and gross-exposure caps in the order path.",
        test="test_exposure_gate_rejects_over_concentration"),
    # --- House ---
    FailureMode(
        StrategyId.HOUSE, "Launch costs outrun fee income",
        severity=7, likelihood=7, detectability=5,
        mitigation="Break-even gate: expected fees must clear cost per launch "
                   "with a safety factor of 2.",
        test="test_house_break_even_gate"),
    FailureMode(
        StrategyId.HOUSE, "House holds and sells its own supply (becomes the rug)",
        severity=10, likelihood=4, detectability=6,
        mitigation="Hard invariant: House never holds or sells its own supply; "
                   "income is fees only.",
        test="test_house_never_holds_supply"),
    FailureMode(
        StrategyId.HOUSE, "Daily launch budget overrun",
        severity=6, likelihood=6, detectability=3,
        mitigation="Per-day launch budget with hard stop when fees < cost.",
        test="test_house_daily_budget"),
    # --- Spy ---
    FailureMode(
        StrategyId.SPY, "Latency: copy the right wallet at the wrong price",
        severity=8, likelihood=8, detectability=4,
        mitigation="Max-chase gate: skip any copy >15% above leader entry.",
        test="test_chase_gate_skips_late_copy"),
    FailureMode(
        StrategyId.SPY, "Copy becomes exit liquidity for the leader",
        severity=9, likelihood=7, detectability=6,
        mitigation="Mirror exits, cap allocation per wallet, demote a wallet "
                   "after three losing copies in a row.",
        test="test_spy_mirrors_exits_and_demotes"),
    FailureMode(
        StrategyId.SPY, "Overfit to a lucky wallet",
        severity=7, likelihood=7, detectability=5,
        mitigation="Wallet score requires >=5 trades and penalises drawdown; "
                   "top-N cap.",
        test="test_wallet_score_penalises_drawdown"),
]


def fmea_table() -> list[FailureMode]:
    """Return the sheet sorted by RPN descending: fix the worst score first."""
    return sorted(FMEA_SHEET, key=lambda f: f.rpn, reverse=True)
