#!/usr/bin/env python3
"""
PolyTrader Paper Trading System

Single process: runs trading loop (every 2 min) + web dashboard (port 3000).
No real money — logs hypothetical trades with realistic costs.

Usage: python paper_trader.py
"""

import asyncio
import csv
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

INITIAL_CAPITAL = 10_000.0
MAX_POSITION_SIZE = 500.0
MAX_CONCURRENT = 20           # CHANGE 2: increased from 10 to 20
POLL_INTERVAL = 120           # seconds (2 minutes)
MARKET_FETCH_LIMIT = 1000     # CHANGE 1: increased from 200 to 1000

# Costs (same as backtester_realistic.py)
SPREAD_COST_PCT = 0.005  # 0.5% each way
SLIPPAGE_PCT = 0.002     # 0.2% per fill

# Strategy params (from strategy.py)
SPIKE_THRESHOLD = 0.155       # 15.5%
SPIKE_WINDOW_CANDLES = 20     # 40min / 2min = 20 candles
REVERSION_EXIT = 0.135        # 13.5%
STOP_LOSS = 0.10              # 10%
MAX_HOLD_SECONDS = 1200 * 60  # 1200 min = 20 hours
COOLDOWN_CANDLES = 15
ADAPTIVE_MULT = 2.5

PRICE_BUFFER_SIZE = 60  # 2 hours of 2-min candles
STALE_PRICE_THRESHOLD = 10    # CHANGE 6: polls before flagging stale

STATE_FILE = Path("state.json")
TRADE_LOG = Path("trades/trade_log.csv")
TEMPLATE_DIR = Path("templates")

