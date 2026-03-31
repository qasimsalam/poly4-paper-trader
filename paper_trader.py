#!/usr/bin/env python3
"""
PolyTrader Paper Trading System v3

Scaled for data collection: 6000 markets, 500 positions, $100K capital.
Adds trade categorization for analyzing which market types are profitable.

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

INITIAL_CAPITAL = 100_000.0
MAX_POSITION_SIZE = 500.0
MAX_CONCURRENT = 500
POLL_INTERVAL = 120
MARKET_FETCH_LIMIT = 6000
API_CONCURRENCY = 50

SPREAD_COST_PCT = 0.005
SLIPPAGE_PCT = 0.002

SPIKE_THRESHOLD = 0.155
SPIKE_WINDOW_CANDLES = 20
REVERSION_EXIT = 0.135
STOP_LOSS = 0.10
MAX_HOLD_SECONDS = 1200 * 60
COOLDOWN_CANDLES = 15
ADAPTIVE_MULT = 2.5

PRICE_BUFFER_SIZE = 60
STALE_PRICE_THRESHOLD = 10
STALE_FORCE_EXIT = 30

STATE_FILE = Path("state.json")
TRADE_LOG = Path("trades/trade_log.csv")
TRADE_CATEGORIES_FILE = Path("trades/trade_categories.csv")
TEMPLATE_DIR = Path("templates")
BUFFER_FILE = Path("data/price_buffer.json")

# No market type filters — matching backtest conditions exactly
# All categorization is tracked in trade_categories.csv for analysis

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("paper_trader")

LIVE_GAME_CATEGORIES = {"spread", "over-under", "player-prop", "game-matchup", "esports"}


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------

def categorize_market(slug: str) -> tuple[str, bool]:
    """Return (category, is_live_game) based on slug patterns."""
    s = slug.lower()
    if any(w in s for w in ['spread:', 'spread:-']):
        cat = "spread"
    elif any(w in s for w in ['o/u-', 'o/u:', '-o/u', 'match-o/u']):
        cat = "over-under"
    elif any(w in s for w in ['assists', 'rebounds', 'strikeouts', 'points-o/u']):
        cat = "player-prop"
    elif any(w in s for w in ['counter-strike', 'valorant', 'dota', 'league-of-legends']):
        cat = "esports"
    elif any(w in s for w in ['-vs-', '-vs.']) and any(w in s for w in [
        'fc', 'united', 'city', 'rovers', 'eagles', 'bears', 'heat', 'lakers',
        'celtics', 'warriors', 'suns', 'bulls', 'hawks', 'nets', 'knicks',
        'bucks', 'spurs', 'grizzlies', 'penguins', 'flames', 'avalanche',
        'pirates', 'rays', 'brewers', 'braves', 'athletics', 'reds',
        'islanders', 'rangers', 'bruins', 'lightning', 'panthers',
        'hornets', 'pistons', 'kings', 'cavaliers', 'jazz',
    ]):
        cat = "game-matchup"
    elif any(w in s for w in ['trump', 'democrat', 'republican', 'election', 'senate',
                               'congress', 'governor', 'party', 'vote', 'nominee']):
        cat = "politics"
    elif any(w in s for w in ['bitcoin', 'ethereum', 'solana', 'xrp', 'crypto', 'token',
                               'fdv', 'market-cap', 'usdt', 'airdrop']):
        cat = "crypto"
    elif any(w in s for w in ['iran', 'israel', 'war', 'military', 'strike', 'ceasefire',
                               'conflict', 'forces', 'ukraine', 'russia', 'houthi', 'nato']):
        cat = "geopolitics"
    elif any(w in s for w in ['s&p', 'nasdaq', 'dow', 'fed', 'interest-rate', 'bank-of',
                               'oil', 'gas', 'gold', 'aapl', 'pltr', 'close-above',
                               'close-at', 'close-below', 'finish-week']):
        cat = "finance"
    elif any(w in s for w in ['mrbeast', 'youtube', 'podcast', 'threadguy', 'appear-on',
                               'spotify', 'netflix', 'tiktok']):
        cat = "entertainment"
    elif any(w in s for w in ['win-the', 'champion', 'medal', 'score-the-most',
                               'lead-the-nba', 'lead-the-nhl', 'mvp', 'rookie']):
        cat = "sports-future"
    elif any(w in s for w in ['temperature', 'storm', 'hurricane']):
        cat = "weather"
    elif '-vs-' in s or '-vs.' in s:
        cat = "game-matchup"
    else:
        cat = "other"
    return cat, cat in LIVE_GAME_CATEGORIES


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
# Async market data
# ---------------------------------------------------------------------------

async def fetch_market_list_async() -> tuple[list[dict], dict[str, int]]:
    """Fetch markets with only volume filter — no market type filters."""
    markets = []
    filter_counts = {}  # no type filters, kept for API compatibility
    sem = asyncio.Semaphore(API_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for offset in range(0, MARKET_FETCH_LIMIT, 100):
            try:
                async with sem:
                    async with session.get(f"{GAMMA_API}/markets", params={
                        "limit": 100, "offset": offset, "order": "volume",
                        "ascending": "false", "closed": "false",
                        "volume_num_min": 10000,
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
                markets.append({
                    "slug": slug, "token_id": token_ids[0], "question": question,
                })
    return markets, filter_counts


async def fetch_prices_async(markets: list[dict]) -> dict[str, float]:
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
        await asyncio.gather(*[fetch_one(m) for m in markets], return_exceptions=True)
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
        self.load_buffer()
        self.load_trade_history()
        self._init_trade_categories()

    def _init_trade_categories(self):
        TRADE_CATEGORIES_FILE.parent.mkdir(exist_ok=True)
        if not TRADE_CATEGORIES_FILE.exists():
            with open(TRADE_CATEGORIES_FILE, "w", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp", "market", "direction", "entry_price", "exit_price",
                    "pnl", "exit_reason", "category", "is_live_game",
                ])

    # --- State ---

    def save_state(self):
        STATE_FILE.write_text(json.dumps({
            "open_positions": self.open_positions, "capital": self.capital,
            "trade_count": self.trade_count, "started_at": self.started_at,
            "last_poll": self.last_poll,
        }, indent=2))

    def load_state(self):
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                self.open_positions = s.get("open_positions", [])
                self.capital = s.get("capital", INITIAL_CAPITAL)
                self.trade_count = s.get("trade_count", 0)
                self.started_at = s.get("started_at", self.started_at)
                self.last_poll = s.get("last_poll")
                log.info(f"Restored state: capital=${self.capital:,.0f}, "
                         f"{len(self.open_positions)} open, {self.trade_count} trades")
            except Exception as e:
                log.warning(f"Failed to load state: {e}")

    def save_buffer(self):
        BUFFER_FILE.parent.mkdir(exist_ok=True)
        data = {slug: list(buf) for slug, buf in self.price_buffer.items()}
        BUFFER_FILE.write_text(json.dumps({"saved_at": time.time(), "buffers": data}))

    def load_buffer(self):
        if not BUFFER_FILE.exists():
            return
        try:
            raw = json.loads(BUFFER_FILE.read_text())
            age = time.time() - raw.get("saved_at", 0)
            if age > 600:
                log.info(f"Buffer too old ({age/60:.0f}m) — starting fresh")
                return
            for slug, entries in raw.get("buffers", {}).items():
                self.price_buffer[slug] = deque(
                    [(int(e[0]), float(e[1])) for e in entries], maxlen=PRICE_BUFFER_SIZE)
            mx = max((len(b) for b in self.price_buffer.values()), default=0)
            log.info(f"Restored buffer: {len(self.price_buffer)} markets, max {mx} candles, {age:.0f}s old")
        except Exception as e:
            log.warning(f"Failed to load buffer: {e}")

    def load_trade_history(self):
        if not TRADE_LOG.exists():
            return
        try:
            with open(TRADE_LOG) as f:
                for row in csv.DictReader(f):
                    if row.get("action") == "exit":
                        cat, is_lg = categorize_market(row.get("market", ""))
                        self.trade_history.append({
                            "market": row.get("market", ""), "direction": row.get("direction", ""),
                            "entry_price": 0, "exit_price": float(row.get("price", 0)),
                            "pnl": float(row.get("position_pnl", 0)), "pnl_pct": 0,
                            "hold_time": 0, "reason": "restored",
                            "timestamp": row.get("timestamp", ""),
                            "category": cat, "is_live_game": is_lg,
                        })
            if self.trade_history:
                log.info(f"Restored {len(self.trade_history)} trades from trade_log.csv")
        except Exception as e:
            log.warning(f"Failed to load trade history: {e}")

    # --- Logging ---

    def log_trade(self, entry: dict):
        TRADE_LOG.parent.mkdir(exist_ok=True)
        exists = TRADE_LOG.exists()
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp", "market", "action", "direction", "price",
                            "fill_price", "position_pnl", "capital", "open_positions_count"])
            w.writerow([entry["timestamp"], entry["market"], entry["action"],
                        entry["direction"], f"{entry['price']:.6f}", f"{entry['fill_price']:.6f}",
                        f"{entry.get('pnl', 0):.2f}", f"{self.capital:.2f}", len(self.open_positions)])

    def log_trade_category(self, rec: dict):
        with open(TRADE_CATEGORIES_FILE, "a", newline="") as f:
            csv.writer(f).writerow([
                rec.get("timestamp", ""), rec.get("market", ""), rec.get("direction", ""),
                f"{rec.get('entry_price', 0):.6f}", f"{rec.get('exit_price', 0):.6f}",
                f"{rec.get('pnl', 0):.2f}", rec.get("reason", ""),
                rec.get("category", "other"), rec.get("is_live_game", False),
            ])

    def add_activity(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity_feed.append({"time": ts, "msg": msg, "level": level})
        if len(self.activity_feed) > 200:
            self.activity_feed = self.activity_feed[-200:]

    # --- Exit helpers ---

    def _exit_position(self, pos: dict, cp: float, reason: str):
        slug = pos["market"]
        fill_price = apply_costs(cp, pos["direction"], is_entry=False)
        if pos["direction"] == "long":
            pnl_per_share = fill_price - pos["fill_price"]
        else:
            pnl_per_share = pos["fill_price"] - fill_price
        pnl_dollar = pnl_per_share * pos["shares"]
        self.capital += pos["pos_value"] + pnl_dollar
        self.trade_count += 1
        cat, is_lg = categorize_market(slug)

        rec = {
            "market": slug, "direction": pos["direction"],
            "entry_price": pos["entry_price"], "exit_price": cp,
            "fill_entry": pos["fill_price"], "fill_exit": fill_price,
            "pnl": pnl_dollar,
            "pnl_pct": pnl_dollar / pos["pos_value"] * 100 if pos["pos_value"] > 0 else 0,
            "hold_time": time.time() - pos["entry_time"], "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": cat, "is_live_game": is_lg,
        }
        self.trade_history.append(rec)
        self.log_trade({"timestamp": rec["timestamp"], "market": slug, "action": "exit",
                        "direction": pos["direction"], "price": cp, "fill_price": fill_price,
                        "pnl": pnl_dollar})
        self.log_trade_category(rec)
        color = "profit" if pnl_dollar > 0 else "loss"
        self.add_activity(
            f"EXIT: {slug[:35]} [{cat}] ({reason}) -- P&L: ${pnl_dollar:+.2f}", color)
        log.info(f"EXIT: {slug[:35]} [{cat}] ({reason}) -- P&L: ${pnl_dollar:+.2f}")
        self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
        self.stale_counts.pop(slug, None)
        self.open_positions.remove(pos)

    # --- Resolution + Stale ---

    def check_resolved_positions(self, prices: dict[str, float]):
        for pos in list(self.open_positions):
            slug = pos["market"]
            cp = prices.get(slug)
            if cp is None:
                continue
            if cp <= 0.02 or cp >= 0.98:
                self._exit_position(pos, cp, "RESOLVED")

    def check_stale_prices(self, prices: dict[str, float]):
        for pos in list(self.open_positions):
            slug = pos["market"]
            cp = prices.get(slug)
            if cp is None:
                continue
            if slug in self.last_prices and abs(cp - self.last_prices[slug]) < 1e-8:
                self.stale_counts[slug] = self.stale_counts.get(slug, 0) + 1
            else:
                self.stale_counts[slug] = 0
            self.last_prices[slug] = cp

            if self.stale_counts[slug] == STALE_PRICE_THRESHOLD:
                self.add_activity(f"WARNING: {slug[:40]} unchanged {STALE_PRICE_THRESHOLD}+ polls", "signal")
                log.warning(f"STALE: {slug[:40]} unchanged {STALE_PRICE_THRESHOLD}+ polls")

            if self.stale_counts.get(slug, 0) >= STALE_FORCE_EXIT:
                self._exit_position(pos, cp, "STALE")

    # --- Signal detection ---

    def check_entry_signals(self, prices: dict[str, float]):
        largest_spike = 0.0
        largest_spike_market = ""
        markets_scanned = 0
        adaptive_filtered_market = ""
        adaptive_filtered_thresh = 0.0

        buf_depths = [len(b) for b in self.price_buffer.values()] if self.price_buffer else [0]
        max_buf = max(buf_depths) if buf_depths else 0
        scanning = max_buf >= SPIKE_WINDOW_CANDLES + 1

        self.scan_info["buffer_candles"] = max_buf
        self.scan_info["scanning_active"] = scanning
        if not scanning:
            rem = SPIKE_WINDOW_CANDLES + 1 - max_buf
            self.scan_info["status_phase"] = "building"
            self.scan_info["status_text"] = f"Building buffer: {max_buf}/{SPIKE_WINDOW_CANDLES} ({rem*2}m left)"

        for slug, cp in prices.items():
            buf = self.price_buffer.get(slug)
            if not buf or len(buf) < SPIKE_WINDOW_CANDLES + 1:
                continue
            if cp < 0.10 or cp > 0.90:
                continue
            buf_prices = [p for _, p in buf]
            if float(np.median(buf_prices)) < 0.15 or float(np.median(buf_prices)) > 0.85:
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
            change = (cp - past_price) / past_price
            if abs(change) > abs(largest_spike):
                largest_spike = change
                largest_spike_market = slug

            recent = []
            for j in range(max(0, len(buf) - SPIKE_WINDOW_CANDLES - 1), len(buf) - 1):
                if j - SPIKE_WINDOW_CANDLES >= 0:
                    po = buf[j - SPIKE_WINDOW_CANDLES][1]
                    pn = buf[j][1]
                    if po > 1e-6:
                        recent.append(abs((pn - po) / po))
            adaptive = max(SPIKE_THRESHOLD, np.mean(recent) * ADAPTIVE_MULT) if recent else SPIKE_THRESHOLD

            if abs(change) >= SPIKE_THRESHOLD and abs(change) < adaptive:
                adaptive_filtered_market = slug
                adaptive_filtered_thresh = adaptive

            if abs(change) >= adaptive:
                direction = "short" if change > 0 else "long"
                cat, _ = categorize_market(slug)
                self.pending_entries.append({
                    "market": slug, "direction": direction,
                    "signal_price": cp, "signal_time": time.time(),
                    "change_pct": change * 100, "category": cat,
                })
                self.add_activity(
                    f"SIGNAL: {slug[:35]} [{cat}] spiked {change*100:.1f}% -- {direction}", "signal")

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
            self.scan_info["status_text"] = f"SIGNAL: {sig['market'][:35]} [{sig.get('category','')}] {sig['change_pct']:.1f}%"
        elif self.open_positions:
            self.scan_info["status_phase"] = "monitoring"
            self.scan_info["status_text"] = f"Monitoring {len(self.open_positions)} pos | scanning {markets_scanned}"
        elif scanning:
            self.scan_info["status_phase"] = "scanning"
            self.scan_info["status_text"] = f"Scanning {markets_scanned} | top: {abs(largest_spike)*100:.1f}% (need {SPIKE_THRESHOLD*100:.1f}%)"

    def check_exit_signals(self, prices: dict[str, float]):
        now = time.time()
        for pos in list(self.open_positions):
            slug = pos["market"]
            cp = prices.get(slug)
            if cp is None:
                continue
            ef = pos["fill_price"]
            pnl_pct = (cp - ef) / max(ef, 1e-6) if pos["direction"] == "long" else (ef - cp) / max(ef, 1e-6)

            # BUG FIX 2: dollar-based max loss cap — exit IMMEDIATELY (no next-candle delay)
            if pos["direction"] == "long":
                dollar_pnl = (cp - ef) * pos["shares"]
            else:
                dollar_pnl = (ef - cp) * pos["shares"]
            if dollar_pnl <= -pos["pos_value"]:  # loss exceeds position size ($500)
                self._exit_position(pos, cp, "MAX_LOSS_CAP")
                continue

            hold = now - pos["entry_time"]
            reason = None
            if pnl_pct >= REVERSION_EXIT:
                reason = "TP"
            elif pnl_pct <= -STOP_LOSS:
                reason = "SL"
            elif hold >= MAX_HOLD_SECONDS:
                reason = "timeout"
            if reason and not any(p["market"] == slug for p in self.pending_exits):
                self.pending_exits.append({"market": slug, "signal_price": cp, "reason": reason, "pnl_pct": pnl_pct})
                cat, _ = categorize_market(slug)
                self.add_activity(f"EXIT SIGNAL: {slug[:35]} [{cat}] ({reason}, {pnl_pct*100:.1f}%)", "signal")

    # --- Execution ---

    def execute_pending_entries(self, prices: dict[str, float]):
        for sig in list(self.pending_entries):
            slug = sig["market"]
            if slug not in prices:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- price unavailable", "signal")
                log.info(f"ENTRY SKIPPED: {slug[:35]} -- price unavailable")
                self.pending_entries.remove(sig); continue
            if len(self.open_positions) >= MAX_CONCURRENT:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- max positions", "signal")
                self.pending_entries.remove(sig); continue
            if self.capital < MAX_POSITION_SIZE * 0.5:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- low capital", "signal")
                self.pending_entries.remove(sig); continue

            raw = prices[slug]
            # BUG FIX 1: execution-time price check
            if raw < 0.10 or raw > 0.90:
                self.add_activity(
                    f"ENTRY SKIPPED: {slug[:35]} -- price outside range at execution (${raw:.3f})", "signal")
                log.info(f"ENTRY SKIPPED: {slug[:35]} -- price ${raw:.3f} outside 0.10-0.90 at execution")
                self.pending_entries.remove(sig); continue
            fill = apply_costs(raw, sig["direction"], is_entry=True)
            pv = min(MAX_POSITION_SIZE, self.capital * 0.5)
            self.capital -= pv
            cat, is_lg = categorize_market(slug)
            pos = {"market": slug, "direction": sig["direction"], "entry_time": time.time(),
                   "entry_price": raw, "fill_price": fill, "pos_value": pv,
                   "shares": pv / max(fill, 1e-6), "category": cat, "is_live_game": is_lg}
            self.open_positions.append(pos)
            self.log_trade({"timestamp": datetime.now(timezone.utc).isoformat(), "market": slug,
                           "action": "entry", "direction": sig["direction"], "price": raw,
                           "fill_price": fill, "pnl": 0})
            self.add_activity(
                f"ENTRY: {sig['direction'].upper()} {slug[:35]} [{cat}] @ ${raw:.4f}", "entry")
            self.pending_entries.remove(sig)

    def execute_pending_exits(self, prices: dict[str, float]):
        for sig in list(self.pending_exits):
            slug = sig["market"]
            pos = next((p for p in self.open_positions if p["market"] == slug), None)
            if not pos:
                self.pending_exits.remove(sig); continue
            cp = prices.get(slug, sig["signal_price"])
            self._exit_position(pos, cp, sig["reason"])
            self.pending_exits.remove(sig)

    # --- Main loop ---

    async def run_loop(self):
        log.info("=" * 60)
        log.info("PolyTrader v3 — Data Collection Mode")
        log.info(f"Capital: ${self.capital:,.0f} | Max pos: {MAX_CONCURRENT} | Markets: {MARKET_FETCH_LIMIT}")
        log.info(f"Costs: {(SPREAD_COST_PCT+SLIPPAGE_PCT)*200:.1f}% RT | Concurrency: {API_CONCURRENCY}")
        log.info(f"Dashboard: http://localhost:3000")
        log.info("=" * 60)

        log.info("Fetching market list...")
        self.markets, self.filter_counts = await fetch_market_list_async()
        log.info(f"Found {len(self.markets)} markets (vol>$10K, no type filters)")

        while True:
            try:
                t0 = time.time()
                self.poll_count += 1
                now = datetime.now(timezone.utc)

                if self.poll_count % 30 == 0:
                    self.markets, self.filter_counts = await fetch_market_list_async()

                prices = await fetch_prices_async(self.markets)
                np_ = len(prices)
                self.last_poll = now.isoformat()

                ts = int(now.timestamp())
                for slug, price in prices.items():
                    if slug not in self.price_buffer:
                        self.price_buffer[slug] = deque(maxlen=PRICE_BUFFER_SIZE)
                    self.price_buffer[slug].append((ts, price))

                self.check_resolved_positions(prices)
                self.execute_pending_entries(prices)
                self.execute_pending_exits(prices)
                self.check_exit_signals(prices)
                self.check_entry_signals(prices)
                self.check_stale_prices(prices)

                # Poll log
                si = self.scan_info
                if not si["scanning_active"]:
                    self.add_activity(f"Polled {np_} | Buffer: {si['buffer_candles']}/{SPIKE_WINDOW_CANDLES}", "poll")
                elif not self.pending_entries:
                    spike_str = f"Top: {si['largest_spike_pct']:.1f}% in {si['largest_spike_market'][:20]}" if si['largest_spike_pct'] > 0 else "No spikes"
                    self.add_activity(
                        f"Polled {np_} | Scanning: {si['markets_scanned']} eligible | "
                        f"{spike_str} (need {SPIKE_THRESHOLD*100:.1f}%)", "poll")

                # Equity
                equity = self.capital
                for pos in self.open_positions:
                    cp = prices.get(pos["market"])
                    if cp:
                        if pos["direction"] == "long":
                            equity += pos["pos_value"] + (cp - pos["fill_price"]) * pos["shares"]
                        else:
                            equity += pos["pos_value"] + (pos["fill_price"] - cp) * pos["shares"]
                    else:
                        equity += pos["pos_value"]

                self.pnl_history.append({"time": now.strftime("%H:%M"),
                                         "pnl": round(equity - INITIAL_CAPITAL, 2),
                                         "equity": round(equity, 2)})
                if len(self.pnl_history) > 500:
                    self.pnl_history = self.pnl_history[-500:]

                self.save_state()
                self.save_buffer()
                await self.broadcast_state(prices)

                # Live game stats
                lg_trades = [t for t in self.trade_history if t.get("is_live_game")]
                ng_trades = [t for t in self.trade_history if not t.get("is_live_game")]
                lg_pnl = sum(t["pnl"] for t in lg_trades)
                ng_pnl = sum(t["pnl"] for t in ng_trades)

                dt = sum(1 for t in self.trade_history if t.get("timestamp", "")[:10] == now.strftime("%Y-%m-%d"))
                dp = sum(t["pnl"] for t in self.trade_history if t.get("timestamp", "")[:10] == now.strftime("%Y-%m-%d"))
                pt = time.time() - t0

                log.info(f"[{now.strftime('%H:%M:%S')}] Open: {len(self.open_positions)}/{MAX_CONCURRENT} | "
                         f"Cap: ${self.capital:,.0f} | Eq: ${equity:,.0f} | "
                         f"Game: {len(lg_trades)}t/${lg_pnl:+,.0f} | Non: {len(ng_trades)}t/${ng_pnl:+,.0f} | "
                         f"Today: {dt}t/${dp:+,.0f} | Poll: {pt:.1f}s")

                if self.poll_count % 30 == 0:
                    self._print_summary()

                await asyncio.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error: {e}")
                import traceback; traceback.print_exc()
                await asyncio.sleep(30)

        self.save_state(); self.save_buffer(); self._print_summary()
        log.info("Stopped. State saved.")

    def _print_summary(self):
        eq = self.capital + sum(p["pos_value"] for p in self.open_positions)
        pnl = eq - INITIAL_CAPITAL
        if self.trade_history:
            w = sum(1 for t in self.trade_history if t["pnl"] > 0)
            wr = w / len(self.trade_history) * 100
        else:
            wr = 0
        log.info(f"--- SUMMARY: Eq=${eq:,.0f} P&L=${pnl:+,.0f} Trades={self.trade_count} WR={wr:.0f}% Open={len(self.open_positions)} ---")

    # --- WebSocket ---

    async def broadcast_state(self, prices: dict[str, float]):
        state = self.get_full_state(prices)
        msg = json.dumps({"type": "update", "data": state})
        dead = []
        for ws in self.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.remove(ws)

    def get_full_state(self, prices: dict[str, float] = None) -> dict:
        if prices is None:
            prices = {slug: buf[-1][1] for slug, buf in self.price_buffer.items() if buf}

        positions = []
        for pos in self.open_positions:
            slug = pos["market"]
            cp = prices.get(slug, pos["fill_price"])
            if pos["direction"] == "long":
                up = (cp - pos["fill_price"]) / max(pos["fill_price"], 1e-6) * 100
                ud = (cp - pos["fill_price"]) * pos["shares"]
            else:
                up = (pos["fill_price"] - cp) / max(pos["fill_price"], 1e-6) * 100
                ud = (pos["fill_price"] - cp) * pos["shares"]
            cat = pos.get("category") or categorize_market(slug)[0]
            is_lg = pos.get("is_live_game", categorize_market(slug)[1])
            positions.append({
                "market": slug[:40], "direction": pos["direction"],
                "entry_price": round(pos["entry_price"], 4), "current_price": round(cp, 4),
                "pnl_pct": round(up, 2), "pnl_dollar": round(ud, 2),
                "hold_time": int(time.time() - pos["entry_time"]),
                "stale": self.stale_counts.get(slug, 0) >= STALE_PRICE_THRESHOLD,
                "category": cat, "is_live_game": is_lg,
            })

        # Category stats
        cat_stats = {}
        for t in self.trade_history:
            c = t.get("category", "other")
            if c not in cat_stats:
                cat_stats[c] = {"trades": 0, "wins": 0, "pnl": 0, "last": ""}
            cat_stats[c]["trades"] += 1
            cat_stats[c]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                cat_stats[c]["wins"] += 1
            cat_stats[c]["last"] = t.get("timestamp", "")[:19]

        # Live game summary
        lg = [t for t in self.trade_history if t.get("is_live_game")]
        ng = [t for t in self.trade_history if not t.get("is_live_game")]

        if self.trade_history:
            pnls = [t["pnl"] for t in self.trade_history]
            wins = sum(1 for p in pnls if p > 0); losses = len(pnls) - wins
            best = max(self.trade_history, key=lambda t: t["pnl"])
            worst = min(self.trade_history, key=lambda t: t["pnl"])
            avg_hold = np.mean([t.get("hold_time", 0) for t in self.trade_history])
        else:
            wins = losses = 0; best = worst = {"market": "N/A", "pnl": 0}; avg_hold = 0

        eq = self.capital + sum(p["pos_value"] for p in self.open_positions)

        return {
            "scan_info": self.scan_info, "filter_counts": self.filter_counts,
            "capital": round(self.capital, 2), "equity": round(eq, 2),
            "pnl": round(eq - INITIAL_CAPITAL, 2),
            "pnl_pct": round((eq - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
            "open_count": len(self.open_positions), "max_positions": MAX_CONCURRENT,
            "trade_count": self.trade_count, "started_at": self.started_at,
            "positions": positions, "trades": self.trade_history[-50:],
            "activity": self.activity_feed[-50:], "pnl_history": self.pnl_history,
            "category_stats": cat_stats,
            "live_game": {"trades": len(lg), "pnl": round(sum(t["pnl"] for t in lg), 2)},
            "non_game": {"trades": len(ng), "pnl": round(sum(t["pnl"] for t in ng), 2)},
            "stats": {
                "win_rate": round(wins / max(len(self.trade_history), 1) * 100, 1),
                "wins": wins, "losses": losses,
                "avg_return": round(np.mean([t["pnl"] for t in self.trade_history]), 2) if self.trade_history else 0,
                "best_trade": {"market": best["market"][:30], "pnl": round(best["pnl"], 2)},
                "worst_trade": {"market": worst["market"][:30], "pnl": round(worst["pnl"], 2)},
                "avg_hold_min": round(avg_hold / 60, 1) if avg_hold else 0,
            },
        }


# ---------------------------------------------------------------------------
# FastAPI
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

async def main():
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=3000, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(trader.run_loop())
    def shutdown(sig, frame):
        log.info("\nShutting down...")
        task.cancel()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    await asyncio.gather(server.serve(), task, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
