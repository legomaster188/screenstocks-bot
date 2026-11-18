"""Swing-trading brain: 1 s price store, a family of swing strategies, honest walk-forward validation, live signals.

Each Screen Stocks stock follows its own pattern (waves, sawtooth climbs and crashes, spikes, pure noise...).
The brain records 1 s prices, simulates every strategy of a parameter grid on each stock and lets the bot
trade only what held up on data the selection never saw, priced the way live fills actually behave.

How a backtest prices a trade (execution model fitted to the bot's own live fills)
  * Fill timing. An order that executes within ~0.9 s of the tick it was decided on fills at that tick's
    price (d = 0); a later one fills at the next 1 s price (d = 1) (live audit 2026-11-13). Entry and exit
    delays are independent: w = (pi, 1 - pi, 0, 0). The backtests assume pi = 0.55 (TICK_PI on the
    market.json tick path, HIST_PI on the history.json path) although the tick path should land ~90 % of
    fills on the tick: in the replay, picking and ranking with pi = 0.9 lost 9 % of log growth against 0.55
    even when the replayed fills landed on the tick 90 % of the time (0.9 favours strategies that need the
    exact extreme tick). Live fills of the current path (data/trades.jsonl, seen price vs impact-free fill,
    EM with FILL_PRIOR pseudo-fills at the prior) can only lower pi for a stock.
    Every trade carries the expected return over these delays (the base of selection and expectations,
    row["oos"]["avgModel"]) and, as diagnostics, the returns with both fills 0/1/2/3 samples late.
  * The bot's position-dependent exits (take-profit vs the entry, trailing vs the best price, the stop) are
    measured from its real fill, so the backtest anchors them at the likelier fill: the decision sample when
    pi >= 0.5, else the next one (row["oos"]["anchor"]).
  * Costs = price impact only (the game's trade code has no spread): open p * (1 + move * f / 2), close
    p * (1 - move * R / 2) with f = share of cash (BT_FRAC in backtests) and R = 1 for a full close; move per
    stock from data/impact_model.json (fitted on live fills, about 0.033), LIVE_MOVE when it is missing.
    Longs earn the dividend per minute held, shorts pay it.
  * The bot's swing stop is simulated: median of the last 10 samples beyond -15 %, held 5 s, not in the
    first 10 s after the fill.
  * Separate cooldowns for buys and shorts (cool_ms may be a number, a (buy, short) pair or a dict).
  * Windows count samples (about one per second); a pause longer than GAP_MS starts a new segment.
How a strategy becomes valid
  * The stock needs MIN_HISTORY_SEC (30 min) of prices; up to BACKTEST_SEC are used.
  * Nested walk-forward: the newest part of the history is cut into K test folds (5-20 min each, more as
    history grows). For every fold the selection rule picks one strategy per side using only trades that
    closed before the fold; that pick's trades inside the fold are out-of-sample. Their union is
    row["test"]: an honest estimate of what the rule earns on unseen data. The live pick is the rule applied
    to all data; row["train"] holds its in-sample statistics.
  * Selection rule: lower confidence bound of the average trade (avg - sd/sqrt(n)) with a minimum number of
    trades, times the share of neighbouring parameter sets that are profitable too (stability), times a bonus
    when the edge survives 2 s and 3 s fill delays.
  * Valid when the rule's out-of-sample trades are numerous enough, profitable on average, significant
    (t >= MIN_T) and profitable in at least half of the folds that traded.
Learning from live fills (row["liveFills"])
  * Every fill-accurate round trip in trades.jsonl (pnlPct) of the row's stock and side is evidence about
    the row: same label weight 1, other labels LIVE_OTHER_W. Normal-normal shrinkage: prior = the model's
    expectation with sd sqrt(sd^2/n + (MODEL_RISK * expected)^2), live trades with the out-of-sample sd.
    row["expected"] becomes the posterior; a posterior <= 0 disables new entries (signal() says None when
    flat, money_per_entry() says 0); open positions still get their exits.
Each chosen row also carries
  * "expected": expected profit per entry after costs as a FRACTION (0.12 = +12 %) at BT_FRAC of cash:
    the out-of-sample mean with every trade capped at +WIN_CAP (200 %), shrunk by its t-statistic, then by
    the live record; "expectedModel": before live; oos["avgModel"]: the uncapped mean. Why capped: for the
    64 live swing round trips so far (live mean +86.3 %), the uncapped backtest mean of the traded strategy
    predicted +145.6 % (long dips +245 % vs +135 % live), the mean capped at +200 % predicted +86.3 % (bias 0,
    lowest error); validity (t >= MIN_T) uses the capped returns too, so one freak trade cannot make a row.
  * "suggestPct": % of cash: 100 unless the stock's free shares cap it (the bot's maxPositionPct applies on
    top). On the recorded prices every Kelly-based size (half Kelly, confidence-scaled, full Kelly on the
    posterior-scaled returns) earned less log growth than always using the position cap, under both fill
    models, and the live audit found log growth per trade still rising at 100 % of cash. "kellyPct" keeps
    the full-Kelly share for information. Brain.money_per_entry(stock, side, cash, availableShares) redoes
    the size live with current cash, volume caps and the impact optimum.
  * "rate": entries per hour the strategy found in the history (used by best_entry()).
Choosing between signals: Brain.best_entry(cands, cash, now, park=...) ranks swing candidates and dividend
parks by expected money and takes the best one per cooldown slot whenever it is worth money, unless waiting
for a likely better signal is clearly (WAIT_CLEAR x) worth more (see its docstring).
Storage (prices.db, table p(stock, t, price), rollback journal so read-only readers keep working)
  * ingest_tick() (market.json, ~0.65 s earlier) and ingest() (history.json) update memory at once. A sample
    within TICK_DUP_MS of a stored one is the same tick and is kept once; history.json back-fills ticks the
    live path missed (up to BACKFILL_MS back). A background thread writes the database in batches. When it
    is busy or the disk is full, samples wait in memory (up to PENDING_MAX) and Brain.db_error says why;
    nothing is raised.
  * candles() aggregates inside SQLite per candle (index seeks, no sort); first_time() counts incrementally;
    prune() deletes by index ranges in a background thread.
"""
import bisect
import collections
import itertools
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

KEEP_SEC = 12 * 3600             # in-memory history per stock (the whole session so far)
BACKTEST_SEC = 12 * 3600         # history each backtest uses (at most): all recorded data
MIN_HISTORY_SEC = 30 * 60        # below this much history nothing can be valid
FOLD_MIN_SEC, FOLD_MAX_SEC = 5 * 60, 20 * 60      # length of one walk-forward test fold (history / 6, clamped)
TRAIN_MIN_SEC, TRAIN_MAX_SEC = 15 * 60, 60 * 60   # the first fold trains on history / 3 (clamped)
FULL_SIZE_SEC = 2 * 3600         # suggestPct can reach full size with this much history
GAP_MS = 10000                   # a longer pause between samples (game or bot offline) splits the series
FILLS = (0, 1, 2, 3)             # every trade is also priced with both fills this many samples after the signal
BASE = 1                         # index in FILLS of the next-sample case (diagnostics; the base is the model mix)
SPREAD = (0.009, 0.019)          # legacy open/close split of a passed `cost`, used only without a live impact model
DEFAULT_MOVE = 0.01              # legacy impact default (stockbot reads it); the brain uses LIVE_MOVE instead
LIVE_MOVE = 0.0331               # impact per 100 % of cash fitted on live fills (impact_model.json "prior")
TICK_PI = 0.55                   # share of fills at the decision tick the backtests assume (at most) on the tick path
HIST_PI = 0.55                   # ... on the history.json path (live audit: 46 % of legs filled a tick late)
TICK_DUP_MS = 300                # a sample this close to a stored one is the same tick (history.json repeats it +-1 ms)
BACKFILL_MS = 60000              # history.json fills ticks the live path missed up to this far behind the newest
WRITE_BATCH_SEC = 0.5            # the database writer collects samples this long per transaction
FILL_TOL = 0.004                 # a live fill within 0.4 % of the seen price (impact removed) landed "on" it
FILL_PRIOR = 8.0                 # pseudo-fills at the regime's prior share of decision-tick fills
BT_FRAC = 0.5                    # share of cash per entry assumed in backtests (the bot's default)
MIN_T = 1.0                      # out-of-sample t-statistic the selection rule needs
WIN_CAP = 2.0                    # expectations count a trade above +200 % as +200 % (see "expected" below)
WAIT_CLEAR = 2.0                 # best_entry waits only when waiting is worth this many times the candidate
PARK_HOLD_MIN = 10.0             # minutes a dividend park is assumed to stay open (best_entry)
PARK_FRAC = 0.7                  # share of cash a park uses (the bot's parkPercent)
PARK_MAX_VOL = 0.05              # no parking on stocks whose 1 s volatility over 10 min exceeds this
MODEL_RISK = 0.5                 # prior sd of the live mean around the model expectation, share of |expected|
LIVE_OTHER_W = 0.3               # weight of live trades of other strategies on the same stock and side
LIVE_MAX_AGE_H = 24              # live round trips older than this are ignored
STOP_LOSS = 0.15                 # the bot's swing stop: median loss ...
STOP_MEDIAN = 10                 # ... of the last 10 samples ...
STOP_MS = 5000                   # ... held this long ...
STOP_GRACE_MS = 10000            # ... and never in the first 10 s after the fill
TIME_EXIT = True                 # True: a swing also exits after its strategy's max hold h. False (the owner's
                                 # "hold as long as it wants"): only the exit rule closes it; backtests then mark a
                                 # trade still open at the end of the data or before a gap to market there
TAIL = 700                       # samples kept for live features (longest window + margin)
BLOCK = 32                       # block size of the exit scanner's skip tables
WRITE_TIMEOUT = 5.0              # s the background writer waits for a chart read before retrying later
PENDING_MAX = 200000             # samples held in memory while prices.db cannot be written (disk full...)
NAN = float("nan")
INF = float("inf")


# ---------------------------------------------------------------- strategy family
# kind -> ordered parameter grids; neighbours differ by one step in one parameter
PARAMS = {
    # mean reversion: enter when the price is e below (long) / above (short) its w-sample mean,
    # exit back at mean +- x (dips bought, spikes shorted)
    "rev": (("w", (30, 60, 180, 600)), ("e", (0.05, 0.10, 0.20, 0.35)), ("x", (0.0, 0.05)), ("h", (180, 900)),
            ("sm", (1, 5))),
    # swing turn: price dipped r below its w-sample mean and turned up r from the 10-sample low (mirror for
    # shorts); exit on the far side of the mean after an r pullback from the best price
    "turn": (("w", (40, 90, 180)), ("r", (0.02, 0.05, 0.10)), ("h", (120, 300)), ("sm", (1, 3))),
    # crash buy / spike short: price fell d below its k-sample high (long) / rose d above its k-sample low
    # (short); take profit tp vs the fill (sawtooth resets, $PUMP crashes, $STEAM noise)
    "crash": (("k", (5, 10, 20)), ("d", (0.25, 0.40, 0.55)), ("tp", (0.10, 0.25, 0.50, 0.90)),
              ("h", (60, 300, 900))),
    # calm entry: the k-sample range is under c; take profit tp (sell into the next spike / cover a crash)
    "calm": (("k", (10, 20)), ("c", (0.03, 0.06, 0.10)), ("tp", (0.10, 0.25, 0.50)), ("h", (60, 180))),
    # momentum: rose (fell) r over L samples without a pullback; trailing stop tr
    "trend": (("L", (10, 30, 60)), ("r", (0.05, 0.15, 0.30)), ("tr", (0.10, 0.25)), ("h", (30, 120, 600))),
}
OLD_KINDS = {"meanrev": "rev", "osc": "turn", "dip": "crash"}   # strategies of the previous brain version