# CHANGE 3: patterns for short-duration binary markets
BINARY_PATTERNS = re.compile(r'up.or.down|up-or-down', re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("paper_trader")


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def apply_costs(price: float, direction: str, is_entry: bool) -> float:
    """Apply spread + slippage to fill price."""
    cost = SPREAD_COST_PCT + SLIPPAGE_PCT  # 0.7% per fill
    if is_entry:
        return price * (1 + cost) if direction == "long" else price * (1 - cost)
    else:
        return price * (1 - cost) if direction == "long" else price * (1 + cost)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def _is_short_duration_binary(market: dict) -> bool:
    """CHANGE 3: Check if market is a short-duration binary (up/down crypto)."""
    q = market.get("question", "")
    slug = market.get("slug", "")
    if BINARY_PATTERNS.search(q) or BINARY_PATTERNS.search(slug):
        return True
    # Check end date within 24 hours
    end_date = market.get("end_date", "")
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if end_dt - datetime.now(timezone.utc) < timedelta(hours=24):
                return True
        except (ValueError, TypeError):
            pass
    return False


def fetch_market_list() -> list[dict]:
    """Fetch top active markets from Gamma API."""
    markets = []
    filtered_count = 0
    for offset in range(0, MARKET_FETCH_LIMIT, 100):  # CHANGE 1: use MARKET_FETCH_LIMIT
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={
                "limit": 100, "offset": offset, "order": "volume",
                "ascending": "false", "closed": "false",
            }, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            log.warning(f"Failed to fetch markets at offset {offset}: {e}")
            break
        if not batch:
            break
        for m in batch:
            token_ids = m.get("clobTokenIds")
            if not token_ids:
                continue
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except json.JSONDecodeError:
                    continue
            if token_ids and token_ids[0]:
                slug = m.get("question", "unknown")[:60].lower().replace(" ", "-")
                market_entry = {
                    "slug": slug,
                    "token_id": token_ids[0],
                    "question": m.get("question", ""),
                    "end_date": m.get("endDate", ""),
                }
                # CHANGE 3: filter short-duration binaries
                if _is_short_duration_binary(market_entry):
                    filtered_count += 1
                    continue
                markets.append(market_entry)
        time.sleep(0.05)
    if filtered_count > 0:
        log.info(f"Filtered {filtered_count} short-duration binary markets")
    return markets


def fetch_current_prices(markets: list[dict]) -> dict[str, float]:
    """Fetch current midpoint price for each market."""
    prices = {}
    for m in markets:
        try:
            resp = requests.get(f"{CLOB_API}/price", params={
                "token_id": m["token_id"], "side": "buy",
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                price = float(data.get("price", 0))
                if 0 < price < 1:
                    prices[m["slug"]] = price
        except Exception:
            pass
        time.sleep(0.05)  # light rate limiting
    return prices


# ---------------------------------------------------------------------------
# Paper Trader
# ---------------------------------------------------------------------------

class PaperTrader:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.open_positions: list[dict] = []
        self.trade_history: list[dict] = []
        self.trade_count = 0
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_poll = None

        # Price buffer: slug -> deque of (timestamp, price)
        self.price_buffer: dict[str, deque] = {}
        self.cooldowns: dict[str, int] = {}  # slug -> poll_count when cooldown expires
        self.poll_count = 0

        # Pending signals (next-candle execution)
        self.pending_entries: list[dict] = []
        self.pending_exits: list[dict] = []

        # Market list (refreshed periodically)
        self.markets: list[dict] = []
        self.market_refresh_count = 0

        # WebSocket clients
        self.ws_clients: list[WebSocket] = []

        # Activity feed
        self.activity_feed: list[dict] = []

        # P&L history for chart
        self.pnl_history: list[dict] = []

        # Scan diagnostics (updated each cycle)
        self.scan_info: dict = {
            "buffer_candles": 0,
            "buffer_needed": SPIKE_WINDOW_CANDLES,
            "scanning_active": False,
            "markets_scanned": 0,
            "largest_spike_pct": 0.0,
            "largest_spike_market": "",
            "adaptive_filtered": "",
            "adaptive_thresh_used": 0.0,
            "status_phase": "building",
            "status_text": "",
        }

        # CHANGE 6: stale price tracking — slug -> consecutive same-price count
        self.stale_counts: dict[str, int] = {}
        self.last_prices: dict[str, float] = {}

        self.load_state()

    # --- State persistence ---

    def save_state(self):
        state = {
            "open_positions": self.open_positions,
            "capital": self.capital,
            "trade_count": self.trade_count,
            "started_at": self.started_at,
            "last_poll": self.last_poll,
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def load_state(self):
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                self.open_positions = state.get("open_positions", [])
                self.capital = state.get("capital", INITIAL_CAPITAL)
                self.trade_count = state.get("trade_count", 0)
                self.started_at = state.get("started_at", self.started_at)
                self.last_poll = state.get("last_poll")
                log.info(f"Restored state: capital=${self.capital:.2f}, "
                         f"{len(self.open_positions)} open positions, "
                         f"{self.trade_count} total trades")
            except Exception as e:
                log.warning(f"Failed to load state: {e}")

    # --- Trade logging ---

    def log_trade(self, entry: dict):
        TRADE_LOG.parent.mkdir(exist_ok=True)
        file_exists = TRADE_LOG.exists()
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "market", "action", "direction", "price",
                    "fill_price", "position_pnl", "capital", "open_positions_count",
                ])
            writer.writerow([
                entry["timestamp"], entry["market"], entry["action"],
                entry["direction"], f"{entry['price']:.6f}",
                f"{entry['fill_price']:.6f}", f"{entry.get('pnl', 0):.2f}",
                f"{self.capital:.2f}", len(self.open_positions),
            ])

    # --- Activity feed ---

    def add_activity(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = {"time": ts, "msg": msg, "level": level}
        self.activity_feed.append(entry)
        if len(self.activity_feed) > 200:
            self.activity_feed = self.activity_feed[-200:]

    # --- CHANGE 4: Resolution detection ---

    def check_resolved_positions(self, prices: dict[str, float]):
        """Check if any open positions have resolved (price at 0 or 1)."""
        for pos in list(self.open_positions):
            slug = pos["market"]
            if slug not in prices:
                continue
            cp = prices[slug]
            # Markets resolve to ~0 or ~1
            if cp <= 0.01 or cp >= 0.99:
                # Exit at resolution price
                fill_price = apply_costs(cp, pos["direction"], is_entry=False)
                if pos["direction"] == "long":
                    pnl_per_share = fill_price - pos["fill_price"]
                else:
                    pnl_per_share = pos["fill_price"] - fill_price
                pnl_dollar = pnl_per_share * pos["shares"]
                self.capital += pos["pos_value"] + pnl_dollar
                self.trade_count += 1

                trade_record = {
                    "market": slug,
                    "direction": pos["direction"],
                    "entry_price": pos["entry_price"],
                    "exit_price": cp,
                    "fill_entry": pos["fill_price"],
                    "fill_exit": fill_price,
                    "pnl": pnl_dollar,
                    "pnl_pct": pnl_dollar / pos["pos_value"] * 100 if pos["pos_value"] > 0 else 0,
                    "hold_time": time.time() - pos["entry_time"],
                    "reason": "RESOLVED",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                self.trade_history.append(trade_record)
                self.log_trade({
                    "timestamp": trade_record["timestamp"],
                    "market": slug, "action": "exit", "direction": pos["direction"],
                    "price": cp, "fill_price": fill_price, "pnl": pnl_dollar,
                })
                color = "profit" if pnl_dollar > 0 else "loss"
                self.add_activity(
                    f"EXIT: {slug[:40]} (RESOLVED @ ${cp:.2f}) -- P&L: ${pnl_dollar:+.2f}",
                    color
                )
                log.info(f"EXIT: {slug[:40]} (RESOLVED @ ${cp:.2f}) -- P&L: ${pnl_dollar:+.2f}")
                self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
                self.open_positions.remove(pos)

    # --- CHANGE 6: Stale price detection ---

    def check_stale_prices(self, prices: dict[str, float]):
        """Warn if open position prices haven't changed for STALE_PRICE_THRESHOLD polls."""
        for pos in self.open_positions:
            slug = pos["market"]
            if slug not in prices:
                continue
            cp = prices[slug]
            if slug in self.last_prices and abs(cp - self.last_prices[slug]) < 1e-8:
                self.stale_counts[slug] = self.stale_counts.get(slug, 0) + 1
            else:
                self.stale_counts[slug] = 0
            self.last_prices[slug] = cp

            if self.stale_counts[slug] == STALE_PRICE_THRESHOLD:
                msg = f"WARNING: {slug[:40]} price unchanged for {STALE_PRICE_THRESHOLD}+ polls -- may have resolved"
                self.add_activity(msg, "signal")
                log.warning(msg)

    # --- Signal detection ---

    def check_entry_signals(self, prices: dict[str, float]):
        """Check for new entry signals using strategy logic."""
        # Reset scan diagnostics
        largest_spike = 0.0
        largest_spike_market = ""
        markets_scanned = 0
        adaptive_filtered_market = ""
        adaptive_filtered_thresh = 0.0

        # Determine buffer depth (use max across all markets)
        buffer_depths = [len(buf) for buf in self.price_buffer.values()] if self.price_buffer else [0]
        max_buf = max(buffer_depths) if buffer_depths else 0
        scanning_active = max_buf >= SPIKE_WINDOW_CANDLES + 1

        self.scan_info["buffer_candles"] = max_buf
        self.scan_info["scanning_active"] = scanning_active

        if not scanning_active:
            remaining_candles = SPIKE_WINDOW_CANDLES + 1 - max_buf
            remaining_min = remaining_candles * 2
            self.scan_info["status_phase"] = "building"
            self.scan_info["status_text"] = (
                f"Building price buffer: {max_buf}/{SPIKE_WINDOW_CANDLES} candles "
                f"({remaining_min} minutes remaining)"
            )

        for slug, current_price in prices.items():
            if slug not in self.price_buffer:
                continue
            buf = self.price_buffer[slug]
            if len(buf) < SPIKE_WINDOW_CANDLES + 1:
                continue

            # Price filter: skip resolved/near-resolved markets
            if current_price < 0.10 or current_price > 0.90:
                continue

            # Median price filter: skip markets that aren't in the uncertain range
            buf_prices = [p for _, p in buf]
            med_price = float(np.median(buf_prices))
            if med_price < 0.15 or med_price > 0.85:
                continue

            markets_scanned += 1

            # Cooldown check
            if slug in self.cooldowns and self.poll_count < self.cooldowns[slug]:
                continue

            # Already in a position for this market?
            if any(p["market"] == slug for p in self.open_positions):
                continue

            # Already pending?
            if any(p["market"] == slug for p in self.pending_entries):
                continue

            # Max positions check
            if len(self.open_positions) + len(self.pending_entries) >= MAX_CONCURRENT:
                continue

            # Capital check
            if self.capital < MAX_POSITION_SIZE * 0.5:
                continue

            # Get price from SPIKE_WINDOW_CANDLES ago
            past_idx = len(buf) - 1 - SPIKE_WINDOW_CANDLES
            if past_idx < 0:
                continue
            past_price = buf[past_idx][1]

            if past_price < 1e-6:
                continue

            change = (current_price - past_price) / past_price

            # Track largest spike
            if abs(change) > abs(largest_spike):
                largest_spike = change
                largest_spike_market = slug

            # Adaptive threshold
            recent_changes = []
            for j in range(max(0, len(buf) - SPIKE_WINDOW_CANDLES - 1), len(buf) - 1):
                if j - SPIKE_WINDOW_CANDLES >= 0:
                    p_old = buf[j - SPIKE_WINDOW_CANDLES][1]
                    p_new = buf[j][1]
                    if p_old > 1e-6:
                        recent_changes.append(abs((p_new - p_old) / p_old))

            if recent_changes:
                adaptive_thresh = max(SPIKE_THRESHOLD, np.mean(recent_changes) * ADAPTIVE_MULT)
            else:
                adaptive_thresh = SPIKE_THRESHOLD

            # Track adaptive filtering
            if abs(change) >= SPIKE_THRESHOLD and abs(change) < adaptive_thresh:
                adaptive_filtered_market = slug
                adaptive_filtered_thresh = adaptive_thresh

            if abs(change) >= adaptive_thresh:
                direction = "short" if change > 0 else "long"
                self.pending_entries.append({
                    "market": slug,
                    "direction": direction,
                    "signal_price": current_price,
                    "signal_time": time.time(),
                    "change_pct": change * 100,
                })
                self.add_activity(
                    f"SIGNAL: {slug[:40]} spiked {change*100:.1f}% -- flagging {direction} entry",
                    "signal"
                )

        # Update scan diagnostics
        self.scan_info["markets_scanned"] = markets_scanned
        self.scan_info["largest_spike_pct"] = round(abs(largest_spike) * 100, 1)
        self.scan_info["largest_spike_market"] = largest_spike_market[:40]
        self.scan_info["adaptive_filtered"] = adaptive_filtered_market[:40]
        self.scan_info["adaptive_thresh_used"] = round(adaptive_filtered_thresh * 100, 1)

        # Update status phase
        if self.pending_entries:
            self.scan_info["status_phase"] = "signal"
            sig = self.pending_entries[-1]
            self.scan_info["status_text"] = (
                f"SIGNAL DETECTED: {sig['market'][:40]} spiked {sig['change_pct']:.1f}% "
                f"-- entering next cycle"
            )
        elif self.open_positions:
            self.scan_info["status_phase"] = "monitoring"
            self.scan_info["status_text"] = (
                f"Monitoring {len(self.open_positions)} open position(s) -- "
                f"scanning {markets_scanned} markets"
            )
        elif scanning_active:
            self.scan_info["status_phase"] = "scanning"
            self.scan_info["status_text"] = (
                f"Scanning {markets_scanned} markets every 2 min -- "
                f"largest spike this cycle: {abs(largest_spike)*100:.1f}% (need {SPIKE_THRESHOLD*100:.1f}%)"
            )

    def check_exit_signals(self, prices: dict[str, float]):
        """Check open positions for exit conditions."""
        now = time.time()
        for pos in list(self.open_positions):
            slug = pos["market"]
            if slug not in prices:
                continue

            current_price = prices[slug]
            entry_fill = pos["fill_price"]

            if pos["direction"] == "long":
                pnl_pct = (current_price - entry_fill) / max(entry_fill, 1e-6)
            else:
                pnl_pct = (entry_fill - current_price) / max(entry_fill, 1e-6)

            hold_time = now - pos["entry_time"]
            reason = None

            if pnl_pct >= REVERSION_EXIT:
                reason = "TP"
            elif pnl_pct <= -STOP_LOSS:
                reason = "SL"
            elif hold_time >= MAX_HOLD_SECONDS:
                reason = "timeout"

            if reason:
                # Already pending exit?
                if any(p["market"] == slug for p in self.pending_exits):
                    continue
                self.pending_exits.append({
                    "market": slug,
                    "signal_price": current_price,
                    "reason": reason,
                    "pnl_pct": pnl_pct,
                })
                self.add_activity(
                    f"EXIT SIGNAL: {slug[:40]} ({reason}, PnL {pnl_pct*100:.1f}%) -- flagging exit",
                    "signal"
                )

    # --- Execution ---

    def execute_pending_entries(self, prices: dict[str, float]):
        """Execute pending entry signals at current prices (next-candle)."""
        for sig in list(self.pending_entries):
            slug = sig["market"]
            # CHANGE 5: log when pending entries are discarded
            if slug not in prices:
                self.add_activity(
                    f"ENTRY SKIPPED: {slug[:40]} -- price not available on execution cycle",
                    "signal"
                )
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- price not available on execution cycle")
                self.pending_entries.remove(sig)
                continue

            if len(self.open_positions) >= MAX_CONCURRENT:
                self.add_activity(
                    f"ENTRY SKIPPED: {slug[:40]} -- max positions ({MAX_CONCURRENT}) reached",
                    "signal"
                )
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- max positions reached")
                self.pending_entries.remove(sig)
                continue

            if self.capital < MAX_POSITION_SIZE * 0.5:
                self.add_activity(
                    f"ENTRY SKIPPED: {slug[:40]} -- insufficient capital (${self.capital:.0f})",
                    "signal"
                )
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- insufficient capital")
                self.pending_entries.remove(sig)
                continue

            raw_price = prices[slug]
            fill_price = apply_costs(raw_price, sig["direction"], is_entry=True)
            pos_value = min(MAX_POSITION_SIZE, self.capital * 0.5)

            self.capital -= pos_value

            pos = {
                "market": slug,
                "direction": sig["direction"],
                "entry_time": time.time(),
                "entry_price": raw_price,
                "fill_price": fill_price,
                "pos_value": pos_value,
                "shares": pos_value / max(fill_price, 1e-6),
            }
            self.open_positions.append(pos)

            self.log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": slug, "action": "entry", "direction": sig["direction"],
                "price": raw_price, "fill_price": fill_price, "pnl": 0,
            })

            self.add_activity(
                f"ENTRY: {sig['direction'].upper()} {slug[:40]} @ ${raw_price:.4f} "
                f"(fill: ${fill_price:.4f})",
                "entry"
            )
            self.pending_entries.remove(sig)

    def execute_pending_exits(self, prices: dict[str, float]):
        """Execute pending exit signals at current prices (next-candle)."""
        for sig in list(self.pending_exits):
            slug = sig["market"]
            pos = next((p for p in self.open_positions if p["market"] == slug), None)
            if not pos:
                self.pending_exits.remove(sig)
                continue

            raw_price = prices.get(slug, sig["signal_price"])
            fill_price = apply_costs(raw_price, pos["direction"], is_entry=False)

            if pos["direction"] == "long":
                pnl_per_share = fill_price - pos["fill_price"]
            else:
                pnl_per_share = pos["fill_price"] - fill_price

            pnl_dollar = pnl_per_share * pos["shares"]
            self.capital += pos["pos_value"] + pnl_dollar
            self.trade_count += 1

            trade_record = {
                "market": slug,
                "direction": pos["direction"],
                "entry_price": pos["entry_price"],
                "exit_price": raw_price,
                "fill_entry": pos["fill_price"],
                "fill_exit": fill_price,
                "pnl": pnl_dollar,
                "pnl_pct": pnl_dollar / pos["pos_value"] * 100 if pos["pos_value"] > 0 else 0,
                "hold_time": time.time() - pos["entry_time"],
                "reason": sig["reason"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.trade_history.append(trade_record)

            self.log_trade({
                "timestamp": trade_record["timestamp"],
                "market": slug, "action": "exit", "direction": pos["direction"],
                "price": raw_price, "fill_price": fill_price, "pnl": pnl_dollar,
            })

            color = "profit" if pnl_dollar > 0 else "loss"
            self.add_activity(
                f"EXIT: {slug[:40]} ({sig['reason']}) -- P&L: ${pnl_dollar:+.2f}",
                color
            )

            self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
            self.open_positions.remove(pos)
            self.pending_exits.remove(sig)

    # --- Main loop ---

    async def run_loop(self):
        """Main trading loop: poll, execute, signal, repeat."""
        log.info("=" * 60)
        log.info("PolyTrader Paper Trading System")
        log.info(f"Capital: ${self.capital:,.2f} | Max positions: {MAX_CONCURRENT}")
        log.info(f"Costs: {(SPREAD_COST_PCT+SLIPPAGE_PCT)*200:.1f}% round-trip")
        log.info(f"Market limit: {MARKET_FETCH_LIMIT} | Binary filter: ON")
        log.info(f"Dashboard: http://localhost:3000")
        log.info("=" * 60)

        # Fetch market list
        log.info("Fetching market list...")
        self.markets = fetch_market_list()
        log.info(f"Found {len(self.markets)} markets (after filtering)")

        while True:
            try:
                cycle_start = time.time()
                self.poll_count += 1
                now = datetime.now(timezone.utc)

                # Refresh market list every 30 polls (1 hour)
                if self.poll_count % 30 == 0:
                    self.markets = fetch_market_list()

                # Poll prices
                prices = fetch_current_prices(self.markets)
                n_prices = len(prices)
                self.last_poll = now.isoformat()

                # Update price buffer
                ts = int(now.timestamp())
                for slug, price in prices.items():
                    if slug not in self.price_buffer:
                        self.price_buffer[slug] = deque(maxlen=PRICE_BUFFER_SIZE)
                    self.price_buffer[slug].append((ts, price))

                # CHANGE 4: check for resolved positions
                self.check_resolved_positions(prices)

                # Execute pending signals from last cycle (next-candle)
                self.execute_pending_entries(prices)
                self.execute_pending_exits(prices)

                # Check for new exit signals
                self.check_exit_signals(prices)

                # Check for new entry signals (also updates scan_info)
                self.check_entry_signals(prices)

                # CHANGE 6: check for stale prices on open positions
                self.check_stale_prices(prices)

                # Build descriptive activity feed message
                si = self.scan_info
                if not si["scanning_active"]:
                    self.add_activity(
                        f"Polled {n_prices} markets | Buffer: {si['buffer_candles']}/{SPIKE_WINDOW_CANDLES} candles",
                        "poll"
                    )
                elif self.pending_entries:
                    pass  # signal activity already added in check_entry_signals
                else:
                    parts = [f"Scanned {si['markets_scanned']} markets"]
                    if si["largest_spike_pct"] > 0:
                        parts.append(f"Top spike: {si['largest_spike_pct']:.1f}% in {si['largest_spike_market'][:25]}")
                    else:
                        parts.append("No spikes detected")
                    parts.append(f"need {SPIKE_THRESHOLD*100:.1f}%")
                    if si["adaptive_filtered"]:
                        self.add_activity(
                            f"Spike {si['adaptive_thresh_used']:.0f}% threshold filtered {si['adaptive_filtered'][:25]}",
                            "info"
                        )
                    self.add_activity(" | ".join(parts), "poll")

                # Record P&L for chart
                total_equity = self.capital
                for pos in self.open_positions:
                    slug = pos["market"]
                    if slug in prices:
                        cp = prices[slug]
                        if pos["direction"] == "long":
                            unreal = (cp - pos["fill_price"]) * pos["shares"]
                        else:
                            unreal = (pos["fill_price"] - cp) * pos["shares"]
                        total_equity += pos["pos_value"] + unreal
                    else:
                        total_equity += pos["pos_value"]

                pnl = total_equity - INITIAL_CAPITAL
                self.pnl_history.append({
                    "time": now.strftime("%H:%M"),
                    "pnl": round(pnl, 2),
                    "equity": round(total_equity, 2),
                })
                if len(self.pnl_history) > 500:
                    self.pnl_history = self.pnl_history[-500:]

                # Save state
                self.save_state()

                # Broadcast to dashboard
                await self.broadcast_state(prices)

                # Print status line
                today_trades = sum(1 for t in self.trade_history
                                   if t["timestamp"][:10] == now.strftime("%Y-%m-%d"))
                today_pnl = sum(t["pnl"] for t in self.trade_history
                                if t["timestamp"][:10] == now.strftime("%Y-%m-%d"))

                log.info(
                    f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Open: {len(self.open_positions)}/{MAX_CONCURRENT} | "
                    f"Capital: ${self.capital:,.2f} | "
                    f"Equity: ${total_equity:,.2f} | "
                    f"Today P&L: ${today_pnl:+,.2f} | "
                    f"Trades today: {today_trades}"
                )

                # Hourly summary
                if self.poll_count % 30 == 0:
                    self.print_summary()

                # Wait for next cycle
                elapsed = time.time() - cycle_start
                wait = max(0, POLL_INTERVAL - elapsed)
                await asyncio.sleep(wait)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in trading loop: {e}")
                await asyncio.sleep(30)

        # Shutdown
        self.save_state()
        self.print_summary()
        log.info("Paper trader stopped. State saved.")

    def print_summary(self):
        """Print hourly/shutdown summary."""
        total_equity = self.capital + sum(p["pos_value"] for p in self.open_positions)
        pnl = total_equity - INITIAL_CAPITAL

        if self.trade_history:
            wins = sum(1 for t in self.trade_history if t["pnl"] > 0)
            win_rate = wins / len(self.trade_history) * 100
            avg_pnl = np.mean([t["pnl"] for t in self.trade_history])
        else:
            win_rate = 0
            avg_pnl = 0

        log.info("--- SUMMARY ---")
        log.info(f"  Equity: ${total_equity:,.2f} | P&L: ${pnl:+,.2f} ({pnl/INITIAL_CAPITAL*100:+.1f}%)")
        log.info(f"  Trades: {self.trade_count} | Win rate: {win_rate:.1f}% | Avg P&L: ${avg_pnl:+.2f}")
        log.info(f"  Open positions: {len(self.open_positions)}/{MAX_CONCURRENT}")
        log.info("---------------")

    # --- WebSocket ---

    async def broadcast_state(self, prices: dict[str, float]):
        """Send current state to all WebSocket clients."""
        state = self.get_full_state(prices)
        msg = json.dumps({"type": "update", "data": state})
        disconnected = []
        for ws in self.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.ws_clients.remove(ws)

    def get_full_state(self, prices: dict[str, float] = None) -> dict:
        """Build full state dict for dashboard."""
        if prices is None:
            prices = {}

        # Mark-to-market open positions
        positions_with_pnl = []
        for pos in self.open_positions:
            slug = pos["market"]
            cp = prices.get(slug, pos["fill_price"])
            if pos["direction"] == "long":
                unreal_pct = (cp - pos["fill_price"]) / max(pos["fill_price"], 1e-6) * 100
                unreal_dollar = (cp - pos["fill_price"]) * pos["shares"]
            else:
                unreal_pct = (pos["fill_price"] - cp) / max(pos["fill_price"], 1e-6) * 100
                unreal_dollar = (pos["fill_price"] - cp) * pos["shares"]

            positions_with_pnl.append({
                "market": slug[:40],
                "direction": pos["direction"],
                "entry_price": round(pos["entry_price"], 4),
                "current_price": round(cp, 4),
                "pnl_pct": round(unreal_pct, 2),
                "pnl_dollar": round(unreal_dollar, 2),
                "hold_time": int(time.time() - pos["entry_time"]),
                "stale": self.stale_counts.get(slug, 0) >= STALE_PRICE_THRESHOLD,
            })

        # Stats
        if self.trade_history:
            pnls = [t["pnl"] for t in self.trade_history]
            wins = sum(1 for p in pnls if p > 0)
            losses = len(pnls) - wins
            win_rate = wins / len(pnls) * 100
            avg_ret = np.mean(pnls)
            best = max(self.trade_history, key=lambda t: t["pnl"])
            worst = min(self.trade_history, key=lambda t: t["pnl"])
            avg_hold = np.mean([t["hold_time"] for t in self.trade_history])
        else:
            wins = losses = 0
            win_rate = avg_ret = avg_hold = 0
            best = worst = {"market": "N/A", "pnl": 0}

        total_equity = self.capital + sum(p["pos_value"] for p in self.open_positions)

        return {
            "scan_info": self.scan_info,
            "capital": round(self.capital, 2),
            "equity": round(total_equity, 2),
            "pnl": round(total_equity - INITIAL_CAPITAL, 2),
            "pnl_pct": round((total_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
            "open_count": len(self.open_positions),
            "max_positions": MAX_CONCURRENT,
            "trade_count": self.trade_count,
            "started_at": self.started_at,
            "positions": positions_with_pnl,
            "trades": self.trade_history[-50:],  # last 50
            "activity": self.activity_feed[-50:],
            "pnl_history": self.pnl_history,
            "stats": {
                "win_rate": round(win_rate, 1),
                "wins": wins,
                "losses": losses,
                "avg_return": round(avg_ret, 2),
                "best_trade": {"market": best["market"][:30], "pnl": round(best["pnl"], 2)},
                "worst_trade": {"market": worst["market"][:30], "pnl": round(worst["pnl"], 2)},
                "avg_hold_min": round(avg_hold / 60, 1) if avg_hold else 0,
            },
        }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

trader = PaperTrader()
app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = TEMPLATE_DIR / "dashboard.html"
    return HTMLResponse(html_path.read_text())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    trader.ws_clients.append(ws)
    # Send full state on connect
    try:
        state = trader.get_full_state()
        await ws.send_text(json.dumps({"type": "state", "data": state}))
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        if ws in trader.ws_clients:
            trader.ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=3000, log_level="warning")
    server = uvicorn.Server(config)

    loop = asyncio.get_event_loop()
    trading_task = asyncio.create_task(trader.run_loop())

    # Handle Ctrl+C
    def shutdown(sig, frame):
        log.info("\nShutting down...")
        trading_task.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Run both
    await asyncio.gather(
        server.serve(),
        trading_task,
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
