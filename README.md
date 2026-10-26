# Cody — a memecoin trading desk that knows how it dies

Cody is a working, test-covered implementation of the three-bot desk described in
the "Grok Bot" thread: **the Gambler**, **the House**, and **the Spy**. It is not a
promise of profit. It is a piece of risk engineering: every strategy is built
around an explicit failure-mode analysis, and the code refuses to trade when the
economics or the safety gates do not clear.

The headline idea from the thread, and the one this repo takes most seriously:

> Contract checks are **gates that reject the order**, not scores that lower it.

A score lets a bad token through with a smaller position. A gate sends it home.
Cody is built out of gates.

---

## What is in the box

```
config/default.yaml      one validated config, no hidden defaults
src/cody/
  models.py              tokens, signals, orders, fills, positions, failure modes
  config.py              typed config + strict validation (unknown keys rejected)
  portfolio.py           book, fixed-fraction sizing, locked-capital accounting
  risk.py                gates, circuit breakers, the FMEA registry
  execution.py           latency + base slippage + price-impact + taker fees
  wallets.py             leader-wallet tracking and ranking
  strategies/
    gambler.py           launch scanner + momentum, gates, fixed sizing
    house.py             fee-farm economics, honest break-even, never holds supply
    spy.py               wallet ranking, 15% chase gate, mirror exits, demotion
  backtest/
    market.py            deterministic synthetic memecoin market + CSV loader
    simulator.py         one event loop shared by backtest / paper / live
    metrics.py           Sharpe, Sortino, max DD, Calmar, profit factor, expectancy
  report.py              self-contained dark HTML dashboard + JSON + CSV
  cli.py                 backtest / fmea / doctor / live (guarded)
tests/                   39 tests, each mapped to a failure mode or invariant
```

Dependencies are deliberately thin: Python 3.11 standard library plus `PyYAML`
and `pytest`. No web3, no exchange SDK, no hidden magic.

---

## The three bots, and how each one dies

### Bot One — the Gambler

Scans fresh launches, checks the contract, and trades direction. It dies on a
streak of wrong reads sized up by previous wins — *martingale wearing a hoodie*.

Cody's Gambler cannot martingale, structurally. Position size comes from
`fixed_fraction_size(equity, risk_per_trade_pct, max_position_pct, liquidity_usd,
max_liquidity_frac)`. There is **no argument for the last trade result anywhere in
that signature**, and `tests/test_sizing.py` enforces it two ways: it checks that
size does not grow after wins, and it inspects the function signature to prove
there is no `last_pnl` parameter to abuse.

### Bot Two — the House

Launches tokens, collects a fee on every trade, and kills the dead ones. Its edge
is that volume exists; direction is irrelevant. It dies when launch costs outrun
fee income, or when it holds and sells its own supply and becomes the rug.

Cody's House enforces four things:

1. **Break-even before anything runs, computed honestly.** The gate uses the
   expected value of the *actual* launch distribution — not a nominal "expected
   volume" the sampler never delivers. Two independent gates must both hold:
   fee income ≥ 2× operating cost (the safety factor), and fee income ≥
   `min_edge_multiple` × expected seed loss (the business must be net positive
   after the seed risk it carries).
2. **It never holds or sells its own supply.** Income is fees only. A test asserts
   no `HOUSE` position ever appears in the book.
3. **Seed liquidity is capital, not free.** It is booked at face value in
   `locked_capital` at launch and the *realized* loss is booked at close. The loss
   is not front-loaded at launch, so the equity curve reflects economics rather
   than an accounting artifact.
4. **Concurrency is capped** (`max_open_launches`) so the desk can never
   over-commit its float to a pile of simultaneous seeds.

### Bot Three — the Spy

Tracks profitable wallets, ranks them, and mirrors their trades. It dies on
latency — copying the right wallet at the wrong price.

Cody's Spy ranks wallets by a score that requires a real sample (`trades >= 5`),
penalises drawdown, and demotes a wallet after three losing copies. Every copy is
checked against the **max-chase gate**: if price has already moved more than 15%
past the leader's entry, the copy is skipped and counted. Mirror exits fire first;
stop / take-profit / trailing / max-hold are backstops only.

