# Strategy validation report

> **SYNTHETIC DATA.** These numbers come from generated random-walk prices, not from a market. They demonstrate that the pipeline works; they say nothing about profitability.

| Strategy | Verdict | OOS trades | OOS exp (R) | t-stat | PF | Sharpe | Max DD % | WF eff. | Holdout exp (R) | Gates failed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| scalp_pullback | **REJECTED** | 577 | 0.117 | 2.26 | 1.24 | 1.99 | 2.15 | 0.77 | 0.097 | 2 |
| intraday_vwap | **REJECTED** | 235 | 0.170 | 1.87 | 1.33 | 1.59 | 1.94 | 0.52 | 0.359 | 3 |
| trend_ema | **SYNTHETIC_ONLY** | 113 | 0.451 | 2.98 | 1.95 | 2.46 | 2.53 | 0.65 | 0.758 | 1 |
| donchian_breakout | **SYNTHETIC_ONLY** | 287 | 0.515 | 5.73 | 2.24 | 4.82 | 1.14 | 0.85 | 0.522 | 1 |
| bb_reversion | **REJECTED** | 77 | -0.035 | -0.19 | 0.91 | -0.33 | 1.86 | -0.88 | 0.368 | 12 |
| macd_momentum | **SYNTHETIC_ONLY** | 175 | 0.857 | 7.19 | 3.39 | 5.83 | 1.37 | 0.91 | 1.353 | 1 |
| liquidity_sweep | **REJECTED** | 472 | -0.076 | -1.21 | 0.88 | -0.90 | 5.91 | -4.18 | -0.240 | 13 |
| smc_orderblock | **SYNTHETIC_ONLY** | 262 | 0.480 | 4.71 | 2.05 | 3.77 | 1.23 | 0.97 | 0.517 | 1 |
| vol_squeeze | **REJECTED** | 124 | 0.092 | 0.81 | 1.19 | 0.71 | 3.22 | 0.40 | 0.473 | 5 |

## scalp_pullback - REJECTED

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"adx_min": 22.0, "rsi_reset": 45.0, "sl_buffer_atr": 0.3, "min_sl_atr": 1.0, "rr": 2.0, "max_bars": 24, "min_rr": 1.0}`

Failed gates:
- FAIL: Monte Carlo P(drawdown >= limit) 40.1% (max 5%)
- FAIL: cost-stress expectancy -0.032R (must stay > 0)

Passed gates:
- pass: out-of-sample trades 577 (min 100)
- pass: OOS expectancy +0.117R (min +0.05R)
- pass: OOS t-statistic 2.26 (min 2.0)
- pass: OOS profit factor 1.24 (min 1.15)
- pass: OOS Sharpe 1.99 (min 0.7)
- pass: OOS max drawdown 2.15% (max 5.0%)
- pass: walk-forward efficiency 0.77 (min 0.4)
- pass: profitable OOS windows 69% (min 60%)
- pass: holdout: 196 trades, expectancy +0.097R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(losing) 1.6% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: deflated Sharpe probability 0.81 over 9 trials (min 0.8)

Monte Carlo (5000 paths of 577 trades): return p05/p50/p95 = +3.75% / +18.16% / +34.21%, max drawdown p50/p95/p99 = 4.60% / 8.28% / 10.49%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.000 | 97 | -0.140 | 52 | -125.19 | `{"adx_min": 18.0, "rr": 1.5}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | -0.087 | 134 | +0.141 | 48 | +71.98 | `{"adx_min": 18.0, "rr": 1.5}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.058 | 142 | +0.339 | 58 | +279.60 | `{"adx_min": 18.0, "rr": 2.0}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +0.203 | 123 | -0.117 | 36 | -47.80 | `{"adx_min": 26.0, "rr": 2.0}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.189 | 118 | -0.009 | 35 | -19.45 | `{"adx_min": 26.0, "rr": 2.0}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.151 | 143 | +0.486 | 29 | +237.81 | `{"adx_min": 18.0, "rr": 2.0}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.139 | 105 | +0.152 | 40 | +91.80 | `{"adx_min": 22.0, "rr": 2.0}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.245 | 106 | +0.054 | 38 | +33.10 | `{"adx_min": 22.0, "rr": 2.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.293 | 95 | +0.159 | 31 | +65.15 | `{"adx_min": 26.0, "rr": 2.0}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.166 | 99 | +0.496 | 35 | +243.26 | `{"adx_min": 26.0, "rr": 2.0}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.239 | 117 | +0.097 | 66 | +80.33 | `{"adx_min": 18.0, "rr": 2.0}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.251 | 135 | +0.086 | 74 | +77.50 | `{"adx_min": 22.0, "rr": 2.0}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.179 | 173 | -0.120 | 35 | -38.39 | `{"adx_min": 22.0, "rr": 2.0}` |

## intraday_vwap - REJECTED

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"touch_atr": 0.15, "max_ext_atr": 1.6, "min_sl_atr": 1.0, "rr": 2.0, "max_bars": 32, "min_rr": 1.0}`