def _grid():
    strats, keys = [], []
    for kind, spec in PARAMS.items():
        for side in (1, -1):
            for idx in itertools.product(*[range(len(v)) for _, v in spec]):
                s = {"kind": kind, "side": side}
                for (name, vals), j in zip(spec, idx):
                    s[name] = vals[j]
                s.setdefault("sm", 1)
                strats.append(s)
                keys.append((kind, side, idx))
    where = {k: i for i, k in enumerate(keys)}
    nbrs = []
    for kind, side, idx in keys:
        out = []
        for p in range(len(idx)):
            for step in (-1, 1):
                j = list(idx)
                j[p] += step
                k = where.get((kind, side, tuple(j)))
                if k is not None:
                    out.append(k)
        nbrs.append(out)
    return strats, nbrs


GRID, NEIGHBORS = _grid()


def _norm(s):
    """Strategy dict in the current vocabulary (rows/positions saved by the previous brain still work)."""
    k = s.get("kind")
    if k == "meanrev":
        return {**s, "kind": "rev", "sm": 1}
    if k == "osc":
        return {"kind": "turn", "side": s["side"], "w": s["L"], "r": s["r"], "h": s.get("h", 300), "sm": 1}
    if k == "dip":
        return {**s, "kind": "crash", "k": 10}
    return s


def need(s):
    """Samples of contiguous history a strategy's features need."""
    k = s["kind"]
    if k == "rev":
        return max(s["w"], s.get("sm", 1))
    if k == "turn":
        return max(s["w"], 10 + s.get("sm", 1) - 1)
    if k in ("crash", "calm"):
        return s["k"]
    if k == "trend":
        return s["L"] + 1
    return 1


def describe(s, time_exit=None):
    s = _norm(s)
    k, side = s["kind"], s["side"]
    sd = "long" if side > 0 else "short"
    sm = f", {s['sm']}s median" if s.get("sm", 1) > 1 else ""
    mx = f", max {s['h']}s" if (TIME_EXIT if time_exit is None else time_exit) else ""
    if k == "rev":
        return (f"{sd} {'dip' if side > 0 else 'spike'} {s['e'] * 100:.0f}% vs {s['w']}s avg, "
                f"exit {'+' if side > 0 else '-'}{s['x'] * 100:.0f}%{sm}{mx}")
    if k == "turn":
        return f"{sd} swing turn {s['r'] * 100:.0f}% around {s['w']}s avg{sm}{mx}"
    if k == "crash":
        if side > 0:
            return f"buy {s['d'] * 100:.0f}% crash ({s['k']}s), sell +{s['tp'] * 100:.0f}%{mx}"
        return f"short {s['d'] * 100:.0f}% spike ({s['k']}s), cover -{s['tp'] * 100:.0f}%{mx}"
    if k == "calm":
        return (f"{sd} when calm (<{s['c'] * 100:.0f}% in {s['k']}s), "
                f"take {'+' if side > 0 else '-'}{s['tp'] * 100:.0f}%{mx}")
    if k == "trend":
        return f"ride {'climb' if side > 0 else 'slide'} {s['r'] * 100:.0f}%/{s['L']}s, trail {s['tr'] * 100:.0f}%{mx}"
    return str(k)


