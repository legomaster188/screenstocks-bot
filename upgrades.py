"""Smart auto-upgrades: buy the game upgrade that pays for itself fastest, judged from the bot's own profits.

Needs the companion plugin: it exports mods/export/plugin.json (upgrades[], effective{}) and buys a level when the
bot sends {"action": "buyUpgrade", "stat": ..., "count": 1}.  The plugin's own rule (cheapest stat, only when no
position is open) is switched off while this runs.

Gain a minute of one more level, measured over the last upgradeLookMin minutes of the bot's trades:
  StockVolume      profit of the entries the volume cap held back x (next / now volume multiplier - 1)
  BuyCooldown      long-entry profit x (now / next cooldown - 1) x how busy the buy slot was
  ShortCooldown    the same for short entries
  DividendGain     the plugin's payback (it knows the dividend rate and the game's formula)
  IncomePerMinute  the plugin's payback
Offline earnings and auto actions are worth nothing to a bot that is always online.
A level is bought when its payback (price / gain a minute) is at most upgradeMaxPaybackMin and its price at most
upgradeMaxCashPct of cash: the fastest payback first, one level per upgradeEverySec, never while an event trade
(a reset or a crash) needs the cash.
"""

STATS = ("StockVolume", "BuyCooldown", "ShortCooldown", "DividendGain", "IncomePerMinute")
BUSY_EVENT = ("pre", "short", "covering", "wait", "long", "selling", "again", "rebound_wait", "rebound")


