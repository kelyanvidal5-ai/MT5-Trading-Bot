# Architecture

## Layout

```
main.py                     command line (run, backtest, validate, stop, ...)
config/default.yaml         non-secret settings (local.yaml overrides, git-ignored)
mt5quant/
  core/           config + validation, domain models, logging/redaction, engine, wiring
  market_data/    timeframes (look-ahead-safe alignment), sessions calendar, loaders,
                  synthetic generator, replay/synthetic feeds, simulator contract specs
  indicators/     ta.py (classic), structure.py (price action / SMC), candles.py
  strategies/     base class, nine strategies, registry
  ai_engine/      regime, context + confidence + selector, anomaly, meta-model,
                  performance monitor, pattern miner, explainer
  news_engine/    events, calendar providers, monitor (blackouts, fail-closed)
  risk_management/ limits (hard ceilings), sizing, manager (gates + circuit breakers)
  execution/      broker interface, paper broker, MT5 adapter, bridge client,
                  order manager (idempotency), position manager, live gate
  bridge/         HMAC codec + gateway server (runs next to the terminal on Windows)
  backtesting/    costs, engine, metrics, walk-forward, Monte Carlo, validation, reports
  dashboard/      FastAPI app + static front end
  notifications/  Telegram / webhook / e-mail
  monitoring/     statistics, daily report, health
  database/       SQLite persistence + audit chain
scripts/watchdog.py         independent liveness check
deploy/                     systemd unit, cron, Windows Task Scheduler script
tests/                      unit, integration and risk tests
```

## Data flow of one decision

```
broker/terminal ──bars, ticks──► MarketData (all timeframes, completed bars only)
                                     │
                    ┌────────────────┼──────────────────┐
                    ▼                ▼                  ▼
              strategies       regime + MTF context   anomaly + news + sessions
           (signal, SL, TP)    (H1 regime, HTF trends)   (stand-aside flags)
                    └───────► selector + confidence ◄───┘
                                     │  conflict resolution
                                     ▼
                               RISK MANAGER  ── rejects with reasons
                                     │  volume
                                     ▼
                              ORDER MANAGER ──► broker (paper | MT5 | bridge)
                                     │
                    database: orders, decisions (+ explanation), trades, audit
```

Everything above the broker interface is identical in backtest, paper, demo and
live. Only the `Broker` implementation changes.

## Key technical choices

**One strategy code path.** `Strategy.generate()` is a vectorised, causal
function of completed bars. The backtester evaluates it over all history; the
live engine evaluates it over a trailing window and reads the last row. What is
tested is what trades. The same holds for the selector (`ai_engine/context.py`),
the risk manager and the position-management rules.

**Look-ahead protection is tested, not promised.** Higher-timeframe values
reach a lower-timeframe bar only through `align_htf`, which uses the last bar
whose *close* time has passed. `tests/test_no_lookahead.py` recomputes every
indicator and every strategy on truncated history and requires identical
output; a meta-test confirms the guard catches a deliberately leaky feature.

**Broker abstraction and the MT5 platform constraint.** The official
`MetaTrader5` Python package is Windows-only and talks to a locally running
terminal. Two supported topologies:

1. *All on Windows* (`broker_backend: mt5`): engine and terminal on the same
   Windows desktop/VPS.
2. *Bridge* (`broker_backend: bridge`): engine on Linux/macOS, terminal plus a
   small gateway (`python main.py bridge`) on Windows. Requests are signed with
   HMAC-SHA256 (timestamp + nonce, replay-protected), only whitelisted methods
   are callable, and `send_order` is idempotent on the gateway too.

The adapter receives the MT5 module by injection, so its logic is unit-tested
against a fake terminal on any OS.

**Duplicate-order prevention.** Each decision has a deterministic id derived
from (strategy, symbol, timeframe, bar time, side). The id is committed to the
database *before* sending and travels in the order comment. After a timeout or
crash the engine never resends; it looks for a position/order/deal carrying the
id. Only return codes that mean "not executed" are retried.

**SQLite (WAL).** One writer (engine), several readers (dashboard, CLI), no
server to run, crash-safe transactions. The audit log is a SHA-256 hash chain:
editing or deleting a row is detected.

**Risk limits are an envelope, not preferences.** Config values are validated
against hard ceilings in code; omitted, zero or excessive values stop the
engine from starting. Martingale, grid and averaging-down flags exist only so
that enabling them fails validation.

**AI that can only restrain.** The regime classifier is rule-based and fully
explainable. The ML meta-model is trained with purged walk-forward validation
and is used only if it beats the unfiltered baseline out of sample; it can
blend into confidence or veto a trade. Neither it nor the performance monitor
has any write access to risk limits, sizing rules or strategy deployment. The
explainer prints computed values only.

**Synthetic data is labelled everywhere.** The generator exists to exercise
the software. Backtest/validation reports carry a banner, the dashboard shows
a banner, and a strategy tested on synthetic data can never obtain the
`validated` status.

## Clocks

Market logic uses the broker clock (`broker.now()`), which is the terminal's
time in demo/live and a virtual clock in replay/synthetic mode. Wall-clock time
is used only for liveness (heartbeats), authorization expiry and the grace
period of unresolved orders.

## Known limitations

- The backtester resolves stops/targets on base-timeframe bars (stop first when
  both are inside one bar). With M1 base data this is close to tick accuracy;
  true tick replay is not implemented.
- Daily/weekly candles built from intraday data follow the New York 17:00
  close. Brokers on other server-time conventions will differ slightly; in
  demo/live the engine uses the broker's own D1/W1 bars.
- The swap model is a flat per-night charge (configurable), not broker tables.
- Generic contract specifications are used only by the simulator; demo/live
  always read the broker's specification.
- Geopolitical news has no structured, reliable free API. It is handled by the
  market-based anomaly detectors (volatility shocks, spread blow-outs), by
  operator-declared blackout windows, and by optional official central-bank
  headline feeds that are informational only.