def _med(vals):
    v = sorted(vals)
    return v[len(v) // 2]


def _median(vals):
    """Median like statistics.median (mean of the middle pair for even counts)."""
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


# ---------------------------------------------------------------- live features (newest sample only)
class Tail:
    """Feature values at the newest sample, defined exactly like the backtest arrays (index-based windows,
    a gap longer than GAP_MS starts a new segment, medians over the samples available in the segment).
    Also callable like the previous brain's feature function: F(span) -> (mean, lo, hi, first)."""

    def __init__(self, ts, ps):
        self.ts, self.ps = ts, ps
        n = len(ps)
        age = 1 if n else 0
        while age < n and ts[n - age] - ts[n - age - 1] <= GAP_MS:
            age += 1
        self.age = age   # samples in the current segment (capped by the tail length)
        self.n = n
        self.c = {}

    def __call__(self, span):
        if self.age < span or span < 1:
            return (None, None, None, None)
        w = self.ps[self.n - span:]
        return (sum(w) / span, min(w), max(w), w[0])

    def basis(self, sm, back=0):
        """Median of the last sm samples (within the segment), `back` samples ago."""
        if sm == 1:
            return self.ps[self.n - 1 - back]
        key = ("b", sm, back)
        v = self.c.get(key)
        if v is None:
            i = self.n - 1 - back
            lo = max(self.n - self.age, i - sm + 1)
            v = self.c[key] = _med(self.ps[lo:i + 1])
        return v

    def mean(self, w):
        key = ("m", w)
        v = self.c.get(key)
        if v is None:
            v = self.c[key] = sum(self.ps[self.n - w:]) / w
        return v

    def lo(self, sm, k):
        key = ("lo", sm, k)
        v = self.c.get(key)
        if v is None:
            v = self.c[key] = min(self.ps[self.n - k:]) if sm == 1 else min(self.basis(sm, b) for b in range(k))
        return v

    def hi(self, sm, k):
        key = ("hi", sm, k)
        v = self.c.get(key)
        if v is None:
            v = self.c[key] = max(self.ps[self.n - k:]) if sm == 1 else max(self.basis(sm, b) for b in range(k))
        return v


def decide(s, t, p, F, pos, time_exit=None):
    """Live decision of one strategy at the newest sample (time t, price p). F: Brain.feat(stock).
    pos: None or {entry, entryT, best} (fill price, fill time, best raw price since the fill on that side).
    time_exit: exit after the strategy's max hold h (default: the module's TIME_EXIT).
    Returns 'enter', 'exit' or None. Mirrors Arrays/simulate() exactly."""
    s = _norm(s)
    side, k = s["side"], s["kind"]
    if (pos is not None and (TIME_EXIT if time_exit is None else time_exit)
            and t - pos["entryT"] >= s["h"] * 1000):
        return "exit"
    if not isinstance(F, Tail):
        return None
    if F.age < need(s) and (pos is None or k in ("rev", "turn")):
        return None   # windows not full yet (take-profit and trailing exits need no window)
    sm = s.get("sm", 1)
    if k == "rev":
        q = F.basis(sm) / F.mean(s["w"])
        if pos is None:
            return "enter" if (q <= 1 - s["e"] if side > 0 else q >= 1 + s["e"]) else None
        return "exit" if (q >= 1 + s["x"] if side > 0 else q <= 1 - s["x"]) else None
    if k == "turn":
        x, m, r = F.basis(sm), F.mean(s["w"]), s["r"]
        if pos is None:
            if side > 0:
                lo = F.lo(sm, 10)
                return "enter" if (x < m and lo < m * (1 - r) and x >= lo * (1 + r)) else None
            hi = F.hi(sm, 10)
            return "enter" if (x > m and hi > m * (1 + r) and x <= hi * (1 - r)) else None
        if side > 0:
            return "exit" if (x > m and x <= pos["best"] * (1 - r)) else None
        return "exit" if (x < m and x >= pos["best"] * (1 + r)) else None
    if k == "crash":
        if pos is None:
            if side > 0:
                return "enter" if p <= F.hi(1, s["k"]) * (1 - s["d"]) else None
            return "enter" if p >= F.lo(1, s["k"]) * (1 + s["d"]) else None
        if side > 0:
            return "exit" if p >= pos["entry"] * (1 + s["tp"]) else None
        return "exit" if p <= pos["entry"] * (1 - s["tp"]) else None
    if k == "calm":
        if pos is None:
            return "enter" if F.hi(1, s["k"]) <= F.lo(1, s["k"]) * (1 + s["c"]) else None
        if side > 0:
            return "exit" if p >= pos["entry"] * (1 + s["tp"]) else None
        return "exit" if p <= pos["entry"] * (1 - s["tp"]) else None
    if k == "trend":
        L, r = s["L"], s["r"]
        if pos is None:
            first = F.ps[F.n - 1 - L]
            if side > 0:
                return "enter" if (p >= first * (1 + r) and F.lo(1, L + 1) >= first * (1 - r / 2)) else None
            return "enter" if (p <= first * (1 - r) and F.hi(1, L + 1) <= first * (1 + r / 2)) else None
        if side > 0:
            return "exit" if p <= pos["best"] * (1 - s["tr"]) else None
        return "exit" if p >= pos["best"] * (1 + s["tr"]) else None
    return None


# ---------------------------------------------------------------- backtest features (whole series)
class Arrays:
    """Feature arrays over a whole price series, cached by key. NaN where a window is not full yet."""

    def __init__(self, ts, ps):
        self.ts, self.ps, self.n = ts, ps, len(ps)
        n = self.n
        seg = [0] * n
        s0 = 0
        for i in range(1, n):
            if ts[i] - ts[i - 1] > GAP_MS:
                s0 = i
            seg[i] = s0
        self.seg = seg
        self.starts = sorted(set(seg))
        self.c = {}

    def _mask(self, arr, k):
        """NaN wherever fewer than k samples of the current segment exist (arr is modified in place)."""
        if k > 1:
            for s0 in self.starts:
                e = min(s0 + k - 1, self.n)
                arr[s0:e] = [NAN] * (e - s0)
        return arr

    def after(self, ms):
        """after[i] = first index j with ts[j] >= ts[i] + ms (n when there is none)."""
        key = ("after", ms)
        v = self.c.get(key)
        if v is None:
            ts, n = self.ts, self.n
            v = [0] * n
            j = 0
            for i in range(n):
                t = ts[i] + ms
                while j < n and ts[j] < t:
                    j += 1
                v[i] = j
            self.c[key] = v
        return v

    def seg_end(self):
        """seg_end[i] = last index of the segment holding i (a gap longer than GAP_MS or the end of the data)."""
        v = self.c.get("segend")
        if v is None:
            n, seg = self.n, self.seg
            v = [0] * n
            end = n - 1
            for i in range(n - 1, -1, -1):
                v[i] = end
                if seg[i] == i:
                    end = i - 1
            self.c["segend"] = v
        return v

    def basis(self, sm):
        if sm == 1:
            return self.ps
        key = ("b", sm)
        v = self.c.get(key)
        if v is None:
            ps, seg = self.ps, self.seg
            v = self.c[key] = [_med(ps[max(s, i - sm + 1):i + 1]) for i, s in enumerate(seg)]
        return v

    def stopmed(self):
        """The bot's stop price: median of the last STOP_MEDIAN samples (fewer right after a gap)."""
        v = self.c.get("stopmed")
        if v is None:
            ps, seg, k = self.ps, self.seg, STOP_MEDIAN
            v = self.c["stopmed"] = [_median(ps[max(s, i - k + 1):i + 1]) for i, s in enumerate(seg)]
        return v

    def mean(self, w):
        key = ("m", w)
        v = self.c.get(key)
        if v is None:
            cs = self.c.get("cs")
            if cs is None:
                cs = self.c["cs"] = list(itertools.accumulate(self.ps, initial=0.0))
            arr = [NAN] * (w - 1) + [(a - b) / w for a, b in zip(cs[w:], cs)]
            v = self.c[key] = self._mask(arr, w)
        return v

    def _sparse(self, sm, op):
        key = ("st", sm, op)
        v = self.c.get(key)
        if v is None:
            lv = [self.basis(sm)]
            half = 1
            while half * 2 <= 64:
                prev = lv[-1]
                lv.append(list(map(op, prev[:-half], prev[half:])))
                half *= 2
            v = self.c[key] = lv
        return v

    def roll(self, sm, k, op):
        """op (min/max) of the basis over the last k samples, NaN until the window is full."""
        key = ("r", sm, k, op)
        v = self.c.get(key)
        if v is None:
            L = k.bit_length() - 1
            w2 = 1 << L
            t = self._sparse(sm, op)[L]
            n = self.n
            arr = [NAN] * (k - 1) + list(map(op, t[0:n - k + 1], t[k - w2:n - w2 + 1]))
            v = self.c[key] = self._mask(arr, k + sm - 1)
        return v

    def ratio(self, sm, w):
        key = ("q", sm, w)
        v = self.c.get(key)
        if v is None:
            x, m = self.basis(sm), self.mean(w)
            v = self.c[key] = self._mask([a / b for a, b in zip(x, m)], sm)   # NaN where the mean is NaN
        return v

    def blocks(self, arr_key):
        """Per-block (min, max) of an array for the exit scanner: arr_key = basis sm, or 'stop'."""
        key = ("blk", arr_key)
        v = self.c.get(key)
        if v is None:
            x = self.stopmed() if arr_key == "stop" else self.basis(arr_key)
            nb = (self.n + BLOCK - 1) // BLOCK
            v = self.c[key] = ([min(x[b * BLOCK:(b + 1) * BLOCK]) for b in range(nb)],
                               [max(x[b * BLOCK:(b + 1) * BLOCK]) for b in range(nb)])
        return v

    def entries(self, s):
        """Sorted sample indices where the strategy wants to enter (when flat)."""
        kind, side = s["kind"], s["side"]
        if kind == "rev":
            key = ("E", kind, side, s["sm"], s["w"], s["e"])
        elif kind == "turn":
            key = ("E", kind, side, s["sm"], s["w"], s["r"])
        elif kind == "crash":
            key = ("E", kind, side, s["k"], s["d"])
        elif kind == "calm":
            key = ("E", kind, s["k"], s["c"])
        else:
            key = ("E", kind, side, s["L"], s["r"])
        v = self.c.get(key)
        if v is not None:
            return v
        if kind == "rev":
            q = self.ratio(s["sm"], s["w"])
            thr = 1 - s["e"] if side > 0 else 1 + s["e"]
            v = [i for i, a in enumerate(q) if a <= thr] if side > 0 else [i for i, a in enumerate(q) if a >= thr]
        elif kind == "turn":
            x, m, r = self.basis(s["sm"]), self.mean(s["w"]), s["r"]
            if side > 0:
                lo = self.roll(s["sm"], 10, min)
                v = [i for i, (a, b, c) in enumerate(zip(x, m, lo)) if a < b and c < b * (1 - r) and a >= c * (1 + r)]
            else:
                hi = self.roll(s["sm"], 10, max)
                v = [i for i, (a, b, c) in enumerate(zip(x, m, hi)) if a > b and c > b * (1 + r) and a <= c * (1 - r)]
        elif kind == "crash":
            ps, d = self.ps, s["d"]
            if side > 0:
                hi = self.roll(1, s["k"], max)
                v = [i for i, (a, b) in enumerate(zip(ps, hi)) if a <= b * (1 - d)]
            else:
                lo = self.roll(1, s["k"], min)
                v = [i for i, (a, b) in enumerate(zip(ps, lo)) if a >= b * (1 + d)]
        elif kind == "calm":
            hi, lo, c = self.roll(1, s["k"], max), self.roll(1, s["k"], min), 1 + s["c"]
            v = [i for i, (a, b) in enumerate(zip(hi, lo)) if a <= b * c]
        else:
            L, r, ps = s["L"], s["r"], self.ps
            first = self._mask([NAN] * L + ps[:-L], L + 1)
            if side > 0:
                lo = self.roll(1, L + 1, min)
                v = [i for i, (a, f, b) in enumerate(zip(ps, first, lo)) if a >= f * (1 + r) and b >= f * (1 - r / 2)]
            else:
                hi = self.roll(1, L + 1, max)
                v = [i for i, (a, f, b) in enumerate(zip(ps, first, hi)) if a <= f * (1 - r) and b <= f * (1 + r / 2)]
        self.c[key] = v
        return v

    def exits(self, s):
        """Sorted indices where a position-independent exit fires (mean reversion only), else None."""
        if s["kind"] != "rev":
            return None
        key = ("X", s["side"], s["sm"], s["w"], s["x"])
        v = self.c.get(key)
        if v is None:
            q = self.ratio(s["sm"], s["w"])
            if s["side"] > 0:
                thr = 1 + s["x"]
                v = [i for i, a in enumerate(q) if a >= thr]
            else:
                thr = 1 - s["x"]
                v = [i for i, a in enumerate(q) if a <= thr]
            self.c[key] = v
        return v


def _scan(A, side, f, cap, entry, tp, tr, gate, xs, xblk, stop_thr, stop_ms, a=None):
    """First index j in [f, cap) where a position-dependent exit fires (take-profit vs the fill, trailing stop
    vs the best raw price since the fill, the bot's median stop-loss), else cap. Blocks that cannot fire are
    skipped. gate: None or the mean array of a turn strategy (its trailing exit needs the basis beyond it).
    a: index of the fill (the best price and the stop's grace start there; default f)."""
    ps, ts = A.ps, A.ts
    pmin, pmax = A.blocks(1)
    smin, smax = A.blocks("stop")
    sm_ = A.stopmed()
    xmin, xmax = xblk
    a = f if a is None else a
    best = ps[a]
    grace = ts[a] + STOP_GRACE_MS
    run = None
    j = f
    if side > 0:
        tp_thr = entry * (1 + tp) if tp else INF
        trf = 1 - tr if tr else 0.0
        while j < cap:
            if run is None and j % BLOCK == 0 and j + BLOCK <= cap:
                b = j // BLOCK
                nb = best if best > pmax[b] else pmax[b]
                if xmax[b] < tp_thr and smin[b] > stop_thr and (not tr or xmin[b] > nb * trf):
                    best = nb
                    j += BLOCK
                    continue
            p, x = ps[j], xs[j]
            if p > best:
                best = p
            if x >= tp_thr:
                return j
            if tr and x <= best * trf and (gate is None or x > gate[j]):
                return j
            if sm_[j] <= stop_thr and ts[j] >= grace:
                if run is None:
                    run = ts[j]
                if ts[j] - run >= stop_ms:
                    return j
            else:
                run = None
            j += 1
        return cap
    tp_thr = entry * (1 - tp) if tp else -INF
    trf = 1 + tr if tr else 0.0
    while j < cap:
        if run is None and j % BLOCK == 0 and j + BLOCK <= cap:
            b = j // BLOCK
            nb = best if best < pmin[b] else pmin[b]
            if xmin[b] > tp_thr and smax[b] < stop_thr and (not tr or xmax[b] < nb * trf):
                best = nb
                j += BLOCK
                continue
        p, x = ps[j], xs[j]
        if p < best:
            best = p
        if x <= tp_thr:
            return j
        if tr and x >= best * trf and (gate is None or x < gate[j]):
            return j
        if sm_[j] >= stop_thr and ts[j] >= grace:
            if run is None:
                run = ts[j]
            if ts[j] - run >= stop_ms:
                return j
        else:
            run = None
        j += 1
    return cap


def simulate(A, s, rate, costs, cool_ms, stop=STOP_LOSS, stop_ms=STOP_MS, fillw=None, anchor=1, time_exit=None):
    """All trades of one strategy over the series. Decisions follow decide() with the position filled at the
    next sample; each trade is priced with both fills FILLS samples after the entry and exit signals, and
    with the fill-delay model fillw (weights over FILLS, entry and exit delays independent; an exit never
    fills before the entry: its decision waits for the entry fill). fillw None = next sample (d = 1).
    costs = (open cost, close cost) as fractions of the price. anchor: the sample whose price is the fill the
    exit rules measure from (and the hold time counts from): 0 = the decision sample, 1 = the next one; the
    first exit decision is always on the next sample. time_exit (default TIME_EXIT): exit after the max hold
    h; without it a trade runs until its exit rule fires, and one still open when its segment ends (end of
    the data, or a gap) is closed at that segment's last prices, so losers that never recover still count.
    Returns tuples (entry signal index, last fill index, return per FILLS entry..., model return, hold min)."""
    E = A.entries(s)
    if not E:
        return []
    timed = TIME_EXIT if time_exit is None else time_exit
    fwz = [(d, w) for d, w in zip(FILLS, fillw or (0.0, 1.0, 0.0, 0.0)) if w > 0]
    side, kind = s["side"], s["kind"]
    X = A.exits(s)
    tp = s["tp"] if kind in ("crash", "calm") else None
    tr = s["tr"] if kind == "trend" else (s["r"] if kind == "turn" else None)
    gate = A.mean(s["w"]) if kind == "turn" else None
    sm = s["sm"] if kind == "turn" else 1
    xs, xblk = A.basis(sm), A.blocks(sm)
    ts, ps, seg, n = A.ts, A.ps, A.seg, A.n
    c_open, c_close = costs
    cin = (1 + c_open) if side > 0 else (1 - c_open)
    cout = (1 - c_close) if side > 0 else (1 + c_close)
    D = FILLS[-1]
    cool_at = A.after(cool_ms)
    hold_at = A.after(s["h"] * 1000) if timed else None
    seg_end = None if timed else A.seg_end()
    nE, nX = len(E), (len(X) if X is not None else 0)
    out = []
    k = lo = 0
    while True:
        k = bisect.bisect_left(E, lo, k)
        if k >= nE:
            break
        e = E[k]
        f = e + 1
        if f >= n:
            break
        if seg[f] != seg[e]:          # the fill would land after a gap
            lo = e + 1
            continue
        a0 = e + anchor
        entry = ps[a0] * cin
        if timed:
            cap = hold_at[a0]
        else:
            cap = seg_end[e] - D          # no time exit: the latest exit whose fills stay inside the segment
            if cap <= e + D:              # too close to the end of the data or a gap to price a trade
                lo = e + 1
                continue
        if nX:
            kx = bisect.bisect_left(X, f)
            if kx < nX and X[kx] < cap:
                cap = X[kx]
        if stop:
            stop_thr = entry * (1 - stop) if side > 0 else entry * (1 + stop)
        else:
            stop_thr = -INF if side > 0 else INF
        j = _scan(A, side, f, cap, entry, tp, tr, gate, xs, xblk, stop_thr, stop_ms, a0)
        last = (j if j > e + D else e + D) + D
        if last >= n:
            break                     # still open when the data ends
        lo = j + 1
        if cool_at[e] > lo:
            lo = cool_at[e]           # the next entry waits for the cooldown
        if seg[last] != seg[e]:       # held across a gap: outcome unknown, drop it
            continue
        rs = []
        for d in FILLS:
            a = ps[e + d] * cin
            b = ps[j + d] * cout
            held = (ts[j + d] - ts[e + d]) / 60000
            r = (b / a - 1 + rate * held) if side > 0 else (1 - b / a - rate * held)
            rs.append(r if r > -0.99 else -0.99)
        rm = 0.0
        for de, we in fwz:
            a = ps[e + de] * cin
            te = ts[e + de]
            jj = j if j > e + de else e + de
            for dx, wx in fwz:
                b = ps[jj + dx] * cout
                held = (ts[jj + dx] - te) / 60000
                r = (b / a - 1 + rate * held) if side > 0 else (1 - b / a - rate * held)
                rm += we * wx * (r if r > -0.99 else -0.99)
        out.append((e, last, *rs, rm, (ts[j + 1] - ts[f]) / 60000))
    return out


# ---------------------------------------------------------------- statistics + selection
def stats(rs, holds=None):
    n = len(rs)
    if not n:
        return {"n": 0, "avg": 0.0, "win": 0.0, "sd": 0.0, "hold": 0.0}
    avg = sum(rs) / n
    sd = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1)) if n > 1 else abs(avg)
    return {"n": n, "avg": avg, "win": sum(1 for r in rs if r > 0) / n, "sd": sd,
            "hold": (sum(holds) / n) if holds else 0.0}


