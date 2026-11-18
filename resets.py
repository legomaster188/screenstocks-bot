"""Daily price resets: the game's one fixed, predictable event.

Some stocks are reset to their base price once a day at a fixed UTC time.  The game announces it as scheduled
news `reset:<stock>:<uuid>:<ms>` (direction "crash", target = the base price), but often hours ahead, before the
game connects, and news published before that is never exported.  So the bot also builds these events from
`resetSchedule`.  Every recording from 2026-10-27 to 2026-11-13 has the same times:
$PLAIN 19:00, $IDXA 23:00 and $IDXB 15:00 UTC (the bot missed the 2026-11-13 $IDXA reset: 78 -> 10 -> 549 -> 1.1 -> 70).

Recorded resets (1 s ticks) share one shape:
  T+0.2..1.2 s   the first tick after T is the target (10, or 20 for $IDXB), down from 78..754;
  next 2..25 s   most then shoot toward the price cap (549..1000) and collapse again within a few ticks (to 1..40);
  next minutes   recovery to the old level: $PLAIN and $IDXA in 2..5 min, $IDXB in about 2 hours.

The legs (the long one is the money; the stock's volume cap is what limits it):
  short   (optional) open resetLeadSec before T, sized so a cover at the price cap costs at most resetShortRiskPct
          of cash; covered on the first tick at or under the cover line (the reset tick), or when no reset came.
  long    bought on the first tick after T at or under resetBuyMaxX x target; sold into the spike (at least
          resetTakeX x the fill and resetTakeStartX x the pre-reset price, in the first resetBurstSec s), else at
          resetRecover % of the way back to the pre-reset price, else after resetHoldMin.  No stop: the collapse
          after the spike comes back.  Exempt from the circuit breaker while the event runs.
  again   one more buy once the buy cooldown allows, while the price is under resetRebuyFrac x the pre-reset price.
  stack   (resetStackFrac > 0) while the long is held and the price is still under resetStackFrac x the pre-reset
          price, buy more on every buy cooldown, up to resetStackMax times: the volume cap limits one order, not the
          position, so each add is another full order at a deep price.
  trail   (resetTrailPct > 0) at the recovery line, don't sell: ride it and sell once the price falls resetTrailPct %
          under the highest price since (or at the spike, or after resetHoldMin).

Bot hooks (stockbot.py): tick -> sync(), run_events -> step(), reservations -> reserve(), run_park -> blocks_park().
"""
import datetime as dt

DAY_MS = 86400000
PREP_MS = 180000          # the "pre" phase (clear the stock, short) starts this long before the reset
OWN_MS = 240000           # swing trading leaves the stock alone from this long before the reset
CREATE_AHEAD_MS = 45 * 60000
TWIN_MS = 120000          # a real reset news item within this of a scheduled one is the same reset
MOVED_MS = 12 * 3600000   # ... and one further away but within this means the game moved that day's reset
FINAL = ("done", "missed", "skipped")


