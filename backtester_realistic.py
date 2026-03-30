from __future__ import annotations
"""
Realistic backtester with transaction costs, slippage, position limits,
next-candle execution, and time-series Sharpe.

Fixes vs original backtester:
  1. Time-series Sharpe (daily portfolio returns, sqrt(252))
  2. 1% round-trip transaction cost (0.5% spread each way)
  3. 0.2% slippage per fill
  4. Next-candle execution (no look-ahead bias)
  5. Maximum 10 concurrent positions
  6. Capital tracking: $10,000 starting, $500 max per position
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data" / "prices"

SPREAD_COST_PCT = 0.005       # 0.5% each way
SLIPPAGE_PCT = 0.002          # 0.2% per fill
INITIAL_CAPITAL = 10_000.0
MAX_POSITION_SIZE = 500.0
MAX_CONCURRENT = 10

TRAIN_DAYS = 30
TEST_DAYS = 14
SLIDE_DAYS = 10
HOLDOUT_DAYS = 14
N_WINDOWS_TARGET = 4

INCONSISTENCY_PENALTY = 0.5
LOW_TRADE_PENALTY = 0.3
MIN_TRADES_PER_WINDOW = 5

# ---------------------------------------------------------------------------
# Data loading (same as original)
# ---------------------------------------------------------------------------

_price_cache: dict[str, pd.DataFrame] = {}


def _load_all_prices() -> dict[str, pd.DataFrame]:
    global _price_cache
    if _price_cache:
        return _price_cache
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"No data dir: {DATA_DIR}")
    files = list(DATA_DIR.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No parquets in {DATA_DIR}")
    for f in files:
        slug = f.stem
        df = pd.read_parquet(f)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = df["timestamp"].astype("int64")
        df["price"] = df["price"].astype("float64")
        _price_cache[slug] = df
    return _price_cache


def load_prices(start_ts: int, end_ts: int) -> dict[str, pd.DataFrame]:
    all_prices = _load_all_prices()
    result = {}
    for slug, df in all_prices.items():
        mask = (df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)
        window_df = df.loc[mask, ["timestamp", "price"]].reset_index(drop=True)
        if len(window_df) >= 10:
            result[slug] = window_df
    return result


# ---------------------------------------------------------------------------
# Walk-forward windows (day-based)
# ---------------------------------------------------------------------------

def _get_data_date_range() -> tuple[int, int]:
    all_prices = _load_all_prices()
    return (
        int(min(df["timestamp"].min() for df in all_prices.values())),
        int(max(df["timestamp"].max() for df in all_prices.values())),
    )


def _d2s(days: int) -> int:
    return days * 86400


def build_walk_forward_windows(data_start=None, data_end=None):
    if data_start is None or data_end is None:
        data_start, data_end = _get_data_date_range()
    total = data_end - data_start
    ideal = _d2s(TRAIN_DAYS + TEST_DAYS + HOLDOUT_DAYS)

    if total >= ideal:
        tr, te, ho, sl = _d2s(TRAIN_DAYS), _d2s(TEST_DAYS), _d2s(HOLDOUT_DAYS), _d2s(SLIDE_DAYS)
    else:
        r = total / ideal
        tr = max(int(_d2s(TRAIN_DAYS) * r), 3600)
        te = max(int(_d2s(TEST_DAYS) * r), 3600)
        ho = max(int(_d2s(HOLDOUT_DAYS) * r), 3600)
        sl = max(int(_d2s(SLIDE_DAYS) * r), 1800)

    usable_end = data_end - ho
    holdout = (usable_end, data_end)

    if usable_end <= data_start + tr:
        third = total // 3
        holdout = (data_end - third, data_end)
        usable_end = data_end - third
        te = max(third, 3600)
        tr = max(usable_end - data_start - te, 3600)
        sl = max(te // 2, 1800)

    windows = []
    cursor = data_start
    while True:
        t0 = cursor
        t1 = t0 + tr
        v0 = t1
        v1 = v0 + te
        if v1 > usable_end:
            break
        windows.append((t0, t1, v0, v1))
        cursor += sl
        if len(windows) >= N_WINDOWS_TARGET * 2:
            break

    if not windows:
        mid = (data_start + usable_end) // 2
        windows = [(data_start, mid, mid, usable_end)]

    if len(windows) > N_WINDOWS_TARGET:
        indices = np.linspace(0, len(windows) - 1, N_WINDOWS_TARGET, dtype=int)
        windows = [windows[i] for i in indices]

    return windows, holdout


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def _apply_costs(price: float, direction: str, is_entry: bool) -> float:
    """Apply spread + slippage. Entry costs more, exit receives less."""
    cost = SPREAD_COST_PCT + SLIPPAGE_PCT  # 0.7% per fill

    if is_entry:
        return price * (1 + cost) if direction == "long" else price * (1 - cost)
    else:
        return price * (1 - cost) if direction == "long" else price * (1 + cost)


# ---------------------------------------------------------------------------
# Portfolio simulation
# ---------------------------------------------------------------------------

def simulate_portfolio(trades: list[dict], price_data: dict[str, pd.DataFrame]) -> dict:
    """
    Simulate with realistic constraints: costs, slippage, next-candle
    execution, position limits, capital tracking, daily returns.
    """
    if not trades:
        return {"sharpe": 0.0, "total_trades": 0, "total_return_pct": 0.0,
                "daily_returns": [], "win_rate": 0.0}

    # Build next-candle price lookup per market
    next_price: dict[str, dict[int, tuple[int, float]]] = {}
    for slug, df in price_data.items():
        ts_arr = df["timestamp"].values
        pr_arr = df["price"].values
        lk = {}
        for j in range(len(ts_arr) - 1):
            lk[int(ts_arr[j])] = (int(ts_arr[j + 1]), float(pr_arr[j + 1]))
        next_price[slug] = lk

    sorted_trades = sorted(trades, key=lambda t: t["entry_time"])

    capital = INITIAL_CAPITAL
    open_pos: list[dict] = []
    executed: list[dict] = []
    daily_equity: dict[int, float] = {}

    # Collect all timestamps for time stepping
    all_ts = sorted(set(
        int(ts) for df in price_data.values() for ts in df["timestamp"].values
    ))
    if not all_ts:
        return {"sharpe": 0.0, "total_trades": 0, "total_return_pct": 0.0,
                "daily_returns": [], "win_rate": 0.0}

    trade_idx = 0

    for current_ts in all_ts:
        current_day = current_ts // 86400

        # Close positions that have reached exit time
        to_close = [p for p in open_pos if current_ts >= p["orig"]["exit_time"]]
        for pos in to_close:
            market = pos["orig"]["market"]
            raw_exit = pos["orig"]["exit_price"]
            # Next-candle execution for exit
            lk = next_price.get(market, {})
            exit_ts = pos["orig"]["exit_time"]
            if exit_ts in lk:
                _, raw_exit = lk[exit_ts]

            fill_exit = _apply_costs(raw_exit, pos["direction"], is_entry=False)

            if pos["direction"] == "long":
                pnl = (fill_exit - pos["fill_entry"]) * pos["shares"]
            else:
                pnl = (pos["fill_entry"] - fill_exit) * pos["shares"]

            capital += pos["pos_value"] + pnl
            executed.append({"pnl": pnl, "pos_value": pos["pos_value"]})
            open_pos.remove(pos)

        # Open new positions
        while trade_idx < len(sorted_trades):
            t = sorted_trades[trade_idx]
            if t["entry_time"] > current_ts:
                break
            trade_idx += 1

            if len(open_pos) >= MAX_CONCURRENT:
                continue
            if capital < MAX_POSITION_SIZE * 0.5:
                continue

            market = t["market"]
            direction = t["direction"]
            raw_entry = t["entry_price"]

            # Next-candle execution for entry
            lk = next_price.get(market, {})
            if t["entry_time"] in lk:
                _, raw_entry = lk[t["entry_time"]]

            if raw_entry < 1e-6:
                continue

            fill_entry = _apply_costs(raw_entry, direction, is_entry=True)
            pos_value = min(MAX_POSITION_SIZE, capital * 0.5)
            shares = pos_value / max(fill_entry, 1e-6)
            capital -= pos_value

            open_pos.append({
                "market": market, "direction": direction,
                "fill_entry": fill_entry, "shares": shares,
                "pos_value": pos_value, "orig": t,
            })

        # End-of-day equity (mark to market)
        total_eq = capital
        for pos in open_pos:
            mkt = pos["market"]
            if mkt in price_data:
                df = price_data[mkt]
                mask = df["timestamp"] <= current_ts
                if mask.any():
                    cur_p = float(df.loc[mask, "price"].iloc[-1])
                    if pos["direction"] == "long":
                        unreal = (cur_p - pos["fill_entry"]) * pos["shares"]
                    else:
                        unreal = (pos["fill_entry"] - cur_p) * pos["shares"]
                    total_eq += pos["pos_value"] + unreal
                else:
                    total_eq += pos["pos_value"]
            else:
                total_eq += pos["pos_value"]

        daily_equity[current_day] = total_eq

    # Daily returns
    days = sorted(daily_equity.keys())
    eq_series = [daily_equity[d] for d in days]
    daily_ret = []
    for i in range(1, len(eq_series)):
        if eq_series[i - 1] > 0:
            daily_ret.append((eq_series[i] - eq_series[i - 1]) / eq_series[i - 1])

    # Time-series Sharpe
    if len(daily_ret) >= 2:
        dr = np.array(daily_ret)
        m, s = np.mean(dr), np.std(dr, ddof=1)
        sharpe = float(m / s * math.sqrt(252)) if s > 1e-10 else 0.0
    else:
        sharpe = 0.0

    n_exec = len(executed)
    winners = sum(1 for e in executed if e["pnl"] > 0)
    win_rate = (winners / n_exec * 100) if n_exec > 0 else 0.0
    final_eq = eq_series[-1] if eq_series else INITIAL_CAPITAL
    total_return_pct = (final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    return {
        "sharpe": round(sharpe, 4),
        "total_trades": n_exec,
        "total_trades_attempted": len(sorted_trades),
        "total_return_pct": round(total_return_pct, 2),
        "final_equity": round(final_eq, 2),
        "win_rate": round(win_rate, 1),
        "daily_returns": daily_ret,
        "n_days": len(daily_ret),
    }


# ---------------------------------------------------------------------------
# Walk-forward evaluation
# ---------------------------------------------------------------------------

def evaluate(strategy_module: Any) -> dict:
    """Walk-forward evaluation with realistic simulation."""
    windows, _ = build_walk_forward_windows()

    window_sharpes = []
    total_trades = 0
    total_attempted = 0

    for i, (train_start, train_end, test_start, test_end) in enumerate(windows):
        test_prices = load_prices(test_start, test_end)
        if not test_prices:
            window_sharpes.append(-999.0)
            continue

        try:
            raw_trades = strategy_module.generate_trades(test_prices)
        except Exception as e:
            print(f"  Window {i+1}: strategy crashed: {e}")
            window_sharpes.append(-999.0)
            continue

        if not isinstance(raw_trades, list):
            window_sharpes.append(-999.0)
            continue

        result = simulate_portfolio(raw_trades, test_prices)
        total_trades += result["total_trades"]
        total_attempted += result["total_trades_attempted"]

        if result["total_trades"] < MIN_TRADES_PER_WINDOW:
            window_sharpes.append(-999.0)
        else:
            window_sharpes.append(result["sharpe"])

    valid = [s for s in window_sharpes if s != -999.0]
    if not valid:
        avg_sharpe = -999.0
    else:
        avg_sharpe = float(np.mean(valid))
        if any(s < 0 for s in valid):
            avg_sharpe *= INCONSISTENCY_PENALTY
        if total_trades < 100:
            avg_sharpe *= LOW_TRADE_PENALTY
        if any(s == -999.0 for s in window_sharpes):
            avg_sharpe *= len(valid) / len(window_sharpes)

    return {
        "walk_forward_sharpe": round(avg_sharpe, 6),
        "total_trades": total_trades,
        "total_attempted": total_attempted,
        "window_sharpes": [round(s, 4) for s in window_sharpes],
        "n_windows": len(windows),
    }
