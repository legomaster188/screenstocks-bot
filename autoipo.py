"""Auto go-public (IPO): when the run's peak net worth passes the game's requirement, click Go Public in the game.

The companion plugin has no IPO command (by design), so this clicks the game's own button like a player would:
the level panel button (orange arrow), scroll the panel to the bottom, then Go Public (IPOPreviewUI.StartIPO, no
confirm dialog). Real clicks need the game in front, so it only runs when nobody has touched the keyboard or mouse
for autoIpoIdleSec, and it gives the focus back to the window that had it.

Requirement (server-set): base x mult^ipoCount. 2026-11-16 the panel showed $9.60Sx at ipoCount 4 = 3.998e18 x 7^4.
autoIpoNeedNw (> 0) overrides it. Success is checked by ipoCount going up; a failed try closes the panel and waits.
Coordinates are in a 1152-px preview of the whole (square) game window, as `kit win shot` shows it.
"""
import ctypes
import threading
import time

MENU = (677, 62)          # orange arrow: level list + Go Public panel
LIST = (577, 450)         # where to scroll the panel
GO = (577, 700)           # Go Public button (where "Reach X Net Worth To Go Public" shows until it's allowed)
CLOSE = (940, 985)        # the panel's red exit button


def _num(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_sec():
    li = _LII()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


def _rect(hwnd):
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _front(hwnd):
    u = ctypes.windll.user32
    u.keybd_event(0x12, 0, 0, 0)
    u.keybd_event(0x12, 0, 2, 0)              # an Alt tap lets SetForegroundWindow work from a background process
    u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return u.GetForegroundWindow() == hwnd


def _click(hwnd, p):
    u = ctypes.windll.user32
    x, y, w, h = _rect(hwnd)
    u.SetCursorPos(int(x + p[0] * w / 1152), int(y + p[1] * h / 1152))
    time.sleep(0.15)
    u.mouse_event(0x2, 0, 0, 0, 0)
    time.sleep(0.08)
    u.mouse_event(0x4, 0, 0, 0, 0)


def _wheel(hwnd, p, notches):
    u = ctypes.windll.user32
    x, y, w, h = _rect(hwnd)
    u.SetCursorPos(int(x + p[0] * w / 1152), int(y + p[1] * h / 1152))
    for _ in range(notches):
        u.mouse_event(0x800, 0, 0, ctypes.c_uint32(-120).value, 0)
        time.sleep(0.05)


def _shot(hwnd, tag):
    """Save the game window to bot/ipo_shots/<tag>-<time>.png (code folder, not the data drive). Never raises."""
    try:
        import os
        from PIL import ImageGrab
        x, y, w, h = _rect(hwnd)
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipo_shots")
        os.makedirs(d, exist_ok=True)
        ImageGrab.grab((x, y, x + w, y + h), all_screens=True).save(
            os.path.join(d, f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"))
    except Exception:
        pass


def need_nw(cfg, ipo_count):
    fixed = _num(cfg.get("autoIpoNeedNw"))
    if fixed > 0:
        return fixed
    return _num(cfg.get("autoIpoBase"), 3.998e18) * _num(cfg.get("autoIpoMult"), 7) ** max(0, int(ipo_count))


def step(bot):
    """Called every tick: cheap checks, the clicking runs on its own thread."""
    c = bot.cfg
    v = bot.__dict__.setdefault("ipo_view", {"peak": 0.0, "count": None, "busy": False, "nextAt": 0, "tries": 0})
    v["on"] = bool(c.get("autoIpo"))
    pl = bot.player()
    cnt = pl.get("ipoCount")
    nw = _num(pl.get("netWorth"))
    if cnt is None or nw <= 0 or not bot.fresh():
        return
    if cnt != v["count"]:                       # a new run: its peak starts over
        v.update(count=cnt, peak=0.0, tries=0, nextAt=0)
    v["peak"] = max(v["peak"], nw)
    v["need"] = need_nw(c, cnt)
    if v["peak"] < v["need"]:
        v.pop("eligibleAt", None)
        v["why"] = f"needs peak net worth {v['need']:.3g} (has {v['peak']:.3g})"
        return
    v.setdefault("eligibleAt", time.time())
    if not v["on"] or v["busy"] or time.time() < v["nextAt"]:
        return
    # the clicks need the game in front, so wait for a pause in the owner's typing and clicking; the longer the IPO
    # is overdue the shorter the pause (2026-11-16: the owner used the PC for the whole 42 min the 5th IPO was due
    # and a fixed 60 s idle never came). Whatever was in front gets the focus back after ~5 s.
    u = ctypes.windll.user32
    late = (time.time() - v["eligibleAt"]) / 60
    want = _num(c.get("autoIpoIdleSec"), 60)
    if late >= _num(c.get("autoIpoForceMin"), 10):
        want = min(want, 3)
    elif late >= 3:
        want = min(want, 15)
    if u.GetForegroundWindow() == u.FindWindowW(None, "Screen Stocks"):
        want = min(want, 3)                     # the game is already in front: no focus to steal
    if idle_sec() < want:
        v["why"] = f"due {late:.0f} min: waiting for {want:.0f} s without keyboard/mouse input"
        return
    v["busy"] = True
    threading.Thread(target=_go, args=(bot, v, cnt), name="auto-ipo", daemon=True).start()


def _go(bot, v, cnt):
    u = ctypes.windll.user32
    try:
        hwnd = u.FindWindowW(None, "Screen Stocks")
        if not hwnd:
            v["why"] = "game window not found"
            v["nextAt"] = time.time() + 300
            return
        prev = u.GetForegroundWindow()
        v["tries"] += 1
        bot.note(f"🏦 going public: peak net worth {v['peak']:.3g} ≥ {v['need']:.3g} (try {v['tries']})", "event")
        ok = False
        try:
            if not _front(hwnd):
                v["why"] = "could not bring the game to the front (locked screen?)"
            else:
                _click(hwnd, MENU)
                time.sleep(1.5)
                _wheel(hwnd, LIST, 40)
                time.sleep(1.0)
                _click(hwnd, GO)
                for _ in range(40):                 # the export shows the new ipoCount within a few seconds
                    time.sleep(0.5)
                    if bot.player().get("ipoCount") not in (None, cnt):
                        ok = True
                        break
                if not ok:
                    _shot(hwnd, "fail")             # 2026-11-17 00:30 a try failed with no clue why; keep the evidence
                    _click(hwnd, CLOSE)
                    v["why"] = "clicked Go Public but the IPO count did not change"
        finally:
            if prev and prev != hwnd:
                time.sleep(0.3)
                u.SetForegroundWindow(prev)
        if ok:
            v["why"] = f"went public (IPO {bot.player().get('ipoCount')})"
            v["last"] = int(time.time() * 1000)
            bot.note(f"🏦 went public: IPO count {cnt} → {bot.player().get('ipoCount')}", "event")
        else:
            v["nextAt"] = time.time() + (600 if v["tries"] < 3 else 3600)
            bot.note(f"go public failed: {v.get('why')}; next try in {600 if v['tries'] < 3 else 3600} s", "warn")
    except Exception as e:                          # never take the bot down
        v["why"] = f"error: {e}"
        v["nextAt"] = time.time() + 600
    finally:
        v["busy"] = False
