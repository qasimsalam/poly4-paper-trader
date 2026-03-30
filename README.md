# PolyTrader Paper Trading

Paper trades the winning mean-reversion strategy against live Polymarket data.
No real money. Logs hypothetical trades with realistic costs.

## Quick Start

```bash
pip install -r requirements.txt
python paper_trader.py
```

Open http://localhost:3000 to see the live dashboard.

## What It Does

- Polls Polymarket prices every 2 minutes
- Runs the strategy's entry/exit logic against a rolling price buffer
- Simulates trades with 1.4% round-trip costs (spread + slippage)
- Max 10 concurrent positions, $500 each, $10K starting capital
- Next-candle execution (signals fire one cycle, execute at next cycle's price)
- Logs everything to trades/trade_log.csv
- Shows real-time dashboard with activity feed, positions, P&L chart

## Strategy

Contrarian mean-reversion. Enters against 15.5%+ price spikes (with 2.5x adaptive threshold).
Takes profit at 13.5% reversion, stops loss at 10%, max 20h hold, 15-candle cooldown.

Backtested results (realistic costs):
- In-sample Sharpe: 12.62 (Jan-Mar 2026)
- Holdout Sharpe: 6.32 (Mar 15-29 2026)
- Q4 2025 OOS Sharpe: 6.54 (Oct-Dec 2025)

## Files

- `paper_trader.py` — main loop + web server (run this)
- `strategy.py` — frozen winning strategy (reference only)
- `backtester_realistic.py` — reference backtester (not used at runtime)
- `templates/dashboard.html` — web dashboard
- `state.json` — persistent state (auto-saved)
- `trades/trade_log.csv` — trade history
- `dashboard_cli.py` — terminal dashboard