Failed gates:
- FAIL: OOS t-statistic 1.87 (min 2.0)
- FAIL: Monte Carlo P(drawdown >= limit) 11.6% (max 5%)
- FAIL: deflated Sharpe probability 0.65 over 9 trials (min 0.8)

Passed gates:
- pass: out-of-sample trades 235 (min 100)
- pass: OOS expectancy +0.170R (min +0.05R)
- pass: OOS profit factor 1.33 (min 1.15)
- pass: OOS Sharpe 1.59 (min 0.7)
- pass: OOS max drawdown 1.94% (max 5.0%)
- pass: walk-forward efficiency 0.52 (min 0.4)
- pass: profitable OOS windows 69% (min 60%)
- pass: holdout: 75 trades, expectancy +0.359R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(losing) 3.1% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: cost-stress expectancy +0.168R (must stay > 0)

Monte Carlo (5000 paths of 235 trades): return p05/p50/p95 = +1.12% / +10.24% / +20.60%, max drawdown p50/p95/p99 = 3.23% / 5.90% / 7.62%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.297 | 69 | +0.394 | 29 | +223.41 | `{"max_ext_atr": 1.6, "rr": 2.5}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.341 | 85 | -0.273 | 22 | -108.84 | `{"max_ext_atr": 1.6, "rr": 2.5}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.360 | 69 | +0.230 | 15 | +67.93 | `{"max_ext_atr": 1.2, "rr": 2.5}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +0.242 | 59 | +0.226 | 25 | +64.87 | `{"max_ext_atr": 1.2, "rr": 2.5}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.114 | 60 | +0.108 | 21 | +57.61 | `{"max_ext_atr": 1.2, "rr": 2.5}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.343 | 48 | +0.335 | 9 | +54.36 | `{"max_ext_atr": 0.8, "rr": 2.5}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.330 | 45 | +0.794 | 12 | +161.47 | `{"max_ext_atr": 0.8, "rr": 2.5}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.375 | 47 | +0.122 | 17 | +7.16 | `{"max_ext_atr": 1.2, "rr": 2.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.505 | 37 | +0.435 | 13 | +76.51 | `{"max_ext_atr": 0.8, "rr": 2.5}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.509 | 41 | -0.087 | 9 | -0.25 | `{"max_ext_atr": 0.8, "rr": 2.5}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.356 | 48 | +0.397 | 23 | +139.70 | `{"max_ext_atr": 1.2, "rr": 2.5}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.433 | 58 | -0.144 | 31 | -45.22 | `{"max_ext_atr": 1.6, "rr": 2.5}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.234 | 71 | -0.122 | 9 | -33.78 | `{"max_ext_atr": 1.6, "rr": 2.0}` |

## trend_ema - SYNTHETIC_ONLY

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"adx_min": 18.0, "zone_atr": 0.25, "sl_atr": 2.5, "rr": 3.0, "trail_atr": 2.5, "min_rr": 1.5}`

Failed gates:
- FAIL: data is SYNTHETIC: statistical gates passed, but this proves nothing about real markets - re-run on broker history

Passed gates:
- pass: out-of-sample trades 113 (min 100)
- pass: OOS expectancy +0.451R (min +0.05R)
- pass: OOS t-statistic 2.98 (min 2.0)
- pass: OOS profit factor 1.95 (min 1.15)
- pass: OOS Sharpe 2.46 (min 0.7)
- pass: OOS max drawdown 2.53% (max 5.0%)
- pass: walk-forward efficiency 0.65 (min 0.4)
- pass: profitable OOS windows 77% (min 60%)
- pass: holdout: 23 trades, expectancy +0.758R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(drawdown >= limit) 0.2% (max 5%)
- pass: Monte Carlo P(losing) 0.2% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: cost-stress expectancy +0.705R (must stay > 0)
- pass: deflated Sharpe probability 0.89 over 18 trials (min 0.8)

Monte Carlo (5000 paths of 113 trades): return p05/p50/p95 = +5.65% / +13.41% / +21.99%, max drawdown p50/p95/p99 = 1.83% / 3.32% / 4.18%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.754 | 30 | +0.783 | 8 | +134.45 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.820 | 31 | +0.040 | 11 | +30.24 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.708 | 26 | -0.377 | 7 | -37.09 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +0.196 | 28 | -0.612 | 12 | -143.80 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 1.5}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.055 | 34 | +0.996 | 9 | +178.85 | `{"adx_min": 22.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.392 | 27 | +1.241 | 9 | +164.97 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.865 | 29 | +0.534 | 8 | +72.63 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.5}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +1.054 | 25 | +0.913 | 8 | +144.24 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 2.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.894 | 26 | +0.466 | 10 | +53.55 | `{"adx_min": 18.0, "rr": 3.0, "sl_atr": 2.0}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.670 | 27 | +0.396 | 9 | +59.64 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 1.5}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.835 | 28 | +1.254 | 8 | +196.63 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 1.5}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.957 | 27 | -0.111 | 6 | -23.26 | `{"adx_min": 26.0, "rr": 3.0, "sl_atr": 1.5}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.928 | 28 | +0.625 | 8 | +62.99 | `{"adx_min": 18.0, "rr": 3.0, "sl_atr": 2.5}` |