def kelly(rs, step=0.05, move=0.0):
    """Share of cash in [0, 1] that maximises the average log growth over these per-entry returns. With
    move > 0 the returns (measured at BT_FRAC of cash) lose move * (f - BT_FRAC) / 2 at share f: the opening
    order's impact grows with the share of cash spent."""
    best_f, best_g = 0.0, 0.0
    for i in range(1, int(round(1 / step)) + 1):
        f = i * step
        adj = move * (f - BT_FRAC) / 2
        g = 0.0
        for r in rs:
            v = 1 + f * (r - adj)
            if v <= 0:
                g = -INF
                break
            g += math.log(v)
        if g > best_g:
            best_f, best_g = f, g
    return best_f


def best_size(edge, move, cap_frac=1.0, ceiling=1.0):
    """Share of cash in [0, 1] maximising expected money per entry when the return falls by move * f / 2 with
    the share f spent (impact of the opening order): f * (edge - move * f / 2) peaks at f = edge / move.
    edge: expected return at f = 0. cap_frac: the most the position cap lets us spend. ceiling: risk limit."""
    if edge <= 0:
        return 0.0
    f = edge / move if move > 0 else 1.0
    return max(0.0, min(f, 1.0, cap_frac, ceiling))


class Table:
    """One strategy's trades with prefix sums, for fast per-fold statistics (base = the fill-model return)."""
    __slots__ = ("te", "tx", "r", "rm", "hold", "cum")

    def __init__(self, trades, ts):
        self.te = [ts[t[0]] for t in trades]
        self.tx = [ts[t[1]] for t in trades]
        cols = list(zip(*trades))
        self.r = [list(cols[2 + d]) for d in range(len(FILLS))]
        self.rm = list(cols[2 + len(FILLS)])
        self.hold = list(cols[-1])
        rb = self.rm
        acc = itertools.accumulate
        self.cum = (list(acc(rb, initial=0.0)), list(acc((x * x for x in rb), initial=0.0)),
                    [list(acc(self.r[d], initial=0.0)) for d in range(BASE + 1, len(FILLS))])

    def before(self, t_ms):
        """(n, sum r, sum r^2, [sum r with each later fill]) of trades closed before t_ms (base fills)."""
        m = bisect.bisect_left(self.tx, t_ms)
        s, q, late = self.cum
        return m, s[m], q[m], [c[m] for c in late]

    def within(self, a, b):
        return bisect.bisect_left(self.te, a), bisect.bisect_left(self.te, b)


LATE = len(FILLS) - BASE - 1
EMPTY = (0, 0.0, 0.0, [0.0] * LATE)


