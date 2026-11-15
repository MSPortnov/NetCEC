import asyncio
import base64
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import decky

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
USB_PATH = "/sys/bus/usb/devices"
_last_resume = 0.0
_last_renew = 0.0
_lang = "en"  # UI language for backend messages, refreshed on every _load()
_stop = threading.Event()  # set on unload so worker threads end quickly


class AuthError(Exception):
    """TV rejected our credentials (pairing missing, expired or removed on the TV)."""


DEFAULTS = {
    # TV (Sony Bravia)
    "ip": "",
    "auth_cookie": "",         # set by PIN pairing
    "mac": "",                 # learned automatically, used for Wake-on-LAN
    "hdmi_port": 1,
    "wake_tv": True,           # what to do on start/resume
    "switch_input": True,
    "suspend_action": "none",  # none | off | home
    "shutdown_action": "none",  # none | off (power off only, never on reboot)
    "off_only_if_active": True,  # don't power off if TV is on another input
    # USB wake sources: {"vid:pid": true|false}
    "wake_rules": {},
    "lang": "en",              # resolved UI language, written by the frontend
    "lang_pref": "auto",       # auto | en | ru
}


PRIVATE = ("auth_cookie", "mac")  # stay in the backend, never sent to / accepted from the UI


def _load() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s = {**DEFAULTS, **json.load(f)}
    except Exception:
        s = dict(DEFAULTS)
    global _lang
    _lang = s.get("lang") or "en"
    return s


