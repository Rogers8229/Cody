"""Core domain models for the Cody memecoin trading desk.

Everything here is deliberately dependency-free and immutable where it matters
so the same objects can be replayed in a backtest, paper session, or live run
and produce identical decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class StrategyId(str, Enum):
    GAMBLER = "gambler"
    HOUSE = "house"
    SPY = "spy"


class ExitReason(str, Enum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    TRAILING = "trailing"
    MAX_HOLD = "max_hold"
    MIRROR = "mirror"
    BREAKER = "breaker"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenSnapshot:
    """A point-in-time view of a token. Every field a strategy is allowed to
    see lives here; nothing downstream reaches into the feed directly."""

    mint: str
    symbol: str
    ts: float
    price: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_5m_usd: float
    top10_holder_pct: float
    mint_authority_revoked: bool
    lp_locked: bool
    honeypot: bool
    age_s: float
    buy_pressure: float  # 0..1 share of trades that are buys in last 5m
    price_5m_ago: float
    price_1h_ago: float
    holder_count: int = 0

    # -- derived helpers ---------------------------------------------------
    @property
    def change_5m(self) -> float:
        if self.price_5m_ago <= 0:
            return 0.0
        return self.price / self.price_5m_ago - 1.0

    @property
    def change_1h(self) -> float:
        if self.price_1h_ago <= 0:
            return 0.0
        return self.price / self.price_1h_ago - 1.0

    @property
    def volume_surge(self) -> float:
        """5m volume annualised against the 24h baseline (6x per hour)."""
        baseline_5m = self.volume_24h_usd / 288.0 if self.volume_24h_usd else 0.0
        if baseline_5m <= 0:
            return 0.0
        return self.volume_5m_usd / baseline_5m

    def age_ok(self, max_age_s: float) -> bool:
        return 0 <= self.age_s <= max_age_s


@dataclass(frozen=True)
class WalletStat:
    """Aggregated performance for a wallet the Spy is considering mirroring."""

    address: str
    label: str
    trades: int
    win_rate: float
    pnl_pct: float
    avg_hold_s: float
    max_drawdown_pct: float
    last_active_ts: float

    def score(self) -> float:
        """Ranking score. Rewards edge and consistency, punishes drawdown.

        Deliberately simple and inspectable rather than a black box: the Spy
        must be able to explain *why* a wallet is ranked where it is.
        """
        if self.trades < 5:
            return 0.0
        consistency = max(0.0, 1.0 - self.max_drawdown_pct)
        sample = min(1.0, self.trades / 20.0)
        return (self.pnl_pct * 0.5 + self.win_rate * 0.3 + consistency * 0.2) * sample


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if gate_result:`
        return self.passed

    @classmethod
    def ok(cls) -> "GateResult":
        return cls(True, [])

    @classmethod
    def reject(cls, *reasons: str) -> "GateResult":
        return cls(False, list(reasons))


@dataclass
class Signal:
    strategy: StrategyId
    mint: str
    symbol: str
    ts: float
    price: float
    score: float
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    strategy: StrategyId
    mint: str
    symbol: str
    side: Side
    notional_usd: float
    ts: float
    ref_price: float
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    order: Order
    ts: float
    price: float
    qty: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float
    latency_s: float

    @property
    def side(self) -> Side:
        return self.order.side


@dataclass
class Position:
    mint: str
    symbol: str
    strategy: StrategyId
    qty: float
    avg_price: float
    opened_ts: float
    entry_notional: float
    peak_price: float
    stop_price: float
    take_profit_price: float
    wallet: Optional[str] = None
    leader_entry_price: Optional[float] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.qty

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0

    def update_peak(self, price: float) -> None:
        if price > self.peak_price:
            self.peak_price = price


@dataclass
class ClosedTrade:
    mint: str
    symbol: str
    strategy: StrategyId
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: float
    exit_ts: float
    pnl_usd: float
    pnl_pct: float
    fees_usd: float
    exit_reason: ExitReason
    wallet: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0

    @property
    def hold_s(self) -> float:
        return self.exit_ts - self.entry_ts


# ---------------------------------------------------------------------------
# Failure-mode analysis (the "one sheet per bot")
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FailureMode:
    """An FMEA row: how bad, how likely, how fast you notice.

    RPN (risk priority number) = severity * likelihood * detectability, where
    detectability is scored 1..10 with 10 = hardest to detect. Fix highest RPN
    first. This is the exact method the desk write-up describes.
    """

    bot: StrategyId
    name: str
    severity: int  # 1..10, cost when it happens
    likelihood: int  # 1..10, how often
    detectability: int  # 1..10, 10 = you find out last
    mitigation: str
    test: str  # name of the test that fails the build if mitigation regresses

    def __post_init__(self) -> None:
        for v in (self.severity, self.likelihood, self.detectability):
            if not 1 <= v <= 10:
                raise ValueError("FMEA scores must be 1..10")

    @property
    def rpn(self) -> int:
        return self.severity * self.likelihood * self.detectability
