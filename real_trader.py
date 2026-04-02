#!/usr/bin/env python3
"""
PolyTrader Real Money Trading System v2

Runs alongside paper_trader.py on port 3001.
Uses py-clob-client for actual CLOB order placement.
FOK (fill-or-kill) orders — zero resting exposure.

Usage: python real_trader.py
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
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
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions

# ---------------------------------------------------------------------------
# Load credentials from .env — NEVER print these
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
MODE = os.getenv("MODE", "dry-run")  # "dry-run" or "live"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RPC_URL = "https://polygon.drpc.org"
WALLET = "0x946845ecb6F1804ae588057Ca5320e5D7c7274Bc"

INITIAL_CAPITAL = 446.07  # true starting capital — shows total P&L from day 1
POSITION_SIZE = 5.0
MAX_CONCURRENT = 5
MAX_DAILY_LOSS = 20.0
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

MIN_VOLUME_24H = 500000
MIN_BASE_PRICE = 0.05      # skip spikes on sub-5-cent base prices
MAX_SPIKE_PCT = 5.0         # skip spikes > 500% (low-price artifacts)
MAX_FEE_RATE = 0.02         # 2% max fee rate
MAX_ORDERS_PER_MINUTE = 2

# FOK pricing offsets
FOK_ENTRY_OFFSET = 0.02     # buy at mid + $0.02
FOK_EXIT_OFFSETS = [-0.02, 0.00, 0.02]  # 3 attempts, progressively worse
FOK_MAX_ENTRY_OFFSET = 0.05  # never more than mid + $0.05

PORT = 3001

STATE_FILE = Path("real_state.json")
TRADE_LOG = Path("trades/real_trade_log.csv")
TRADE_CATEGORIES_FILE = Path("trades/real_trade_categories.csv")
TEMPLATE_DIR = Path("templates")
BUFFER_FILE = Path("data/real_price_buffer.json")

FILTERED_CATEGORIES = {"spread", "over-under", "game-matchup", "player-prop", "esports"}
LIVE_GAME_CATEGORIES = {"spread", "over-under", "player-prop", "game-matchup", "esports"}

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("real_trader")


# ---------------------------------------------------------------------------
# ClobClient singleton
# ---------------------------------------------------------------------------

def create_clob_client() -> ClobClient | None:
    if not PRIVATE_KEY:
        log.warning("No PRIVATE_KEY in .env — CLOB client disabled")
        return None
    try:
        client = ClobClient(CLOB_API, key=PRIVATE_KEY, chain_id=137)
        creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
        client.set_api_creds(creds)
        log.info(f"CLOB client initialized (MODE={MODE})")
        return client
    except Exception as e:
        log.error(f"Failed to create CLOB client: {e}")
        return None


# ---------------------------------------------------------------------------
# Categorization (identical to paper_trader.py)
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
# Cost model (identical to paper_trader.py)
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
    """Fetch markets — stores both YES and NO token IDs for order placement."""
    markets = []
    filter_counts: dict[str, int] = {}
    sem = asyncio.Semaphore(API_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for offset in range(0, MARKET_FETCH_LIMIT, 100):
            try:
                async with sem:
                    async with session.get(f"{GAMMA_API}/markets", params={
                        "limit": 100, "offset": offset, "order": "volume",
                        "ascending": "false", "closed": "false",
                        "volume_num_min": 100000,
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
                market_entry = {
                    "slug": slug,
                    "token_id": token_ids[0],  # YES token (used for price fetch)
                    "question": question,
                    "yes_token": token_ids[0],
                    "no_token": token_ids[1] if len(token_ids) > 1 else token_ids[0],
                    "condition_id": m.get("conditionId", ""),
                    "neg_risk": m.get("negRisk", False),
                    "volume24hr": float(m.get("volume24hr", 0) or 0),
                }
                markets.append(market_entry)
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
# Real Trader
# ---------------------------------------------------------------------------

class RealTrader:
    def __init__(self, clob_client: ClobClient | None):
        self.clob = clob_client
        self.mode = MODE
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
        self.token_map: dict[str, dict] = {}  # slug -> {yes_token, no_token, neg_risk}
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
        self.last_order_time: float = 0  # rate limiting

        # Safety
        self.kill_switch_active = False
        self.daily_realized_pnl = 0.0
        self.daily_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.daily_loss_limit_hit = False
        self.wallet_balance: float | None = None
        self.heartbeat_count = 0

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
                    "pnl", "exit_reason", "category", "is_live_game", "order_id", "mode",
                ])

    # --- State ---

    def save_state(self):
        STATE_FILE.write_text(json.dumps({
            "open_positions": self.open_positions, "capital": self.capital,
            "trade_count": self.trade_count, "started_at": self.started_at,
            "last_poll": self.last_poll, "mode": self.mode,
            "daily_pnl": self.daily_realized_pnl, "daily_date": self.daily_date,
            "kill_switch": self.kill_switch_active,
            "wallet_balance": self.wallet_balance,
            "pnl_history": self.pnl_history[-500:],
            "activity_feed": self.activity_feed[-200:],
            "cooldowns": dict(self.cooldowns),
            "stale_counts": dict(self.stale_counts),
            "last_prices": dict(self.last_prices),
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
                self.daily_realized_pnl = s.get("daily_pnl", 0.0)
                self.daily_date = s.get("daily_date", self.daily_date)
                self.kill_switch_active = s.get("kill_switch", False)
                self.wallet_balance = s.get("wallet_balance")
                self.pnl_history = s.get("pnl_history", [])
                self.activity_feed = s.get("activity_feed", [])
                self.cooldowns = s.get("cooldowns", {})
                self.stale_counts = s.get("stale_counts", {})
                self.last_prices = s.get("last_prices", {})
                log.info(f"Restored state: capital=${self.capital:,.2f}, "
                         f"{len(self.open_positions)} open, {self.trade_count} trades, "
                         f"{len(self.pnl_history)} pnl points, {len(self.activity_feed)} activities")
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
            if age > 3600:
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
        # Load from categories CSV first (has all trades with P&L), fall back to trade_log
        if TRADE_CATEGORIES_FILE.exists():
            try:
                with open(TRADE_CATEGORIES_FILE) as f:
                    for row in csv.DictReader(f):
                        cat = row.get("category", "other")
                        is_lg = row.get("is_live_game", "False").lower() == "true"
                        pnl = float(row.get("pnl", 0))
                        reason = row.get("exit_reason", "restored")
                        self.trade_history.append({
                            "market": row.get("market", ""),
                            "direction": row.get("direction", ""),
                            "entry_price": float(row.get("entry_price", 0)),
                            "exit_price": float(row.get("exit_price", 0)),
                            "pnl": pnl, "pnl_pct": 0,
                            "hold_time": 0, "reason": reason,
                            "timestamp": row.get("timestamp", ""),
                            "category": cat, "is_live_game": is_lg,
                        })
                if self.trade_history:
                    log.info(f"Restored {len(self.trade_history)} trades from categories CSV")
                return
            except Exception as e:
                log.warning(f"Failed to load categories: {e}")
        # Fallback to trade_log
        if TRADE_LOG.exists():
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
                    log.info(f"Restored {len(self.trade_history)} trades from trade_log")
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
                            "fill_price", "position_pnl", "capital", "open_positions_count",
                            "order_id", "mode"])
            w.writerow([entry["timestamp"], entry["market"], entry["action"],
                        entry["direction"], f"{entry['price']:.6f}", f"{entry['fill_price']:.6f}",
                        f"{entry.get('pnl', 0):.2f}", f"{self.capital:.2f}",
                        len(self.open_positions), entry.get("order_id", ""), self.mode])

    def log_trade_category(self, rec: dict):
        with open(TRADE_CATEGORIES_FILE, "a", newline="") as f:
            csv.writer(f).writerow([
                rec.get("timestamp", ""), rec.get("market", ""), rec.get("direction", ""),
                f"{rec.get('entry_price', 0):.6f}", f"{rec.get('exit_price', 0):.6f}",
                f"{rec.get('pnl', 0):.2f}", rec.get("reason", ""),
                rec.get("category", "other"), rec.get("is_live_game", False),
                rec.get("order_id", ""), self.mode,
            ])

    def add_activity(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity_feed.append({"time": ts, "msg": msg, "level": level})
        if len(self.activity_feed) > 200:
            self.activity_feed = self.activity_feed[-200:]

    # --- Safety checks ---

    def check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_date:
            log.info(f"Daily reset: {self.daily_date} -> {today} (was ${self.daily_realized_pnl:+.2f})")
            self.daily_realized_pnl = 0.0
            self.daily_date = today
            self.daily_loss_limit_hit = False

    def check_daily_loss(self):
        if self.daily_realized_pnl <= -MAX_DAILY_LOSS:
            if not self.daily_loss_limit_hit:
                self.daily_loss_limit_hit = True
                self.add_activity(
                    f"DAILY LOSS LIMIT HIT: ${self.daily_realized_pnl:.2f} (limit: -${MAX_DAILY_LOSS})", "loss")
                log.warning(f"DAILY LOSS LIMIT: ${self.daily_realized_pnl:.2f}")

    def can_enter(self) -> tuple[bool, str]:
        if self.kill_switch_active:
            return False, "kill switch active"
        if self.daily_loss_limit_hit:
            return False, f"daily loss limit (${self.daily_realized_pnl:.2f})"
        if len(self.open_positions) + len(self.pending_entries) >= MAX_CONCURRENT:
            return False, "max positions"
        if self.capital < POSITION_SIZE * 0.5:
            return False, "insufficient capital"
        if self.wallet_balance is not None and self.wallet_balance < POSITION_SIZE:
            return False, f"insufficient wallet balance (${self.wallet_balance:.2f})"
        return True, ""

    def check_rate_limit(self) -> bool:
        """Returns True if we can place an order (rate limit not exceeded)."""
        now = time.time()
        if now - self.last_order_time < 60.0 / MAX_ORDERS_PER_MINUTE:
            return False
        return True

    async def get_real_balance(self) -> float:
        """Check USDC.e balance on-chain. Returns balance or -1 on error."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{
                        "to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                        "data": "0x70a08231000000000000000000000000" + WALLET[2:].lower(),
                    }, "latest"]
                }
                async with session.post(RPC_URL, json=payload) as resp:
                    if resp.status == 200:
                        result = (await resp.json()).get("result", "0x0")
                        raw = int(result, 16)
                        return raw / 1e6
        except Exception as e:
            log.warning(f"Balance check failed: {e}")
        return -1

    async def check_wallet_balance(self):
        """Update cached wallet balance."""
        bal = await self.get_real_balance()
        if bal >= 0:
            self.wallet_balance = bal

    # --- CLOB order helpers (FOK) ---

    def _get_tick_size(self, token_id: str) -> str:
        """Get tick size from order book, fallback to API, then default."""
        try:
            book = self.clob.get_order_book(token_id)
            ts = getattr(book, 'min_tick_size', None) or getattr(book, 'tick_size', None)
            if ts:
                return str(ts)
        except Exception:
            pass
        try:
            return str(self.clob.get_tick_size(token_id))
        except Exception:
            return "0.01"

    async def place_entry_order(self, slug: str, direction: str, token_id: str,
                                neg_risk: bool, mid_price: float) -> dict | None:
        """Place FOK entry order. Returns fill info or None. Zero resting exposure."""
        if self.mode == "dry-run":
            fill_price = apply_costs(mid_price, direction, is_entry=True)
            size = POSITION_SIZE / max(fill_price, 0.01)
            self.add_activity(
                f"DRY-RUN ENTRY: {'BUY' if direction == 'long' else 'SELL'} "
                f"@ ${mid_price:.4f} x {size:.1f} shares", "entry")
            return {"fill_price": fill_price, "size": size, "order_id": "DRY-RUN"}

        elif self.mode == "live":
            # Rate limit check
            if not self.check_rate_limit():
                log.info(f"RATE LIMIT: skipping {slug[:35]}")
                return None

            # On-chain balance check
            real_bal = await self.get_real_balance()
            if real_bal >= 0 and real_bal < POSITION_SIZE:
                log.warning(f"INSUFFICIENT BALANCE: ${real_bal:.2f} < ${POSITION_SIZE}")
                self.add_activity(f"BLOCKED: insufficient balance ${real_bal:.2f}", "loss")
                return None

            try:
                # Get tick size
                tick_size = self._get_tick_size(token_id)

                # FOK price: mid + offset (aggressive enough to fill as taker)
                order_price = round(mid_price + FOK_ENTRY_OFFSET, 2)
                # Sanity: cap at mid + max offset
                if order_price > mid_price + FOK_MAX_ENTRY_OFFSET:
                    order_price = round(mid_price + FOK_MAX_ENTRY_OFFSET, 2)
                # Never buy above $0.90
                if order_price > 0.90:
                    log.warning(f"ENTRY SKIP: price too high ${order_price:.3f} for {slug[:35]}")
                    return None
                # Never buy below $0.05
                if order_price < 0.05:
                    log.warning(f"ENTRY SKIP: price too low ${order_price:.3f} for {slug[:35]}")
                    return None

                # Polymarket precision rules for BUY:
                #   maker_amount (price * size = USDC spent) <= 2 decimal places
                #   taker_amount (size = shares received) <= 4 decimal places
                # Find largest size where price*size is clean to 2 decimals
                raw_size = POSITION_SIZE / order_price
                size = None
                for dec in [4, 3, 2, 1, 0]:
                    candidate = math.floor(raw_size * 10**dec) / 10**dec
                    if candidate < 1:
                        continue
                    product = round(order_price * candidate, 8)
                    if abs(product - round(product, 2)) < 1e-9:
                        size = candidate
                        break
                if size is None:
                    size = max(1, int(raw_size))

                log.info(f"FOK ENTRY: {slug[:35]} BUY @ ${order_price:.3f} x {size:.0f} (mid=${mid_price:.3f})")

                order_args = OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=size,
                    side="BUY",
                )
                signed_order = self.clob.create_order(order_args, PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk))
                resp = self.clob.post_order(signed_order, OrderType.FOK)

                order_id = resp.get("orderID", resp.get("id", ""))
                self.last_order_time = time.time()

                if not order_id:
                    log.warning(f"FOK REJECTED (no order ID): {slug[:35]} resp={resp}")
                    self.add_activity(f"FOK REJECTED: {slug[:30]}", "loss")
                    return None

                # FOK is instant — brief pause then check
                await asyncio.sleep(1.5)
                try:
                    order_status = self.clob.get_order(order_id)
                    status = order_status.get("status", "")
                    size_matched = float(order_status.get("size_matched", 0))

                    if size_matched > 0:
                        fill_price = float(order_status.get("price", order_price))
                        # If somehow partially filled (shouldn't happen with FOK), cancel remainder
                        if status in ("LIVE", "OPEN"):
                            try:
                                self.clob.cancel(order_id)
                                log.warning(f"CANCELLED REMAINDER (unexpected partial): {slug[:35]}")
                            except Exception:
                                pass
                        log.info(f"FOK FILLED: {slug[:35]} @ ${fill_price:.4f} x {size_matched:.0f}")
                        self.add_activity(f"FOK FILLED: {slug[:30]} @ ${fill_price:.4f}", "entry")
                        return {"fill_price": fill_price, "size": size_matched, "order_id": order_id}
                    else:
                        log.info(f"FOK NOT FILLED: {slug[:35]} (no liquidity at ${order_price:.3f})")
                        self.add_activity(f"FOK NO FILL: {slug[:30]} @ ${order_price:.3f}", "signal")
                        return None
                except Exception as e:
                    log.warning(f"FOK status check failed: {slug[:35]} — {e}")
                    return None

            except Exception as e:
                log.error(f"FOK ENTRY ERROR: {slug[:35]} — {e}")
                self.add_activity(f"FOK ERROR: {slug[:30]} — {e}", "loss")
                return None
        return None

    async def place_exit_order(self, slug: str, direction: str, token_id: str,
                               pos: dict, current_price: float) -> dict | None:
        """Place GTC exit order at mid price. Returns order info or None.
        Does NOT wait for fill — order rests on book. check_pending_exits_gtc() monitors it."""
        if self.mode == "dry-run":
            fill_price = current_price
            self.add_activity(
                f"DRY-RUN EXIT: SELL @ ${fill_price:.4f}", "info")
            return {"fill_price": fill_price, "order_id": "DRY-RUN", "filled": True}

        elif self.mode == "live":
            neg_risk = pos.get("neg_risk", False)
            # Polymarket SELL precision: maker_amount (size) <= 2 decimals
            raw_shares = pos.get("shares", POSITION_SIZE / max(current_price, 0.01))
            shares = math.floor(raw_shares * 100) / 100  # floor to 2 dec
            if shares < 1:
                shares = 1.0

            sell_price = round(current_price, 2)
            if sell_price < 0.01:
                sell_price = 0.01

            try:
                tick_size = self._get_tick_size(token_id)
                log.info(f"GTC EXIT: {slug[:35]} SELL @ ${sell_price:.3f} x {shares:.0f}")

                order_args = OrderArgs(
                    token_id=token_id,
                    price=sell_price,
                    size=shares,
                    side="SELL",
                )
                signed = self.clob.create_order(order_args, PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk))
                resp = self.clob.post_order(signed, OrderType.GTC)
                order_id = resp.get("orderID", resp.get("id", ""))
                self.last_order_time = time.time()

                if not order_id:
                    log.warning(f"GTC EXIT REJECTED: no order ID for {slug[:35]}")
                    return None

                log.info(f"GTC EXIT PLACED: {slug[:35]} order_id={order_id[:20]}...")
                self.add_activity(f"GTC EXIT PLACED: {slug[:30]} SELL @ ${sell_price:.3f}", "signal")
                return {"order_id": order_id, "sell_price": sell_price, "filled": False}

            except Exception as e:
                log.error(f"GTC EXIT ERROR: {slug[:35]} — {e}")
                self.add_activity(f"GTC EXIT ERROR: {slug[:30]} — {e}", "loss")
                return None
        return None

    async def check_pending_exits_gtc(self, prices: dict[str, float]):
        """Check status of resting GTC exit orders. Called every poll cycle."""
        for pos in list(self.open_positions):
            exit_oid = pos.get("exit_order_id")
            if not exit_oid:
                continue
            slug = pos["market"]
            try:
                status = self.clob.get_order(exit_oid)
                st = status.get("status", "")
                sm = float(status.get("size_matched", 0))

                if sm > 0 or st.upper() in ("MATCHED", "FILLED"):
                    # Exit filled — record P&L and close position
                    fill_price = float(status.get("price", 0))
                    if fill_price <= 0:
                        fill_price = float(status.get("associate_trades", [{}])[0].get("price", pos.get("exit_sell_price", 0)))
                    if fill_price <= 0:
                        fill_price = prices.get(slug, pos.get("exit_sell_price", 0))
                    log.info(f"GTC EXIT FILLED: {slug[:35]} @ ${fill_price:.4f}")
                    self.add_activity(f"EXIT FILLED (GTC): {slug[:30]} @ ${fill_price:.4f}", "profit")

                    # Use _exit_position for accounting
                    self._exit_position(pos, fill_price, pos.get("exit_reason", "SL"))

                    # Sync capital
                    real_bal = await self.get_real_balance()
                    if real_bal >= 0:
                        self.capital = real_bal
                        self.wallet_balance = real_bal
                    self.save_state()

                elif time.time() - pos.get("exit_placed_at", 0) > 7200:
                    # Stale exit — cancel and re-place at current mid
                    try:
                        self.clob.cancel(exit_oid)
                        log.info(f"GTC EXIT CANCELLED (stale): {slug[:35]}")
                    except Exception as ce:
                        log.warning(f"GTC EXIT CANCEL ERROR: {slug[:35]} — {ce}")
                    pos.pop("exit_order_id", None)
                    pos.pop("exit_placed_at", None)
                    pos.pop("exit_sell_price", None)
                    self.add_activity(f"EXIT REFRESH: {slug[:30]} — cancelled stale, will re-place", "signal")
                    self.save_state()

            except Exception as e:
                log.warning(f"GTC EXIT CHECK ERROR: {slug[:35]} — {e}")

    # --- Exit accounting (sync — no CLOB calls) ---

    def _exit_position(self, pos: dict, cp: float, reason: str):
        """Record exit P&L and remove position. No CLOB calls here."""
        slug = pos["market"]
        fill_price = apply_costs(cp, pos["direction"], is_entry=False)
        if pos["direction"] == "long":
            pnl_per_share = fill_price - pos["fill_price"]
        else:
            pnl_per_share = pos["fill_price"] - fill_price
        pnl_dollar = pnl_per_share * pos["shares"]
        self.capital += pos["pos_value"] + pnl_dollar
        self.trade_count += 1
        self.daily_realized_pnl += pnl_dollar
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
            "order_id": pos.get("order_id", ""),
        }
        self.trade_history.append(rec)
        self.log_trade({"timestamp": rec["timestamp"], "market": slug, "action": "exit",
                        "direction": pos["direction"], "price": cp, "fill_price": fill_price,
                        "pnl": pnl_dollar, "order_id": pos.get("order_id", "")})
        self.log_trade_category(rec)
        color = "profit" if pnl_dollar > 0 else "loss"
        self.add_activity(
            f"EXIT: {slug[:35]} [{cat}] ({reason}) -- P&L: ${pnl_dollar:+.2f}", color)
        log.info(f"EXIT [{self.mode}]: {slug[:35]} [{cat}] ({reason}) -- P&L: ${pnl_dollar:+.2f}")
        self.cooldowns[slug] = self.poll_count + COOLDOWN_CANDLES
        self.stale_counts.pop(slug, None)
        self.open_positions.remove(pos)
        self.save_state()  # save immediately after position removal
        self.check_daily_loss()

    # --- Resolution + Stale (queue to pending_exits) ---

    def check_resolved_positions(self, prices: dict[str, float]):
        for pos in list(self.open_positions):
            slug = pos["market"]
            cp = prices.get(slug)
            if cp is None:
                continue
            if cp <= 0.02 or cp >= 0.98:
                if not any(p["market"] == slug for p in self.pending_exits):
                    self.pending_exits.append({
                        "market": slug, "signal_price": cp,
                        "reason": "RESOLVED", "pnl_pct": 0,
                        "skip_clob": True,  # resolved markets — just do accounting
                    })

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

            if self.stale_counts.get(slug, 0) >= STALE_FORCE_EXIT:
                if not any(p["market"] == slug for p in self.pending_exits):
                    self.pending_exits.append({
                        "market": slug, "signal_price": cp,
                        "reason": "STALE", "pnl_pct": 0,
                    })

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

            # Safety checks
            can, reason = self.can_enter()
            if not can:
                continue

            past_idx = len(buf) - 1 - SPIKE_WINDOW_CANDLES
            if past_idx < 0:
                continue
            past_price = buf[past_idx][1]
            if past_price < 1e-6:
                continue
            # Skip low base prices — percentage moves are unreliable
            if past_price < MIN_BASE_PRICE:
                continue
            change = (cp - past_price) / past_price
            # Skip extreme spikes — likely data artifacts on low-price markets
            if abs(change) > MAX_SPIKE_PCT:
                continue

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
                cat, is_lg = categorize_market(slug)

                # Category filter — skip live game markets
                if cat in FILTERED_CATEGORIES:
                    self.add_activity(
                        f"FILTERED: {slug[:35]} [{cat}] — live game category", "signal")
                    continue

                # Volume filter — skip illiquid markets before generating signal
                market_info = self.token_map.get(slug, {})
                vol_24h = market_info.get("volume24hr", 0)
                if vol_24h < MIN_VOLUME_24H:
                    self.add_activity(
                        f"FILTERED: {slug[:35]} — low volume ${vol_24h:,.0f}", "signal")
                    continue

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
            # Skip if GTC exit already resting on book
            if pos.get("exit_order_id"):
                continue
            cp = prices.get(slug)
            if cp is None:
                continue
            ef = pos["fill_price"]
            pnl_pct = (cp - ef) / max(ef, 1e-6) if pos["direction"] == "long" else (ef - cp) / max(ef, 1e-6)

            # Dollar-based max loss cap
            if pos["direction"] == "long":
                dollar_pnl = (cp - ef) * pos["shares"]
            else:
                dollar_pnl = (ef - cp) * pos["shares"]
            if dollar_pnl <= -pos["pos_value"]:
                if not any(p["market"] == slug for p in self.pending_exits):
                    self.pending_exits.append({"market": slug, "signal_price": cp, "reason": "MAX_LOSS_CAP", "pnl_pct": pnl_pct})
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

    # --- Execution (async for CLOB calls) ---

    async def execute_pending_entries(self, prices: dict[str, float]):
        """Execute pending entries — FOK orders in live mode."""
        for sig in list(self.pending_entries):
            slug = sig["market"]
            if slug not in prices:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- price unavailable", "signal")
                self.pending_entries.remove(sig)
                continue

            can, reason = self.can_enter()
            if not can:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- {reason}", "signal")
                self.pending_entries.remove(sig)
                continue

            raw = prices[slug]
            if raw < 0.10 or raw > 0.90:
                self.add_activity(
                    f"ENTRY SKIPPED: {slug[:35]} -- price outside range (${raw:.3f})", "signal")
                self.pending_entries.remove(sig)
                continue

            # Volume filter — skip illiquid markets
            market_info = self.token_map.get(slug, {})
            vol_24h = market_info.get("volume24hr", 0)
            if vol_24h < MIN_VOLUME_24H:
                self.add_activity(
                    f"ENTRY SKIP: low 24h volume ${vol_24h:,.0f} < ${MIN_VOLUME_24H:,.0f} for {slug[:30]}", "signal")
                log.info(f"ENTRY SKIP: low volume ${vol_24h:,.0f} for {slug[:35]}")
                self.pending_entries.remove(sig)
                continue

            # Get token info for this market
            if sig["direction"] == "long":
                token_id = market_info.get("yes_token", "")
            else:
                token_id = market_info.get("no_token", "")

            if not token_id:
                self.add_activity(f"ENTRY SKIPPED: {slug[:35]} -- no token ID", "signal")
                self.pending_entries.remove(sig)
                continue

            neg_risk = market_info.get("neg_risk", False)

            # Place order (FOK in live, simulated in dry-run)
            result = await self.place_entry_order(slug, sig["direction"], token_id, neg_risk, mid_price=raw)

            if result is None:
                # FOK not filled — skip, no harm done
                self.pending_entries.remove(sig)
                continue

            # Build position from fill result
            fill_price = result["fill_price"]
            actual_size = result.get("size", POSITION_SIZE / max(fill_price, 0.01))
            order_id = result.get("order_id", "")
            pv = fill_price * actual_size  # actual cost
            cat, is_lg = categorize_market(slug)

            pos = {
                "market": slug, "direction": sig["direction"], "entry_time": time.time(),
                "entry_price": raw, "fill_price": fill_price, "pos_value": pv,
                "shares": actual_size, "category": cat, "is_live_game": is_lg,
                "order_id": order_id, "token_id": token_id,
                "neg_risk": neg_risk,
            }
            self.open_positions.append(pos)

            # Sync capital with on-chain balance after live trades
            if self.mode == "live":
                real_bal = await self.get_real_balance()
                if real_bal >= 0:
                    self.capital = real_bal
                    self.wallet_balance = real_bal
                    log.info(f"CAPITAL SYNCED: ${real_bal:.2f} (on-chain)")
            else:
                self.capital -= pv

            self.save_state()  # save immediately so position is not lost

            self.log_trade({"timestamp": datetime.now(timezone.utc).isoformat(), "market": slug,
                           "action": "entry", "direction": sig["direction"], "price": raw,
                           "fill_price": fill_price, "pnl": 0, "order_id": order_id})
            self.add_activity(
                f"ENTRY [{self.mode}]: {sig['direction'].upper()} {slug[:30]} [{cat}] @ ${fill_price:.4f}", "entry")
            log.info(f"ENTRY [{self.mode}]: {sig['direction'].upper()} {slug[:35]} [{cat}] @ ${fill_price:.4f}")
            self.pending_entries.remove(sig)

    async def execute_pending_exits(self, prices: dict[str, float]):
        """Execute pending exits — GTC orders in live mode (rests on book)."""
        for sig in list(self.pending_exits):
            slug = sig["market"]
            pos = next((p for p in self.open_positions if p["market"] == slug), None)
            if not pos:
                self.pending_exits.remove(sig)
                continue

            # Skip if GTC exit already placed for this position
            if pos.get("exit_order_id"):
                self.pending_exits.remove(sig)
                continue

            cp = prices.get(slug, sig["signal_price"])

            # In live mode, place GTC exit order (unless skip_clob flag set)
            if self.mode == "live" and pos.get("token_id") and not sig.get("skip_clob"):
                try:
                    result = await self.place_exit_order(
                        slug, pos["direction"], pos["token_id"], pos, cp)
                    if result is not None:
                        if result.get("filled"):
                            # Dry-run mode immediate fill
                            cp = result["fill_price"]
                            log.info(f"EXIT ORDER FILLED [live]: {slug[:35]} @ ${cp:.4f}")
                        else:
                            # GTC order placed — store on position, monitor via check_pending_exits_gtc
                            pos["exit_order_id"] = result["order_id"]
                            pos["exit_placed_at"] = time.time()
                            pos["exit_sell_price"] = result["sell_price"]
                            pos["exit_reason"] = sig["reason"]
                            log.info(f"GTC EXIT RESTING [live]: {slug[:35]} @ ${result['sell_price']:.3f}")
                            self.save_state()
                            self.pending_exits.remove(sig)
                            continue  # don't do accounting yet — wait for fill
                    else:
                        # Place failed — will retry next cycle
                        log.error(f"EXIT PLACE FAILED [live]: {slug[:35]} — will retry")
                        self.add_activity(f"EXIT RETRY: {slug[:30]} — GTC place failed", "loss")
                        self.pending_exits.remove(sig)
                        continue
                except Exception as e:
                    log.error(f"EXIT ORDER ERROR [live]: {slug[:35]} — {e}")
                    self.add_activity(f"EXIT ERROR: {slug[:30]} — {e}", "loss")
                    self.pending_exits.remove(sig)
                    continue

            # Accounting (position removal, P&L recording) — for dry-run/skip_clob
            self._exit_position(pos, cp, sig["reason"])

            # Sync capital after live exit
            if self.mode == "live":
                real_bal = await self.get_real_balance()
                if real_bal >= 0:
                    self.capital = real_bal
                    self.wallet_balance = real_bal

            self.pending_exits.remove(sig)

    # --- Main loop ---

    async def run_loop(self):
        log.info("=" * 60)
        log.info(f"PolyTrader REAL v2 — MODE={self.mode} — FOK ORDERS")
        log.info(f"Capital: ${self.capital:,.2f} | Max pos: {MAX_CONCURRENT} | Pos size: ${POSITION_SIZE}")
        log.info(f"Daily loss limit: ${MAX_DAILY_LOSS} | Kill switch: {self.kill_switch_active}")
        log.info(f"FOK entry offset: +${FOK_ENTRY_OFFSET} | Exit offsets: {FOK_EXIT_OFFSETS}")
        log.info(f"Base price filter: ${MIN_BASE_PRICE} | Max spike: {MAX_SPIKE_PCT*100:.0f}%")
        log.info(f"Max fee rate: {MAX_FEE_RATE*100:.0f}% | Rate limit: {MAX_ORDERS_PER_MINUTE}/min")
        log.info(f"Category filter: {FILTERED_CATEGORIES}")
        log.info(f"Dashboard: http://localhost:{PORT}")
        log.info("=" * 60)

        log.info("Fetching market list...")
        self.markets, self.filter_counts = await fetch_market_list_async()
        # Build token map
        for m in self.markets:
            self.token_map[m["slug"]] = {
                "yes_token": m.get("yes_token", m["token_id"]),
                "no_token": m.get("no_token", m["token_id"]),
                "neg_risk": m.get("neg_risk", False),
                "volume24hr": m.get("volume24hr", 0),
            }
        log.info(f"Found {len(self.markets)} markets, {len(self.token_map)} token mappings")

        # Initial balance check
        await self.check_wallet_balance()
        if self.wallet_balance is not None:
            log.info(f"Wallet USDC balance: ${self.wallet_balance:,.2f}")

        while True:
            try:
                t0 = time.time()
                self.poll_count += 1
                now = datetime.now(timezone.utc)

                # Daily reset
                self.check_daily_reset()

                # Refresh markets every 30 polls
                if self.poll_count % 30 == 0:
                    self.markets, self.filter_counts = await fetch_market_list_async()
                    for m in self.markets:
                        self.token_map[m["slug"]] = {
                            "yes_token": m.get("yes_token", m["token_id"]),
                            "no_token": m.get("no_token", m["token_id"]),
                            "neg_risk": m.get("neg_risk", False),
                            "volume24hr": m.get("volume24hr", 0),
                        }

                # Balance check every 15 polls (30 min)
                if self.poll_count % 15 == 0:
                    await self.check_wallet_balance()

                prices = await fetch_prices_async(self.markets)
                np_ = len(prices)
                self.last_poll = now.isoformat()

                ts = int(now.timestamp())
                for slug, price in prices.items():
                    if slug not in self.price_buffer:
                        self.price_buffer[slug] = deque(maxlen=PRICE_BUFFER_SIZE)
                    self.price_buffer[slug].append((ts, price))

                self.check_resolved_positions(prices)
                await self.check_pending_exits_gtc(prices)
                await self.execute_pending_entries(prices)
                await self.execute_pending_exits(prices)
                self.check_exit_signals(prices)
                self.check_entry_signals(prices)
                self.check_stale_prices(prices)

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

                # Heartbeat
                self.heartbeat_count += 1
                if self.heartbeat_count % 3 == 0:  # every ~6 min
                    bal_str = f"${self.wallet_balance:.2f}" if self.wallet_balance is not None else "unknown"
                    log.info(f"HEARTBEAT: ALIVE | MODE={self.mode} | positions={len(self.open_positions)} | "
                             f"daily_pnl=${self.daily_realized_pnl:+.2f} | wallet={bal_str}")

                pt = time.time() - t0
                log.info(f"[{now.strftime('%H:%M:%S')}] [{self.mode}] Open: {len(self.open_positions)}/{MAX_CONCURRENT} | "
                         f"Cap: ${self.capital:,.2f} | Eq: ${equity:,.2f} | "
                         f"Today: ${self.daily_realized_pnl:+.2f}/{-MAX_DAILY_LOSS} | "
                         f"Trades: {self.trade_count} | Poll: {pt:.1f}s")

                await asyncio.sleep(max(0, POLL_INTERVAL - (time.time() - t0)))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error: {e}")
                import traceback; traceback.print_exc()
                await asyncio.sleep(30)

        self.save_state()
        self.save_buffer()
        log.info("Stopped. State saved.")

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

    def get_full_state(self, prices: dict[str, float] | None = None) -> dict:
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

        if self.trade_history:
            pnls = [t["pnl"] for t in self.trade_history]
            wins = sum(1 for p in pnls if p > 0)
            losses = len(pnls) - wins
            best = max(self.trade_history, key=lambda t: t["pnl"])
            worst = min(self.trade_history, key=lambda t: t["pnl"])
            avg_hold = np.mean([t.get("hold_time", 0) for t in self.trade_history])
        else:
            wins = losses = 0
            best = worst = {"market": "N/A", "pnl": 0}
            avg_hold = 0

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
            "mode": self.mode,
            "daily_pnl": round(self.daily_realized_pnl, 2),
            "daily_loss_remaining": round(MAX_DAILY_LOSS + self.daily_realized_pnl, 2),
            "daily_loss_limit": MAX_DAILY_LOSS,
            "wallet_balance": self.wallet_balance,
            "kill_switch": self.kill_switch_active,
            "position_size": POSITION_SIZE,
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

clob_client = create_clob_client()
trader = RealTrader(clob_client)
app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse((TEMPLATE_DIR / "real_dashboard.html").read_text())


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


@app.post("/kill")
async def kill_switch():
    trader.kill_switch_active = True
    trader.save_state()
    log.warning("KILL SWITCH ACTIVATED — no new entries")
    return JSONResponse({"status": "killed", "msg": "No new entries will be placed"})


@app.post("/resume")
async def resume():
    trader.kill_switch_active = False
    trader.save_state()
    log.info("KILL SWITCH DEACTIVATED — entries resumed")
    return JSONResponse({"status": "resumed", "msg": "Entries re-enabled"})


async def main():
    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning")
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