def select(ids, tables, t_ms, min_n):
    """The selection rule: the best strategy among `ids`, judged only on trades closed before t_ms.
    Score = (avg - sd/sqrt(n)) x share of profitable neighbours x late-fill bonus (2/4 .. 4/4)."""
    pre = {i: (tables[i].before(t_ms) if tables[i] is not None else EMPTY) for i in ids}
    best, best_score = None, 0.0
    for i in ids:
        n, s, q, late = pre[i]
        if n < min_n or s <= 0:
            continue
        avg = s / n
        sdev = math.sqrt(max(0.0, (q - n * avg * avg) / (n - 1)))
        lcb = avg - sdev / math.sqrt(n)
        if lcb <= 0:
            continue
        nb = [j for j in NEIGHBORS[i] if tables[j] is not tables[i]]   # an identical run (h without a time exit) proves nothing
        ok = sum(1 for j in nb if pre[j][0] >= min_n // 2 and pre[j][1] > 0)
        stab = ok / len(nb) if nb else 1.0
        if stab < 0.5:
            continue
        survive = sum(1 for x in late if x > 0)          # still profitable with 2 s / 3 s fills
        score = lcb * stab * (2 + survive) / (2 + LATE)
        if score > best_score:
            best, best_score = i, score
    return best, best_score


def plan(t0, t1):
    """Walk-forward test folds [(start, end)] for history t0..t1 (ms) and the out-of-sample trade minimum."""
    H = (t1 - t0) / 1000
    F = min(FOLD_MAX_SEC, max(FOLD_MIN_SEC, H / 6))
    T = min(TRAIN_MAX_SEC, max(TRAIN_MIN_SEC, H / 3))
    K = max(0, int((H - T) // F))
    folds = [(int(t1 - (K - k) * F * 1000), int(t1 - (K - k - 1) * F * 1000)) for k in range(K)]
    min_test = max(3, min(8, int(K * F / 300)))       # one trade per 5 min of test data, 3..8
    return folds, min_test


def min_train(t0, t_ms):
    return max(4, min(8, int((t_ms - t0) / 300000)))  # one trade per 5 min of training data, 4..8


def shrunk(avg, tstat):
    """The out-of-sample mean shrunk by its significance: t^2 / (t^2 + 1) of it is signal."""
    return avg * tstat * tstat / (tstat * tstat + 1)


def sizing(oos, tstat, hours, move, cap_frac=1.0, expected=None):
    """(expected, suggestPct). expected: given (e.g. the live posterior) or the out-of-sample mean shrunk by
    its t-statistic. suggestPct: 100, or the position cap the stock's free shares allow (cap_frac), 10..100.
    Why not Kelly: replaying the bot on the recorded prices (2.6 h, 7 stocks, backtests every 10 min, fills
    priced with the fitted model and with pure latency), log growth was +3326 % / +2295 % at the position cap
    against +3219 % / +2217 % for full Kelly on the posterior-scaled returns, +2873 % / +1832 % for
    confidence-scaled Kelly and +2051 % / +1205 % for half Kelly; live, growth per trade still rose at 100 %."""
    n = len(oos)
    avg = sum(r if r < WIN_CAP else WIN_CAP for r in oos) / n if n else 0.0
    if expected is None:
        expected = shrunk(avg, tstat)
    if expected <= 0:
        return expected, 10
    return expected, int(max(10, min(100, round(min(1.0, cap_frac) * 100))))


def kelly_pct(oos, expected, move):
    """Full-Kelly % of cash for the out-of-sample returns scaled to the expected mean (information only)."""
    n = len(oos)
    if not n or expected is None or expected <= 0:
        return 0
    oos = [r if r < WIN_CAP else WIN_CAP for r in oos]
    avg = sum(oos) / n
    rs = [r * expected / avg for r in oos] if avg > 0 else [r - avg + expected for r in oos]
    return int(round(kelly(rs, move=move) * 100))


def park_stats(ts, ps, rate, hold_min=PARK_HOLD_MIN, look_min=60, mult=1.0):
    """What a dividend park of hold_min minutes made on this history, per unit of money, before impact:
    the dividend rate x minutes plus the price change over hold_min (median of all start times every 30 s,
    so a few jumps do not count; only a falling median counts against the park). vol: 1 s volatility over
    the last 10 minutes. all/dip: whole park results on 1-minute medians, see park_windows (mult: the measured
    dividend multiplier of the DividendGain upgrades)."""
    n = len(ps)
    if n < 120 or not rate or rate <= 0:
        return None
    H = hold_min * 60000
    rs = []
    j = 0
    for i in range(0, n, 30):
        t = ts[i] + H
        if j < i:
            j = i
        while j < n and ts[j] < t:
            j += 1
        if j >= n:
            break
        if ts[j] - t < 30000 and ps[i] > 0:
            rs.append(ps[j] / ps[i] - 1)
    tail = [math.log(ps[k + 1] / ps[k]) for k in range(max(0, n - 601), n - 1) if ps[k] > 0 and ps[k + 1] > 0]
    vol = math.sqrt(sum(x * x for x in tail) / len(tail)) if tail else None
    if not rs:
        return None
    med = sorted(rs)[len(rs) // 2]
    out = {"rate": rate, "hold": hold_min, "median": round(med, 5), "mean": round(sum(rs) / len(rs), 5),
           "n": len(rs), "vol": round(vol, 5) if vol is not None else None,
           "gain": round(rate * hold_min + min(0.0, med), 5)}
    out.update(park_windows(ts, ps, rate * (mult or 1.0), hold_min, look_min))
    return out


def _dist(xs):
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return {"n": n, "mean": round(sum(xs) / n, 5), "median": round(xs[n // 2], 5), "p10": round(xs[n // 10], 5),
            "worst": round(xs[0], 5), "neg": round(sum(1 for x in xs if x < 0) / n, 4)}


def minute_medians(ts, ps):
    """[(minute index, median price)] of 1 s prices, one per minute that has prices."""
    out, k0, buf = [], None, []
    for t, p in zip(ts, ps):
        k = int(t // 60000)
        if k != k0:
            if buf:
                buf.sort()
                out.append((k0, buf[len(buf) // 2]))
            k0, buf = k, []
        if p > 0:
            buf.append(p)
    if buf:
        buf.sort()
        out.append((k0, buf[len(buf) // 2]))
    return out


def park_windows(ts, ps, rate, hold_min, look_min=60):
    """Every park this history allows, on 1-minute medians (a few jumps don't count): bought at a minute's median,
    held hold_min minutes, paid `rate` (the effective dividend rate, upgrades included) each minute on the price
    then. "all": every start minute; "dip": the starts at or below the median of the look_min minutes before (the
    rule parks enter by). Each: n, mean, median, p10, worst, neg (share that lost money), as fractions of the money
    parked; None without windows. Windows across a gap in the recording are left out."""
    mm = minute_medians(ts, ps)
    H, L = int(round(hold_min)), int(round(look_min))
    if H < 1 or L < 1 or len(mm) < L + H + 1:
        return {"all": None, "dip": None, "look": L}
    ks = [k for k, _ in mm]
    ms = [m for _, m in mm]
    pre = [0.0]
    for m in ms:
        pre.append(pre[-1] + m)
    allr, dip = [], []
    for i in range(L, len(ms) - H):
        m0 = ms[i]
        if m0 <= 0 or ks[i + H] - ks[i] != H or ks[i] - ks[i - L] != L:
            continue
        r = ms[i + H] / m0 - 1 + rate * (pre[i + H + 1] - pre[i + 1]) / m0
        allr.append(r)
        w = sorted(ms[i - L:i])
        if m0 <= w[len(w) // 2]:
            dip.append(r)
    return {"all": _dist(allr), "dip": _dist(dip), "look": L}


def cools(cool_ms):
    """(buy cooldown, short cooldown) in ms from a number (both), a (buy, short) pair or a dict."""
    if isinstance(cool_ms, dict):
        b = cool_ms.get("buy", cool_ms.get("long"))
        s = cool_ms.get("short", b)
        b = s if b is None else b
        return float(b or 0), float(s or 0)
    if isinstance(cool_ms, (tuple, list)):
        return float(cool_ms[0] or 0), float((cool_ms[1] if len(cool_ms) > 1 else cool_ms[0]) or 0)
    return float(cool_ms or 0), float(cool_ms or 0)


def evaluate(ts, ps, rate, costs, cool_ms, stop=STOP_LOSS, stop_ms=STOP_MS, breathe=None, move=LIVE_MOVE,
             fillw=None, anchor=1, time_exit=None):
    """Nested walk-forward evaluation of the whole grid on one stock. cool_ms: number or (buy, short) pair.
    fillw: fill-delay weights over FILLS (None = next sample); anchor, time_exit: see simulate(). Returns
    {"long": info, "short": info, "folds": K, "hours": h}; info["row"] exists when the side is valid."""
    out = {"folds": 0, "hours": (ts[-1] - ts[0]) / 3.6e6 if len(ts) > 1 else 0.0}
    if len(ps) < 60:
        return out
    te = TIME_EXIT if time_exit is None else time_exit
    out["timeExit"] = te
    cool_l, cool_s = cools(cool_ms)
    A = Arrays(ts, ps)
    tables, same = [], {}
    for s in GRID:
        # without a time exit the max hold h changes nothing: strategies that differ only in h share one run
        key = None if te else tuple(sorted((k, v) for k, v in s.items() if k != "h"))
        if key is not None and key in same:
            tables.append(same[key])
            continue
        tr = simulate(A, s, rate, costs, cool_l if s["side"] > 0 else cool_s, stop, stop_ms, fillw, anchor, te)
        tables.append(Table(tr, ts) if tr else None)
        if key is not None:
            same[key] = tables[-1]
        if breathe:
            breathe()
    t0, t1 = ts[0], ts[-1] + 1
    folds, min_test = plan(t0, t1)
    out["folds"] = len(folds)
    hours = out["hours"]
    for side, name in ((1, "long"), (-1, "short")):
        ids = [i for i, s in enumerate(GRID) if s["side"] == side]
        oos = [[] for _ in FILLS]
        oos_m, oos_hold = [], []
        per_fold, picks = [], []
        for a, b in folds:
            i, _ = select(ids, tables, a, min_train(t0, a))
            picks.append(i)
            if i is None:
                per_fold.append(None)
                continue
            lo, hi = tables[i].within(a, b)
            for d in range(len(FILLS)):
                oos[d].extend(tables[i].r[d][lo:hi])
            oos_m.extend(tables[i].rm[lo:hi])
            oos_hold.extend(tables[i].hold[lo:hi])
            per_fold.append(sum(tables[i].rm[lo:hi]) if hi > lo else None)
        final, score = select(ids, tables, t1 + 10 ** 9, min_train(t0, t1))
        test = stats(oos_m, oos_hold)
        avgs = [stats(oos[d])["avg"] for d in range(len(FILLS))]
        n = test["n"]
        capped = stats([r if r < WIN_CAP else WIN_CAP for r in oos_m])
        tstat = capped["avg"] / (capped["sd"] / math.sqrt(n)) if n > 1 and capped["sd"] > 0 else 0.0
        traded = [x for x in per_fold if x is not None]
        pos_folds = sum(1 for x in traded if x > 0)
        info = {"test": test, "avgs": avgs, "t": tstat, "traded": len(traded), "positive": pos_folds,
                "folds": len(folds), "picks": picks, "final": final, "oos": oos_m}
        if not folds:
            why = "history too short for a walk-forward fold"
        elif final is None:
            why = "no strategy passes in-sample"
        elif n < min_test:
            why = f"only {n} out-of-sample trades (need {min_test})"
        elif capped["avg"] <= 0:
            why = f"out-of-sample loss ({capped['avg'] * 100:+.1f}%/trade, trades capped at +{WIN_CAP:.0%})"
        elif tstat < MIN_T:
            why = f"out-of-sample edge not significant (t={tstat:.1f})"
        elif pos_folds * 2 < len(traded):
            why = f"profitable in only {pos_folds} of {len(traded)} folds"
        else:
            why = None
        info["why"] = why
        if final is not None:
            tbl = tables[final]
            info["train"] = stats(tbl.rm, tbl.hold)
            info["strat"] = GRID[final]
            info["score"] = score
        if why is None:
            expected, pct = sizing(oos_m, tstat, hours, move)
            info["row"] = {
                "strat": dict(GRID[final]), "label": describe(GRID[final], te), "train": info["train"], "test": test,
                "score": score, "valid": True, "expected": round(expected, 4), "expectedModel": round(expected, 4),
                "suggestPct": pct, "kellyPct": kelly_pct(oos_m, expected, move),
                "oos": {"avgModel": round(test["avg"], 4), "avgCapped": round(capped["avg"], 4),
                        "sdCapped": round(capped["sd"], 4), "avgSameTick": round(avgs[0], 4),
                        "avg1s": round(avgs[1], 4), "avg2s": round(avgs[2], 4), "avg3s": round(avgs[3], 4),
                        "sd": round(test["sd"], 4), "t": round(tstat, 2), "folds": len(folds),
                        "traded": len(traded), "positive": pos_folds,
                        "samePick": sum(1 for p in picks if p == final),
                        "fillW": [round(w, 3) for w in (fillw or (0.0, 1.0, 0.0, 0.0))], "anchor": anchor},
                "rate": round(info["train"]["n"] / hours, 2) if hours > 0 else 0.0,
                "hours": round(hours, 2), "move": move,
                "_oos": oos_m,
            }
        out[name] = info
    return out


def _num(v, default=None):
    """float of a number or numeric string (market.json sends availableShares as a string), else default."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


# ---------------------------------------------------------------- learning from live fills
OPENS = ("buypercent", "buymax", "shortpercent", "shortmax")
CLOSES = ("sellmax", "sellpercent", "covermax", "coverpercent", "closemax", "closepercent")


def read_jsonl(path, max_bytes=8_000_000):
    """Records of a JSON-lines file (bad lines skipped, only the last max_bytes read); [] when missing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def read_moves(path):
    """(prior, {stock: move}) from the bot's impact_model.json (fitted on its own fills); None when missing."""
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
        prior = float(m.get("prior") or LIVE_MOVE)
        out = {}
        for s, e in (m.get("stocks") or {}).items():
            v = e.get("move") if isinstance(e, dict) else None
            if isinstance(v, (int, float)) and 0.001 < v < 0.5:
                out[s] = float(v)
        return (prior if 0.001 < prior < 0.5 else LIVE_MOVE), out
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def live_record(rows, move_of=lambda s: LIVE_MOVE, since_ms=0):
    """From the bot's trades.jsonl records: fill noise per stock and fill-accurate swing round trips.
    fills: {stock: [z]} with z = log(impact-free fill / seen price) for entries (the position's average price
    at the close, the game's authoritative number) and exits. trips: {(stock, side): [(label, r, t, f)]} with
    r = realized pnlPct / 100 and f = share of cash spent on the entry."""
    fills, trips, opens = collections.defaultdict(list), collections.defaultdict(list), {}
    for r in rows:
        if not isinstance(r, dict) or r.get("source") != "bot" or r.get("status") != "done":
            continue
        s, a, t = r.get("stock"), r.get("action"), r.get("t") or 0
        if not s or t < since_ms:
            continue
        if a in OPENS:
            opens[s] = r
            continue
        if a not in CLOSES:
            continue
        o = opens.pop(s, None)
        try:
            long = a.startswith("sell") or (a.startswith("close") and o is not None and o["action"].startswith("buy"))
            mv = move_of(s)
            seen, fx = float(r.get("sentPrice") or 0), float(r.get("fillExit") or 0)
            if seen > 0 and fx > 0:
                fills[s].append(math.log(fx / (1 - mv / 2 if long else 1 + mv / 2) / seen))
            if o is None:
                continue
            x = float(o.get("impactX") or BT_FRAC)
            seen, fe = float(o.get("sentPrice") or 0), float(r.get("fillEntry") or o.get("fillEntry") or 0)
            if seen > 0 and fe > 0:
                fills[s].append(math.log(fe / (1 + mv * x / 2 if long else 1 - mv * x / 2) / seen))
            if o.get("kind") == "swing" and r.get("pnlPct") is not None:
                label = str(o.get("why") or "").split(" · ")[0]
                trips[(s, "long" if long else "short")].append((label, float(r["pnlPct"]) / 100, t, x))
        except (TypeError, ValueError, ZeroDivisionError, AttributeError, KeyError):
            continue
    return dict(fills), dict(trips)


def fill_weights(zs, p_same, prior=FILL_PRIOR, pi0=TICK_PI):
    """Fill-delay weights over FILLS for one stock: (pi, 1 - pi, 0, 0), pi = share of fills at the decision
    tick. zs: live fill noise (log impact-free fill / seen price) of the current regime. A fill within FILL_TOL
    of the seen price landed "on" it, unless a next-tick fill simply did not move (probability p_same,
    measured on the series): EM for pi with `prior` pseudo-fills at pi0. Live fills can only lower pi below
    pi0: a backtest that assumed 90 % decision-tick fills ranked and picked worse in the replay (log growth
    -9 %) even where fills really landed on the tick 90 % of the time; assuming 55 % keeps the picks robust."""
    n = len(zs)
    k = sum(1 for z in zs if abs(z) < FILL_TOL)
    pi = pi0
    for _ in range(60):
        den = pi + (1 - pi) * p_same
        pi = (k * (pi / den if den > 0 else 0.0) + prior * pi0) / (n + prior)
    pi = min(pi0, max(0.0, pi))
    return (pi, 1.0 - pi, 0.0, 0.0)


def same_share(ps, tol=FILL_TOL):
    """Share of samples whose next price is within tol (a next-tick fill that looks on-price)."""
    n = len(ps)
    if n < 10:
        return 0.0
    hit = tot = 0
    for i in range(0, n - 1, 3):                # every 3rd sample is plenty
        a, b = ps[i], ps[i + 1]
        if a > 0 and b > 0:
            tot += 1
            hit += abs(math.log(b / a)) < tol
    return hit / tot if tot else 0.0


def live_posterior(expected, sd, n_oos, trips, label, move=LIVE_MOVE):
    """Normal-normal update of a row's expected per-entry return (fraction at BT_FRAC of cash) with the live
    round trips of its stock and side: [(label, r, t, f)]. Same label weight 1, others LIVE_OTHER_W; each r is
    moved to the BT_FRAC basis (its own size changed the opening impact). Prior sd: the model's sampling error
    and MODEL_RISK x |expected|; per-trade sd: the out-of-sample sd."""
    w = [(1.0 if lab == label else LIVE_OTHER_W, min(WIN_CAP, r + move * (f - BT_FRAC) / 2)) for lab, r, _, f in trips]
    n_eff = sum(x for x, _ in w)
    out = {"n": len(trips), "same": sum(1 for lab, *_ in trips if lab == label), "prior": round(expected, 4)}
    if n_eff <= 0:
        out.update(avg=None, posterior=round(expected, 4), disabled=False)
        return out
    mean = sum(x * r for x, r in w) / n_eff
    s2 = max(sd, 0.05) ** 2
    tau2 = s2 / max(n_oos, 1) + (MODEL_RISK * abs(expected)) ** 2 + 1e-9
    prec = 1 / tau2 + n_eff / s2
    post = (expected / tau2 + n_eff * mean / s2) / prec
    out.update(avg=round(mean, 4), weight=round(n_eff, 2), posterior=round(post, 4),
               postSd=round(math.sqrt(1 / prec), 4), disabled=post <= 0)
    return out


# ---------------------------------------------------------------- store + brain
class Brain:
    def __init__(self, data_dir, note=print):
        self.db_path = Path(data_dir) / "prices.db"
        self.note = note
        self.lock = threading.RLock()
        self.series = collections.defaultdict(lambda: collections.deque(maxlen=KEEP_SEC))
        self.choices = {}                           # stock -> {"long": row, "short": row} (valid rows only)
        self.signals = {}                           # stock -> {"long": sig, "short": sig}
        self.report = {}                            # stock -> diagnostics of the last backtest
        self.park_hold = PARK_HOLD_MIN              # park_stats windows: the bot's parkHoldMin,
        self.park_look = 60                         # ... its parkLookMin
        self.div_mult = 1.0                         # ... and the measured dividend multiplier (DividendGain)
        self._medc = {}                             # median_since cache
        self.last_bt = 0
        self.bt_running = False
        self.bt_info = "waiting for data"
        self.stop_loss = STOP_LOSS                  # mirror of the bot's swing stop (None = none)
        self.bt_time_exit = None                    # TIME_EXIT the current choices were selected with
        self.stop_ms = STOP_MS
        self.moves = {}                             # stock -> our price impact per 100 % of cash
        self.fill_model = {}                        # stock -> {"w": fill-delay weights, "n": live fills, ...}
        self.live = {}                              # (stock, side) -> live round trips [(label, r, t, f)]
        self.cools = (0.0, 0.0)                     # (buy, short) cooldown ms of the last backtest
        self.last_entry = None                      # the last best_entry() decision (for the dashboard)
        self.tick_since = None                      # wall ms of the first ingest_tick() sample (None: history path)
        self.ticks = 0                              # samples that arrived through ingest_tick()
        self.db_error = None                        # last prices.db problem (None when writes work)
        self._tails = {}
        self._pending = []                          # samples not yet written to prices.db
        self._writer = None
        self._wake = threading.Event()
        self._warned = False
        self._said = 0.0
        self._pruning = False
        self._counts = {}                           # stock -> (first t, count, newest t counted, epoch)
        self._epoch = 0                             # bumped when samples are deleted
        self._init_db()

    def _say(self, msg, level="warn"):
        try:
            try:
                self.note(msg, level)
            except TypeError:
                self.note(msg)
        except Exception:
            pass

    @staticmethod
    def _stocks(con):
        """Stocks in the database: one index seek per stock instead of a scan of every row."""
        out, row = [], con.execute("SELECT MIN(stock) FROM p").fetchone()
        while row and row[0] is not None:
            out.append(row[0])
            row = con.execute("SELECT MIN(stock) FROM p WHERE stock > ?", (row[0],)).fetchone()
        return out

    def _init_db(self):
        try:
            con = sqlite3.connect(self.db_path, timeout=5)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS p (stock TEXT, t INTEGER, price REAL, "
                            "PRIMARY KEY (stock, t)) WITHOUT ROWID")
                con.commit()
                cut = int(time.time() * 1000) - KEEP_SEC * 1000
                for stock in self._stocks(con):
                    for t, price in con.execute("SELECT t, price FROM p WHERE stock = ? AND t >= ? ORDER BY t",
                                                (stock, cut)):
                        self._push(stock, t, price)
            finally:
                con.close()
        except sqlite3.Error as e:   # the bot still runs on the prices it records from now on
            self.db_error = f"prices.db unusable: {e}"
            self._say(self.db_error)

    def _push(self, stock, t, price):
        """Store one sample (call with the lock held). False when it is already there: a sample within
        TICK_DUP_MS of a stored one is the same tick (history.json repeats market.json's tick +-1 ms). An
        older sample that is missing (a tick the live path skipped) is inserted in place, up to BACKFILL_MS
        behind the newest."""
        ser = self.series[stock]
        if not ser or t > ser[-1][0] + TICK_DUP_MS:
            ser.append((t, price))
            return True
        if t >= ser[-1][0] - TICK_DUP_MS or t < ser[-1][0] - BACKFILL_MS:
            return False
        k = len(ser) - 1
        while k >= 0 and ser[k][0] > t:
            k -= 1
        if (k >= 0 and t - ser[k][0] <= TICK_DUP_MS) or ser[k + 1][0] - t <= TICK_DUP_MS:
            return False
        if len(ser) == ser.maxlen:
            if k < 0:
                return False            # older than everything kept
            ser.popleft()
            k -= 1
        ser.insert(k + 1, (t, price))
        self._tails.pop(stock, None)    # the cached live features miss this sample
        return True

    def _wake_writer(self):
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._write_loop, name="brain-db-writer", daemon=True)
            self._writer.start()

    def ingest(self, history):
        """Add new samples from the game's history.json. Memory is updated at once; prices.db is written by
        a background thread, so the bot's loop never waits on the disk or on a chart read. Samples that
        ingest_tick() already stored are skipped; ticks it missed are filled in."""
        n = 0
        with self.lock:
            for st in history.get("stocks") or []:
                sid = st.get("stockId")
                if not sid:
                    continue
                smp = st.get("samples") or []
                ser = self.series.get(sid)
                k, have = 0, []
                if ser:   # samples are oldest-first: only a suffix can be new (or fill a missed tick)
                    lim = ser[-1][0] - (BACKFILL_MS if self.tick_since is not None else 0)
                    k = len(smp)
                    while k > 0 and (smp[k - 1].get("timeMs") or 0) > lim:
                        k -= 1
                    if self.tick_since is not None:      # stored times in that window, ascending
                        for x in reversed(ser):
                            if x[0] < lim - TICK_DUP_MS:
                                break
                            have.append(x[0])
                        have.reverse()
                for s in smp[k:]:
                    t, pr = s.get("timeMs"), s.get("price")
                    if not (t and pr and pr > 0):
                        continue
                    if have:                             # the tick path stored it already: skip fast
                        h = bisect.bisect_left(have, t)
                        if (h < len(have) and have[h] - t <= TICK_DUP_MS) or (h and t - have[h - 1] <= TICK_DUP_MS):
                            continue
                    if self._push(sid, t, pr):
                        self._pending.append((sid, t, pr))
                        n += 1
            if n:
                self._wake_writer()
        if n:
            self._wake.set()
        return n

    def ingest_tick(self, stock, t_ms=None, price=None):
        """One live price from market.json, ~0.65 s before history.json repeats it: ingest_tick(stock, t_ms,
        price), or ingest_tick(t_ms, {stock: price}) for a whole tick. Idempotent: a sample within
        TICK_DUP_MS of a stored one is the same tick and is kept once (history.json's copy is skipped later).
        Cheap: memory only (the database write is batched in the background); signal() then decides on it.
        Returns the number of new samples."""
        if isinstance(t_ms, dict):
            items = [(s, stock, p) for s, p in t_ms.items()]
        else:
            items = [(stock, t_ms, price)]
        n = 0
        with self.lock:
            for sid, t, pr in items:
                try:
                    t, pr = int(t), float(pr)
                except (TypeError, ValueError):
                    continue
                if sid and t > 0 and pr > 0 and math.isfinite(pr) and self._push(sid, t, pr):
                    self._pending.append((sid, t, pr))
                    n += 1
            if n:
                self.ticks += n
                if self.tick_since is None:
                    self.tick_since = int(time.time() * 1000)
                    self.last_bt = 0            # re-run the backtests with the tick-path fill model soon
                self._wake_writer()
        if n:
            self._wake.set()
        return n

    def _write_loop(self):
        while True:
            self._wake.wait()
            time.sleep(WRITE_BATCH_SEC)         # one transaction for a burst of ticks
            self._wake.clear()
            if not self.flush():
                time.sleep(5)   # database unavailable: the samples wait in memory, retry later

    def flush(self):
        """Write the waiting samples to prices.db now. Never raises: when the database is busy or cannot be
        written (disk full), the samples stay in memory for the next try and False is returned."""
        with self.lock:
            rows, self._pending = self._pending, []
        if not rows:
            return True
        try:
            con = sqlite3.connect(self.db_path, timeout=WRITE_TIMEOUT)
            try:
                with con:   # commit, or roll back on error
                    con.executemany("INSERT OR IGNORE INTO p VALUES (?, ?, ?)", rows)
            finally:
                con.close()
        except sqlite3.Error as e:
            with self.lock:
                self._pending[:0] = rows
                extra = len(self._pending) - PENDING_MAX
                if extra > 0:
                    del self._pending[:extra]   # oldest first; the in-memory series keeps the last 6 h anyway
                n = len(self._pending)
            self.db_error = f"prices.db write failed: {e} ({n} samples waiting in memory)"
            if time.time() - self._said > 300:
                self._said = time.time()
                self._warned = True
                self._say(self.db_error)
            return False
        if self.db_error:
            if self._warned:
                self._say(f"prices.db writable again ({len(rows)} waiting samples saved)", "info")
            self.db_error = None
            self._warned = False
            self._said = 0.0
        return True

    def feat(self, stock):
        """Feature accessor for decide() at the newest sample of `stock` (also callable as F(span))."""
        with self.lock:
            ser = self.series.get(stock)
            if not ser:
                return Tail([], [])
            last_t = ser[-1][0]
            c = self._tails.get(stock)
            if c and c[0] == last_t:
                return c[1]
            n = len(ser)
            tail = [ser[i] for i in range(max(0, n - TAIL), n)]
            F = Tail([x[0] for x in tail], [x[1] for x in tail])
            self._tails[stock] = (last_t, F)
            return F

    def last(self, stock):
        ser = self.series.get(stock)
        return ser[-1] if ser else None

    def signal(self, stock, side, pos):
        """Live decision of the stock's chosen strategy for one side ('long'/'short').
        pos: None or {entry, entryT, best} for an open position on that side."""
        row = (self.choices.get(stock) or {}).get(side)
        last = self.last(stock)
        if not row or not last:
            return None, None
        sig = decide(row["strat"], last[0], last[1], self.feat(stock), pos)
        if pos is None and sig == "enter" and row.get("disabled"):
            sig = None                  # live results fell clearly below the model: no new entries
        self.signals.setdefault(stock, {})[side] = sig
        return sig, row

    def money_per_entry(self, stock, side, cash, avail_shares=None, price=None):
        """(expected money of one entry, % of cash to use, note) for the chosen strategy right now.
        Always the largest size: 100 % of cash, trimmed only by the free shares (availableShares, number or
        string; the game trims an order to it) and by the impact rule (our impact grows with the share of cash,
        so beyond edge / move more cash adds no money; with these edges that never binds). The expectation
        only decides take or skip (money > 0). Rows disabled by their live record are worth 0."""
        row = (self.choices.get(stock) or {}).get(side)
        cash = _num(cash, 0.0)
        if not row or cash <= 0:
            return 0.0, 0, None
        if row.get("disabled"):
            lf = row.get("liveFills") or {}
            return 0.0, 0, (f"disabled by live fills: {lf.get('n', 0)} trades avg "
                            f"{(lf.get('avg') or 0) * 100:+.1f}% vs model {row.get('expectedModel', 0) * 100:+.1f}%")
        price = _num(price)
        avail_shares = _num(avail_shares)
        if price is None:
            last = self.last(stock)
            price = last[1] if last else 0.0
        move = row.get("move", LIVE_MOVE)
        cap_frac = 1.0
        if avail_shares is not None and price > 0:
            cap_frac = min(1.0, max(0.0, avail_shares) * price / cash)
        edge0 = row["expected"] + move * BT_FRAC / 2           # return before the opening order's own impact
        f = best_size(edge0, move, cap_frac, 1.0)
        if f <= 0:
            return 0.0, 0, None
        pct = int(max(10, min(100, round(f * 100))))
        f = min(pct / 100, cap_frac)
        money = cash * f * (edge0 - move * f / 2)
        note = None
        if cap_frac < 1.0:
            note = (f"volume binds: {avail_shares:,.0f} shares x {price:.4g} = {avail_shares * price:,.0f} "
                    f"({cap_frac * 100:.0f}% of cash)")
        return money, pct, note

    def park_value(self, stock, cash, park_cash=None, avail_shares=None, price=None, hold_min=PARK_HOLD_MIN):
        """(expected money, % of cash, note) of parking idle cash in a dividend stock for hold_min minutes:
        x = min(park_cash, free shares x price); money = x x (dividend rate x minutes + the median price change
        over that hold when it falls - opening impact move x f / 2 - closing impact move / 2), f = x / cash.
        None when the stock pays no dividend, is too jumpy (PARK_MAX_VOL) or has no backtest yet."""
        st = (self.report.get(stock) or {}).get("park")
        cash = _num(cash, 0.0)
        if not st or cash <= 0:
            return None
        if st.get("vol") is None or st["vol"] > PARK_MAX_VOL:
            return None
        price = _num(price)
        if price is None:
            last = self.last(stock)
            price = last[1] if last else 0.0
        x = _num(park_cash, cash * PARK_FRAC)
        x = min(x, cash)
        av = _num(avail_shares)
        if av is not None and price > 0:
            x = min(x, max(0.0, av) * price)
        if x <= 0:
            return None
        move = (self.report.get(stock) or {}).get("move") or LIVE_MOVE
        f = x / cash
        gain = st["rate"] * hold_min + min(0.0, st["median"]) * (hold_min / st["hold"])
        money = x * (gain - move * f / 2 - move / 2)
        note = (f"park {x:,.0f} for ~{hold_min:.0f} min: dividend {st['rate'] * 100:.2f}%/min, "
                f"impact {(move * f / 2 + move / 2) * 100:.1f}%")
        return money, int(max(1, min(100, math.floor(f * 100)))), note

    def best_entry(self, cands, cash, now=None, cool_ms=None, avail=None, prices=None, park=None,
                   park_cash=None, hold_min=PARK_HOLD_MIN):
        """Which entry to take now on each cooldown slot (buy: long swings and dividend parks; short: shorts).

        cands: the swing entries the bot could open now: (stock, side) pairs or dicts {"stock", "side",
          optional "avail" (availableShares, number or string), "price"}; the bot's own filters (cooldown
          ready, no position, not paused) are applied before. cash: cash now. now: ms (logged only).
        cool_ms: cooldown after an entry, number or (buy, short) pair (default: the last backtest's).
        avail / prices: optional {stock: availableShares} / {stock: price} for candidates given as pairs.
        park: stocks the bot may park idle cash in now (unlocked, no position), or True for every stock
          with a dividend in the last backtest, or None (no parking). park_cash: cash a park may use
          (default PARK_FRAC of cash). hold_min: minutes a park is expected to stay open.

        Each swing candidate is worth money_per_entry() (expected money after costs, impact, live record and
        the volume cap, always at the largest size those allow); a park is worth park_value(). Parking uses
        the buy slot. A slot takes its best candidate whenever it is worth money, unless waiting is clearly
        better: with the side's validated strategies signalling at rates lambda_k (entries/hour in the
        history) worth m_k, the best policy earns R* per second of the slot (renewal reward: R(T) =
        sum(lambda_k m_k) / (1 + C sum(lambda_k)) over strategies worth >= T, C = the cooldown); taking a
        candidate worth m uses the slot for C, so the slot waits only when m < R* x C / WAIT_CLEAR.

        Returns {"pick": {"stock", "side" ('long'/'short'/'park'), "money", "pct", "note"} or None,
        "picks": the taken candidate per slot (a long or park and a short can both go: separate cooldowns),
        "ranked": all candidates, "sides": {"long" (= buy slot)/"short": {"threshold", "rate", "take", "why"}},
        "why": str}."""
        cl, cs = cools(cool_ms) if cool_ms is not None else self.cools
        cd = {"long": max(cl, 1000.0) / 1000.0, "short": max(cs, 1000.0) / 1000.0}   # seconds
        cash = _num(cash, 0.0)
        ranked = []
        for c in cands or []:
            if isinstance(c, dict):
                s, side = c.get("stock"), c.get("side")
                av, pr = c.get("avail"), c.get("price")
            else:
                s, side = c[0], c[1]
                av = pr = None
            if av is None and avail:
                av = avail.get(s)
            if pr is None and prices:
                pr = prices.get(s)
            money, pct, note = self.money_per_entry(s, side, cash, av, pr)
            ranked.append({"stock": s, "side": side, "money": money, "pct": pct, "note": note})
        if park:
            pool = [s for s, r in self.report.items() if r.get("park")] if park is True else list(park)
            for s in pool:
                pv = self.park_value(s, cash, park_cash, (avail or {}).get(s), (prices or {}).get(s), hold_min)
                if pv is not None:
                    ranked.append({"stock": s, "side": "park", "money": pv[0], "pct": pv[1], "note": pv[2]})
        ranked.sort(key=lambda x: -x["money"])
        slot_of = {"long": "long", "park": "long", "short": "short"}
        sides, picks = {}, []
        for side in ("long", "short"):
            opts = []
            for s, ch in self.choices.items():
                row = ch.get(side)
                if not row or row.get("disabled"):
                    continue
                m, _, _ = self.money_per_entry(s, side, cash)
                lam = (row.get("rate") or 0.0) / 3600.0           # entries per second
                if m > 0 and lam > 0:
                    opts.append((m, lam))
            opts.sort(key=lambda x: -x[0])
            best_r, acc_lm, acc_l = 0.0, 0.0, 0.0
            for m, lam in opts:                                  # best threshold = best prefix of the sorted list
                acc_lm += lam * m
                acc_l += lam
                best_r = max(best_r, acc_lm / (1 + cd[side] * acc_l))
            thr = best_r * cd[side] / WAIT_CLEAR
            top = next((x for x in ranked if slot_of.get(x["side"]) == side), None)
            take = bool(top and top["money"] > 0 and top["money"] >= thr)
            if top is None:
                why = "no candidate"
            elif top["money"] <= 0:
                why = "nothing worth money"
            elif take:
                why = (f"{top['stock']} {top['side']} worth {top['money']:,.0f} (waiting worth "
                       f"{best_r * cd[side]:,.0f})")
            else:
                why = (f"wait: {top['stock']} {top['side']} worth {top['money']:,.0f} < 1/{WAIT_CLEAR:g} of the "
                       f"{best_r * cd[side]:,.0f} a likely better signal within the {cd[side]:.0f}s cooldown is worth")
            sides[side] = {"threshold": round(thr, 2), "rate": round(best_r * 3600, 2), "take": take, "why": why}
            if take:
                picks.append(top)
        picks.sort(key=lambda x: -x["money"])
        pick = picks[0] if picks else None
        out = {"pick": pick, "picks": picks, "ranked": ranked, "sides": sides, "now": now,
               "why": "; ".join(f"{'buy' if k == 'long' else k}: {v['why']}" for k, v in sides.items())}
        self.last_entry = out
        return out

    def volatility(self, stock, sec=600):
        """Std-dev of 1-sample log returns over the last `sec` seconds (fraction)."""
        ser = self.series.get(stock)
        if not ser or len(ser) < 30:
            return None
        cut = ser[-1][0] - sec * 1000
        ps = []
        for t, p in reversed(ser):
            if t < cut:
                break
            ps.append(p)
        ps.reverse()
        rs = [math.log(ps[i + 1] / ps[i]) for i in range(len(ps) - 1) if ps[i] > 0 and ps[i + 1] > 0]
        if len(rs) < 20:
            return None
        m = sum(rs) / len(rs)
        return math.sqrt(sum((r - m) ** 2 for r in rs) / len(rs))

    def median_since(self, stock, sec):
        """Median of the 1 s prices of the last `sec` seconds (None with fewer than half of them); cached 5 s."""
        now = time.time()
        hit = self._medc.get((stock, sec))
        if hit and now - hit[0] < 5:
            return hit[1]
        ser = self.series.get(stock)
        val = None
        if ser:
            cut = ser[-1][0] - sec * 1000
            ps = []
            for t, p in reversed(ser):
                if t < cut:
                    break
                if p > 0:
                    ps.append(p)
            if len(ps) >= sec * 0.5:
                ps.sort()
                val = ps[len(ps) // 2]
        self._medc[(stock, sec)] = (now, val)
        return val

    # one row per candle: index seeks for the first/last sample of each bucket, range scans for high/low
    # (no sort of the raw samples, which a GROUP BY needs)
    _CANDLES = (
        "WITH RECURSIVE k(b) AS (SELECT ?4 UNION ALL SELECT b + 1 FROM k WHERE b < ?5) "
        "SELECT g.b, o.price, g.hi, g.lo, c.price FROM (SELECT b, "
        "(SELECT MIN(t) FROM p WHERE stock = ?1 AND t >= max(b * ?3, ?2) AND t < (b + 1) * ?3) AS t0, "
        "(SELECT MAX(t) FROM p WHERE stock = ?1 AND t >= max(b * ?3, ?2) AND t < (b + 1) * ?3) AS t1, "
        "(SELECT MAX(price) FROM p WHERE stock = ?1 AND t >= max(b * ?3, ?2) AND t < (b + 1) * ?3) AS hi, "
        "(SELECT MIN(price) FROM p WHERE stock = ?1 AND t >= max(b * ?3, ?2) AND t < (b + 1) * ?3) AS lo "
        "FROM k) AS g "
        "JOIN p AS o ON o.stock = ?1 AND o.t = g.t0 JOIN p AS c ON c.stock = ?1 AND c.t = g.t1 ORDER BY g.b")

    def candles(self, stock, since_ms, buckets=600, until_ms=None, step_ms=None):
        """OHLC candles from the database for the chart, aggregated inside SQLite (1 s candles when the span
        between since_ms and until_ms fits in `buckets` seconds). With step_ms the candles are that long (whole
        seconds, at least 1 s) and, when the span holds more than `buckets` of them, only the newest are returned."""
        until_ms = until_ms or 2 ** 62
        con = sqlite3.connect(self.db_path)
        try:
            first = con.execute("SELECT t FROM p WHERE stock = ? AND t >= ? AND t <= ? ORDER BY t LIMIT 1",
                                (stock, since_ms, until_ms)).fetchone()
            if not first:
                return [], 1
            last = con.execute("SELECT t FROM p WHERE stock = ? AND t >= ? AND t <= ? ORDER BY t DESC LIMIT 1",
                               (stock, since_ms, until_ms)).fetchone()
            if step_ms:
                step = max(1000, int(step_ms) // 1000 * 1000)
                if (last[0] - first[0]) // step > buckets:
                    since_ms = last[0] - buckets * step
                    first = (since_ms,)
            else:
                step = max(1000, int((last[0] - first[0]) / buckets // 1000) * 1000 or 1000)
            rows = con.execute(self._CANDLES, (stock, since_ms, step, first[0] // step, last[0] // step)).fetchall()
        finally:
            con.close()
        return [{"time": b * step // 1000, "open": o, "high": h, "low": l, "close": c} for b, o, h, l, c in rows], step // 1000

    def first_time(self, stock):
        """(time of the first sample, number of samples) of a stock in the database. The full count runs once;
        later calls only count what was added since (the chart asks on every refresh)."""
        con = sqlite3.connect(self.db_path)
        try:
            c = self._counts.get(stock)
            if c is None or c[3] != self._epoch:
                epoch = self._epoch
                mn, cnt, mx = con.execute("SELECT MIN(t), COUNT(*), MAX(t) FROM p WHERE stock = ?", (stock,)).fetchone()
            else:
                epoch = c[3]
                add, mx = con.execute("SELECT COUNT(*), MAX(t) FROM p WHERE stock = ? AND t > ?", (stock, c[2])).fetchone()
                mn, cnt, mx = c[0], c[1] + add, (c[2] if mx is None else mx)
        finally:
            con.close()
        if mn is not None:
            self._counts[stock] = (mn, cnt, mx, epoch)
        return mn, cnt

    def prune(self, keep_days=30, wait=False):
        """Delete samples older than keep_days. Runs in a background thread (index range deletes, one hour of
        one stock per transaction) so the bot's loop never waits on it; wait=True runs it inline."""
        if self._pruning:
            return
        self._pruning = True
        cut = int(time.time() * 1000) - keep_days * 86400000

        def work():
            try:
                con = sqlite3.connect(self.db_path, timeout=5)
                try:
                    for stock in self._stocks(con):
                        while True:
                            lo = con.execute("SELECT MIN(t) FROM p WHERE stock = ?", (stock,)).fetchone()[0]
                            if lo is None or lo >= cut:
                                break
                            with con:
                                con.execute("DELETE FROM p WHERE stock = ? AND t < ?", (stock, min(cut, lo + 3600000)))
                            self._epoch += 1
                            time.sleep(0.005)
                finally:
                    con.close()
            except sqlite3.Error as e:
                self._say(f"prices.db prune failed: {e}")
            finally:
                self._pruning = False

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _move(v):
        """Impact per 100 % of cash from a number (fraction) or a list of measurements (% moves, as in
        impact.json); noisy or missing measurements fall back to DEFAULT_MOVE."""
        if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0:
            return float(v)
        if isinstance(v, (list, tuple)) and len(v) >= 5:
            xs = sorted(float(x) for x in v)
            med = xs[len(xs) // 2] / 100
            q1, q3 = xs[len(xs) // 4] / 100, xs[3 * len(xs) // 4] / 100
            if 0 < med < 1 and (q3 - q1) < med:   # consistent enough to trust (never true for $STEAM's noise)
                return med
        return DEFAULT_MOVE

    def calibrate(self, now_ms=None, since_ms=None):
        """Execution model from the bot's own live fills in the data folder: (prior move, {stock: move},
        fill noise {stock: [z]}, round trips {(stock, side): [...]}, impact model found?). Only trades of the
        last LIVE_MAX_AGE_H hours; fill noise only since since_ms (the start of the current decision path)."""
        d = self.db_path.parent
        mv = read_moves(d / "impact_model.json")
        prior, per = mv if mv else (LIVE_MOVE, {})
        now_ms = now_ms or int(time.time() * 1000)
        since = now_ms - LIVE_MAX_AGE_H * 3600000
        rows = read_jsonl(d / "trades.jsonl")
        fills, trips = live_record(rows, lambda s: per.get(s, prior), since)
        if since_ms and since_ms > since:     # fill timing of the current path only (the round trips all count)
            fills, _ = live_record(rows, lambda s: per.get(s, prior), since_ms)
        return prior, per, fills, trips, mv is not None

    def run_backtests(self, stocks, rates, cost, cool_ms, impact=None, cash=None, avail=None):
        """Pick the best validated long and short strategy per stock (runs in a background thread).
        Costs are fitted on live fills: price impact only (impact_model.json in the data folder, per stock,
        LIVE_MOVE without it); fills follow the per-stock fill-delay model: prior TICK_PI once the bot feeds
        market.json ticks through ingest_tick() (then only fills since then count), else HIST_PI.
        cost: legacy spread per side, ignored (the game's trade code has no spread); an explicit
        (open, close) pair is added on top of the impact. impact: legacy {stock: move}, used only without
        impact_model.json (never below LIVE_MOVE). cool_ms: the buy cooldown, or a (buy, short) pair / dict
        (the game has separate shared cooldowns). cash, avail {stock: availableShares}: optional, to size
        rows against the position cap."""
        if self.bt_running:
            return
        self.bt_running = True
        extra = (float(cost[0]), float(cost[1])) if isinstance(cost, (tuple, list)) else (0.0, 0.0)
        if impact is not None:
            self.moves = {k: self._move(v) for k, v in impact.items()}
        cool = cools(cool_ms)
        self.cools = cool
        cash_v = _num(cash, 0.0)

        def work():
            started = time.time()
            slot = [time.perf_counter()]

            def breathe():  # hand the GIL to the bot's main loop every ~10 ms
                if time.perf_counter() - slot[0] > 0.01:
                    time.sleep(0.001)
                    slot[0] = time.perf_counter()

            ticking = self.tick_since is not None
            pi0 = TICK_PI if ticking else HIST_PI
            te = TIME_EXIT                  # one setting for the whole run
            try:
                try:
                    prior, per, fills, trips, fitted = self.calibrate(since_ms=self.tick_since if ticking else None)
                except Exception as e:      # a broken log never stops the backtests
                    prior, per, fills, trips, fitted = LIVE_MOVE, {}, {}, {}, False
                    self._say(f"live fills unreadable, using defaults: {type(e).__name__}: {e}")
                self.live = trips
                result, report, waiting, fm = {}, {}, [], {}
                for sid in stocks:
                    with self.lock:
                        ser = list(self.series.get(sid, []))
                    if ser:
                        cut = ser[-1][0] - BACKTEST_SEC * 1000
                        ser = [x for x in ser if x[0] >= cut]
                    hours = (ser[-1][0] - ser[0][0]) / 3.6e6 if len(ser) > 1 else 0.0
                    if hours * 3600 < MIN_HISTORY_SEC or len(ser) < MIN_HISTORY_SEC * 0.8:
                        waiting.append((sid, hours))
                        report[sid] = {"hours": hours, "why": f"needs {MIN_HISTORY_SEC // 60} min of history"}
                        continue
                    ts, ps = [x[0] for x in ser], [x[1] for x in ser]
                    move = per.get(sid, prior) if fitted else max(LIVE_MOVE, self.moves.get(sid, 0.0))
                    costs = (move * BT_FRAC / 2 + extra[0], move / 2 + extra[1])
                    zs = fills.get(sid, [])
                    p_same = same_share(ps[-3600:])
                    fw = fill_weights(zs, p_same, pi0=pi0)
                    anchor = 0 if fw[0] >= 0.5 else 1
                    fm[sid] = {"w": [round(w, 3) for w in fw], "n": len(zs), "anchor": anchor,
                               "on": sum(1 for z in zs if abs(z) < FILL_TOL), "pSame": round(p_same, 3),
                               "path": "tick" if ticking else "history"}
                    rate = _num((rates or {}).get(sid), 0.0) or 0.0
                    ev = evaluate(ts, ps, rate, costs, cool, self.stop_loss, self.stop_ms,
                                  breathe, move, fw, anchor, te)
                    pick, rep = {}, {"hours": round(hours, 2), "folds": ev["folds"], "move": move,
                                     "fills": fm[sid]}
                    for side in ("long", "short"):
                        info = ev.get(side) or {}
                        row = info.get("row")
                        if row:
                            oos = row.pop("_oos")
                            lf = live_posterior(row["expectedModel"], row["oos"]["sdCapped"], row["test"]["n"],
                                                trips.get((sid, side), []), row["label"], move)
                            row["liveFills"] = lf
                            row["expected"] = lf["posterior"]
                            row["disabled"] = bool(lf["disabled"])
                            row["valid"] = not row["disabled"]
                            cap_frac = 1.0
                            av = _num((avail or {}).get(sid))
                            if cash_v and av is not None and ps[-1] > 0:
                                cap_frac = min(1.0, max(0.0, av) * ps[-1] / cash_v)
                            _, row["suggestPct"] = sizing(oos, row["oos"]["t"], hours, move, cap_frac,
                                                          expected=row["expected"])
                            row["kellyPct"] = kelly_pct(oos, row["expected"], move)
                            if cap_frac < 1.0:
                                row["volumeNote"] = (f"volume binds: {av:,.0f} shares x {ps[-1]:.4g} = "
                                                     f"{av * ps[-1]:,.0f} ({cap_frac * 100:.0f}% of cash)")
                            pick[side] = row
                        rep[side] = {"why": info.get("why"), "test": info.get("test"), "t": info.get("t"),
                                     "avgs": info.get("avgs"),
                                     "label": describe(info["strat"], te) if info.get("strat") else None}
                    result[sid] = pick
                    rep["park"] = park_stats(ts, ps, rate, self.park_hold, self.park_look, self.div_mult)
                    report[sid] = rep
                    breathe()
                with self.lock:
                    self.choices = result
                    self.report = report
                    self.fill_model = fm
                    self.bt_time_exit = te
                n = sum(len(v) for v in result.values())
                off = sum(1 for v in result.values() for r in v.values() if r.get("disabled"))
                if result:
                    msg = f"{n} validated strategies on {len(result)} stocks ({time.time() - started:.1f}s)"
                    if off:
                        msg += f", {off} paused by their live fills"
                    nlive = sum(len(v) for v in trips.values())
                    if nlive:
                        msg += f"; learned from {nlive} live round trips"
                    msg += (f"; decisions on market.json ticks (fills assumed <= {TICK_PI:.0%} at the seen tick)" if ticking
                            else f"; decisions on history.json (fills assumed <= {HIST_PI:.0%} at the seen tick)")
                    if waiting:
                        msg += f"; {len(waiting)} still collecting history (need {MIN_HISTORY_SEC // 60} min)"
                else:
                    have = max([h for _, h in waiting] or [0.0]) * 60
                    msg = (f"nothing can be valid yet: walk-forward validation needs {MIN_HISTORY_SEC // 60} min "
                           f"of 1 s prices per stock (have {have:.0f} min)")
                self.bt_info = msg
            except Exception as e:  # never kill the bot over a backtest
                self.bt_info = f"backtest error: {type(e).__name__}: {e}"
            finally:
                self.last_bt = time.time() if ticking == (self.tick_since is not None) else 0
                self.bt_running = False

        threading.Thread(target=work, daemon=True).start()

    def history_minutes(self, stock):
        ser = self.series.get(stock)
        return (ser[-1][0] - ser[0][0]) / 60000 if ser and len(ser) > 1 else 0