Meta-model: rejected (OOS AUC n/a, 158 trades) - only 158 labelled trades (< 300)

## donchian_breakout - SYNTHETIC_ONLY

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"channel": 30, "squeeze_pct": 0.6, "min_rel_volume": 1.1, "sl_atr": 1.5, "rr": 2.5, "trail_atr": 2.5, "max_bars": 72, "min_rr": 1.2}`

Failed gates:
- FAIL: data is SYNTHETIC: statistical gates passed, but this proves nothing about real markets - re-run on broker history

Passed gates:
- pass: out-of-sample trades 287 (min 100)
- pass: OOS expectancy +0.515R (min +0.05R)
- pass: OOS t-statistic 5.73 (min 2.0)
- pass: OOS profit factor 2.24 (min 1.15)
- pass: OOS Sharpe 4.82 (min 0.7)
- pass: OOS max drawdown 1.14% (max 5.0%)
- pass: walk-forward efficiency 0.85 (min 0.4)
- pass: profitable OOS windows 100% (min 60%)
- pass: holdout: 80 trades, expectancy +0.522R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(drawdown >= limit) 0.0% (max 5%)
- pass: Monte Carlo P(losing) 0.0% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: cost-stress expectancy +0.488R (must stay > 0)
- pass: deflated Sharpe probability 1.00 over 18 trials (min 0.8)

Monte Carlo (5000 paths of 287 trades): return p05/p50/p95 = +30.84% / +44.49% / +59.92%, max drawdown p50/p95/p99 = 1.81% / 2.81% / 3.47%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.596 | 72 | +0.137 | 22 | +70.63 | `{"channel": 20, "rr": 2.5, "squeeze_pct": 0.45}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.454 | 67 | +0.153 | 20 | +61.48 | `{"channel": 30, "rr": 2.5, "squeeze_pct": 0.45}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.506 | 72 | +0.212 | 24 | +101.35 | `{"channel": 30, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +0.380 | 66 | +0.882 | 22 | +369.28 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.649 | 65 | +0.829 | 24 | +434.20 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.689 | 67 | +0.297 | 22 | +96.06 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.671 | 68 | +0.860 | 20 | +324.32 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.652 | 79 | +0.439 | 26 | +191.58 | `{"channel": 20, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.499 | 78 | +0.359 | 26 | +130.49 | `{"channel": 20, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.671 | 64 | +0.640 | 22 | +256.01 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.578 | 65 | +0.779 | 28 | +390.73 | `{"channel": 40, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.746 | 74 | +0.662 | 24 | +194.01 | `{"channel": 30, "rr": 2.5, "squeeze_pct": 0.6}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.760 | 75 | +0.139 | 7 | +18.53 | `{"channel": 30, "rr": 2.5, "squeeze_pct": 0.6}` |

Meta-model: rejected (OOS AUC 0.522, 365 trades) - out-of-sample AUC 0.522 < 0.55; filtered trades do not beat the unfiltered baseline out of sample; improves expectancy in only 50% of folds

## bb_reversion - REJECTED

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"bb_k": 2.0, "rsi_extreme": 30.0, "adx_max": 26.0, "sl_buffer_atr": 0.5, "min_sl_atr": 0.8, "max_bars": 24, "min_rr": 1.0}`

Failed gates:
- FAIL: out-of-sample trades 77 (min 100)
- FAIL: OOS expectancy -0.035R (min +0.05R)
- FAIL: OOS t-statistic -0.19 (min 2.0)
- FAIL: OOS profit factor 0.91 (min 1.15)
- FAIL: OOS Sharpe -0.33 (min 0.7)
- FAIL: walk-forward efficiency -0.88 (min 0.4)
- FAIL: profitable OOS windows 38% (min 60%)
- FAIL: Monte Carlo P(drawdown >= limit) 28.0% (max 5%)
- FAIL: Monte Carlo P(losing) 59.9% (max 20%)
- FAIL: parameter neighbours profitable 4% (min 70%)
- FAIL: cost-stress expectancy -0.169R (must stay > 0)
- FAIL: deflated Sharpe probability 0.01 over 18 trials (min 0.8)

Passed gates:
- pass: OOS max drawdown 1.86% (max 5.0%)
- pass: holdout: 24 trades, expectancy +0.368R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension

Monte Carlo (5000 paths of 77 trades): return p05/p50/p95 = -6.29% / -0.81% / +5.06%, max drawdown p50/p95/p99 = 3.84% / 7.41% / 8.95%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | -0.215 | 25 | -0.683 | 9 | -97.16 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | -0.240 | 22 | -0.411 | 8 | -63.09 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | -0.234 | 20 | +0.767 | 7 | +77.10 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | -0.054 | 21 | +0.730 | 1 | +11.72 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.157 | 16 | -0.223 | 7 | -27.01 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.300 | 15 | +0.878 | 7 | +112.77 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.360 | 15 | -0.172 | 4 | -7.03 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.353 | 21 | +0.168 | 7 | +7.42 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.490 | 21 | -0.135 | 6 | -9.77 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | -0.071 | 18 | +0.556 | 9 | +43.19 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.237 | 22 | -1.072 | 6 | -92.62 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | -0.043 | 17 | -0.633 | 3 | -18.53 | `{"adx_max": 22.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | -0.317 | 21 | -0.113 | 3 | -14.46 | `{"adx_max": 26.0, "bb_k": 2.0, "rsi_extreme": 30.0}` |

## macd_momentum - SYNTHETIC_ONLY

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"roc_n": 10, "sl_atr": 2.0, "rr": 2.5, "max_bars": 48, "min_rr": 1.0}`

Failed gates:
- FAIL: data is SYNTHETIC: statistical gates passed, but this proves nothing about real markets - re-run on broker history

Passed gates:
- pass: out-of-sample trades 175 (min 100)
- pass: OOS expectancy +0.857R (min +0.05R)
- pass: OOS t-statistic 7.19 (min 2.0)
- pass: OOS profit factor 3.39 (min 1.15)
- pass: OOS Sharpe 5.83 (min 0.7)
- pass: OOS max drawdown 1.37% (max 5.0%)
- pass: walk-forward efficiency 0.91 (min 0.4)
- pass: profitable OOS windows 92% (min 60%)
- pass: holdout: 53 trades, expectancy +1.353R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(drawdown >= limit) 0.0% (max 5%)
- pass: Monte Carlo P(losing) 0.0% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: cost-stress expectancy +1.024R (must stay > 0)
- pass: deflated Sharpe probability 1.00 over 9 trials (min 0.8)

Monte Carlo (5000 paths of 175 trades): return p05/p50/p95 = +33.68% / +45.38% / +57.91%, max drawdown p50/p95/p99 = 1.31% / 2.09% / 2.60%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +1.150 | 41 | -0.059 | 8 | -5.27 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.798 | 42 | +1.180 | 14 | +324.54 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.986 | 41 | +1.638 | 10 | +283.10 | `{"rr": 2.5, "sl_atr": 1.5}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +1.014 | 32 | +1.127 | 16 | +354.29 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +1.272 | 40 | +1.338 | 20 | +570.14 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +1.343 | 46 | +0.381 | 8 | +54.16 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +1.088 | 44 | +0.012 | 11 | +5.23 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.779 | 40 | +0.108 | 12 | +41.62 | `{"rr": 2.5, "sl_atr": 1.2}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.355 | 31 | +0.777 | 19 | +248.06 | `{"rr": 2.5, "sl_atr": 1.2}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.479 | 42 | +0.283 | 15 | +45.30 | `{"rr": 2.5, "sl_atr": 1.2}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.741 | 43 | +1.153 | 18 | +384.88 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.943 | 50 | +1.152 | 16 | +280.59 | `{"rr": 2.5, "sl_atr": 2.0}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +1.107 | 48 | +1.266 | 8 | +165.71 | `{"rr": 2.5, "sl_atr": 2.0}` |

Meta-model: rejected (OOS AUC n/a, 213 trades) - only 213 labelled trades (< 300)

## liquidity_sweep - REJECTED

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"min_pierce_atr": 0.05, "sl_buffer_atr": 0.3, "min_sl_atr": 0.8, "rr": 2.0, "adx_max": 35.0, "max_bars": 32, "min_rr": 1.2}`

Failed gates:
- FAIL: OOS expectancy -0.076R (min +0.05R)
- FAIL: OOS t-statistic -1.21 (min 2.0)
- FAIL: OOS profit factor 0.88 (min 1.15)
- FAIL: OOS Sharpe -0.90 (min 0.7)
- FAIL: OOS max drawdown 5.91% (max 5.0%)
- FAIL: walk-forward efficiency -4.18 (min 0.4)
- FAIL: profitable OOS windows 38% (min 60%)
- FAIL: holdout: 147 trades, expectancy -0.240R (min 20 trades, > +0.00R)
- FAIL: Monte Carlo P(drawdown >= limit) 98.6% (max 5%)
- FAIL: Monte Carlo P(losing) 88.8% (max 20%)
- FAIL: parameter neighbours profitable 0% (min 70%)
- FAIL: cost-stress expectancy -0.194R (must stay > 0)
- FAIL: deflated Sharpe probability 0.01 over 9 trials (min 0.8)

Passed gates:
- pass: out-of-sample trades 472 (min 100)
- pass: holdout did not hit the drawdown suspension

Monte Carlo (5000 paths of 472 trades): return p05/p50/p95 = -19.81% / -8.90% / +3.29%, max drawdown p50/p95/p99 = 12.89% / 21.59% / 25.50%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | -0.126 | 113 | -0.213 | 61 | -203.14 | `{"min_pierce_atr": 0.1, "rr": 1.5}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | -0.030 | 146 | -0.029 | 59 | -56.98 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | -0.071 | 161 | -0.108 | 45 | -86.08 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | -0.041 | 167 | +0.357 | 27 | +169.21 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.140 | 145 | -0.152 | 47 | -124.45 | `{"min_pierce_atr": 0.05, "rr": 2.5}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.139 | 113 | +0.061 | 31 | +28.89 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.191 | 99 | +0.115 | 24 | +36.23 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.158 | 98 | -0.158 | 40 | -106.65 | `{"min_pierce_atr": 0.2, "rr": 2.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.048 | 97 | +0.302 | 28 | +180.26 | `{"min_pierce_atr": 0.2, "rr": 2.0}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.150 | 91 | -0.480 | 34 | -207.51 | `{"min_pierce_atr": 0.2, "rr": 2.5}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | -0.032 | 110 | -0.125 | 30 | -112.54 | `{"min_pierce_atr": 0.1, "rr": 2.5}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | -0.061 | 98 | +0.053 | 26 | +72.53 | `{"min_pierce_atr": 0.1, "rr": 2.5}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | -0.137 | 104 | -0.347 | 20 | -126.71 | `{"min_pierce_atr": 0.05, "rr": 2.0}` |

## smc_orderblock - SYNTHETIC_ONLY

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"max_touches": 3, "sl_buffer_atr": 0.25, "min_sl_atr": 0.8, "rr": 3.0, "max_bars": 48, "min_rr": 1.5}`

Failed gates:
- FAIL: data is SYNTHETIC: statistical gates passed, but this proves nothing about real markets - re-run on broker history

Passed gates:
- pass: out-of-sample trades 262 (min 100)
- pass: OOS expectancy +0.480R (min +0.05R)
- pass: OOS t-statistic 4.71 (min 2.0)
- pass: OOS profit factor 2.05 (min 1.15)
- pass: OOS Sharpe 3.77 (min 0.7)
- pass: OOS max drawdown 1.23% (max 5.0%)
- pass: walk-forward efficiency 0.97 (min 0.4)
- pass: profitable OOS windows 85% (min 60%)
- pass: holdout: 82 trades, expectancy +0.517R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: Monte Carlo P(drawdown >= limit) 0.0% (max 5%)
- pass: Monte Carlo P(losing) 0.0% (max 20%)
- pass: parameter neighbours profitable 100% (min 70%)
- pass: cost-stress expectancy +0.397R (must stay > 0)
- pass: deflated Sharpe probability 1.00 over 9 trials (min 0.8)

Monte Carlo (5000 paths of 262 trades): return p05/p50/p95 = +23.63% / +36.46% / +50.83%, max drawdown p50/p95/p99 = 2.07% / 3.32% / 4.10%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.540 | 57 | +0.781 | 25 | +367.73 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.667 | 71 | +0.277 | 21 | +100.91 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.602 | 66 | +0.218 | 21 | +38.48 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | +0.445 | 67 | +0.465 | 26 | +180.93 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | +0.328 | 68 | +0.686 | 26 | +307.87 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.493 | 74 | +0.884 | 24 | +297.02 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.686 | 76 | +0.326 | 19 | +101.65 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.659 | 69 | +0.139 | 19 | +31.63 | `{"max_touches": 3, "rr": 3.0}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.544 | 48 | -0.113 | 13 | -5.86 | `{"max_touches": 2, "rr": 3.0}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.138 | 55 | +0.031 | 11 | -15.75 | `{"max_touches": 3, "rr": 3.0}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.208 | 31 | +0.252 | 20 | +91.80 | `{"max_touches": 2, "rr": 3.0}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.223 | 54 | +0.966 | 29 | +409.36 | `{"max_touches": 3, "rr": 3.0}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.648 | 65 | +0.501 | 8 | +93.54 | `{"max_touches": 3, "rr": 3.0}` |