---

## The engineering method: FMEA, gates, breakers, blast radius

**Failure Mode and Effects Analysis.** Every way the desk can lose money is logged
as a `FailureMode` with severity, likelihood, and detectability (each 1–10). The
risk-priority number is `RPN = severity × likelihood × detectability`, and the FMEA
sheet is sorted descending so the worst risks are always at the top. Run
`cody fmea` to print it.

**Gates vs. scores.** A gate returns a boolean reject in the order path. If any
gate fails, the order never reaches the book. There is no "partial credit" for a
honeypot.

**Circuit breakers.** These halt *new entries* and start a cooldown; they never
force-close a healthy position. Cody ships five:

- consecutive-loss breaker (fixed so it releases after its cooldown instead of
  deadlocking — a tripped breaker that can never trade again can never produce the
  win that would reset it),
- drawdown breaker,
- daily-loss breaker,
- feed-lag breaker,
- liquidity-drop breaker (a rug in progress).

**Blast radius.** Capital is partitioned per strategy. One bot blowing up cannot
reach another bot's book. The House's seed float is isolated the same way.

**Safety factor.** Nothing runs at its rated limit. The House requires fee income
to clear twice its operating cost before it will launch.

---

## Running it

```bash
pip install -r requirements.txt

# full backtest -> outputs/report.html, results.json, trades.csv
PYTHONPATH=src python -m cody --config config/default.yaml backtest --out outputs

# the failure-mode register, worst RPN first
PYTHONPATH=src python -m cody --config config/default.yaml fmea

# sanity-check the config and environment
PYTHONPATH=src python -m cody --config config/default.yaml doctor

# tests
python -m pytest -q
```

The backtest is fully deterministic for a given `seed`. The same seed always
produces the same equity curve, the same trades, and the same report.

### The report

`outputs/report.html` is a single self-contained file: inline SVG equity curves,
per-strategy attribution, the FMEA table, the rejection histogram (what the gates
actually stopped), and each strategy's internals. No external assets, no CDN.

---

## A worked example (seed 7, 30 days, $10,000)

| strategy | return | max DD | Sharpe | trades |
|----------|--------|--------|--------|--------|
| Gambler  | +9.7%  | 2.3%   | 5.1    | 53     |
| House    | +83.6% | 20.0%  | 5.3    | ~150 launches |
| Spy      | +48.4% | 1.7%   | 23.3   | 71     |
| **Desk** | **+40.0%** | **3.9%** | **11.3** | 124 |

The House's high return is not a fantasy number bolted on: it is ~150 small
launches each netting a few dollars, compounding on a small $2,000 book. It is
also the most fragile number in the table. Across five independent backtest seeds
the House returns between +84% and +289% with 10–20% drawdown — a wide band that
is exactly what a fee farm looks like when its edge is thin and its outcomes are
heavy-tailed.

---

## The honest caveat

**These are simulated returns on a synthetic market.** The synthetic market is
deterministic and deliberately heavy-tailed: most launches die, a few carry the
desk, rugs and honeypots exist, and execution has latency, slippage, impact, and
fees. That makes it a useful testbed for the *risk machinery*. It is **not** a
forecast.

Real memecoin trading adds things the simulator only approximates: MEV and
sandwiching, RPC failures and reorgs, liquidity that vanishes faster than the
breaker can react, contract exploits the static checks miss, and the small matter
that a strategy which works at $2,000 often does not work at $2,000,000. The
House's own failure mode — *volume drying up* — is the one most likely to turn a
green curve red in the real world.

Treat Cody as a reference implementation of the discipline in the thread: gates
that reject, breakers that halt, sizes that cannot martingale, and an FMEA that
names how it dies before it does.

---

## Live trading

`cody live` is a guarded stub. It refuses to run without `--confirm` and refuses
when `live.dry_run` is true. Wiring a real venue (Jupiter / Raydium / an on-chain
router) means implementing the `Executor` protocol and the feed adapters in
`src/cody/live/`. The risk engine, sizing, and breakers are already in the loop;
only the transport is missing. Do not point it at real money until the FMEA sheet
is boring.
