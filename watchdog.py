#!/usr/bin/env python3
r"""Watchdog for the Screen Stocks trading agent (stockbot.py): keeps one bot per port running unattended.

  pythonw watchdog.py --port 8641 -- --data "data"           watch / start / restart the bot
  pythonw watchdog.py --port 8641 --open -- --data "data"    same, and open the dashboard
  pythonw watchdog.py --port 8641 --stop                                    stop the watchdog and its bot
  (run_watchdog.vbs takes the same arguments and starts this without a window.)

Everything after "--" goes to stockbot.py, except that the watchdog sets --port and --data itself and adds or
drops --arm, so a restarted bot comes back armed or disarmed exactly as it was before.

Health, every 10 s, from GET /api/state:
  * HTTP: unhealthy after more than 30 s without a good answer (a hung tick holds the bot's lock, so /api/state
    hangs too).
  * Tick loop: /api/state has no tick counter ("now" is stamped by the HTTP thread on every request). Only the tick
    loop re-reads market.json into the bot, so now - exportAgeSec is the writtenAtMs of the copy the bot holds.
    While the game is writing (the file is under 15 s old), a bot that hasn't picked up a newer file for more than
    60 s has a stalled tick loop. While the game isn't writing, the tick loop can't be judged and only the HTTP
    check applies (a closed game is not the bot's fault, and a restart wouldn't help).
  * Sleep / hibernate: when the watchdog itself ran more than 15 s late, both timers start over (the bot was frozen
    too, so that silence says nothing about it).
Recovery: POST /api/quit, wait up to 15 s, then terminate only the process that listens on the port AND whose
command line contains stockbot.py (checked through a handle held during the check, so the PID can't be reused),
then start a new bot. Restarts back off exponentially; at most 5 in any rolling hour, then it stops and logs loudly.
A bot that exits with code 0 after it ran was quit on purpose (dashboard Quit or stop.bat): the watchdog exits too.

Files in the bot's data folder: watchdog.log (rotating) and watchdog-status.json (format in WATCHDOG.md).
Standard library only; Windows 10/11; Python 3.10+.
"""
import argparse
import collections
import ctypes
import http.client
import json
import logging
import logging.handlers
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

VERSION = 1
HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8641
POLL_SEC = 10            # normal poll interval
FAST_POLL_SEC = 2        # while a bot starts or looks unhealthy, and while a restart's disarm is being enforced
HTTP_TIMEOUT = 8         # per /api/state request
HTTP_DEAD_SEC = 30       # no good answer for longer than this: unhealthy
STALL_SEC = 60           # the bot's copy of market.json this far behind the file on disk: tick loop stalled
FEED_FRESH_SEC = 15      # market.json older than this: the game isn't writing (the bot's own "fresh" limit)
QUIT_WAIT_SEC = 15       # after POST /api/quit
START_GRACE_SEC = 90     # a bot we just started gets this long to answer
MAX_RESTARTS = 5         # per rolling hour
WINDOW_SEC = 3600
BACKOFF_BASE = 30        # wait between restarts in a row: none before the 1st, then 30 s, 1, 2, 4 ... min
BACKOFF_MAX = 1800
HEALTHY_RESET_SEC = 600  # healthy this long: the backoff starts over
ARM_WINDOW_SEC = 660     # the bot's --arm / armOnStart thread waits up to 600 s for a live feed
DISK_WARN_BYTES = 2 * 10 ** 9   # 2 GB (MB and GB here are decimal, like the bot's diskFreeMB)
LOUD_EVERY_SEC = 1800    # repeat "giving up" / "low disk" / "port taken" this often
PAUSE_SEC = 15           # the watchdog ran this much later than planned: the PC slept (the bot was frozen too)
STATUS_NAME = "watchdog-status.json"
LOG_NAME = "watchdog.log"
BOT_MARK = "stockbot.py"  # the watchdog only ever terminates a process with this in its command line

log = logging.getLogger("watchdog")

# ---------------------------------------------------------------- Win32
k32 = ntdll = iphlp = None
if sys.platform == "win32":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    iphlp = ctypes.WinDLL("iphlpapi")
    _H = wintypes.HANDLE
    for _name, _res, _args in (
            ("CreateMutexW", _H, (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)),
            ("OpenMutexW", _H, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)),
            ("CreateEventW", _H, (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)),
            ("OpenEventW", _H, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)),
            ("SetEvent", wintypes.BOOL, (_H,)),
            ("ResetEvent", wintypes.BOOL, (_H,)),
            ("ReleaseMutex", wintypes.BOOL, (_H,)),
            ("WaitForSingleObject", wintypes.DWORD, (_H, wintypes.DWORD)),
            ("CloseHandle", wintypes.BOOL, (_H,)),
            ("OpenProcess", _H, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ("TerminateProcess", wintypes.BOOL, (_H, wintypes.UINT)),
            ("GetExitCodeProcess", wintypes.BOOL, (_H, ctypes.POINTER(wintypes.DWORD)))):
        _fn = getattr(k32, _name)
        _fn.restype, _fn.argtypes = _res, _args
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = (_H, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG))
    iphlp.GetExtendedTcpTable.restype = wintypes.DWORD
    iphlp.GetExtendedTcpTable.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                          wintypes.ULONG, ctypes.c_int, wintypes.ULONG)

ERROR_ALREADY_EXISTS, ERROR_INSUFFICIENT_BUFFER = 183, 122
SYNCHRONIZE, EVENT_MODIFY_STATE = 0x00100000, 0x0002
PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION = 0x0001, 0x1000
STILL_ACTIVE, WAIT_OBJECT_0, WAIT_ABANDONED = 259, 0, 0x80
CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW = 0x200, 0x01000000, 0x08000000
TCP_TABLE_OWNER_PID_LISTENER, PROCESS_COMMAND_LINE_INFORMATION = 3, 60


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