def _save(settings: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    clean = {k: settings.get(k, v) for k, v in DEFAULTS.items()}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def _log(msg):
    try:
        decky.logger.info(msg)
    except Exception:
        pass


def _clocks():
    """(boottime, monotonic). BOOTTIME keeps counting during suspend, MONOTONIC does not."""
    return time.clock_gettime(time.CLOCK_BOOTTIME), time.monotonic()


def _ok(msg):
    return {"ok": True, "message": msg}


def _fail(msg):
    return {"ok": False, "message": msg}


TR = {
    "en": {
        "need_ip": "Set the TV IP address first",
        "need_auth": "Pair with the TV first (Show PIN on TV)",
        "need_pair": "Pairing lost or expired. Pair with the TV again (PIN)",
        "not_paired": "Not paired",
        "already_on": "TV is already on",
        "no_wake": "TV did not wake (check Remote start / network standby)",
        "switched": "Switched to HDMI {port}",
        "switch_failed": "Input switch failed: {err}",
        "not_switched": "TV did not switch to HDMI {port}",
        "what_unknown": "unknown input",
        "what_other": "another input",
        "left_on": "TV left on ({what})",
        "turning_off": "TV is turning off",
        "off_failed": "Power off failed: {err}",
        "not_configured": "Not configured",
        "tv_http": "TV error: HTTP {code}",
        "no_conn": "No connection: {err}",
        "tv_state": "TV: {st}",
        "st_standby": "standby",
        "st_activating": "starting",
        "tv_on": "TV: on · {where}",
        "where_unknown": "input unknown",
        "where_other": "other input/app",
        "pin_enter": "Enter the PIN shown on the TV",
        "paired": "Paired",
        "pair_unexpected": "Unexpected reply ({code}). On the TV set IP control authentication to Normal",
        "pair_need_pin": "Enter the TV IP and the PIN",
        "pair_wrong": "Wrong PIN. Press Show PIN again",
        "pair_failed": "Pairing failed (HTTP {code})",
        "wake_failed": "Wake settings not applied (root needed?)",
        "part_tv": "TV",
        "part_wake": "Wake sources",
        "error": "error",
        "nothing": "Nothing to do",
        "dup": "Skipped (duplicate event)",
        "reboot_left": "Reboot: TV left on",
        "home_sent": "TV sent to the Android TV home screen",
        "unpaired": "Pairing removed. Set up the TV again",
    },
    "ru": {
        "need_ip": "Сначала укажите IP-адрес телевизора",
        "need_auth": "Сначала выполните сопряжение с ТВ (кнопка «Показать PIN на ТВ»)",
        "need_pair": "Сопряжение потеряно или истекло. Выполните сопряжение с ТВ заново (PIN)",
        "not_paired": "Не сопряжено",
        "already_on": "ТВ уже включён",
        "no_wake": "ТВ не включился (проверьте «Remote start» и сетевой режим ожидания)",
        "switched": "Переключено на HDMI {port}",
        "switch_failed": "Не удалось переключить вход: {err}",
        "not_switched": "ТВ не переключился на HDMI {port}",
        "what_unknown": "вход неизвестен",
        "what_other": "другой вход",
        "left_on": "ТВ оставлен включённым ({what})",
        "turning_off": "ТВ выключается",
        "off_failed": "Не удалось выключить: {err}",
        "not_configured": "Не настроено",
        "tv_http": "Ошибка ТВ: HTTP {code}",
        "no_conn": "Нет связи: {err}",
        "tv_state": "ТВ: {st}",
        "st_standby": "в режиме ожидания",
        "st_activating": "запускается",
        "tv_on": "ТВ: включён · {where}",
        "where_unknown": "вход неизвестен",
        "where_other": "другой вход/приложение",
        "pin_enter": "Введите PIN-код, показанный на ТВ",
        "paired": "Сопряжено",
        "pair_unexpected": "Неожиданный ответ ({code}). В настройках ТВ включите аутентификацию IP-управления «Normal»",
        "pair_need_pin": "Введите IP ТВ и PIN",
        "pair_wrong": "Неверный PIN. Нажмите «Показать PIN на ТВ» ещё раз",
        "pair_failed": "Не удалось выполнить сопряжение (HTTP {code})",
        "wake_failed": "Настройки пробуждения не применены (нужен root?)",
        "part_tv": "ТВ",
        "part_wake": "Пробуждение",
        "error": "ошибка",
        "nothing": "Нечего делать",
        "dup": "Пропущено (повторное событие)",
        "reboot_left": "Перезагрузка: ТВ оставлен включённым",
        "home_sent": "ТВ переведён на домашний экран Android TV",
        "unpaired": "Сопряжение сброшено. Настройте ТВ заново",
    },
}


def _t(key, **kw):
    return (TR.get(_lang, TR["en"]).get(key) or TR["en"][key]).format(**kw)


# ---------- subprocess helpers (Decky's bundled env breaks system binaries) ----------

def _env():
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    return env


def _exe(args):
    """Resolve the binary (Decky's PATH may lack /usr/bin) and return the full argv."""
    path = _env().get("PATH", "") + ":/usr/bin:/usr/sbin:/bin:/sbin"
    return [shutil.which(args[0], path=path) or args[0]] + list(args[1:])


def _run(args, timeout=6):
    try:
        p = subprocess.run(_exe(args), capture_output=True, text=True, timeout=timeout, env=_env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---------- Sony Bravia ----------

def _auth_headers(s):
    h = {"Content-Type": "application/json"}
    if s["auth_cookie"]:
        h["Cookie"] = s["auth_cookie"]
    return h


def _rest(s, service, method, params=None, timeout=4):
    body = json.dumps({"method": method, "id": 1, "params": params or [], "version": "1.0"}).encode()
    req = urllib.request.Request(f"http://{s['ip']}/sony/{service}", body, _auth_headers(s))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(str(e))
        raise
    if "error" in res:
        if res["error"] and res["error"][0] in (401, 403):
            raise AuthError(str(res["error"]))
        raise RuntimeError(res["error"])
    return res


def _current_port(s, timeout=4):
    """HDMI port the TV currently shows; 0 = something else; None = unknown."""
    try:
        uri = _rest(s, "avContent", "getPlayingContentInfo", timeout=timeout)["result"][0].get("uri", "")
    except AuthError:
        raise
    except Exception:
        return None
    m = re.match(r"extInput:hdmi\?port=(\d+)", uri)
    return int(m.group(1)) if m else 0


def _guess_mac(ip):
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if p[0] == ip and p[3] != "00:00:00:00:00:00":
                    return p[3]
    except Exception:
        pass
    return ""


def _wol(s):
    mac = (s.get("mac") or _guess_mac(s["ip"])).replace(":", "").replace("-", "")
    if len(mac) != 12:
        return
    packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ("255.255.255.255", 9))


def _learn_mac(s):
    if not s.get("mac"):
        mac = _guess_mac(s["ip"])
        if mac:
            cur = _load()
            cur["mac"] = mac
            _save(cur)
            s["mac"] = mac


REG_BODY = {
    "method": "actRegister", "id": 8, "version": "1.0",
    "params": [
        {"clientid": "netcec:steamos", "nickname": "NetCEC", "level": "private"},
        [{"value": "yes", "function": "WOL"}],
    ],
}


def _register(s, pin=None, cookie=None):
    """actRegister. Without PIN/cookie the TV answers 401 and shows a PIN on screen."""
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if pin is not None:
        headers["Authorization"] = "Basic " + base64.b64encode((":" + pin).encode()).decode()
    req = urllib.request.Request(
        f"http://{s['ip']}/sony/accessControl", json.dumps(REG_BODY).encode(), headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            cookies = r.headers.get_all("Set-Cookie") or []
            return 200, next((c.split(";")[0] for c in cookies if c.startswith("auth=")), "")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _store_cookie(s, cookie):
    cur = _load()
    cur["auth_cookie"] = cookie
    _save(cur)
    s["auth_cookie"] = cookie
    _learn_mac(s)  # the TV has just answered, so its MAC (for Wake-on-LAN) is in the ARP table


def _renew(s, force=False):
    """Re-register with the stored cookie (what a remote app does) to keep the pairing alive.
    Returns False only if the TV demands a new PIN. If the TV is unreachable nothing is done."""
    global _last_renew
    if not s["auth_cookie"]:
        return False
    if not force and time.monotonic() - _last_renew < 600:
        return True
    try:
        code, cookie = _register(s, None, s["auth_cookie"])
    except Exception:
        return True
    if code == 200:
        if cookie:
            _store_cookie(s, cookie)
        _last_renew = time.monotonic()
        return True
    return False


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except AuthError:
            return _fail(_t("need_pair"))
    return wrapper


def _pair_start(s):
    if not s["ip"]:
        return _fail(_t("need_ip"))
    try:
        code, cookie = _register(s)
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    if code == 401:
        return _ok(_t("pin_enter"))
    if code == 200 and cookie:
        _store_cookie(s, cookie)
        return _ok(_t("paired"))
    return _fail(_t("pair_unexpected", code=code))


def _pair_finish(s, pin):
    pin = (pin or "").strip()
    if not s["ip"] or not pin:
        return _fail(_t("pair_need_pin"))
    try:
        code, cookie = _register(s, pin)
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    if code == 200 and cookie:
        _store_cookie(s, cookie)
        return _ok(_t("paired"))
    if code in (401, 403):
        return _fail(_t("pair_wrong"))
    return _fail(_t("pair_failed", code=code))


def _tv_check(s):
    if not s["ip"]:
        return _fail(_t("need_ip"))
    if not s["auth_cookie"]:
        return _fail(_t("need_auth"))
    return None


@_guard
def _status(s):
    if not s["ip"]:
        return _fail(_t("not_configured"))
    if not s["auth_cookie"]:
        return _fail(_t("not_paired"))
    try:
        st = _rest(s, "system", "getPowerStatus")["result"][0]["status"]
    except urllib.error.HTTPError as e:
        return _fail(_t("tv_http", code=e.code))
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    if st != "active":
        return _ok(_t("tv_state", st=_t("st_" + st) if st in ("standby", "activating") else st))
    if not _renew(s):
        raise AuthError("pairing expired")
    port = _current_port(s)  # authenticated call: also validates the pairing
    where = _t("where_unknown") if port is None else (_t("where_other") if port == 0 else f"HDMI {port}")
    return _ok(_t("tv_on", where=where))


IRCC_HOME = "AAAAAQAAAAEAAABgAw=="  # remote control "Home" key


def _ircc(s, code, timeout=3):
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1"><IRCCCode>%s</IRCCCode>'
        "</u:X_SendIRCC></s:Body></s:Envelope>" % code
    ).encode()
    headers = {"Content-Type": "text/xml; charset=UTF-8",
               "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'}
    if s["auth_cookie"]:
        headers["Cookie"] = s["auth_cookie"]
    req = urllib.request.Request(f"http://{s['ip']}/sony/IRCC", body, headers)
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(str(e))
        raise


@_guard
def _leave(s, mode):
    """Fast path for sleep/shutdown (logind waits about 5 s): few round trips, short timeouts.
    mode: 'off' = power off, 'home' = Android TV home screen."""
    bad = _tv_check(s)
    if bad:
        return bad
    t0 = time.monotonic()
    if s["off_only_if_active"]:
        cur = _current_port(s, timeout=2)
        _log(f"leave: input check {time.monotonic() - t0:.2f}s -> {cur}")
        if cur != int(s["hdmi_port"]):
            label = _t("what_unknown") if cur is None else (_t("what_other") if cur == 0 else f"HDMI {cur}")
            return _ok(_t("left_on", what=label))
    try:
        if mode == "home":
            _ircc(s, IRCC_HOME, timeout=2)
            msg = _t("home_sent")
        else:
            _rest(s, "system", "setPowerStatus", [{"status": False}], timeout=2)
            msg = _t("turning_off")
    except AuthError:
        raise
    except Exception as e:
        return _fail(_t("off_failed", err=e))
    _log(f"leave {mode}: done in {time.monotonic() - t0:.2f}s")
    return _ok(msg)


def _power_state(s):
    """'active' | 'standby' | other status | None (unreachable)."""
    try:
        return _rest(s, "system", "getPowerStatus", timeout=2)["result"][0]["status"]
    except Exception:
        return None


SEND_TIMEOUT = 2    # seconds for every request to the TV while waking / switching
SWITCH_SENDS = 3    # how many times the input switch is sent before giving up
WAKE_WINDOW = 45    # how long a sleeping TV may take to wake up (s)


def _tv_fast(s):
    """Wake and/or switch the input as early as possible and stop as soon as it is confirmed.
    The input switch is tried at once (the TV is usually already on, showing its home screen);
    after every switch the TV is asked which input it shows, and nothing more is sent once it is
    on the right HDMI port. If the TV is off, it is woken (power on + Wake-on-LAN) every ~3 s
    and polled until it is active, for up to WAKE_WINDOW seconds."""
    bad = _tv_check(s)
    if bad:
        return bad
    port = int(s["hdmi_port"])
    want_sw, want_wake = s["switch_input"], s["wake_tv"]
    deadline = time.monotonic() + WAKE_WINDOW
    renewed, n, sends, last, first = False, 0, 0, "", True
    while time.monotonic() < deadline and not _stop.is_set():
        st = None
        try:
            if not (first and want_sw):  # first try: no state check, just switch
                st = _power_state(s)
            if (first and want_sw) or st == "active":
                if not want_sw:
                    return _ok(_t("already_on"))
                _rest(s, "avContent", "setPlayContent", [{"uri": f"extInput:hdmi?port={port}"}],
                      timeout=SEND_TIMEOUT)
                sends += 1
                _learn_mac(s)
                time.sleep(0.5)  # give the TV a moment to change the input
                cur = _current_port(s, timeout=SEND_TIMEOUT)
                if cur == port or cur is None:  # confirmed (None: cannot be checked, trust the reply)
                    return _ok(_t("switched", port=port))
                if sends >= SWITCH_SENDS:
                    return _fail(_t("not_switched", port=port))
                first = False
                time.sleep(0.4)
                continue
        except AuthError:
            if renewed or not _renew(s, force=True):
                raise
            renewed = True
            continue
        except Exception as e:
            last = str(e)
        first = False
        if want_wake and st != "active" and n % 7 == 0:
            try:
                _rest(s, "system", "setPowerStatus", [{"status": True}], timeout=SEND_TIMEOUT)
            except Exception:
                pass
            try:
                _wol(s)
            except Exception:
                pass
        n += 1
        time.sleep(0.4)
    return _fail(_t("no_wake")) if want_wake else _fail(_t("switch_failed", err=last))


@_guard
def _tv_actions(s):
    """Wake/switch according to settings. None = nothing configured."""
    if not s["ip"] or not (s["wake_tv"] or s["switch_input"]):
        return None
    return _tv_fast(s)


@_guard
def _tv_suspend(s):
    if not s["ip"] or s["suspend_action"] not in ("off", "home"):
        return None
    return _leave(s, s["suspend_action"])


def _is_reboot():
    rc, out = _run(["systemctl", "list-jobs", "--no-legend", "--no-pager"], timeout=2)
    return bool(re.search(r"(reboot|kexec)\.target", out))


@_guard
def _tv_shutdown(s, typ=None):
    act = s["shutdown_action"]
    if not s["ip"] or act == "none":
        return None
    reboot = (typ in ("reboot", "kexec", "soft-reboot")) if typ else _is_reboot()
    if reboot:  # the TV is only turned off on a real power-off, never on a reboot
        return _ok(_t("reboot_left"))
    return _leave(s, "off")


# ---------- sleep / shutdown helpers ----------

SLEEP_POLL = 0.5     # resume detector tick (s)
SLEEP_BUDGET = 4.0   # logind waits at most 5 s for our lock
LOCK_ARGS = [
    "systemd-inhibit", "--what=sleep:shutdown", "--who=NetCEC",
    "--why=Turn the TV off before sleep or shutdown", "--mode=delay", "sleep", "infinity",
]
MONITOR_ARGS = ["dbus-monitor", "--system", "type='signal',interface='org.freedesktop.login1.Manager'"]


def _pdeathsig():
    """Run in the child: get SIGKILL when the plugin process dies (no orphaned lock or monitor)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
    except Exception:
        pass


# ---------- USB wake sources ----------

def _rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _usb_devices():
    devs = {}
    try:
        names = sorted(os.listdir(USB_PATH))
    except Exception:
        return []
    for name in names:
        if ":" in name:
            continue  # interface entries
        d = os.path.join(USB_PATH, name)
        wk = os.path.join(d, "power", "wakeup")
        vid, pid = _rd(os.path.join(d, "idVendor")), _rd(os.path.join(d, "idProduct"))
        if not os.path.exists(wk) or not vid:
            continue
        cls = _rd(os.path.join(d, "bDeviceClass"))
        if cls == "09":  # hubs
            continue
        key = f"{vid}:{pid}"
        label = " ".join(x for x in (_rd(os.path.join(d, "manufacturer")), _rd(os.path.join(d, "product"))) if x) or key
        e = devs.setdefault(key, {"id": key, "name": label, "wake": False, "bt": cls == "e0", "paths": []})
        e["paths"].append(wk)
        if _rd(wk) == "enabled":
            e["wake"] = True
    return sorted(devs.values(), key=lambda x: (not x["bt"], x["name"].lower()))


def _usb_set(dev_id, enabled):
    n = 0
    for d in _usb_devices():
        if d["id"] == dev_id:
            want = "enabled" if enabled else "disabled"
            for p in d["paths"]:
                if _rd(p) == want:  # already as wanted: do not write
                    n += 1
                    continue
                try:
                    with open(p, "w") as f:
                        f.write(want)
                    n += 1
                except Exception:
                    pass
    return n


def _apply_wake_rules(s):
    """Re-apply the saved USB wake rules. Returns a result only if nothing could be applied."""
    rules = s.get("wake_rules") or {}
    if not rules:
        return None
    n = sum(_usb_set(k, bool(v)) for k, v in rules.items())
    return None if n else _fail(_t("wake_failed"))


def _public_usb():
    return [{k: v for k, v in d.items() if k != "paths"} for d in _usb_devices()]


# ---------- orchestration ----------

async def _resume_all():
    s = _load()
    tasks = {}
    tasks["TV"] = asyncio.to_thread(_tv_actions, s)
    tasks["Wake"] = asyncio.to_thread(_apply_wake_rules, s)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    parts, ok = [], True
    for name, r in zip(tasks, results):
        if isinstance(r, Exception):
            parts.append(f"{_t('part_' + name.lower())}: {_t('error')} {r}")
            ok = False
        elif r is not None:
            parts.append(f"{_t('part_' + name.lower())}: {r['message']}")
            ok = ok and r["ok"]
    res = {"ok": ok, "message": "; ".join(parts) if parts else _t("nothing")}
    _log(f"actions done: {res}")
    return res


class Plugin:
    _lock = None   # systemd-inhibit process holding the sleep/shutdown delay lock
    _mon = None    # dbus-monitor process (logind signals)
    _tasks = ()
    _bg = set()
    _sleep_fut = None   # sleep actions of the current sleep (shared by the Steam hook and logind)
    _sleep_ts = 0.0
    _plain_task = None
    _meta_ts = 0.0

    def _spawn(self, coro):
        t = asyncio.get_event_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def _emit(self, result):
        try:
            await decky.emit("resume_result", result)
        except Exception as e:
            _log(f"emit failed: {e}")

    # ----- settings / info
    async def get_settings(self) -> dict:
        s = _load()
        out = {k: v for k, v in s.items() if k not in PRIVATE}  # the UI never sees the pairing key
        out["paired"] = bool(s["auth_cookie"] and s["ip"])
        return out

    async def set_settings(self, patch: dict) -> None:
        s = _load()
        s.update({k: v for k, v in patch.items() if k in DEFAULTS and k not in PRIVATE})
        _save(s)
        await self._take_lock()

    async def get_info(self) -> dict:
        return {
            "root": os.geteuid() == 0,
            "monitor": bool(self._mon and self._mon.returncode is None),
            "lock": bool(self._lock and self._lock.returncode is None),
        }

    # ----- TV
    async def pair_start(self) -> dict:
        return await asyncio.to_thread(_pair_start, _load())

    async def pair_finish(self, pin: str) -> dict:
        return await asyncio.to_thread(_pair_finish, _load(), pin)

    async def tv_status(self) -> dict:
        return await asyncio.to_thread(_status, _load())

    async def tv_unpair(self) -> dict:
        s = _load()
        s.update(ip="", auth_cookie="", mac="")
        _save(s)
        return _ok(_t("unpaired"))

    # ----- USB wake sources
    async def usb_list(self) -> list:
        return await asyncio.to_thread(_public_usb)

    async def usb_set(self, dev_id: str, enabled: bool) -> list:
        s = _load()
        s["wake_rules"] = {**s["wake_rules"], dev_id: bool(enabled)}
        _save(s)
        await asyncio.to_thread(_usb_set, dev_id, bool(enabled))
        return await asyncio.to_thread(_public_usb)

    # ----- sleep / shutdown lock (systemd delay inhibitor)
    async def _take_lock(self):
        s = _load()
        need = s["suspend_action"] != "none" or s["shutdown_action"] != "none"
        if not need:
            await self._release_lock()
            return
        if self._lock and self._lock.returncode is None:
            return
        try:
            self._lock = await asyncio.create_subprocess_exec(
                *_exe(LOCK_ARGS), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=_env(),
                preexec_fn=_pdeathsig)
            _log("sleep/shutdown lock taken")
        except Exception as e:
            self._lock = None
            _log(f"could not take lock: {e}")

    async def _release_lock(self):
        p, self._lock = self._lock, None
        if p and p.returncode is None:
            try:
                p.kill()
                await asyncio.wait_for(p.wait(), 2)
            except Exception:
                pass
            _log("sleep/shutdown lock released")

    # ----- resume handling
    async def _handle_resume(self, source: str) -> dict:
        """Single entry for "the device woke up". Three sources report it: logind (the main one),
        the Steam UI event and the clock-jump detector (fallbacks). Duplicates within 20 s are dropped."""
        global _last_resume
        now = time.time()
        if now - _last_resume < 20:
            _log(f"resume ({source}) skipped: duplicate")
            return {**_ok(_t("dup")), "skipped": True}
        _last_resume = now
        self._sleep_fut, self._sleep_ts = None, 0.0  # a new sleep after this resume must not be skipped
        _log(f"resume ({source})")
        return await _resume_all()

    async def on_resume(self) -> dict:  # Steam UI event (fallback path)
        return await self._handle_resume("steam-event")

    async def _sleep_run(self, source: str):
        """Run configured TV actions before sleep."""
        s = _load()
        try:
            tv = await asyncio.to_thread(_tv_suspend, s)
        except Exception as e:
            _log(f"sleep actions ({source}): tv error: {e}")
            return _fail(str(e))
        _log(f"sleep actions ({source}): tv={tv}")
        return tv

    def _sleep_recent(self) -> bool:
        return self._sleep_fut is not None and time.time() - self._sleep_ts < 10

    async def on_suspend(self, source: str = "steam-event") -> dict:
        """Steam UI announces the sleep BEFORE logind: the network is still up here, whereas
        NetworkManager shuts it down at the same moment logind signals PrepareForSleep."""
        if self._sleep_recent():
            _log(f"suspend ({source}): skipped, already handled")
            return {**_ok(_t("dup")), "skipped": True}
        self._sleep_ts = time.time()
        self._sleep_fut = asyncio.ensure_future(self._sleep_run(source))
        r = await self._sleep_fut
        return r or _ok(_t("nothing"))

    async def _on_logind(self, kind: str, going_down: bool, typ=None):
        _log(f"logind: {kind} {'start' if going_down else 'end'}" + (f" (type={typ})" if typ else ""))
        if going_down:
            try:
                if kind == "sleep":
                    if self._sleep_recent():
                        _log("sleep: actions already started by a Steam UI hook, waiting for them")
                    else:
                        self._sleep_ts = time.time()
                        self._sleep_fut = asyncio.ensure_future(self._sleep_run("logind"))
                    r = await asyncio.wait_for(asyncio.shield(self._sleep_fut), SLEEP_BUDGET)
                else:
                    r = await asyncio.wait_for(asyncio.to_thread(_tv_shutdown, _load(), typ), SLEEP_BUDGET)
                _log(f"{kind}: {r}")
            except asyncio.TimeoutError:
                _log(f"{kind}: actions timed out")
            except Exception as e:
                _log(f"{kind}: failed: {e}")
            await self._release_lock()  # let logind continue
        elif kind == "sleep":
            await self._take_lock()
            r = await self._handle_resume("logind")
            if not r.get("skipped"):
                await self._emit(r)

    async def _handle_signal(self, member: str, lines):
        text = "".join(lines)
        m = re.search(r"boolean\s+(true|false)", text)
        if not m:
            return
        start = m.group(1) == "true"
        if member == "PrepareForSleep":
            await self._on_logind("sleep", start)
        elif member == "PrepareForShutdownWithMetadata":
            tm = re.search(r'string\s+"type"\s+variant\s+string\s+"([^"]+)"', text)
            typ = tm.group(1) if tm else None
            if typ is None:
                _log(f"shutdown metadata not parsed: {text!r}")
            self._meta_ts = time.time()
            if self._plain_task:
                self._plain_task.cancel()
            if start:
                await self._on_logind("shutdown", True, typ)
        elif member == "PrepareForShutdown" and start:
            # systemd >= 257 follows with ...WithMetadata (reboot vs poweroff); wait briefly for it
            self._plain_task = self._spawn(self._plain_shutdown())

    async def _plain_shutdown(self):
        await asyncio.sleep(0.8)
        if time.time() - self._meta_ts > 2:
            await self._on_logind("shutdown", True, None)

    async def _logind_loop(self):
        """Follow logind's PrepareForSleep / PrepareForShutdown(WithMetadata) via dbus-monitor."""
        while True:
            proc = None
            try:
                proc = self._mon = await asyncio.create_subprocess_exec(
                    *_exe(MONITOR_ARGS), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=_env(), preexec_fn=_pdeathsig)
                _log("logind watcher started")
                member, lines = None, []

                async def flush():
                    nonlocal member, lines
                    m, ls = member, lines
                    member, lines = None, []
                    if m:
                        await self._handle_signal(m, ls)

                while True:
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), 0.2)
                    except asyncio.TimeoutError:
                        await flush()  # the arguments of a signal have all arrived
                        continue
                    if not raw:
                        break
                    line = raw.decode(errors="replace")
                    if line.startswith("signal"):
                        await flush()
                        for name in ("PrepareForSleep", "PrepareForShutdownWithMetadata", "PrepareForShutdown"):
                            if f"member={name}" in line:
                                member = name
                                break
                    elif member:
                        lines.append(line)
                        if member == "PrepareForSleep" and "boolean" in line:
                            await flush()  # no further arguments: act immediately
                await flush()
                _log("logind watcher ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"logind watcher error: {e}")
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            await asyncio.sleep(5)

    async def _watch_sleep(self):
        """Fallback: BOOTTIME advances during suspend, MONOTONIC does not."""
        boot, mono = _clocks()
        while True:
            await asyncio.sleep(SLEEP_POLL)
            nboot, nmono = _clocks()
            slept = (nboot - boot) - (nmono - mono)
            boot, mono = nboot, nmono
            if slept > 8:
                _log(f"sleep detected (~{slept:.0f}s)")
                try:
                    r = await self._handle_resume("sleep-detector")
                    if not r.get("skipped"):
                        await self._emit(r)
                except Exception as e:
                    _log(f"resume handling failed: {e}")

    # ----- lifecycle
    async def _startup(self):
        # No fixed delay: _tv_fast keeps retrying until the network and the TV answer (up to 45 s)
        r = await _resume_all()
        _log(f"startup: {r}")

    async def _main(self):
        _log(f"NetCEC loaded (root={os.geteuid() == 0})")
        loop = asyncio.get_event_loop()
        self._tasks = [loop.create_task(c) for c in
                       (self._startup(), self._watch_sleep(), self._logind_loop())]
        await self._take_lock()

    async def _unload(self):
        _log("unloading: step 1 (stop flag, cancel tasks)")
        _stop.set()
        for t in list(self._tasks) + list(self._bg):
            t.cancel()
        if self._mon and self._mon.returncode is None:
            try:
                self._mon.kill()
            except Exception:
                pass
        _log("unloading: step 2 (release lock)")
        try:
            await asyncio.wait_for(self._release_lock(), 3)
        except Exception as e:
            _log(f"unloading: lock release problem: {e}")
        _log("NetCEC unloaded")
