"""Market data sources.

Two implementations:

* `SyntheticMarket` — a deterministic memecoin universe with launches, pumps,
  rugs, honeypots, decaying volume, and skilled/unskilled leader wallets. Same
  seed in, same tape out. This is what makes the backtest reproducible.
* `load_csv` — replay a real snapshot tape exported from a live feed.

Both produce `TokenSnapshot` objects, so strategies cannot tell them apart.
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from ..models import TokenSnapshot
from ..wallets import LeaderAction

KINDS = ("pump", "rug", "flat", "slow", "honeypot")


@dataclass
class SyntheticToken:
    mint: str
    symbol: str
    launch_ts: float
    kind: str
    p0: float
    liq0: float
    peak_age: float
    peak_mult: float
    decay: float
    base_vol_5m: float
    top10: float
    mint_revoked: bool
    lp_locked: bool
    honeypot: bool
    rug_age: float = 0.0
    noise_seed: int = 0
    holder_count: int = 0

    def _noise(self, t: float, amp: float = 0.02) -> float:
        r = random.Random(f"{self.noise_seed}:{int(t)}")
        return 1.0 + r.uniform(-amp, amp)

    def price_at(self, t: float) -> float:
        age = t - self.launch_ts
        if age < 0:
            return 0.0
        if self.kind == "rug" and age >= self.rug_age:
            return self.p0 * 0.02 * self._noise(t, 0.05)
        if self.kind in ("pump", "honeypot", "rug"):
            if age <= self.peak_age:
                p = self.p0 * (1.0 + (self.peak_mult - 1.0) * age / self.peak_age)
            else:
                peak = self.p0 * self.peak_mult
                p = peak * math.exp(-self.decay * (age - self.peak_age))
            return p * self._noise(t)
        if self.kind == "slow":
            return self.p0 * (1.0 + 0.6 * age / 3600.0) * self._noise(t, 0.01)
        return self.p0 * (1.0 + 0.08 * math.sin(age / 300.0)) * self._noise(t, 0.01)

    def liquidity_at(self, t: float) -> float:
        age = t - self.launch_ts
        if age < 0:
            return 0.0
        if self.kind == "rug" and age >= self.rug_age:
            return self.liq0 * 0.04
        growth = 0.6 + 0.4 * (self.price_at(t) / self.p0 if self.p0 else 1.0)
        return self.liq0 * growth

    def volume_5m_at(self, t: float) -> float:
        age = t - self.launch_ts
        if age < 0:
            return 0.0
        base = self.base_vol_5m * math.exp(-age / 7200.0)
        if self.kind in ("pump", "honeypot", "rug"):
            spike = 1.0 + 3.0 * math.exp(-((age - self.peak_age) / 300.0) ** 2)
            base *= spike
        if self.kind == "rug" and age >= self.rug_age:
            base *= 2.0  # panic volume
        return base

    def buy_pressure_at(self, t: float) -> float:
        age = t - self.launch_ts
        if self.kind == "rug" and age >= self.rug_age:
            return 0.08
        if self.kind in ("pump", "honeypot", "rug"):
            if age <= self.peak_age:
                return 0.62 + 0.20 * math.sin(age / 120.0)
            return 0.38 + 0.10 * math.sin(age / 90.0)
        return 0.50 + 0.08 * math.sin(age / 200.0)

    def volume_24h_at(self, t: float) -> float:
        age = t - self.launch_ts
        if age < 0:
            return 0.0
        # decaying average: fresh tapes have a higher 24h baseline
        factor = 0.30 + 0.70 * math.exp(-age / 43_200.0)
        return self.base_vol_5m * 288.0 * factor

    def snapshot(self, t: float) -> TokenSnapshot:
        price = self.price_at(t)
        return TokenSnapshot(
            mint=self.mint, symbol=self.symbol, ts=t, price=price,
            liquidity_usd=self.liquidity_at(t),
            volume_24h_usd=self.volume_24h_at(t),
            volume_5m_usd=self.volume_5m_at(t),
            top10_holder_pct=self.top10,
            mint_authority_revoked=self.mint_revoked,
            lp_locked=self.lp_locked,
            honeypot=self.honeypot,
            age_s=max(0.0, t - self.launch_ts),
            buy_pressure=max(0.0, min(1.0, self.buy_pressure_at(t))),
            price_5m_ago=self.price_at(max(self.launch_ts, t - 300)),
            price_1h_ago=self.price_at(max(self.launch_ts, t - 3600)),
            holder_count=self.holder_count,
        )

    def is_alive(self, t: float) -> bool:
        age = t - self.launch_ts
        return 0 <= age <= 6 * 3600


@dataclass
class MarketData:
    tokens: list[SyntheticToken]
    leader_actions: list[LeaderAction]
    warmup_actions: list[LeaderAction]
    start_ts: float
    end_ts: float

    def snapshots_at(self, ts: float) -> dict[str, TokenSnapshot]:
        out: dict[str, TokenSnapshot] = {}
        for tok in self.tokens:
            if tok.is_alive(ts):
                snap = tok.snapshot(ts)
                if snap.price > 0 and snap.liquidity_usd > 0:
                    out[tok.mint] = snap
        return out

    def actions_between(self, lo: float, hi: float) -> list[LeaderAction]:
        return [a for a in self.leader_actions if lo < a.ts <= hi]


def _make_token(rng: random.Random, idx: int, launch_ts: float) -> SyntheticToken:
    kind = rng.choices(KINDS, weights=[34, 18, 26, 12, 10])[0]
    p0 = rng.uniform(0.0002, 0.005)
    liq0 = rng.uniform(15_000, 120_000)
    peak_age = rng.uniform(240, 2400)
    peak_mult = rng.uniform(1.6, 7.5)
    decay = rng.uniform(0.0004, 0.0025)
    base_vol = rng.uniform(2_000, 45_000)
    top10 = rng.uniform(0.10, 0.30)
    mint_revoked = rng.random() < 0.82
    lp_locked = rng.random() < 0.78
    honeypot = kind == "honeypot"
    rug_age = rng.uniform(120, 900) if kind == "rug" else 0.0
    if kind in ("rug", "honeypot"):
        top10 = rng.uniform(0.28, 0.55)
        mint_revoked = rng.random() < 0.45
        lp_locked = rng.random() < 0.35
    return SyntheticToken(
        mint=f"MINT{idx:05d}", symbol=f"${_sym(rng)}",
        launch_ts=launch_ts, kind=kind, p0=p0, liq0=liq0,
        peak_age=peak_age, peak_mult=peak_mult, decay=decay,
        base_vol_5m=base_vol, top10=top10, mint_revoked=mint_revoked,
        lp_locked=lp_locked, honeypot=honeypot, rug_age=rug_age,
        noise_seed=rng.randint(0, 1 << 30),
        holder_count=rng.randint(40, 4000),
    )


def _sym(rng: random.Random) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(rng.choice(letters) for _ in range(rng.randint(3, 6)))


@dataclass
class SyntheticMarket:
    days: int = 30
    step_s: float = 60.0
    seed: int = 7
    tokens_per_day: float = 12.0
    warmup_days: int = 7
    start_ts: float = 0.0

    def generate(self) -> MarketData:
        rng = random.Random(self.seed)
        total_days = self.days + self.warmup_days
        n_tokens = int(total_days * self.tokens_per_day)
        span = total_days * 86_400.0
        tokens: list[SyntheticToken] = []
        for i in range(n_tokens):
            launch = self.start_ts - self.warmup_days * 86_400.0 + rng.uniform(0, span)
            tokens.append(_make_token(rng, i, launch))
        tokens.sort(key=lambda t: t.launch_ts)

        wallets = [
            ("W1", "alpha-whale", 0.92),
            ("W2", "sniper-01", 0.85),
            ("W3", "rotation-desk", 0.72),
            ("W4", "degen-king", 0.58),
            ("W5", "mid-curve", 0.45),
            ("W6", "late-chaser", 0.30),
            ("W7", "exit-liq", 0.18),
            ("W8", "noise-trader", 0.10),
        ]
        actions: list[LeaderAction] = []
        for tok in tokens:
            for addr, label, skill in wallets:
                # better wallets are pickier and trade the good tapes
                p_trade = 0.05 + 0.20 * skill
                if tok.kind in ("rug", "honeypot"):
                    p_trade *= (1.0 - 0.7 * skill)  # skilled avoid rugs
                if rng.random() > p_trade:
                    continue
                entry_age = rng.uniform(30, 420)
                entry_ts = tok.launch_ts + entry_age
                entry_price = tok.price_at(entry_ts)
                if entry_price <= 0:
                    continue
                if tok.kind == "rug":
                    if skill > 0.6:
                        exit_age = tok.rug_age * rng.uniform(0.4, 0.8)
                    else:
                        exit_age = tok.rug_age * rng.uniform(1.0, 1.4)
                elif tok.kind in ("pump", "honeypot"):
                    # even good wallets mistime the top; some exits are late
                    exit_age = tok.peak_age * rng.uniform(0.55, 1.55)
                else:
                    exit_age = entry_age + rng.uniform(300, 5400)
                exit_ts = tok.launch_ts + exit_age
                exit_price = tok.price_at(exit_ts)
                if exit_price <= 0:
                    continue
                outcome = exit_price / entry_price - 1.0
                size = rng.uniform(500, 25_000)
                actions.append(LeaderAction(
                    wallet=addr, label=label, mint=tok.mint, symbol=tok.symbol,
                    action="enter", ts=entry_ts, price=entry_price,
                    entry_price=entry_price, size_usd=size))
                actions.append(LeaderAction(
                    wallet=addr, label=label, mint=tok.mint, symbol=tok.symbol,
                    action="exit", ts=exit_ts, price=exit_price,
                    entry_price=entry_price, size_usd=size,
                    outcome_pct=outcome))
        actions.sort(key=lambda a: a.ts)

        backtest_start = self.start_ts
        warmup = [a for a in actions if a.ts <= backtest_start]
        live = [a for a in actions if a.ts > backtest_start]
        return MarketData(
            tokens=tokens, leader_actions=live, warmup_actions=warmup,
            start_ts=backtest_start,
            end_ts=backtest_start + self.days * 86_400.0)


def load_csv(path: str | Path) -> list[TokenSnapshot]:
    """Load a snapshot tape. One row per (ts, mint)."""
    rows: list[TokenSnapshot] = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(TokenSnapshot(
                mint=r["mint"], symbol=r.get("symbol", r["mint"]),
                ts=float(r["ts"]), price=float(r["price"]),
                liquidity_usd=float(r["liquidity_usd"]),
                volume_24h_usd=float(r.get("volume_24h_usd", 0)),
                volume_5m_usd=float(r.get("volume_5m_usd", 0)),
                top10_holder_pct=float(r.get("top10_holder_pct", 0)),
                mint_authority_revoked=r.get("mint_authority_revoked", "1") in ("1", "true", "True"),
                lp_locked=r.get("lp_locked", "1") in ("1", "true", "True"),
                honeypot=r.get("honeypot", "0") in ("1", "true", "True"),
                age_s=float(r.get("age_s", 0)),
                buy_pressure=float(r.get("buy_pressure", 0.5)),
                price_5m_ago=float(r.get("price_5m_ago", r["price"])),
                price_1h_ago=float(r.get("price_1h_ago", r["price"])),
                holder_count=int(r.get("holder_count", 0)),
            ))
    rows.sort(key=lambda s: s.ts)
    return rows