def mutex_name(port):
    return f"Local\\ScreenStocksWatchdog-{int(port)}"


def stop_event_name(port):
    return f"Local\\ScreenStocksWatchdogStop-{int(port)}"


def tcp_listeners(port):
    """[(pid, address)] of the IPv4 sockets listening on `port`: the Python version of `netstat -ano`."""
    size = wintypes.DWORD(64 * 1024)
    for _ in range(6):
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, socket.AF_INET,
                                        TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret == 0:
            break
        if ret != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"GetExtendedTcpTable failed ({ret})")
        size = wintypes.DWORD(size.value + 4096)
    else:
        raise OSError("GetExtendedTcpTable: the table keeps growing")
    n = wintypes.DWORD.from_buffer(buf).value
    out = []
    for r in (MIB_TCPROW_OWNER_PID * n).from_buffer(buf, 4):
        if socket.ntohs(r.dwLocalPort & 0xFFFF) == port:
            out.append((int(r.dwOwningPid), socket.inet_ntoa(struct.pack("<I", r.dwLocalAddr))))
    return out


def listener_pids(port):
    try:
        return sorted({pid for pid, _ in tcp_listeners(port)})
    except OSError as e:
        log.warning("couldn't read the TCP table: %s", e)
        return []


def open_process(pid, access):
    h = k32.OpenProcess(access, False, int(pid))
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def cmdline_of_handle(h):
    size = wintypes.ULONG(0)
    ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
    if not size.value:
        raise OSError("NtQueryInformationProcess gave no size")
    buf = ctypes.create_string_buffer(size.value + 16)
    st = ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, buf, len(buf), ctypes.byref(size))
    if st != 0:
        raise OSError(f"NtQueryInformationProcess failed (0x{st & 0xFFFFFFFF:08X})")
    us = UNICODE_STRING.from_buffer(buf)
    return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""


def cmdline_cim(pid):
    """Fallback: (Get-CimInstance Win32_Process).CommandLine."""
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       timeout=60, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    return r.stdout.decode("utf-8", "replace").strip() or None


def process_cmdline(h, pid):
    """Command line of the process behind handle h (pid for the fallback), or None if it can't be read."""
    try:
        return cmdline_of_handle(h)
    except OSError as e:
        log.info("reading pid %s's command line directly failed (%s); asking Get-CimInstance", pid, e)
    try:
        return cmdline_cim(pid)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Get-CimInstance failed for pid %s: %s", pid, e)
        return None


def cmdline_of_pid(pid):
    try:
        h = open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    except OSError:
        return None
    try:
        return process_cmdline(h, pid)
    finally:
        k32.CloseHandle(h)


def is_bot_cmdline(cmd):
    return bool(cmd) and BOT_MARK in cmd.lower()


