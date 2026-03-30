#!/usr/bin/env python3
"""
Terminal dashboard: reads trade_log.csv and prints summary stats.
Usage: python dashboard_cli.py
"""

import csv
import json
import math
from pathlib import Path

import numpy as np

TRADE_LOG = Path("trades/trade_log.csv")
STATE_FILE = Path("state.json")
INITIAL_CAPITAL = 10_000.0


def main():
    print("=" * 60)
    print("PolyTrader Paper Trading — CLI Dashboard")
    print("=" * 60)

    # Load state
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        print(f"\nCapital:     ${state.get('capital', 0):,.2f}")
        print(f"Open:        {len(state.get('open_positions', []))} positions")
        print(f"Total trades:{state.get('trade_count', 0)}")
        print(f"Started:     {state.get('started_at', 'N/A')}")
        print(f"Last poll:   {state.get('last_poll', 'N/A')}")
    else:
        print("\nNo state.json found. Is the paper trader running?")

    # Load trade log
    if not TRADE_LOG.exists():
        print("\nNo trade log found yet.")
        return

    entries = []
    exits = []
    with open(TRADE_LOG) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["action"] == "entry":
                entries.append(row)
            else:
                exits.append(row)

    if not exits:
        print(f"\nEntries logged: {len(entries)}, no completed trades yet.")
        return

    pnls = [float(e["position_pnl"]) for e in exits]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = len(pnls) - wins
    win_rate = wins / len(pnls) * 100

    print(f"\n--- Trade Summary ---")
    print(f"Completed:   {len(pnls)}")
    print(f"Win rate:    {win_rate:.1f}% ({wins}W / {losses}L)")
    print(f"Total P&L:   ${total_pnl:+,.2f}")
    print(f"Avg P&L:     ${np.mean(pnls):+,.2f}")
    print(f"Best trade:  ${max(pnls):+,.2f}")
    print(f"Worst trade: ${min(pnls):+,.2f}")

    # Daily Sharpe
    by_day = {}
    for e in exits:
        day = e["timestamp"][:10]
        by_day.setdefault(day, []).append(float(e["position_pnl"]))

    if len(by_day) >= 2:
        daily_returns = [sum(v) / INITIAL_CAPITAL for v in by_day.values()]
        m = np.mean(daily_returns)
        s = np.std(daily_returns, ddof=1)
        sharpe = m / s * math.sqrt(252) if s > 1e-10 else 0.0
        print(f"Daily Sharpe:{sharpe:.2f}")

    # Comparison to backtest
    print(f"\n--- Expected vs Actual ---")
    print(f"  Backtest win rate:  49-54%")
    print(f"  Actual win rate:    {win_rate:.1f}%")
    bt_avg = 0.28  # approximate from backtest
    actual_avg_pct = np.mean(pnls) / 500 * 100  # rough per-position %
    print(f"  Backtest avg ret:   ~{bt_avg:.2f}%/trade")
    print(f"  Actual avg ret:     ~{actual_avg_pct:.2f}%/trade")

    # Open positions
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        positions = state.get("open_positions", [])
        if positions:
            print(f"\n--- Open Positions ({len(positions)}) ---")
            for p in positions:
                print(f"  {p['direction']:5s} {p['market'][:40]}")


if __name__ == "__main__":
    main()
