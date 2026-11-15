# Status: what is tested, how, and what is not

Development and testing were done on Linux with Python 3.13. No MetaTrader 5
terminal, broker account, market-data vendor or news API was reachable from
that environment, and that shapes exactly what can be claimed below.

## Verified by automated tests (`python -m pytest`)

| Area | What the tests establish |
|---|---|
| Configuration | defaults are paper + conservative; unknown keys rejected; every risk limit refuses removal, zero, and values above its hard ceiling |
| Persistence | idempotent order reservation; trades recorded once; state survives reopen; audit hash chain detects edits and deletions |
| Logging | secrets and credential patterns are redacted; account numbers masked |
| Market data | resampling matches manual aggregation; five daily candles per FX week; HTF alignment never exposes an unfinished bar; FX week, DST, holidays, rollover, Friday/Monday rules |
| Indicators | known-value checks for RSI, EMA, MACD, Bollinger, ATR, ADX, VWAP, Donchian, swings, BOS/CHoCH, FVG, order blocks, sweeps, breakouts, zones, candles |
| **No look-ahead** | all 26 indicators and all 9 strategies give identical output on truncated history; a meta-test proves the guard catches a leaky feature |
| Strategies | every signal has a stop and target on the correct side, bounded risk distance, minimum reward/risk, cool-down, session rules; strategies do not fire on the same bars |
| AI engine | regime labels on constructed series; selector/confidence behaviour; anomaly flags; purged CV never trains on overlapping labels; meta-model accepts a planted edge, rejects noise and refuses a tampered file; degradation detection; pattern miner finds a planted effect and not noise |
| Risk | sizing never exceeds the budget and never rounds up; no size growth after losses; spread, margin, position, correlation, open-risk and daily-headroom limits; daily/weekly halts; persistent drawdown suspension; loss-streak cool-down; error breaker; kill switch |
| Simulated broker | bid/ask fills, commission, swap (triple Wednesday), adverse slippage, gap-through stops, stop-before-target, pending orders, MT5 return codes, JPY conversion, state restore |
| Execution | duplicate decisions never sent twice (also after restart); retries only when not executed; timeouts recovered or failed safely, never resent; foreign positions untouched |
| Engine (integration, replayed feed) | trades autonomously; every order has a stop and fits the risk budget; exactly-once trade recording; session rules; restart without duplicates; emergency stop flattens and blocks; drawdown suspension; reconnect; news block; validation requirement; auto-suspension of a degraded strategy; mode gates |
| News | provider parsing against recorded sample payloads; tier-1 classification; blackout windows; fail-closed when the calendar is unavailable; manual blackouts |
| Backtesting | next-bar-open execution; determinism; costs reduce results; drawdown halt; metrics on known trades; Monte Carlo; walk-forward windows strictly out-of-sample; validation rejects a no-edge series; synthetic data can never be `validated` |
| Dashboard | every required metric present; token enforcement; emergency stop and resume are audited; CSP headers; refuses public bind without a token |
| CLI | `check`, `stop`/`status`/`resume`, `backtest`, `authorize-live` refusal, bridge failure message off Windows, watchdog alert |

Additionally checked by hand in a real browser (Chromium): the dashboard
renders without console errors at desktop and phone widths, the chart hover
layer works, and the EMERGENCY STOP button reached a running engine, which
flattened and blocked entries. `python main.py run` was run end-to-end in paper
mode on the synthetic feed (this is how a timestamp-resolution bug was found
and fixed).

## Verified only against a stand-in

- **MetaTrader 5 adapter and gateway.** Request construction, gating, result
  handling, time-offset correction and the HTTP bridge are tested against a
  fake `MetaTrader5` module written from the MetaQuotes documentation
  (`tests/fake_mt5.py`). **This code has never talked to a real terminal.**
  Expect to find broker-specific details on first contact: filling modes,
  symbol suffixes, comment handling, server-time conventions, stop levels.
  First run it on a demo account and watch the logs and the audit trail.

## Not tested at all (no access from the build environment)

- Live calendar endpoints (`faireconomy`, `finnhub`) and the central-bank RSS
  feeds: parsers are tested on recorded samples only.
- Real delivery through Telegram, webhooks or SMTP: payloads are tested with
  injected transports.
- The Windows scheduled-task installer and the systemd unit: written, not run.
- Any real market data. Every backtest and validation run performed so far
  used the synthetic generator.

## What the synthetic validation run showed

`docs/sample_reports/validation_synthetic.md` is the output of
`python main.py validate --synthetic 160000 --symbols EURUSD GBPUSD USDJPY`.

- 5 of 9 strategies were **rejected** by the statistical gates.
- 4 trend-style strategies passed the statistical gates and were marked
  **`synthetic_only`** - not `validated`. They pass because the generator
  contains persistent trend regimes by construction, which trend logic is
  designed to capture.

This demonstrates that the pipeline runs and discriminates. **It is not
evidence that any strategy is profitable in a real market.** No strategy in
this repository currently has, or can obtain, a `validated` status until it is
run on broker history.

## Before trusting it with a demo account

1. Run `python main.py check` on the Windows machine.
2. Set your broker's real spreads and commission in `config/local.yaml`.
3. Download history, then run `validate` on it. Read the failed gates.
4. Run paper mode on real quotes; read the decision log.
5. Move to `mode: demo` only with at least one `validated` strategy.

See [LIVE_TRADING.md](LIVE_TRADING.md) for the rest of the path.