def terminate_bot_on_port(port):
    """Terminate the process listening on `port`, but only if its command line contains stockbot.py.
    Returns (True if nothing listens on the port any more, what was done)."""
    msgs = []
    for pid in listener_pids(port):
        try:   # hold a handle: the PID can't be reused by another process while we check and terminate
            h = open_process(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
        except OSError as e:
            msgs.append(f"pid {pid}: can't open it ({e}); not touching it")
            continue
        try:
            cmd = process_cmdline(h, pid)
            if cmd is None:
                msgs.append(f"pid {pid}: couldn't read its command line; not touching it")
                continue
            if not is_bot_cmdline(cmd):
                msgs.append(f"pid {pid} is not a stockbot ({cmd[:200]}); not touching it")
                continue
            if pid not in listener_pids(port):
                msgs.append(f"pid {pid} stopped listening on its own")
                continue
            if not k32.TerminateProcess(h, 1):
                msgs.append(f"pid {pid}: TerminateProcess failed ({ctypes.WinError(ctypes.get_last_error())})")
                continue
            k32.WaitForSingleObject(h, 10000)
            msgs.append(f"terminated pid {pid} (listening on {port}; command line: {cmd[:300]})")
        finally:
            k32.CloseHandle(h)
    for _ in range(20):
        if not listener_pids(port):
            return True, "; ".join(msgs) or "nothing listens"
        time.sleep(0.25)
    return False, "; ".join(msgs) or "still listening"


# ---------------------------------------------------------------- HTTP
def probe(port, timeout=HTTP_TIMEOUT):
    """GET /api/state -> ("ok", dict) | ("refused", text) | ("bad", text)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/api/state", headers={"Connection": "close"})
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            return "bad", f"HTTP {r.status}"
        st = json.loads(body)
        if not isinstance(st, dict) or "now" not in st or "cfg" not in st:
            return "bad", "the answer is not a stockbot /api/state"
        return "ok", st
    except ConnectionRefusedError:
        return "refused", "connection refused"
    except TimeoutError:
        return "bad", f"no answer within {timeout} s"
    except (OSError, http.client.HTTPException, ValueError) as e:
        return "bad", f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def post(port, path, body, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "Connection": "close"})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def open_url(url):
    os.startfile(url)


# ---------------------------------------------------------------- helpers
def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except (OSError, TypeError, ValueError):
        return None


def wall_ms(t=None):
    return int((time.time() if t is None else t) * 1000)


def pythonw_exe():
    exe = Path(sys.executable)
    for cand in (exe.with_name("pythonw.exe"), exe):
        if cand.name.lower() == "pythonw.exe" and cand.exists():
            return str(cand)
    return shutil.which("pythonw") or sys.executable


def bot_arg_parser():
    """The bot's own options, so their values (and abbreviations) are read exactly as stockbot.py reads them."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--mods")
    p.add_argument("--data")
    p.add_argument("--port", type=int)
    p.add_argument("--arm", action="store_true")
    p.add_argument("--title")
    p.add_argument("--window-class")
    p.add_argument("--launch")
    return p


def resolve(p, base):
    q = Path(p)
    return (q if q.is_absolute() else base / q).resolve()


# ---------------------------------------------------------------- the watchdog
class Watchdog:
    def __init__(self, port, bot_path, bot_args, data_dir, mods_dir, stop_handle, open_dashboard=False,
                 backoff=BACKOFF_BASE, dropped_arm=False):
        self.port = port
        self.bot_path = bot_path
        self.bot_dir = bot_path.parent
        self.bot_args = bot_args              # pass-through, without --port/--data/--arm (added per start)
        self.data = data_dir
        self.market = mods_dir / "export" / "market.json" if mods_dir else None
        self.stop_handle = stop_handle
        self.status_path = data_dir / STATUS_NAME
        self.backoff_base = max(0.0, backoff)
        self.dropped_arm = dropped_arm
        self.open_pending = open_dashboard
        self.t0 = time.monotonic()
        self.started_wall = time.time()
        # the bot being watched
        self.child = None                     # Popen of the bot we started last
        self.bot_pid = None
        self.bot_handle = None                # handle of a bot we didn't start (for its exit code)
        self.adopted = False
        self.launches = 0
        self.launched_at = None               # monotonic
        self.launched_wall = None
        self.served = False                   # the current bot answered at least once
        self.ever_ok = False
        self.last_ok = None                   # monotonic time of the last good /api/state
        self.last_ok_wall = None
        self.bot = {}                         # summary of the last good /api/state
        self.current_at = time.time()         # last time the bot was known current with the game's export (wall)
        self.prev_seen = None                 # writtenAtMs (s) of the bot's copy of market.json at the last poll
        self.healthy_since = None
        self.feed = "unknown"
        self.tick_lag = None
        self.no_tick_field_noted = False
        self.long_probe = False               # the last /api/state request took far longer than its timeout
        self.foreign_noted = 0.0
        self.bot_err = ("", 0.0)
        # restarts
        self.restarts = []                    # monotonic times, rolling hour
        self.recent = collections.deque(maxlen=10)
        self.consecutive = 0
        self.total = 0
        self.last_restart = None
        self.next_allowed = None
        self.gave_up = False
        self.gave_up_noted = 0.0
        # armed state to restore (see update_arm)
        self.intent = None                    # True / False / None (unknown: the bot's own settings decide)
        self.intent_src = "not known yet"
        self.settled = True                   # does the running bot's armed state count as the user's choice?
        self.guard = False                    # restart of a disarmed bot: undo an armOnStart self-arm
        self.arm_done = False                 # the fallback re-arm, once per start
        # disk
        self.disk_free = None
        self.disk_low = False
        self.disk_noted = 0.0
        # state for the status file
        self.state = "starting"
        self.message = "watchdog starting"
        self.exit_reason = None
        prev = read_json(self.status_path)
        self.prev = prev if isinstance(prev, dict) else {}

    # ---- small helpers
    def now(self):
        return time.monotonic()

    def set_state(self, state, message):
        if state != self.state:
            log.info("state: %s -> %s (%s)", self.state, state, message)
        self.state, self.message = state, message

    def stop_requested(self, timeout=0.0):
        """Wait up to `timeout` s for a stop request (in 1 s pieces, so Ctrl+C still works)."""
        end = time.monotonic() + max(0.0, timeout)
        while True:
            left = end - time.monotonic()
            if k32.WaitForSingleObject(self.stop_handle, int(max(0.0, min(1.0, left)) * 1000)) == WAIT_OBJECT_0:
                return True
            if left <= 1.0:
                return False

    def bot_up_sec(self):
        return self.now() - self.launched_at if self.launched_at is not None else None

    # ---- main loop
    def run(self):
        log.info("watchdog v%s (pid %s) for port %s: bot %s, data %s, game export %s", VERSION, os.getpid(),
                 self.port, self.bot_path, self.data, self.market or "unknown")
        if self.dropped_arm:
            log.warning("--arm was dropped from the bot's arguments: the watchdog restores the last armed/disarmed "
                        "state itself, and --arm would arm the bot on every restart")
        by_request = False
        due = None                                # wall-clock time the next cycle should start
        while True:
            if due is not None and time.time() - due > PAUSE_SEC:
                self.after_pause(time.time() - due)
            started = self.now()
            try:
                self.cycle()
            except Exception:
                log.exception("watchdog cycle failed")
            try:
                self.check_disk()
            except Exception:
                log.exception("disk check failed")
            self.write_status()
            if self.exit_reason:
                break
            wait = max(0.0, self.interval() - (self.now() - started))
            due = time.time() + wait
            if self.stop_requested(wait):
                by_request = True
                break
        if by_request:
            log.warning("stop requested: stopping the bot on port %s, then the watchdog", self.port)
            self.set_state("stopping", "stop requested")
            self.write_status()
            try:
                self.shutdown_bot()
            except Exception:
                log.exception("stopping the bot failed")
            self.exit_reason = "stopped on request (--stop)"
        self.set_state("stopped", self.exit_reason)
        self.write_status()
        log.warning("watchdog exiting: %s", self.exit_reason)

    def interval(self):
        if self.state in ("starting", "unhealthy"):
            return FAST_POLL_SEC
        if not self.settled and self.guard:
            return FAST_POLL_SEC
        if self.state == "backoff" and self.next_allowed is not None:
            return max(1.0, min(POLL_SEC, self.next_allowed - self.now() + 0.5))
        return POLL_SEC

    def cycle(self):
        t = time.time()
        kind, info = probe(self.port)
        took = time.time() - t
        long_probe = took > HTTP_TIMEOUT + PAUSE_SEC
        if long_probe and not self.long_probe:   # the PC slept during the request. Only once in a row: a bot that is
            self.long_probe = True               # this slow every time is judged normally from the next request on
            self.after_pause(took - HTTP_TIMEOUT)
            if kind != "ok":
                return
        self.long_probe = long_probe
        if kind == "refused":
            pids = listener_pids(self.port)
            if pids:      # refused although something listens: its accept queue is full (a hung bot)
                kind, info = "bad", f"connection refused although pid {pids[0]} listens on the port"
        if kind == "ok":
            self.on_ok(info)
        elif kind == "refused":
            self.on_down()
        else:
            self.on_bad(info)
        if self.open_pending:
            up = self.bot_up_sec() if self.launched_at is not None else self.now() - self.t0
            if self.served or up > 60:
                self.open_dashboard()

    def after_pause(self, sec):
        """The watchdog itself didn't run for `sec` s more than planned: the PC slept or hibernated (or the clock
        jumped). The bot and the game were frozen as well, so that silence says nothing about the bot: the health
        timers start over instead of restarting a bot that is just waking up."""
        log.warning("the watchdog was paused for %.0f s (sleep, hibernate or a clock change?): the health timers "
                    "start over", sec)
        now = self.now()
        self.t0 = now
        if self.last_ok is not None:
            self.last_ok = now
        if self.launched_at is not None and not self.served:
            self.launched_at = now              # a bot that was still starting gets its full start time again
        self.current_at, self.prev_seen = time.time(), None

    # ---- a good answer
    def on_ok(self, st):
        now = self.now()
        pids = listener_pids(self.port)
        pid = pids[0] if pids else self.bot_pid
        if pid and pid != self.bot_pid:
            self.track(pid)
        if not self.served:
            up = self.bot_up_sec()
            log.info("bot pid %s is answering on port %s%s", pid, self.port,
                     f" ({up:.0f} s after it was started)" if up is not None and not self.adopted else "")
            if not self.ever_ok and self.launches == 0:
                self.adopt_arm_state(st, pid)
        self.served = self.ever_ok = True
        self.last_ok, self.last_ok_wall = now, time.time()
        cfg = st.get("cfg") if isinstance(st.get("cfg"), dict) else {}
        risk = st.get("risk") if isinstance(st.get("risk"), dict) else {}
        self.bot = {"fresh": st.get("fresh"), "exportAgeSec": st.get("exportAgeSec"), "armed": cfg.get("armed"),
                    "armOnStart": cfg.get("armOnStart"), "tripped": risk.get("tripped"),
                    "error": st.get("error") or "", "errorCount": st.get("errorCount"),
                    "restartingGame": st.get("restarting"), "diskFreeMB": st.get("diskFreeMB")}
        err = self.bot["error"]
        if err and (err != self.bot_err[0] or now - self.bot_err[1] > 600):
            log.warning("the bot reports an error: %s (errorCount %s)", err, st.get("errorCount"))
            self.bot_err = (err, now)
        self.update_arm(st, cfg, risk)
        lag = self.tick_lag = self.judge_tick(st)
        if lag is not None and lag > STALL_SEC:
            self.recover(f"tick loop stalled: the bot hasn't picked up the game's market.json for {lag:.0f} s "
                         f"although the game keeps writing it", present=True)
            return
        if self.gave_up:
            log.warning("the bot on port %s is healthy again: restarts are allowed again", self.port)
            self.gave_up = False
        self.next_allowed = None
        if self.healthy_since is None:
            self.healthy_since = now
        if self.consecutive and now - self.healthy_since >= HEALTHY_RESET_SEC:
            log.info("bot healthy for %d min: the restart backoff starts over", HEALTHY_RESET_SEC // 60)
            self.consecutive = 0
        tick = ("tick loop current" if lag is not None else
                "game not writing market.json, tick loop not judged" if self.feed == "stale" else
                "market.json not found, tick loop not judged")
        self.set_state("healthy", f"bot pid {pid} healthy; {tick}")

    def judge_tick(self, st):
        """Seconds the bot has been behind the game's market.json (0 = current), None when that can't be judged.

        The bot's copy was written at seen = now - exportAgeSec. It is current when that copy changed since the last
        poll (only the tick loop replaces it) or is within 3 s of the file on disk. Otherwise it has been behind at
        most since the last poll that found it current or found the game not writing, which keeps a poll that lands
        between the game's first write after a pause and the bot's next tick from looking like a long stall."""
        wall = time.time()
        mt = mtime(self.market) if self.market else None
        if mt is None or wall - mt > FEED_FRESH_SEC:
            self.feed = "unknown" if mt is None else "stale"
            self.current_at, self.prev_seen = wall, None      # a game that isn't writing can't be fallen behind
            return None
        self.feed = "live"
        if "exportAgeSec" not in st or not isinstance(st.get("now"), (int, float)):
            if not self.no_tick_field_noted:
                self.no_tick_field_noted = True
                log.warning("/api/state has no exportAgeSec/now: the tick loop can't be judged, only HTTP")
            return None
        age = st["exportAgeSec"]
        if age is not None and not isinstance(age, (int, float)):
            return None
        seen = st["now"] / 1000.0 - age if age is not None else None      # None: no copy at all yet
        moved = seen is not None and self.prev_seen is not None and abs(seen - self.prev_seen) > 0.5
        self.prev_seen = seen
        if moved or (seen is not None and mt <= seen + 3.0):
            self.current_at = wall
            return 0.0
        return max(0.0, wall - max(self.current_at, seen or 0.0))

    # ---- nothing listens on the port
    def on_down(self):
        up = self.bot_up_sec()
        child_alive = self.child is not None and self.child.poll() is None
        if child_alive and not self.served and up is not None and up < START_GRACE_SEC:
            self.set_state("starting", f"waiting for the bot (pid {self.child.pid}) to open port {self.port}")
            return
        code = self.exit_code()
        if code == 0 and self.served:
            self.exit_reason = (f"the bot (pid {self.bot_pid}) exited normally, so it was stopped on purpose "
                                f"(dashboard Quit or stop.bat): the watchdog stops too")
            log.warning("%s", self.exit_reason)
            return
        if self.launches == 0 and not self.ever_ok:
            reason = f"no bot on port {self.port}"
        elif code is not None:
            reason = f"the bot (pid {self.bot_pid}) exited with code {code}" + (
                "" if self.served else " before it answered")
        elif child_alive and not self.served:
            reason = (f"the bot we started (pid {self.child.pid}) didn't open port {self.port} within "
                      f"{START_GRACE_SEC} s (left alone: it can't trade without its port)")
        else:
            reason = f"nothing answers on port {self.port} any more (pid {self.bot_pid} closed it)"
        self.recover(reason, present=False)

    # ---- something listens but /api/state isn't answered properly
    def on_bad(self, info):
        now = self.now()
        up = self.bot_up_sec()
        if not self.served and up is not None and up < START_GRACE_SEC:
            self.set_state("starting", f"bot started {up:.0f} s ago, not answering yet ({info})")
            return
        self.healthy_since = None
        pids = listener_pids(self.port)
        if pids:
            cmd = cmdline_of_pid(pids[0])
            if cmd is not None and not is_bot_cmdline(cmd):
                if now - self.foreign_noted > LOUD_EVERY_SEC:
                    self.foreign_noted = now
                    log.error("port %s is taken by pid %s, which is not a stockbot: %s. Not touching it; the bot "
                              "can't start until that program frees the port", self.port, pids[0], cmd[:300])
                self.set_state("port-taken", f"port {self.port} is used by another program (pid {pids[0]})")
                return
        if self.last_ok is not None:
            ref = self.last_ok
        elif self.launched_at is not None:
            ref = self.launched_at + START_GRACE_SEC - HTTP_DEAD_SEC
        else:
            ref = self.t0
        dead = now - ref
        if dead > HTTP_DEAD_SEC:
            self.recover(f"HTTP dead for {dead:.0f} s ({info})", present=True)
        else:
            self.set_state("unhealthy", f"no good answer for {dead:.0f} s ({info})")

    # ---- recovery
    def restart_allowed(self, reason):
        now = self.now()
        self.restarts = [t for t in self.restarts if now - t < WINDOW_SEC]
        if len(self.restarts) >= MAX_RESTARTS:
            self.next_allowed = self.restarts[0] + WINDOW_SEC
            mins = (self.next_allowed - now) / 60
            if not self.gave_up or now - self.gave_up_noted >= LOUD_EVERY_SEC:
                self.gave_up_noted = now
                log.error("!!!!! GIVING UP: the bot on port %s was restarted %d times in the last hour (limit %d). "
                          "No more restarts for %.0f min. Find the cause in bot.log and this log. Problem now: "
                          "%s !!!!!", self.port, len(self.restarts), MAX_RESTARTS, mins, reason)
            self.gave_up = True
            self.set_state("gave-up", f"{len(self.restarts)} restarts in the last hour; none for {mins:.0f} min "
                                      f"({reason})")
            return False
        if self.last_restart is not None and self.consecutive > 0:
            wait = min(BACKOFF_MAX, self.backoff_base * 2 ** (self.consecutive - 1))
            if now < self.last_restart + wait:
                self.next_allowed = self.last_restart + wait
                self.set_state("backoff", f"restart {self.consecutive + 1} in a row in "
                                          f"{self.next_allowed - now:.0f} s ({reason})")
                return False
        self.next_allowed = None
        return True

    def recover(self, reason, present):
        self.healthy_since = None
        initial = self.launches == 0 and not self.ever_ok and not present
        if initial:
            log.info("%s: starting it", reason)
        else:
            if not self.restart_allowed(reason):
                return
            now = self.now()
            self.restarts.append(now)
            self.recent.append({"at": wall_ms(), "reason": reason})
            self.consecutive += 1
            self.total += 1
            self.last_restart = now
            log.warning("UNHEALTHY: %s -> restart (%d of %d allowed per hour)", reason, len(self.restarts),
                        MAX_RESTARTS)
        self.set_state("restarting", reason)
        self.write_status()
        if present and not self.stop_bot():
            return
        if self.stop_requested(0):
            return                              # the main loop handles the stop
        if initial:
            self.initial_arm_intent()
        elif self.settled:
            self.refine_intent_from_state_json()
        self.launch(reason, restart=not initial)

    def stop_bot(self):
        """POST /api/quit, wait up to 15 s, then terminate the verified listener. True when the port is free."""
        if not listener_pids(self.port):
            return True
        try:
            code, _ = post(self.port, "/api/quit", {})
            log.info("POST /api/quit -> HTTP %s", code)
        except (OSError, http.client.HTTPException) as e:
            log.info("POST /api/quit failed: %s", e)
        end = self.now() + QUIT_WAIT_SEC
        while self.now() < end:
            if not listener_pids(self.port):
                log.info("the bot left port %s after the quit request", self.port)
                return True
            time.sleep(0.5)
        log.warning("the bot still listens %d s after the quit request: terminating it", QUIT_WAIT_SEC)
        ok, msg = terminate_bot_on_port(self.port)
        (log.warning if ok else log.error)("%s", msg)
        if not ok:
            self.set_state("port-taken", f"couldn't free port {self.port}: {msg}")
        return ok

    def shutdown_bot(self):
        """Stop on request: quit the bot (terminate it if it won't), including one we started that is still loading."""
        c = self.child
        if c is not None and c.poll() is None and not listener_pids(self.port):
            end = self.now() + 20               # just started: let it open its port, then quit it properly
            while self.now() < end and c.poll() is None and not listener_pids(self.port):
                time.sleep(0.5)
        self.stop_bot()
        if c is not None and c.poll() is None:
            try:
                c.wait(10)
            except subprocess.TimeoutExpired:
                if is_bot_cmdline(process_cmdline(int(c._handle), c.pid)):
                    c.terminate()
                    log.warning("terminated the bot we started (pid %s); it never opened its port", c.pid)

    def launch(self, reason, restart):
        now = self.now()
        cmd = [pythonw_exe(), str(self.bot_path), "--port", str(self.port), "--data", str(self.data), *self.bot_args]
        if self.intent:
            cmd.append("--arm")
        kw = dict(cwd=str(self.bot_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
        self.launches += 1
        self.launched_at, self.launched_wall = now, time.time()
        self.served = False
        self.last_ok = self.last_ok_wall = None
        self.current_at, self.prev_seen = time.time(), None
        self.arm_done = False
        self.guard = self.intent is False and (restart or self.intent_src.startswith("previous watchdog"))
        self.settled = self.intent is None or (self.intent is False and not self.guard)
        try:
            try:      # leave any job object, so the bot outlives whatever ends the watchdog
                child = subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                child = subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP, **kw)
        except OSError as e:
            log.error("couldn't start the bot (%s): %s", subprocess.list2cmdline(cmd), e)
            self.child = None
            self.set_state("restarting", f"couldn't start the bot: {e}")
            return
        self.close_bot_handle()
        self.child, self.bot_pid, self.adopted = child, child.pid, False
        what = "restarted" if restart else "started"
        arm = {True: "re-arms it (it was armed)", False: "keeps it disarmed (it was disarmed)",
               None: "armed state unknown, so the bot's own settings decide"}[self.intent]
        (log.warning if restart else log.info)("%s the bot: pid %s; %s [from %s]; %s", what, child.pid, arm,
                                               self.intent_src, subprocess.list2cmdline(cmd))
        self.set_state("starting", f"{what} the bot (pid {child.pid}): {reason}")

    # ---- the bot's process (for its exit code)
    def track(self, pid):
        """Watch the process now serving the port (one we started, or one that was already running)."""
        if self.child is not None and self.child.pid == pid:
            self.bot_pid, self.adopted = pid, False
            return
        self.close_bot_handle()
        self.bot_pid, self.adopted = pid, True
        try:
            self.bot_handle = open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError as e:
            log.info("can't open pid %s (%s): a deliberate quit can't be told from a crash", pid, e)
        cmd = (cmdline_of_pid(pid) or "command line unknown")[:300]
        if self.launches or self.ever_ok:
            log.warning("port %s is now served by pid %s, which this watchdog didn't start: watching it (%s)",
                        self.port, pid, cmd)
            self.settled, self.guard = True, False     # someone started it by hand: its armed state is theirs
        else:
            log.info("adopted the bot that was already running: pid %s (%s)", pid, cmd)

    def close_bot_handle(self):
        if self.bot_handle:
            k32.CloseHandle(self.bot_handle)
            self.bot_handle = None

    def exit_code(self):
        """Exit code of the watched bot; None if unknown or still running."""
        if self.child is not None and self.child.pid == self.bot_pid:
            try:
                return self.child.wait(3)
            except subprocess.TimeoutExpired:
                return None
        h = self.bot_handle
        if not h or k32.WaitForSingleObject(h, 3000) != WAIT_OBJECT_0:
            return None
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return code.value
        return None

    # ---- armed / disarmed across restarts
    def state_json(self):
        p = self.data / "state.json"
        d = read_json(p)
        if isinstance(d, dict) and isinstance(d.get("armed"), bool):
            return d["armed"], mtime(p)
        return None, None

    def initial_arm_intent(self):
        """Before the first start: the armed state the bot saved last, unless the previous watchdog run ended while
        a restarted bot hadn't reached its old state yet (that bot's state.json says nothing about the user)."""
        armed, mt = self.state_json()
        pa = self.prev.get("arm") if isinstance(self.prev.get("arm"), dict) else {}
        upd = self.prev.get("updatedAt")
        if (pa.get("settled") is False and isinstance(pa.get("restore"), bool) and isinstance(upd, (int, float))
                and (mt is None or mt <= upd / 1000 + 30)):
            self.intent, self.intent_src = pa["restore"], "previous watchdog run (its restart hadn't finished)"
        elif armed is not None:
            self.intent, self.intent_src = armed, "state.json"
        else:
            self.intent, self.intent_src = None, "no state.json"

    def adopt_arm_state(self, st, pid):
        """First good answer from a bot that was already running when the watchdog started."""
        cfg = st.get("cfg") if isinstance(st.get("cfg"), dict) else {}
        pa = self.prev.get("arm") if isinstance(self.prev.get("arm"), dict) else {}
        pb = self.prev.get("bot") if isinstance(self.prev.get("bot"), dict) else {}
        if pa.get("settled") is False and pb.get("pid") == pid and isinstance(pa.get("restore"), bool):
            # the previous watchdog restarted this very bot, and it hasn't reached its old armed state yet
            self.intent, self.settled = pa["restore"], False
            self.intent_src = "previous watchdog run (its restart hadn't finished)"
            self.guard = pa.get("guard") is True
            started = pb.get("startedAt")
            if isinstance(started, (int, float)):
                self.launched_wall = started / 1000
                self.launched_at = self.now() - max(0.0, time.time() - self.launched_wall)
        else:
            self.intent, self.intent_src, self.settled = bool(cfg.get("armed")), "running bot", True

    def refine_intent_from_state_json(self):
        """Just before a restart: a state.json saved after the last good poll knows about a later arm/disarm."""
        armed, mt = self.state_json()
        if armed is None:
            return
        if self.last_ok_wall is None or mt > self.last_ok_wall:
            if armed != self.intent:
                log.info("state.json (saved after the last poll) says armed=%s", armed)
            self.intent, self.intent_src = armed, "state.json"

    @staticmethod
    def last_arm_message(st):
        for e in st.get("log") or []:
            msg = e.get("msg", "") if isinstance(e, dict) else ""
            if isinstance(msg, str) and msg.startswith("bot ON"):
                return msg
        return ""

    def set_armed(self, on):
        """keepBreaker: re-arming after a restart must not clear a tripped circuit breaker (like --arm)."""
        try:
            code, body = post(self.port, "/api/arm", {"armed": True, "keepBreaker": True} if on else {"armed": False})
            return code == 200 and bool(json.loads(body).get("armed")) == bool(on)
        except (OSError, ValueError, AttributeError, http.client.HTTPException) as e:
            log.warning("POST /api/arm failed: %s", e)
            return False

    def update_arm(self, st, cfg, risk):
        """Track the armed state the user chose (dashboard / POST /api/arm), and after a restart bring it back."""
        armed = bool(cfg.get("armed"))
        if self.settled:
            if self.intent is not None and armed != self.intent:
                log.info("the bot is now %s (dashboard): a restart will keep that",
                         "ARMED" if armed else "disarmed")
            self.intent, self.intent_src = armed, "running bot"
            return
        up = self.bot_up_sec() or 0.0
        if self.intent:            # armed before the restart: --arm re-arms it as soon as the feed is live
            if risk.get("armedAt") or armed:
                self.settled = True
                log.info("the restarted bot is %s", "armed again" if armed else
                         "disarmed again after re-arming (dashboard)")
                self.intent, self.intent_src = armed, "running bot"
            elif st.get("fresh") and up > ARM_WINDOW_SEC and not self.arm_done:
                ok = self.set_armed(True)       # the feed came back after --arm stopped waiting (600 s)
                self.arm_done = ok
                (log.warning if ok else log.error)("re-armed the bot through /api/arm (it was armed before the "
                                                   "restart; the feed went live after --arm stopped waiting)%s",
                                                   "" if ok else ": FAILED, retrying")
            return
        # disarmed before the restart: the bot's armOnStart setting must not turn that into armed
        if not cfg.get("armOnStart"):
            self.settled = True
            return
        if armed:
            why = self.last_arm_message(st)
            if "dashboard" in why:
                log.info("the restarted bot was armed from the dashboard (%s): keeping it", why)
                self.settled, self.intent, self.intent_src = True, True, "running bot"
            elif self.set_armed(False):
                log.error("the restarted bot armed itself (armOnStart is on: %s) although it was DISARMED before "
                          "the restart: disarmed it again. Turn armOnStart off while the watchdog runs this bot.",
                          why or "no reason in its log")
                self.settled = True
            else:
                log.error("the restarted bot armed itself (armOnStart) although it was disarmed before; disarming "
                          "it FAILED, retrying")
        elif up > ARM_WINDOW_SEC:
            self.settled = True

    # ---- dashboard
    def open_dashboard(self):
        self.open_pending = False
        url = f"http://127.0.0.1:{self.port}"
        try:
            open_url(url)
            log.info("opened %s", url)
        except OSError as e:
            log.warning("couldn't open %s: %s", url, e)

    # ---- disk
    def check_disk(self):
        try:
            free = shutil.disk_usage(self.data).free
        except OSError:
            self.disk_free = None
            return
        self.disk_free = free
        now = self.now()
        if free < DISK_WARN_BYTES:
            if not self.disk_low or now - self.disk_noted >= LOUD_EVERY_SEC:
                self.disk_noted = now
                log.warning("LOW DISK: only %.2f GB free on the drive of %s (warning below %.0f GB). On a full disk "
                            "the bot stops saving prices, trades and state, and it once died of 'database or disk "
                            "is full'", free / 1e9, self.data, DISK_WARN_BYTES / 1e9)
            self.disk_low = True
        elif self.disk_low:
            log.info("disk space is back: %.2f GB free on the drive of %s", free / 1e9, self.data)
            self.disk_low = False

    # ---- status file
    def status(self):
        now = self.now()

        def wall_of(t):            # monotonic -> epoch ms
            return wall_ms(time.time() + (t - now)) if t is not None else None

        healthy = self.state == "healthy"
        return {
            "format": 1,
            "updatedAt": wall_ms(),
            "state": self.state,
            "message": self.message,
            "port": self.port,
            "dashboard": f"http://127.0.0.1:{self.port}",
            "watchdog": {"pid": os.getpid(), "version": VERSION, "startedAt": wall_ms(self.started_wall),
                         "pollSec": POLL_SEC, "bot": str(self.bot_path), "dataDir": str(self.data),
                         "marketJson": str(self.market) if self.market else None},
            "bot": {"pid": self.bot_pid, "adopted": self.adopted,
                    "startedAt": wall_ms(self.launched_wall) if self.launched_wall else None,
                    "lastOkAt": wall_ms(self.last_ok_wall) if self.last_ok_wall else None,
                    "httpDeadSec": 0 if healthy else (round(now - self.last_ok, 1) if self.last_ok is not None
                                                      else None),
                    "feed": self.feed,
                    "behindSec": round(self.tick_lag, 1) if self.tick_lag is not None else None,
                    **self.bot},
            "arm": {"restore": self.intent, "settled": self.settled, "guard": self.guard, "source": self.intent_src},
            "restarts": {"lastHour": len([t for t in self.restarts if now - t < WINDOW_SEC]),
                         "maxPerHour": MAX_RESTARTS, "consecutive": self.consecutive, "total": self.total,
                         "gaveUp": self.gave_up, "nextAllowedAt": wall_of(self.next_allowed),
                         "recent": list(self.recent)},
            "disk": {"freeMB": round(self.disk_free / 1e6) if self.disk_free is not None else None,
                     "low": self.disk_low, "warnBelowMB": round(DISK_WARN_BYTES / 1e6)},
        }

    def write_status(self):
        tmp = self.status_path.with_name(STATUS_NAME + ".tmp")
        try:
            tmp.write_text(json.dumps(self.status(), indent=1), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError:            # full disk: skip this one
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- entry points
def request_stop(port):
    """--stop: ask the watchdog for `port` to stop its bot and exit; with no watchdog, quit the bot like stop.bat."""
    h = k32.OpenEventW(EVENT_MODIFY_STATE, False, stop_event_name(port))
    if not h:
        try:
            code, _ = post(port, "/api/quit", {})
            print(f"no watchdog for port {port}; asked the bot to quit (HTTP {code})")
        except (OSError, http.client.HTTPException):
            print(f"no watchdog and no bot on port {port}")
        return 0
    m = k32.OpenMutexW(SYNCHRONIZE, False, mutex_name(port))
    k32.SetEvent(h)
    k32.CloseHandle(h)
    if not m:
        print(f"asked the watchdog for port {port} to stop")
        return 0
    try:   # the watchdog owns its mutex until it exits: waiting for it is waiting for the watchdog
        r = k32.WaitForSingleObject(m, 120000)
        if r in (WAIT_OBJECT_0, WAIT_ABANDONED):
            k32.ReleaseMutex(m)
            print(f"the watchdog for port {port} has stopped")
            return 0
        print(f"the watchdog for port {port} didn't stop within 120 s")
        return 1
    finally:
        k32.CloseHandle(m)


def wait_and_open(port):
    """--open while another watchdog runs this port: open the dashboard once the bot answers (at most 60 s)."""
    end = time.monotonic() + 60
    while time.monotonic() < end and probe(port, 5)[0] != "ok":
        time.sleep(2)
    try:
        open_url(f"http://127.0.0.1:{port}")
    except OSError:
        pass


def setup_logging(data_dir):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(data_dir / LOG_NAME, maxBytes=1_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        if sys.stderr:
            print(f"can't write {data_dir / LOG_NAME}: {e}", file=sys.stderr)
    if sys.stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Keeps the Screen Stocks bot (stockbot.py) running.",
                                 epilog="Everything after -- is passed to stockbot.py. See WATCHDOG.md.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="the bot's port (default 8641)")
    ap.add_argument("--open", action="store_true", help="also open the dashboard once the bot answers")
    ap.add_argument("--stop", action="store_true", help="stop the watchdog for --port and its bot, then exit")
    ap.add_argument("--bot", default=str(HERE / "stockbot.py"), help="path of stockbot.py (default: next to this file)")
    ap.add_argument("--backoff", type=float, default=BACKOFF_BASE,
                    help=f"seconds between the 1st and 2nd restart in a row, doubling after that (default "
                         f"{BACKOFF_BASE})")
    if "--" in argv:
        i = argv.index("--")
        a, bot_args = ap.parse_args(argv[:i]), argv[i + 1:]
    else:                 # no "--": the bot's options may follow the watchdog's directly
        a, bot_args = ap.parse_known_args(argv)
    if sys.platform != "win32":
        print("watchdog.py runs on Windows only", file=sys.stderr)
        return 2
    if a.stop:
        return request_stop(a.port)

    bot_path = Path(a.bot).resolve()
    known, rest = bot_arg_parser().parse_known_args(bot_args)
    if known.port is not None and known.port != a.port:
        print(f"--port {a.port} but the bot's arguments say --port {known.port}", file=sys.stderr)
        return 2
    data_dir = resolve(known.data, bot_path.parent) if known.data else bot_path.parent / "data"
    mods_dir = resolve(known.mods, bot_path.parent) if known.mods else (
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData/LocalLow/Conradical Games/Screen Stocks/mods")
    pass_args = []
    for opt, val in (("--mods", known.mods), ("--title", known.title), ("--window-class", known.window_class),
                     ("--launch", known.launch)):
        if val is not None:
            pass_args += [opt, str(resolve(val, bot_path.parent)) if opt == "--mods" else val]
    pass_args += rest

    mutex = k32.CreateMutexW(None, True, mutex_name(a.port))     # owned until this process ends (see --stop)
    if not mutex or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if mutex:
            k32.CloseHandle(mutex)
        print(f"a watchdog for port {a.port} is already running")
        if a.open:
            wait_and_open(a.port)
        return 0
    stop_handle = k32.CreateEventW(None, True, False, stop_event_name(a.port))
    if not stop_handle:
        print("can't create the stop event", file=sys.stderr)
        return 1
    k32.ResetEvent(stop_handle)
    setup_logging(data_dir)
    if not bot_path.is_file():
        log.error("bot script not found: %s", bot_path)
        return 2
    wd = Watchdog(a.port, bot_path, pass_args, data_dir, mods_dir, stop_handle, open_dashboard=a.open,
                  backoff=a.backoff, dropped_arm=known.arm)
    try:
        wd.run()
    except KeyboardInterrupt:
        log.warning("interrupted (Ctrl+C): watchdog exiting, the bot is left running")
        wd.set_state("stopped", "interrupted; the bot was left running")
        wd.write_status()
    finally:
        k32.CloseHandle(stop_handle)
        k32.ReleaseMutex(mutex)
        k32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
