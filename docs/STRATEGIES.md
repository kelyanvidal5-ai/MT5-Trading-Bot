# Strategies, selection and validation

Nothing here is claimed to be profitable. Each strategy states a falsifiable
hypothesis; the validation pipeline exists to reject it when the evidence is
not there.

## The nine strategies

| Name | Style | Entry TF | Context | Hypothesis (short) | Regimes |
|---|---|---|---|---|---|
| `scalp_pullback` | scalping | M5 | M15, H1 | M5 pullbacks to the 20-EMA inside an M15 trend resume during London/NY | trend |
| `intraday_vwap` | intraday | M15 | H1 | pullbacks to session VWAP in an H1 trend leave VWAP in trend direction | trend |
| `trend_ema` | trend following | H1 | H4, D1 | H1/H4/D1-aligned trends persist; buy pullbacks into the 20/50-EMA zone | trend |
| `donchian_breakout` | breakout | H1 | H4 | a 20-bar range break after volatility contraction, on volume, extends | quiet, range, trend |
| `bb_reversion` | mean reversion | M15 | H1 | in a non-trending H1 regime, band excursions revert to the mean | range, quiet |
| `macd_momentum` | momentum | H1 | H4 | a fresh MACD zero-cross aligned with H4 momentum marks acceleration | trend |
| `liquidity_sweep` | liquidity sweep | M15 | H1 | a stop-run through the prior day's/swing extreme that closes back inside fails | range, quiet, trend |
| `smc_orderblock` | smart money concepts | M15 | H1, H4 | first retracements into the order block/FVG after a break of structure continue | trend |
| `vol_squeeze` | volatility | H1 | H4 | a Bollinger-inside-Keltner squeeze release starts an expansion | quiet, range, trend |

Every strategy returns, per completed bar: direction, stop-loss, take-profit,
a 0-1 strength and a management plan (break-even, ATR trail, time stop). No
strategy sizes positions, adds to losers or trades without a stop.

Indicators are not stacked blindly: each strategy uses only what its
hypothesis needs. The full indicator library (`mt5quant/indicators/`) is also
available for research: RSI, MACD, EMA 9/20/50/100/200, SMA, Bollinger, ATR,
ADX, VWAP, Fibonacci, support/resistance, market structure (HH/HL, BOS,
CHoCH), breakouts and false breakouts, liquidity sweeps and equal highs/lows,
supply/demand zones, fair value gaps, order blocks, volume, candle patterns.

## Selection and confidence (`ai_engine/context.py`)

For each signal the engine knows, at the close of the signal bar:

- the **H1 regime** (trend up/down, range, quiet, volatile, unknown);
- the **trend of each timeframe** M15/H1/H4/D1/W1 and a weighted primary trend.

A signal is discarded when the strategy does not suit the regime, when the
regime is volatile/unknown, when it fights a strong higher-timeframe trend, or
when opposite signals conflict without a clear winner. Otherwise it receives a
confidence score:

| Component | Weight |
|---|---|
| strategy strength | 0.30 |
| regime fit | 0.25 |
| timeframe agreement | 0.25 |
| cost (spread / ATR) | 0.10 |
| news clearance | 0.10 |

A validated ML meta-model, if one exists for the strategy, contributes 30% of
the final score and may veto. Below `strategies.min_confidence` there is no
trade. Confidence also scales position size *down* (never up).

## Validation pipeline (`python main.py validate`)

1. **Walk-forward** on the first 80% of history: parameters are chosen from a
   small pre-declared grid on each in-sample window and traded on the next,
   unseen window. Only the stitched out-of-sample trades are scored.
2. **Holdout**: the last 20% is touched once, with the final parameters.
3. **Monte Carlo** (block bootstrap of out-of-sample R-multiples): distribution
   of drawdown and return.
4. **Parameter sensitivity**: +/-10% and +/-20% on every numeric parameter.
5. **Cost stress**: spreads x1.5, slippage x2.
6. **Deflated Sharpe ratio**: penalises the number of parameter sets tried.

Default gates (all must pass):

| Gate | Threshold |
|---|---|
| out-of-sample trades | >= 100 |
| OOS expectancy | >= +0.05 R, t-statistic >= 2.0 |
| OOS profit factor / Sharpe | >= 1.15 / >= 0.7 |
| OOS max drawdown | <= the configured drawdown limit |
| walk-forward efficiency (OOS / IS expectancy) | >= 0.4 |
| profitable OOS windows | >= 60% |
| holdout | >= 20 trades, expectancy > 0, no drawdown suspension |
| Monte Carlo | P(drawdown >= limit) <= 5%, P(loss) <= 20% |
| parameter neighbours profitable | >= 70% |
| cost-stressed expectancy | > 0 |
| deflated Sharpe probability | >= 0.80 |

A strategy that only works on optimised history fails the walk-forward
efficiency and holdout gates. The verdict (`validated` / `rejected` /
`synthetic_only`) is stored per symbol; demo and live trade `validated`
strategies only.

With H1 strategies, 100 out-of-sample trades needs several years of data or
several symbols - pass multiple `--data SYMBOL=file` arguments.

## Costs in backtests

Spread (per-bar recorded spread when the data has it, never below half the
configured typical), commission per lot per side, adverse slippage, execution
delay modelled as adverse price drift, swap per night (triple on Wednesday),
gap-through fills on stops, stop-before-target inside one bar, no positive
slippage on targets. Set `costs:` in `config/local.yaml` to your account's
real numbers.

## After deployment

The performance monitor compares each strategy's live R-multiples with its
validation baseline: significantly negative expectancy, significantly worse
than validated, an improbable losing streak, or a drawdown beyond the Monte
Carlo 95th percentile suspends the strategy and notifies you. Un-suspending is
manual (`python main.py resume --confirm --strategy NAME`) and resets the
status to `untested`.

## Adding a strategy

Subclass `Strategy` (`mt5quant/strategies/base.py`), declare `hypothesis`,
`entry_tf`, `context_tfs`, `regimes`, `defaults` and a small `grid`, implement
`_compute`, register it in `registry.py`. The look-ahead test and the
stop/target invariants run against every registered strategy automatically.
