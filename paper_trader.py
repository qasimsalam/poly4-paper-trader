#!/usr/bin/env python3
"""
PolyTrader Paper Trading System v2

Single process: runs trading loop (every 2 min) + web dashboard (port 3000).
No real money — logs hypothetical trades with realistic costs.

Features:
  - Parallel API calls via aiohttp (50 concurrent max)
  - Resolution detection (exit at $0/$1)
  - Temperature/weather + sports short-term + crypto binary filters
  - Buffer persistence to disk (instant resume on restart)
  - Stale price detection + entry skip logging

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

import aiohttp
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

INITIAL_CAPITAL = 10_000.0
MAX_POSITION_SIZE = 500.0
MAX_CONCURRENT = 20
POLL_INTERVAL = 120           # seconds (2 minutes)
MARKET_FETCH_LIMIT = 600
API_CONCURRENCY = 50          # max parallel API requests

# Costs
SPREAD_COST_PCT = 0.005
SLIPPAGE_PCT = 0.002

# Strategy params
SPIKE_THRESHOLD = 0.155
SPIKE_WINDOW_CANDLES = 20
REVERSION_EXIT = 0.135
STOP_LOSS = 0.10
MAX_HOLD_SECONDS = 1200 * 60
COOLDOWN_CANDLES = 15
ADAPTIVE_MULT = 2.5

PRICE_BUFFER_SIZE = 60
STALE_PRICE_THRESHOLD = 10

STATE_FILE = Path("state.json")
TRADE_LOG = Path("trades/trade_log.csv")
TEMPLATE_DIR = Path("templates")
BUFFER_FILE = Path("data/price_buffer.json")

# Filter patterns
BINARY_PATTERNS = re.compile(r'up.or.down|up-or-down', re.IGNORECASE)
TEMPERATURE_PATTERNS = re.compile(r'temperature|highest.temperature|lowest.temperature', re.IGNORECASE)
SPORTS_SHORT_PATTERNS = re.compile(r'win-on-|end-in-a-draw|exact-score', re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("paper_trader")


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

def apply_costs(price: float, direction: str, is_entry: bool) -> float:
    cost = SPREAD_COST_PCT + SLIPPAGE_PCT
    if is_entry:
        return price * (1 + cost) if direction == "long" else price * (1 - cost)
    else:
        return price * (1 - cost) if direction == "long" else price * (1 + cost)


# ---------------------------------------------------------------------------
# Async market data (CHANGE 1: parallel API)
# ---------------------------------------------------------------------------

async def fetch_market_list_async() -> tuple[list[dict], dict[str, int]]:
    """Fetch markets from Gamma API with filtering. Returns (markets, filter_counts)."""
    markets = []
    filter_counts = {"crypto_binary": 0, "temperature": 0, "sports_short": 0}
    sem = asyncio.Semaphore(API_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for offset in range(0, MARKET_FETCH_LIMIT, 100):
            try:
                async with sem:
                    async with session.get(f"{GAMMA_API}/markets", params={
                        "limit": 100, "offset": offset, "order": "volume",
                        "ascending": "false", "closed": "false",
                    }) as resp:
                        if resp.status != 200:
                            break
                        batch = await resp.json()
            except Exception as e:
                log.warning(f"Market fetch error at offset {offset}: {e}")
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
                if not token_ids or not token_ids[0]:
                    continue

                slug = (m.get("question", "unknown"))[:60].lower().replace(" ", "-")
                question = m.get("question", "")

                # CHANGE 3: temperature filter
                if TEMPERATURE_PATTERNS.search(slug) or TEMPERATURE_PATTERNS.search(question):
                    filter_counts["temperature"] += 1
                    continue

                # CHANGE 4: sports short-term filter
                if SPORTS_SHORT_PATTERNS.search(slug) or SPORTS_SHORT_PATTERNS.search(question):
                    filter_counts["sports_short"] += 1
                    continue

                # CHANGE 5: crypto binary filter
                if BINARY_PATTERNS.search(slug) or BINARY_PATTERNS.search(question):
                    filter_counts["crypto_binary"] += 1
                    continue

                markets.append({
                    "slug": slug,
                    "token_id": token_ids[0],
                    "question": question,
                })

    return markets, filter_counts


async def fetch_prices_async(markets: list[dict]) -> dict[str, float]:
    """Fetch prices for all markets in parallel with semaphore."""
    prices = {}
    sem = asyncio.Semaphore(API_CONCURRENCY)

    async def fetch_one(m):
        try:
            async with sem:
                async with session.get(f"{CLOB_API}/price", params={
                    "token_id": m["token_id"], "side": "buy",
                }) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data.get("price", 0))
                        if 0 < price < 1:
                            prices[m["slug"]] = price
        except Exception:
            pass

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        tasks = [fetch_one(m) for m in markets]
        await asyncio.gather(*tasks, return_exceptions=True)

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

        self.price_buffer: dict[str, deque] = {}
        self.cooldowns: dict[str, int] = {}
        self.poll_count = 0

        self.pending_entries: list[dict] = []
        self.pending_exits: list[dict] = []

        self.markets: list[dict] = []
        self.filter_counts: dict[str, int] = {}

        self.ws_clients: list[WebSocket] = []
        self.activity_feed: list[dict] = []
        self.pnl_history: list[dict] = []

        self.scan_info: dict = {
            "buffer_candles": 0, "buffer_needed": SPIKE_WINDOW_CANDLES,
            "scanning_active": False, "markets_scanned": 0,
            "largest_spike_pct": 0.0, "largest_spike_market": "",
            "adaptive_filtered": "", "adaptive_thresh_used": 0.0,
            "status_phase": "building", "status_text": "",
        }

        self.stale_counts: dict[str, int] = {}
        self.last_prices: dict[str, float] = {}

        self.load_state()
        self.load_buffer()  # CHANGE 7

    # --- State persistence ---

    def save_state(self):
        STATE_FILE.write_text(json.dumps({
            "open_positions": self.open_positions,
            "capital": self.capital,
            "trade_count": self.trade_count,
            "started_at": self.started_at,
            "last_poll": self.last_poll,
        }, indent=2))

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
                         f"{len(self.open_positions)} open, {self.trade_count} trades")
            except Exception as e:
                log.warning(f"Failed to load state: {e}")

    # --- CHANGE 7: Buffer persistence ---

    def save_buffer(self):
        """Save price buffer to disk for instant resume."""
        BUFFER_FILE.parent.mkdir(exist_ok=True)
        data = {}
        for slug, buf in self.price_buffer.items():
            data[slug] = list(buf)  # deque -> list of [ts, price]
        BUFFER_FILE.write_text(json.dumps({
            "saved_at": time.time(),
            "buffers": data,
        }))

    def load_buffer(self):
        """Load price buffer if recent (<10 min old)."""
        if not BUFFER_FILE.exists():
            return
        try:
            raw = json.loads(BUFFER_FILE.read_text())
            saved_at = raw.get("saved_at", 0)
            age = time.time() - saved_at
            if age > 600:  # older than 10 minutes
                log.info(f"Buffer file too old ({age/60:.0f}m) — starting fresh")
                return
            buffers = raw.get("buffers", {})
            for slug, entries in buffers.items():
                self.price_buffer[slug] = deque(
                    [(int(e[0]), float(e[1])) for e in entries],
                    maxlen=PRICE_BUFFER_SIZE
                )
            max_depth = max((len(b) for b in self.price_buffer.values()), default=0)
            log.info(f"Restored buffer: {len(self.price_buffer)} markets, "
                     f"max depth {max_depth} candles, {age:.0f}s old")
        except Exception as e:
            log.warning(f"Failed to load buffer: {e}")

    # --- Trade logging ---

    def log_trade(self, entry: dict):
        TRADE_LOG.parent.mkdir(exist_ok=True)
        file_exists = TRADE_LOG.exists()
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "market", "action", "direction",
                                 "price", "fill_price", "position_pnl", "capital",
                                 "open_positions_count"])
            writer.writerow([
                entry["timestamp"], entry["market"], entry["action"],
                entry["direction"], f"{entry['price']:.6f}",
                f"{entry['fill_price']:.6f}", f"{entry.get('pnl', 0):.2f}",
                f"{self.capital:.2f}", len(self.open_positions),
            ])

    def add_activity(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity_feed.append({"time": ts, "msg": msg, "level": level})
        if len(self.activity_feed) > 200:
            self.activity_feed = self.activity_feed[-200:]

    # --- CHANGE 2: Resolution detection ---

    def check_resolved_positions(self, prices: dict[str, float]):
        for pos in list(self.open_positions):
            slug = pos["market"]
            if slug not in prices:
                continue
            cp = prices[slug]
            if cp > 0.02 and cp < 0.98:
                continue
            # Resolved
            fill_price = apply_costs(cp, pos["direction"], is_entry=False)
            if pos["direction"] == "long":
                pnl_per_share = fill_price - pos["fill_price"]
            else:
                pnl_per_share = pos["fill_price"] - fill_price
            pnl_dollar = pnl_per_share * pos["shares"]
            self.capital += pos["pos_value"] + pnl_dollar
            self.trade_count += 1

            self.trade_history.append({
                "market": slug, "direction": pos["direction"],
                "entry_price": pos["entry_price"], "exit_price": cp,
                "fill_entry": pos["fill_price"], "fill_exit": fill_price,
                "pnl": pnl_dollar,
                "pnl_pct": pnl_dollar / pos["pos_value"] * 100 if pos["pos_value"] > 0 else 0,
                "hold_time": time.time() - pos["entry_time"],
                "reason": "RESOLVED",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self.log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": slug, "action": "exit", "direction": pos["direction"],
                "price": cp, "fill_price": fill_price, "pnl": pnl_dollar,
            })
            color = "profit" if pnl_dollar > 0 else "loss"
            self.add_activity(
                f"EXIT: {slug[:40]} (RESOLVED to ${cp:.2f}) -- P&L: ${pnl_dollar:+.2f}", color)
            log.info(f"EXIT: {slug[:40]} (RESOLVED to ${cp:.2f}) -- P&L: ${pnl_dollar:+.2f}")
            self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
            self.open_positions.remove(pos)

    # --- CHANGE 9: Stale price detection ---

    def check_stale_prices(self, prices: dict[str, float]):
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
                msg = f"WARNING: {slug[:40]} price unchanged for {STALE_PRICE_THRESHOLD}+ polls"
                self.add_activity(msg, "signal")
                log.warning(msg)

    # --- Signal detection ---

    def check_entry_signals(self, prices: dict[str, float]):
        largest_spike = 0.0
        largest_spike_market = ""
        markets_scanned = 0
        adaptive_filtered_market = ""
        adaptive_filtered_thresh = 0.0

        buffer_depths = [len(b) for b in self.price_buffer.values()] if self.price_buffer else [0]
        max_buf = max(buffer_depths) if buffer_depths else 0
        scanning_active = max_buf >= SPIKE_WINDOW_CANDLES + 1

        self.scan_info["buffer_candles"] = max_buf
        self.scan_info["scanning_active"] = scanning_active

        if not scanning_active:
            remaining = SPIKE_WINDOW_CANDLES + 1 - max_buf
            self.scan_info["status_phase"] = "building"
            self.scan_info["status_text"] = (
                f"Building price buffer: {max_buf}/{SPIKE_WINDOW_CANDLES} candles "
                f"({remaining * 2} minutes remaining)")

        for slug, current_price in prices.items():
            if slug not in self.price_buffer:
                continue
            buf = self.price_buffer[slug]
            if len(buf) < SPIKE_WINDOW_CANDLES + 1:
                continue
            if current_price < 0.10 or current_price > 0.90:
                continue
            buf_prices = [p for _, p in buf]
            med_price = float(np.median(buf_prices))
            if med_price < 0.15 or med_price > 0.85:
                continue

            markets_scanned += 1

            if slug in self.cooldowns and self.poll_count < self.cooldowns[slug]:
                continue
            if any(p["market"] == slug for p in self.open_positions):
                continue
            if any(p["market"] == slug for p in self.pending_entries):
                continue
            if len(self.open_positions) + len(self.pending_entries) >= MAX_CONCURRENT:
                continue
            if self.capital < MAX_POSITION_SIZE * 0.5:
                continue

            past_idx = len(buf) - 1 - SPIKE_WINDOW_CANDLES
            if past_idx < 0:
                continue
            past_price = buf[past_idx][1]
            if past_price < 1e-6:
                continue

            change = (current_price - past_price) / past_price
            if abs(change) > abs(largest_spike):
                largest_spike = change
                largest_spike_market = slug

            recent_changes = []
            for j in range(max(0, len(buf) - SPIKE_WINDOW_CANDLES - 1), len(buf) - 1):
                if j - SPIKE_WINDOW_CANDLES >= 0:
                    p_old = buf[j - SPIKE_WINDOW_CANDLES][1]
                    p_new = buf[j][1]
                    if p_old > 1e-6:
                        recent_changes.append(abs((p_new - p_old) / p_old))

            adaptive_thresh = max(SPIKE_THRESHOLD,
                                  np.mean(recent_changes) * ADAPTIVE_MULT) if recent_changes else SPIKE_THRESHOLD

            if abs(change) >= SPIKE_THRESHOLD and abs(change) < adaptive_thresh:
                adaptive_filtered_market = slug
                adaptive_filtered_thresh = adaptive_thresh

            if abs(change) >= adaptive_thresh:
                direction = "short" if change > 0 else "long"
                self.pending_entries.append({
                    "market": slug, "direction": direction,
                    "signal_price": current_price, "signal_time": time.time(),
                    "change_pct": change * 100,
                })
                self.add_activity(
                    f"SIGNAL: {slug[:40]} spiked {change*100:.1f}% -- flagging {direction} entry",
                    "signal")

        self.scan_info.update({
            "markets_scanned": markets_scanned,
            "largest_spike_pct": round(abs(largest_spike) * 100, 1),
            "largest_spike_market": largest_spike_market[:40],
            "adaptive_filtered": adaptive_filtered_market[:40],
            "adaptive_thresh_used": round(adaptive_filtered_thresh * 100, 1),
        })

        if self.pending_entries:
            sig = self.pending_entries[-1]
            self.scan_info["status_phase"] = "signal"
            self.scan_info["status_text"] = (
                f"SIGNAL: {sig['market'][:40]} spiked {sig['change_pct']:.1f}%")
        elif self.open_positions:
            self.scan_info["status_phase"] = "monitoring"
            self.scan_info["status_text"] = (
                f"Monitoring {len(self.open_positions)} position(s) | scanning {markets_scanned}")
        elif scanning_active:
            self.scan_info["status_phase"] = "scanning"
            self.scan_info["status_text"] = (
                f"Scanning {markets_scanned} markets | top spike: "
                f"{abs(largest_spike)*100:.1f}% (need {SPIKE_THRESHOLD*100:.1f}%)")

    def check_exit_signals(self, prices: dict[str, float]):
        now = time.time()
        for pos in list(self.open_positions):
            slug = pos["market"]
            if slug not in prices:
                continue
            cp = prices[slug]
            entry_fill = pos["fill_price"]
            if pos["direction"] == "long":
                pnl_pct = (cp - entry_fill) / max(entry_fill, 1e-6)
            else:
                pnl_pct = (entry_fill - cp) / max(entry_fill, 1e-6)

            hold_time = now - pos["entry_time"]
            reason = None
            if pnl_pct >= REVERSION_EXIT:
                reason = "TP"
            elif pnl_pct <= -STOP_LOSS:
                reason = "SL"
            elif hold_time >= MAX_HOLD_SECONDS:
                reason = "timeout"

            if reason and not any(p["market"] == slug for p in self.pending_exits):
                self.pending_exits.append({
                    "market": slug, "signal_price": cp,
                    "reason": reason, "pnl_pct": pnl_pct,
                })
                self.add_activity(
                    f"EXIT SIGNAL: {slug[:40]} ({reason}, PnL {pnl_pct*100:.1f}%)", "signal")

    # --- Execution ---

    def execute_pending_entries(self, prices: dict[str, float]):
        for sig in list(self.pending_entries):
            slug = sig["market"]
            # CHANGE 8: log all skips
            if slug not in prices:
                self.add_activity(f"ENTRY SKIPPED: {slug[:40]} -- price unavailable", "signal")
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- price unavailable")
                self.pending_entries.remove(sig)
                continue
            if len(self.open_positions) >= MAX_CONCURRENT:
                self.add_activity(f"ENTRY SKIPPED: {slug[:40]} -- max positions", "signal")
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- max positions")
                self.pending_entries.remove(sig)
                continue
            if self.capital < MAX_POSITION_SIZE * 0.5:
                self.add_activity(f"ENTRY SKIPPED: {slug[:40]} -- low capital", "signal")
                log.info(f"ENTRY SKIPPED: {slug[:40]} -- low capital")
                self.pending_entries.remove(sig)
                continue

            raw_price = prices[slug]
            fill_price = apply_costs(raw_price, sig["direction"], is_entry=True)
            pos_value = min(MAX_POSITION_SIZE, self.capital * 0.5)
            self.capital -= pos_value

            pos = {
                "market": slug, "direction": sig["direction"],
                "entry_time": time.time(), "entry_price": raw_price,
                "fill_price": fill_price, "pos_value": pos_value,
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
                f"(fill: ${fill_price:.4f})", "entry")
            self.pending_entries.remove(sig)

    def execute_pending_exits(self, prices: dict[str, float]):
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

            self.trade_history.append({
                "market": slug, "direction": pos["direction"],
                "entry_price": pos["entry_price"], "exit_price": raw_price,
                "fill_entry": pos["fill_price"], "fill_exit": fill_price,
                "pnl": pnl_dollar,
                "pnl_pct": pnl_dollar / pos["pos_value"] * 100 if pos["pos_value"] > 0 else 0,
                "hold_time": time.time() - pos["entry_time"],
                "reason": sig["reason"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self.log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market": slug, "action": "exit", "direction": pos["direction"],
                "price": raw_price, "fill_price": fill_price, "pnl": pnl_dollar,
            })
            color = "profit" if pnl_dollar > 0 else "loss"
            self.add_activity(
                f"EXIT: {slug[:40]} ({sig['reason']}) -- P&L: ${pnl_dollar:+.2f}", color)
            self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
            self.open_positions.remove(pos)
            self.pending_exits.remove(sig)

    # --- Main loop ---

    async def run_loop(self):
        log.info("=" * 60)
        log.info("PolyTrader Paper Trading System v2")
        log.info(f"Capital: ${self.capital:,.2f} | Max positions: {MAX_CONCURRENT}")
        log.info(f"Costs: {(SPREAD_COST_PCT+SLIPPAGE_PCT)*200:.1f}% RT | Concurrency: {API_CONCURRENCY}")
        log.info(f"Filters: temperature, sports-short, crypto-binary")
        log.info(f"Dashboard: http://localhost:3000")
        log.info("=" * 60)

        # Fetch market list
        log.info("Fetching market list...")
        self.markets, self.filter_counts = await fetch_market_list_async()
        fc = self.filter_counts
        log.info(f"Found {len(self.markets)} markets | Filtered: "
                 f"{fc.get('temperature',0)} temp, {fc.get('sports_short',0)} sports, "
                 f"{fc.get('crypto_binary',0)} crypto-binary")

        while True:
            try:
                cycle_start = time.time()
                self.poll_count += 1
                now = datetime.now(timezone.utc)

                # Refresh market list hourly
                if self.poll_count % 30 == 0:
                    self.markets, self.filter_counts = await fetch_market_list_async()

                # Poll prices (parallel)
                prices = await fetch_prices_async(self.markets)
                n_prices = len(prices)
                self.last_poll = now.isoformat()

                # Update buffer
                ts = int(now.timestamp())
                for slug, price in prices.items():
                    if slug not in self.price_buffer:
                        self.price_buffer[slug] = deque(maxlen=PRICE_BUFFER_SIZE)
                    self.price_buffer[slug].append((ts, price))

                # Resolution detection
                self.check_resolved_positions(prices)

                # Execute pending (next-candle)
                self.execute_pending_entries(prices)
                self.execute_pending_exits(prices)

                # New signals
                self.check_exit_signals(prices)
                self.check_entry_signals(prices)

                # Stale detection
                self.check_stale_prices(prices)

                # CHANGE 10: filter summary in activity feed
                si = self.scan_info
                fc = self.filter_counts
                if not si["scanning_active"]:
                    self.add_activity(
                        f"Polled {n_prices} mkts | Buffer: {si['buffer_candles']}/{SPIKE_WINDOW_CANDLES}",
                        "poll")
                elif not self.pending_entries:
                    filter_str = (f"Filtered: {fc.get('temperature',0)} temp, "
                                  f"{fc.get('sports_short',0)} sports, "
                                  f"{fc.get('crypto_binary',0)} crypto")
                    spike_str = (f"Top spike: {si['largest_spike_pct']:.1f}% in "
                                 f"{si['largest_spike_market'][:20]}"
                                 if si['largest_spike_pct'] > 0 else "No spikes")
                    self.add_activity(
                        f"Polled {n_prices} | {filter_str} | "
                        f"Scanning: {si['markets_scanned']} eligible | "
                        f"{spike_str} (need {SPIKE_THRESHOLD*100:.1f}%)",
                        "poll")
                    if si["adaptive_filtered"]:
                        self.add_activity(
                            f"Adaptive filtered: {si['adaptive_filtered'][:30]} "
                            f"(thresh {si['adaptive_thresh_used']:.0f}%)", "info")

                # P&L for chart
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

                self.pnl_history.append({
                    "time": now.strftime("%H:%M"),
                    "pnl": round(total_equity - INITIAL_CAPITAL, 2),
                    "equity": round(total_equity, 2),
                })
                if len(self.pnl_history) > 500:
                    self.pnl_history = self.pnl_history[-500:]

                # Save state + buffer
                self.save_state()
                self.save_buffer()

                # Broadcast
                await self.broadcast_state(prices)

                # Status line
                today_trades = sum(1 for t in self.trade_history
                                   if t["timestamp"][:10] == now.strftime("%Y-%m-%d"))
                today_pnl = sum(t["pnl"] for t in self.trade_history
                                if t["timestamp"][:10] == now.strftime("%Y-%m-%d"))

                poll_time = time.time() - cycle_start
                log.info(
                    f"[{now.strftime('%H:%M:%S')}] "
                    f"Open: {len(self.open_positions)}/{MAX_CONCURRENT} | "
                    f"Capital: ${self.capital:,.0f} | "
                    f"Equity: ${total_equity:,.0f} | "
                    f"P&L: ${today_pnl:+,.0f} | "
                    f"Trades: {today_trades} | "
                    f"Poll: {poll_time:.1f}s")

                if self.poll_count % 30 == 0:
                    self.print_summary()

                elapsed = time.time() - cycle_start
                await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in trading loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(30)

        self.save_state()
        self.save_buffer()
        self.print_summary()
        log.info("Paper trader stopped. State saved.")

    def print_summary(self):
        total_equity = self.capital + sum(p["pos_value"] for p in self.open_positions)
        pnl = total_equity - INITIAL_CAPITAL
        if self.trade_history:
            wins = sum(1 for t in self.trade_history if t["pnl"] > 0)
            win_rate = wins / len(self.trade_history) * 100
            avg_pnl = np.mean([t["pnl"] for t in self.trade_history])
        else:
            win_rate = avg_pnl = 0
        log.info("--- SUMMARY ---")
        log.info(f"  Equity: ${total_equity:,.2f} | P&L: ${pnl:+,.2f}")
        log.info(f"  Trades: {self.trade_count} | Win: {win_rate:.0f}% | Avg: ${avg_pnl:+.2f}")
        log.info(f"  Open: {len(self.open_positions)}/{MAX_CONCURRENT}")
        log.info("---------------")

    # --- WebSocket ---

    async def broadcast_state(self, prices: dict[str, float]):
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
        if prices is None:
            prices = {}

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
                "market": slug[:40], "direction": pos["direction"],
                "entry_price": round(pos["entry_price"], 4),
                "current_price": round(cp, 4),
                "pnl_pct": round(unreal_pct, 2),
                "pnl_dollar": round(unreal_dollar, 2),
                "hold_time": int(time.time() - pos["entry_time"]),
                "stale": self.stale_counts.get(slug, 0) >= STALE_PRICE_THRESHOLD,
            })

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
            "trades": self.trade_history[-50:],
            "activity": self.activity_feed[-50:],
            "pnl_history": self.pnl_history,
            "stats": {
                "win_rate": round(win_rate, 1), "wins": wins, "losses": losses,
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
    return HTMLResponse((TEMPLATE_DIR / "dashboard.html").read_text())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    trader.ws_clients.append(ws)
    try:
        await ws.send_text(json.dumps({"type": "state", "data": trader.get_full_state()}))
        while True:
            await ws.receive_text()
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
    trading_task = asyncio.create_task(trader.run_loop())

    def shutdown(sig, frame):
        log.info("\nShutting down...")
        trading_task.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    await asyncio.gather(server.serve(), trading_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
