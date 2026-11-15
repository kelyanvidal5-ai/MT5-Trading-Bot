# From paper to live: the required path

The platform ships in **PAPER** mode. Real-money execution is locked behind a
gate that no single switch can open.

## Modes

| Mode | Orders go to | Requirements |
|---|---|---|
| `paper` (default) | internal simulator | none |
| `demo` | MT5 **demo** account | account type is verified at connect and before every order; only `validated` strategies trade |
| `live` | MT5 real account | every condition below, simultaneously |

## The live gate (`mt5quant/execution/live_gate.py`)

All four must hold at the same time:

1. `mode: live` in `config/local.yaml`.
2. Environment variable `MT5QUANT_LIVE_AUTHORIZED=I_AUTHORIZE_REAL_MONEY_TRADING`.
3. An authorization file created by the interactive command
   `python main.py authorize-live`. It names the exact account login, requires
   the phrase to be typed by hand, refuses to run non-interactively, and expires
   after `demo_gate.authorization_valid_days` (default 90).
4. A demo track record in the database: at least `demo_gate.min_demo_trades`
   (default 50) trades over `demo_gate.min_demo_days` (default 30).

The gate is evaluated when the engine starts, when the broker connects, and
again inside the MT5 adapter for **every order**. If the account type changes
(for example someone switches account in the terminal), orders are refused.

Nothing in the dashboard, the AI engine or the strategies can alter the gate.

## Recommended sequence

1. **Download your broker's history** (on Windows):
   `python main.py download-history --symbols EURUSD GBPUSD --timeframe M5 --days 1095`
2. **Validate** each strategy on that history:
   `python main.py validate --data EURUSD=data/EURUSD_M5.csv GBPUSD=data/GBPUSD_M5.csv`
   Expect most strategies to be rejected. Only `validated` ones may trade in
   demo. Set real spreads/commission for your account in `config/local.yaml`
   first (`costs:`), otherwise the test is optimistic or pessimistic.
3. **Paper** with real quotes (`paper.data_source: mt5` or `bridge`) for a few
   weeks. Check the decision log and the daily reports.
4. **Demo** (`mode: demo`) for at least the gate minimum. Compare live
   expectancy with the validation baseline (the engine does this continuously
   and suspends a strategy whose live results are statistically worse).
5. Only then consider `authorize-live`. Start with the default risk limits or
   tighter ones.

## What live authorization does not change

- Risk limits and their hard ceilings.
- The requirement that every strategy be `validated` for the symbol.
- The circuit breakers (daily/weekly loss, max drawdown suspension, kill switch).

## Stopping

- Dashboard: **EMERGENCY STOP** button.
- CLI: `python main.py stop "reason"`.
- Automatic: max drawdown, repeated order errors, repeated engine errors.

A stop blocks new entries, cancels pending orders and (with
`risk.flatten_on_emergency: true`) closes all positions owned by the system.
Resuming is always manual: `python main.py resume --confirm`.
