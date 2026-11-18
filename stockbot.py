#!/usr/bin/env python3
"""Screen Stocks trading agent + dashboard.

Uses only the game's official mod interface:
  reads  mods/export/market.json   (prices, player, scheduled pump/crash news, command results)
  reads  mods/export/history.json  (1-second price samples -> recorded forever in data/prices.db)
  reads  mods/export/plugin.json   (optional: state written by the companion BepInEx plugin)
  writes mods/commands/<stock>/<action>.json   ({"execute": true, "percent": N})
  writes mods/plugin-commands/<ms>-<action>.json   (requests for the companion plugin; it buys upgrades
                                                    and claims rewards itself, the bot only passes settings on)
The game must be running and online (it can be hidden); it executes the trades.  Nobody trades by hand
through this agent: the dashboard can arm/disarm and configure it, nothing else.

Strategies, in priority order:
  1. Scheduled events: pump = buy before, sell into the spike, short the fade, cover near the start.
                       crash = sell + short ahead, cover near the target, buy the bottom, sell the recovery.
  2. Swing trading: per-stock strategies picked by walk-forward backtests (brain.py).
  3. Dividend parking: idle cash goes into a calm, high-dividend stock.
Costs: every fill is measured (entry price from the change in average price and shares, exit proceeds from the
change in cash) and feeds a per-stock price-impact model (data/impact_model.json) that sizes entries and splits
large exits into chunks.
Safety: client-side rate limit, per-side/per-stock backoff after failures, median-based stops with a 5 s
persistence check and a 10 s grace after each fill, position caps, and a drawdown circuit breaker.
Standard library only.  Run:  python stockbot.py [--data DIR] [--arm]  (or run_hidden.vbs)  ->  http://127.0.0.1:8641
"""
import argparse
import collections
import ctypes
import inspect
import json
import logging
import logging.handlers
import math
import os
import re
import shlex
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.dont_write_bytecode = True   # the code folder's drive may be full: never write __pycache__ there
import brain as brain_mod  # noqa: E402
from brain import Brain, decide, describe  # noqa: E402,F401  (describe kept for older imports)
import resets  # noqa: E402  (daily reset events)
import upgrades  # noqa: E402  (smart auto-upgrades)
import autoipo  # noqa: E402  (click Go Public when allowed)
BT_SPREAD = tuple(getattr(brain_mod, "SPREAD", (0.009, 0.019)))   # the brain's open/close spread split
BT_FRAC = getattr(brain_mod, "BT_FRAC", 0.5)                      # share of cash its backtests assume per entry
BT_MOVE = getattr(brain_mod, "DEFAULT_MOVE", 0.01)                # its impact when none is passed

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DEFAULT_MODS = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData/LocalLow/Conradical Games/Screen Stocks/mods"
GAME_APPID = "4356780"
GAME_TITLE = "Screen Stocks"
GAME_CLASS = "UnityWndClass"     # Sandboxie renames it, e.g. "Sandbox:ScreenStocks2:UnityWndClass"
LAUNCH = f"steam://rungameid/{GAME_APPID}"
PORT = 8641
log = logging.getLogger("stockbot")

DEFAULTS = {
    "armed": False,              # master switch; the bot only trades while armed
    "armOnStart": False,         # arm automatically when the bot starts and the feed is live
    "autoLaunchGame": False,     # start the game through Steam when the bot starts
    "autoHideGame": False,       # keep the game window hidden
    # scheduled events
    "events": True,              # off = no new event legs (positions already open are still closed properly)
    "eventPercent": 100,         # % of cash per event leg
    "eventFreeCash": True,       # sell the bot's other longs to fund event legs
    "pumpMinGain": 5.0,          # % to target needed to trade a pump
    "pumpCapture": 50.0,         # sell after this % of the start->target move (recorded pumps peak on the first tick
                                 # after T at 58-85 % of the move, then fall 10-45 % a tick: sell that tick)
    "pumpTrail": 10.0,           # after the event time: sell on this % pullback from the peak
    "pumpExitAfterSec": 30,      # sell this long after the event time regardless
    "pumpFadeShort": True,       # short after selling the spike
    "fadeCoverAbove": 20.0,      # cover the fade when the price is within this % of the start price
    "fadeMaxSec": 120,
    "crashMinDrop": 5.0,
    "crashLeadMin": 15,          # act this many minutes before a crash (fractions allowed)
    "crashShort": True,
    "crashCoverWithin": 5.0,     # cover when within this % above the target
    "crashReboundBuy": True,     # buy the bottom after the crash
    "reboundRecover": 90.0,      # sell after recovering this % of the way back to the start price
    "reboundStop": 15.0,         # % below the fill (10 s median, must persist 5 s) that sells a rebound buy (0 = no stop)
    "reboundMaxHoldMin": 45,
    "reboundMaxWaitMin": 10,
    # daily resets (resets.py): stocks dropped to their base price at a fixed UTC time every day
    "resets": True,              # trade them, also when the game never exported the announcement
    "resetSchedule": "$PLAIN 19:00 10; $IDXA 23:00 10; $IDXB 15:00 20",   # "stock HH:MM(UTC) target; ..."
    "resetDetect": True,         # also trade crashes nobody announced, like a reset that just happened:
    "resetDetectFrac": 0.3,      # ... the price falls under this x its median of the 10 min before the last minute
    "resetDetectStocks": "$PLAIN",   # ... on these stocks ("" = all)
    "resetDetectGapMin": 10,     # ... at most once per stock in this many minutes
    "resetDetectKeepBuy": True,  # ... keep the buy cooldown free for them: no swing longs while watching
    "resetDetectKeepPark": False, # ... and no new parks either
    "resetDetectMaxFrac": 0.45,    # ... buy only at or under this x the price before the crash (0 = any)
    "resetFastKeepBuy": False,   # keep the buy slot free for the resetDetectBy stocks' next dip: other buys only
    "resetFastOpenSec": 170,     # ... in this many seconds after one of their dips
    "resetDetectBy": {},         # per-stock crash settings over the three above, and watched too, e.g.
                                 # {"$WHALE": {"frac": 0.55, "maxFrac": 0.55, "gapMin": 3, "badFill": 0.7}}; parks skip them
    "resetShort": True,          # short ahead of the reset, cover on the reset tick
    "resetLeadSec": 25,          # ... this many seconds before the reset time
    "resetShortRiskPct": 30.0,   # ... sized so a cover at the price cap would cost at most this % of cash
    "resetShortMinPct": 20.0,    # ... and only if that size is at least this % (a small short isn't worth the late buy)
    "resetCoverX": 3.0,          # cover at or under this x the target (or 1/4 of the pre-reset price)
    "resetNoResetSec": 8,        # no drop this long after the reset time: cover (no reset today)
    "resetBuyMaxX": 3.0,         # buy once the price is at or under this x the target ...
    "resetBuyWindowSec": 300,    # ... within this long after the reset time
    "resetTakeX": 8.0,           # first resetBurstSec s: sell into the spike at this x the fill (0 = don't) ...
    "resetTakeStartX": 1.5,      # ... and at least this x the pre-reset price
    "resetBurstSec": 45,
    "resetRecover": 85.0,        # then: sell after recovering this % of the way back to the pre-reset price
    "resetHoldMin": 240,         # ... or after this many minutes ($IDXB takes about 2 h to come back)
    "resetRebuy": True,          # one more buy after selling the spike, when the buy cooldown allows, while ...
    "resetRebuyFrac": 0.35,      # ... the price is under this share of the pre-reset price ...
    "resetRebuyMin": 10,         # ... within this many minutes of the reset
    # swing trading
    "swing": True,
    "swingPercent": 50,          # % of cash per swing entry when the brain gives no size (it normally does)
    "swingMaxNwPct": 25,         # one swing entry uses at most this % of net worth (2026-11-15 13:27: a $PUMP short
                                 # sized at 94 % of net worth after the IPO lost 75 % of it and tripped the breaker)
    "swingMinEdge": 3.0,         # min backtested avg % per trade (train and test) to trade a strategy
    "swingCost": 1.4,            # % cost per side assumed in backtests (spread)
    "swingStop": 15.0,           # % loss that closes a swing (10 s median price, must persist 5 s; 0 = no stop); also
                                 # covers a pre-crash event short once the median is (1 + swingStop/100) x its entry
    "swingPauseLosses": 2,       # consecutive losses on a stock that pause it for 15 min
    "swingExcluded": ["$STUMP", "$STEAM"],  # no swing trades on these (parks/harvest still allowed): after the 3rd
                                 # IPO their swing shorts lost 3.2e13 net (2026-11-15 23:00-23:40) while harvests made 8.4e13
    "swingPauseFillLossPct": 10,  # a real loss this big counts toward the pause even if the seen prices made money
    "swingTimeExit": False,      # True: a swing also closes after its strategy's max hold. False (owner's choice): it is
                                 # held until its exit rule fires, however long that takes (backtests use the same rule)
    "backtestEveryMin": 10,
    # dividend parking
    "dividendPark": True,
    "parkIdleSec": 180,          # park only after this long without a swing entry
    "parkMinCashPct": 20.0,
    "parkPercent": 100,
    "parkMaxVol": 6.0,           # only park in stocks whose 1 s moves are calmer than this %
    "parkMaxRange": 15.0,        # ... and whose high/low over the last 10 min spans less than this % (0 = off)
    "parkBlockMin": 30,          # after a park stop-loss, don't park that stock again for this many minutes
    "parkStopLoss": 20.0,        # % median loss that sells a park (0 = no stop)
    "swingHoldLosersLong": False,   # never close a losing swing long on the strategy's exit: wait until it is
    "swingHoldLosersShort": False,  # ... back at swingHoldMinPct profit (shorts pay the dividend while they wait)
    "swingHoldMinPct": 0.5,
    "swingShortDivMaxPct": 3.0,  # a held losing short is closed once the dividends it paid pass this % of its value:
                                 # at a high DividendGain multiplier a short pays ~1.7 %/min, so waiting it out loses
    "swingTrailPct": 3.0,        # swing: when the strategy says take profit, ride it; sell this % off the best price
    "resetStackFrac": 0.0,      # crash longs: keep buying on every buy cooldown while under this x the old price
    "resetStackMax": 3,          # ... at most this many adds
    "resetTrailPct": 0.0,        # ... and at the recovery line ride it: sell this % under the peak (0 = sell there)
    "parkSpikeSell": True,      # sell a park when its price jumps to parkSpikeX x its 10 min median: take the jump,
    "parkSpikeX": 1.3,           # ... and skip the crash that often follows ($BASE spikes 1.3-1.9x seconds before)
    # risk
    "maxDrawdownPct": 30.0,      # circuit breaker: no new entries once the bot's money (realized + open, at the better
                                 # of median and last price) is this % below its best realized level since arming (0 = off)
    "breakerCloseAll": False,    # on a breaker trip also close every bot position (default: keep managing them)
    "breakerResumeMin": 0,       # after a trip, allow new entries again after this many minutes, with the peak reset
                                 # to the current level (0 = blocked until the bot is armed again)
    "maxPositionPct": 50.0,      # never put more than this % of net worth into one swing or park position
    "noisyVolPct": 8.0,          # a stock is noisy when its 1 s volatility is above this %
    "noisyCapPct": 20.0,         # max % of net worth parked in a noisy stock
    "excluded": [],
    # costs (price-impact model, measured per stock from the bot's own fills)
    "sizeByImpact": True,        # shrink an entry when entry + exit impact would eat the expected edge
    "dustPctOfCash": 0.1,        # a volume-capped entry worth less than this % of cash is skipped (it would only use a cooldown)
    "exitChunkAbovePct": 2.0,    # split a non-urgent exit into chunks when its estimated impact is above this % (0 = never)
    "exitChunkMax": 4,           # at most this many chunks
    "exitChunkSec": 1.0,         # seconds between chunks (after the previous chunk shows in the position)
    "parkHoldMin": 30,           # expected minutes a park is held: its dividend edge, for sizing
    "parkFirst": True,           # the buy slot goes to a park when its dividends over parkHoldMin beat the best long now
    "parkKeepPct": 20.0,         # % of net worth a park leaves in cash, so swing entries can go at once
    "parkDipStocks": {},         # crash stocks parked only in a crash dip: {"$PLAIN": 0.5} = at or under 0.5 x
                                 # the 10 min median; held for the dividends, sold at the next spike or the cap
    "parkCapSellPct": 0.0,       # also sell a park at this % of its stock's price cap (0 = off)
    "parkEntryPctile": 0.0,      # other parks buy only in the cheapest this % of the last parkLookMin (0 = off)
    "capShort": {},              # {"$WHALE": {"entryX": 0.98, "coverX": 0.6, "maxMin": 20, "pct": 100}}: short a stock
                                 # that crashes from its price cap (it cannot rise past the cap), cover in the crash
    "floorBuy": {},              # {"$VOID": {"entryX": 1.2, "sellX": 12, "maxMin": 5, "floorMin": 120}}: buy a stock at its
                                 # price floor (lowest of floorMin minutes), sell when it is sellX x the fill
    "floorBuySafeSec": 100,      # floor buys take the buy slot only this soon after a resetDetectBy dip (the next
                                 # $WHALE dip comes >= 187 s later), so they never block a crash buy; 0 = any time
    "parkSafeWindow": False,     # new parks and top-ups also wait for that window (a park buy took the slot 6 s
                                 # before a $WHALE dip, 2026-11-15 06:20)
    "parkKeepCrashReserve": False, # new parks leave the cash the next resetDetectBy crash buy needs (crash_reserve)
    "capShortKeepReserve": False, # cap shorts size down to leave the cash the next crash buy needs (crash_reserve)
    "pumpTopUp": True,           # a pump long bought hours early buys the shares freed since, until 8 s before T
    "parkCapSellFromBelow": False, # the cap sell (parkCapSellPct) only for parks bought under the cap zone
    "parkTopUp": False,          # an open park buys more of its stock when the game frees more shares (same entry
                                 # checks as a new park; the best dividend money per minute wins the buy slot)
    "parkTopUpReserve": True,    # ... but leave the cash the next resetDetectBy crash buy needs
    "parkTopUpTol": 0.0,         # ... and may buy up to this % above the parkLookMin median (a stock that drifts up
                                 #     is above its own hour median most of the time)
    "harvestStocks": {},         # spike/crash stocks whose held shares (the game's auto-buy, or the bot's own buy-back)
                                 # are sold at each spike and bought back right after the crash, e.g. {"$TECH": {}};
                                 # options: spikeX 1.35 (sell at this x the avgSec mean), avgSec 600, minGainX 2 (and
                                 # at this x the buy price), crashX 0.5 (buy back at this x the 10 s high), maxPct 5
                                 # (cash for a buy-back, %), start false (also start the cycle with no shares held),
                                 # overReserve false (buy back even while resetFastKeepBuy holds the buy slot)
    "parkLookMin": 60,           # a park buys only at or below the median price of this many minutes (no spike buys)
    "parkEntryTol": 0.0,         # ... or at most this % above it
    "carryPark": True,           # also park in volatile high-dividend stocks when their recorded parks paid:
    "carryMinP10": 0.0,          # ... 90 % of the recorded parks there (bought by the rule above, held parkHoldMin,
    "carryMinN": 30,             #     dividends included) made at least this %, over at least this many windows
    "parkResetExitSec": 240,     # sell a park this long before its stock's daily reset (resetSchedule; the price drops
                                 # ~97 % for minutes), and open none there within parkHoldMin + 5 min of one
    # companion plugin: it buys the upgrades and claims rewards; the bot only sends these settings (setAuto)
    "autoIpo": True,             # go public by itself once the run peak passes autoIpoBase x autoIpoMult^ipoCount
    "autoIpoBase": 3.998e18,     # (2026-11-16: $9.60Sx at ipoCount 4); autoIpoNeedNw > 0 overrides it
    "autoIpoMult": 7,
    "autoIpoNeedNw": 0,
    "autoIpoIdleSec": 60,        # it clicks in the game window: only after this long with no keyboard/mouse input
    "autoUpgrade": True,
    "autoUpgradeBuyCdPct": 3,
    "autoUpgradeShortCdPct": 1,
    "autoClaim": True,
    "smartUpgrade": True,        # the bot picks the upgrade (fastest payback from its own profits) instead of the plugin
    "upgradeMaxPaybackMin": 720, # ... buy a level only if it pays for itself within this many minutes
    "upgradeMaxCashPct": 40,     # ... and costs at most this % of cash
    "upgradeEverySec": 30,       # ... at most one level this often
    "upgradeLookMin": 120,       # ... profits measured over this many minutes (median of 10-minute slices)
    "upgradeMaxSpendPctHour": 10, # ... and at most this % of net worth on upgrades per rolling hour
}
AUTO_KEYS = ("autoUpgrade", "autoUpgradeBuyCdPct", "autoUpgradeShortCdPct", "autoClaim", "smartUpgrade")
FINAL = ("done", "missed", "skipped")
OPEN_ACTIONS = ("buymax", "buypercent", "shortmax", "shortpercent")
ACTIONS = ("buymax", "buypercent", "shortmax", "shortpercent", "closemax", "closepercent",
           "sellmax", "sellpercent", "covermax", "coverpercent")
PLUGIN_ACTIONS = {   # action -> {field: type}; required fields are listed in PLUGIN_REQUIRED
    "buyUpgrade": {"stat": "str", "count": "int"},
    "claimAll": {},
    "claimLevel": {"level": "int"},
    "dismissWelcome": {},
    "setAuto": {"upgrades": "bool", "claim": "bool", "dismissWelcome": "bool", "maxPaybackMin": "num",
                "cashPctPerHour": "num", "stats": "stats"},
    "refresh": {},
}
PLUGIN_REQUIRED = {"buyUpgrade": ("stat",), "claimLevel": ("level",)}
NAME_RE = r"[A-Za-z0-9_.\- ]{1,64}"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
IMPACT_PRIOR = 0.033   # live: entries at ~50 % of cash filled ~0.83 % worse than the price at send -> move ~3.3 %

ACK_TIMEOUT_MS = 20000      # a command the game never acknowledged is given up after this
RUN_TIMEOUT_MS = 120000     # ...and one stuck in "running" after this
OPEN_DEADLINE_MS = 30000    # an entry with no pending command and no new shares is dropped after this
STOP_CONFIRM_MS = 5000      # a stop must hold this long
STOP_GRACE_MS = 10000       # no stop in the first 10 s after a fill
MEDIAN_MS = 10000           # stops use the median of the last 10 s of prices
RATE_BURST, RATE_PER_SEC = 9.0, 0.9   # client-side bucket, a little under the game's 10 burst / 1 per s
TRANSIENT = ("rate-limited", "not-ready", "cooldown", "cannot-afford", "queue-full", "expired")
TICK_GATE_MS = 450          # entries and target exits go out only this early in a 1 s price tick, so they execute in
                            # it at the price they were decided on (later ones fill at the next tick, 30-80 % away
                            # on jumpy stocks); stops and forced exits go at any time
POLL_SEC = 0.05             # market.json is checked this often (a stat); a full pass runs when it changed ...
PASS_SEC = 0.25             # ... and at least this often
OPEN_SETTLE_MS = 4000       # an open's fill is final this long after it shows: the first snapshot can hold the
                            # game's prediction, which the server then re-prices (same shares, new average) or trims
REPRICE_WINDOW_MS = 15000   # a re-price within this long of the open still re-anchors the position (entry, stop, target)
EASE_MS = 2000.0            # MarketEaser: the game sizes percent orders at its display price, eased over 2 s


def now_ms():
    return int(time.time() * 1000)


def num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def side_of(action):
    return "buy" if action.startswith("buy") else "short" if action.startswith("short") else None


def trade_kind(action):
    """('open'|'close', 'long'|'short'|None) for a command action."""
    a = action or ""
    if a.startswith("buy"):
        return "open", "long"
    if a.startswith("short"):
        return "open", "short"
    if a.startswith("sell"):
        return "close", "long"
    if a.startswith("cover"):
        return "close", "short"
    if a.startswith("close"):
        return "close", None
    return None, None


def _finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def jdumps(obj):
    """JSON for the browser: NaN / Infinity (JSON.parse rejects them) become null."""
    try:
        return json.dumps(obj, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(obj))


class BgWriter:
    """Writes files on a background thread, newest text per path wins. state.json and impact_model.json were written
    on the trading thread: one write took 1.2 s live (2026-11-14), long enough to miss a $WHALE dip."""

    def __init__(self):
        self.want = {}                 # path -> (text, on_error)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None

    def put(self, path, text, on_error=None):
        with self.lock:
            self.want[str(path)] = (text, on_error)
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="file-writer", daemon=True)
            self.thread.start()
        self.wake.set()

    def _run(self):
        while True:
            self.wake.wait(5.0)
            self.wake.clear()
            self.flush()

    def flush(self):
        with self.lock:
            items, self.want = self.want, {}
        for path, (text, on_error) in items.items():
            try:
                write_atomic(path, text)
            except OSError as e:
                with self.lock:
                    self.want.setdefault(path, (text, on_error))     # retry with the next wake
                if on_error:
                    try:
                        on_error(e)
                    except Exception:
                        pass


BG_WRITER = BgWriter()


class BgAppender:
    """Appends lines to the .jsonl logs on a background thread, in order. The data folder can sit on a spinning
    disk: one append on the trading thread blocked 1-2.6 s live (2026-11-14), long enough to miss a $WHALE dip.
    sync=True writes at once (the replay harness reads the files right after a run)."""

    MAX = 50000

    def __init__(self):
        self.q = []                    # (path, line)
        self.lock = threading.Lock()
        self.io = threading.Lock()     # one writer at a time: the thread or flush()
        self.wake = threading.Event()
        self.thread = None
        self.sync = False
        self.on_error = None

    def add(self, path, line):
        if self.sync:
            with self.io:
                self._write([(str(path), line)])
            return
        with self.lock:
            self.q.append((str(path), line))
            if len(self.q) > self.MAX:
                del self.q[:len(self.q) - self.MAX]
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="log-appender", daemon=True)
            self.thread.start()
        self.wake.set()

    def _run(self):
        while True:
            self.wake.wait(5.0)
            self.wake.clear()
            self.flush()

    def flush(self):
        """Write everything queued now (also called before a log file is read back)."""
        with self.io:
            with self.lock:
                items, self.q = self.q, []
            left = self._write(items)
            if left:
                with self.lock:
                    self.q[:0] = left              # retry with the next wake, order kept

    def _write(self, items):
        by = {}
        for path, line in items:
            by.setdefault(path, []).append(line)
        left = []
        for path, lines in by.items():
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("".join(x + "\n" for x in lines))
            except OSError as e:
                left += [(path, x) for x in lines]
                if self.on_error:
                    try:
                        self.on_error(path, e)
                    except Exception:
                        pass
        return left


APPENDER = BgAppender()


def write_atomic(path, text):
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()          # don't leave a half-written temp file on a full disk
        except OSError:
            pass
        raise


def split_launch(cmd):
    """'C:\\x\\go.cmd' / '"C:\\Program Files\\x.exe" /a b' / 'C:\\Program Files\\x.exe /a' -> (path, [args]);
    anything else (steam://..., https://...) -> (None, [])."""
    s = (cmd or "").strip()
    m = re.match(r'"([^"]+)"\s*(.*)$', s) or re.match(r'(.+?\.(?:exe|cmd|bat))(?=\s|$)\s*(.*)$', s, re.I)
    if not m or "://" in m.group(1) or not m.group(1).lower().endswith((".exe", ".cmd", ".bat")):
        return None, []
    rest = m.group(2).strip()
    args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(rest, posix=False)] if rest else []
    return m.group(1), args


# ---------------------------------------------------------------- game window (Windows)
class GameWindow:
    WM_CLOSE = 0x0010

    def __init__(self, title=GAME_TITLE, launch=LAUNCH, wclass=GAME_CLASS):
        self.title = title
        self.wclass = wclass or GAME_CLASS
        self.launch_cmd = launch or LAUNCH
        self.user32 = None
        if sys.platform == "win32":
            from ctypes import wintypes
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.FindWindowW.restype = wintypes.HWND
            u.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
            u.IsWindowVisible.argtypes = (wintypes.HWND,)
            u.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
            u.SetForegroundWindow.argtypes = (wintypes.HWND,)
            u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            self.user32 = u

    def handle(self):
        return (self.user32.FindWindowW(self.wclass, self.title) or None) if self.user32 else None

    def state(self):
        h = self.handle()
        if not h:
            return "not running"
        return "visible" if self.user32.IsWindowVisible(h) else "hidden"

    def hide(self):
        h = self.handle()
        if h:
            self.user32.ShowWindow(h, 0)
        return bool(h)

    def show(self):
        h = self.handle()
        if h:
            self.user32.ShowWindow(h, 5)
            self.user32.SetForegroundWindow(h)
        return bool(h)

    def close(self):
        h = self.handle()
        if h:
            self.user32.PostMessageW(h, self.WM_CLOSE, 0, 0)
        return bool(h)

    def launch(self):
        """Run --launch: a .cmd/.bat/.exe path (with arguments: subprocess, without: os.startfile) or a URL."""
        if sys.platform != "win32":
            return False
        exe, args = split_launch(self.launch_cmd)
        if not exe:
            os.startfile(self.launch_cmd.strip().strip('"'))      # steam://rungameid/... or another URL
            return True
        p = Path(exe)
        if not p.is_absolute():
            p = next((b / p for b in (Path.cwd(), HERE) if (b / p).exists()), p)
        if not args:
            os.startfile(str(p))
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if p.suffix.lower() in (".cmd", ".bat") else 0
            subprocess.Popen([str(p), *args], cwd=str(p.parent) if p.parent.is_dir() else None, close_fds=True,
                             creationflags=flags)
        return True