Meta-model: rejected (OOS AUC 0.499, 327 trades) - out-of-sample AUC 0.499 < 0.55; improves expectancy in only 50% of folds

## vol_squeeze - REJECTED

Symbols: EURUSD, GBPUSD, USDJPY | data 2022-01-03 to 2024-02-19 | holdout from 2023-09-17
Final parameters: `{"min_squeeze_bars": 3, "kc_mult": 2.0, "sl_atr": 1.5, "rr": 2.0, "trail_atr": 2.0, "max_bars": 48, "min_rr": 1.0}`

Failed gates:
- FAIL: OOS t-statistic 0.81 (min 2.0)
- FAIL: walk-forward efficiency 0.40 (min 0.4)
- FAIL: Monte Carlo P(drawdown >= limit) 11.0% (max 5%)
- FAIL: Monte Carlo P(losing) 24.1% (max 20%)
- FAIL: deflated Sharpe probability 0.25 over 9 trials (min 0.8)

Passed gates:
- pass: out-of-sample trades 124 (min 100)
- pass: OOS expectancy +0.092R (min +0.05R)
- pass: OOS profit factor 1.19 (min 1.15)
- pass: OOS Sharpe 0.71 (min 0.7)
- pass: OOS max drawdown 3.22% (max 5.0%)
- pass: profitable OOS windows 69% (min 60%)
- pass: holdout: 37 trades, expectancy +0.473R (min 20 trades, > +0.00R)
- pass: holdout did not hit the drawdown suspension
- pass: parameter neighbours profitable 96% (min 70%)
- pass: cost-stress expectancy +0.132R (must stay > 0)