def _num(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x and abs(x) != float("inf") else default


BUCKET_MIN = 10


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return 0.0 if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def measure(trades, now_ms, look_min, buy_cd, short_cd):
    """Profit a minute and slot use over the last look_min minutes (parks left out: their money is dividends).
    Profit a minute is the median of 10-minute slices: one freak trade (a +85B short) must not make every upgrade
    look twice as good as it is."""
    since = now_ms - look_min * 60000
    nb = max(1, int(look_min // BUCKET_MIN))
    lb, sb = [0.0] * nb, [0.0] * nb
    long_p = short_p = 0.0
    opens = {"buy": 0, "short": 0}
    capped = 0
    for t in trades:
        if _num(t.get("t")) < since or (t.get("status") or "done") != "done" or t.get("kind") == "park":
            continue
        a = str(t.get("action") or "")
        if t.get("source") not in (None, "bot"):
            continue
        if a.startswith(("buy", "short")):
            opens["buy" if a.startswith("buy") else "short"] += 1
            if "volume" in str(t.get("why") or ""):          # "volume binds: ..." or "sized N% (volume cap ...)"
                capped += 1
        elif t.get("pnl") is not None:
            i = min(nb - 1, int((now_ms - _num(t.get("t"))) // (BUCKET_MIN * 60000)))
            if a.startswith("sell"):
                long_p += _num(t.get("pnl"))
                lb[i] += _num(t.get("pnl"))
            else:
                short_p += _num(t.get("pnl"))
                sb[i] += _num(t.get("pnl"))
    n = opens["buy"] + opens["short"]
    secs = look_min * 60.0
    return {"longPerMin": _median(lb) / BUCKET_MIN, "shortPerMin": _median(sb) / BUCKET_MIN,
            "longPerMinMean": long_p / look_min, "shortPerMinMean": short_p / look_min,
            "cappedFrac": capped / n if n else 0.0, "opens": opens,
            "buyBusy": min(1.0, opens["buy"] * buy_cd / secs) if buy_cd else 0.0,
            "shortBusy": min(1.0, opens["short"] * short_cd / secs) if short_cd else 0.0}


def candidates(plugin, m, cash, cfg):
    """[(payback min, stat, price, gain a minute, note)] for every level that could be bought now, best first."""
    eff = plugin.get("effective") or {}
    vol = eff.get("volume") or {}
    max_cash = _num(cfg.get("upgradeMaxCashPct"), 40) / 100 * cash
    out = []
    for u in plugin.get("upgrades") or []:
        stat = u.get("stat")
        if stat not in STATS or u.get("maxed") or u.get("pending") or u.get("buyable") is False:
            continue
        price = _num(u.get("price"), -1)
        if price <= 0:
            continue
        gain, note = 0.0, ""
        if stat == "StockVolume":
            now_x, nxt_x = _num(vol.get("upgradeMultiplier"), 1), _num(vol.get("nextUpgradeMultiplier"))
            if now_x > 0 and nxt_x > now_x:
                prof = max(0.0, m["longPerMin"]) + max(0.0, m["shortPerMin"])
                gain = prof * m["cappedFrac"] * (nxt_x / now_x - 1)
                note = f"volume x{now_x:.3g} -> x{nxt_x:.3g}, {m['cappedFrac'] * 100:.0f}% of entries capped"
                park = _num(m.get("parkDivPerMin"))
                if park > 0:                     # parks that top up hold that much more of their capped stocks
                    gain += park * (nxt_x / now_x - 1)
                    note += f", parks +{park * (nxt_x / now_x - 1):,.0f}/min"
        elif stat in ("BuyCooldown", "ShortCooldown"):
            cur, nxt = _num(u.get("value")), _num(u.get("nextValue"))
            side = "long" if stat == "BuyCooldown" else "short"
            busy = m["buyBusy"] if side == "long" else m["shortBusy"]
            if cur > 0 and 0 < nxt < cur:
                gain = max(0.0, m[side + "PerMin"]) * (cur / nxt - 1) * busy
                note = f"cooldown {cur:.4g}s -> {nxt:.4g}s, slot busy {busy * 100:.0f}%"
        else:
            pb = u.get("paybackMin")
            if pb is not None and _num(pb) > 0:
                gain = price / _num(pb)
                note = "the plugin's payback"
        if gain <= 0:
            continue
        out.append((price / gain, stat, price, gain, note + ("" if price <= max_cash else ", over the cash limit")))
    out.sort()
    return out


def step(bot):
    """Called every tick while the bot is on: buy at most one level when one pays back fast enough."""
    c = bot.cfg
    view = bot.__dict__.setdefault("upgrade_view", {"on": False})
    view["on"] = bool(c.get("autoUpgrade") and c.get("smartUpgrade"))
    if not view["on"]:
        return
    pl = bot.plugin if isinstance(bot.plugin, dict) else None
    if pl is None or not bot.plugin_fresh():
        view["why"] = "needs the companion plugin running in the game"
        return
    t = _num(pl.get("writtenAtMs")) or 0
    now = bot.server_now()
    if t <= view.get("lastPluginMs", 0):            # wait for a fresh export after each look
        return
    view["lastPluginMs"] = t
    pd = bot.player()
    eff = pl.get("effective") or {}
    buy_cd = _num(eff.get("buyCooldownSeconds")) or _num(pd.get("buyCooldownSeconds"))
    short_cd = _num(eff.get("shortCooldownSeconds")) or _num(pd.get("shortCooldownSeconds"))
    look = max(30.0, _num(c.get("upgradeLookMin"), 120))
    m = measure(list(bot.trades), now, look, buy_cd, short_cd)
    if c.get("parkTopUp") and hasattr(bot, "_parked_view"):
        try:
            m["parkDivPerMin"] = _num(bot._parked_view(bot.prices(), bot.net_worth()).get("divPerMin"))
        except Exception:
            m["parkDivPerMin"] = 0.0
    cash = bot.cash()
    cands = candidates(pl, m, cash, c)
    view.update(measured=m, candidates=[{"stat": s, "price": p, "gainPerMin": g, "paybackMin": pb, "note": n}
                                        for pb, s, p, g, n in cands[:9]])
    # a tripped breaker blocks trades, not upgrades: an upgrade is not a bet, and after an IPO a halt of hours
    # left every 4 s payback level unbought (2026-11-15 13:29-15:52)
    import time as _t
    if _t.time() * 1000 < view.get("nextAt", 0):
        return
    if any(u.get("pending") for u in pl.get("upgrades") or []):
        view["why"] = "waiting for the last purchase"
        return
    if any(e.get("reset") and e.get("phase") in BUSY_EVENT for e in bot.events.values()):
        # a crash trade needs the cash, unless the cash over what the largest crash buy could use is plenty
        try:
            free = cash - bot.crash_reserve(bot.stock_info(), bot.prices())
        except Exception:
            free = 0.0
        # a level that costs a sliver of net worth can't starve a crash buy (after an IPO the $WHALE reserve is
        # over the whole cash, so this gate alone held every upgrade back while any reset traded)
        tiny = cands and cands[0][2] <= _num(c.get("upgradeEventMaxNwPct"), 1) / 100 * max(0.0, bot.net_worth())
        if not cands or (free < 10 * cands[0][2] and not tiny):
            view["why"] = "an event trade needs the cash"
            return
    max_pb = _num(c.get("upgradeMaxPaybackMin"), 120)
    max_cash = _num(c.get("upgradeMaxCashPct"), 40) / 100 * cash
    hour_ago = _t.time() * 1000 - 3600000
    spent = [x for x in view.get("spent", []) if x[0] >= hour_ago]
    view["spent"] = spent
    budget = _num(c.get("upgradeMaxSpendPctHour"), 10) / 100 * max(0.0, bot.net_worth()) - sum(x[1] for x in spent)
    view["budgetLeft"] = budget
    pick = next((x for x in cands if x[0] <= max_pb and x[2] <= max_cash and x[2] <= budget), None)
    if pick is None:
        if not cands:
            view["why"] = "no upgrade would add profit yet"
        elif cands[0][0] <= max_pb and cands[0][2] > budget:
            view["why"] = (f"best is {cands[0][1]} ({cands[0][0]:.0f} min payback): this hour's upgrade budget "
                           f"({c.get('upgradeMaxSpendPctHour', 10)}% of net worth) is used up")
        elif cands[0][0] <= max_pb:
            view["why"] = f"best is {cands[0][1]} ({cands[0][0]:.0f} min payback) but it costs over {c.get('upgradeMaxCashPct', 40)}% of cash"
        else:
            view["why"] = f"best is {cands[0][1]}: pays back in {cands[0][0]:.0f} min (limit {max_pb:.0f})"
        return
    pb, stat, price, gain, note = pick
    nw = max(0.0, bot.net_worth())
    fast = pb <= _num(c.get("upgradeFastPaybackMin"), 5) and price <= 0.001 * nw
    count, cost = 1, price
    if fast:
        # several levels in one order while they stay cheap: each costs at most x1.3 the one before (the
        # cooldown stats; StockVolume x1.05), so the sum is bounded by the budget, the cash cap and 0.5 % of NW
        cap = min(budget, max_cash, 0.005 * nw)
        nxt = price
        while count < int(_num(c.get("upgradeMaxBatch"), 10)):
            nxt *= 1.3
            if cost + nxt > cap:
                break
            count, cost = count + 1, cost + nxt
    code, out = bot.plugin_command({"action": "buyUpgrade", "stat": stat, "count": count})
    every = max(10.0, _num(c.get("upgradeEverySec"), 30))
    # after an IPO every level costs next to nothing and pays back in seconds: don't wait 30 s per level
    if fast:
        every = max(2.0, _num(c.get("upgradeFastEverySec"), 3))
    view["nextAt"] = _t.time() * 1000 + every * 1000
    if code == 200:
        view.setdefault("spent", []).append((_t.time() * 1000, cost))
        view["last"] = {"t": int(_t.time() * 1000), "stat": stat, "price": price, "paybackMin": pb, "note": note}
        view["why"] = f"bought {stat}"
        bot.note(f"⬆ upgrade {stat}{f' x{count}' if count > 1 else ''} for {cost:,.0f}: pays back in {pb:.0f} min ({note})", "event")
    else:
        view["why"] = f"could not send the purchase: {out.get('error')}"
