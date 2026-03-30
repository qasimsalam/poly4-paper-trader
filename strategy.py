from __future__ import annotations
"""
Mean Reversion Strategy

Enter against sharp price spikes, betting the move will revert.
When price moves beyond a threshold in a short window, take the
opposite position expecting a bounce back.

Signal: price spike exceeding spike_threshold_pct over spike_window_minutes.
Direction: counter-trend (short after up-spike, long after down-spike).
"""

import numpy as np
import pandas as pd

PARAMS = {
    "spike_threshold_pct": 15.5,
    "spike_window_minutes": 40,
    "reversion_exit_pct": 13.5,
    "stop_loss_pct": 10.0,
    "max_hold_minutes": 1200,
}


def generate_trades(price_data: dict[str, pd.DataFrame]) -> list[dict]:
    """
    Scan for sharp price spikes and enter counter-trend positions.

    Args:
        price_data: dict mapping market_slug -> DataFrame[timestamp, price]

    Returns:
        List of trade dicts.
    """
    spike_thresh = PARAMS["spike_threshold_pct"] / 100.0
    spike_window = PARAMS["spike_window_minutes"]
    reversion_exit = PARAMS["reversion_exit_pct"] / 100.0
    sl = PARAMS["stop_loss_pct"] / 100.0
    max_hold = PARAMS["max_hold_minutes"] * 60

    trades = []

    for slug, df in price_data.items():
        if len(df) < 20:
            continue

        prices = df["price"].values
        timestamps = df["timestamp"].values

        if len(timestamps) > 1:
            median_gap = int(pd.Series(timestamps).diff().dropna().median())
        else:
            continue

        if median_gap <= 0:
            median_gap = 120

        window_candles = max(1, (spike_window * 60) // median_gap)

        in_trade = False
        entry_idx = 0
        entry_price = 0.0
        direction = "long"
        cooldown_until = 0

        for i in range(window_candles, len(prices)):
            if in_trade:
                hold_time = timestamps[i] - timestamps[entry_idx]
                current_price = prices[i]

                # Check reversion toward pre-spike level
                if direction == "long":
                    pnl_pct = (current_price - entry_price) / max(entry_price, 1e-6)
                else:
                    pnl_pct = (entry_price - current_price) / max(entry_price, 1e-6)

                exit_signal = False
                if pnl_pct >= reversion_exit:
                    exit_signal = True
                elif pnl_pct <= -sl:
                    exit_signal = True
                elif hold_time >= max_hold:
                    exit_signal = True

                if exit_signal:
                    trades.append({
                        "market": slug,
                        "direction": direction,
                        "entry_time": int(timestamps[entry_idx]),
                        "exit_time": int(timestamps[i]),
                        "entry_price": float(entry_price),
                        "exit_price": float(current_price),
                    })
                    in_trade = False
                    cooldown_until = i + 15

            else:
                if i < cooldown_until:
                    continue
                past_price = prices[i - window_candles]
                current_price = prices[i]

                if past_price < 1e-6:
                    continue

                change = (current_price - past_price) / past_price

                # Adaptive threshold: use fixed threshold OR 2x recent avg move
                if window_candles >= 5 and i >= window_candles * 2:
                    recent_changes = []
                    for j in range(i - window_candles, i):
                        if prices[j - window_candles] > 1e-6:
                            rc = abs((prices[j] - prices[j - window_candles]) / prices[j - window_candles])
                            recent_changes.append(rc)
                    if recent_changes:
                        adaptive_thresh = max(spike_thresh, np.mean(recent_changes) * 2.5)
                    else:
                        adaptive_thresh = spike_thresh
                else:
                    adaptive_thresh = spike_thresh

                if abs(change) >= adaptive_thresh:
                    in_trade = True
                    entry_idx = i
                    entry_price = current_price
                    # Counter-trend: if price spiked UP, go SHORT (bet on reversion)
                    direction = "short" if change > 0 else "long"

    return trades