# ---------------------------------------------------------------- price impact
class ImpactModel:
    """Per-stock price impact of the player's own trades, measured from the bot's fills.

    How the game moves the price (decompiled ManualTradePredictionLedger.Simulate):
      open  (buy/short) spending fraction f of cash: price p -> p*(1 +- move*f), fill at the midpoint p*(1 +- move*f/2)
      close (sell/cover) of fraction q of a position: x = R*q, price p -> p*(1 -+ move*x), fill at the midpoint;
            R (remaining close impact) is 1 for a fresh position and drops by x with each partial close, so a full
            exit always moves the price by move*R no matter how big the position is
      prices clamp to [1, cap]; move = server PlayerTradeMove x the stock's playerImpactMultiplier (not exported).
    Each bot fill gives cost = (fill / price at send - 1), signed so that worse is positive, ~= move*x/2.
    move per stock = median of 2*cost/x over its last fills with x >= 0.1, shrunk toward the median of all stocks.
    A stock with fewer than 3 fills, or whose fills disagree (quartile spread >= median) or look implausible
    (median <= 0 or >= 50 %), gets the prior: on a noisy stock ($STEAM) the price moves more between send and
    fill than any impact, and the backtests already price that by filling at the next sample."""
    KEEP = 60
    MIN_X = 0.1
    SHRINK = 3.0

    def __init__(self, path, note_once=None):
        self.path = Path(path)
        self.note_once = note_once
        self.obs = {}            # stock -> [{t, kind, side, x, cost, base, fill, ...}]
        self.est = {}            # stock -> {move, n, raw, mad}
        self.prior = IMPACT_PRIOR
        self.updated = None
        self.dirty = False
        self.saved_ms = 0
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.updated = d.get("updatedMs")
            for s, v in (d.get("stocks") or {}).items():
                lst = [o for o in (v.get("obs") or []) if isinstance(o, dict)] if isinstance(v, dict) else []
                if lst:
                    self.obs[s] = lst[-self.KEEP:]
        except Exception:
            pass
        self.fit()

    def _u(self, o):
        x, c = num(o.get("x")), num(o.get("cost"), float("nan"))
        if x < self.MIN_X or not math.isfinite(c):
            return None
        return 2 * c / x                           # unclipped move estimate of one fill

    def fit(self):
        stats_ = {}
        for s, lst in self.obs.items():
            us = sorted(u for u in (self._u(o) for o in lst) if u is not None)
            n = len(us)
            if not n:
                continue
            um = statistics.median(us)
            iqr = us[(3 * n) // 4] - us[n // 4] if n >= 4 else None
            ok = n >= 3 and 0 < um < 0.5 and (iqr is None or iqr < um)    # one or two fills prove nothing
            ms = [min(1.0, max(0.0, u)) for u in us]
            raw = statistics.median(ms)
            stats_[s] = {"n": n, "raw": raw, "median": um, "iqr": iqr, "consistent": ok,
                         "mad": statistics.median([abs(m - raw) for m in ms])}
        meds = [v["raw"] for v in stats_.values() if v["n"] >= 3 and v["consistent"]]
        self.prior = statistics.median(meds) if meds else IMPACT_PRIOR
        self.est = {}
        for s, v in stats_.items():
            n = v["n"]
            mv = (n * v["raw"] + self.SHRINK * self.prior) / (n + self.SHRINK) if v["consistent"] else self.prior
            self.est[s] = {"move": mv, **v}

    def move(self, stock):
        e = self.est.get(stock)
        return e["move"] if e else self.prior

    def entry_cost(self, stock, f):
        """Expected fill cost (fraction of price) of an open spending fraction f of cash."""
        return self.move(stock) * max(0.0, f) / 2

    def exit_cost(self, stock, x=1.0):
        """Expected fill cost of a close with impact fraction x (R*q; 1 = full exit of a fresh position)."""
        return self.move(stock) * max(0.0, x) / 2

    def best_fraction(self, stock, edge, f_max):
        """Fraction of cash maximising f*cash*(edge - move*f/2 - move/2): entry impact plus a full exit."""
        m = self.move(stock)
        if m <= 1e-6:
            return max(0.0, f_max)
        return max(0.0, min(f_max, (edge - m / 2) / m))

    def add(self, stock, ob):
        lst = self.obs.setdefault(stock, [])
        lst.append(ob)
        del lst[:-self.KEEP]
        self.updated = now_ms()
        self.dirty = True
        self.fit()

    def view(self, stock):
        e = self.est.get(stock) or {}
        pct = lambda k: (e[k] * 100) if e.get(k) is not None else None     # noqa: E731
        return {"move": self.move(stock) * 100, "n": e.get("n", 0), "raw": pct("raw"), "mad": pct("mad"),
                "iqr": pct("iqr"), "src": "measured" if e.get("consistent") else "prior",
                "consistent": bool(e.get("consistent")), "exitPct": self.exit_cost(stock) * 100}

    def save(self, force=False):
        if not self.dirty or (not force and now_ms() - self.saved_ms < 5000):
            return
        self.saved_ms = now_ms()
        blob = {"version": 1, "updatedMs": self.updated, "prior": self.prior,
                "model": "open: fill = p*(1 +- move*f/2), f = fraction of cash; close: fill = p*(1 -+ move*R*q/2)",
                "stocks": {s: {**self.est.get(s, {}), "obs": lst} for s, lst in self.obs.items()}}
        nonce = self.note_once
        BG_WRITER.put(self.path, json.dumps(blob), (lambda e: nonce("impact-save", f"couldn't save {self.path.name}: "
                                                                     f"{e}", "warn")) if nonce else None)
        self.dirty = False


# ---------------------------------------------------------------- agent
def _totals():
    return {"n": 0, "win": 0, "sum": 0.0, "money": 0.0, "cost": 0.0, "fills": 0}


class TradeLog:
    """trades.jsonl, read incrementally for /api/trades (the file only grows; if it shrinks it was replaced)."""
    MAX = 200000

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.offset = 0
        self.rows = []

    def read(self):
        with self.lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                return list(self.rows)
            if size < self.offset:
                self.offset, self.rows = 0, []
            if size > self.offset:
                with open(self.path, "rb") as fh:
                    fh.seek(self.offset)
                    chunk = fh.read(size - self.offset)
                end = chunk.rfind(b"\n") + 1            # complete lines only
                for line in chunk[:end].splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        self.rows.append(e)
                self.offset += end
                del self.rows[:-self.MAX]
            return list(self.rows)


class NetWorthLog:
    """Net worth and cash every second in <data>/networth.db, kept across restarts. Rows are written by a background
    thread every WRITE_SEC in one transaction: a commit on the trading thread took 1.4-1.8 s live (and up to the 5 s
    busy timeout while the chart read the table), long enough to miss a $WHALE dip that lasts 2-4 s."""

    WRITE_SEC = 2.0
    MAX_QUEUE = 100000

    def __init__(self, path, note_once):
        self.path = Path(path)
        self.note_once = note_once
        self.ok = False
        self.q = collections.deque()
        self._thread = None
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _init(self):
        try:
            con = self._connect()
            try:
                con.execute("CREATE TABLE IF NOT EXISTS nw (t INTEGER PRIMARY KEY, nw REAL, cash REAL)")
                con.commit()
            finally:
                con.close()
            self.ok = True
        except sqlite3.Error as e:
            self.note_once("nwdb", f"net-worth history unavailable: {e}", "warn", 600000)

    def add(self, t, nw, cash):
        """Queue one row (never blocks: the writer thread starts on the first row)."""
        self.q.append((int(t), float(nw), float(cash)))
        while len(self.q) > self.MAX_QUEUE:
            self.q.popleft()
        if self._thread is None:
            self._thread = threading.Thread(target=self._writer, name="nw-writer", daemon=True)
            self._thread.start()

    def _writer(self):
        wake = threading.Event()
        while True:
            wake.wait(self.WRITE_SEC)
            if self.q:
                self.flush()

    def flush(self):
        rows = []
        while self.q:
            rows.append(self.q.popleft())
        if not rows:
            return
        if not self.ok:
            self._init()
        try:
            if not self.ok:
                raise sqlite3.OperationalError("database unavailable")
            con = self._connect()
            try:
                con.executemany("INSERT OR REPLACE INTO nw VALUES (?, ?, ?)", rows)
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as e:
            self.q.extendleft(reversed(rows[-self.MAX_QUEUE:]))       # try again next time, oldest first
            self.note_once("nwdb-write", f"couldn't record net worth: {e}", "warn", 600000)

    def query(self, range_sec, points=1000, frm=None, to=None):
        """Rows [t, nw, cash] over the last range_sec seconds (0 = all), or between frm and to (ms), the last row
        of each of ~points buckets (1 s rows when the span is short enough)."""
        since = int(frm) if frm else (now_ms() - int(range_sec * 1000) if range_sec > 0 else 0)
        until = int(to) if to else 2 ** 62
        con = self._connect()
        try:
            lo, hi, n = con.execute("SELECT MIN(t), MAX(t), COUNT(*) FROM nw WHERE t >= ? AND t <= ?",
                                    (since, until)).fetchone()
            if not n:
                return {"points": [], "step": 1000, "count": 0, "range": range_sec}
            step = max(1000, int((hi - lo) / points) + 1)
            rows = con.execute("SELECT t, nw, cash FROM nw WHERE t IN (SELECT MAX(t) FROM nw WHERE t >= ? AND t <= ? "
                               "GROUP BY (t - ?) / ?) ORDER BY t", (since, until, lo, step)).fetchall()
        finally:
            con.close()
        return {"points": [list(r) for r in rows], "step": step, "count": n, "range": range_sec}


class Bot:
    def __init__(self, mods, data_dir, title=GAME_TITLE, launch=LAUNCH, wclass=GAME_CLASS):
        self.mods = Path(mods)
        self.export = self.mods / "export"
        self.cmds = self.mods / "commands"
        self.plugin_cmds = self.mods / "plugin-commands"
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.window = GameWindow(title, launch, wclass)
        self.log = collections.deque(maxlen=500)
        self.warned = {}
        self.cfg = dict(DEFAULTS)
        for k, v in self._load_json(self.data / "config.json", {}).items():
            try:
                self.set_cfg(k, v)
            except (TypeError, ValueError):
                pass
        self.cfg["armed"] = False
        self.brain = Brain(self.data, self.note)
        self.market = None
        self.market_mtime = 0
        self.history_mtime = 0
        self.plugin = None
        self.plugin_mtime = None
        self.plugin_seen = 0.0      # mtime (s) of plugin.json at the last stat
        self.managed = {}           # stock -> bot position {kind, side, state, entry, entryT, shares, ...}
        self.harvest = {}           # stock -> {shares, px, t} sold at a spike, to buy back on the crash (run_harvest)
        self.events = {}            # occurrenceId -> event state machine
        self.pending = []           # commands sent, waiting for a result
        self.expired = collections.deque(maxlen=50)   # given up on, but a late result is still matched
        self.seen = {}
        self.impact = ImpactModel(self.data / "impact_model.json", self.note_once)
        fl = self._load_json(self.data / "fills.json", [])
        self.fills = collections.deque([x for x in fl if isinstance(x, (int, float))] if isinstance(fl, list) else [], maxlen=200)
        self.recent_fills = collections.deque(maxlen=200)   # position changes waiting to be matched to a result
        self.unflushed = collections.deque()                # trade entries not yet in trades.jsonl (waiting for a fill)
        self.trades = collections.deque(self._load_trades(), maxlen=500)
        self.trade_log = TradeLog(self.data / "trades.jsonl")
        self.nwlog = NetWorthLog(self.data / "networth.db", self.note_once)
        self.last_nwdb_ms = 0
        self.nw = collections.deque(maxlen=20000)
        self.realized = collections.defaultdict(_totals)          # bot kind -> totals
        self.realized_stock = collections.defaultdict(_totals)    # stock -> totals
        self.auto_dirty = True          # send the plugin its auto settings on the first fresh plugin.json
        self.auto_retry_ms = 0
        self.plugin_sync = {"sentAt": None, "file": None, "error": None, "last": None}
        self.refusal = ""
        self._disk_t, self._disk_mb = 0.0, None
        self.losses = collections.Counter()
        self.pause_until = {}
        self.park_block = {}        # stock -> time (local ms) of its last park stop-loss
        self.exit_backoff = {}      # stock -> {"n": failures in a row, "until": ms}
        self.side_block = {"buy": 0, "short": 0}   # no opens of that side before (local ms)
        self.stock_block = {}       # (stock, side) -> no opens before (local ms)
        self.tokens, self.tokens_t, self.rate_block_until = 5.0, time.monotonic(), 0
        self.ticks = collections.defaultdict(lambda: collections.deque(maxlen=240))  # stock -> (ms, price)
        self.price_key = None
        self.snap = None
        self.risk = {"armedAt": None, "tripped": False}
        self.restarting = False
        self.start_nw = None
        self.started_ms = now_ms()
        self.last_nw_ms = 0
        self.last_swing_ms = now_ms()
        self.saved_blob = ""
        self.last_buy_ms = now_ms()      # last buy-side entry (swing or event long): parking waits parkIdleSec after it
        self.park_try_ms = 0             # last park entry or refusal
        self.park_first_ms = 0           # when run_swing last gave the buy slot to a park (parkFirst)
        self.close_hist = collections.deque()   # (t, stock, pnl) of swing/event closes, last 3 h (park_pick)
        self.close_hist_since = None             # first close on record (None: closes.jsonl not read yet)
        self.divs = {"total": 0.0, "n": 0, "mult": None, "last": None}   # dividends received (cash, payouts)
        self.last_prune = 0
        self.last_error = ""
        self.last_error_ms = 0
        self.error_count = 0
        self.ease = {}              # stock -> {"A", "P", "T"}: the game's eased display price (percent orders size at it)
        self.tick_ms = 0            # local time of the current 1 s price tick (market.json lastPriceUpdateAtMs)
        self.tick_fed = False       # the brain takes market.json ticks (Brain.ingest_tick): decisions use them at once
        self._tick_mode = None
        self.market_bad = None      # mtime of a market.json that didn't parse (the fast poll waits for the next one)
        self.perf = {"passes": 0, "slow": 0, "maxMs": 0.0, "recent": collections.deque(maxlen=20),
                     "api": {}}       # pass timing: slow passes miss the seconds-long $WHALE dips
        self._pass_t = None
        self._tick_called = 0.0
        self.last_result_ms = 0     # when the last command result came in
        self.ext = {"total": 0.0, "n": 0, "last": None}   # cash changes that weren't trades (spending, income, dividends)
        self.port = PORT
        self._restore()
        self.note("agent started (bot OFF)")

    # ---- config
    def set_cfg(self, k, v):
        """Validated config write (raises ValueError/TypeError on bad input)."""
        if k not in DEFAULTS or k == "armed":
            return False
        d = DEFAULTS[k]
        if isinstance(d, bool):
            val = bool(v)
        elif isinstance(d, list):
            if not isinstance(v, (list, tuple)):
                raise ValueError(f"{k} must be a list")
            val = [str(x) for x in v]
        elif isinstance(d, (int, float)):
            x = float(v)
            if not math.isfinite(x) or x < 0:
                raise ValueError(f"{k} must be a number >= 0")
            val = int(x) if isinstance(d, int) and x.is_integer() else x
        else:
            val = type(d)(v)
        self.cfg[k] = val
        return True

    def save_cfg(self):
        """Write config.json; returns an error message instead of raising (the change stays in effect)."""
        try:
            self._write_atomic(self.data / "config.json", json.dumps(self.cfg, indent=2))
            return ""
        except OSError as e:
            self.note_once("save-cfg", f"couldn't save config.json: {e}", "warn")
            return f"couldn't save config.json: {e}"

    def arm(self, on, why="", keep_trip=False):
        """Arming starts a new breaker baseline and clears a trip, unless keep_trip (a restart restoring the
        armed state with --arm / armOnStart, or POST /api/arm with keepBreaker): a tripped breaker then stays
        tripped, so new entries stay blocked."""
        self.cfg["armed"] = bool(on)
        if on:
            old = self.risk if self.risk.get("tripped") else None
            nw = self.net_worth() if self.fresh() else 0.0
            self.risk = {"armedAt": now_ms(), "armNw": nw or None, "peak": nw or None, "realized": 0.0,
                         "unrealized": 0.0, "pnl": 0.0, "peakPnl": 0.0, "drawdown": 0.0, "tripped": False}
            for s, mg in self.managed.items():
                if mg.get("state") in ("open", "exiting"):
                    mg["armRef"] = self.median_price(s) or mg.get("entry")
                    mg.pop("armAtEntry", None)
            self.note(f"bot ON: trading{(' (' + why + ')') if why else ''}")
            if old and keep_trip:
                self.risk.update(tripped=True, trippedAt=old.get("trippedAt"), tripMsg=old.get("tripMsg"))
                when = (time.strftime("%m-%d %H:%M:%S", time.localtime(num(old.get("trippedAt")) / 1000))
                        if old.get("trippedAt") else "earlier")
                self.note(f"⛔ circuit breaker still tripped (since {when}): no new entries, open positions stay "
                          f"managed. Arm the bot again from the dashboard to resume entries.", "warn")
            elif old:
                self.note("circuit breaker reset: new entries allowed again")
        else:
            self.note(f"bot OFF{(' (' + why + ')') if why else ''}")

    # ---- persistence
    @staticmethod
    def _load_json(path, default):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_atomic(path, text):
        write_atomic(path, text)

    def _load_trades(self):
        out = []
        p = self.data / "trades.jsonl"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-500:] if p.exists() else []
        except OSError:
            lines = []
        for line in lines:
            try:
                e = json.loads(line)
                if isinstance(e, dict):
                    out.append(e)
            except Exception:
                pass
        out.reverse()
        return out

    def _restore(self):
        st = self._load_json(self.data / "state.json", {})
        if not isinstance(st, dict):
            st = {}
        t = now_ms()
        self.managed = {k: v for k, v in (st.get("managed") or {}).items() if isinstance(v, dict) and v.get("side")}
        self.events = {k: v for k, v in (st.get("events") or {}).items()
                       if isinstance(v, dict) and v.get("stock") and num(v.get("at")) > t - 2 * 3600000}
        for mg in self.managed.values():
            mg.setdefault("kind", "swing")
            if mg.get("state") == "exiting":
                mg["state"] = "open"            # the exit is re-checked against real positions
            elif mg.get("state") == "opening":
                mg["deadline"] = t + OPEN_DEADLINE_MS   # confirm_opens decides by the shares held
            elif mg.get("state") != "open":
                mg["state"] = "open"
        self.pause_until = {k: v for k, v in (st.get("pauseUntil") or {}).items() if num(v) > t}
        self.harvest = {k: v for k, v in (st.get("harvest") or {}).items() if isinstance(v, dict)}
        self.losses = collections.Counter(st.get("losses") or {})
        rk = st.get("risk")
        if isinstance(rk, dict) and rk.get("tripped"):      # a trip survives a restart (see arm(keep_trip))
            self.risk.update(tripped=True, trippedAt=rk.get("trippedAt"), tripMsg=rk.get("tripMsg"))
        for src, dst in (("realized", self.realized), ("realizedByStock", self.realized_stock)):
            for k, v in (st.get(src) or {}).items():
                if isinstance(v, dict) and k not in ("total", "byStock"):
                    dst[k].update({kk: v[kk] for kk in _totals() if isinstance(v.get(kk), (int, float))})

    def save_state(self):
        blob = json.dumps({"managed": self.managed, "events": self.events, "pauseUntil": self.pause_until,
                           "harvest": self.harvest,
                           "losses": dict(self.losses), "realized": {k: dict(v) for k, v in self.realized.items()},
                           "realizedByStock": {k: dict(v) for k, v in self.realized_stock.items()},
                           "risk": ({k: self.risk.get(k) for k in ("tripped", "trippedAt", "tripMsg")}
                                    if self.risk.get("tripped") else {}),
                           "armed": self.cfg["armed"]})
        if blob == self.saved_blob:
            return
        self.saved_blob = blob
        BG_WRITER.put(self.data / "state.json", blob,
                      lambda e: self.note_once("save", f"couldn't save state.json: {e}", "warn"))

    def _append(self, name, obj):
        """Queue one line for <data>/name (written in order by the background appender)."""
        if APPENDER.on_error is None:
            APPENDER.on_error = lambda path, e: self.note_once(f"append-{Path(path).name}",
                                                               f"couldn't write {Path(path).name}: {e}", "warn")
        APPENDER.add(self.data / name, json.dumps(obj))

    def note(self, msg, level="info"):
        self.log.appendleft({"t": now_ms(), "level": level, "msg": msg})
        (log.error if level == "error" else log.warning if level == "warn" else log.info)("%s", msg)

    def note_once(self, key, msg, level="warn", every_ms=60000):
        t = now_ms()
        if t - self.warned.get(key, 0) >= every_ms:
            self.warned[key] = t
            self.note(msg, level)

    # ---- reading the export
    def read_files(self):
        p = self.export / "market.json"
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            mt = None
        if mt and mt != self.market_mtime:
            ok = False
            for _ in range(3):
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    time.sleep(0.03)
                    continue
                if isinstance(m, dict):
                    ok = True
                    self.market = m
                    self.market_mtime = mt
                    self._safe("snapshot", self.on_snapshot)
                break
            self.market_bad = None if ok else mt
        h = self.export / "history.json"
        try:
            ht = h.stat().st_mtime_ns
        except OSError:
            ht = None
        if ht and ht != self.history_mtime:
            try:
                data = json.loads(h.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = None                       # half-written: read it again next tick
            if isinstance(data, dict):
                self.history_mtime = ht
                try:
                    self.brain.ingest(data)
                except Exception as e:            # full disk, locked or read-only prices.db: keep trading
                    self.note_once("history", f"couldn't record price history ({type(e).__name__}: {e}); "
                                              "trading continues on the prices in memory", "warn", 600000)
        try:
            self.read_plugin()
        except Exception as e:
            self.note_once("plugin-read", f"plugin.json: {type(e).__name__}: {e}", "warn")

    def read_plugin(self):
        p = self.export / "plugin.json"
        try:
            stt = p.stat()
        except OSError:
            self.plugin_seen = 0.0
            return
        self.plugin_seen = stt.st_mtime
        if stt.st_mtime_ns == self.plugin_mtime:
            return
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return                                # partial write: keep the last good copy, retry next tick
        self.plugin_mtime = stt.st_mtime_ns
        if isinstance(d, dict):
            self.plugin = d

    def plugin_fresh(self):
        return bool(self.plugin is not None and self.plugin_seen and time.time() - self.plugin_seen < 30)

    def on_snapshot(self):
        """A new market.json. On a new price tick: feed the brain at once (history.json carries the same sample
        ~0.65 s later, too late to act in that tick), follow the game's eased display price and record a sample
        for the stop medians. Then attribute fills and cash."""
        m = self.market
        key = (m.get("serverTick"), m.get("lastPriceUpdateAtMs")) if (m.get("serverTick") or m.get("lastPriceUpdateAtMs")) \
            else m.get("sequence")
        if key != self.price_key:
            self.price_key = key
            t = now_ms()
            lpu = num(m.get("lastPriceUpdateAtMs"))
            tick = lpu if t - 60000 < lpu <= t + 1000 else t      # the game's clock is this PC's clock
            self.tick_ms = tick
            P = {}
            for s in m.get("stocks") or []:
                px = num(s.get("price"))
                if px > 0 and s.get("stockId"):
                    P[s["stockId"]] = px
                    self.ticks[s["stockId"]].append((t, px))
                    self._ease_tick(s["stockId"], px, tick)
            srv, wr = num(m.get("serverTimeMs")), num(m.get("writtenAtMs"))
            if P and srv:
                # the sample's server time, as history.json stamps it: market.json is written a little after the tick
                lag = wr - lpu if (wr and lpu and 0 <= wr - lpu < 60000) else 0
                self._feed_tick(int(round(srv - lag)), P)
        self.track_fills()

    def _feed_tick(self, t_srv, prices):
        """Give the brain this tick's prices (Brain.ingest_tick, idempotent; history.json repeats them later)."""
        fn = getattr(self.brain, "ingest_tick", None)
        if not callable(fn):
            self.tick_fed = False
            return
        try:
            if self._tick_mode is None:
                try:
                    ps = [x for x in inspect.signature(fn).parameters.values()
                          if x.kind in (x.POSITIONAL_ONLY, x.POSITIONAL_OR_KEYWORD)]
                    self._tick_mode = "all" if len(ps) == 2 else "stock"     # (t, prices) or (stock, t, price)
                except (TypeError, ValueError):
                    self._tick_mode = "stock"
            if self._tick_mode == "all":
                fn(t_srv, dict(prices))
            else:
                for s, px in prices.items():
                    fn(s, t_srv, px)
            if not self.tick_fed:
                self.note("deciding on market.json ticks (the brain takes them as they come)")
            self.tick_fed = True
        except Exception as e:
            self.tick_fed = False
            self.note_once("ingest-tick", f"brain.ingest_tick failed ({type(e).__name__}: {e}); deciding on "
                                          f"history.json samples instead", "warn", 600000)

    def tick_window_open(self):
        """True early in a price tick (TICK_GATE_MS), when an order still executes at the tick's price. Only gates
        while the brain decides on market.json ticks: on history.json samples every decision is late anyway."""
        if not self.tick_fed or not self.tick_ms:
            return True
        return now_ms() - self.tick_ms <= TICK_GATE_MS

    # ---- the game's eased display price (decompiled MarketEaser): percent orders are sized at it
    def _ease_tick(self, s, px, t):
        st = self.ease.get(s)
        self.ease[s] = {"A": self._eased(st, t) if st else px, "P": px, "T": t}

    @staticmethod
    def _eased(st, t):
        u = min(1.0, max(0.0, (t - st["T"]) / EASE_MS))
        return st["A"] + (st["P"] - st["A"]) * (1 - (1 - u) ** 3)

    def plan_ratio(self, stock, price):
        """The game turns `percent` of cash into a share count at its eased display price (each tick eases from
        where the display was toward the new price over 2 s, cubic) and the server fills that count at its own
        price: the cash used is percent x price / eased. Returns the lowest eased / price over the pickup window
        (the mod polls command files every 50 ms), so percent x ratio uses at most the intended fraction."""
        st = self.ease.get(stock)
        if not st or not price or price <= 0:
            return 1.0
        t = now_ms()
        e = min(self._eased(st, t + 10), self._eased(st, t + 150))
        return e / price if e > 0 else 1.0

    def fresh(self):
        m = self.market
        return bool(m and m.get("marketReady") and now_ms() - num(m.get("writtenAtMs")) < 15000)

    def server_now(self):
        m = self.market
        if m and m.get("serverTimeMs"):
            return num(m["serverTimeMs"]) + (now_ms() - num(m.get("writtenAtMs"), now_ms()))
        return now_ms()

    def player(self):
        return (self.market or {}).get("player") or {}

    def cash(self):
        return num(self.player().get("cash"))

    def net_worth(self):
        return num(self.player().get("netWorth"))

    def prices(self):
        return {s["stockId"]: num(s.get("price")) for s in (self.market or {}).get("stocks") or [] if s.get("stockId")}

    def stock_info(self):
        return {s["stockId"]: s for s in (self.market or {}).get("stocks") or [] if s.get("stockId")}

    def positions(self):
        out = {}
        for p in self.player().get("positions") or []:
            if p.get("stockId"):
                out[p["stockId"]] = {"owned": num(p.get("sharesOwned")), "avgBuy": num(p.get("averageBuyPrice")),
                                     "shorted": num(p.get("sharesShorted")), "avgShort": num(p.get("averageShortPrice"))}
        return out

    def median_price(self, stock):
        buf = self.ticks.get(stock)
        if not buf:
            return self.prices().get(stock)
        cut = buf[-1][0] - MEDIAN_MS
        vals = [p for t, p in buf if t >= cut]
        return statistics.median(vals) if vals else buf[-1][1]

    def range_pct(self, stock, sec=600):
        """High/low span of the last `sec` seconds, % of the low, on 10-sample medians (tick noise can't hit
        the median stop, slow waves can). None without 5 minutes of prices."""
        ser = self.brain.series.get(stock)
        if not ser:
            return None
        cut, ps = ser[-1][0] - sec * 1000, []
        for t, p in reversed(ser):
            if t < cut:
                break
            ps.append(p)
        if len(ps) < 300:
            return None
        meds = [statistics.median(ps[i:i + 10]) for i in range(0, len(ps) - 9, 10)]
        lo = min(meds)
        return (max(meds) / lo - 1) * 100 if lo > 0 else None

    def noisy(self, stock):
        vol = self.brain.volatility(stock)
        return vol is None or vol * 100 > self.cfg["noisyVolPct"]

    # ---- fills: every position change between two complete snapshots
    def _got_dividends(self, flow):
        base = self._div_base(self.positions(), self.prices())     # no position changed in this snapshot
        tot = sum(base.values())
        d = self.divs
        if tot:
            m = flow / tot
            d["mult"] = m if d["mult"] is None else 0.7 * d["mult"] + 0.3 * m
        d["total"] += flow
        d["n"] += 1
        bot = {s: v * (flow / tot if tot else 1.0) for s, v in base.items()
               if (self.managed.get(s) or {}).get("state") in ("open", "exiting")}
        d["last"] = {"t": now_ms(), "cash": round(flow, 4), "bot": round(sum(bot.values()), 4)}
        parts = ", ".join(f"{s} {v:+,.2f}" for s, v in sorted(bot.items(), key=lambda kv: -abs(kv[1])))
        self.note(f"💵 dividends {flow:+,.2f}{(' (bot positions: ' + parts + ')') if parts else ''}", "ok")

    def _buy_marks(self):
        """Exported fields that only change when the player buys something (cooldown upgrades, unlocks, an IPO)."""
        pl = self.player()
        return (pl.get("ipoCount"), pl.get("buyCooldownSeconds"), pl.get("shortCooldownSeconds"),
                tuple(sorted((str(s.get("stockId")), bool(s.get("unlocked", True)), str(s.get("maxVolume")))
                             for s in (self.market or {}).get("stocks") or [] if s.get("stockId"))))

    def _plugin_spending(self, since_ms):
        """The companion plugin bought an upgrade or claimed a reward since `since_ms`, or is doing so now
        (plugin.json state.purchase / state.claim)."""
        st = self.plugin.get("state") if isinstance(self.plugin, dict) and self.plugin_fresh() else None
        if not isinstance(st, dict):
            return False                          # no live plugin: nothing it could be buying
        pu, cl = st.get("purchase") or {}, st.get("claim") or {}
        if not isinstance(pu, dict) or not isinstance(cl, dict):
            return False
        if pu.get("active") or cl.get("active") or num(pu.get("lastPurchaseAtMs")) >= since_ms:
            return True
        return any(isinstance(b, dict) and num(b.get("t")) >= since_ms for b in pu.get("recent") or [])

    def _fill_ok(self, r, fill):
        """A close's fill implied by the cash change is plausible: within the snapshot prices, widened by half
        the price impact and 3 x the stock's 1 s volatility (at most 40 %)."""
        lo, hi = min(r["prev"], r["after"]), max(r["prev"], r["after"])
        vol = self.brain.volatility(r["stock"])
        tol = min(0.4, max(0.02, self.impact.move(r["stock"]) / 2 + 3 * (vol if vol is not None else 0.13)))
        return lo * (1 - tol) <= fill <= hi * (1 + tol)

    def track_fills(self):
        """Fills from the change between two complete snapshots: entries from shares and average price, exit
        proceeds from cash. A fresh open can show the game's prediction first; a later change on that side (new
        average with the same shares, or fewer shares with no exit on its way) is the server's real fill: it
        corrects the open instead of being a trade. Cash that moved for other reasons (upgrades, unlocks,
        rewards, dividends, income) is kept out of the trades and out of the circuit breaker."""
        pl = self.player()
        if not pl or "cash" not in pl or not isinstance(pl.get("positions"), list):
            return                                # keep comparing against the last complete snapshot
        cash, pos, P = self.cash(), self.positions(), self.prices()
        marks = self._buy_marks()
        w = num((self.market or {}).get("writtenAtMs"), now_ms())
        prev, self.snap = self.snap, {"cash": cash, "pos": pos, "P": P, "marks": marks, "w": w}
        if not prev:
            return
        t = now_ms()
        opens, closes, fixes = [], [], []
        for s in set(pos) | set(prev["pos"]):
            a, b = prev["pos"].get(s, {}), pos.get(s, {})
            px = P.get(s) or prev["P"].get(s) or 0.0
            p0 = prev["P"].get(s) or px
            for side, k_n, k_avg in (("long", "owned", "avgBuy"), ("short", "shorted", "avgShort")):
                n0, n1 = a.get(k_n, 0.0), b.get(k_n, 0.0)
                a0, a1 = a.get(k_avg, 0.0), b.get(k_avg, 0.0)
                d = n1 - n0
                same = abs(d) <= 1e-9 * max(1.0, n0)
                if same and not (n1 > 1e-9 and a0 > 0 and a1 > 0 and abs(a1 / a0 - 1) > 1e-5):
                    continue
                if same or d < 0:
                    r = self._open_to_fix(s, side, t)
                    if r is not None:
                        fixes.append(self._fix_open(r, n0, a0, n1, a1, t))
                        continue
                    if same:
                        continue                  # an average that moved without a fresh open: nothing to book
                rec = {"t": t, "stock": s, "side": side, "kind": "open" if d > 0 else "close", "shares": abs(d),
                       "heldBefore": n0, "avgBefore": a0, "avgAfter": a1,
                       "prev": p0, "after": px, "cashBefore": prev["cash"], "x": None}
                if d > 0:
                    fill = (n1 * a1 - n0 * a0) / d
                    if not (fill > 0 and math.isfinite(fill)):
                        fill = px
                    rec.update(fill=fill, cash=-d * fill, src="avg", settle=t + OPEN_SETTLE_MS)   # buys and shorts
                    opens.append(rec)                                                             # both pay cash
                else:
                    mid = (p0 + px) / 2 if p0 > 0 else px          # the game fills at the midpoint of its own impact
                    exp = -d * mid if side == "long" else -d * (2 * a0 - mid)
                    rec.update(fill=mid, cash=exp, src="estimate")
                    closes.append(rec)
        fixed = sum(f["cash"] for f in fixes)
        spend = marks != prev.get("marks") or self._plugin_spending(num(prev.get("w"), t) - 2000)
        if not opens and not closes:
            quiet = not fixes and not self.pending and t - self.last_result_ms > 3000
            flow = (cash - prev["cash"]) - fixed
            why = "spending" if spend else self._dividend_why(flow, prev) if not fixes else "unexplained"
            if why == "unexplained" and quiet:
                why = "income or rewards" if flow > 0 else "spending"
            self._external(flow, spend or quiet or why == "dividends", why)
            return
        resid = (cash - prev["cash"]) - fixed - sum(r["cash"] for r in opens + closes)
        wsum = sum(abs(r["cash"]) for r in closes)
        # one cash change for several exits: the total is exact, each exit's share of it is an estimate
        # ("split": shown as estimated and kept out of the impact model)
        src = "split" if len(closes) > 1 else "cash"
        used = 0.0
        for r in closes:
            if wsum <= 0:
                break
            share = resid * abs(r["cash"]) / wsum
            if spend:
                r["src"] = "mixed"        # spending in the same snapshot: the exit keeps its expected proceeds
                continue
            attr = r["cash"] + share
            n, avg = r["shares"], r["avgBefore"]
            fill = attr / n if r["side"] == "long" else 2 * avg - attr / n
            if self._fill_ok(r, fill):
                r.update(fill=fill, cash=attr, src=src)
                used += share
            # else: cash also moved for another reason (or the fill is that far off): keep the price estimate
        if spend:
            self._external(resid - used, True, "spending")
        for r in opens + closes:
            self.on_fill(r)

    def _open_to_fix(self, s, side, t):
        """The open that a change on (s, side) corrects: the newest fill on that side if it is an open within
        REPRICE_WINDOW_MS and no exit is on its way (none pending or given up on, the bot isn't exiting it)."""
        if any(p["stock"] == s and p["action"] not in OPEN_ACTIONS for p in self.pending):
            return None
        if any(p["stock"] == s and p["action"] not in OPEN_ACTIONS and t - p["sentMs"] < 2 * RUN_TIMEOUT_MS
               for p in self.expired):
            return None
        mg = self.managed.get(s)
        if mg and mg.get("side") == side and mg.get("state") == "exiting":
            return None
        for r in reversed(self.recent_fills):
            if r["stock"] != s or r["side"] != side:
                continue
            if r["kind"] != "open" or t - r["t"] > REPRICE_WINDOW_MS or r.get("reverted"):
                return None
            return r
        return None

    def _fix_open(self, r, n0, a0, n1, a1, t):
        """Correct an open to what the server booked: (n1, a1) is the position now, (n0, a0) what the previous
        snapshot showed. Returns the cash this change explains."""
        hb, ab = r["heldBefore"], r["avgBefore"]
        old_fill, old_n = r["fill"], r["shares"]
        r.setdefault("predictedFill", old_fill)
        r.setdefault("predictedShares", old_n)
        shares = n1 - hb
        if shares <= 1e-9 * max(1.0, n1):
            r.update(shares=0.0, cash=0.0, reverted=True)          # the server undid the open
            fill = old_fill
        else:
            fill = (n1 * a1 - hb * ab) / shares
            if not (fill > 0 and math.isfinite(fill)):
                fill = a1 or old_fill
            r.update(shares=shares, fill=fill, cash=-shares * fill, src="avg", repriced=True)
            if r.get("cashBefore", 0) > 0:
                r["x"] = min(1.0, shares * fill / r["cashBefore"])
        r["fixedMs"] = t
        r["settle"] = max(num(r.get("settle")), t + 1500)          # another correction may follow
        self._append("fills.jsonl", {"t": t, "stock": r["stock"], "side": r["side"], "kind": "fix",
                                     "shares": round(r["shares"], 6), "fill": round(fill, 6),
                                     "predictedFill": round(r["predictedFill"], 6),
                                     "predictedShares": round(r["predictedShares"], 6), "reverted": bool(r.get("reverted")),
                                     "bot": bool(r.get("bot"))})
        mg = self.managed.get(r["stock"])
        if r.get("bot") and mg and mg.get("side") == r["side"] and mg.get("state") in ("opening", "open"):
            if r.get("reverted"):
                mg["reverted"] = True
                self.note(f"the server undid the {mg['kind']} {mg['side']} entry on {r['stock']}", "warn")
            else:
                mg["fillEntry"] = fill
                if mg["state"] == "open":
                    self._set_entry(r["stock"], mg, a1, n1)
        return {"stock": r["stock"], "side": r["side"], "cash": -(n1 * a1 - n0 * a0)}

    def _set_entry(self, s, mg, avg, held):
        """Re-anchor an open position on the price the server really booked: its stop, target, P/L and the
        breaker all measure from the entry."""
        old = num(mg.get("entry"))
        mg.update(entry=avg, shares=held, cost=held * avg)
        if mg.get("armAtEntry") or (old > 0 and abs(num(mg.get("armRef")) - old) <= 1e-9 * old):
            mg["armRef"] = avg
        mg.pop("fillChecked", None)               # the rebound leg checks its fill again
        mg.pop("stopSince", None)
        if old > 0 and abs(avg / old - 1) > 0.002:
            mg["repriced"] = {"from": old, "to": avg, "t": now_ms()}
            self.note(f"server re-priced the {mg['kind']} {mg['side']} on {s}: entry {old:.4g} -> {avg:.4g} "
                      f"(stop and target follow)", "warn")

    def _div_base(self, pos, P):
        """One dividend payout at multiplier 1 (decompiled ApplyDividendPayout: price x rate x shares owned, shorts
        pay it), per stock."""
        out = {}
        for s, st in self.stock_info().items():
            p = pos.get(s) or {}
            n = p.get("owned", 0.0) - p.get("shorted", 0.0)
            rate, px = num(st.get("dividendRate")), P.get(s, 0.0)
            if n and rate > 0 and px > 0:
                out[s] = n * px * rate
        return out

    def _dividend_why(self, flow, prev):
        base = sum(self._div_base(prev["pos"], prev["P"]).values())
        if base and flow and 0.8 <= flow / base <= 50:          # the DividendGain upgrades multiply it
            return "dividends"
        return "unexplained"

    def _external(self, flow, apply, why):
        """Cash that moved without a trade. Applied flows shift the breaker's baseline and peak alike: spending
        isn't a trading loss, and the limit then applies to the money the bot really has."""
        nw = abs(self.net_worth())
        if abs(flow) <= max(0.01, 1e-7 * nw):
            return
        e = self.ext
        e["total"] += flow
        e["n"] += 1
        e["last"] = {"t": now_ms(), "cash": round(flow, 4), "why": why, "applied": bool(apply)}
        r = self.risk
        on = bool(apply and self.cfg["armed"] and r.get("armedAt") and r.get("armNw"))
        if on:
            r["armNw"] += flow
            r["peak"] = num(r.get("peak")) + flow
            r["external"] = r.get("external", 0.0) + flow
        if why == "dividends":
            self._got_dividends(flow)
        if abs(flow) >= 0.001 * max(1.0, nw) or why == "dividends":
            self._append("cashflows.jsonl", {"t": now_ms(), "cash": round(flow, 4), "why": why, "applied": on,
                                             "cashNow": self.cash()})
        if abs(flow) >= 0.01 * max(1.0, nw) and why != "dividends":
            self.note(f"cash {flow:+,.2f} without a trade ({why}): kept out of the bot's P/L"
                      f"{' and the circuit breaker' if on else ''}", "info")

    def on_fill(self, r):
        s, t = r["stock"], r["t"]
        mg = self.managed.get(s)
        mine = mg is not None and mg.get("side") == r["side"]
        r["bot"] = False                              # counted in the bot's totals
        if r["kind"] == "open":
            r.setdefault("settle", t + OPEN_SETTLE_MS)
            if r["cashBefore"] > 0:
                r["x"] = min(1.0, r["shares"] * r["fill"] / r["cashBefore"])
            if mine and mg.get("state") == "opening":
                r["bot"] = True
                mg["fillEntry"] = r["fill"]
                if r["heldBefore"] <= 1e-9:
                    mg.update(R=1.0, Rknown=True)              # a fresh position: a full exit moves the price by `move`
        elif mine and mg.get("state") != "opening":
            r["bot"] = True
            R = min(1.0, max(0.0, num(mg.get("R"), 1.0))) if mg.get("R") is not None else 1.0
            q = min(1.0, r["shares"] / r["heldBefore"]) if r["heldBefore"] > 0 else 1.0
            if mg.get("Rknown", not mg.get("adopted")):
                r["x"] = R * q
            mg["R"] = max(0.0, R * (1 - q))
            self.realize(s, mg, r)
        if r["kind"] == "close" and "pnl" not in r and r["avgBefore"] > 0:
            r["pnl"] = r["cash"] - r["shares"] * r["avgBefore"]     # the player's own position: shown, not counted
            r["pnlPct"] = r["pnl"] / (r["shares"] * r["avgBefore"]) * 100
        self.recent_fills.append(r)
        while self.recent_fills and t - self.recent_fills[0]["t"] > 120000:
            self.recent_fills.popleft()
        self._append("fills.jsonl", {k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k != "linked"})

    def realize(self, s, mg, r):
        n, avg, attr, fill, src = r["shares"], r["avgBefore"], r["cash"], r["fill"], r["src"]
        basis = n * avg
        pnl = attr - basis
        r.update(pnl=pnl, pnlPct=(pnl / basis * 100) if basis > 0 else None, botKind=mg["kind"])
        mg["realizedMoney"] = mg.get("realizedMoney", 0.0) + pnl
        mg["exitShares"] = mg.get("exitShares", 0.0) + n
        mg["exitValue"] = mg.get("exitValue", 0.0) + n * fill
        mg["fillExit"] = mg["exitValue"] / mg["exitShares"] if mg["exitShares"] > 0 else fill
        mg["closedCost"] = mg.get("closedCost", 0.0) + basis
        mg["shares"] = max(0.0, num(mg.get("shares")) - n)
        for tot in (self.realized[mg["kind"]], self.realized_stock[s]):
            tot["money"] += pnl
            tot["cost"] += basis
            tot["fills"] += 1
        ref = mg.get("armRef")
        if mg.get("armAtEntry") and avg > 0:
            ref = avg                         # opened since arming: its real average, a re-priced entry included
        if self.risk.get("armedAt") and ref:
            self.risk["realized"] = self.risk.get("realized", 0.0) + attr - n * (ref if mg["side"] == "long" else 2 * avg - ref)
        self._append("closes.jsonl", {"t": now_ms(), "stock": s, "kind": mg["kind"], "side": mg["side"], "shares": n,
                                      "avg": avg, "fill": fill, "cash": attr, "pnl": pnl, "pnlPct": r["pnlPct"], "src": src,
                                      "why": mg.get("exitReason", "")})
        if mg["kind"] != "park":
            if self.close_hist_since is None:
                self._close_hist_load()               # this close is in the file already
            else:
                self.close_hist.append((now_ms(), s, pnl))
        est = {"cash": "", "mixed": ", estimated: spending in the same second"}.get(src, ", estimated")
        self.note(f"{'💰' if pnl >= 0 else '🔻'} {mg['kind']} {mg['side']} {s}: {pnl:+,.2f} realized "
                  f"({n:.4g} @ {fill:.4g} vs {avg:.4g}{est})", "ok" if pnl >= 0 else "warn")

    # ---- trade records: written to trades.jsonl once their fill is known (or after 15 s)
    def _record_trade(self, e, need_fill=False, from_ms=None):
        if need_fill:
            e["_need"] = True
            e["_from"] = from_ms if from_ms is not None else now_ms() - 5000
            e["_until"] = now_ms() + 15000
        self.trades.appendleft(e)
        self.unflushed.append(e)

    def link_fills(self):
        t = now_ms()
        for e in self.unflushed:
            if not e.get("_need") or e.get("_linked"):
                continue
            kind, side = trade_kind(e.get("action"))
            best = None
            for r in self.recent_fills:
                if r.get("linked") or r["stock"] != e.get("stock") or r["kind"] != kind or (side and r["side"] != side):
                    continue
                if e["_from"] <= r["t"] <= e["_until"] and (best is None or r["t"] < best["t"]):
                    best = r
            if best and (best["kind"] != "open" or t >= num(best.get("settle")) or self._exited(best)):
                self._apply_fill(e, best)
        while self.unflushed:
            e = self.unflushed[0]
            if e.get("_need") and not e.get("_linked") and t < e["_until"]:
                break
            self.unflushed.popleft()
            self._flush_trade(e)

    def _exited(self, r):
        """A close followed this open on its side (no more corrections can come)."""
        return any(x is not r and x["kind"] == "close" and x["stock"] == r["stock"] and x["side"] == r["side"]
                   and x["t"] >= r["t"] for x in self.recent_fills)

    def _apply_fill(self, e, r):
        r["linked"] = True
        e["_linked"] = True
        e["shares"] = r["shares"]
        e["fillSrc"] = r["src"]
        if r["kind"] == "open":
            e["fillEntry"] = r["fill"]
            if r.get("repriced") or r.get("reverted"):
                e["predictedFill"] = round(r["predictedFill"], 6)
                e["predictedShares"] = round(r["predictedShares"], 6)
                e["repriced"] = bool(r.get("repriced"))
            if r.get("reverted"):
                e["reverted"] = True
                return
            if r.get("x") is not None:
                e["cashUsedPct"] = round(r["x"] * 100, 3)
        else:
            e["fillExit"] = r["fill"]
            e["fillEntry"] = r["avgBefore"]
            if r.get("pnl") is not None:
                e["pnl"] = round(r["pnl"], 4)
                e["pnlPct"] = round(r["pnlPct"], 4) if r.get("pnlPct") is not None else None
            e["counted"] = bool(r.get("bot"))
        base = num(e.get("sentPrice"))
        if base > 0 and r["fill"] > 0:
            sgn = 1 if (r["kind"] == "open") == (r["side"] == "long") else -1     # buy/cover: higher is worse
            cost = (r["fill"] / base - 1) * sgn
            e["slipPct"] = round(cost * 100, 4)
            if r.get("x") is not None:
                e["impactX"] = round(r["x"], 4)
            if e.get("source") == "bot" and r.get("x") is not None and r["src"] in ("avg", "cash"):
                self.impact.add(r["stock"], {"t": r["t"], "kind": r["kind"], "side": r["side"], "x": round(r["x"], 5),
                                             "cost": round(cost, 6), "base": base, "fill": round(r["fill"], 6),
                                             "prev": r["prev"], "after": r["after"]})
            if e.get("source") == "bot" and r["kind"] == "open":
                self.fills.append(round(cost * 100, 3))
                BG_WRITER.put(self.data / "fills.json", json.dumps(list(self.fills)))   # off the trading thread

    def _flush_trade(self, e):
        for k in [k for k in e if k.startswith("_")]:
            del e[k]
        self._append("trades.jsonl", e)

    def flush_trades(self):
        while self.unflushed:
            self._flush_trade(self.unflushed.popleft())

    # ---- commands
    def _rate_take(self, cost):
        t = time.monotonic()
        self.tokens = min(RATE_BURST, self.tokens + (t - self.tokens_t) * RATE_PER_SEC)
        self.tokens_t = t
        if now_ms() < self.rate_block_until or self.tokens < cost:
            return False
        self.tokens -= cost
        return True

    def _cost(self, stock, action):
        p = self.positions().get(stock, {})
        if action in OPEN_ACTIONS:
            return 2 if (p.get("shorted", 0) if action.startswith("buy") else p.get("owned", 0)) > 0 else 1
        if action.startswith("close"):
            return max(1, int(p.get("owned", 0) > 0) + int(p.get("shorted", 0) > 0))
        return 1

    def send(self, stock, action, percent=None, source="bot", reason="", phase=None, event=None, leg=None, mg=None):
        if action not in ACTIONS:
            return False, "unknown action"
        if stock not in self.stock_info():
            return False, f"unknown stock {stock!r}"
        f = self.cmds / stock / f"{action}.json"
        if not f.exists():
            msg = f"can't {action} {stock}: command file missing (mod support off or game not running?)"
            self.note_once(f"missing-{stock}-{action}", msg, "error")
            return False, msg
        if not self._rate_take(self._cost(stock, action)):
            return False, "rate limit: try again in a second"
        body = {"execute": True}
        if action.endswith("percent"):
            body["percent"] = int(max(1, min(100, round(num(percent, 25) or 25))))
        err = None
        for i in range(5):
            try:
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(body))
                err = None
                break
            except PermissionError as e:      # the game is reading it: try again in a moment
                err = e
                time.sleep(0.02 * (i + 1))
            except OSError as e:
                err = e
                break
        if err is not None:
            self.tokens = min(RATE_BURST, self.tokens + self._cost(stock, action))   # nothing went out
            self.note_once(f"write-{stock}-{action}", f"couldn't write {f.name} for {stock}: {err}", "error")
            return False, "write failed"
        price = self.prices().get(stock)
        self.pending.append({"stock": stock, "action": action, "percent": body.get("percent", 100), "sentMs": now_ms(),
                             "price": price, "source": source, "reason": reason, "phase": phase, "event": event,
                             "leg": leg, "key": None, "file": str(f), "mg": mg})
        pct = f" {body['percent']}%" if "percent" in body else ""
        at = f" @ {price:.4g}" if price else ""
        self.note(f"→ {action}{pct} {stock}{at} ({source}: {reason})", "trade")
        return True, "sent"

    def pending_open(self, side):
        return any(p["action"].startswith("buy" if side == "buy" else "short") for p in self.pending)

    def pending_for(self, stock):
        return any(p["stock"] == stock for p in self.pending)

    def can_open(self, stock, side):
        t = now_ms()
        return t >= self.side_block.get(side, 0) and t >= self.stock_block.get((stock, side), 0) and t >= self.rate_block_until

    def exit_ready(self, stock):
        b = self.exit_backoff.get(stock)
        return not b or now_ms() >= b["until"]

    def side_ready_by(self, side, server_ms):
        """True if an open on `side` can happen by server time `server_ms` (cooldown and our own backoff)."""
        pl = self.player()
        nxt = num(pl.get("nextBuyAtMs" if side == "buy" else "nextShortAtMs"))
        block = self.side_block.get(side, 0) - now_ms() + self.server_now()
        return max(nxt, block) <= server_ms

    def _match(self, key, r):
        for coll in (self.pending, self.expired):
            for p in coll:
                if p.get("key") == key:
                    return p
        recv = num(r.get("receivedAtMs"))
        for coll in (self.pending, self.expired):
            cands = [p for p in coll if not p.get("key") and p["stock"] == r.get("stockId")
                     and p["action"] == r.get("action") and recv >= p["sentMs"] - 1500]
            if cands:
                p = min(cands, key=lambda x: x["sentMs"])
                p["key"], p["ackMs"] = key, now_ms()
                return p
        return None

    def process_results(self):
        results = (self.market or {}).get("commandResults") or []
        keys = set()
        for r in results:
            if not isinstance(r, dict):
                continue
            key = f"{r.get('run')}:{r.get('receivedAtMs')}"
            keys.add(key)
            status = r.get("status")
            prev = self.seen.get(key)
            if prev == status:
                continue
            if prev is None and num(r.get("receivedAtMs")) < self.started_ms - 2000:
                self.seen[key] = status       # from before the agent started
                continue
            p = self._match(key, r)
            if status == "running":
                self.seen[key] = status
                if p:
                    p["runningMs"] = now_ms()
                continue
            late = False
            if p is not None and any(x is p for x in self.pending):
                self.pending = [x for x in self.pending if x is not p]
            elif p is not None:
                late = True
                self.expired = collections.deque((x for x in self.expired if x is not p), maxlen=50)
            ok = status == "done"
            self.last_result_ms = now_ms()
            stock = r.get("stockId")
            mgk = ((p or {}).get("mg") or self.managed.get(stock) or {}).get("kind") if p and p.get("source") == "bot" else None
            entry = {"t": r.get("finishedAtMs") or now_ms(), "stock": stock, "action": r.get("action"),
                     "percent": r.get("percent") or (p or {}).get("percent"), "status": status or "?",
                     "reason": r.get("reason") or "", "source": (p or {}).get("source", "other"),
                     "why": (p or {}).get("reason", ""), "price": self.prices().get(stock),
                     "sentPrice": (p or {}).get("price"), "kind": mgk,
                     "ms": (int(num(r.get("finishedAtMs")) - p["sentMs"]) if p and r.get("finishedAtMs") else None)}
            mgp = (p or {}).get("mg") or {}
            if r.get("action") in OPEN_ACTIONS and mgp.get("pct") is not None:
                entry["pctWanted"] = mgp["pct"]            # % of cash meant; `percent` is what was sent for it
                if mgp.get("planRatio") is not None:
                    entry["planRatio"] = mgp["planRatio"]
            try:
                self.after_result(p, r, ok, late=late)
            except Exception:
                log.exception("after_result failed")
            self.seen[key] = status
            self._record_trade(entry, need_fill=ok and trade_kind(r.get("action"))[0] is not None,
                               from_ms=(p["sentMs"] - 500) if p else num(r.get("receivedAtMs"), now_ms()) - 1000)
            self.note(f"{'✓' if ok else '✗'} {entry['action']} {entry['stock']} {entry['status']}"
                      f"{(' (' + entry['reason'] + ')') if entry['reason'] else ''}{' (late)' if late else ''}",
                      "ok" if ok else "warn")
        for k in [k for k in self.seen if k not in keys]:
            del self.seen[k]

    def _defuse(self, p):
        """The game never picked the command up: make sure it can't fire late."""
        try:
            f = Path(p["file"])
            body = json.loads(f.read_text(encoding="utf-8") or "{}")
            if isinstance(body, dict) and body.get("execute"):
                off = '{\n  "execute": false\n}\n' if not p["action"].endswith("percent") else \
                    '{\n  "execute": false,\n  "percent": %d\n}\n' % p["percent"]
                f.write_text(off, encoding="utf-8")
        except (OSError, ValueError):
            pass

    def expire_pending(self):
        t = now_ms()
        for p in list(self.pending):
            if t - p["sentMs"] <= (RUN_TIMEOUT_MS if p.get("key") else ACK_TIMEOUT_MS):
                continue
            self.pending = [x for x in self.pending if x is not p]
            p["expiredMs"] = t
            self.expired.append(p)
            if not p.get("key"):
                self._defuse(p)
            what = "still running" if p.get("key") else "never picked up"
            self.note(f"no result for {p['action']} {p['stock']} after {(t - p['sentMs']) / 1000:.0f}s ({what})", "warn")
            entry = {"t": t, "stock": p["stock"], "action": p["action"], "percent": p["percent"], "status": "expired",
                     "reason": what, "source": p["source"], "why": p["reason"], "price": self.prices().get(p["stock"]),
                     "sentPrice": p.get("price")}
            self._record_trade(entry)
            self.after_result(p, {"stockId": p["stock"], "action": p["action"], "status": "expired", "reason": "expired"},
                              False, expired=True)
        while self.expired and t - self.expired[0].get("expiredMs", t) > 300000:
            self.expired.popleft()

    def after_result(self, p, r, ok, late=False, expired=False):
        stock, action = r.get("stockId"), r.get("action", "")
        reason = r.get("reason") or ""
        is_open = action in OPEN_ACTIONS
        if p and is_open and not ok:
            self._open_failed(p, stock, side_of(action), reason, r)
        elif p and not is_open:
            if ok:
                self.exit_backoff.pop(stock, None)
            else:
                self._exit_failed(stock, reason, r)
        if not p or p.get("source") != "bot":
            return
        mg = self.managed.get(stock)
        if late and ok and is_open and p.get("mg") and not mg:
            rec = dict(p["mg"], state="opening", deadline=now_ms() + OPEN_DEADLINE_MS)
            self.managed[stock] = rec                     # a late fill for an entry we had given up on
            self.note(f"late fill for {action} {stock}: managing it again", "warn")
            return
        if not mg:
            return
        if p.get("phase") == "entry" and mg["state"] == "opening":
            if ok or expired:
                mg["deadline"] = now_ms() + OPEN_DEADLINE_MS   # confirm_opens decides by the shares held
            else:
                del self.managed[stock]
        elif p.get("phase") == "exit" and mg["state"] == "exiting":
            if ok:
                mg["exitDoneMs"] = now_ms()               # removed once the position shows zero shares
            else:
                mg["state"] = "open"
                mg.pop("chunk", None)                     # the strategy decides again (after the backoff)

    def _open_failed(self, p, stock, side, reason, r):
        t = now_ms()
        retry = num(r.get("retryAtMs"))
        if reason == "cooldown":       # retryAtMs is server time here
            until = retry - self.server_now() + t if retry else t + 1000
            self.side_block[side] = max(self.side_block.get(side, 0), min(until, t + 300000))
        elif reason == "rate-limited":  # ...and local time here
            self.rate_block_until = max(self.rate_block_until, min(retry or t + 2000, t + 60000))
            self.tokens = 0.0
        elif reason in TRANSIENT:
            self.side_block[side] = max(self.side_block.get(side, 0), t + (3000 if reason == "expired" else 1000))
        elif reason == "no-volume":
            self.stock_block[(stock, side)] = t + 60000
        elif reason == "locked":
            self.stock_block[(stock, side)] = t + 600000
        else:
            self.stock_block[(stock, side)] = t + 10000
        ev = self.events.get(p.get("event")) if p.get("event") else None
        if ev and reason in TRANSIENT:      # quick transient failures don't use up an event's tries
            k = "rtries" if p.get("leg") == "rebound" else "tries"
            ev[k] = max(0, ev.get(k, 0) - 1)

    def _exit_failed(self, stock, reason, r):
        t = now_ms()
        b = self.exit_backoff.get(stock) or {"n": 0, "until": 0}
        b["n"] += 1
        delay = min(30000, 2000 * 2 ** (b["n"] - 1))
        retry = num(r.get("retryAtMs"))
        if reason == "rate-limited" and retry:
            delay = max(delay, min(60000, retry - t))
            self.tokens = 0.0
        b["until"] = t + delay
        self.exit_backoff[stock] = b

    # ---- shared helpers for strategies
    def confirm_opens(self, pos, prices):
        """Track bot positions against the real ones: fills, closes, partial exits, entries that never filled."""
        t = now_ms()
        for s, mg in list(self.managed.items()):
            p = pos.get(s, {})
            long = mg["side"] == "long"
            held = p.get("owned", 0.0) if long else p.get("shorted", 0.0)
            st = mg["state"]
            if st == "opening":
                if held > num(mg.get("heldBefore")) + 1e-9:
                    fill = (p.get("avgBuy") if long else p.get("avgShort")) or mg.get("fillEntry") or prices.get(s)
                    mg.update(state="open", entry=fill, entryT=self.server_now(), best=prices.get(s, fill),
                              filledMs=t, shares=held, cost=held * fill, armRef=fill, armAtEntry=True)
                    mg.setdefault("fillEntry", fill)
                    if mg.get("R") is None:
                        mg.update(R=1.0, Rknown=num(mg.get("heldBefore")) <= 1e-9)
                    mg.pop("deadline", None)
                elif not self.pending_for(s) and (mg.get("reverted") or
                                                  t > num(mg.get("deadline"), num(mg.get("sentMs")) + OPEN_DEADLINE_MS)):
                    del self.managed[s]
                    self.note(f"{mg['kind']} entry on {s} {'was undone by the server' if mg.get('reverted') else 'never filled'}"
                              f"; dropped", "warn")
            elif mg.get("reverted") and held <= num(mg.get("heldBefore")) + 1e-9 and not self.pending_for(s):
                del self.managed[s]                    # confirmed from the prediction, then undone by the server
                self.note(f"{mg['kind']} entry on {s} was undone by the server; dropped", "warn")
            elif held <= 1e-9:
                if not self.pending_for(s) and (st == "exiting" or t - num(mg.get("sentMs")) > 10000):
                    self._finish_close(s, mg, by_bot=st == "exiting")
                    del self.managed[s]
            else:
                ch = mg.get("chunk")
                wait = 15000 if ch and ch["i"] < ch["n"] else 1000 if mg.get("partial") else 3000   # chunks pause
                if st == "exiting" and mg.get("exitDoneMs") and t - mg["exitDoneMs"] > wait and not self.pending_for(s):
                    mg["state"] = "open"
                    mg.pop("exitDoneMs", None)
                    mg.pop("chunk", None)
                    if mg.pop("partial", None):
                        self.note(f"sold part of the {mg['kind']} on {s}: {held:.4g} shares stay", "info")
                    else:
                        self.note(f"exit on {s} left {held:.4g} shares; still managing them", "warn")
                avg = p.get("avgBuy") if long else p.get("avgShort")
                if (st == "open" and avg and mg.get("entry") and not mg.get("adopted") and not self.pending_for(s)
                        and not mg.get("exitShares") and abs(avg / mg["entry"] - 1) > 0.002
                        and t - num(mg.get("filledMs")) < REPRICE_WINDOW_MS):
                    self._set_entry(s, mg, avg, held)  # a re-price the fill tracking didn't see (safety net)
                if not mg.get("shares") or abs(num(mg.get("shares")) - held) > 1e-6:
                    mg["shares"] = held

    def _finish_close(self, s, mg, by_bot):
        money = mg.get("realizedMoney")
        entry, shares = num(mg.get("entry")), num(mg.get("shares"))
        cost = num(mg.get("closedCost")) or num(mg.get("cost")) or entry * shares
        if money is None and entry and shares:      # no cash change seen (closed while the agent was off)
            px = self.prices().get(s) or entry
            attr = shares * px if mg["side"] == "long" else shares * (2 * entry - px)
            self.realize(s, mg, {"shares": shares, "avgBefore": entry, "cash": attr, "fill": px, "src": "estimate"})
            money = mg.get("realizedMoney")
            cost = num(mg.get("closedCost")) or cost
        ret = money / cost if money is not None and cost > 0 else None
        for stt in (self.realized[mg["kind"]], self.realized_stock[s]):
            if ret is not None:
                stt["n"] += 1
                stt["sum"] += ret
                stt["win"] += ret > 0
        if ret is not None:
            self.note(f"closed {mg['kind']} {mg['side']} {s}: {money:+,.2f} ({ret * 100:+.2f}%), entry "
                      f"{num(mg.get('fillEntry') or entry):.4g} exit {num(mg.get('fillExit')):.4g}", "ok" if money >= 0 else "warn")
        if mg["kind"] == "swing" and ret is not None:
            seen = self._seen_return(s, mg)
            # a big real loss counts even when the seen prices made money: on $STEAM after the 3rd IPO the fills
            # ran 17-30 % off the seen price and three such "fill" losses in 6 min cost ~5e12 (2026-11-15 23:30)
            fill_cap = num(self.cfg.get("swingPauseFillLossPct"), 10)
            big = fill_cap > 0 and ret * 100 <= -fill_cap
            lost = ret < 0 and (seen is None or seen < 0 or big)
            if ret < 0 and not lost:
                self.note(f"{s}: at the prices the bot saw this trade made {seen * 100:+.1f}% after costs; the loss "
                          f"came from the fills, so it doesn't count toward the loss pause", "info")
            self.losses[s] = self.losses[s] + 1 if lost else 0
            if self.losses[s] >= self.cfg["swingPauseLosses"] > 0:
                self.pause_until[s] = now_ms() + 15 * 60000
                self.losses[s] = 0
                self.note(f"pausing swing trades on {s} for 15 min after {self.cfg['swingPauseLosses']} losses", "warn")
        if not by_bot:
            self.note(f"{mg['kind']} {mg['side']} on {s} was closed outside the bot", "warn")

    def _seen_return(self, s, mg):
        """Return of a closed position at the prices the bot saw when it sent the entry and the exit, after the
        game's fixed costs (entry and exit impact); None when either price is unknown."""
        a, b = num(mg.get("signalPrice")), num(mg.get("exitSeen"))
        if a <= 0 or b <= 0:
            return None
        gross = (b / a - 1) if mg["side"] == "long" else (1 - b / a)
        return gross - self.impact.entry_cost(s, num(mg.get("pct"), 50) / 100) - self.impact.exit_cost(s, 1.0)

    def short_oversize(self, stock):
        """How much more cash than the percent sent this stock's recent shorts used (cash used / percent): the
        largest of the last 4, between 1 and 2.5; 1.5 when it has none."""
        rs = []
        for t in self.trades:                        # newest first
            if (t.get("stock") == stock and t.get("action") == "shortpercent" and t.get("status") == "done"
                    and t.get("cashUsedPct") is not None and 0 < num(t.get("percent")) < 95):
                rs.append(num(t["cashUsedPct"]) / num(t["percent"]))
                if len(rs) >= 4:
                    break
        return min(2.5, max(1.0, max(rs))) if rs else 1.5

    def size_entry(self, stock, kind, percent, edge):
        """Percent of cash for an entry: the wanted size, capped by position limits, by the impact model
        (maximise expected money after entry + exit impact) and by the shares the stock has left."""
        cash, nw = self.cash(), self.net_worth()
        if cash <= 0:
            return 0, "no cash"
        pct, why = min(100.0, num(percent)), ""
        if kind in ("swing", "park") and nw > 0:
            cap = self.cfg["maxPositionPct"]
            if kind == "park" and self.noisy(stock):
                cap = min(cap, self.cfg["noisyCapPct"])
            pct = min(pct, cap / 100 * nw / cash * 100)
        if self.cfg["sizeByImpact"] and edge is not None:
            f = self.impact.best_fraction(stock, edge, pct / 100) * 100
            if f < pct - 0.5:
                why = f"impact {self.impact.move(stock) * 100:.1f}%/100% cash vs edge {edge * 100:.1f}%"
            pct = min(pct, f)
        st = self.stock_info().get(stock, {})
        avail, price = num(st.get("availableShares"), -1.0), self.prices().get(stock) or 0.0
        if avail >= 0 and price > 0:
            vcap = avail * price / cash * 100 * 0.98
            if vcap < pct:
                pct, why = vcap, f"volume cap ({avail:,.4g} shares left)"
                if (num(self.cfg.get("dustPctOfCash"), 0.1) <= pct or (kind == "harvest" and avail > 0)) and pct < 1:
                    # orders are whole percents, and the server trims an open to the free shares (and prices
                    # the impact of what it filled: MarketServerStub.ApplyBuyOrShortOpen), so 1 % buys them all;
                    # under 0.1 % of cash it is dust that would only use up the cooldown
                    pct = 1.0
        return int(math.floor(max(0.0, pct) + 1e-9)), why

    def open_position(self, stock, side, percent, kind, reason, edge=None, **extra):
        self.refusal = ""
        if self.risk.get("tripped"):
            self.refusal = "circuit breaker tripped"
            return False
        if stock in self.managed or self.pending_for(stock):
            return False                      # never overwrite another position / event's entry
        sideb = "buy" if side == "long" else "short"
        if not self.can_open(stock, sideb):
            return False
        if not self.tick_window_open():
            return False                      # late in the tick: it would fill at the next price; decide again then
        cash, nw = self.cash(), self.net_worth()
        if cash < max(1.0, 0.002 * nw):
            return False
        pct, why = self.size_entry(stock, kind, percent, edge)
        if pct < 1:
            self.refusal = why or "too small"
            self.note_once(f"size-{stock}-{kind}", f"no {kind} {side} on {stock}: {self.refusal}", "info", 300000)
            return False
        price = self.prices().get(stock)
        # the game sizes the order at its eased display price: scale the percent so the cash it uses is `pct`
        ratio = min(2.0, max(0.05, self.plan_ratio(stock, price)))
        send_pct = min(100.0, pct * ratio)
        p = self.positions().get(stock, {})
        rec = {"kind": kind, "side": side, "state": "opening", "entry": price, "entryT": self.server_now(),
               "best": price, "signalPrice": price, "sentMs": now_ms(), "deadline": now_ms() + OPEN_DEADLINE_MS,
               "heldBefore": p.get("owned", 0.0) if side == "long" else p.get("shorted", 0.0), "pct": pct,
               "pctSent": round(send_pct, 2), "planRatio": round(ratio, 4),
               "edge": edge, "moveAtEntry": self.impact.move(stock), **extra}
        action = "buypercent" if side == "long" else "shortpercent"
        sized = f" · sized {pct}% ({why})" if why and pct < num(percent) else ""
        plan = (f" · sent {max(1, round(send_pct))}% (the game sizes at {ratio * price:.4g})"
                if price and abs(ratio - 1) > 0.05 else "")
        ok, _ = self.send(stock, action, send_pct, reason=reason + sized + plan, phase="entry", event=extra.get("ev"),
                          leg=extra.get("leg"), mg=rec)
        if ok:
            self.managed[stock] = rec
            if side == "long" and kind != "park":
                self.last_buy_ms = now_ms()
        return ok

    def add_position(self, stock, kind, percent, reason, edge=None):
        """Buy more of an open bot long (a crash leg keeps buying while the price is still deep). The position
        re-anchors on the game's new average price once the add shows (confirm_opens' re-price net)."""
        self.refusal = ""
        mg = self.managed.get(stock)
        if not mg or mg["state"] != "open" or mg["side"] != "long" or self.pending_for(stock):
            return False
        if self.risk.get("tripped") and not mg.get("breakerExempt"):
            self.refusal = "circuit breaker tripped"
            return False
        if not self.can_open(stock, "buy") or not self.tick_window_open():
            return False
        cash, nw = self.cash(), self.net_worth()
        if cash < max(1.0, 0.002 * nw):
            self.refusal = "no cash"
            return False
        pct, why = self.size_entry(stock, kind, percent, edge)
        if pct < 1:
            self.refusal = why or "too small"
            return False
        price = self.prices().get(stock)
        ratio = min(2.0, max(0.05, self.plan_ratio(stock, price)))
        send_pct = min(100.0, pct * ratio)
        sized = f" · sized {pct}% ({why})" if why and pct < num(percent) else ""
        ok, _ = self.send(stock, "buypercent", send_pct, reason=reason + sized, phase="add", event=mg.get("ev"),
                          leg=mg.get("leg"))
        if ok:
            mg["filledMs"] = now_ms() + 5000      # opens the re-price window for the new average
            mg["adds"] = mg.get("adds", 0) + 1
        return ok

    def exit_chunks(self, stock, mg):
        """How many pieces to sell a position in: 1 unless its full exit would cost more than exitChunkAbovePct.
        Never on a noisy stock: the price moves more in the seconds between chunks than chunking could save (the
        game's total close impact is the same either way; chunks only win if the price recovers in between)."""
        lim, kmax = self.cfg["exitChunkAbovePct"], int(self.cfg["exitChunkMax"])
        if lim <= 0 or kmax < 2 or self.noisy(stock):
            return 1
        R = min(1.0, max(0.0, num(mg.get("R"), 1.0))) if mg.get("R") is not None else 1.0
        est = self.impact.exit_cost(stock, R) * 100
        if est <= lim:
            return 1
        return int(max(2, min(kmax, math.ceil(est / (lim / 2)))))

    def close_position(self, stock, reason, urgent=False, target=None):
        """Exit a bot position.  Non-urgent exits with a large estimated impact go out in chunks ~1 s apart
        (sellpercent/coverpercent, then sellmax/covermax); stops and event legs always go out in one piece.
        Target exits (default: the non-urgent ones) only go out early in a price tick; stops and forced exits
        go at any time."""
        mg = self.managed.get(stock)
        if not mg or mg["state"] != "open" or self.pending_for(stock) or not self.exit_ready(stock):
            return False
        if ((not urgent) if target is None else target) and not self.tick_window_open():
            return False
        long = mg["side"] == "long"
        k = 1 if urgent else self.exit_chunks(stock, mg)
        if k > 1:
            est = self.impact.exit_cost(stock, num(mg.get("R"), 1.0)) * 100
            ok, _ = self.send(stock, "sellpercent" if long else "coverpercent", int(round(100 / k)),
                              reason=f"{reason} · chunk 1/{k} (full exit ~{est:.1f}% impact)", phase="exit")
        else:
            ok, _ = self.send(stock, "sellmax" if long else "covermax", reason=reason, phase="exit")
        if ok:
            p = self.positions().get(stock, {})
            mg["state"] = "exiting"
            mg["exitReason"] = reason
            mg["exitSeen"] = self.prices().get(stock)
            mg.pop("exitDoneMs", None)
            if k > 1:
                mg["chunk"] = {"n": k, "i": 1, "held": p.get("owned", 0.0) if long else p.get("shorted", 0.0),
                               "p0": self.prices().get(stock), "mR": self.impact.move(stock) * num(mg.get("R"), 1.0)}
            else:
                mg.pop("chunk", None)
        return ok

    def run_chunks(self, pos):
        """Send the next chunk of a chunked exit once the previous one shows in the position."""
        t = now_ms()
        gap = self.cfg["exitChunkSec"] * 1000
        for s, mg in list(self.managed.items()):
            ch = mg.get("chunk")
            if not ch or mg["state"] != "exiting" or ch["i"] >= ch["n"] or not mg.get("exitDoneMs") or self.pending_for(s):
                continue
            long = mg["side"] == "long"
            p = pos.get(s, {})
            held = p.get("owned", 0.0) if long else p.get("shorted", 0.0)
            if held <= 1e-9 or held >= ch["held"] - 1e-9 * max(1.0, held):
                continue                       # gone (confirm_opens finishes it) or the last chunk isn't visible yet
            if t - mg["exitDoneMs"] < gap or not self.exit_ready(s) or not self.tick_window_open():
                continue
            i, k = ch["i"], ch["n"]
            # the price went against us by more than our own chunks explain plus the impact we're trying to save:
            # the recovery chunking bets on isn't coming, so the rest goes out at once
            px, p0, mR = self.prices().get(s), num(ch.get("p0")), num(ch.get("mR"))
            room = mR * i / k + mR / 2
            adverse = bool(px and p0 and (px < p0 * (1 - room) if long else px > p0 * (1 + room)))
            last = i + 1 >= k or adverse
            why = f"{mg.get('exitReason', 'exit')} · chunk {i + 1}/{k}" + (" · rest at once (price ran away)" if adverse and i + 1 < k else "")
            if last:
                ok, _ = self.send(s, "sellmax" if long else "covermax", reason=why, phase="exit")
            else:
                ok, _ = self.send(s, "sellpercent" if long else "coverpercent", int(round(100 / (k - i))), reason=why,
                                  phase="exit")
            if ok:
                ch.update(i=k if last else i + 1, held=held)
                mg.pop("exitDoneMs", None)

    def raw_exit(self, stock, action, reason):
        """Close a position the bot doesn't manage (crash ahead, pump coming), with backoff."""
        if self.pending_for(stock) or not self.exit_ready(stock):
            return False
        return self.send(stock, action, reason=reason, phase="exit")[0]

    def _freeable(self, exclude, kinds=("park", "swing")):
        """Market value of the bot's open longs of `kinds` (other than `exclude`)."""
        P = self.prices()
        return sum(num(v.get("shares")) * P.get(k, 0.0) for k, v in self.managed.items()
                   if k != exclude and v["kind"] in kinds and v["side"] == "long" and v["state"] == "open")

    def cash_need(self, stock, pct, kinds=("park", "swing"), price=None):
        """Cash an entry of `pct` % on `stock` would use if the bot's longs of `kinds` were sold, capped by the
        shares the stock has left."""
        total = self.cash() + self._freeable(stock, kinds)
        want = pct / 100 * total
        price = price or self.prices().get(stock) or 0.0
        avail = num((self.stock_info().get(stock) or {}).get("availableShares"))
        if avail > 0 and price > 0:
            want = min(want, avail * price)
        return want

    def free_cash(self, exclude, reason, kinds=("park", "swing"), urgent=True, need=None):
        """Sell the bot's other longs for cash: with `need`, only what is missing up to that much cash (parks
        first, and only the part of a park that is needed), else all of them. True while a sale is still
        settling (the cash isn't there yet)."""
        if self.risk.get("tripped"):
            return False                      # nothing to fund: no new entries after a breaker trip
        missing = None if need is None else need - self.cash()
        if missing is not None and missing <= 0:
            return False
        busy = False
        P = self.prices()
        info = self.stock_info()
        # parks first, the lowest dividend rate first: the best-paying parks (a carry park in $STEP) go last
        for k, v in sorted(self.managed.items(), key=lambda kv: (kv[1]["kind"] != "park",
                                                                 num((info.get(kv[0]) or {}).get("dividendRate")))):
            if k == exclude or v["kind"] not in kinds or v["side"] != "long":
                continue
            if v["state"] == "exiting" or (v["state"] == "open" and self.pending_for(k)):
                busy = True
                continue
            if v["state"] != "open" or (missing is not None and missing <= 0):
                continue
            val = num(v.get("shares")) * P.get(k, 0.0)
            if missing is not None and v["kind"] == "park" and val > 0 and missing * 1.03 < 0.95 * val:
                q = missing * 1.03 / val      # a little extra for the exit's price impact
                if self.trim_position(k, q * 100, reason):
                    busy = True
                    missing -= q * val
                continue
            if self.close_position(k, reason, urgent=urgent, target=False):
                busy = True
                if missing is not None:
                    missing -= val
        return busy

    def trim_position(self, stock, pct, reason):
        """Sell `pct` % of a bot long (sellpercent); the rest stays as it is."""
        mg = self.managed.get(stock)
        if not mg or mg["state"] != "open" or mg["side"] != "long" or self.pending_for(stock) or not self.exit_ready(stock):
            return False
        q = int(min(99, max(1, math.ceil(pct))))
        ok, _ = self.send(stock, "sellpercent", q, reason=f"{reason} · sell {q}% of it", phase="exit")
        if ok:
            mg["state"] = "exiting"
            mg["exitReason"] = reason
            mg["partial"] = q
            mg.pop("exitDoneMs", None)
            mg.pop("chunk", None)
        return ok

    def reservations(self, now):
        """Cooldown slots (and, before a rebound buy, the cash) and stocks that scheduled events need."""
        buy_res = short_res = False
        owned = set()
        pl = self.player()
        buy_cd = num(pl.get("buyCooldownSeconds"), 60) * 1000
        short_cd = num(pl.get("shortCooldownSeconds"), 60) * 1000
        lead = self.cfg["crashLeadMin"] * 60000
        on = self.cfg["events"]
        for ev in self.events.values():
            if ev["phase"] in FINAL:
                continue
            s = ev["stock"]
            if ev.get("reset"):
                b, sh, own = resets.reserve(self, ev, now, buy_cd, short_cd)
                buy_res, short_res = buy_res or b, short_res or sh
                if own:
                    owned.add(s)
                continue
            if ev["dir"] == "pump":
                owned.add(s)
                if on:
                    buy_res = buy_res or ev["phase"] in ("new", "entering")
                    # the fade short needs the short slot right after T; hours before T it stays free (a pump
                    # announced 12 h ahead held it all night and blocked every $WHALE cap short, 2026-11-14/15)
                    near = now >= ev["at"] - short_cd - 30000
                    short_res = short_res or (self.cfg["pumpFadeShort"] and (
                        (near and ev["phase"] in ("new", "entering", "long")) or ev["phase"] in ("selling", "fade")))
            else:
                if now >= ev["at"] - lead - 90000:
                    owned.add(s)
                if on and self.cfg["crashShort"] and ev["phase"] in ("new", "pre") and now >= ev["at"] - lead - short_cd - 10000:
                    short_res = True
                if on and self.cfg["crashReboundBuy"] and ev["phase"] in ("new", "pre", "short", "covering", "rebound_wait") \
                        and now >= ev["at"] - buy_cd - 10000:
                    # the rebound buy needs the cash as well as the buy slot: free_cash sells longs for it but cannot
                    # cover shorts, so no swing shorts either until it is bought
                    buy_res = short_res = True
        if resets.fast_buy_reserved(self, now):
            buy_res = True                    # the buy slot waits for the next $WHALE-style dip
        return buy_res, short_res, owned

    def stop_hit(self, mg, breached):
        """Stops need the breach to hold 5 s, and never fire in the first 10 s after a fill."""
        t = now_ms()
        if not breached or t - num(mg.get("filledMs")) < STOP_GRACE_MS:
            mg.pop("stopSince", None)
            return False
        since = mg.setdefault("stopSince", t)
        return t - since >= STOP_CONFIRM_MS

    # ---- 1. scheduled events
    def sync_events(self):
        for e in (self.market or {}).get("scheduledNews") or []:
            if not isinstance(e, dict):
                continue
            oid, d, sid = e.get("occurrenceId"), e.get("direction"), e.get("stockId")
            if not oid or d not in ("pump", "crash") or not sid:
                continue
            at, tgt = int(num(e.get("scheduledAtMs"))), num(e.get("targetPrice"))
            ev = self.events.get(oid)
            if ev is None:
                ev = {"id": oid, "stock": sid, "dir": d, "target": tgt, "at": at, "published": e.get("publishedAtMs"),
                      "phase": "new", "note": "", "start": None, "low": None, "high": None, "tries": 0}
                self.events[oid] = ev
                mins = (ev["at"] - self.server_now()) / 60000
                self.note(f"📣 {d.upper()} {sid} → {tgt:.4g} in {mins:.1f} min", "event")
                self._append("events.jsonl", {**e, "seenMs": now_ms(), "priceNow": self.prices().get(sid)})
            elif (at and at != ev["at"]) or (tgt and abs(tgt - ev["target"]) > 1e-6 * max(1.0, tgt)):
                was = f"{ev['target']:.4g} in {(ev['at'] - self.server_now()) / 60000:.1f} min"
                ev.update(at=at or ev["at"], target=tgt or ev["target"])
                if ev["phase"] == "skipped":
                    ev.update(phase="new", note="revised")
                self.note(f"📣 {d.upper()} {sid} revised → {ev['target']:.4g} in "
                          f"{(ev['at'] - self.server_now()) / 60000:.1f} min (was {was})", "event")
        cut = now_ms() - 3 * 3600000
        for k in [k for k, v in self.events.items() if v["phase"] in FINAL and v["at"] < cut]:
            del self.events[k]

    def _off_note(self):
        return "circuit breaker tripped: no new entries" if self.risk.get("tripped") else "events off"

    def run_events(self, now, P, pos, buy_ready, short_ready):
        # allow=False opens no new legs (no buy, short, fade or rebound, no cash freed) and keeps managing open ones
        allow = bool(self.cfg["events"]) and not self.risk.get("tripped")
        for ev in sorted(self.events.values(), key=lambda e: e["at"]):
            if ev["phase"] in FINAL or not P.get(ev["stock"]):
                continue
            if ev.get("reset"):
                resets.step(self, ev, now, P, pos, buy_ready, short_ready, allow)
            elif ev["dir"] == "pump":
                self.pump_step(ev, now, P, pos, buy_ready, short_ready, allow)
            else:
                self.crash_step(ev, now, P, pos, buy_ready, short_ready, allow)

    def _mine(self, ev):
        mg = self.managed.get(ev["stock"])
        return mg if mg and mg.get("ev") == ev["id"] else None

    def _adopt(self, s, side, p, ev, leg, price):
        """Take over the player's own position on an event stock instead of trading against it."""
        held = p.get("owned", 0.0) if side == "long" else p.get("shorted", 0.0)
        avg = (p.get("avgBuy") if side == "long" else p.get("avgShort")) or price
        self.managed[s] = {"kind": "event", "side": side, "state": "open", "entry": avg, "entryT": self.server_now(),
                           "best": price, "sentMs": now_ms(), "filledMs": now_ms() - STOP_GRACE_MS, "shares": held,
                           "cost": held * avg, "heldBefore": 0.0, "ev": ev["id"], "leg": leg, "adopted": True,
                           "armRef": self.median_price(s) or price}

    def _busy_with_other_event(self, ev, other):
        return other is not None and other.get("kind") == "event" and other.get("ev") != ev["id"] \
            and other.get("ev") in self.events and self.events[other["ev"]]["phase"] not in FINAL

    def pump_step(self, ev, now, P, pos, buy_ready, short_ready, allow):
        c = self.cfg
        s, price = ev["stock"], P[ev["stock"]]
        mg = self._mine(ev)
        if ev["phase"] == "new":
            if not allow:
                ev["note"] = self._off_note()
                return
            if now > ev["at"] - 1500:
                ev.update(phase="missed", note="announced too late")
                return
            gain = (ev["target"] / price - 1) * 100
            if gain < c["pumpMinGain"]:
                ev.update(phase="skipped", note=f"only {gain:.0f}% to target")
                return
            ev.update(phase="entering", start=price, note="buying")
        if ev["phase"] == "entering":
            if mg:
                if mg["state"] == "open":
                    ev.update(phase="long", note="holding for the spike")
                return
            if not allow:
                ev.update(phase="done", note=self._off_note())
                return
            if now > ev["at"] - 1500:
                ev.update(phase="missed", note="couldn't buy before the event")
                return
            if self.pending_for(s):
                return
            p = pos.get(s, {})
            other = self.managed.get(s)
            if other:
                if self._busy_with_other_event(ev, other):
                    ev["note"] = "stock busy with another event"
                elif other["state"] == "open" and other["side"] == "long":
                    other.update(kind="event", ev=ev["id"], leg="pump")   # adopt the swing/park long
                    ev.update(phase="long", note="holding for the spike (adopted)")
                elif other["state"] == "open" and other["side"] == "short":
                    self.close_position(s, "pump coming, cover short", urgent=True)
                return
            if p.get("shorted", 0) > 0:
                self.raw_exit(s, "covermax", "pump coming, cover short")
                return
            if p.get("owned", 0) > 0:
                self._adopt(s, "long", p, ev, "pump", price)
                ev.update(phase="long", note="holding for the spike (your long)")
                return
            if not self.side_ready_by("buy", ev["at"] - 1500):
                ev["note"] = "buy cooldown ends after the event"
                return
            if c["eventFreeCash"] and now < ev["at"] - 8000:
                want = self.cash_need(s, c["eventPercent"])
                if self.cash() < 0.95 * want and self.free_cash(s, f"free cash for {s} pump", need=want * 1.03):
                    ev["note"] = "freeing cash"
                    return
            if buy_ready and not self.pending_open("buy") and ev["tries"] < 4:
                start = ev.get("start") or price
                edge = (start + c["pumpCapture"] / 100 * (ev["target"] - start)) / price - 1
                if self.open_position(s, "long", c["eventPercent"], "event",
                                      f"pump → {ev['target']:.4g} in {(ev['at'] - now) / 1000:.0f}s", edge=edge,
                                      ev=ev["id"], leg="pump"):
                    ev["tries"] += 1
                    ev["note"] = "buying"
                elif self.refusal:
                    ev["note"] = f"not buying: {self.refusal}"
            return
        if ev["phase"] == "long":
            if not mg:
                ev.update(phase="done", note="position gone")
                return
            if mg["state"] != "open":
                return
            if (c.get("pumpTopUp", True) and allow and now < ev["at"] - 8000 and buy_ready
                    and not self.pending_open("buy") and not self.pending_for(s)
                    and (self._whale_safe(now) or now >= ev["at"] - 120000)):
                self._pump_top_up(ev, s, price, pos, now)
            start, tgt = ev.get("start") or mg["entry"], ev["target"]
            ev["high"] = max(ev.get("high") or price, price)
            after = now - ev["at"]
            reason = None
            if price >= start + c["pumpCapture"] / 100 * (tgt - start) and after >= -1500:
                reason = "spike reached target"    # pumps peak on the first tick after T: don't sell a pre-T drift
            elif after > 0 and ev["high"] >= start + 0.5 * (tgt - start) and price <= ev["high"] * (1 - c["pumpTrail"] / 100):
                reason = "pulled back from peak"
            elif after > c["pumpExitAfterSec"] * 1000:
                reason = "event timeout"
            if reason and self.close_position(s, f"pump: {reason} ({(price / mg['entry'] - 1) * 100:+.0f}%)", urgent=True,
                                              target=reason == "spike reached target"):
                ev.update(phase="selling", note=reason)
            return
        if ev["phase"] == "selling":
            if mg and mg["state"] == "open":
                ev["phase"] = "long"          # the sell failed; try again
            elif not mg:
                fade = allow and c["pumpFadeShort"] and now - ev["at"] < 120000
                ev.update(phase="fade" if fade else "done", note="shorting the fade" if fade else "sold")
            return
        if ev["phase"] == "fade":
            if mg:
                if mg["state"] == "open":
                    ev.update(phase="fading", note="short open")
                return
            if not allow or now - ev["at"] > 120000 or ev["tries"] >= 8:
                ev.update(phase="done", note="fade skipped")
                return
            line = (ev.get("start") or price) * (1 + c["fadeCoverAbove"] / 100) * (1 + 2 * c["swingCost"] / 100)
            if price <= line:
                ev.update(phase="done", note="no room to short the fade")
                return
            if short_ready and not self.pending_open("short") and not self.pending_for(s) \
                    and pos.get(s, {}).get("owned", 0) <= 0 and s not in self.managed:
                edge = 1 - (ev.get("start") or price) * (1 + c["fadeCoverAbove"] / 100) / price
                if self.open_position(s, "short", c["eventPercent"], "event", "short the pump fade", edge=edge,
                                      ev=ev["id"], leg="fade"):
                    ev["tries"] += 1
            return
        if ev["phase"] == "fading":
            if not mg:
                ev.update(phase="done", note="fade closed")
                return
            if mg["state"] != "open":
                return
            reason = None
            stop = self.stop_hit(mg, (self.median_price(s) or price) >= mg["entry"] * 1.5)
            if price <= (ev.get("start") or mg["entry"]) * (1 + c["fadeCoverAbove"] / 100):
                reason = "back near start"
            elif now - mg["entryT"] > c["fadeMaxSec"] * 1000:
                reason = "fade timeout"
            elif stop:
                reason = "stop"
            if reason:
                self.close_position(s, f"fade: {reason} ({(1 - price / mg['entry']) * 100:+.0f}%)", urgent=True,
                                    target=reason == "back near start")

    def _pump_top_up(self, ev, s, price, pos, now):
        """pumpTopUp: a pump long bought hours before T is sized to the cash of then (the $BUBL pump announced 12 h
        ahead was bought at 1e12 net worth and was still 1e8 shares at 1e18): buy the shares the game has freed
        since, while the target is still far above the price. Every 20 s at most, in the $WHALE safe window."""
        c = self.cfg
        if ev["target"] < 1.5 * price or now - num(ev.get("topUpMs")) < 20000:
            return
        avail = num((self.stock_info().get(s) or {}).get("availableShares"))
        held = num((pos.get(s) or {}).get("owned"))
        room, nw = avail * price, self.net_worth()
        # worth a buy slot when the extra shares could make >= 0.2 % of net worth at the target (a pump of 10 -> 778
        # makes 77x: a top-up of 1 % of net worth is worth ~+77 %)
        if room < 0.25 * held * price or room * (ev["target"] / price - 1) < 0.002 * nw:
            return
        ev["topUpMs"] = now
        want = min(room, self.cash_need(s, c["eventPercent"], price=price))
        if c["eventFreeCash"] and self.cash() < 0.95 * want and self.free_cash(s, f"free cash for the {s} pump top-up", need=want * 1.03):
            ev["note"] = "freeing cash for a top-up"
            return
        start = ev.get("start") or price
        edge = (start + c["pumpCapture"] / 100 * (ev["target"] - start)) / price - 1
        if self.add_position(s, "event", c["eventPercent"],
                             f"pump top-up → {ev['target']:.4g} in {(ev['at'] - now) / 1000:.0f}s: "
                             f"{avail:.3g} more shares are free", edge=edge):
            ev["note"] = "topping up for the spike"
            ev["adds"] = ev.get("adds", 0) + 1

    def crash_step(self, ev, now, P, pos, buy_ready, short_ready, allow):
        c = self.cfg
        s, price = ev["stock"], P[ev["stock"]]
        mg = self._mine(ev)
        lead = c["crashLeadMin"] * 60000
        rebound = allow and c["crashReboundBuy"]
        if ev["phase"] == "new":
            if now >= ev["at"] + 60000:
                ev.update(phase="missed", note="seen after the crash")
                return
            if not allow:
                ev["note"] = self._off_note()
                return
            if now < ev["at"] - lead:
                ev["note"] = f"acts {c['crashLeadMin']:g} min before"
                return
            drop = (1 - ev["target"] / price) * 100
            if drop < c["crashMinDrop"]:
                ev.update(phase="skipped", note=f"only {drop:.0f}% drop")
                return
            ev.update(phase="pre", start=price, note="selling + shorting ahead")
        if ev["phase"] in ("pre", "short", "covering") and rebound and c["eventFreeCash"] \
                and now >= ev["at"] - 60000 and not ev.get("freed") \
                and self.side_ready_by("buy", ev["at"] + c["reboundMaxWaitMin"] * 60000):
            ev["freed"] = True
            self.free_cash(s, f"free cash for {s} rebound",
                           need=1.03 * self.cash_need(s, c["eventPercent"], price=ev["target"]))
        if ev["phase"] == "pre":
            if mg:
                if mg["state"] == "open" and mg["side"] == "short":
                    ev.update(phase="short", note="short open")
                return
            if not allow:
                ev.update(phase="done", note=self._off_note())
                return
            if any(e is not ev and e["stock"] == s and e["dir"] == "pump" and e["phase"] not in FINAL
                   for e in self.events.values()):
                ev["note"] = "waiting for the pump on this stock to finish"
                if now >= ev["at"]:
                    ev.update(phase="rebound_wait" if rebound else "done", note="waiting for the bottom")
                return
            p = pos.get(s, {})
            other = self.managed.get(s)
            if other:
                if self._busy_with_other_event(ev, other):
                    ev["note"] = "stock busy with another event"
                elif other["state"] == "open" and other["side"] == "long":
                    self.close_position(s, "crash coming, sell", urgent=True)
                elif other["state"] == "open" and other["side"] == "short":
                    other.update(kind="event", ev=ev["id"], leg="crash")
                    ev.update(phase="short", note="short open (adopted)")
                return
            if p.get("owned", 0) > 0:
                self.raw_exit(s, "sellmax", "crash coming, sell")
                return
            if p.get("shorted", 0) > 0:
                self._adopt(s, "short", p, ev, "crash", price)
                ev.update(phase="short", note="short open (your short)")
                return
            if now >= ev["at"]:
                ev.update(phase="rebound_wait" if rebound else "done", note="waiting for the bottom")
                return
            if c["crashShort"] and ev["tries"] < 6:
                want = self.cash_need(s, c["eventPercent"])
                if c["eventFreeCash"] and self.cash() < 0.95 * want and now < ev["at"] - 10000 \
                        and self.side_ready_by("short", ev["at"] - 10000):
                    if self.free_cash(s, f"free cash for {s} crash short", need=want * 1.03):
                        ev["note"] = "freeing cash"
                        return
                if short_ready and not self.pending_open("short") and not self.pending_for(s):
                    edge = 1 - ev["target"] * (1 + c["crashCoverWithin"] / 100) / price
                    if self.open_position(s, "short", c["eventPercent"], "event", f"crash → {ev['target']:.4g}",
                                          edge=edge, ev=ev["id"], leg="crash"):
                        ev["tries"] += 1
                    elif self.refusal:
                        ev["note"] = f"not shorting: {self.refusal}"
            return
        if ev["phase"] == "short":
            if not mg:
                ev.update(phase="rebound_wait" if rebound else "done", note="short gone")
                return
            if mg["state"] != "open":
                return
            ev["low"] = min(ev.get("low") or price, price)
            # far guard, as on swing shorts: cover once the median is (1 + swingStop/100) x the entry (5 s, not in
            # the first 10 s after the fill); checked before the spike rule below, which would otherwise ignore it
            med = self.median_price(s) or price
            far = c["swingStop"] > 0 and med >= mg["entry"] * (1 + c["swingStop"] / 100)
            if self.stop_hit(mg, far):
                if self.close_position(s, f"crash: stop, median {med:.4g} is {med / mg['entry']:.2f}x the entry "
                                          f"({(1 - price / mg['entry']) * 100:+.0f}%)", urgent=True):
                    ev.update(phase="covering", note="stop: far above the entry")
                return
            if price > ev["start"] * 1.5 and now < ev["at"] + 300000:
                ev["note"] = "ignoring a spike"
                return
            reason = None
            if price <= ev["target"] * (1 + c["crashCoverWithin"] / 100):
                reason = "near target"
            elif now >= ev["at"] and price >= ev["low"] * 1.5:
                reason = "rebounding"
            elif now >= ev["at"] + 120000:
                reason = "timeout"
            if reason and self.close_position(s, f"crash: {reason} ({(1 - price / mg['entry']) * 100:+.0f}%)", urgent=True,
                                              target=reason == "near target"):
                ev.update(phase="covering", note=reason)
            return
        if ev["phase"] == "covering":
            if mg and mg["state"] == "open":
                ev["phase"] = "short"
            elif not mg:
                ev.update(phase="rebound_wait" if rebound else "done", note="covered")
            return
        if ev["phase"] == "rebound_wait":
            if mg:
                if mg["state"] == "open":
                    ev.update(phase="rebound", note="holding the rebound")
                return
            if not rebound:
                ev.update(phase="done", note="no rebound buy")
                return
            if now > ev["at"] + c["reboundMaxWaitMin"] * 60000 or ev.get("rtries", 0) >= 4:
                ev.update(phase="done", note="no rebound entry")
                return
            if now < ev["at"] - 5000 or price > ev["start"] * 1.5 or price > ev["start"] * 0.9:
                return
            if buy_ready and not self.pending_open("buy") and not self.pending_for(s) \
                    and pos.get(s, {}).get("shorted", 0) <= 0 and s not in self.managed:
                edge = c["reboundRecover"] / 100 * (ev["start"] / price - 1)
                if self.open_position(s, "long", c["eventPercent"], "event", "buy the crash bottom", edge=edge,
                                      ev=ev["id"], leg="rebound"):
                    ev["rtries"] = ev.get("rtries", 0) + 1
            return
        if ev["phase"] == "rebound":
            if not mg:
                ev.update(phase="done", note="rebound closed")
                return
            if mg["state"] != "open":
                return
            fill = mg["entry"]
            if not mg.get("fillChecked"):
                mg["fillChecked"] = True
                mg["badFill"] = fill > ev["start"] * 0.95
            stop = self.stop_hit(mg, c["reboundStop"] > 0
                                 and (self.median_price(s) or price) <= fill * (1 - c["reboundStop"] / 100))
            reason = None
            if mg.get("badFill"):
                reason = "bad fill"
            elif price >= fill + c["reboundRecover"] / 100 * (ev["start"] - fill):
                reason = "recovered"
            elif stop:
                reason = "stop"
            elif now - mg["entryT"] > c["reboundMaxHoldMin"] * 60000:
                reason = "max hold"
            if reason:
                self.close_position(s, f"rebound: {reason} ({(price / fill - 1) * 100:+.0f}%)", urgent=reason == "stop",
                                    target=reason == "recovered")

    def sweep_orphans(self):
        """Event positions whose event is gone or over are closed instead of being stranded."""
        for s, mg in list(self.managed.items()):
            if mg.get("kind") != "event" or mg["state"] != "open":
                continue
            ev = self.events.get(mg.get("ev"))
            if ev is None or ev["phase"] in FINAL:
                self.close_position(s, "event over: closing the leftover position", target=False)

    # ---- 2. swing trading
    def swing_exit_signal(self, s, mg):
        pos = {"entry": mg["entry"], "entryT": mg["entryT"], "best": mg["best"]}
        strat = mg.get("strat")
        if strat:
            last = self.brain.last(s)
            if not last:
                return None
            try:
                sig = decide(strat, last[0], last[1], self.brain.feat(s), pos)
            except Exception:
                sig = None
            self.brain.signals.setdefault(s, {})[mg["side"]] = sig
            return sig
        sig, _ = self.brain.signal(s, mg["side"], pos)   # position from an older build: current choice (+ 15 min cap)
        if sig != "exit" and brain_mod.TIME_EXIT and self.server_now() - num(mg.get("entryT")) > 900000:
            sig = "exit"
        return sig

    def run_swing(self, now, P, pos, info, buy_ready, short_ready, buy_res, short_res, owned):
        c = self.cfg
        for s, mg in list(self.managed.items()):  # exits first: they have no cooldown
            if mg["kind"] != "swing" or mg["state"] != "open" or s not in P:
                continue
            price = P[s]
            long = mg["side"] == "long"
            mg["best"] = max(mg.get("best") or price, price) if long else min(mg.get("best") or price, price)
            sig = self.swing_exit_signal(s, mg)
            pnl = (price / mg["entry"] - 1) * 100 if long else (1 - price / mg["entry"]) * 100
            med = self.median_price(s) or price
            pnl_med = (med / mg["entry"] - 1) * 100 if long else (1 - med / mg["entry"]) * 100
            stop = self.stop_hit(mg, c["swingStop"] > 0 and pnl_med <= -c["swingStop"])
            hold = c.get("swingHoldLosersLong") if long else c.get("swingHoldLosersShort")
            if not long:
                # shorts pay the dividend every minute they are open (shortsDeductMoney): count it as a loss
                div_pct = (num((info.get(s) or {}).get("dividendRate")) * (self.divs.get("mult") or 1.0) * 100
                           * max(0.0, self.server_now() - num(mg.get("entryT"))) / 60000)
                pnl -= div_pct
                cap = num(c.get("swingShortDivMaxPct"))
                if cap > 0 and div_pct >= cap and pnl < 0 and s not in owned and not self.pending_for(s):
                    # losing and still paying: every minute it stays open costs more (no time exit, held losers)
                    hold = False
                    sig = "exit"
                    self.note_once(f"short-div-{s}", f"closing the losing short {s}: it paid {div_pct:.1f}% in "
                                                      f"dividends ({pnl:+.1f}% with them)", "warn", 600000)
            if hold and s not in owned:
                # the owner's rule: never sell a loser, wait until it is back in profit (no time limit)
                if sig == "exit" and pnl < num(c.get("swingHoldMinPct"), 0.5):
                    if not mg.get("heldLoser"):
                        mg["heldLoser"] = True
                        self.note(f"holding the {mg['side']} {s} at {pnl:+.1f}%: waits until it is back in profit", "info")
                    sig = None
                elif mg.get("heldLoser") and pnl >= num(c.get("swingHoldMinPct"), 0.5):
                    sig = "exit"                      # back in profit: take it (the trail below may ride it)
            trail = num(c.get("swingTrailPct"), 0)
            if trail > 0 and not stop and s not in owned:
                if sig == "exit" and pnl > 0 and not mg.get("trailBest"):
                    mg["trailBest"] = price           # the strategy says take it: ride it while it keeps going
                if mg.get("trailBest"):
                    mg["trailBest"] = max(mg["trailBest"], price) if long else min(mg["trailBest"], price)
                    back = (1 - price / mg["trailBest"]) * 100 if long else (price / mg["trailBest"] - 1) * 100
                    sig = "exit" if back >= trail else None
            reason = "strategy exit" if sig == "exit" else "stop-loss" if stop else \
                "event on this stock" if s in owned else None
            if reason:
                self.close_position(s, f"swing {reason} ({pnl:+.1f}%{', median ' + format(pnl_med, '+.1f') + '%' if stop else ''})",
                                    urgent=reason != "strategy exit")
        if not c["swing"] or self.risk.get("tripped"):     # breaker tripped: the exits above still run
            return
        cash, nw = self.cash(), self.net_worth()
        if cash < max(1.0, 0.002 * nw):
            return
        cands = []
        for s, st in info.items():
            if (not st.get("unlocked", True) or s in c["excluded"] or s in (c.get("swingExcluded") or [])
                    or s in owned or s in self.managed
                    or self.pause_until.get(s, 0) > now_ms() or self.pending_for(s) or not P.get(s)
                    or num(st.get("availableShares")) <= 0):
                continue
            p = pos.get(s, {})
            if p.get("owned", 0) > 0 or p.get("shorted", 0) > 0:
                continue                      # the player's own position: don't trade into or against it
            for side in ("long", "short"):
                if side == "long" and (not buy_ready or buy_res or self.pending_open("buy") or not self.can_open(s, "buy")
                                       or (self.cfg.get("resetDetectKeepBuy") and resets.watching(self))):
                    continue
                if side == "short" and (not short_ready or short_res or self.pending_open("short") or not self.can_open(s, "short")):
                    continue
                sig, row = self.brain.signal(s, side, None)
                if sig != "enter" or not row:
                    continue
                if min(row["train"]["avg"], row["test"]["avg"]) * 100 < c["swingMinEdge"]:
                    continue
                # expected money of one entry now (costs, our impact, Kelly ceiling and share cap included)
                money, pct, vnote = self.brain.money_per_entry(s, side, cash, num(st.get("availableShares")), P[s])
                if money > 0 and pct > 0:
                    cands.append((money, s, side, row, pct, vnote))
        if not cands:
            return
        # each cooldown slot takes its own best entry this tick: the best long (buy slot) and the best short
        # (short slot) both go out, the bigger one first (it gets the cash)
        best = {}
        for x in cands:
            if x[2] not in best or x[0] > best[x[2]][0]:
                best[x[2]] = x
        if "long" in best and c["parkFirst"] and now_ms() - self.park_try_ms >= c["parkIdleSec"] * 1000:
            pk = self.park_pick(now, P, info, pos, owned)
            if pk and pk["money"] > best["long"][0]:
                # parking the idle cash pays more than this long: the buy slot goes to the park (run_park, this tick)
                lm, ls = best.pop("long")[:2]
                self.park_first_ms = now_ms()
                self.note_once("park-first", f"buy slot to a park: {pk['stock']} dividends ~{pk['money']:,.0f} over "
                                             f"{c['parkHoldMin']} min beat the {ls} long (expect {lm:+,.0f})", "info", 60000)
        left = cash
        for money, s, side, row, want, vnote in sorted(best.values(), key=lambda x: -x[0]):
            parked = self._freeable(s, ("park",))
            if parked > 0:
                # what this entry would use with the parked cash back: sell only that part of the park, then enter
                # (on a later pass: the game sizes the order on the cash it sees when it plans it)
                m2, p2, _ = self.brain.money_per_entry(s, side, cash + parked, num(info[s].get("availableShares")), P[s])
                desired = min(p2 / 100 * (cash + parked), c["maxPositionPct"] / 100 * nw,
                              num(c.get("swingMaxNwPct"), 25) / 100 * nw)
                if m2 > money and desired > 1.1 * cash and self.free_cash(
                        s, f"free cash for {s} swing", kinds=("park",), urgent=False, need=desired * 1.03):
                    left = 0.0                    # that cash is spoken for: the other slot waits for the next tick
                    continue
            pct = want or c["swingPercent"]
            cap = num(c.get("swingMaxNwPct"), 25)
            if cap > 0 and cash > 0:
                # a short gets more than `pct` of cash: the game sizes it as if the price fell while it fills
                # (MarketTradeMath.MaxAffordableShares, up to 2x shares); a $STUMP short sent at 44 % used 76 %
                over = self.short_oversize(s) if side == "short" else 1.0
                pct = min(pct, max(1.0, cap / 100 * nw / cash * 100 / over))
            use = min(pct / 100 * cash, max(0.0, num(info[s].get("availableShares"))) * P[s])
            if use > left * 1.02:
                continue                          # the other slot's entry this tick takes this cash: next tick
            # the brain's size; size_entry still applies our position limit and the shares left (no edge: the brain
            # already priced costs and impact, a second impact cap would count them twice)
            reason = (f"{row['label']} · backtest {row['test']['avg'] * 100:+.1f}%/trade · expect {money:+,.0f}"
                      + (f" · {vnote}" if vnote else ""))
            if self.open_position(s, side, pct, "swing", reason, label=row["label"],
                                  strat=row["strat"], expected=row.get("expected"), expectMoney=round(money, 2)):
                self.last_swing_ms = now_ms()
                left -= use

    # ---- 3. dividend parking
    def _recent(self, s, sec, now):
        """s's prices of the last `sec` seconds (brain series, newest first)."""
        ser = getattr(self.brain, "series", {}).get(s)
        out, cut = [], now - sec * 1000
        for t, p in reversed(ser or ()):
            if t < cut:
                break
            out.append(p)
        return out

    def run_cap_short(self, now, P, pos, info, short_ready, short_res):
        """capShort: $WHALE crashes ~60 % within seconds of touching its 1000 cap (9 h, 2026-11-14: every one of
        65 touches of 980+ fell under 0.6x within ~20 s) and cannot trade above the cap, so a short there risks a
        few % and makes ~50 %. Covers in the crash (price <= coverX x entry) or after maxMin; runs before the crash
        buy so the cover goes out first (the buy waits for no short on the stock)."""
        cs = self.cfg.get("capShort")
        if not isinstance(cs, dict) or not cs:
            return
        for s, o in cs.items():
            o = o if isinstance(o, dict) else {}
            px = P.get(s)
            if not px:
                continue
            mg = self.managed.get(s)
            if mg is not None and mg.get("kind") == "capshort":
                if mg["state"] != "open":
                    continue
                held_min = (self.server_now() - num(mg.get("entryT"))) / 60000
                why = None
                if px <= num(o.get("coverX"), 0.6) * num(mg["entry"]):
                    why = "crash"
                elif held_min >= num(o.get("maxMin"), 20):
                    why = f"{held_min:.0f} min, no crash"
                if why:
                    self.close_position(s, f"cap short: cover, {why} ({(1 - px / num(mg['entry'])) * 100:+.0f}%)",
                                        urgent=True, target=False)
                continue
            st = info.get(s) or {}
            p = pos.get(s, {})
            if (mg is not None or not short_ready or short_res or self.risk.get("tripped")
                    or not st.get("unlocked", True) or self.pending_for(s) or self.pending_open("short")
                    or p.get("owned", 0) > 0 or p.get("shorted", 0) > 0 or self.pause_until.get(s, 0) > now_ms()):
                continue
            cap = num(st.get("priceCap"), 1000.0) or 1000.0
            if px < num(o.get("entryX"), 0.98) * cap:
                continue
            pct, keep = num(o.get("pct"), 100), ""
            if self.cfg.get("capShortKeepReserve"):
                # a short locks its value in cash: once the crash buys are cash-limited (1.8Qi net worth, 2026-11-15)
                # a full-size cap short starves the crash buy that follows (+100-140 % vs the short's +40-60 %)
                cash = self.cash()
                room = cash - self.crash_reserve(info, P)
                if cash <= 0 or room < 0.05 * cash:
                    continue
                if room < cash * pct / 100:
                    pct = max(1, int(room / cash * 100))
                    keep = f" · {pct}%: leaves the next crash buy's cash"
            self.open_position(s, "short", pct, "capshort",
                               f"cap short: {px:.4g} is {px / cap:.0%} of the {cap:.4g} cap; it crashes from here{keep}",
                               edge=max(0.05, 1 - num(o.get("coverX"), 0.6)))

    def _floor(self, s, minutes, now):
        """Lowest price of s over the last `minutes` (brain series, cached 30 s), or None with under 10 min of data."""
        cache = self.__dict__.setdefault("_floor_c", {})
        hit = cache.get((s, minutes))
        if hit and now - hit[0] < 30000:
            return hit[1]
        ser = getattr(self.brain, "series", {}).get(s)
        lo, first = None, None
        if ser:
            cut = now - minutes * 60000
            for t, p in reversed(ser):
                if t < cut:
                    break
                first = t
                if p > 0 and (lo is None or p < lo):
                    lo = p
        if first is None or now - first < 600000:
            lo = None
        cache[(s, minutes)] = (now, lo)
        return lo

    def _whale_safe(self, now):
        """True when a floor buy can take the buy slot: within floorBuySafeSec after a dip of every resetDetectBy
        stock (its next dip is minutes away and the 80 s cooldown is over by then)."""
        c = self.cfg
        safe = num(c.get("floorBuySafeSec"), 100)
        if safe <= 0:
            return True
        by = c.get("resetDetectBy") or {}
        if not (c.get("resetDetect") and isinstance(by, dict) and by):
            return True
        seen = self.__dict__.get("_crash_seen", {})
        info = self.stock_info()
        for s in by:
            if not (info.get(s) or {}).get("unlocked", True):
                continue
            t = seen.get(s)
            if t is None or not 0 <= now - t <= safe * 1000:
                return False
        return True

    def run_floor_buy(self, now, P, pos, info, buy_ready, buy_res):
        """floorBuy: stocks that sit on a price floor and spike 10-45x within minutes ($VOID 1 -> 13-45 about 38
        times an hour, 2026-11-14). Buy at the floor (no room to fall), sell into the spike at sellX x the fill or
        after maxMin. Exits first; entries only in the safe window after a $WHALE dip."""
        fb = self.cfg.get("floorBuy")
        if not isinstance(fb, dict) or not fb:
            return
        for s, o in fb.items():
            mg = self.managed.get(s)
            px = P.get(s)
            if not px or mg is None or mg.get("kind") != "floor" or mg["state"] != "open":
                continue
            o = o if isinstance(o, dict) else {}
            entry = num(mg["entry"])
            held_min = (self.server_now() - num(mg.get("entryT"))) / 60000
            why = None
            if entry > 0 and px >= num(o.get("sellX"), 8) * entry:
                why = f"spike {px / entry:.1f}x"
            elif held_min >= num(o.get("maxMin"), 10):
                why = f"{held_min:.0f} min"
            if why:
                self.close_position(s, f"floor buy: sell, {why} ({(px / entry - 1) * 100:+.0f}%)", urgent=True, target=False)
        if (not buy_ready or buy_res or self.pending_open("buy") or self.risk.get("tripped")
                or not self._whale_safe(now)):
            return
        best = None
        for s, o in fb.items():
            o = o if isinstance(o, dict) else {}
            px, st, p = P.get(s), info.get(s) or {}, pos.get(s, {})
            if (not px or s in self.managed or self.pending_for(s) or not st.get("unlocked", True)
                    or p.get("owned", 0) > 0 or p.get("shorted", 0) > 0 or num(st.get("availableShares")) <= 0
                    or self.pause_until.get(s, 0) > now_ms() or not self.can_open(s, "buy")):
                continue
            lo = self._floor(s, num(o.get("floorMin"), 120), now)
            if not lo or px > num(o.get("entryX"), 1.2) * lo:
                continue
            gain = num(st.get("availableShares")) * px * (num(o.get("sellX"), 8) - 1)
            if best is None or gain > best[0]:
                best = (gain, s, o, lo)
        if best:
            _, s, o, lo = best
            px = P[s]
            self.open_position(s, "long", num(o.get("pct"), 100), "floor",
                               f"floor buy: {px:.4g} is {px / lo:.2f}x its {num(o.get('floorMin'), 120):g} min low "
                               f"{lo:.4g}; sell at {num(o.get('sellX'), 8):g}x", edge=num(o.get("sellX"), 8) - 1)

    def run_harvest(self, now, P, pos, buy_ready, buy_res):
        """harvestStocks: a stock that climbs for minutes and then crashes ~95 % in one tick (the $TECH saw-tooth).
        Its held shares, the player's (the game's auto-buy) or the bot's own, are sold at the spike, and bought
        back right after the crash; the player's shares are taken over at the spike (an adopted harvest long)."""
        hs = self.cfg.get("harvestStocks")
        if not isinstance(hs, dict) or not hs:
            return
        for s, o in hs.items():
            o = o if isinstance(o, dict) else {}
            px = P.get(s)
            if not px or self.pending_for(s) or self.pause_until.get(s, 0) > now_ms():
                continue
            mg, p = self.managed.get(s), pos.get(s, {})
            held = p.get("owned", 0.0)
            spike_x, min_gain = num(o.get("spikeX"), 1.35), num(o.get("minGainX"), 2.0)
            if (mg is None or (mg["kind"] == "harvest" and mg["state"] == "open")) and held > 0:
                avg = num(mg["entry"]) if mg else num(p.get("avgBuy"))
                xs = self._recent(s, num(o.get("avgSec"), 600), now)
                mean = sum(xs) / len(xs) if len(xs) >= 60 else None
                if not mean or px < spike_x * mean or avg <= 0 or px < min_gain * avg:
                    continue
                why = (f"harvest: spike to {px / mean:.2f}x its {num(o.get('avgSec'), 600):g}s mean, "
                       f"{px / avg:.1f}x the buy price {avg:.4g}; sell before the crash, buy back after it")
                if mg is None:
                    self.managed[s] = {"kind": "harvest", "side": "long", "state": "open", "entry": avg,
                                       "entryT": self.server_now(), "best": px, "sentMs": now_ms(),
                                       "filledMs": now_ms() - STOP_GRACE_MS, "shares": held, "cost": held * avg,
                                       "heldBefore": 0.0, "adopted": True, "R": 1.0, "Rknown": True,
                                       "armRef": px}   # the player's shares: the breaker counts them from here
                if self.close_position(s, why, urgent=True):
                    self.harvest[s] = {"shares": held, "px": px, "t": now}
                elif mg is None:
                    del self.managed[s]                # nothing went out: the shares stay the player's
                continue
            hv = self.harvest.get(s)
            if mg is not None:
                continue                               # another bot position on it (a swing long, a park...)
            if hv is None and not (o.get("start") and held <= 0):
                continue
            if hv is not None and held >= 0.5 * num(hv.get("shares")) > 0:
                self.harvest.pop(s, None)              # the game's auto-buy bought them back already
                continue
            if not buy_ready or (buy_res and not o.get("overReserve")) or self.pending_open("buy"):
                continue
            hi = max(self._recent(s, 10, now) or [px])
            if px > num(o.get("crashX"), 0.5) * hi:
                continue
            sold = num((hv or {}).get("shares"))
            cash = self.cash()
            want = sold * px * 1.03 / cash * 100 if cash > 0 and sold > 0 else 1.0
            pct = max(1.0, min(num(o.get("maxPct"), 5.0), math.ceil(want)))
            note = f"sold {sold:,.4g} at {num(hv.get('px')):.4g}" if hv else "no shares held yet"
            if self.open_position(s, "long", pct, "harvest",
                                  f"harvest: crash to {px / hi:.2f}x its 10 s high, buy back ({note}); "
                                  f"sell at the next spike"):
                self.harvest.pop(s, None)

    def run_park(self, now, P, pos, info, buy_ready, buy_res, owned):
        c = self.cfg
        for s, mg in list(self.managed.items()):
            if mg["kind"] != "park" or mg["state"] != "open" or s not in P:
                continue
            due = self.reset_in_ms(s, now)
            if due is not None and due < c["parkResetExitSec"] * 1000 and not self.pending_for(s):
                # a park never rides the daily reset (the price drops ~97 % for minutes): sell it, re-park later
                if self.close_position(s, f"park: daily reset in {due / 1000:.0f}s, sell before it", urgent=True):
                    self.park_block[s] = now_ms()
                continue
            if resets.fast_crash(c, s) and not self.pending_for(s) and P[s] >= 0.98 * mg["entry"]:
                # a park on a stock that dips ~60 % every few minutes rides every dip and blocks the dip buys:
                # sell it while the price is up, the crash detector buys the next dip instead
                if self.close_position(s, f"park: {s} dips every few minutes, sell at {P[s]:.4g} "
                                          f"({(P[s] / mg['entry'] - 1) * 100:+.0f}%) and buy the dips instead", urgent=True):
                    self.park_block[s] = now_ms()
                continue
            spike_x = num(c.get("parkSpikeX"), 1.3)
            cap_x = num(c.get("parkCapSellPct")) / 100
            cap = num((info.get(s) or {}).get("priceCap"), 1000.0) or 1000.0
            # only a park that rose into the cap zone: one bought there ($IDXB lives at 75-100 % of its cap) was sold
            # the second it filled, -0.2..-0.5Q a time, once a minute (2026-11-14)
            below = mg["entry"] < cap_x * cap or not c.get("parkCapSellFromBelow", False)
            if c.get("parkSpikeSell") and cap_x > 0 and P[s] >= cap_x * cap and P[s] > mg["entry"] and below                     and not self.pending_for(s):
                if self.close_position(s, f"park: {P[s]:.4g} is {P[s] / cap * 100:.0f}% of the price cap "
                                          f"({(P[s] / mg['entry'] - 1) * 100:+.0f}%), sell before a crash", urgent=True):
                    self.park_block[s] = now_ms() - c["parkBlockMin"] * 60000 + 30000   # may re-park the next dip
                continue
            if c.get("parkSpikeSell") and spike_x > 1 and not self.pending_for(s):
                ref = resets._detect_ref(self, s, now)
                if ref and P[s] >= spike_x * ref:
                    gain = (P[s] / mg["entry"] - 1) * 100
                    if self.close_position(s, f"park: spike to {P[s] / ref:.2f}x its 10 min median ({gain:+.0f}%), "
                                              f"sell before a crash", urgent=True):
                        dips = c.get("parkDipStocks")
                        # a dip stock may re-park the very next crash; others wait parkBlockMin
                        self.park_block[s] = now_ms() - (c["parkBlockMin"] * 60000 - 30000 if isinstance(dips, dict)
                                                         and num(dips.get(s)) > 0 else 0)
                    continue
            med = self.median_price(s) or P[s]
            loss = (1 - med / mg["entry"]) * 100
            if self.stop_hit(mg, c["parkStopLoss"] > 0 and loss >= c["parkStopLoss"] and not mg.get("carry")):
                if self.close_position(s, f"park stop-loss (median -{loss:.0f}%)", urgent=True):
                    self.park_block[s] = now_ms()      # a "calm" stock that fell that far: not again for parkBlockMin
        if not c["dividendPark"] or self.risk.get("tripped") or not buy_ready or buy_res or self.pending_open("buy") \
                or (c.get("resetDetectKeepPark") and resets.watching(self)):
            return
        # idle = no buy-side entry for parkIdleSec (shorts use the other slot); and not again right after a try.
        # A park that beat this tick's best long (run_swing, parkFirst) goes at once.
        if c.get("parkSafeWindow") and not self._whale_safe(now):
            return                           # a park buy right before a $WHALE dip took its buy slot (06:20, 1e18)
        first = now_ms() - self.park_first_ms < 2000
        if now_ms() - (self.park_try_ms if first else max(self.last_buy_ms, self.park_try_ms)) < c["parkIdleSec"] * 1000:
            return
        pk = self.park_pick(now, P, info, pos, owned)
        if pk:
            cy = pk.get("carry")
            why = (f"crash dip: {pk['dip']:.2f}x its 10 min median, held for the dividends until the next spike"
                   if pk.get("dip") else
                   f"carry: its recorded {c['parkHoldMin']:g} min parks made {cy['mean'] * 100:+.0f}% on average, "
                   f"9 in 10 made {cy['p10'] * 100:+.0f}% or more (n={cy['n']})" if cy
                   else f"calm ({pk['vol'] * 100:.1f}%/s)")
            if pk.get("add"):
                ok = self.add_position(pk["stock"], "park", pk["pct"],
                                       f"park top-up: the game freed more shares; dividend {pk['rate'] * 100:.2f}%/min "
                                       f"on {pk['amount']:,.0f} more (~{pk['perMin']:,.0f}/min), {why}",
                                       edge=pk["rate"] * c["parkHoldMin"])
            else:
                ok = self.open_position(pk["stock"], "long", pk["pct"], "park",
                                        f"park idle cash: dividend {pk['rate'] * 100:.2f}%/min on {pk['amount']:,.0f} "
                                        f"(~{pk['perMin']:,.0f}/min), {why}",
                                        edge=pk["rate"] * c["parkHoldMin"], carry=bool(cy))
            if ok:
                self.park_try_ms = now_ms()    # don't re-park every tick
            elif self.refusal:
                self.park_try_ms = now_ms()    # sized to nothing: look again after parkIdleSec

    def park_pick(self, now, P, info, pos, owned):
        """The dividend park the buy slot could open now, or None: the unlocked stock that pays the most dividend
        money per minute on the cash a park may use (cash above parkKeepPct of net worth, at most parkPercent of
        cash, the free shares and maxPositionPct).
        - Calm stocks (1 s moves under parkMaxVol, 10 min span under parkMaxRange) qualify as before.
        - With carryPark, volatile ones qualify when their recorded parks paid (carry_ok): the dividends of a
          long hold dwarf the noise there.
        - Every park buys at or below the stock's parkLookMin median (+parkEntryTol %): never on a spike.
        - A park blocks the bot's own trading of that stock: it is skipped when that trading made more per hour
          lately (stock_money_rate) than the park pays; without that history, when one of its long entries is
          worth more than the park (money_per_entry). With swings off nothing is blocked.
        Returns {stock, rate, vol, amount, pct, perMin, money, carry, look}; perMin includes the measured dividend
        multiplier; money = perMin x parkHoldMin."""
        c = self.cfg
        if not c["dividendPark"] or self.risk.get("tripped"):
            return None
        nw, cash = self.net_worth(), self.cash()
        if nw <= 0 or cash <= 0 or cash < nw * c["parkMinCashPct"] / 100:
            return None
        room = min(cash * c["parkPercent"] / 100, cash - nw * c["parkKeepPct"] / 100, nw * c["maxPositionPct"] / 100)
        if c.get("parkKeepCrashReserve"):
            # once the crash buys are cash-limited (1e18: the $WHALE free shares x its crash price > net worth),
            # a park takes the cash the next crash buy needs; keep it
            room = min(room, cash - self.crash_reserve(info, P))
        if room <= max(1.0, 0.01 * nw):
            return None
        mult = self.divs.get("mult") or 1.0
        best = None
        for s, st in info.items():
            p = pos.get(s, {})
            mg = self.managed.get(s)
            # parkTopUp: an open park buys more when the game frees more shares (the caps grow with the level)
            add = bool(c.get("parkTopUp") and mg and mg.get("kind") == "park" and mg.get("state") == "open"
                       and mg.get("side") == "long")
            if (not st.get("unlocked", True) or s in c["excluded"] or s in owned or num(st.get("availableShares")) <= 0
                    or (s in self.managed and not add) or self.pending_for(s) or (p.get("owned", 0) > 0 and not add)
                    or p.get("shorted", 0) > 0
                    or not self.can_open(s, "buy") or now_ms() - self.park_block.get(s, -1e18) < c["parkBlockMin"] * 60000
                    or resets.blocks_park(self, s, now) or resets.fast_crash(c, s)):
                continue
            due = self.reset_in_ms(s, now)
            if due is not None and due < (c["parkHoldMin"] + 5) * 60000 + c["parkResetExitSec"] * 1000:
                continue                     # its daily reset comes before the park would pay
            rate, price = num(st.get("dividendRate")), num(P.get(s))
            if rate <= 0 or price <= 0:
                continue
            dips = c.get("parkDipStocks")
            dipx = num(dips.get(s)) if isinstance(dips, dict) else 0.0
            if dipx > 0:
                # a crash stock: park only in a crash dip, at or under dipx x its 10 min median, then hold it for the
                # dividends and sell into the next spike (calm/carry/median checks don't apply: the dip is the edge)
                ref10 = resets._detect_ref(self, s, now)
                if not ref10 or price > dipx * ref10:
                    continue
                amount = min(room, max(0.0, num(st.get("availableShares"))) * price)
                per_min = rate * mult * amount
                if per_min <= 0 or (best is not None and best.get("dip") and per_min <= best["perMin"]):
                    continue
                best = {"stock": s, "rate": rate, "vol": self.brain.volatility(s) or 0.0, "amount": amount,
                        "perMin": per_min, "money": per_min * c["parkHoldMin"], "dip": price / ref10,
                        "pct": int(min(100, max(1, math.ceil(amount / cash * 100)))), "carry": None, "look": ref10}
                continue
            if best is not None and best.get("dip"):
                continue                     # a crash dip beats any ordinary park
            if c.get("parkSpikeSell") and num(c.get("parkSpikeX"), 1.3) > 1:
                ref10 = resets._detect_ref(self, s, now)
                if ref10 and price >= 0.9 * num(c.get("parkSpikeX"), 1.3) * ref10:
                    continue                 # already spiking: the spike sell would close it at once
            q = num(c.get("parkEntryPctile"))
            if q > 0:
                lo = self.price_pctile(s, c["parkLookMin"] * 60, q, now)
                if lo is None or price > lo:
                    continue                 # not a good deal yet: waits for the cheapest q % of the last hour
            amount = min(room, max(0.0, num(st.get("availableShares"))) * price)
            per_min = rate * mult * amount
            if per_min <= 0 or (best is not None and per_min <= best["perMin"]):
                continue
            vol = self.brain.volatility(s)
            if vol is None:
                continue
            calm = vol * 100 <= c["parkMaxVol"]
            if calm and c["parkMaxRange"] > 0:
                rng = self.range_pct(s)      # smooth waves pass the 1 s test but swing far
                calm = rng is not None and rng <= c["parkMaxRange"]
            carry = None
            if not calm:
                carry = self.carry_ok(s) if c["carryPark"] else None
                if carry is None:
                    continue
            look = self.brain.median_since(s, c["parkLookMin"] * 60) if c["parkLookMin"] > 0 else None
            tol = c["parkEntryTol"] + (num(c.get("parkTopUpTol")) if add else 0.0)
            if look and (self.median_price(s) or price) > look * (1 + tol / 100):
                continue                     # above its own recent median: wait for the price to come back
            money = per_min * c["parkHoldMin"]
            if c["swing"]:
                block = self.stock_money_rate(s)
                if block is not None:
                    if block > per_min * 60:
                        continue             # the bot's own trading of this stock pays more than parking it
                else:
                    lm, _, _ = self.brain.money_per_entry(s, "long", cash, num(st.get("availableShares")), price)
                    if lm > money:
                        continue             # no live history yet: its long entries are worth more
            if add and c.get("parkTopUpReserve", True):
                # a top-up leaves the cash the next crash buy needs (the free shares of the resetDetectBy stocks
                # at their crash price): those buys made more than the dividends a top-up adds
                amount = min(amount, max(0.0, cash - self.crash_reserve(info, P)))
                per_min = rate * mult * amount
                if best is not None and per_min <= best["perMin"]:
                    continue
            if add and amount < 0.01 * cash:
                continue                     # a top-up under 1 % of cash: an order can't be smaller
            best = {"stock": s, "rate": rate, "vol": vol, "amount": amount, "perMin": per_min, "money": money,
                    "pct": int(min(100, max(1, math.ceil(amount / cash * 100)))), "carry": carry, "look": look,
                    "add": add}
        return best

    def crash_reserve(self, info, P):
        """Cash the largest resetDetectBy crash buy could use now: its free shares at the crash price."""
        c = self.cfg
        by = c.get("resetDetectBy")
        need = 0.0
        for s, o in (by.items() if isinstance(by, dict) else ()):
            px = num(P.get(s))
            frac = num((o if isinstance(o, dict) else {}).get("frac"), num(c.get("resetDetectFrac"), 0.5))
            need = max(need, num((info.get(s) or {}).get("availableShares")) * px * max(0.05, 1 - frac))
        return need * 1.05

    def price_pctile(self, s, sec, q, now):
        """The q-th percentile of s's prices over the last `sec` seconds (brain series, cached 15 s), or None."""
        cache = self.__dict__.setdefault("_pctile", {})
        hit = cache.get((s, sec, q))
        if hit and now - hit[0] < 15000:
            return hit[1]
        ser = getattr(self.brain, "series", {}).get(s)
        xs = [p for t, p in ser if t >= now - sec * 1000] if ser else []
        val = None
        if len(xs) >= 60:
            xs.sort()
            val = xs[min(len(xs) - 1, int(len(xs) * q / 100))]
        cache[(s, sec, q)] = (now, val)
        return val

    def reset_in_ms(self, s, now):
        """ms until the next scheduled daily price reset of s (cfg resetSchedule), or None. Works whether the
        reset trading (cfg resets) is on or off."""
        best = None
        try:
            for st, mod, _ in resets.schedule(self.cfg):
                if st == s:
                    d = resets.next_at(mod, now, 0) - now
                    if d >= 0 and (best is None or d < best):
                        best = d
        except Exception:
            return None
        return best

    def carry_ok(self, s):
        """The brain's recorded parks of s (park_stats "dip": bought at or below the parkLookMin median, held
        parkHoldMin, dividends included) when they qualify a volatile stock for a park: 90 % made at least
        carryMinP10 %, over at least carryMinN windows. Else None."""
        c = self.cfg
        dip = (((self.brain.report or {}).get(s) or {}).get("park") or {}).get("dip")
        if not dip or dip.get("n", 0) < c["carryMinN"] or num(dip.get("p10"), -1.0) * 100 < c["carryMinP10"]:
            return None
        return dip

    def _close_hist_load(self):
        """Seed close_hist from closes.jsonl (the swing/event closes of the last 3 h)."""
        t = now_ms()
        cut, since = t - 3 * 3600000, t
        self.close_hist.clear()
        APPENDER.flush()                      # closes still queued for the background appender
        try:
            with open(self.data / "closes.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        rt = num(r.get("t"))
                    except (ValueError, TypeError):
                        continue
                    if rt <= 0:
                        continue
                    since = min(since, rt)
                    if rt >= cut and r.get("kind") != "park" and r.get("stock"):
                        self.close_hist.append((rt, r["stock"], num(r.get("pnl"))))
        except OSError:
            pass
        self.close_hist_since = since

    def stock_money_rate(self, s, hours=3.0):
        """Money per hour the bot's own swing and event trades of s made over the last `hours` (from closes.jsonl,
        so a restart keeps it), or None with under 30 minutes of trading history."""
        if self.close_hist_since is None:
            self._close_hist_load()
        t = now_ms()
        cut = t - hours * 3600000
        while self.close_hist and self.close_hist[0][0] < cut:
            self.close_hist.popleft()
        span = (t - max(cut, self.close_hist_since)) / 3600000
        if span < 0.5:
            return None
        return sum(x[2] for x in self.close_hist if x[1] == s) / span

    # ---- circuit breaker
    def check_risk(self):
        r = self.risk
        if not self.cfg["armed"] or not r.get("armedAt"):
            return
        if not r.get("armNw"):
            nw = self.net_worth()
            if nw <= 0:
                return
            r["armNw"] = r["peak"] = nw
        unreal = lo = hi = 0.0
        P = self.prices()
        for s, mg in self.managed.items():
            if mg.get("state") not in ("open", "exiting") or not mg.get("shares") or not mg.get("armRef"):
                continue
            if mg.get("breakerExempt") and mg.get("kind") == "event":
                continue                          # a reset leg: its dip after the spike is expected to come back
            n, ref, sg = num(mg["shares"]), mg["armRef"], (1 if mg["side"] == "long" else -1)
            vals = [n * (v - ref) * sg for v in (self.median_price(s), P.get(s)) if v]
            if vals:
                unreal += vals[0]                 # shown: the 10 s median (the last price if there's none)
                lo += min(vals)
                hi += max(vals)
        base = r["armNw"] + r.get("realized", 0.0)
        pnl = r.get("realized", 0.0) + unreal
        eq = base + unreal
        r.update(unrealized=unreal, pnl=pnl, equity=eq)
        # The peak is realized money (open losses lower it, open gains don't raise it: a noisy stock's paper
        # gain coming back is not a loss), and a drop must show in the 10 s median AND the last price (right
        # after a spike the median still holds pre-spike prices, a single tick can be anything).
        if base + min(0.0, lo) > (r.get("peak") or 0):
            r["peak"] = base + min(0.0, lo)
        r["peakPnl"] = r["peak"] - r["armNw"]
        dd = max(0.0, r["peak"] - (base + hi))
        r["drawdown"] = dd
        if r.get("tripped"):
            self.after_trip()
            mins = self.cfg["breakerResumeMin"]
            if mins > 0 and r.get("trippedAt") and now_ms() - num(r["trippedAt"]) >= mins * 60000:
                # entries resume; the peak starts again from where the bot is now (realized, open losses counted)
                r["lastTrip"] = {"at": r.get("trippedAt"), "msg": r.get("tripMsg"), "resumedAt": now_ms()}
                r.update(tripped=False, peak=base + min(0.0, lo), drawdown=0.0, resumes=r.get("resumes", 0) + 1)
                r["peakPnl"] = r["peak"] - r["armNw"]
                for k in ("tripMsg", "trippedAt", "breachSince", "flatNoted"):
                    r.pop(k, None)
                self.note(f"circuit breaker: new entries allowed again after {mins:g} min "
                          f"(peak reset to the current level {r['peak']:,.2f})", "warn")
            return
        lim = self.cfg["maxDrawdownPct"]
        # the limit is a share of the money the bot really has: spending (upgrades, the owner's in-game auto-buys
        # turning cash into stock) lowers the baseline but not net worth, and on 2026-11-16 a 4 %-of-NW dip tripped
        # it as "255 %" while the baseline went negative (which then could never trip at all)
        ref = max(num(r.get("peak")), self.net_worth())
        if not (lim > 0 and ref > 0 and dd > ref * lim / 100):
            r.pop("breachSince", None)
            return
        # hold the breach as long as a stop needs (grace + confirmation): a falling position's own stop gets the
        # first chance to close it, and a single bad tick can't trip the breaker
        since = r.setdefault("breachSince", now_ms())
        if now_ms() - since < STOP_GRACE_MS + STOP_CONFIRM_MS:
            return
        n = sum(1 for mg in self.managed.values() if mg.get("state") in ("opening", "open", "exiting"))
        what = f"{n} bot position{'' if n == 1 else 's'}"
        then = (f"closing {what} now (breakerCloseAll)" if n and self.cfg["breakerCloseAll"] else
                f"{what} {'stays' if n == 1 else 'stay'} managed (stops, targets, max hold)" if n else
                "no bot positions open")
        msg = (f"CIRCUIT BREAKER: bot P/L {pnl:+,.2f} is {dd:,.2f} ({dd / ref * 100:.1f}% of net worth) below its peak "
               f"{r['peakPnl']:+,.2f} since arming (limit {lim:g}%). No new entries; {then}. "
               + (f"Entries resume by themselves in {self.cfg['breakerResumeMin']:g} min (or arm the bot again)."
                  if self.cfg["breakerResumeMin"] > 0 else "Arm the bot again to resume entries."))
        r.pop("breachSince", None)
        r.update(tripped=True, trippedAt=now_ms(), tripMsg=msg, flatNoted=not n)
        self.note("⛔ " + msg, "error")
        self.after_trip()

    def after_trip(self):
        """Every tick while the breaker is tripped (and the bot armed). Entries are blocked in the steps; open
        positions keep their stops, targets and max hold, or with breakerCloseAll are all closed (an exit that
        fails is retried on the next tick)."""
        if self.cfg["breakerCloseAll"]:
            for s, mg in list(self.managed.items()):
                if mg.get("state") == "open":
                    self.close_position(s, "circuit breaker: closing everything", urgent=True)
        if not self.risk.get("flatNoted") and not any(mg.get("state") in ("opening", "open", "exiting")
                                                      for mg in self.managed.values()):
            self.risk["flatNoted"] = True
            self.note("circuit breaker: no bot positions left; new entries stay blocked until the bot is armed again",
                      "warn")

    # ---- plugin bridge + game control
    def plugin_command(self, body):
        action = body.get("action")
        spec = PLUGIN_ACTIONS.get(action)
        if spec is None:
            return 400, {"error": f"unknown plugin action {action!r}", "allowed": sorted(PLUGIN_ACTIONS)}
        out = {"action": action}
        for k, typ in spec.items():
            if k not in body or body[k] is None:
                continue
            v = body[k]
            if typ == "bool":
                out[k] = bool(v)
            elif typ == "int":
                if isinstance(v, bool) or num(v, -1) < 0 or num(v) != int(num(v)):
                    return 400, {"error": f"{k} must be a whole number >= 0"}
                out[k] = int(num(v))
            elif typ == "num":
                if isinstance(v, bool) or num(v, -1) < 0:
                    return 400, {"error": f"{k} must be a number >= 0"}
                out[k] = num(v)
            elif typ == "str":
                if not isinstance(v, str) or not re.fullmatch(NAME_RE, v):
                    return 400, {"error": f"{k} must be a short name"}
                out[k] = v
            elif typ == "stats":
                if not isinstance(v, dict) or len(v) > 32:
                    return 400, {"error": f"{k} must be an object of stat name -> percent"}
                stats = {}
                for name, val in v.items():
                    if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or isinstance(val, bool) \
                            or not 0 <= num(val, -1) <= 100:
                        return 400, {"error": f"{k}: {str(name)[:40]!r} needs a stat name and a percent 0..100"}
                    x = num(val)
                    stats[name] = int(x) if x.is_integer() else x
                out[k] = stats
        for k in PLUGIN_REQUIRED.get(action, ()):
            if k not in out:
                return 400, {"error": f"{action} needs {k}"}
        if action == "buyUpgrade":
            out.setdefault("count", 1)
            if not 1 <= out["count"] <= 1000:
                return 400, {"error": "count must be 1..1000"}
        try:
            self.plugin_cmds.mkdir(parents=True, exist_ok=True)
            ms = now_ms()
            while (self.plugin_cmds / f"{ms}-{action}.json").exists():
                ms += 1
            name = f"{ms}-{action}.json"
            self._write_atomic(self.plugin_cmds / name, json.dumps(out))
        except OSError as e:
            return 500, {"error": f"couldn't write the plugin command: {e}"}
        self.note(f"→ plugin {action} {json.dumps({k: v for k, v in out.items() if k != 'action'})}", "trade")
        self.plugin_sync["last"] = {"t": now_ms(), "file": name, "command": out}
        return 200, {"ok": True, "file": name, "command": out}

    def auto_settings(self):
        c = self.cfg
        return {"upgrades": bool(c["autoUpgrade"] and not c.get("smartUpgrade")), "claim": bool(c["autoClaim"]),
                "stats": {"BuyCooldown": c["autoUpgradeBuyCdPct"], "ShortCooldown": c["autoUpgradeShortCdPct"]}}

    def sync_plugin_auto(self):
        """Send the auto-upgrade/claim settings on the first fresh plugin.json after a start and after each change."""
        if not self.auto_dirty or not self.plugin_fresh() or now_ms() < self.auto_retry_ms:
            return
        code, out = self.plugin_command({"action": "setAuto", **self.auto_settings()})
        if code == 200:
            self.auto_dirty = False
            self.plugin_sync.update(sentAt=now_ms(), file=out["file"], error=None)
        else:
            self.auto_retry_ms = now_ms() + 30000
            self.plugin_sync["error"] = out.get("error")

    def mirror_auto(self, cmd):
        """A setAuto from the dashboard becomes the bot's setting too, so a restart re-sends the user's choice."""
        changed = False
        for src, dst in (("upgrades", "autoUpgrade"), ("claim", "autoClaim")):
            if src in cmd and self.cfg[dst] != bool(cmd[src]):
                self.cfg[dst] = bool(cmd[src])
                changed = True
        for src, dst in (("BuyCooldown", "autoUpgradeBuyCdPct"), ("ShortCooldown", "autoUpgradeShortCdPct")):
            if src in (cmd.get("stats") or {}) and self.cfg[dst] != cmd["stats"][src]:
                self.set_cfg(dst, cmd["stats"][src])
                changed = True
        if changed:
            self.save_cfg()
        self.plugin_sync.update(sentAt=now_ms(), error=None)
        if self.cfg.get("smartUpgrade") and cmd.get("upgrades"):
            self.auto_dirty = True            # the bot picks the upgrades: switch the plugin's own rule off again

    def plugin_status(self):
        pl = self.plugin if isinstance(self.plugin, dict) else None
        try:
            queued = sum(1 for _ in self.plugin_cmds.glob("*.json")) if self.plugin_cmds.exists() else 0
        except OSError:
            queued = None
        return {"installed": pl is not None, "fresh": self.plugin_fresh(),
                "ageSec": round(time.time() - self.plugin_seen, 1) if self.plugin_seen else None,
                "version": pl.get("pluginVersion") if pl else None, "ready": pl.get("ready") if pl else None,
                "auto": pl.get("auto") if pl else None, "autoWanted": self.auto_settings(), "autoPending": self.auto_dirty,
                "autoSentAt": self.plugin_sync["sentAt"], "autoFile": self.plugin_sync["file"],
                "autoError": self.plugin_sync["error"], "lastCommand": self.plugin_sync["last"], "commandsQueued": queued}

    def restart_game(self):
        """Close the game window (WM_CLOSE), wait up to 30 s for it to go, then launch it again. Runs in a thread."""
        if self.restarting:
            return False
        self.restarting = True

        def work():
            try:
                if self.window.handle():
                    self.note(f"restarting the game: closing '{self.window.title}'")
                    self.window.close()
                    deadline = time.time() + 30
                    while time.time() < deadline and self.window.handle():
                        time.sleep(0.5)
                    if self.window.handle():
                        self.note("the game didn't close within 30 s; not relaunching", "error")
                        return
                    time.sleep(3)          # let the process finish exiting before Steam starts it again
                self.note("launching the game")
                self.window.launch()
            except Exception as e:
                self.note(f"game restart failed: {type(e).__name__}: {e}", "error")
            finally:
                self.restarting = False

        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- main loop
    def _safe(self, name, fn, *args):
        t0 = time.monotonic()
        try:
            return fn(*args)
        except Exception as e:
            self.last_error = f"{name}: {type(e).__name__}: {e}"
            self.last_error_ms = now_ms()
            self.error_count += 1
            log.exception("%s failed", name)
            return None
        finally:
            pt = self._pass_t
            if pt is not None:
                pt[name] = pt.get(name, 0.0) + time.monotonic() - t0

    def _autohide(self):
        if self.window.state() == "visible":
            self.window.hide()

    def _start_backtests(self, info, pl):
        c = self.cfg
        unlocked = [s for s, st in info.items() if st.get("unlocked", True)]
        self.brain.stop_loss = c["swingStop"] / 100 if c["swingStop"] > 0 else None   # backtests mirror our stop
        brain_mod.TIME_EXIT = bool(c["swingTimeExit"])                                  # ... and our hold rule
        self.brain.park_hold, self.brain.park_look = c["parkHoldMin"], c["parkLookMin"]   # park_stats windows
        self.brain.div_mult = self.divs.get("mult") or 1.0
        cool = (num(pl.get("buyCooldownSeconds"), 85) * 1000, num(pl.get("shortCooldownSeconds"), 85) * 1000)
        self.brain.run_backtests(unlocked, {s: num(st.get("dividendRate")) for s, st in info.items()},
                                 c["swingCost"] / 100, cool,
                                 impact=self.brain_moves(), cash=self.cash(),
                                 avail={s: num(st.get("availableShares")) for s, st in info.items()})

    def brain_moves(self):
        """Measured impact handed to the backtests once it can be trusted (6+ fills that agree, see ImpactModel).
        Our model counts the whole fill cost as impact (the client's trade code has no spread); the brain adds
        its own open/close spread on top. Matching the two at the brain's sizes (BT_FRAC of cash in, a full
        exit) gives brain move = ours - open spread / BT_FRAC - close spread. Values at or below the brain's
        default are not passed: the default is then the more cautious one."""
        cost = self.cfg["swingCost"] / 100
        c_open, c_close = (2 * cost * x / sum(BT_SPREAD) for x in BT_SPREAD)
        out = {}
        for s, e in self.impact.est.items():
            if e.get("n", 0) >= 6 and e.get("consistent"):
                mv = e["median"] - c_open / BT_FRAC - c_close
                if mv > BT_MOVE:
                    out[s] = round(mv, 4)
        return out

    def disk_free_mb(self):
        """Free MB on the data drive, refreshed every 60 s on a background thread (the query blocked the trading
        thread 1-1.6 s on a busy spinning disk, 2026-11-14)."""
        t = time.time()
        if t - self._disk_t >= 60:
            self._disk_t = t
            threading.Thread(target=self._disk_check, name="disk-check", daemon=True).start()
        return self._disk_mb

    def _disk_check(self):
        try:
            mb = shutil.disk_usage(self.data).free / 1e6
        except OSError:
            mb = None
        self._disk_mb = mb
        if mb is not None and mb < 300:
            self.note_once("disk", f"only {mb:.0f} MB free on the drive of {self.data}: price history, "
                                   "trades and state may stop being saved (trading goes on)", "error", 1800000)

    def _note_pass(self, t0):
        """Record one pass's time and its slowest steps (a pass over 1 s is logged once a minute at most)."""
        ms = (time.monotonic() - t0) * 1000
        pt = self._pass_t or {}
        self._pass_t = None
        pf = self.perf
        pf["passes"] += 1
        pf["maxMs"] = max(pf["maxMs"], ms)
        if ms >= 1000:
            pf["slow"] += 1
            try:
                top = sorted(dict(pt).items(), key=lambda kv: -kv[1])[:5]
            except RuntimeError:         # an API thread added a step meanwhile
                top = []
            row = {"t": now_ms(), "ms": round(ms), "top": {k: round(v * 1000) for k, v in top}}
            pf["recent"].append(row)
            self.note_once("slow-pass", f"slow pass {ms:.0f} ms: " + ", ".join(f"{k} {v}" for k, v in row["top"].items()),
                           "warn", 60000)

    def tick(self):
        """One pass.  Every step is isolated: a failing step (a full disk, a bad file) never stops the others."""
        with self.lock:
            self._pass_t = {"lock wait": time.monotonic() - self._tick_called} if self._tick_called else {}
            self._safe("read", self.read_files)
            if self.cfg["autoHideGame"] and not self.restarting:
                self._safe("hide", self._autohide)
            self._safe("plugin auto", self.sync_plugin_auto)
            self._safe("disk", self.disk_free_mb)
            if not self.fresh():
                self._safe("fills", self.link_fills)
                self._safe("save", self.save_state)
                return
            t = now_ms()
            pl = self.player()
            if t - self.last_nw_ms >= 5000 and pl:
                self.last_nw_ms = t
                nw = num(pl.get("netWorth"))
                self.nw.append((t, nw))
                if self.start_nw is None:
                    self.start_nw = nw
            if t - self.last_nwdb_ms >= 1000 and pl.get("netWorth") is not None:
                self.last_nwdb_ms = t
                self._safe("nw history", self.nwlog.add, t, num(pl.get("netWorth")), num(pl.get("cash")))
            self._safe("results", self.process_results)
            self._safe("expiry", self.expire_pending)
            self._safe("fills", self.link_fills)
            self._safe("events", self.sync_events)
            self._safe("resets", resets.sync, self)
            P, pos, info = self.prices(), self.positions(), self.stock_info()
            self._safe("positions", self.confirm_opens, pos, P)
            now = self.server_now()
            buy_ready = now >= num(pl.get("nextBuyAtMs")) and t >= self.side_block["buy"]
            short_ready = now >= num(pl.get("nextShortAtMs")) and t >= self.side_block["short"]
            te = bool(self.cfg["swingTimeExit"])
            if te != brain_mod.TIME_EXIT:        # the hold rule changed: live exits follow it at once and the
                brain_mod.TIME_EXIT = te         # strategies are selected again under it
                self.brain.last_bt = 0
            if time.time() - self.brain.last_bt > self.cfg["backtestEveryMin"] * 60 and not self.brain.bt_running:
                self._safe("backtest", self._start_backtests, info, pl)
            if self.cfg["armed"] and not self.restarting:
                self._safe("risk", self.check_risk)
            if self.cfg["armed"] and not self.restarting:
                res = self._safe("reservations", self.reservations, now)
                buy_res, short_res, owned = res if res else (True, True, set())   # unsure: no swing/park opens
                self._safe("cap short", self.run_cap_short, now, P, pos, info, short_ready, short_res)
                self._safe("event step", self.run_events, now, P, pos, buy_ready, short_ready)
                self._safe("orphans", self.sweep_orphans)
                self._safe("chunks", self.run_chunks, pos)
                self._safe("floor buy", self.run_floor_buy, now, P, pos, info, buy_ready, buy_res)
                self._safe("harvest", self.run_harvest, now, P, pos, buy_ready, buy_res)
                self._safe("swing", self.run_swing, now, P, pos, info, buy_ready, short_ready, buy_res, short_res, owned)
                self._safe("park", self.run_park, now, P, pos, info, buy_ready, buy_res, owned)
                self._safe("upgrades", upgrades.step, self)
            self._safe("auto ipo", autoipo.step, self)
            self._safe("save", self.save_state)
            self._safe("impact save", self.impact.save)
            if t - self.last_prune > 6 * 3600000:
                self.last_prune = t
                self._safe("prune", self.brain.prune)

    def loop(self):
        """A full pass as soon as market.json changes (checked every POLL_SEC with a stat), else every PASS_SEC."""
        f = self.export / "market.json"
        last = 0.0
        while True:
            t0 = time.monotonic()
            try:
                mt = os.stat(f).st_mtime_ns
            except OSError:
                mt = None
            if (mt and mt != self.market_mtime and mt != self.market_bad) or t0 - last >= PASS_SEC:
                last = t0
                self._tick_called = t0
                try:
                    self.tick()
                    self._note_pass(t0)
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    self.last_error_ms = now_ms()
                    self.error_count += 1
                    log.exception("tick failed")
            time.sleep(max(0.005, POLL_SEC - (time.monotonic() - t0)))

    # ---- API views
    STRAT_FIELDS = ("expected", "suggestPct", "oos", "move", "volumeNote", "hours")

    def _strat_view(self, sid, side, row, cash, avail, price):
        """A validated row for the dashboard, plus what one entry is worth right now (brain.money_per_entry)."""
        out = {"label": row.get("label"), "train": row.get("train"), "test": row.get("test")}
        out.update({f: row.get(f) for f in self.STRAT_FIELDS})
        try:
            money, pct, note = self.brain.money_per_entry(sid, side, cash, num(avail), price or None)
            out["live"] = {"money": money, "pct": pct, "note": note}
        except Exception as e:      # display only
            out["live"] = {"money": None, "pct": None, "note": f"{type(e).__name__}: {e}"}
        return out

    def state(self):
        with self.lock:
            m = self.market or {}
            pl = self.player()
            now = self.server_now()
            pos = self.positions()
            P = self.prices()
            cash_now = num(pl.get("cash"))
            stocks = []
            div_min = 0.0
            for s in m.get("stocks") or []:
                sid = s.get("stockId")
                if not sid:
                    continue
                price = num(s.get("price"))
                p = pos.get(sid, {})
                upnl = 0.0
                if p.get("owned"):
                    upnl += (price - p["avgBuy"]) * p["owned"]
                if p.get("shorted"):
                    upnl += (p["avgShort"] - price) * p["shorted"]
                div_min += (p.get("owned", 0) - p.get("shorted", 0)) * price * num(s.get("dividendRate"))
                imp = self.impact.view(sid)
                ch = self.brain.choices.get(sid) or {}
                ser = self.brain.series.get(sid)
                spark = [x[1] for x in list(ser)[-180:][::3]] if ser else []
                vol = self.brain.volatility(sid)
                mg = self.managed.get(sid)
                stocks.append({
                    "id": sid, "name": s.get("name"), "price": price, "unlocked": s.get("unlocked", True),
                    "div": num(s.get("dividendRate")), "avail": num(s.get("availableShares")),
                    "ch1m": self._change(sid, 60), "ch15m": self._change(sid, 900), "ch1h": self._change(sid, 3600),
                    "vol": vol * 100 if vol else None,
                    "owned": p.get("owned", 0), "avgBuy": p.get("avgBuy", 0), "shorted": p.get("shorted", 0),
                    "avgShort": p.get("avgShort", 0), "upnl": upnl,
                    "impact": imp["move"], "impactN": imp["n"], "impactModel": imp,
                    "maxVolume": s.get("maxVolume"),
                    "realized": self._tot(self.realized_stock[sid]) if sid in self.realized_stock else None,
                    "bot": dict(mg) if mg else None, "paused": self.pause_until.get(sid, 0) > now_ms(),
                    "strat": {k: self._strat_view(sid, k, v, cash_now, s.get("availableShares"), price)
                              for k, v in ch.items()},
                    "signals": dict(self.brain.signals.get(sid, {})),
                    "historyMin": self.brain.history_minutes(sid), "spark": spark,
                    "median": self.median_price(sid),
                })
            events = []
            for ev in sorted(self.events.values(), key=lambda e: e["at"]):
                pr = P.get(ev["stock"])
                events.append({**ev, "inSec": (ev["at"] - now) / 1000,
                               "movePct": (ev["target"] / pr - 1) * 100 if pr else None})
            nw = list(self.nw)
            step = max(1, len(nw) // 500)
            cur_nw = num(pl.get("netWorth"))
            risk = dict(self.risk)
            lim = self.cfg["maxDrawdownPct"]
            risk.update(pnlSinceArm=risk.get("pnl", 0.0) if risk.get("armedAt") else None, limitPct=lim,
                        limit=(risk["peak"] * lim / 100) if risk.get("peak") else None,
                        drawdownPct=(risk.get("drawdown", 0.0) / risk["peak"] * 100) if risk.get("peak") else None,
                        entriesBlocked=bool(risk.get("tripped") and self.cfg["armed"]),
                        resumeAt=(num(risk.get("trippedAt")) + self.cfg["breakerResumeMin"] * 60000
                                  if risk.get("tripped") and risk.get("trippedAt") and self.cfg["breakerResumeMin"] > 0
                                  else None))
            return {
                "now": now_ms(), "fresh": self.fresh(), "modsFolder": self.cmds.exists() or self.export.exists(),
                "exportAgeSec": (now_ms() - num(m.get("writtenAtMs"))) / 1000 if m else None,
                "gameVersion": m.get("gameVersion"), "game": self.window.state(), "title": self.window.title,
                "restarting": self.restarting,
                "error": self.last_error if now_ms() - self.last_error_ms < 60000 else "",
                "errorCount": self.error_count, "lastErrorAt": self.last_error_ms or None,
                "player": {"cash": num(pl.get("cash")), "netWorth": cur_nw, "level": pl.get("level"), "ipoCount": pl.get("ipoCount"),
                           "buyInSec": max(0, (num(pl.get("nextBuyAtMs")) - now) / 1000),
                           "shortInSec": max(0, (num(pl.get("nextShortAtMs")) - now) / 1000),
                           "buyCooldown": pl.get("buyCooldownSeconds"), "shortCooldown": pl.get("shortCooldownSeconds")},
                "session": {"startNw": self.start_nw, "pnl": (cur_nw - self.start_nw) if self.start_nw else 0, "since": self.started_ms},
                "divPerMin": div_min,
                "parked": self._parked_view(P, cur_nw),
                "dividends": {"perMinEst": div_min * (self.divs["mult"] or 1.0), "mult": self.divs["mult"],
                              "received": self.divs["total"], "payouts": self.divs["n"], "last": self.divs["last"]},
                "nw": [[t, v] for t, v in nw[::step]],
                "stocks": stocks, "events": events,
                "news": list(reversed(m.get("marketNews") or []))[:30],
                "trades": [{k: v for k, v in x.items() if not k.startswith("_")} for x in list(self.trades)[:120]],
                "log": list(self.log)[:150],
                "pending": len(self.pending), "cfg": dict(self.cfg),
                "brain": {"info": self.brain.bt_info, "running": self.brain.bt_running,
                          "lastMinAgo": (time.time() - self.brain.last_bt) / 60 if self.brain.last_bt else None,
                          "dbError": getattr(self.brain, "db_error", None), "report": getattr(self.brain, "report", {}),
                          "moves": dict(getattr(self.brain, "moves", {}))},
                "fills": {"median": statistics.median(self.fills) if self.fills else None, "n": len(self.fills)},
                "realized": self.realized_view(),
                "impactModel": {"priorMove": self.impact.prior * 100, "updatedMs": self.impact.updated,
                                "chunkAbovePct": self.cfg["exitChunkAbovePct"]},
                "risk": risk,
                "tick": {"fed": self.tick_fed, "ageMs": (now_ms() - self.tick_ms) if self.tick_ms else None,
                         "gateMs": TICK_GATE_MS, "open": self.tick_window_open()},
                "perf": {"passes": self.perf["passes"], "slow": self.perf["slow"], "maxMs": round(self.perf["maxMs"]),
                         "recent": list(self.perf["recent"]), "api": dict(self.perf["api"])},
                "external": dict(self.ext),
                "plugin": self.plugin, "pluginFresh": self.plugin_fresh(), "pluginStatus": self.plugin_status(),
                "upgrades": getattr(self, "upgrade_view", {"on": False}),
                "ipo": {k: v for k, v in getattr(self, "ipo_view", {}).items() if k != "busy"},
                "dataDir": str(self.data), "diskFreeMB": self._disk_mb,
            }

    def _parked_view(self, P, nw):
        """Cash parked in dividend stocks and what it earns per minute (at the measured payout multiplier)."""
        info, mult = self.stock_info(), self.divs["mult"] or 1.0
        rows = []
        for s, mg in self.managed.items():
            if mg.get("kind") != "park" or mg.get("state") not in ("open", "exiting"):
                continue
            n, px = num(mg.get("shares")), P.get(s, 0.0)
            rate = num((info.get(s) or {}).get("dividendRate"))
            rows.append({"stock": s, "shares": n, "value": n * px, "divPerMin": n * px * rate * mult,
                         "ratePct": rate * 100, "entry": mg.get("entry"),
                         "upnl": (px - num(mg.get("entry"))) * n if mg.get("entry") else None})
        val = sum(r["value"] for r in rows)
        return {"value": val, "pctOfNw": val / nw * 100 if nw > 0 else None,
                "divPerMin": sum(r["divPerMin"] for r in rows), "positions": rows}

    @staticmethod
    def _tot(d):
        out = {k: d.get(k, 0) for k in _totals()}
        out["pnl"] = out["money"]
        out["pnlPct"] = out["money"] / out["cost"] * 100 if out["cost"] > 0 else None
        out["winRate"] = out["win"] / out["n"] if out["n"] else None
        return out

    def realized_view(self):
        """{<kind>: totals, ..., "total": totals, "byStock": {stock: totals}}; money is P/L in cash."""
        out = {k: self._tot(v) for k, v in self.realized.items()}
        tot = _totals()
        for v in self.realized.values():
            for k in tot:
                tot[k] += v.get(k, 0)
        out["total"] = self._tot(tot)
        out["byStock"] = {k: self._tot(v) for k, v in self.realized_stock.items()}
        return out

    def _change(self, stock, sec):
        ser = self.brain.series.get(stock)
        if not ser or len(ser) < 2:
            return None
        cut = ser[-1][0] - sec * 1000
        if ser[0][0] > cut + sec * 200:   # need at least 80 % of the span
            return None
        old = next((p for t, p in ser if t >= cut), None)
        return (ser[-1][1] / old - 1) * 100 if old else None

    def chart(self, stock, range_sec, frm=None, to=None, points=600, step_ms=None):
        since = (self.brain.last(stock) or (now_ms(), 0))[0] - range_sec * 1000 if range_sec else 0
        if frm:
            since = int(frm)
        candles, step = self.brain.candles(stock, since, points, int(to) if to else None, step_ms)   # outside the lock
        first, count = self.brain.first_time(stock)
        with self.lock:
            lo = since or (candles[0]["time"] * 1000 if candles else 0)
            marks = []
            for t in list(self.trades):
                if t.get("stock") == stock and t.get("status") == "done" and t.get("t", 0) >= lo:
                    a = t["action"]
                    kind = "B" if a.startswith("buy") else "S" if a.startswith("sell") else "H" if a.startswith("short") else "C"
                    marks.append({"time": int(t["t"] // 1000), "kind": kind, "by": t.get("source"), "why": t.get("why", "")})
            evs = [{"time": int(e["at"] // 1000), "dir": e["dir"], "target": e["target"], "phase": e["phase"]}
                   for e in self.events.values() if e["stock"] == stock]
            mg = self.managed.get(stock)
            p = self.positions().get(stock, {})
            return {"stock": stock, "candles": candles, "step": step, "marks": sorted(marks, key=lambda x: x["time"]),
                    "events": evs, "firstMs": first, "samples": count,
                    "position": {"avgBuy": p.get("avgBuy"), "owned": p.get("owned"), "avgShort": p.get("avgShort"),
                                 "shorted": p.get("shorted")}, "bot": dict(mg) if mg else None}


# ---------------------------------------------------------------- HTTP
class Server(ThreadingHTTPServer):
    """One agent per port: on Windows SO_REUSEADDR would let a second copy bind the same port and trade twice."""
    daemon_threads = True
    request_queue_size = 64      # a page that loads many charts at once got refused connections past the default 5
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_handler(bot):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else jdumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, fn, *args):
            t0 = time.monotonic()
            with bot.lock:                       # serialize while nothing can change underneath
                t1 = time.monotonic()
                data = jdumps(fn(*args)).encode()
                held = time.monotonic() - t1
            try:
                k = urlparse(self.path).path
                a = bot.perf["api"].setdefault(k, {"n": 0, "maxHoldMs": 0, "sumHoldMs": 0, "maxWaitMs": 0})
                a["n"] += 1
                a["sumHoldMs"] += round(held * 1000)
                a["maxHoldMs"] = max(a["maxHoldMs"], round(held * 1000))
                a["maxWaitMs"] = max(a["maxWaitMs"], round((t1 - t0) * 1000))
            except Exception:
                pass
            return self._send(code, data)

        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/":
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                ext = os.path.splitext(u.path)[1].lower()
                if ext in STATIC_TYPES:          # /index.html, /analytics.html, /vendor/x.js, /app.css, /icon.svg ...
                    root = STATIC.resolve()
                    f = (root / unquote(u.path).lstrip("/\\")).resolve()
                    if root in f.parents and f.is_file():
                        return self._send(200, f.read_bytes(), STATIC_TYPES[ext])
                    return self._send(404, {"error": "not found"})
                if u.path.startswith("/vendor/"):
                    f = (STATIC / unquote(u.path).lstrip("/\\")).resolve()
                    if STATIC.resolve() in f.parents and f.is_file():
                        return self._send(200, f.read_bytes(), "application/javascript")
                if u.path == "/api/state":
                    return self._json(200, bot.state)
                if u.path == "/api/trades":
                    try:
                        limit = max(1, min(10000, int(q.get("limit", ["2000"])[0])))
                        since = float(q.get("since", ["0"])[0])
                    except ValueError:
                        return self._send(400, {"error": "bad limit or since"})
                    rows = bot.trade_log.read()               # outside the agent lock
                    with bot.lock:
                        rows += [{k: v for k, v in e.items() if not k.startswith("_")} for e in bot.unflushed]
                    rows = [e for e in rows if num(e.get("t")) >= since]
                    rows.sort(key=lambda e: num(e.get("t")), reverse=True)
                    return self._send(200, {"trades": rows[:limit], "total": len(rows), "limit": limit, "since": since})
                if u.path == "/api/cashflows":          # cash that moved without a trade (spending, rewards)
                    try:
                        since = float(q.get("since", ["0"])[0])
                    except ValueError:
                        return self._send(400, {"error": "bad since"})
                    flows = []
                    try:
                        with open(bot.data / "cashflows.jsonl", encoding="utf-8") as fh:
                            for line in fh:
                                try:
                                    e = json.loads(line)
                                except ValueError:
                                    continue
                                if num(e.get("t")) >= since and e.get("why") != "dividends":
                                    flows.append(e)
                    except OSError:
                        pass
                    return self._send(200, {"flows": flows[-3000:]})
                if u.path == "/api/nw":
                    try:
                        rng = float(q.get("range", ["0"])[0])
                        pts = max(50, min(6000, int(q.get("points", ["1000"])[0])))
                        frm, to = float(q.get("from", ["0"])[0]), float(q.get("to", ["0"])[0])
                    except ValueError:
                        return self._send(400, {"error": "bad range"})
                    try:
                        return self._send(200, bot.nwlog.query(max(0.0, rng), pts, frm or None, to or None))
                    except sqlite3.Error as e:
                        return self._send(503, {"error": f"net-worth history unavailable: {e}", "points": []})
                if u.path == "/api/chart":
                    try:
                        rng = int(q.get("range", ["3600"])[0])
                        pts = max(50, min(6000, int(q.get("points", ["600"])[0])))
                        frm, to = float(q.get("from", ["0"])[0]), float(q.get("to", ["0"])[0])
                        step = float(q.get("step", ["0"])[0])        # candle seconds (0 = fit `points`)
                        if not math.isfinite(step) or step < 0:
                            raise ValueError
                    except ValueError:
                        return self._send(400, {"error": "bad range"})
                    data = bot.chart(q.get("stock", [""])[0], rng, frm or None, to or None, pts,
                                     int(step * 1000) if step else None)
                    with bot.lock:
                        body = jdumps(data).encode()
                    return self._send(200, body)
                self._send(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as e:
                log.exception("GET %s failed", self.path)
                try:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                except OSError:
                    pass

        def do_POST(self):
            try:
                self._post()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as e:
                log.exception("POST %s failed", self.path)
                try:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                except OSError:
                    pass

        def _post(self):
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{bot.port}", f"http://localhost:{bot.port}"):
                return self._send(403, {"error": "bad origin"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "body must be a JSON object"})
            path = urlparse(self.path).path
            with bot.lock:       # no /api/trade or /api/release: only the bot trades
                if path == "/api/config":
                    before = {k: bot.cfg[k] for k in AUTO_KEYS}
                    bad = {}
                    for k, v in body.items():
                        try:
                            bot.set_cfg(k, v)
                        except (TypeError, ValueError) as e:
                            bad[k] = str(e)
                    if any(bot.cfg[k] != before[k] for k in AUTO_KEYS):
                        bot.auto_dirty = True          # the plugin gets the new auto settings when it is live
                    warn = bot.save_cfg()
                    out = {"ok": not bad, "errors": bad, "cfg": dict(bot.cfg)}
                    if warn:
                        out["warning"] = warn
                    return self._send(400 if bad else 200, out)
                if path == "/api/arm":
                    bot.arm(bool(body.get("armed")), "dashboard", keep_trip=bool(body.get("keepBreaker")))
                    return self._send(200, {"ok": True, "armed": bot.cfg["armed"]})
                if path == "/api/backtest":
                    bot.brain.last_bt = 0
                    return self._send(200, {"ok": True})
                if path == "/api/plugin":
                    code, out = bot.plugin_command(body)
                    if code == 200 and out["command"]["action"] == "setAuto":
                        bot.mirror_auto(out["command"])
                    return self._send(code, out)
                if path == "/api/game":
                    fn = {"hide": bot.window.hide, "show": bot.window.show, "launch": bot.window.launch,
                          "restart": bot.restart_game}.get(body.get("action"))
                    try:
                        return self._send(200, {"ok": bool(fn and fn())})
                    except OSError as e:
                        return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                if path == "/api/quit":
                    bot.note("agent stopped from the dashboard")
                    for step in (bot.flush_trades, bot.save_state, lambda: bot.impact.save(force=True)):
                        try:
                            step()
                        except Exception:
                            log.exception("shutdown step failed")
                    threading.Timer(0.3, lambda: os._exit(0)).start()
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return H


def main():
    ap = argparse.ArgumentParser(description="Screen Stocks trading agent + dashboard")
    ap.add_argument("--mods", default=str(DEFAULT_MODS))
    ap.add_argument("--data", default=str(HERE / "data"))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--title", default=GAME_TITLE, help="game window title (Sandboxie: e.g. '[#] Screen Stocks [#]')")
    ap.add_argument("--window-class", default=GAME_CLASS,
                    help="game window class (Sandboxie renames it, e.g. 'Sandbox:ScreenStocks2:UnityWndClass')")
    ap.add_argument("--launch", default=LAUNCH,
                    help="what Launch and restart run: a URL (default: Steam) or a .cmd/.bat/.exe path, optionally with arguments")
    ap.add_argument("--arm", action="store_true", help="arm the bot as soon as the feed is live (this run only)")
    a = ap.parse_args()
    Path(a.data).mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(Path(a.data) / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # bot.log is written by a listener thread: a log line on the trading thread waited 1-2.6 s for a busy
    # spinning data disk (2026-11-14)
    import queue as _queue
    lq = _queue.SimpleQueue()
    listener = logging.handlers.QueueListener(lq, handler)
    listener.start()
    qh = logging.handlers.QueueHandler(lq)
    qh.setFormatter(logging.Formatter("%(message)s"))   # the file handler adds the time and level
    logging.basicConfig(level=logging.INFO, handlers=[qh])
    try:
        srv = Server(("127.0.0.1", a.port), None)
    except OSError:
        log.error("port %s busy: another copy is probably running", a.port)
        if sys.stdout:
            print(f"port {a.port} busy: another copy is probably running")
        return
    bot = Bot(a.mods, a.data, a.title, a.launch, a.window_class)
    bot.port = a.port
    bot.note(f"data in {bot.data}; game window {a.window_class!r} / {a.title!r}")
    srv.RequestHandlerClass = make_handler(bot)
    if bot.cfg["autoLaunchGame"] and bot.window.state() == "not running":
        bot.note("launching the game")
        try:
            bot.window.launch()
        except OSError as e:
            bot.note(f"couldn't launch the game ({a.launch}): {e}", "error")
    if bot.cfg["armOnStart"] or a.arm:
        def arm_when_ready():
            for _ in range(600):
                if bot.fresh():
                    with bot.lock:
                        if not bot.cfg["armed"]:
                            bot.arm(True, "--arm" if a.arm else "arm on start", keep_trip=True)
                    return
                time.sleep(1)
            bot.note("feed never went live; not arming", "warn")
        threading.Thread(target=arm_when_ready, daemon=True).start()
    threading.Thread(target=bot.loop, daemon=True).start()
    if sys.stdout:
        print(f"Screen Stocks agent: http://127.0.0.1:{a.port}   (mods: {a.mods})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