Monte Carlo (5000 paths of 124 trades): return p05/p50/p95 = -3.49% / +2.79% / +9.63%, max drawdown p50/p95/p99 = 3.00% / 5.92% / 7.73%

| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | Params |
|---|---|---:|---:|---:|---:|---:|---|
| 2022-01-03..2022-05-03 | 2022-06-12 | +0.358 | 43 | +0.466 | 11 | +87.43 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2022-02-12..2022-06-12 | 2022-07-22 | +0.338 | 44 | -0.319 | 16 | -93.96 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2022-03-24..2022-07-22 | 2022-08-31 | +0.187 | 41 | -0.813 | 13 | -179.77 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2022-05-03..2022-08-31 | 2022-10-10 | -0.030 | 16 | +1.829 | 1 | +26.82 | `{"kc_mult": 1.5, "min_squeeze_bars": 3}` |
| 2022-06-12..2022-10-10 | 2022-11-19 | -0.237 | 35 | +0.372 | 7 | +58.42 | `{"kc_mult": 2.0, "min_squeeze_bars": 4}` |
| 2022-07-22..2022-11-19 | 2022-12-29 | +0.008 | 31 | +0.084 | 8 | +15.48 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2022-08-31..2022-12-29 | 2023-02-07 | +0.444 | 26 | +0.021 | 11 | +3.26 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2022-10-10..2023-02-07 | 2023-03-19 | +0.402 | 16 | -0.198 | 5 | -7.65 | `{"kc_mult": 2.0, "min_squeeze_bars": 6}` |
| 2022-11-19..2023-03-19 | 2023-04-28 | +0.192 | 22 | +0.240 | 14 | +64.24 | `{"kc_mult": 2.0, "min_squeeze_bars": 4}` |
| 2022-12-29..2023-04-28 | 2023-06-07 | +0.248 | 28 | -0.251 | 11 | -64.12 | `{"kc_mult": 2.0, "min_squeeze_bars": 4}` |
| 2023-02-07..2023-06-07 | 2023-07-17 | +0.250 | 41 | +0.555 | 11 | +107.52 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2023-03-19..2023-07-17 | 2023-08-26 | +0.329 | 44 | +0.417 | 11 | +90.55 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
| 2023-04-28..2023-08-26 | 2023-09-17 | +0.379 | 39 | +1.265 | 5 | +102.59 | `{"kc_mult": 2.0, "min_squeeze_bars": 3}` |
