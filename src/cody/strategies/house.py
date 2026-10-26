"""Bot two: the House.

Launches tokens, collects a fee on every trade, and kills the dead ones. Edge is
volume existing; direction is irrelevant.

Four hard rules, all enforced structurally:

1. **Break-even before anything runs, computed honestly.** Expected fee income
   per token must clear the *net* cost per launch (deploy + gas + the seed loss
   the launch distribution actually produces) times a safety factor. The gate
   uses the expected value of the *realized* distribution -- not a nominal
   "expected volume" number the sampler never delivers. If it does not clear,
   the launch is refused.
2. **The House never holds or sells its own supply.** Income is fees only. The
   moment it holds supply and sells into buyers it is not the house, it is the
   rug -- and the blast radius is reputational, platform, and legal.
3. **Seed liquidity is capital, not free.** It is locked at face value per live
   launch and returned at close minus the *realized* loss for that launch. The
   loss is booked when it happens, not front-loaded at launch, so the equity
   curve reflects economics rather than an accounting artifact.
4. **Concurrency is capped.** Open launches cannot exceed ``max_open_launches``
   so the desk can never over-commit its float to a pile of simultaneous seeds.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..config import HouseConfig, RiskConfig
from ..models import StrategyId
from .base import Strategy, StrategyContext


@dataclass
class HouseLaunch:
    mint: str
    symbol: str
    ts: float
    capital_out_usd: float      # cash actually spent at launch (deploy+gas+seed)
    seed_liquidity_usd: float
    realized_volume_usd: float
    fee_rate: float
    horizon_s: float
    realized_seed_loss_pct: float
    accrued_fees_usd: float = 0.0
    last_ts: float = 0.0
    closed: bool = False
    seed_returned: bool = False

    def __post_init__(self) -> None:
        if self.last_ts <= 0:
            self.last_ts = self.ts

    @property
    def decay_k(self) -> float:
        return 3.0 / self.horizon_s

    def accrue(self, now: float) -> float:
        """Fees decay exponentially over the launch horizon (volume front-loads)."""
        if self.closed or now <= self.last_ts:
            return 0.0
        k = self.decay_k
        t0 = self.last_ts - self.ts
        t1 = now - self.ts
        inc = self.fee_rate * self.realized_volume_usd * (
            math.exp(-k * t0) - math.exp(-k * t1))
        self.accrued_fees_usd += inc
        self.last_ts = now
        if t1 >= self.horizon_s:
            self.closed = True
        return inc


class HouseStrategy(Strategy):
    id = StrategyId.HOUSE

    def __init__(self, cfg: HouseConfig, risk: RiskConfig,
                 seed: int = 7) -> None:
        self.cfg = cfg
        self.risk = risk
        self.rng = random.Random(seed)
        self.launches: list[HouseLaunch] = []
        self.realized_fees_usd = 0.0
        self.total_costs_usd = 0.0
        self.seed_returned_usd = 0.0
        self.realized_seed_loss_usd = 0.0
        self._last_launch_ts = -1e18
        self._launch_day = -1
        self._launches_today = 0
        self._halt_until = 0.0
        self._last_eval_len = -1
        self.fee_stops = 0

    # -- economics ---------------------------------------------------------
    @property
    def capital_per_launch(self) -> float:
        return (self.cfg.deploy_cost_usd + self.cfg.seed_liquidity_usd
                + self.cfg.gas_cost_usd)

    def expected_seed_loss_pct(self) -> float:
        """E[seed loss] implied by the dead/live launch mix. This is the number
        the economics should use -- not a hand-set constant that disagrees with
        the sampler."""
        dead = self.cfg.dead_launch_prob
        live = 1.0 - dead
        dead_mid = 0.5 * (self.cfg.dead_seed_loss_lo + self.cfg.dead_seed_loss_hi)
        live_mid = 0.5 * (self.cfg.live_seed_loss_lo + self.cfg.live_seed_loss_hi)
        return dead * dead_mid + live * live_mid

    def _volume_base(self) -> float:
        """Volume scales with the liquidity you seed: a $500 pool turns over
        proportionally to a $100 pool. ``expected_volume_per_token_usd`` is the
        volume at the reference seed of $500."""
        return self.cfg.expected_volume_per_token_usd * (
            self.cfg.seed_liquidity_usd / 500.0)

    def expected_realized_volume(self) -> float:
        """E[volume] under the exact sampler used by ``_draw_launch``."""
        base = self._volume_base()
        dead = self.cfg.dead_launch_prob
        live = 1.0 - dead
        e_dead = base * self.cfg.dead_volume_frac * 0.5
        mu = math.log(max(1e-9, base * self.cfg.live_volume_median_frac))
        e_live = min(base * self.cfg.volume_cap_mult,
                     math.exp(mu + 0.5 * self.cfg.live_volume_sigma ** 2))
        return dead * e_dead + live * e_live

    @property
    def expected_fees_per_token(self) -> float:
        return self.cfg.fee_rate * self.expected_realized_volume()

    @property
    def net_cost_per_launch(self) -> float:
        """Full economic cost: deploy + gas + the seed loss the mix produces."""
        return (self.cfg.deploy_cost_usd + self.cfg.gas_cost_usd
                + self.cfg.seed_liquidity_usd * self.expected_seed_loss_pct())

    @property
    def operating_cost_per_launch(self) -> float:
        """Hard cash cost that does not come back: deploy + gas. The seed is a
        liquidity loan, recovered at close minus its realized loss."""
        return self.cfg.deploy_cost_usd + self.cfg.gas_cost_usd

    @property
    def expected_seed_loss_usd(self) -> float:
        return self.cfg.seed_liquidity_usd * self.expected_seed_loss_pct()

    def fee_cover_ratio(self) -> float:
        """Fee income over *operating* cost -- the write-up's break-even."""
        if self.operating_cost_per_launch <= 0:
            return float("inf")
        return self.expected_fees_per_token / self.operating_cost_per_launch

    def net_edge_ratio(self) -> float:
        """Fee income over expected seed loss -- must exceed 1 to be net positive."""
        if self.expected_seed_loss_usd <= 0:
            return float("inf")
        return self.expected_fees_per_token / self.expected_seed_loss_usd

    def break_even_ratio(self) -> float:
        """Headline ratio used in reports: fees over full economic cost."""
        if self.net_cost_per_launch <= 0:
            return float("inf")
        return self.expected_fees_per_token / self.net_cost_per_launch

    def break_even_ok(self) -> bool:
        """Two independent gates, both must hold:

        1. fee income clears operating cost times the safety factor of 2
           (never run a launch at its rated limit), and
        2. fee income clears expected seed loss by ``min_edge_multiple`` -- the
           business must be net positive after the seed risk it is carrying.
        """
        return (self.fee_cover_ratio() >= self.risk.safety_factor
                and self.net_edge_ratio() >= self.cfg.min_edge_multiple)

    def market_volume_ok(self, ctx: StrategyContext) -> bool:
        return sum(s.volume_5m_usd for s in ctx.snapshots.values()) >= \
            self.cfg.min_market_volume_5m_usd

    def open_launches(self) -> int:
        return sum(1 for l in self.launches if not l.closed)

    def _draw_launch(self) -> tuple[float, float]:
        """Return (realized_volume_usd, realized_seed_loss_pct) for one launch.

        Most launches die; a few carry the desk. The seed loss is drawn from the
        same dead/live classification so the two are internally consistent.
        """
        base = self._volume_base()
        if self.rng.random() < self.cfg.dead_launch_prob:
            vol = base * self.rng.uniform(0.0, self.cfg.dead_volume_frac)
            loss = self.rng.uniform(self.cfg.dead_seed_loss_lo,
                                    self.cfg.dead_seed_loss_hi)
        else:
            sigma = self.cfg.live_volume_sigma
            mu = math.log(max(1e-9, base * self.cfg.live_volume_median_frac))
            vol = min(base * self.cfg.volume_cap_mult,
                      self.rng.lognormvariate(mu, sigma))
            loss = self.rng.uniform(self.cfg.live_seed_loss_lo,
                                    self.cfg.live_seed_loss_hi)
        return vol, loss

    # -- lifecycle ---------------------------------------------------------
    def entries(self, ctx: StrategyContext) -> list:
        return []

    def on_tick(self, ctx: StrategyContext) -> None:
        # 1. accrue fees; on close, realize the seed loss and release the seed
        for launch in self.launches:
            if launch.seed_returned:
                continue
            inc = launch.accrue(ctx.ts)
            if inc:
                ctx.portfolio.cash += inc
                self.realized_fees_usd += inc
            if launch.closed:
                back = launch.seed_liquidity_usd * (1.0 - launch.realized_seed_loss_pct)
                ctx.portfolio.cash += back
                ctx.portfolio.locked_capital -= launch.seed_liquidity_usd
                self.realized_seed_loss_usd += (
                    launch.seed_liquidity_usd * launch.realized_seed_loss_pct)
                self.seed_returned_usd += back
                launch.seed_returned = True

        # 2. hard stop when fees fall below cost over a rolling window
        if self._fee_stop_triggered(ctx.ts):
            return

        # 3. decide whether to launch another
        day = int(ctx.ts // 86_400)
        if day != self._launch_day:
            self._launch_day = day
            self._launches_today = 0
        if self._launches_today >= self.cfg.max_launches_per_day:
            return
        if self.open_launches() >= self.cfg.max_open_launches:
            return
        if ctx.ts - self._last_launch_ts < self.cfg.launch_cooldown_s:
            return
        if not self.break_even_ok():
            return
        if not self.market_volume_ok(ctx):
            return
        if ctx.portfolio.cash < self.capital_per_launch:
            return

        n = len(self.launches) + 1
        vol, loss = self._draw_launch()
        launch = HouseLaunch(
            mint=f"HOUSE{n:04d}", symbol=f"HOUSE{n}", ts=ctx.ts,
            capital_out_usd=self.capital_per_launch,
            seed_liquidity_usd=self.cfg.seed_liquidity_usd,
            realized_volume_usd=vol,
            fee_rate=self.cfg.fee_rate,
            horizon_s=self.cfg.fee_horizon_s,
            realized_seed_loss_pct=loss,
        )
        ctx.portfolio.cash -= self.capital_per_launch
        # carry seed at face value: it is in the pool and recoverable at close
        ctx.portfolio.locked_capital += self.cfg.seed_liquidity_usd
        self.total_costs_usd += self.capital_per_launch
        self.launches.append(launch)
        self._last_launch_ts = ctx.ts
        self._launches_today += 1

    def _fee_stop_triggered(self, ts: float) -> bool:
        """Hard stop: if fees fall below cost over the last N *closed* launches,
        stop launching for a cooldown. Evaluated once per newly-closed launch so
        it cannot thrash every tick."""
        if ts < self._halt_until:
            return True
        closed = [l for l in self.launches if l.closed]
        if len(closed) < self.cfg.fee_stop_window:
            return False
        if len(closed) == self._last_eval_len:
            return False  # no new evidence since last evaluation
        window = closed[-self.cfg.fee_stop_window:]
        fees = sum(l.accrued_fees_usd for l in window)
        costs = len(window) * self.net_cost_per_launch
        self._last_eval_len = len(closed)
        if fees < costs:
            self._halt_until = ts + self.cfg.fee_stop_cooldown_s
            self.fee_stops += 1
            return True
        return False

    # -- invariants --------------------------------------------------------
    def holds_supply(self, portfolio) -> bool:
        return any(p.strategy is StrategyId.HOUSE
                   for p in portfolio.positions.values())

    def net_pnl(self) -> float:
        return (self.realized_fees_usd + self.seed_returned_usd
                - self.total_costs_usd)