def _num(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x and abs(x) != float("inf") else default


def schedule(cfg):
    """[(stock, minute of the UTC day, target)] from "$PLAIN 19:00 10; $IDXA 23:00 10" (or a list of such entries);
    bad entries are skipped."""
    out = []
    items = cfg.get("resetSchedule") or []
    if isinstance(items, str):
        items = items.split(";")
    for item in items:
        parts = str(item).replace(",", " ").split()
        if len(parts) < 3:
            continue
        try:
            hh, mm = parts[1].split(":")[:2]
            mod = int(hh) * 60 + int(mm)
            tgt = float(parts[2])
        except ValueError:
            continue
        if 0 <= mod < 1440 and tgt > 0:
            out.append((parts[0], mod, tgt))
    return out


def hhmm(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M")


def next_at(mod, now, grace_ms):
    """Server ms of the next time-of-day `mod` (minutes) at or after now - grace."""
    base = int((now - grace_ms) // DAY_MS * DAY_MS + mod * 60000)
    return base if base >= now - grace_ms else base + DAY_MS


def price_before(bot, stock, at):
    """The last recorded price before server time `at` (None if the bot has no sample from then)."""
    ser = getattr(bot.brain, "series", {}).get(stock)
    best = None
    if ser:
        for t, p in reversed(ser):
            if t < at:
                best = p
                break
    return best


def is_reset(ev):
    return bool(ev.get("reset"))


def sync(bot):
    """Build scheduled reset events and mark the game's own reset news for the reset logic."""
    c = bot.cfg
    if not c.get("resets"):
        return
    now = bot.server_now()
    info = bot.stock_info()
    evs = bot.events
    for ev in list(evs.values()):            # the game's own news ("reset:..." or a crash at a scheduled time)
        if ev.get("synthetic") or ev.get("reset") or ev.get("dir") != "crash":
            continue
        twin = next((e for e in evs.values() if e.get("synthetic") and e["stock"] == ev["stock"]
                     and abs(e["at"] - ev["at"]) < TWIN_MS), None)
        if twin is not None:
            if ev["phase"] == "new":
                ev.update(phase="skipped", note="same reset as the daily schedule")
                twin["published"] = ev.get("published")
            continue
        if str(ev.get("id", "")).startswith("reset:"):
            ev["reset"] = True
            for e in evs.values():               # the game announced another time: its news wins over the schedule
                if e.get("synthetic") and e["phase"] == "new" and e["stock"] == ev["stock"] \
                        and abs(e["at"] - ev["at"]) < MOVED_MS:
                    e.update(phase="skipped", note=f"the game announced {hhmm(ev['at'])} UTC instead")
    grace = (_num(c.get("resetBuyWindowSec"), 300) + 60) * 1000
    for stock, mod, tgt in schedule(c):
        if stock not in info:
            continue
        at = next_at(mod, now, grace)
        if at - now > CREATE_AHEAD_MS:
            continue
        eid = f"reset@{stock}@{at}"
        if eid in evs:
            continue
        real = next((e for e in evs.values() if not e.get("synthetic") and e["stock"] == stock
                     and e.get("dir") == "crash" and abs(e["at"] - at) < TWIN_MS), None)
        if real is not None:
            real["reset"] = True
            continue
        if any(str(e.get("id", "")).startswith("reset:") and e["stock"] == stock and abs(e["at"] - at) < MOVED_MS
               for e in evs.values()):
            continue                             # announced at another time today: that one is traded instead
        evs[eid] = {"id": eid, "stock": stock, "dir": "crash", "target": tgt, "at": at, "published": None,
                    "phase": "new", "note": "", "start": None, "low": None, "high": None, "tries": 0,
                    "reset": True, "synthetic": True}
        bot.note(f"📅 RESET {stock} → {tgt:.4g} at {hhmm(at)} UTC (daily schedule, in {(at - now) / 60000:.0f} min)",
                 "event")
    detect(bot, now, info, evs)


DETECT_REF_MS = 10 * 60000   # the reference is the median price of this long before ...
DETECT_SKIP_MS = 60000       # ... leaving out the last minute (the crash itself)
DETECT_EVERY_MS = 15000      # the reference is recomputed this often


def _detect_ref(bot, stock, now):
    """Median price of the 10 minutes before the last minute (cached per stock for 15 s)."""
    cache = bot.__dict__.setdefault("_crash_ref", {})
    hit = cache.get(stock)
    if hit and now - hit[0] < DETECT_EVERY_MS:
        return hit[1]
    ser = getattr(bot.brain, "series", {}).get(stock)
    ref = None
    if ser:
        xs = []
        for t, p in reversed(ser):
            if t < now - DETECT_SKIP_MS - DETECT_REF_MS:
                break
            if t <= now - DETECT_SKIP_MS:
                xs.append(p)
        if len(xs) >= 30:
            xs.sort()
            ref = xs[len(xs) // 2]
    cache[stock] = (now, ref)
    return ref


def detect_opts(c, stock):
    """Crash settings of one stock: resetDetectBy {"$WHALE": {"frac", "gapMin", "maxFrac", "badFill"}} over the
    global resetDetectFrac / resetDetectGapMin / resetDetectMaxFrac (badFill: a buy filled above this x the old
    price is sold at cost)."""
    by = c.get("resetDetectBy") or {}
    o = by.get(stock) if isinstance(by, dict) else None
    o = o if isinstance(o, dict) else {}
    return {"frac": _num(o.get("frac"), _num(c.get("resetDetectFrac"), 0.3)),
            "gapMin": _num(o.get("gapMin"), _num(c.get("resetDetectGapMin"), 10)),
            "maxFrac": _num(o.get("maxFrac"), _num(c.get("resetDetectMaxFrac"), 0)),
            "badFill": _num(o.get("badFill"), 0.5),
            "buyWindowSec": _num(o.get("buyWindowSec"), _num(c.get("resetBuyWindowSec"), 300)),
            "burstSec": _num(o.get("burstSec"), _num(c.get("resetBurstSec"), 45)),
            "recover": _num(o.get("recover"), _num(c.get("resetRecover"), 85))}


def detect_stocks(c):
    """The stocks the detector watches (empty set = all): resetDetectStocks plus the resetDetectBy ones."""
    only = c.get("resetDetectStocks") or ""
    only = set(only.replace(",", " ").split()) if isinstance(only, str) else set(only)
    by = c.get("resetDetectBy") or {}
    if only and isinstance(by, dict):
        only |= set(by)
    return only


def fast_crash(c, stock):
    """True for a stock the detector trades with its own settings (resetDetectBy, e.g. $WHALE: a 60 % dip every ~8
    min that lasts seconds). Parks leave it alone: a park bought at its usual price rides every dip and blocks the
    dip buys."""
    by = c.get("resetDetectBy") or {}
    return bool(c.get("resetDetect")) and isinstance(by, dict) and stock in by


def fast_buy_reserved(bot, now):
    """True while the buy slot is kept free for the next dip of a fast-crash stock (resetDetectBy, e.g. $WHALE: a
    dip every 3-15 min that lasts seconds, while any other buy holds the slot for the whole buy cooldown). Other
    buys (parks) get the slot only in the first resetFastOpenSec after a dip: the next one came 187 s later at the
    earliest in 19 h of $WHALE (95 % of them 277 s or more)."""
    c = bot.cfg
    if not (c.get("resetFastKeepBuy") and c.get("resetDetect") and c.get("resets") and c.get("events")):
        return False
    by = c.get("resetDetectBy") or {}
    if not isinstance(by, dict):
        return False
    info = bot.stock_info()
    seen = bot.__dict__.get("_crash_seen", {})
    open_ms = _num(c.get("resetFastOpenSec"), 170) * 1000
    for s in by:
        if s not in info or not (info.get(s) or {}).get("unlocked", True):
            continue
        t = seen.get(s)
        if t is None or not 0 <= now - t <= open_ms:
            return True
    return False


def detect(bot, now, info, evs):
    """A crash nobody announced (no news, not on the schedule): the price falls under resetDetectFrac x its median of
    the 10 minutes before. $PLAIN does this many times a day (spike to the cap, collapse to 1-150, back within
    minutes). It is traded like a reset that just happened: buy now, sell into a spike or the recovery."""
    c = bot.cfg
    if not c.get("resetDetect"):
        return
    only = detect_stocks(c)
    last = bot.__dict__.setdefault("_crash_seen", {})
    for stock, si in info.items():
        if only and stock not in only:
            continue
        if not si.get("unlocked", True):
            continue
        ser = getattr(bot.brain, "series", {}).get(stock)
        if not ser:
            continue
        t, price = ser[-1]
        o = detect_opts(c, stock)
        frac = o["frac"]
        if now - t > 5000 or now - last.get(stock, 0) < o["gapMin"] * 60000:
            continue
        ref = _detect_ref(bot, stock, now)
        if not ref or price > frac * ref:
            continue
        if any(e["stock"] == stock and e["phase"] not in FINAL
               and (abs(e["at"] - now) < 30 * 60000 or (e.get("reset") and e["phase"] != "new"))
               for e in evs.values()):
            continue                             # an event on this stock is being traded, or one is due within 30 min
                                                 # (a daily reset hours away used to block every crash buy all day)
        last[stock] = now
        eid = f"reset~{stock}~{int(now)}"
        evs[eid] = {"id": eid, "stock": stock, "dir": "crash", "target": price, "at": int(now), "published": int(now),
                    "phase": "wait", "note": "crash seen: buying", "start": ref, "low": price, "high": None,
                    "tries": 0, "reset": True, "synthetic": True, "detected": True}
        if c.get("eventFreeCash") and not fast_crash(c, stock) and bot.side_ready_by("buy", now + 2000):
            bot.free_cash(stock, f"free cash for the {stock} crash", need=1.03 * bot.cash_need(stock, c["eventPercent"],
                                                                                              price=price))
        bot.note(f"📉 CRASH {stock} {ref:.4g} → {price:.4g} ({(price / ref - 1) * 100:.0f}%): buying the rebound", "event")


def watching(bot):
    """True while the crash detector is live on an unlocked stock (its buy needs the buy cooldown free)."""
    c = bot.cfg
    if not (c.get("resetDetect") and c.get("resets") and c.get("events")):
        return False
    only = detect_stocks(c)
    return any((not only or s in only) and si.get("unlocked", True) for s, si in bot.stock_info().items())


def blocks_park(bot, stock, now):
    """True when a reset on `stock` is due within a park's expected hold (or one is running)."""
    c = bot.cfg
    if not c.get("resets"):
        return False
    horizon = (_num(c.get("parkHoldMin"), 30) + 5) * 60000
    for ev in bot.events.values():
        if ev.get("reset") and ev["stock"] == stock and ev["phase"] not in FINAL and ev["at"] - now < horizon:
            return True
    for s, mod, _ in schedule(c):
        if s == stock and 0 <= next_at(mod, now, 0) - now < horizon:
            return True
    return False


def reserve(bot, ev, now, buy_cd, short_cd):
    """(buy slot, short slot, stock owned) a reset event needs now."""
    c = bot.cfg
    at, ph = ev["at"], ev["phase"]
    if ph in FINAL:
        return False, False, False
    unlocked = (bot.stock_info().get(ev["stock"]) or {}).get("unlocked", True)
    own = now >= at - OWN_MS or ph not in ("new",)
    if not (c["events"] and c.get("resets") and unlocked):
        return False, False, own
    lead = _num(c.get("resetLeadSec"), 25) * 1000
    short_res = bool(c.get("resetShort")) and ph in ("new", "pre") and at - lead - short_cd - 10000 <= now < at
    # the buy needs the slot at T, and the cash: no swing shorts (they lock cash that free_cash can't free) either
    buy_res = ph in ("new", "pre", "short", "covering", "wait") and now >= at - buy_cd - 10000
    buy_res = buy_res or (ph == "long" and bool(ev.get("stackOpen")))      # keep the slot for the next add
    return buy_res, short_res or buy_res, own


def _clear(bot, ev, s, p, mg, price):
    """Before the reset: close the bot's other positions on the stock (a swing short is closed too, not adopted: the
    reset short is sized for the spike risk), sell your long, take over your short as the short leg. True while busy."""
    other = bot.managed.get(s)
    if other is not None and other is not mg:
        if bot._busy_with_other_event(ev, other):
            ev["note"] = "stock busy with another event"
        elif other["state"] == "open":
            bot.close_position(s, "reset coming, close", urgent=True)
            ev["note"] = f"closing the bot's {other['side']} before the reset"
        return True
    if other is None:
        if p.get("owned", 0) > 0:
            bot.raw_exit(s, "sellmax", "reset coming, sell")
            ev["note"] = "selling your long before the reset"
            return True
        if p.get("shorted", 0) > 0:
            bot._adopt(s, "short", p, ev, "crash", price)
            bot.managed[s]["breakerExempt"] = True
            ev.update(phase="short", note="short open (your short)")
            return True
    return False


def _miss(bot, ev, s, price, why):
    """A crash price the buy could not take: say why (once per event and reason)."""
    ev.setdefault("misses", {})
    if why not in ev["misses"]:
        ev["misses"][why] = round(price, 6)
        bot.note(f"{s} crash at {price:.4g}: not bought ({why})", "warn")


def step(bot, ev, now, P, pos, buy_ready, short_ready, allow):
    c = bot.cfg
    s, price = ev["stock"], P[ev["stock"]]
    at, tgt = ev["at"], ev["target"]
    mg = bot._mine(ev)
    p = pos.get(s, {})
    info = bot.stock_info().get(s) or {}
    window = (detect_opts(c, s)["buyWindowSec"] if ev.get("detected") else _num(c.get("resetBuyWindowSec"), 300)) * 1000

    if ev["phase"] == "new":
        if now >= at + window:
            ev.update(phase="missed", note="seen after the reset")
            return
        if not allow:
            ev["note"] = bot._off_note()
            return
        if not info.get("unlocked", True):
            ev["note"] = "stock locked"
            return
        if now < at - PREP_MS:
            ev["note"] = f"resets at {hhmm(at)} UTC"
            return
        start = price if now < at else (price_before(bot, s, at) or None)
        if start and start <= _num(c.get("resetBuyMaxX"), 3) * tgt:
            ev.update(phase="skipped", note=f"already at {start:.4g}: nothing to gain")
            return
        ev.update(phase="pre" if now < at else "wait", start=start,
                  note="getting ready" if now < at else "late: buying the reset if it is still low")

    if ev["phase"] == "pre":
        if mg is not None and mg["side"] == "short":
            if mg["state"] == "open":
                ev.update(phase="short", note="short open")
            return                              # opening: confirm_opens marks it open or drops it
        if not allow:
            ev.update(phase="done", note=bot._off_note())
            return
        if now < at and _clear(bot, ev, s, p, mg, price):
            return
        if c["eventFreeCash"] and now >= at - 60000 and not ev.get("freed"):
            ev["freed"] = True
            need = 1.03 * bot.cash_need(s, c["eventPercent"], price=tgt)
            if bot.free_cash(s, f"free cash for the {s} reset", need=need):
                ev["note"] = "freeing cash"
        if now >= at:
            ev.update(phase="wait", note="waiting for the reset tick")
        else:
            lead = _num(c.get("resetLeadSec"), 25) * 1000
            if c.get("resetShort") and at - lead <= now <= at - 2000 and ev["tries"] < 4:
                cap = _num(info.get("priceCap"), 1000.0) or 1000.0
                pct = min(_num(c["eventPercent"], 100), _num(c.get("resetShortRiskPct"), 30) / max(0.05, cap / price - 1))
                drop = 1 - 2 * tgt / price
                # the cover's result must come back before the buy can go out, and the reset price lasts about one
                # export tick: on a slow round trip the buy lands a tick late (replays: $IDXA bought at 28 not 10,
                # -$355M against a +$3M short). Short only when the safe size is big enough to be worth that.
                min_pct = max(1.0, _num(c.get("resetShortMinPct"), 20))
                if drop * 100 < c["crashMinDrop"]:
                    ev["note"] = "no short: the price is already low"
                elif pct < min_pct:
                    ev["note"] = f"no short: only {pct:.1f}% is safe at {price:.4g} (needs {min_pct:.0f}%)"
                elif short_ready and not bot.pending_open("short") and not bot.pending_for(s) and s not in bot.managed:
                    if bot.open_position(s, "short", pct, "event", f"reset {s} → {tgt:.4g} at {hhmm(at)} UTC",
                                         edge=drop, ev=ev["id"], leg="crash", breakerExempt=True):
                        ev["tries"] += 1
                        ev["note"] = "shorting"
                    elif bot.refusal:
                        ev["note"] = f"not shorting: {bot.refusal}"
            elif now < at - lead:
                ev["note"] = f"short {int(lead / 1000)} s before {hhmm(at)} UTC"
            return

    if ev["phase"] == "short":
        if mg is None:
            ev.update(phase="wait", note="short gone")
        elif mg["state"] != "open":
            return
        elif now < at:
            return
        else:
            start = ev.get("start") or mg["entry"]
            ev["low"] = min(ev.get("low") or price, price)
            line = max(_num(c.get("resetCoverX"), 3) * tgt, 0.25 * start)
            reason = None
            if price <= line:
                reason = "reset tick"
            elif now >= at + _num(c.get("resetNoResetSec"), 8) * 1000 and price >= 0.6 * start:
                reason = "no reset"
            elif now >= at + 180000:
                reason = "timeout"
            if reason and bot.close_position(s, f"reset: cover, {reason} ({(1 - price / mg['entry']) * 100:+.0f}%)",
                                              urgent=True, target=False):
                ev.update(phase="covering", note=f"covering: {reason}")
            return

    if ev["phase"] == "covering":
        if mg is not None:
            if mg["state"] == "open":
                ev["phase"] = "short"           # the cover failed: the short rules decide again
            return
        ev.update(phase="wait", note="covered; buying the reset")

    if ev["phase"] in ("wait", "again"):
        first = ev["phase"] == "wait"
        if mg is not None:
            if mg["state"] == "open" and mg["side"] == "long":
                ev.update(phase="long", note="holding the reset" if first else "holding the second entry")
                if not first:
                    ev["rebuys"] = ev.get("rebuys", 0) + 1
            return
        limit = at + window if first else at + _num(c.get("resetRebuyMin"), 10) * 60000
        if not allow or now > limit or ev.get("rtries", 0) >= 6:
            ev.update(phase="done", note=("no buy at the reset price" if first else "no second entry")
                      if allow else bot._off_note())
            return
        if now < at:
            return
        start = ev.get("start")
        ev["low"] = min(ev.get("low") or price, price)
        if first and start and now >= at + 60000 and ev["low"] > 0.6 * start:
            ev.update(phase="done", note="no reset today")
            return
        if first:
            cap = _num(c.get("resetBuyMaxX"), 3) * tgt
            mf = detect_opts(c, s)["maxFrac"]
            if ev.get("detected") and start and mf > 0:
                cap = min(cap, mf * start)
            if price > cap:
                ev["note"] = f"waiting for ≤ {cap:.4g}"
                return
        elif not start or price > _num(c.get("resetRebuyFrac"), 0.35) * start:
            return
        if buy_ready and not bot.pending_open("buy") and not bot.pending_for(s) and p.get("shorted", 0) <= 0 \
                and s not in bot.managed:
            edge = max(1.0, _num(c.get("resetRecover"), 85) / 100 * ((start or price * 3) / price - 1))
            what = "buy the reset" if first else "second reset entry"
            if bot.open_position(s, "long", c["eventPercent"], "event",
                                 f"{what}: {price:.4g} (was {start:.4g})" if start else f"{what}: {price:.4g}",
                                 edge=edge, ev=ev["id"], leg="rebound", breakerExempt=True):
                ev["rtries"] = ev.get("rtries", 0) + 1
                ev["note"] = "buying"
            elif bot.refusal:
                ev["note"] = f"not buying: {bot.refusal}"
            else:
                _miss(bot, ev, s, price, "late in the price tick" if not bot.tick_window_open() else
                      "blocked after a failed order" if not bot.can_open(s, "buy") else "no cash")
        else:
            _miss(bot, ev, s, price, "buy cooldown" if not buy_ready else "a buy is still pending"
                  if bot.pending_open("buy") else "an order on this stock is pending" if bot.pending_for(s)
                  else "a short is open on it" if p.get("shorted", 0) > 0 else "the bot holds it already")
        return

    if ev["phase"] == "long":
        if mg is None:
            ev.update(phase="done", note="position gone")
            return
        if mg["state"] != "open":
            return
        fill = mg["entry"]
        start = ev.get("start") or 0.0
        ev["high"] = max(ev.get("high") or price, price)
        if not mg.get("fillChecked"):
            mg["fillChecked"] = True
            bf = detect_opts(c, s)["badFill"] if ev.get("detected") else 0.5
            mg["badFill"] = bool(start) and fill > bf * start
        do = detect_opts(c, s) if ev.get("detected") else None
        # $WHALE snaps back to its 1000 cap seconds after a dip, then sags: waiting out a 45 s burst before the
        # recovery sell gave up a sixth of the crash profit (61 dips, 9 h: +119 % a trade vs +144 % with no wait)
        burst = now - at <= (do["burstSec"] if do else _num(c.get("resetBurstSec"), 45)) * 1000
        recover = do["recover"] if do else _num(c.get("resetRecover"), 85)
        take = max(_num(c.get("resetTakeX"), 8) * fill, _num(c.get("resetTakeStartX"), 1.5) * start)
        trail = _num(c.get("resetTrailPct"), 0)
        stack_x = _num(c.get("resetStackFrac"), 0)
        ev["stackOpen"] = (stack_x > 0 and bool(start) and price <= stack_x * start and allow and not mg.get("badFill")
                           and ev.get("adds", 0) < int(_num(c.get("resetStackMax"), 3)))
        reason = None
        if mg.get("badFill"):
            if price >= fill * 0.98:
                reason = "filled high; out at cost"
        elif burst and _num(c.get("resetTakeX"), 8) > 0 and price >= take:
            reason = "spike"
        elif ev.get("trailHigh"):
            ev["trailHigh"] = max(ev["trailHigh"], price)
            if price <= ev["trailHigh"] * (1 - trail / 100):
                reason = f"peak passed ({ev['trailHigh']:.4g})"
            else:
                ev["note"] = f"riding the recovery: peak {ev['trailHigh']:.4g}, sells {trail:g}% under it"
        elif not burst and start > fill * 1.2 and price >= fill + recover / 100 * (start - fill):
            if trail > 0:
                ev["trailHigh"] = price
                ev["note"] = f"recovered: riding it, sells {trail:g}% under the peak"
            else:
                reason = "recovered"
        if reason is None and now - mg["entryT"] > _num(c.get("resetHoldMin"), 240) * 60000:
            reason = "max hold"
        if reason is None and ev["stackOpen"] and buy_ready and not bot.pending_open("buy") and not bot.pending_for(s):
            if bot.add_position(s, "event", c["eventPercent"],
                                f"reset: buy more at {price:.4g} ({price / start:.2f}x the old price {start:.4g})",
                                edge=max(1.0, start / price - 1)):
                ev["adds"] = ev.get("adds", 0) + 1
                ev["note"] = f"buying more ({ev['adds']})"
            elif bot.refusal:
                ev["note"] = f"not buying more: {bot.refusal}"
            return
        if reason and bot.close_position(s, f"reset: {reason} ({(price / fill - 1) * 100:+.0f}%)", urgent=True,
                                          target=reason == "recovered"):
            ev.update(phase="selling", note=reason)
        return

    if ev["phase"] == "selling":
        if mg is not None:
            if mg["state"] == "open":
                ev["phase"] = "long"            # the sell failed: decide again
            return
        again = allow and c.get("resetRebuy") and not ev.get("rebuys") \
            and now - at < _num(c.get("resetRebuyMin"), 10) * 60000
        ev.update(phase="again" if again else "done", note="watching for a second entry" if again else "sold")
